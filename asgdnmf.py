#conding: utf-8
import os
import sys
import time
from itertools import chain
import numpy as np
import pandas as pd
import scipy.sparse as ssp
import tensorflow as tf
from network import SamplingNetwork
from string import Template

class ASGDNMF(object):
    def __init__(self, K, batch_size=32, window_size=0,
                 warp_prob=0.1, learning_rate=0.01, iter_per_node=10,
                 n_jobs=8):
        self.K = K
        self.lr = learning_rate
        self.batch_size = batch_size
        self.window_size = window_size
        self.warp_prob = warp_prob
        self.iter_per_node = iter_per_node
        self.n_jobs = max(2, n_jobs)
        print(repr(self))

    def __repr__(self):
        string = "ASGDNMF("
        for key, val in self.__dict__.items():
            string += key + "=" + str(val)+",\n\t"
        string = string[:-3] + ")"
        return string

    def fit(self, X, values=None, print_loss=False):
        if isinstance(X, list) and isinstance(X[0], tuple) and len(X[0]) == 2:
            X = self.convert_edgelist_into_matrix(X, values)
        self.X = X = ssp.lil_matrix(X)
        self.net = SamplingNetwork(X)
        n_nodes = X.shape[0]
        self.batch_size = min(self.batch_size, n_nodes)
        self.window_size = min(self.window_size, self.batch_size)

        # Prepare dataflow graph and initialize variables
        self._build_graph()
        self.sess.run(self.init_op)

        # A list of tuples (node_id_list, sub-adjmatrix)
        self.enq_list = []

        # Create enqueue thread
        coord = tf.train.Coordinator()
        threads = [tf.train.threading.Thread(target=self.sample_by_random_walk,
                                             args=(coord, i))
                                        for i in range(self.n_jobs)]
        # Run enqueue threads
        self.global_enq_count = 0
        start = time.time()
        for t in threads: t.start()
        coord.join(threads)
        print("Sampling time: ", time.time()-start)
        start = time.time()
        self.enqueue_list()
        print("Enqueue time: ", time.time()-start)


        # Create and several training threads
        coord = tf.train.Coordinator()
        threads = [tf.train.threading.Thread(target=self.partial_fit,
                                              args=(coord, i, print_loss))
                                        for i in range(self.n_jobs)]
        # Run training threads
        self.global_count = 0
        start = time.time()
        for t in threads: t.start()
        coord.join(threads)
        print("Training time: ", time.time()-start)

        W = self.get_W()
        H = self.get_H()
        return W, H

    def enqueue_list(self):
        num = 0
        length = len(self.enq_list)
        max_iter = self.net.n_nodes * self.iter_per_node // self.batch_size
        while num < max_iter + self.n_jobs:
            for indices, X_s in self.enq_list:
                self.sess.run(self.enqueue,
                              feed_dict={self.enq_indices: indices,
                                         self.enq_X: X_s})
            num += length

    def partial_fit(self, coord, i, print_loss):
        count = 0
        max_iter = self.net.n_nodes * self.iter_per_node // self.batch_size
        while not coord.should_stop():
            count += 1
            _, loss = self.sess.run([self.opt, self.loss])
            #print("Finished " + str(self.global_count), "Loss: " + str(loss))
            if print_loss:
                print(self.global_count, "global loss", self.global_loss())
            self.global_count += 1
            if self.global_count > max_iter:
                coord.request_stop()

    def sample_by_random_walk(self, coord, i):
        buf = []
        X = self.X
        n_nodes = self.net.n_nodes
        pos = np.random.randint(n_nodes)
        sess = self.sess
        batch_size = self.batch_size
        window_size = self.window_size
        max_iter = n_nodes * self.iter_per_node // batch_size
        time_s = 0
        time_m = 0
        time_r = 0
        while not coord.should_stop():
            start = time.time()
            if np.random.rand() < self.warp_prob:
                pos = self.net.get_start_node()
            else:
                pos = self.net.walk_to_neighbor(pos, buf)
            if pos not in buf:
                buf.append(pos)
            time_s += time.time() - start
            if len(buf) == batch_size:
                start = time.time()
                X_s = X[buf,:][:,buf].todense()
                time_m += time.time() - start
                start = time.time()
                #sess.run(self.enqueue,
                #        feed_dict={self.enq_indices: buf,
                #                self.enq_X: X_s})

                # list-append is thread-safe operation
                self.enq_list.append((buf, X_s))

                time_r += time.time() - start
                if self.net.check_all:
                    coord.request_stop()
                    break
                #if max_iter < self.global_enq_count:
                #    coord.request_stop()
                #    break
                if window_size > 0:
                    buf = buf[-window_size:]
                else:
                    buf = []
            time_m += time.time() - start

    def _build_graph(self):
        self.graph = tf.Graph()
        with self.graph.as_default():
            K = self.K
            n_nodes = self.net.n_nodes
            sum_weight = self.X.sum()
            batch_size = self.batch_size

            max_iter = n_nodes * self.iter_per_node // batch_size
            capacity = max_iter * 10
            self.queue = queue = tf.RandomShuffleQueue(capacity, 0, ["int64", "float"],
                              shapes=[[batch_size,], [batch_size, batch_size]])

            self.enq_indices = enq_inp = tf.placeholder("int64", [batch_size])
            self.enq_X = enq_X = tf.placeholder("float32",
                                               [batch_size, batch_size])
            self.enqueue = queue.enqueue((enq_inp, enq_X))

            indices, X_s = queue.dequeue()

            scale = np.sqrt(sum_weight / (n_nodes * n_nodes * K))
            initializer = tf.random_uniform_initializer(maxval=2*scale)
            self.W_var = W_var = tf.get_variable("W", [n_nodes, K], "float32",
                                                 initializer)
            self.H_var = H_var = tf.get_variable("H", [n_nodes, K], "float32",
                                                 initializer)

            self.W = tf.abs(W_var)
            self.H = tf.abs(H_var)

            W_s = tf.gather(W_var, indices)
            H_s = tf.gather(H_var, indices)

            W_abs = tf.abs(W_s)
            H_abs = tf.abs(H_s)

            self.loss = loss = tf.nn.l2_loss(X_s - tf.matmul(W_abs, H_abs,
                                                             transpose_b=True))

            dW, dH = tf.gradients(loss, [W_s, H_s])

            update_W = tf.scatter_sub(W_var, indices, self.lr*dW)
            update_H = tf.scatter_sub(H_var, indices, self.lr*dH)

            self.opt = tf.group(update_W, update_H)

            self.sess = tf.Session()
            self.init_op = tf.initialize_all_variables()

    @staticmethod
    def convert_edgelist_into_matrix(edge_list, values=None):
        nn = max(chain.from_iterable(edge_list)) + 1
        if values is None:
            edge_list = edge_list + [(j, i) for i, j in edge_list]
            # drop duplicates
            edge_list = list(set(edge_list))
            values = np.ones(len(edge_list))
        assert len(edge_list) == np.array(values).size
        ind = np.array(edge_list)
        X = ssp.lil_matrix((nn, nn))
        X[ind[:,0],ind[:,1]] = values
        return X
    
    def get_H(self):
        return self.sess.run(self.H)
    
    def get_W(self):
        return self.sess.run(self.W)

    def community_label(self):
        H = self.get_H()
        label = H.argmax(axis=1)
        return label

    def global_loss(self):
        X = self.X
        W = np.mat(self.get_W())
        H = np.mat(self.get_H())
        loss = np.power(self.X - W * H.T, 2).sum() / 2
        return loss

if __name__=="__main__":
    from sklearn.metrics import normalized_mutual_info_score
    nmf = ASGDNMF(5, batch_size=100, window_size=0, n_jobs=8,
                  warp_prob=0.001, learning_rate=0.01, iter_per_node=500)
    if False:
        elist = pd.read_pickle("data/LRF_10000_5_100_1000_1000_0.1_edge.pkl")
        #elist = pd.read_pickle("data/karate.pkl")
        label = pd.read_pickle("data/LRF_10000_5_100_1000_1000_0.1_label.pkl")
        #label = pd.read_pickle("data/karate_label.pkl")
        X = nmf.convert_edgelist_into_matrix(elist)
        print("Start NMF")
        W, H = nmf.fit(elist, print_loss=False)
        print("Finish NMF")
        com = nmf.community_label()
        nmi = normalized_mutual_info_score(label, com)
        print(nmi)


