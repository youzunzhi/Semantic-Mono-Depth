# Copyright UCL Business plc 2017. Patent Pending. All rights reserved.
#
# The MonoDepth Software is licensed under the terms of the UCLB ACP-A licence
# which allows for non-commercial use only, the full terms of which are made
# available in the LICENSE file.
#
# For any other use of the software not covered by the UCLB ACP-A Licence,
# please contact info@uclb.com

from __future__ import absolute_import, division, print_function

# only keep warnings and errors
import os
os.environ['TF_CPP_MIN_LOG_LEVEL']='1'

import numpy as np
import argparse
import re
import time
import tensorflow as tf
import tensorflow.contrib.slim as slim
import sys

from monodepth_model import *
from monodepth_dataloader import *
from average_gradients import *

parser = argparse.ArgumentParser(description='Monodepth TensorFlow implementation.')
parser.add_argument('--sem_mask',                   type=str,   help='mask some categories in semantic gt when training', required=True)
parser.add_argument('--mode',                      type=str,   help='train or test', default='train')
parser.add_argument('--task',                      type=str,   help='depth, semantic, semantic-depth', default='semantic-depth', choices=['depth', 'semantic', 'semantic-depth'])
# parser.add_argument('--model_name',                type=str,   help='model name', default='semantic-monodepth')
parser.add_argument('--model_name',                type=str,   help='For log, so be aware what experiment you are doing', required=True)
parser.add_argument('--encoder',                   type=str,   help='type of encoder, vgg or resnet50', default='resnet50', choices=['vgg', 'resnet50'])
parser.add_argument('--dataset',                   type=str,   help='dataset to train on, kitti, or cityscapes', default='cityscapes', choices=['kitti','cityscapes'])
# parser.add_argument('--data_path',                 type=str,   help='path to the data', default='/work/u2263506/kitti_stereo/')
# parser.add_argument('--filenames_file',            type=str,   help='path to the filenames text file', default='utils/filenames/kitti_semantic_stereo_2015_train_split.txt')
parser.add_argument('--input_height',              type=int,   help='input height', default=256)
parser.add_argument('--input_width',               type=int,   help='input width', default=512)
parser.add_argument('--batch_size',                type=int,   help='batch size', default=2)
parser.add_argument('--num_epochs',                type=int,   help='number of epochs', default=50)
parser.add_argument('--learning_rate',             type=float, help='initial learning rate', default=1e-4)
parser.add_argument('--lr_loss_weight',            type=float, help='left-right consistency weight', default=1.0)
parser.add_argument('--alpha_image_loss',          type=float, help='weight between SSIM and L1 in the image loss', default=0.85)
parser.add_argument('--disp_gradient_loss_weight', type=float, help='disparity smoothness weigth', default=0.1)
parser.add_argument('--do_stereo',                             help='if set, will train the stereo model', action='store_true')
parser.add_argument('--wrap_mode',                 type=str,   help='bilinear sampler wrap mode, edge or border', default='border')
parser.add_argument('--use_deconv',                            help='if set, will use transposed convolutions', action='store_true')
parser.add_argument('--num_gpus',                  type=int,   help='number of GPUs to use for training', default=1)
parser.add_argument('--num_threads',               type=int,   help='number of threads to use for data loading', default=8)
parser.add_argument('--output_directory',          type=str,   help='output directory for test disparities, if empty outputs to checkpoint folder', default='')
parser.add_argument('--log_directory',             type=str,   help='directory to save checkpoints and summaries', default='./logs/')
parser.add_argument('--checkpoint_path',           type=str,   help='path to a specific checkpoint to load', default='')
parser.add_argument('--retrain',                               help='if used with checkpoint_path, will restart training from step zero', action='store_true')
parser.add_argument('--full_summary',                          help='if set, will keep more data for each summary. Warning: the file can become very large', action='store_true')
parser.add_argument('--no_shuffle', help='Disabling shuffling at train time',   action='store_true')
args = parser.parse_args()

def post_process_disparity(disp):
    _, h, w = disp.shape
    l_disp = disp[0,:,:]
    r_disp = np.fliplr(disp[1,:,:])
    m_disp = 0.5 * (l_disp + r_disp)
    l, _ = np.meshgrid(np.linspace(0, 1, w), np.linspace(0, 1, h))
    l_mask = 1.0 - np.clip(20 * (l - 0.05), 0, 1)
    r_mask = np.fliplr(l_mask)
    return r_mask * l_disp + l_mask * r_disp + (1.0 - l_mask - r_mask) * m_disp

def count_text_lines(file_path):
    f = open(file_path, 'r')
    lines = f.readlines()
    f.close()
    return len(lines)   

