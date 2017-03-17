from __future__ import absolute_import, division, print_function

import logging
import random
import os

import tensorflow as tf
import numpy as np

import IPython

import multiprocessing

logging.basicConfig(level=logging.INFO)
FLAGS = tf.app.flags.FLAGS

import qa_model
from tensorflow.python.ops import variable_scope as vs

"""
This file is for the version of the QA_Model with separate
Questions and answer input placeholders.
"""

# Encoder class for coattention
class CoEncoder(object):
    def __init__(self, hidden_size, vocab_dim, c_len, q_len):
        self.hidden_size = hidden_size
        self.vocab_dim = vocab_dim
        self.c_len = c_len
        self.q_len = q_len

    def encode(self, qs, q_lens, cs, c_lens, dropout):
        Q = self.q_len #length of questions
        C = self.c_len #length of contexts
        hidden_size = self.hidden_size

        cell = tf.nn.rnn_cell.LSTMCell(hidden_size)
        cell = tf.nn.rnn_cell.DropoutWrapper(cell, output_keep_prob = 1.0 - dropout)

        #Run the first LSTM on the questions
        with tf.variable_scope("encoder") as scope:
            xav_init = tf.contrib.layers.xavier_initializer()
            w_q = tf.get_variable("W_q", (hidden_size,hidden_size), tf.float32, xav_init)
            b_q = tf.get_variable("b_q", (hidden_size), tf.float32, xav_init)


            q_outputs, q_states = tf.nn.dynamic_rnn(
                cell=cell, inputs=qs,
                sequence_length=q_lens, dtype=tf.float32,
                swap_memory=True)

            scope.reuse_variables() #Keep the same parameters for encoding questions and contexts

            #Run the LSTM on the contexts
            c_outputs, c_states = tf.nn.dynamic_rnn(
                cell=cell, inputs=cs,
                sequence_length=c_lens, dtype=tf.float32,
                swap_memory=True)


            #Now append the sentinel to each batch
            #sentinel = tf.zeros([B,1,L])
            sentinel_len = 0
            #d = tf.concat(1,[c_outputs, sentinel]) #dimensions BxC+1xL
            #q_prime = tf.concat(1,[q_outputs, sentinel]) #dimensions BxQ+1xL

            doc = c_outputs
            q_prime = q_outputs

            q = tf.reshape(q_prime, [-1, hidden_size])
            q = tf.reshape(tf.matmul(q, w_q), [-1, Q + sentinel_len, hidden_size]) + b_q
            q = tf.tanh(q)

            # Now, to calculate the coattention matrix etc

            l = tf.matmul(doc, tf.transpose(q, perm=[0,2,1])) #shape: BxC+1xQ+1

            # for each context position, weights for corresponding question positions
            Aq = tf.nn.softmax(l) #shape: BxC+1xQ+1
            # for each question position, weights for corresponding context positions
            Ad = tf.nn.softmax(tf.transpose(l, perm=[0,2,1])) # shape: BxQ+1xC+1

            # For each question index, a weighted sum of context word representations,
            # weighted by the attention paid to that
            Cq = tf.matmul(tf.transpose(doc, perm=[0,2,1]), Aq) #shape: BxLxQ+1
            QCq = tf.concat(1, [tf.transpose(q, perm=[0,2,1]), Cq]) #shape: Bx2L*Q+1
            Cd = tf.matmul(QCq, Ad) #shape: Bx2LxC+1
            Cd = tf.transpose(Cd, perm=[0,2,1]) #shape: BxC+1x2L

            DCd = tf.concat(2, [doc, Cd])

        with tf.variable_scope("encoder2") as scope:
            # Also concat with raw c representation like in github?
            # we stop when we hit the last index and output a 0 - is that cool?
            outputs, states = tf.nn.bidirectional_dynamic_rnn(
                cell_fw=cell, cell_bw=cell,
                sequence_length=c_lens,
                dtype=tf.float32, inputs=DCd,
                swap_memory=True)

            outputs = tf.concat(2, outputs) #shape BxC+1x2L
            #outputs = tf.slice(outputs, [0,0,0], [B, C, 2*hidden_size ])

        return outputs

