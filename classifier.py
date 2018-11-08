#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Nov  2 16:37:25 2018

@author: zhanghuangzhao
"""

import tensorflow as tf
import numpy
import os, sys

import dataset
from util import write_log

class SequenceClassifier(object):
    
    def __init__(self, seq_max_len=20, embed_w=5, vocab_size=2, n_class=2,
                 n_hidden=128, cell_type="rnn", keep_prob=0.9, lr=1e-5,
                 n_gpu=1, grad_clip=1, is_training=True):
        
        self.__is_training = is_training
        if is_training == False:
            is_training = True
            keep_prob = 1.0
        
        # placeholders for the language model -- input, output & valid seq length
        self.__X = tf.placeholder(shape=[None, seq_max_len],
                                  dtype=tf.int32, name="input")
        self.__Y = tf.placeholder(shape=[None, ],
                                  dtype=tf.int32, name="label")
        self.__L = tf.placeholder(shape=[None], dtype=tf.int32, name="valid_len")
        
        # embedding matrix
        with tf.device("/cpu:0"):
            self.__embed_matrix = tf.get_variable("embedding_martix",
                                                  [vocab_size, embed_w],
                                                  dtype=tf.float32)
            self.__embed = tf.nn.embedding_lookup(self.__embed_matrix, self.__X,
                                                  name="embedding")
        
        # softmax dense layer
        self.__dense_W = tf.get_variable("dense_w",
                                         [n_hidden, n_class],
                                         dtype=tf.float32)
        self.__dense_b = tf.get_variable("dense_b", [n_class],
                                         dtype=tf.float32)
        # rnn cells
        self.__cell_fw = self.__make_cell(is_training, n_hidden, keep_prob, cell_type)
        
        # optimizer, if training
        if is_training:
            self.__opt = tf.train.AdamOptimizer(lr, name="adam_optimizer")
            
        self.__saver = tf.train.Saver(tf.trainable_variables(), max_to_keep=1)
        
        # split the minibatch for the gpus
        self.__embed_list = tf.split(self.__embed, num_or_size_splits=n_gpu,
                                     axis=0, name="x_list")
        self.__y_list = tf.split(self.__Y, num_or_size_splits=n_gpu,
                                 axis=0, name="y_list")
        self.__l_list = tf.split(self.__L, num_or_size_splits=n_gpu,
                                 axis=0, name="l_list")
            
        self.__loss_list = []
        self.__output_list =[]
        self.__acc_list = []
        self.__grad_and_var_list = []
        
        self.__rnn_in_list = []
        self.__rnn_outs_list = []
        self.__rnn_states_list = []
        self.__logit_list = []
        self.__prob_list = []
        self.__y_onehot_list = []
        self.__loss_vec_list = []    
        
        for i in range(n_gpu):
            with tf.device("/device:GPU:"+str(i)):
                
                tmp_embed = self.__embed_list[i]
                tmp_y = self.__y_list[i]
                tmp_l = self.__l_list[i]
                
                # input dropout
                if is_training and keep_prob < 1:
                    self.__rnn_in_list.append(tf.nn.dropout(tmp_embed, keep_prob,
                                                            name="input_dropout_"+str(i)))
                else:
                    self.__rnn_in_list.append(tmp_embed)
                
                out = tf.nn.dynamic_rnn(self.__cell_fw,
                                        self.__rnn_in_list[-1],
                                        tmp_l,
                                        dtype=tf.float32)
                self.__rnn_outs_list.append(out[0])
                self.__rnn_states_list.append(out[1])
                
                if cell_type in ["rnn", "gru"]:
                    self.__logit_list.append(tf.matmul(self.__rnn_states_list[-1],
                                                       self.__dense_W) + self.__dense_b)
                elif cell_type in ["lstm"]:
                    self.__logit_list.append(tf.matmul(self.__rnn_states_list[-1].h,
                                                       self.__dense_W) + self.__dense_b)
                self.__prob_list.append(tf.nn.softmax(self.__logit_list[-1], name="probability_"+str(i)))
        
                # cross entropy loss
                self.__y_onehot_list.append(tf.one_hot(tmp_y, n_class, name="y_onehot_"+str(i)))
                self.__loss_vec_list.append(tf.nn.softmax_cross_entropy_with_logits(labels=self.__y_onehot_list[-1],
                                                                                    logits=self.__logit_list[-1],
                                                                                    name="loss_for_each_"+str(i)))
                self.__loss_list.append(tf.reduce_mean(self.__loss_vec_list[-1],
                                                       name="loss_"+str(i)))
                
                # compute gradients. if training
                if is_training:
                    tmp_grads = self.__opt.compute_gradients(self.__loss_list[-1],
                                                             var_list=tf.trainable_variables())
                    self.__grad_and_var_list.append(tmp_grads)
                
                # accuracy
                self.__output_list.append(tf.cast(tf.argmax(self.__prob_list[-1], -1,
                                                            name="output_label"), tf.int32))
                self.__accurate = tf.equal(tmp_y, self.__output_list[-1],
                                           name="accurate_output")
                self.__acc_list.append(tf.reduce_mean(tf.cast(self.__accurate, tf.float32),
                                                      name="accuracy"))
            
        # merge results from the multiple gpus
        self.__loss = tf.reduce_mean(self.__loss_list, 0, name="final_loss")
        self.__prob = tf.concat(self.__prob_list, 0, name="final_prob")
        self.__output = tf.concat(self.__output_list, 0, name="final_output")
        self.__acc = tf.reduce_mean(self.__acc_list, 0, name="final_accuracy")
        
        # training operation
        if is_training:
            self.__final_grads_and_vars = []
            for grads_and_vars in zip(*self.__grad_and_var_list):
                grads = []
                var = None
                for tmp_grad, tmp_var in grads_and_vars:
                    grads.append(tf.expand_dims(tmp_grad, 0))
                    var = tmp_var
                tmp_grad = tf.reduce_mean(tf.concat(grads, 0), 0,
                                          name="gradient")
                tmp_grad = tf.clip_by_value(tmp_grad, -grad_clip, grad_clip,
                                            name="gradient_clipping")
                self.__final_grads_and_vars.append((tmp_grad, var))
            self.__train_op = self.__opt.apply_gradients(self.__final_grads_and_vars,
                                                         global_step=tf.train.get_or_create_global_step(),
                                                         name="train_op")

    def train_op(self, sess, X, Y, L):
        
        if self.__is_training:
            _, l, a = sess.run((self.__train_op, self.__loss, self.__acc),
                               feed_dict={self.__X: X,
                                          self.__Y: Y,
                                          self.__L: L})
            return l, a
        else:
            return None
    
    def test_op(self, sess, X, Y, L):
        
        l, a = sess.run((self.__loss, self.__acc),
                        feed_dict={self.__X: X,
                                   self.__Y: Y,
                                   self.__L: L})
        return l, a
    
    def prob_op(self, sess, X, L):
        
        p, o = sess.run((self.__prob, self.__output),
                        feed_dict={self.__X: X,
                                   self.__L: L})
        return p, o

    def save(self, sess, path):
        
        self.__saver.save(sess, path)
        
    def load(self, sess, path):
        
        self.__saver.restore(sess, path)

    def __make_cell(self, is_training, n_hidden, keep_prob, cell_type):
        
        # make the cells
        if cell_type == "rnn":
            tmp_cell = tf.contrib.rnn.BasicRNNCell(n_hidden, reuse=not is_training)
        elif cell_type == "lstm":
            tmp_cell = tf.contrib.rnn.BasicLSTMCell(n_hidden, forget_bias=0.0,
                                                    reuse=not is_training)
        elif cell_type == "gru":
            tmp_cell = tf.contrib.rnn.GRUCell(n_hidden, reuse=not is_training)
        else:
            assert False, "invalid cell type "+cell_type
        # dropout if needed
        if is_training and keep_prob < 1:
            cell = tf.contrib.rnn.DropoutWrapper(tmp_cell,
                                                 output_keep_prob=keep_prob)
        else:
            cell = tmp_cell
        return cell
    
if __name__ == "__main__":
        
    tomita_idx = 1
    print ("TOMITA "+str(tomita_idx))
    if len(sys.argv) >= 2:
        tomita_idx = int(sys.argv[1])
    
    dataset_path = "./tomita/tomita_"+str(tomita_idx)+".pkl"
    cell_type = "gru"
    
    seq_max_len = 20
    embed_w = 5
    n_cell = 128
    
    n_epoch = 100
    batch_size = 32
    model_root = "./model/tomita_"+str(tomita_idx)+"_rnn"+"/"
    if not os.path.exists(model_root):
        os.system("mkdir "+model_root)
    model_save_path = os.path.join(model_root, "model.ckpt")
    log_save_path = "./log/tomita_"+str(tomita_idx)+"_rnn.log"
    
    os.environ['CUDA_VISIBLE_DEVICES'] = "1,4"
    n_gpu = 2
    os.system("rm -rf "+log_save_path)
    
    data = dataset.Dataset(dataset_path, seq_max_len)
    model = SequenceClassifier(seq_max_len, embed_w, len(data.get_alphabet())+1, 2, n_cell,
                               cell_type, 0.8, 1e-5, n_gpu, True)
    
    cfg = tf.ConfigProto(allow_soft_placement=True)
    cfg.gpu_options.allow_growth = True
    sess = tf.Session(config=cfg)
    sess.run(tf.global_variables_initializer())
    
    for epoch in range(n_epoch):
        test_loss_mean_best = 100.0
        data.reset_train_epoch()
        data.reset_test_epoch()
        train_acc_list=[]
        test_acc_list=[]
        train_loss_list=[]
        test_loss_list=[]
        n_tr_iter = int(data.get_train_size() / batch_size)
        n_te_iter = int(data.get_test_size() / batch_size)
        for iteration in range(n_tr_iter):
            x, y, l = data.minibatch(batch_size*n_gpu)
            loss, acc = model.train_op(sess, x, y, l)
            train_acc_list.append(acc)
            train_loss_list.append(loss)
            if (iteration % 100 == 0):
                print("Epoch = %d\t iter = %d/%d\tTrain Loss = %.3f\tAcc = %.3f"
                      % (epoch+1, iteration+1, n_tr_iter, loss, acc))
        for iteration in range(n_te_iter):
            x, y, l = data.test_batch(batch_size*n_gpu)
            loss, acc = model.test_op(sess, x, y, l)
            test_acc_list.append(acc)
            test_loss_list.append(loss)
            if (iteration % 100 == 0):
                print("Epoch = %d\t iter = %d/%d\tTest Loss = %.3f\tAcc = %.3f"
                      % (epoch+1, iteration+1, n_te_iter, loss, acc))
            
        test_loss_mean = numpy.mean(test_loss_list)
        train_loss_mean = numpy.mean(train_loss_list)
        test_acc_mean = numpy.mean(test_acc_list)
        train_acc_mean = numpy.mean(train_acc_list)
        if test_loss_mean < test_loss_mean_best:
            test_loss_mean_best = test_loss_mean
            model.save(sess, model_save_path)
        write_log('Epoch '+str(epoch+1)+'\ttrain loss = '+str(train_loss_mean)
                +'\ttest loss = '+str(test_loss_mean)+"\ttrain acc = "+str(train_acc_mean)
                +'\ttest acc = '+str(test_acc_mean)+'\n',
                log_save_path)