def test(params):
    """Test function."""
    assert args.dataset == 'kitti'
    data_path = '/work/u2263506/kitti_stereo/'
    filenames_file = 'utils/filenames/kitti_semantic_stereo_2015_test_split.txt'
    dataloader = MonodepthDataloader(data_path, filenames_file, params, args.dataset, args.mode, args.sem_mask)
    left  = dataloader.left_image_batch
    right = dataloader.right_image_batch
    semantic = dataloader.semantic_image_batch
    valid = dataloader.valid_image_batch    
    vars_to_restore = []

    model = MonodepthModel(params, args.mode, args.task, left, right, semantic, valid)
    if args.checkpoint_path != '' and len(vars_to_restore) == 0: 
       vars_to_restore = get_var_to_restore_list(args.checkpoint_path)
       print('Vars to restore ' + str(len(vars_to_restore)) + ' vs total vars ' + str(len(tf.trainable_variables())))

    # SESSION
    config = tf.ConfigProto(allow_soft_placement=True)
    sess = tf.Session(config=config)

    # SAVER
    train_loader = tf.train.Saver()
    if args.checkpoint_path != '':
        train_loader = tf.train.Saver(var_list=vars_to_restore)

    # INIT
    sess.run(tf.global_variables_initializer())
    sess.run(tf.local_variables_initializer())
    coordinator = tf.train.Coordinator()
    threads = tf.train.start_queue_runners(sess=sess, coord=coordinator)

    # RESTORE
    if args.checkpoint_path == '':
        restore_path = tf.train.latest_checkpoint(args.log_directory + '/' + args.model_name)
    else:
        restore_path = args.checkpoint_path
    train_loader.restore(sess, restore_path)

    num_test_samples = count_text_lines(filenames_file)

    print('now testing {} files'.format(num_test_samples))
    disparities    = np.zeros((num_test_samples, params.height, params.width), dtype=np.float32)
    disparities_pp = np.zeros((num_test_samples, params.height, params.width), dtype=np.float32)
    semantics      = np.zeros((num_test_samples, params.height, params.width), dtype=np.float32)

    for step in range(num_test_samples):
        print('step:', step)
        if 'semantic' in args.task and 'depth' in args.task:
            disp, sem = sess.run([model.disp_left_est[0], tf.argmax(model.sem_est[0],-1)])
            disparities[step] = disp[0].squeeze()
            disparities_pp[step] = post_process_disparity(disp.squeeze())
            semantics[step] = sem[0].squeeze()
        elif 'depth' in args.task:
            disp = sess.run(model.disp_left_est[0])
            disparities[step] = disp[0].squeeze()
            disparities_pp[step] = post_process_disparity(disp.squeeze())
        elif 'semantic' in args.task:
            sem = sess.run(tf.argmax(model.sem_est[0],-1))
            semantics[step] = sem[0].squeeze()

    print('done.')

    print('writing results.')
    if args.output_directory == '':
        output_directory = os.path.dirname(args.checkpoint_path)
    else:
        output_directory = args.output_directory

    if 'depth' in args.task:
        np.save(output_directory + '/disparities.npy',    disparities)
        np.save(output_directory + '/disparities_pp.npy', disparities_pp)
    if 'semantic' in args.task:
        np.save(output_directory + '/semantics.npy', semantics)

    print('done.')