class NaiveCoDecoder(object):
    def __init__(self, hidden_size):
        self.hidden_size = hidden_size

    # Inputs of shape (Batch size) x (Context length) x (2*hidden_size)
    def decode(self, inputs, lengths, c_len, dropout):

        cell = tf.nn.rnn_cell.LSTMCell(self.hidden_size, use_peepholes=False)
        cell = tf.nn.rnn_cell.DropoutWrapper(cell, output_keep_prob = 1.0 - dropout)
        xav_init = tf.contrib.layers.xavier_initializer()

        #Decoder for start positions
        with vs.variable_scope("start_decoder"):
            s_h, _ = tf.nn.dynamic_rnn(
                cell=cell, inputs=inputs,
                sequence_length=lengths, dtype=tf.float32,
                swap_memory=True)

            w_s = tf.get_variable("W_s", (self.hidden_size, 1), tf.float32, xav_init)
            b_s = tf.get_variable("b_s", (1,), tf.float32, tf.constant_initializer(0.0))
            s_h = tf.reshape(s_h, [-1, self.hidden_size])
            inner = tf.matmul(s_h, w_s) + b_s
            inner = tf.reshape(inner, [-1, c_len])
            #start_probs = tf.nn.softmax(inner)
            start_probs = inner

        #Decoder for start positions
        with vs.variable_scope("end_decoder"):
            e_h, _ = tf.nn.dynamic_rnn(
                cell=cell, inputs=inputs,
                sequence_length=lengths, dtype=tf.float32,
                swap_memory=True)

            w_e = tf.get_variable("W_e", (self.hidden_size, 1), tf.float32, xav_init)
            b_e = tf.get_variable("b_e", (1,), tf.float32, tf.constant_initializer(0.0))
            e_h = tf.reshape(e_h, [-1, self.hidden_size])
            inner = tf.matmul(e_h, w_e) + b_e
            inner = tf.reshape(inner, [-1, c_len])
            #end_probs = tf.nn.softmax(inner)
            end_probs = inner


        with vs.variable_scope("final_start_layer"):
            z = tf.concat(1, [start_probs, end_probs])

            wfs = tf.get_variable("W_f_s", [2*c_len, 2*c_len], tf.float32, xav_init)
            bfs = tf.get_variable("B_f_s", [2*c_len], tf.float32, tf.constant_initializer(0.0))
            z2 = tf.nn.relu(tf.matmul(z, wfs) + bfs)

            wfs2 = tf.get_variable("W_f_s2", [2*c_len, c_len], tf.float32, xav_init)
            bfs2 = tf.get_variable("B_f_s2", [c_len], tf.float32, tf.constant_initializer(0.0))
            #start_probs = tf.nn.relu(tf.matmul(z2, wfs2) + bfs2)
            start_probs = tf.matmul(z2, wfs2) + bfs2


        with vs.variable_scope("final_end_layer"):
            z = tf.concat(1, [start_probs, end_probs])

            wfe = tf.get_variable("W_f_e", [2*c_len, 2*c_len], tf.float32, xav_init)
            bfe = tf.get_variable("B_f_e", [2*c_len], tf.float32, tf.constant_initializer(0.0))
            z2 = tf.nn.relu(tf.matmul(z, wfe) + bfe)

            wfe2 = tf.get_variable("W_f_e2", [2*c_len, c_len], tf.float32, xav_init)
            bfe2 = tf.get_variable("B_f_e2", [c_len], tf.float32, tf.constant_initializer(0.0))
            #end_probs = tf.nn.relu(tf.matmul(z2, wfe2) + bfe2)
            end_probs = tf.matmul(z2, wfe2) + bfe2


        return start_probs, end_probs


