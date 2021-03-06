# Copyright UCL Business plc 2017. Patent Pending. All rights reserved. 
#
# The MonoDepth Software is licensed under the terms of the UCLB ACP-A licence
# which allows for non-commercial use only, the full terms of which are made
# available in the LICENSE file.
#
# For any other use of the software not covered by the UCLB ACP-A Licence, 
# please contact info@uclb.com

"""Monodepth data loader.
"""

from __future__ import absolute_import, division, print_function
import tensorflow as tf
import os
tf.random.set_random_seed(1234)

def string_length_tf(t):
  return tf.py_func(len, [t], [tf.int64])

class MonodepthDataloader(object):
    """monodepth dataloader"""

    def __init__(self, data_path, filenames_file, params, dataset, mode, sem_mask, noShuffle=False):
        self.data_path = data_path
        self.params = params
        self.dataset = dataset
        self.mode = mode
        self.sem_mask = sem_mask

        self.left_image_batch  = None
        self.right_image_batch = None
        self.semantic_image_batch = None
        self.valid_image_batch = None

        input_queue = tf.train.string_input_producer([filenames_file], shuffle=False)
        line_reader = tf.TextLineReader()
        _, line = line_reader.read(input_queue)

        split_line = tf.string_split([line]).values

        # we load only one image for test, except if we trained a stereo model
        if mode == 'test' and not self.params.do_stereo:
            left_image_path  = tf.string_join([self.data_path, split_line[0]])
            left_image_o  = self.read_image(left_image_path)
#            semantic_image_path = tf.string_join([self.data_path, split_line[2]])
#            semantic_image_o = self.read_semantic_gt(semantic_image_path)
        else:
            if self.dataset == 'cityscapes':
                left_image_path = tf.string_join([os.path.join(self.data_path, 'leftImg8bit/train/'), split_line[0]])
                right_image_path = tf.string_join([os.path.join(self.data_path, 'rightImg8bit/train/'), split_line[1]])
                semantic_image_path = tf.string_join([os.path.join(self.data_path, 'gtFine/train/'), split_line[2]])
            else:
                left_image_path = tf.string_join([self.data_path, split_line[0]])
                right_image_path = tf.string_join([self.data_path, split_line[1]])
                semantic_image_path = tf.string_join([self.data_path, split_line[2]])

            left_image_o  = self.read_image(left_image_path)
            right_image_o = self.read_image(right_image_path)
            semantic_image_o, valid_image_o = self.read_semantic_gt(semantic_image_path)

        if mode == 'train':
            if params.do_flip:
                # randomly flip images
                do_flip = tf.random_uniform([], 0, 1)
                left_image  = tf.cond(do_flip > 0.5, lambda: tf.image.flip_left_right(right_image_o), lambda: left_image_o)
                right_image = tf.cond(do_flip > 0.5, lambda: tf.image.flip_left_right(left_image_o),  lambda: right_image_o)
                valid_image = tf.cond(do_flip > 0.5, lambda: tf.zeros([self.params.height, self.params.width, 1], tf.float32),  lambda: valid_image_o)
            else:
                left_image = left_image_o
                right_image = right_image_o
                valid_image = valid_image_o
            semantic_image = semantic_image_o #tf.cond(do_flip > 0.5, lambda: tf.image.flip_left_right(semantic_image_o),  lambda: semantic_image_o)

            # randomly augment images
            do_augment  = tf.random_uniform([], 0, 1)
            left_image, right_image = tf.cond(do_augment > 0.5, lambda: self.augment_image_pair(left_image, right_image), lambda: (left_image, right_image))

            left_image.set_shape( [None, None, 3])
            right_image.set_shape([None, None, 3])
            semantic_image.set_shape([None, None, 1])
            valid_image.set_shape([None, None, 1])

            # capacity = min_after_dequeue + (num_threads + a small safety margin) * batch_size
            min_after_dequeue = 2048
            capacity = min_after_dequeue + 4 * params.batch_size
            if noShuffle:
              self.left_image_batch, self.right_image_batch, self.semantic_image_batch, self.valid_image_batch = tf.train.batch([left_image, right_image, semantic_image, valid_image], params.batch_size, capacity)
            else:
              self.left_image_batch, self.right_image_batch, self.semantic_image_batch, self.valid_image_batch = tf.train.shuffle_batch([left_image, right_image, semantic_image, valid_image], params.batch_size, capacity, min_after_dequeue, params.num_threads)

        elif mode == 'test':
            self.left_image_batch = tf.stack([left_image_o,  tf.image.flip_left_right(left_image_o)],  0)
            self.left_image_batch.set_shape([2, None, None, 3])