def train(params):
    """Training loop."""
    
    with tf.Graph().as_default(), tf.device('/cpu:0'):

        global_step = tf.Variable(0, trainable=False)

        # OPTIMIZER
        filenames_file = 'utils/filenames/kitti_semantic_stereo_2015_train_split.txt' if args.dataset == 'kitti' else 'utils/filenames/cityscapes_semantic_train_files.txt'
        num_training_samples = count_text_lines(filenames_file)

        steps_per_epoch = np.ceil(num_training_samples / params.batch_size).astype(np.int32)
        num_total_steps = params.num_epochs * steps_per_epoch
        start_learning_rate = args.learning_rate

        boundaries = [np.int32((3/5) * num_total_steps), np.int32((4/5) * num_total_steps)]
        values = [args.learning_rate, args.learning_rate / 2, args.learning_rate / 4]
        learning_rate = tf.train.piecewise_constant(global_step, boundaries, values)

        opt_step = tf.train.AdamOptimizer(learning_rate)

        print("total number of samples: {}".format(num_training_samples))
        print("total number of steps: {}".format(num_total_steps))

        if tf.test.is_gpu_available():
            data_path = '/work/u2263506/kitti_stereo/' if args.dataset == 'kitti' else '/work/u2263506/cityscapes/'
        else:
            data_path = '/Users/youzunzhi/pro/datasets/kitti/kitti_stereo/'
        dataloader = MonodepthDataloader(data_path, filenames_file, params, args.dataset, args.mode, args.sem_mask, args.no_shuffle)
        left  = dataloader.left_image_batch
        right = dataloader.right_image_batch
        semantic = dataloader.semantic_image_batch
        valid = dataloader.valid_image_batch

        # split for each gpu
        left_splits  = tf.split(left,  args.num_gpus, 0)
        right_splits = tf.split(right, args.num_gpus, 0)
        semantic_splits = tf.split(semantic, args.num_gpus, 0)
        valid_splits = tf.split(valid, args.num_gpus, 0)        

        tower_grads  = []
        tower_losses = []
        vars_to_restore = []

        reuse_variables = None
        with tf.variable_scope(tf.get_variable_scope()):
            for i in range(args.num_gpus):
                with tf.device('/gpu:%d' % i):

                    model = MonodepthModel(params, args.mode, args.task, left_splits[i], right_splits[i], semantic_splits[i], valid_splits[i], reuse_variables, i)

                    # Restore weights iff present in checkpoint
                    if args.checkpoint_path != '' and len(vars_to_restore) == 0: 
                      vars_to_restore = get_var_to_restore_list(args.checkpoint_path)
                      print('Vars to restore ' + str(len(vars_to_restore)) + ' vs total vars ' + str(len(tf.trainable_variables())))

                    vars_to_optimize = []
                    if 'cascade' in args.task and 'cross' not in args.task:
                      for v in tf.trainable_variables():
                        if v.name.split(':')[0] not in vars_to_restore:
                          vars_to_optimize.append(v)
                    else:
                      vars_to_optimize = tf.trainable_variables()

                    print('Vars to optimize ' + str(len(vars_to_optimize)) + ' vs total vars ' + str(len(tf.trainable_variables())))

                    loss = model.total_loss
                    tower_losses.append(loss)

                    reuse_variables = True

                    grads = opt_step.compute_gradients(loss, vars_to_optimize)

                    tower_grads.append(grads)

        grads = average_gradients(tower_grads)

        apply_gradient_op = opt_step.apply_gradients(grads, global_step=global_step)

        total_loss = tf.reduce_mean(tower_losses)

        tf.summary.scalar('learning_rate', learning_rate, ['model_0'])
        tf.summary.scalar('total_loss', total_loss, ['model_0'])
        summary_op = tf.summary.merge_all('model_0')

        # SESSION
        config = tf.ConfigProto(allow_soft_placement=True)
        config.gpu_options.per_process_gpu_memory_fraction=0.5
        sess = tf.Session(config=config)

        # SUMMARY WRITER
        summary_writer = tf.summary.FileWriter(args.log_directory + '/' + args.model_name, sess.graph)
        # SAVER
        train_saver = tf.train.Saver(max_to_keep=0)
        train_loader = tf.train.Saver()
        if args.checkpoint_path != '':
            train_loader = tf.train.Saver(var_list=vars_to_restore)

        with open(os.path.join(args.log_directory + '/' + args.model_name, 'params.sh'), 'w+') as out:
          sys.argv[0] = os.path.join(os.getcwd(), sys.argv[0])
          out.write('#!/bin/bash\n')
          out.write('python ')
          out.write(' '.join(sys.argv))
          out.write('\n')        

        # COUNT PARAMS
        total_num_parameters = 0
        for variable in tf.trainable_variables():
            total_num_parameters += np.array(variable.get_shape().as_list()).prod()
        print("number of trainable parameters: {}".format(total_num_parameters))

        # INIT
        sess.run(tf.global_variables_initializer())
        sess.run(tf.local_variables_initializer())
        coordinator = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(sess=sess, coord=coordinator)

        # LOAD CHECKPOINT IF SET
        if args.checkpoint_path != '':
            train_loader.restore(sess, args.checkpoint_path)
            print("Weights restored")

            if args.retrain:
                sess.run(global_step.assign(0))

        # GO!
        start_step = global_step.eval(session=sess)
        start_time = time.time()
        for step in range(start_step, num_total_steps):
            before_op_time = time.time()
            _, loss_value = sess.run([apply_gradient_op, total_loss])
            duration = time.time() - before_op_time
            if step and step % 100 == 0:
                examples_per_sec = params.batch_size / duration
                time_sofar = (time.time() - start_time) / 3600
                training_time_left = (num_total_steps / step - 1.0) * time_sofar
                print_string = 'batch {:>6} | examples/s: {:4.2f} | loss: {:.5f} | time elapsed: {:.2f}h | time left: {:.2f}h'
                print(print_string.format(step, examples_per_sec, loss_value, time_sofar, training_time_left))
                summary_str = sess.run(summary_op)
                summary_writer.add_summary(summary_str, global_step=step)
            # if step and step % 10000 == 0:
            #     train_saver.save(sess, args.log_directory + '/' + args.model_name + '/model', global_step=step)

        train_saver.save(sess, args.log_directory + '/' + args.model_name + '/model', global_step=num_total_steps)

def main(_):

    params = monodepth_parameters(
        encoder=args.encoder,
        height=args.input_height,
        width=args.input_width,
        batch_size=args.batch_size,
        num_threads=args.num_threads,
        num_epochs=args.num_epochs,
        do_stereo=args.do_stereo,
        wrap_mode=args.wrap_mode,
        use_deconv=args.use_deconv,
        alpha_image_loss=args.alpha_image_loss,
        disp_gradient_loss_weight=args.disp_gradient_loss_weight,
        lr_loss_weight=args.lr_loss_weight, task = args.task,
        full_summary=args.full_summary)
        
    if args.mode == 'train':
        train(params)
    elif args.mode == 'test':
        test(params)
    elif args.mode == 'template':
        template(params)

if __name__ == '__main__':
    tf.app.run()