class QASepSystem(qa_model.QASystem):
    def __init__(self, input_size, hidden_size, *args):
        self.in_size = input_size
        self.hidden_size = hidden_size
        # self.out_size = output_size
        self.eval_res_file = open(FLAGS.log_dir + "/eval_res.txt", 'w')
        self.extra_log_process = None

    def build_pipeline(self):
        self.encoder = CoEncoder(self.hidden_size, self.in_size,
                                       self.max_c_len, self.max_q_len)
        self.decoder = NaiveCoDecoder(self.hidden_size)


        self.q_placeholder = tf.placeholder(tf.int32, (None, self.max_q_len))
        self.ctx_placeholder = tf.placeholder(tf.int32, (None, self.max_c_len))

        self.q_len_pholder = tf.placeholder(tf.int32, (None,))
        self.c_len_pholder = tf.placeholder(tf.int32, (None,))

        # True 1/0 labelings of words in the context
        self.labels_placeholder = tf.placeholder(tf.int32, (None, 2))

        # 1/0 mask to ignore padding in context for loss purposes
        self.mask_placeholder = tf.placeholder(tf.bool, (None, self.max_c_len))

        self.dropout_placeholder = tf.placeholder(tf.float32, ())  # Proportion of connections to drop
        self.learn_r_placeholder = tf.placeholder_with_default(FLAGS.learning_rate, ())

        with tf.variable_scope("qa", initializer=tf.uniform_unit_scaling_initializer(1.0)):
            embeds = self.setup_embeddings()
            decode_res = self.setup_system(embeds)
            final_res = tf.stack(decode_res, axis=2)
            self.loss = self.setup_loss(decode_res)

            # build the results
            self.results = tf.argmax(final_res, axis=1)

        self.train_op = tf.train.AdamOptimizer().minimize(self.loss)


    def setup_embeddings(self):
        embed_path = FLAGS.embed_path or os.path.join(
            "data", "squad", "glove.trimmed.{}.npz".format(FLAGS.embedding_size))
        with open(embed_path, "rb") as f:
            self.pretrained_embeddings = np.load(f)['glove']

        # We now need to set up the tensorflow emedding
        embed = tf.Variable(self.pretrained_embeddings, dtype=tf.float32)
        q_embed = tf.nn.embedding_lookup(embed, self.q_placeholder)
        ctx_embed = tf.nn.embedding_lookup(embed, self.ctx_placeholder)

        return {"q": q_embed, "ctx": ctx_embed}

    def setup_system(self, embeds):
        encoding = self.encoder.encode(embeds["q"], self.q_len_pholder, embeds["ctx"],
                                             self.c_len_pholder, self.dropout_placeholder)
        res = self.decoder.decode(encoding, self.c_len_pholder, self.max_c_len,
                                  self.dropout_placeholder)

        return res

    def decode_arbitration_layer(self, word_res, masks):
        # If we are doing masking, we should mask here as well as at the end.
        # that way the nn gets an accurate assessment of the actual probs
        xav_init = tf.contrib.layers.xavier_initializer()

        #output two values instead of 1? for positive and negative class
        #then run through softmax
        w = tf.get_variable("W_final", (2*self.hidden_size, 1), tf.float32, xav_init)
        b = tf.get_variable("b_final", (1,), tf.float32, tf.constant_initializer(0.0))

        word_res_tmp = tf.reshape(word_res, [-1, 2*self.hidden_size])
        inner = tf.matmul(word_res_tmp, w) + b
        #use relu and softmax here instead?
        #inner = tf.nn.sigmoid(inner)
        word_res = tf.reshape(inner, [-1, self.max_c_len])

        masked_wr = word_res * masks
        res1_inner = self.simple_arb_layer(masked_wr, "arb_layer_1")
        #res1 = tf.nn.relu(res1_inner)
        #res2_inner = self.simple_arb_layer(res1, "arb_layer_2")
        #res2 = tf.nn.relu(res2_inner)  # we might not want this relu layer
        res2 = res1_inner
        masked_res = res2 * masks
        return masked_res

    def simple_arb_layer(self, inputs, layer_name):
        with vs.variable_scope(layer_name):
            xav_init = tf.contrib.layers.xavier_initializer()
            w = tf.get_variable("W_arb", [self.max_c_len, self.max_c_len], tf.float32, xav_init)
            b = tf.get_variable("B_arb", [self.max_c_len], tf.float32, tf.constant_initializer(0.0))
            inner = tf.matmul(inputs, w) + b
        return inner

    def setup_loss(self, final_res):
        """
        final_res: originally B x context_len x 2 tensor
        """
        with vs.variable_scope("loss"):
            ce_wl = tf.nn.sparse_softmax_cross_entropy_with_logits
            start_labels, end_labels = tf.unpack(self.labels_placeholder, axis=1)
            start_losses = ce_wl(final_res[0], start_labels)
            end_losses = ce_wl(final_res[1], end_labels)

            # TODO: figure out how to do masking properly
            # tf.boolean_mask(losses, self.mask_placeholder)
            loss = tf.reduce_mean(start_losses + end_losses)

        return loss

    def process_dataset(self, dataset, max_q_length=None, max_c_length=None):
        self.train_contexts = all_cs = dataset['contexts']
        self.train_questions = all_qs = dataset['questions']
        self.train_spans = all_spans = dataset['spans']
        self.vocab = dataset['vocab']

        self.max_q_len = max_q_length or max(all_qs, key=len)
        self.max_c_len = max_c_length or max(all_cs, key=len)

        # build the padded questions, contexts, spans, lengths
        pad_qs, pad_cs, spans, seq_lens = (list() for i in range(4))

        for q, c, span in zip(all_qs, all_cs, all_spans):
            if len(q) > max_q_length:
                continue
            if len(c) > max_c_length:
                continue
            pad_qs.append(self.pad_ele(q, self.max_q_len))
            pad_cs.append(self.pad_ele(c, self.max_c_len))
            spans.append(span)
            seq_lens.append((len(q), len(c)))

        # now we sort the whole thing
        all_qs = list(zip(pad_qs, pad_cs, spans, seq_lens))
        train_size = int(len(all_qs) * .8)
        self.train_qas = all_qs[:train_size]
        self.dev_qas = all_qs[train_size:]

        sort_alg = lambda x: x[3][1] + x[3][0] / 1000  # small bias for quesiton length
        self.train_qas.sort(key=sort_alg)
        self.dev_qas.sort(key=sort_alg)

    @staticmethod
    def pad_vocab_ids(seqs, max_len=None):
        if max_len is None:
            max_len = max((len(s) for s in seqs))
        else:
            seqs = (s for s in seqs if len(s) <= max_len)
        return [s + (max_len - len(s)) * [0] for s in seqs]

    @staticmethod
    def pad_and_max_len(seqs):
        max_len = max((len(s) for s in seqs))
        return [QASepSystem.pad_ele(s, max_len) for s in seqs], max_len

    @staticmethod
    def pad_ele(seq, max_len):
        return seq + (max_len - len(seq)) * [0]

    def train_on_batch(self, session, batch_data):
        """Perform one step of gradient descent on the provided batch of data.
        """

        feed_dict = self.prepare_data(batch_data, dropout=FLAGS.dropout)
        _, l = session.run([self.train_op, self.loss], feed_dict=feed_dict)
        return l

    def prepare_data(self, data, dropout=0):
        q_batch, ctx_batch, labels_batch, context_spans_batch = data
        q_lens, c_lens = zip(*context_spans_batch)
        masks = [self.selector_sequence(0, c - 1, self.max_c_len) for c in c_lens]

        feed_dict = {self.q_placeholder: q_batch,
                     self.ctx_placeholder: ctx_batch,
                     self.labels_placeholder: labels_batch,
                     self.q_len_pholder: q_lens,
                     self.c_len_pholder: c_lens,
                     self.dropout_placeholder: dropout,
                     self.mask_placeholder: masks}
        return feed_dict

    def evaluate_answer(self, session, sample=None, log=True):
        if sample is None:
            sample = 2000 if FLAGS.is_azure else 50

        eval_set = list(random.sample(self.dev_qas, sample))
        q_vec, ctx_vec, gold_probs, masks = zip(*eval_set)

        if self.extra_log_process:
            self.extra_log_process.join()  # make sure it has finished by now

        pred_probs = []
        for batch in self.build_batches(eval_set, shuffle=False):
            feed_dict = self.prepare_data(zip(*batch))
            pred_probs.extend(session.run([self.results], feed_dict=feed_dict)[0])

        gold_spans = [self.selector_sequence(start, end, self.max_c_len) for start, end in gold_probs]
        pred_spans = [self.selector_sequence(start, end, self.max_c_len) for start, end in pred_probs]

        f1_stuff, ems, pred_s, gold_s = zip(*(self.eval_sentence(p, g, s)
                         for p, g, s in zip(pred_spans, gold_spans, ctx_vec)))

        precisions, recalls, f1s = zip(*f1_stuff)
        f1 = np.mean(f1s)
        em = np.mean(ems)

        if log:
            logging.info("\nF1: {}, EM: {}, for {} samples".format(f1, em, sample))
            logging.info("Precision: {}, Recall: {}; {} total words predicted".format(
                np.mean(precisions), np.mean(recalls), np.sum(pred_spans)))

            if self.epoch % 5 == 1:
                self.eval_res_file.write("\n\nEpoch {}:".format(self.epoch))
                self.extended_log(self.vocab, self.eval_res_file, q_vec, gold_s, pred_s, ems, f1s)

            """
            self.extra_log_process = \
                multiprocessing.Process(target=self.extended_log,
                                        args=(self.vocab, self.eval_res_file, q_vec, gold_s, pred_s, ems, f1s))
            self.extra_log_process.start()
            """""

        return f1, em

    @staticmethod
    def extended_log(vocab, eval_res_file, q_vec, gold_s, pred_s, ems, f1s):
        # all the evaluate info
        text = lambda vecs: ' '.join(vocab[i] for i in vecs)

        # sorting into buckets
        em_sents, partial_matches, no_match, empty = [[] for i in range(4)]
        for ques, gold, our, is_em, sample_f1 in zip(q_vec, gold_s, pred_s, ems, f1s):
            if len(our) == 0:
                empty.append((ques, gold))
            elif is_em:
                em_sents.append((ques, gold))
            elif sample_f1 > 0:
                partial_matches.append((ques, gold, our))
            else:
                no_match.append((ques, gold, our))


        # Yes, my fellow CS107 TAs will hate this....
        if len(em_sents):
            eval_res_file.write("Sample Exact Matches")
            for ques, gold in em_sents[:5]:
                eval_res_file.write("Ques: " + text(ques))
                eval_res_file.write("Answ: " + gold)

        if len(empty):
            eval_res_file.write("Sents where we didn't predict anything")
            for ques, gold in empty[:5]:
                eval_res_file.write("Ques: " + text(ques))
                eval_res_file.write("Answ: " + gold)

        if len(partial_matches):
            eval_res_file.write("Partial matches")
            for ques, gold, our in partial_matches[:5]:
                eval_res_file.write("Ques: " + text(ques))
                eval_res_file.write("Answ: " + gold)
                eval_res_file.write("OurA: " + our)

        if len(no_match):
            eval_res_file.write("Partial matches")
            for ques, gold, our in no_match[:5]:
                eval_res_file.write("Ques: " + text(ques))
                eval_res_file.write("Answ: " + gold)
                eval_res_file.write("OurA: " + our)