#            self.semantic_image_batch = tf.stack([semantic_image_o,  tf.image.flip_left_right(semantic_image_o)],  0)
#            self.semantic_image_batch.set_shape( [2, None, None, 1])

            if self.params.do_stereo:
                self.right_image_batch = tf.stack([right_image_o,  tf.image.flip_left_right(right_image_o)],  0)
                self.right_image_batch.set_shape( [2, None, None, 3])

    def augment_image_pair(self, left_image, right_image):
        # randomly shift gamma
        random_gamma = tf.random_uniform([], 0.8, 1.2)
        left_image_aug  = left_image  ** random_gamma
        right_image_aug = right_image ** random_gamma

        # randomly shift brightness
        random_brightness = tf.random_uniform([], 0.5, 2.0)
        left_image_aug  =  left_image_aug * random_brightness
        right_image_aug = right_image_aug * random_brightness

        # randomly shift color
        random_colors = tf.random_uniform([3], 0.8, 1.2)
        white = tf.ones([tf.shape(left_image)[0], tf.shape(left_image)[1]])
        color_image = tf.stack([white * random_colors[i] for i in range(3)], axis=2)
        left_image_aug  *= color_image
        right_image_aug *= color_image

        # saturate
        left_image_aug  = tf.clip_by_value(left_image_aug,  0, 1)
        right_image_aug = tf.clip_by_value(right_image_aug, 0, 1)

        return left_image_aug, right_image_aug

    def read_semantic_gt(self, image_path):
        # tf.decode_image does not return the image size, this is an ugly workaround to handle both jpeg and png
        path_length = string_length_tf(image_path)[0]
        file_extension = tf.substr(image_path, path_length - 3, 3)
        file_cond = tf.equal(file_extension, 'png')
        
        image  = tf.cond(file_cond, lambda: tf.image.decode_png(tf.read_file(image_path)), lambda: tf.zeros([self.params.height, self.params.width, 1], tf.uint8))

        # if the dataset is cityscapes, we crop the last fifth to remove the car hood
        if self.dataset == 'cityscapes':
            o_height    = tf.shape(image)[0]
            crop_height = (o_height * 4) // 5
            image  =  image[:crop_height,:,:]

        image = tf.to_int32(tf.image.resize_images(image,  [self.params.height, self.params.width], tf.image.ResizeMethod.NEAREST_NEIGHBOR))

        # mask semantics
        if self.sem_mask == 'no_flat':
            sem_not_flat = tf.logical_and(tf.logical_and(tf.not_equal(image, 7), tf.not_equal(image, 8)),
                                          tf.logical_and(tf.not_equal(image, 9), tf.not_equal(image, 10)))
            valid = tf.cast(sem_not_flat, tf.float32)
            print('No Flat in Semantics\n')
        elif self.sem_mask == 'only_flat':
            sem_flat = tf.logical_or(tf.logical_or(tf.equal(image, 7), tf.equal(image, 8)),
                                     tf.logical_or(tf.equal(image, 9), tf.equal(image, 10)))
            valid = tf.cast(sem_flat, tf.float32)
            print('Only Flat in Semantics\n')
        elif self.sem_mask == 'no_vehicle':
            sem_not_vehicle = tf.less(image, 26)
            valid = tf.cast(sem_not_vehicle, tf.float32)
            print('No Vehicle in Semantics\n')
        elif self.sem_mask == 'only_vehicle':
            sem_vehicle = tf.greater_equal(image, 26)
            valid = tf.cast(sem_vehicle, tf.float32)
            print('Only Vehicle in Semantics\n')
        else:
            valid = tf.ones([self.params.height, self.params.width, 1], tf.float32)
            print('Not masking Semantics\n')

        valid = tf.cond(file_cond, lambda: valid, lambda: tf.zeros([self.params.height, self.params.width, 1], tf.float32))

        return image, valid

    def read_image(self, image_path):
        # tf.decode_image does not return the image size, this is an ugly workaround to handle both jpeg and png
        path_length = string_length_tf(image_path)[0]
        file_extension = tf.substr(image_path, path_length - 3, 3)
        file_cond = tf.equal(file_extension, 'jpg')
        
        image  = tf.cond(file_cond, lambda: tf.image.decode_jpeg(tf.read_file(image_path)), lambda: tf.image.decode_png(tf.read_file(image_path)))

        # if the dataset is cityscapes, we crop the last fifth to remove the car hood
        if self.dataset == 'cityscapes':
            o_height    = tf.shape(image)[0]
            crop_height = (o_height * 4) // 5
            image  =  image[:crop_height,:,:]

        image  = tf.image.convert_image_dtype(image,  tf.float32)
        image  = tf.image.resize_images(image,  [self.params.height, self.params.width], tf.image.ResizeMethod.AREA)

        return image
