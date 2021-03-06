# Network class called by main, with train, test functions

import json
import numpy as np
import os
import pickle
import tensorflow as tf
from tensorflow.python.client import timeline

from datasets import get_dataset
from prepare_data import SENT_BICLASS_LABEL2INT, SENT_TRICLASS_LABEL2INT, SENTIBANK_EMO_LABEL2INT, MVSO_EMO_LABEL2INT, \
    get_bc2idx
from core.image.basic_cnn import BasicVizsentCNN
from core.image.basic_plus_cnn import BasicPlusCNN
from core.image.ff_net import FFNet
from core.image.vgg.vgg16 import vgg16
from core.image.modified_alexnet import ModifiedAlexNet
from core.utils.utils import get_optimizer, load_model, save_model, setup_logging, scramble_img, scramble_img_recursively

class Network(object):
    def __init__(self, params):
        self.params = params

    ####################################################################################################################
    # Train
    ####################################################################################################################
    def train(self):
        """Train"""
        self.dataset = get_dataset(self.params)
        self.logger = self._get_logger()
        with tf.Session() as sess:
            # Get data
            self.logger.info('Retrieving training data and setting up graph')
            splits = self.dataset.setup_graph()
            tr_img_batch, self.tr_label_batch = splits['train']['img_batch'], splits['train']['label_batch']
            va_img_batch, va_label_batch = splits['valid']['img_batch'], splits['valid']['label_batch']

            # Get model
            self.output_dim = self.dataset.get_output_dim()
            model = self._get_model(sess, tr_img_batch)
            self.model = model

            # Loss
            self._get_loss(model)

            # Optimize - split into two steps (get gradients and then apply so we can create summary vars)
            optimizer = get_optimizer(self.params)
            grads = tf.gradients(self.loss, tf.trainable_variables())
            self.grads_and_vars = list(zip(grads, tf.trainable_variables()))
            train_step = optimizer.apply_gradients(grads_and_vars=self.grads_and_vars)
            # capped_grads_and_vars = [(tf.clip_by_value(gv[0], -5., 5.), gv[1]) for gv in self.grads_and_vars]
            # train_step = optimizer.apply_gradients(grads_and_vars=capped_grads_and_vars)

            # Summary ops and writer
            if self.params['tboard_debug']:
                self.img_batch_for_summ = tr_img_batch
            summary_op = self._get_summary_ops()
            tr_summary_writer = tf.summary.FileWriter(self.params['ckpt_dirpath'] + '/train', graph=tf.get_default_graph())
            va_summary_writer = tf.summary.FileWriter(self.params['ckpt_dirpath'] + '/valid')

            # Initialize after optimization - this needs to be done after adam
            coord, threads = self._initialize(sess)

            if self.params['obj'] == 'bc':
                # labelidx2filteredidx created by dataset
                labelidx2filteredidx = pickle.load(open(
                    os.path.join(self.params['ckpt_dirpath'], 'bc_labelidx2filteredidx.pkl'), 'rb'))
                filteredidx2labelidx = {v:k for k,v in labelidx2filteredidx.items()}
                bc2labelidx = get_bc2idx(self.params['dataset'])
                labelidx2bc = {v:k for k,v in bc2labelidx.items()}

            # Training
            saver = tf.train.Saver(max_to_keep=None)
            for i in range(self.params['epochs']):
                self.logger.info('Epoch {}'.format(i))
                # Normally slice_input_producer should have epoch parameter, but it produces a bug when set. So,
                num_tr_batches = self.dataset.get_num_batches('train')
                for j in range(num_tr_batches):
                    # Compute CPU GPU usage timeline
                    compute_timeline = self.params['timeline'] and (j % 100 == 0)
                    run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE) if compute_timeline else None
                    run_metadata = tf.RunMetadata() if compute_timeline else None

                    if self.params['obj'] == 'bc':       # same thing but with topk accuracy
                        _, imgs, last_fc, loss_val, acc_val,\
                        top10_indices, ids, labels,\
                        top5_acc_val, top10_acc_val, summary = sess.run(
                            [train_step, tr_img_batch, model.last_fc, self.loss, self.acc,
                             self.top10_indices, splits['train']['id_batch'], self.tr_label_batch,
                             self.top5_acc, self.top10_acc, summary_op],
                            options=run_options, run_metadata=run_metadata)

                        for loop_i, label_idx in enumerate(labels):
                            print 'Actual: {}, {}, {}. {}'.format(label_idx,
                                                                  filteredidx2labelidx[label_idx],
                                                                  labelidx2bc[filteredidx2labelidx[label_idx]],
                                                                  ids[loop_i])
                            for pred_idx in top10_indices[loop_i]:
                                print '------------> {}, {}, {}'.format(pred_idx,
                                                                   filteredidx2labelidx[pred_idx],
                                                                   labelidx2bc[filteredidx2labelidx[pred_idx]])
                    else:
                        _, imgs, last_fc, loss_val, acc_val, summary = sess.run(
                            [train_step, tr_img_batch, model.last_fc, self.loss, self.acc, summary_op],
                            options=run_options, run_metadata=run_metadata)

                    self.logger.info('Train minibatch {} / {} -- Loss: {}'.format(j, num_tr_batches, loss_val))
                    self.logger.info('................... -- Acc: {}'.format(acc_val))

                    if self.params['obj'] == 'bc':
                        self.logger.info('................. -- Top-5 Acc: {}'.format(top5_acc_val))
                        self.logger.info('................. -- Top-10 Acc: {}'.format(top10_acc_val))

                    # Write summary
                    if j % 10 == 0:
                        tr_summary_writer.add_summary(summary, i * num_tr_batches + j)

                    # if j == 10:
                    #     break



                    # Save (potentially) before end of epoch just so I don't have to wait
                    if j % 100 == 0:
                        save_model(sess, saver, self.params, i, self.logger)

                        # Create the Timeline object, and write it to a json
                        if self.params['timeline']:
                            tl = timeline.Timeline(run_metadata.step_stats)
                            ctf = tl.generate_chrome_trace_format()
                            timeline_outpath = os.path.join(self.params['ckpt_dirpath'], 'timeline.json')
                            with open(timeline_outpath, 'wb') as tfp:
                                tfp.write(ctf)

                # Evaluate on validation set (potentially)
                if (i+1) % self.params['val_every_epoch'] == 0:
                    num_va_batches = self.dataset.get_num_batches('valid')
                    for j in range(num_va_batches):
                        img_batch, label_batch = sess.run([va_img_batch, va_label_batch])
                        loss_val, acc_val, loss_summary, acc_summary = sess.run([self.loss, self.acc,
                                                                    self.loss_summary, self.acc_summary],
                                                              feed_dict={'img_batch:0': img_batch,
                                                                        'label_batch:0': label_batch})

                        self.logger.info('Valid minibatch {} / {} -- Loss: {}'.format(j, num_va_batches, loss_val))
                        self.logger.info('................... -- Acc: {}'.format(acc_val))

                        # Write summary
                        if j % 10 == 0:
                            va_summary_writer.add_summary(loss_summary, i * num_tr_batches + j)
                            va_summary_writer.add_summary(acc_summary, i * num_tr_batches + j)

                        # if j == 5:
                        #     break

                # Save model at end of epoch (potentially)
                save_model(sess, saver, self.params, i, self.logger)

            coord.request_stop()
            coord.join(threads)

    ####################################################################################################################
    # Test
    ####################################################################################################################
    def test(self):
        """Test"""
        self.dataset = get_dataset(self.params)
        self.logger = self._get_logger()
        with tf.Session() as sess:
            # Get data
            self.logger.info('Getting test set')
            if self.params['save_preds_for_prog_finetune']:
                self.logger.info('Saving predictions for prog_finetune: testing on train set')
                splits = self.dataset.setup_graph()
                te_img_batch, self.te_label_batch, te_id_batch = splits['train']['img_batch'], \
                                                                 splits['train']['label_batch'], splits['train']['id_batch']
                num_batches = self.dataset.get_num_batches('train')
                id2pred = {}
            else:
                te_img_batch, self.te_label_batch, te_id_batch = self.dataset.setup_graph()
                num_batches = self.dataset.get_num_batches('test')

            # Get model
            self.logger.info('Building graph')
            self.output_dim = self.dataset.get_output_dim()
            model = self._get_model(sess, te_img_batch)
            self.model = model

            # Loss
            self._get_loss(model)

            # Weights and gradients
            grads = tf.gradients(self.loss, tf.trainable_variables())
            self.grads_and_vars = list(zip(grads, tf.trainable_variables()))

            # Summary ops and writer
            summary_op = self._get_summary_ops()
            summary_writer = tf.summary.FileWriter(self.params['ckpt_dirpath'] + '/test', graph=tf.get_default_graph())

            # Initialize
            coord, threads = self._initialize(sess)

            # Restore model now that graph is complete -- loads weights to variables in existing graph
            self.logger.info('Restoring checkpoint')
            saver = load_model(sess, self.params)

            # print sess.run(tf.trainable_variables()[0])

            if self.params['obj'] == 'bc':
                # labelidx2filteredidx created by dataset
                labelidx2filteredidx = pickle.load(open(
                    os.path.join(self.params['ckpt_dirpath'], 'bc_labelidx2filteredidx.pkl'), 'rb'))
                filteredidx2labelidx = {v:k for k,v in labelidx2filteredidx.items()}
                bc2labelidx = get_bc2idx(self.params['dataset'])
                labelidx2bc = {v:k for k,v in bc2labelidx.items()}

            # Test
            overall_correct = 0
            overall_num = 0
            if self.params['obj'] == 'bc':
                top5_overall_correct = 0
                top10_overall_correct = 0
            for j in range(num_batches):
                if self.params['save_preds_for_prog_finetune']:
                    probs, ids, loss_val, acc_val, summary = sess.run([model.probs, te_id_batch,
                                                                       self.loss, self.acc, summary_op])
                    for k in range(len(ids)):
                        id2pred[ids[k]] = probs[k]
                elif self.params['scramble_img_mode']:
                    fn = {'uniform': scramble_img, 'recursive': scramble_img_recursively}[self.params['scramble_img_mode']]
                    img_batch, label_batch = sess.run([te_img_batch, self.te_label_batch])
                    for k in range(len(img_batch)):
                        img_batch[k] = fn(img_batch[k], self.params['scramble_blocksize'])
                    loss_val, acc_val, summary = sess.run([self.loss, self.acc, summary_op],
                                                              feed_dict={'img_batch:0': img_batch,
                                                                        'label_batch:0': label_batch})
                elif self.params['obj'] == 'bc':

                    probs, loss_val, acc_val, top5_acc_val, top10_acc_val, top10_indices,\
                        ids, labels, summary \
                        = sess.run(
                        [model.probs, self.loss, self.acc, self.top5_acc, self.top10_acc,
                         self.top10_indices, te_id_batch, self.te_label_batch, summary_op
                         ])
                    # probs, loss_val, acc_val, top5_acc_val, top10_acc_val, labels, summary = sess.run(
                    #     [model.probs, self.loss, self.acc, self.top5_acc, self.top10_acc, self.te_label_batch,
                    #      summary_op])

                    for loop_idx in range(25):
                        print probs[0][loop_idx]
                else:
                    loss_val, acc_val, summary = sess.run([self.loss, self.acc, summary_op])
                    labels, ids, last_fc, probs = sess.run([self.te_label_batch, te_id_batch, model.last_fc, model.probs])
                    for i in range(len(probs)):
                        print probs[i], labels[i], ids[i]
                    # print probs[0]

                overall_correct += int(acc_val * te_img_batch.get_shape().as_list()[0])
                overall_num += te_img_batch.get_shape().as_list()[0]
                overall_acc = float(overall_correct) / overall_num

                self.logger.info('Test minibatch {} / {} -- Loss: {}'.format(j, num_batches, loss_val))
                self.logger.info('................... -- Acc: {}'.format(acc_val))
                if self.params['obj'] == 'bc':
                    self.logger.info('................... -- Top-5 Acc: {}'.format(top5_acc_val))
                    self.logger.info('................... -- Top-10 Acc: {}'.format(top10_acc_val))
                self.logger.info('................... -- Overall acc: {}'.format(overall_acc))

                if self.params['obj'] == 'bc':
                    top5_overall_correct += int(top5_acc_val * te_img_batch.get_shape().as_list()[0])
                    top10_overall_correct += int(top10_acc_val * te_img_batch.get_shape().as_list()[0])
                    top5_overall_acc = float(top5_overall_correct) / overall_num
                    top10_overall_acc = float(top10_overall_correct) / overall_num
                    self.logger.info('................... -- Top5 overall acc: {}'.format(top5_overall_acc))
                    self.logger.info('................... -- Top10 overall acc: {}'.format(top10_overall_acc))
                    for loop_i, label_idx in enumerate(labels):
                        print 'Actual: {}, {}, {}. {}'.format(label_idx,
                                                              filteredidx2labelidx[label_idx],
                                                              labelidx2bc[filteredidx2labelidx[label_idx]],
                                                              ids[loop_i])
                        for pred_idx in top10_indices[loop_i]:
                            print '------------> {}, {}, {}'.format(pred_idx,
                                                               filteredidx2labelidx[pred_idx],
                                                               labelidx2bc[filteredidx2labelidx[pred_idx]])

                # Write summary
                if j % 10 == 0:
                    summary_writer.add_summary(summary, j)

            if self.params['save_preds_for_prog_finetune']:
                with open(os.path.join(self.params['ckpt_dirpath'], '{}-{}-id2pred.pkl'.format(
                        self.params['dataset'], self.params['obj'])), 'w') as f:
                    pickle.dump(id2pred, f, protocol=2)

            coord.request_stop()
            coord.join(threads)

    def test_bc_precatk(self):
        """
        To compare biconcept classifier against results in papers, calculate precision @ k metric for information
        retrieval. For a given bc, get the top k images with the highest score for that concept. How many of those
        top k images' label are actually that bc?
        """
        self.dataset = get_dataset(self.params)
        self.logger = self._get_logger()
        with tf.Session() as sess:
            # Get data
            self.logger.info('Getting test set')
            te_img_batch, self.te_label_batch = self.dataset.setup_graph()
            num_batches = self.dataset.get_num_batches('test')

            # Get model
            self.logger.info('Building graph')
            self.output_dim = self.dataset.get_output_dim()
            model = self._get_model(sess, te_img_batch)
            self.model = model

            # Loss
            self._get_loss(model)

            # Weights and gradients
            grads = tf.gradients(self.loss, tf.trainable_variables())
            self.grads_and_vars = list(zip(grads, tf.trainable_variables()))

            # Summary ops and writer
            summary_op = self._get_summary_ops()
            summary_writer = tf.summary.FileWriter(self.params['ckpt_dirpath'] + '/test', graph=tf.get_default_graph())

            # Initialize
            coord, threads = self._initialize(sess)

            # Restore model now that graph is complete -- loads weights to variables in existing graph
            self.logger.info('Restoring checkpoint')
            saver = load_model(sess, self.params)



            # Test
            self.logger.info('Predicting on test set')
            from collections import defaultdict
            import heapq
            class2heap = defaultdict(list)

            print self.output_dim

            for j in range(num_batches):
                last_fc, probs, labels, loss_val, acc_val, top5_acc_val, top10_acc_val, summary = sess.run(
                    [model.last_fc, model.probs, self.te_label_batch,
                     self.loss, self.acc, self.top5_acc, self.top10_acc, summary_op])

                for pt_idx in range(probs.shape[0]):
                    for class_idx in range(probs.shape[1]):
                        heapq.heappush(class2heap[labels[pt_idx]], (last_fc[pt_idx][class_idx], class_idx))

            overall_top10_correct = 0
            overall_n = 0
            class2precattopk = {}
            for class_idx, heap in class2heap.items():
                top10 = heapq.nlargest(10, heap)
                top10_correct = 0
                n = 0
                for _, pred_idx in top10:
                    if pred_idx == class_idx:
                        top10_correct += 1
                        overall_top10_correct += 1
                    n += 1
                    overall_n += 1
                class2precattopk[class_idx] = float(top10_correct) / n
                print class_idx, float(top10_correct) / n

            # print class2precattopk

            print float(overall_top10_correct) / overall_n

            coord.request_stop()
            coord.join(threads)

    ####################################################################################################################
    # Predict
    ####################################################################################################################

    def get_all_vidpaths_with_frames(self, starting_dir):
        """
        Return list of full paths to every video directory that contains frames/
        e.g. [<VIDEOS_PATH>/@Animated/@OldDisney/Feast/, ...]
        """
        vidpaths = []
        for root, dirs, files in os.walk(starting_dir):
            if 'frames' in os.listdir(root):
                vidpaths.append(root)

        return vidpaths

    # def predict(self):
    #     """Predict"""
    #     self.logger = self._get_logger()
    #     idx2label = self.get_idx2label()
    #
    #     # If given path contains frames/, just predict for that one video
    #     # Else walk through directory and predict for every folder that contains frames/
    #     dirpaths = None
    #     if os.path.exists(os.path.join(self.params['vid_dirpath'], 'frames')):
    #         dirpaths = [self.params['vid_dirpath']]
    #     else:
    #         dirpaths = self.get_all_vidpaths_with_frames(self.params['vid_dirpath'])
    #
    #     with tf.Session() as sess:
    #         # Skip if exists
    #         # if os.path.exists(os.path.join(dirpath, 'preds', 'sent_biclass_19.csv')):
    #         #     print 'Skip: {}'.format(dirpath)
    #         #     continue
    #         # Construct initial graph by using first movie as placeholder for img_batch (model requires img_batch)
    #         self.logger.info('Building graph')
    #         self.dataset = get_dataset(self.params, dirpaths[0])
    #         self.output_dim = self.dataset.get_output_dim()
    #         img_batch = self.dataset.setup_graph()
    #         model = self._get_model(sess, img_batch)
    #
    #         # # Initialize
    #         # coord, threads = self._initialize(sess)
    #
    #         # Restore model now that graph is complete -- loads weights to variables in existing graph
    #         self.logger.info('Restoring checkpoint')
    #         saver = load_model(sess, self.params)
    #
    #         for dirpath in dirpaths:
    #             self.logger.info('Getting images to predict for {}'.format(dirpath))
    #             dataset = get_dataset(self.params, dirpath)
    #             img_batch = dataset.setup_graph()
    #
    #             # Initialize - have to do this here to initialize img_batch tensor for this video
    #             coord, threads = self._initialize(sess)
    #
    #             # Make directory to store predictions
    #             preds_dir = os.path.join(dirpath, 'preds')
    #             if not os.path.exists(preds_dir):
    #                 os.mkdir(preds_dir)
    #
    #             # Write to file
    #             if self.params['load_epoch'] is not None:
    #                 fn = '{}_{}.csv'.format(self.params['obj'], self.params['load_epoch'])
    #             else:
    #                 fn = '{}.csv'.format(self.params['obj'])
    #
    #             # Predict
    #             with open(os.path.join(preds_dir, fn), 'w') as f:
    #                 labels = [idx2label[i] for i in range(self.output_dim)]
    #                 f.write('{}\n'.format(','.join(labels)))
    #                 num_batches = dataset.get_num_batches('predict')
    #                 for j in range(num_batches):
    #                     last_fc, probs = sess.run([model.last_fc, model.probs],
    #                                               feed_dict={'img_batch:0': img_batch.eval()})
    #
    #                     if self.params['debug']:
    #                         print last_fc
    #                         print probs
    #                     for frame_prob in probs:
    #                         frame_prob = ','.join([str(v) for v in frame_prob])
    #                         f.write('{}\n'.format(frame_prob))
    #
    #         coord.request_stop()
    #         coord.join(threads)
    #
    #         # # Clear previous video's graph
    #         # tf.reset_default_graph()

    def predict(self):
        """Predict"""
        self.logger = self._get_logger()

        # If given path contains frames/, just predict for that one video
        # Else walk through directory and predict for every folder that contains frames/
        dirpaths = None
        if os.path.exists(os.path.join(self.params['vid_dirpath'], 'frames')):
            dirpaths = [self.params['vid_dirpath']]
        else:
            dirpaths = self.get_all_vidpaths_with_frames(self.params['vid_dirpath'])

        for dirpath in dirpaths:
            # Skip if exists
            # if os.path.exists(os.path.join(dirpath, 'preds', 'sent_biclass_19.csv')):
            #     print 'Skip: {}'.format(dirpath)
            #     continue
            with tf.Session() as sess:
                # Get data
                self.logger.info('Getting images to predict for {}'.format(dirpath))
                self.dataset = get_dataset(self.params, dirpath)
                self.output_dim = self.dataset.get_output_dim()
                img_batch = self.dataset.setup_graph()

                # Get model
                self.logger.info('Building graph')
                model = self._get_model(sess, img_batch)

                # Initialize
                coord, threads = self._initialize(sess)

                # Restore model now that graph is complete -- loads weights to variables in existing graph
                self.logger.info('Restoring checkpoint')
                saver = load_model(sess, self.params)

                # Make directory to store predictions
                preds_dir = os.path.join(dirpath, 'preds')
                if not os.path.exists(preds_dir):
                    os.mkdir(preds_dir)

                # Predict, write to file
                idx2label = self.get_idx2label()
                num_batches = self.dataset.get_num_batches('predict')
                fn = self.params['obj']
                if self.params['load_epoch'] is not None:
                    fn += '_{}'.format(self.params['load_epoch'])
                if self.params['dropout_conf']:
                    fn += '_conf{}'.format(self.params['batch_size'])
                fn += '.csv'

                with open(os.path.join(preds_dir, fn), 'w') as f:
                    # If creating confidence intervals from dropout, each batch is one image
                    if self.params['dropout_conf']:
                        header = []
                        for i in range(self.output_dim):
                            header.extend([str(idx2label[i]) + '_mean', str(idx2label[i]) + '_std'])
                        f.write('{}\n'.format(','.join(header)))
                        for j in range(num_batches):
                            last_fc, probs = sess.run([model.last_fc, model.probs],
                                                      feed_dict={'img_batch:0': img_batch.eval()})
                            if self.params['debug']:
                                print last_fc
                                print probs
                            means = np.mean(probs, axis=0)
                            stds = np.std(probs, axis=0)
                            row = [item for sublst in zip(means, stds) for item in sublst]  # list comp for flatten
                            row = ','.join([str(v) for v in row])
                            f.write('{}\n'.format(row))
                    else:
                        header = [idx2label[i] for i in range(self.output_dim)]
                        f.write('{}\n'.format(','.join(header)))
                        for j in range(num_batches):
                            last_fc, probs = sess.run([model.last_fc, model.probs],
                                                      feed_dict={'img_batch:0': img_batch.eval()})

                            if self.params['debug']:
                                print last_fc
                                print probs
                            for frame_prob in probs:
                                frame_prob = ','.join([str(v) for v in frame_prob])
                                f.write('{}\n'.format(frame_prob))

                coord.request_stop()
                coord.join(threads)

            # Clear previous video's graph
            tf.reset_default_graph()

    def predict_bc(self):
        """
        Predict for biconcept classification. Pretty much the same as predict(), except it only saves the top k
        biconcepts. Function getting messy so just making a separate one for now.
        """
        self.logger = self._get_logger()

        # labelidx2filteredidx created by dataset
        labelidx2filteredidx = pickle.load(open(
            os.path.join(self.params['ckpt_dirpath'], 'bc_labelidx2filteredidx.pkl'), 'rb'))
        filteredidx2labelidx = {v:k for k,v in labelidx2filteredidx.items()}
        bc2labelidx = get_bc2idx(self.params['dataset'])
        labelidx2bc = {v:k for k,v in bc2labelidx.items()}

        # If given path contains frames/, just predict for that one video
        # Else walk through directory and predict for every folder that contains frames/
        dirpaths = None
        if os.path.exists(os.path.join(self.params['vid_dirpath'], 'frames')):
            dirpaths = [self.params['vid_dirpath']]
        else:
            dirpaths = self.get_all_vidpaths_with_frames(self.params['vid_dirpath'])

        for dirpath in dirpaths:
            # Skip if exists
            # if os.path.exists(os.path.join(dirpath, 'preds', 'sent_biclass_19.csv')):
            #     print 'Skip: {}'.format(dirpath)
            #     continue
            with tf.Session() as sess:
                # Get data
                self.logger.info('Getting images to predict for {}'.format(dirpath))
                self.dataset = get_dataset(self.params, dirpath)
                self.output_dim = self.dataset.get_output_dim()
                img_batch = self.dataset.setup_graph()

                # Get model
                self.logger.info('Building graph')
                model = self._get_model(sess, img_batch)

                # Initialize
                coord, threads = self._initialize(sess)

                # Restore model now that graph is complete -- loads weights to variables in existing graph
                self.logger.info('Restoring checkpoint')
                saver = load_model(sess, self.params)

                # Make directory to store predictions
                preds_dir = os.path.join(dirpath, 'preds')
                if not os.path.exists(preds_dir):
                    os.mkdir(preds_dir)

                # Predict, write to file
                num_batches = self.dataset.get_num_batches('predict')
                fn = self.params['obj']
                if self.params['load_epoch'] is not None:
                    fn += '_{}'.format(self.params['load_epoch'])
                if self.params['dropout_conf']:
                    fn += '_conf{}'.format(self.params['batch_size'])
                fn += '.csv'

                with open(os.path.join(preds_dir, fn), 'w') as f:
                    for j in range(num_batches):
                        last_fc, probs = sess.run([model.last_fc, model.probs],
                                                  feed_dict={'img_batch:0': img_batch.eval()})
                        top10_filteredidxs = np.argpartition(probs[0], -10)[-10:][::-1]
                        top10_labelidxs = [filteredidx2labelidx[idx] for idx in top10_filteredidxs]
                        top10_bc = [labelidx2bc[idx] for idx in top10_labelidxs]
                        f.write('{}\n'.format(','.join(top10_bc)))

                coord.request_stop()
                coord.join(threads)

            # Clear previous video's graph
            tf.reset_default_graph()

    ####################################################################################################################
    # Helper functions
    ####################################################################################################################
    def _get_logger(self):
        """Return logger, where path is dependent on mode (train/test), arch, and obj"""
        logs_path = os.path.join(os.path.dirname(__file__), 'logs')
        _, logger = setup_logging(save_path=os.path.join(
            logs_path, '{}-{}-{}.log'.format(self.params['mode'], self.params['arch'], self.params['obj'])))
        # _, self.logger = setup_logging(save_path=logs_path)
        return logger

    def _get_model(self, sess, img_batch):
        """Return model (sess is required to load weights for vgg)"""
        # Get model
        model = None
        self.logger.info('Making {} model'.format(self.params['arch']))
        if self.params['arch'] == 'basic_cnn':
            model = BasicVizsentCNN(
                                    img_w=self.params['img_crop_w'],
                                    img_h=self.params['img_crop_h'],
                                    output_dim=self.output_dim,
                                    imgs=img_batch,
                                    dropout_keep=self.params['dropout'])
        elif self.params['arch'] == 'basic_plus_cnn':
            is_training = True if self.params['mode'] == 'train' else False
            model = BasicPlusCNN(img_w=self.params['img_crop_w'],
                                 img_h=self.params['img_crop_h'],
                                 output_dim=self.output_dim,
                                 imgs=img_batch,
                                 dropout_keep=self.params['dropout'],
                                 bn_decay=self.params['bn_decay'],
                                 is_training=is_training)
        elif 'vgg' in self.params['arch']:
            load_weights = True if self.params['arch'] == 'vgg_finetune' else False
            model = vgg16(batch_size=self.params['batch_size'],
                          w=self.params['img_crop_w'],
                          h=self.params['img_crop_h'],
                          sess=sess,
                          load_weights=load_weights,
                          output_dim=self.output_dim,
                          img_batch=img_batch)
        elif self.params['arch'] == 'alexnet':
            is_training = True if self.params['mode'] == 'train' else False
            # print is_training
            # is_training = True
            model = ModifiedAlexNet(img_w=self.params['img_crop_w'],
                                    img_h=self.params['img_crop_h'],
                                    output_dim=self.output_dim,
                                    imgs=img_batch,
                                    dropout_keep=self.params['dropout'],
                                    bn_decay=self.params['bn_decay'],
                                    is_training=is_training)
        # Note: hist are features not arch, but hacky way to match things, e.g. in config.yaml
        elif self.params['arch'] == 'gray_hist' or self.params['arch'] == 'color_hist':
            model = FFNet(
            input_dim=self.input_dim,
            hidden_dim=self.params['hidden_dim'],
            output_dim=self.output_dim,
            imgs=img_batch,
            dropout_keep=self.params['dropout'])

        return model

    def _get_loss(self, model):
        if self.params['weight_classes']:
            # Get class weights
            # Formula: Divide each count into max count, the normalize:
            # Example: [35, 1, 5] -> [1, 3.5, 7] -> [0.08696, 0.30435, 0.608696]
            # ckpt_dir for test, save_dir for training
            ckpt_dir =  self.params['ckpt_dirpath'] if self.params['mode'] == 'train' else self.params['ckpt_dirpath']
            label2count = json.load(open(os.path.join(ckpt_dir, 'label2count.json'), 'r'))
            label2count = [float(c) for l,c in label2count.items()]             # (num_classes, )
            self.logger.info('Class counts: {}'.format(label2count))
            max_count = max(label2count)
            for i, c in enumerate(label2count):
                if c != 0:
                    label2count[i] = max_count / c
                else:
                    label2count[i] = 0.0
            label2count = [w / sum(label2count) for w in label2count]
            self.logger.info('Class weights: {}'.format(label2count))
            label2count = np.array(label2count)
            label2count = np.expand_dims(label2count, 1).transpose()            # (num_classes, 1) -> (1, num_classes)
            class_weights = tf.cast(tf.constant(label2count), tf.float32)

        label_batch = self.tr_label_batch if self.params['mode'] == 'train' else self.te_label_batch

        label_batch_op = tf.placeholder_with_default(label_batch,
                                                     shape=[None],
                                                     name='label_batch')

        if self.params['obj'] == 'sent_reg':
            # TODO: add weights for regression as well
            self.loss = tf.sqrt(tf.reduce_mean(tf.square(tf.sub(model.last_fc, label_batch_op))))
        else:
            labels_onehot = tf.one_hot(label_batch_op, self.output_dim)     # (batch_size, num_classes)

            logits = tf.mul(model.last_fc, class_weights) if self.params['weight_classes'] else model.last_fc
            self.loss = tf.reduce_mean(tf.nn.softmax_cross_entropy_with_logits(labels=labels_onehot, logits=logits))

            # Accuracy
            acc = tf.equal(tf.cast(tf.argmax(model.last_fc, 1), tf.int32), label_batch_op)
            self.acc = tf.reduce_mean(tf.cast(acc, tf.float32))

            if self.params['obj'] == 'bc':      # in top-k accuracy
                self.top10_indices = tf.nn.top_k(model.last_fc, 10, sorted=True).indices
                top5_acc = tf.nn.in_top_k(model.last_fc, label_batch_op, 5)
                self.top5_acc = tf.reduce_mean(tf.cast(top5_acc, tf.float32))
                top10_acc = tf.nn.in_top_k(model.last_fc, label_batch_op, 10)
                self.top10_acc = tf.reduce_mean(tf.cast(top10_acc, tf.float32))

        if self.params['use_l2']:
            vars = tf.trainable_variables()
            l2_reg = tf.add_n([tf.nn.l2_loss(v) for v in vars])
            self.loss += self.params['weight_decay_lreg'] * l2_reg

    def _get_summary_ops(self):
        """Define summaries and return summary_op"""
        self.loss_summary = tf.summary.scalar('loss', self.loss)
        if self.params['obj'] != 'sent_reg':    # classification, thus has accuracy
            self.acc_summary = tf.summary.scalar('accuracy', self.acc)
        if self.params['obj'] == 'bc':
            self.top5_acc_summary = tf.summary.scalar('top5_accuracy', self.top5_acc)
            self.top10_acc_summary = tf.summary.scalar('top10_accuracy', self.top10_acc)

        # Weights and gradients
        if self.params['tboard_debug']:
            for var in tf.trainable_variables():
                tf.summary.histogram(var.op.name, var)
            for grad, var in self.grads_and_vars:
                tf.summary.histogram(var.op.name+'/gradient', grad)

            if hasattr(self.model, 'activations'):
                for act, name in self.model.activations:
                    tf.summary.histogram(name, act)

        # Image
        if hasattr(self, 'img_batch_for_summ'):
            tf.summary.image('img_batch', self.img_batch_for_summ)

        summary_op = tf.summary.merge_all()

        return summary_op

    def _initialize(self, sess):
        if self.params['mode'] == 'train':
            sess.run(tf.global_variables_initializer())
        coord = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(sess=sess, coord=coord)
        return coord, threads

    # For prediction
    def get_idx2label(self):
        """Used to turn indices into human readable labels"""
        label2idx = None
        if self.params['obj'] == 'sent_biclass':
            label2idx = SENT_BICLASS_LABEL2INT
        elif self.params['obj'] == 'sent_triclass':
            label2idx = SENT_TRICLASS_LABEL2INT
        elif self.params['obj'] == 'emo':
            if self.params['dataset'] == 'Sentibank':
                label2idx = SENTIBANK_EMO_LABEL2INT
            elif self.params['dataset'] == 'MVSO':
                label2idx = MVSO_EMO_LABEL2INT
        elif self.params['obj'] == 'bc':
            label2idx = get_bc2idx(self.params['dataset'])

        idx2label = {v:k for k,v in label2idx.items()}
        return idx2label
