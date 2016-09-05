"""Sequence-to-tree model with an attention mechanism."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import numpy as np

import random
import tensorflow as tf

import encoder, decoder
import data_utils
import graph_utils

class EncoderDecoderModel(object):

    def __init__(self, hyperparams, buckets=None, forward_only=False):
        """Create the model.

        Hyperparameters:
          source_vocab_size: size of the source vocabulary.
          target_vocab_size: size of the target vocabulary.
          buckets: a list of pairs (I, O), where I specifies maximum input length
            that will be processed in that bucket, and O specifies maximum output
            length. Training instances that have inputs longer than I or outputs
            longer than O will be pushed to the next bucket and padded accordingly.
            We assume that the list is sorted, e.g., [(2, 4), (8, 16)].
          size: number of units in each layer of the model.
          num_layers: number of layers in the model.
          max_gradient_norm: gradients will be clipped to maximally this norm.
          batch_size: the size of the batches used during training;
            the model construction is independent of batch_size, so it can be
            changed after initialization if this is convenient, e.g., for decoding.
          learning_rate: learning rate to start with.
          learning_rate_decay_factor: decay learning rate by this much when needed.
          use_lstm: if true, we use LSTM cells instead of GRU cells.
          num_samples: number of samples for sampled softmax.
          forward_only: if set, we do not construct the backward pass in the model.
          beam_decoder: beam search decoder.
          use_attention: if set, use attention model.
        """

        self.hyperparams = hyperparams
        self.buckets = buckets

        self.learning_rate = tf.Variable(float(hyperparams["learning_rate"]), trainable=False)
        self.learning_rate_decay_op = self.learning_rate.assign(
            self.learning_rate * hyperparams["learning_rate_decay_factor"])

        # variable sharing
        self.output_projection_vars = False
        self.source_embedding_vars = False
        self.target_embedding_vars = False


    def source_embeddings(self):
        with tf.variable_scope("source_embeddings"):
            sqrt3 = math.sqrt(3)
            initializer = tf.random_normal_initializer(-sqrt3, sqrt3)
            if self.source_embedding_vars:
                tf.get_variable_scope().reuse_variables()
            embeddings = tf.get_variable("embedding", [self.source_vocab_size,
                                                       self.dim],
                                         initializer=initializer)
            self.source_embedding_vars = True
            return embeddings


    def target_embeddings(self):
        with tf.variable_scope("target_embeddings"):
            sqrt3 = math.sqrt(3)
            initializer = tf.random_normal_initializer(-sqrt3, sqrt3)
            if self.target_embedding_vars:
                tf.get_variable_scope().reuse_variables()
            embeddings = tf.get_variable("embedding", [self.target_vocab_size,
                                                       self.dim],
                                         initializer=initializer)
            self.target_embedding_vars = True
            return embeddings


    def format_example(self, encoder_input, decoder_input, bucket_id=-1,
                       original_encoder_input=None,
                       original_decoder_input=None,
                       copy_mask=None):
        """Prepare data to feed in step()"""
        if bucket_id >= 0:
            encoder_size, decoder_size = self.buckets[bucket_id]
        else:
            encoder_size, decoder_size = self.max_source_length, self.max_target_length

        encoder_inputs = []
        decoder_inputs = []
        if self.use_copy:
            original_encoder_inputs = []
            original_decoder_inputs = []
            copy_masks = []

        # Encoder inputs are padded and then reversed
        encoder_pad = [data_utils.PAD_ID] * (encoder_size - len(encoder_input))
        encoder_inputs.append(list(reversed(encoder_input + encoder_pad)))
        if self.use_copy:
            copy_mask_pad = [0] * (encoder_size - len(encoder_input))
            original_encoder_inputs.append(list(reversed(original_encoder_input + encoder_pad)))
            copy_masks.append(list(reversed(copy_mask + copy_mask_pad)))

        decoder_pad = [data_utils.PAD_ID] * (decoder_size - len(decoder_input))
        decoder_inputs.append(decoder_input + decoder_pad)

        # create batch-major vectors
        batch_size = 1
        batch_encoder_inputs, batch_decoder_inputs, batch_weights = [], [], []
        if self.use_copy:
            batch_original_encoder_inputs, batch_original_decoder_inputs, batch_copy_masks = [], [], []

        # Batch encoder inputs are just re-indexed encoder_inputs.
        for length_idx in xrange(encoder_size):
            batch_encoder_inputs.append(
                np.array([encoder_inputs[batch_idx][length_idx]
                          for batch_idx in xrange(batch_size)], dtype=np.int32))
            if self.use_copy:
                batch_original_decoder_inputs.append(
                    np.array([original_decoder_inputs[batch_idx][length_idx]
                          for batch_idx in xrange(batch_size)], dtype=np.int32))
                # Create copy_masks to be 0 for encoder inputs that are not copiable
                batch_copy_mask = np.zeros(batch_size, dtype=np.int32)
                for batch_idx in xrange(batch_size):
                    if copy_masks[batch_idx][length_idx] == 1:
                        batch_copy_mask[batch_idx] = 1
                batch_copy_masks.append(batch_copy_mask)

        # Batch decoder inputs are re-indexed decoder_inputs.
        for length_idx in xrange(decoder_size):
            batch_decoder_inputs.append(
                np.array([decoder_inputs[batch_idx][length_idx]
                          for batch_idx in xrange(batch_size)], dtype=np.int32))
            if self.use_copy:
                batch_original_decoder_inputs.append(
                    np.array([original_decoder_inputs[batch_idx][length_idx]
                          for batch_idx in xrange(batch_size)], dtype=np.int32))

            # Create target_weights to be 0 for targets that are padding.
            batch_weight = np.ones(batch_size, dtype=np.float32)
            for batch_idx in xrange(batch_size):
                # We set weight to 0 if the corresponding target is a PAD symbol.
                # The corresponding target is decoder_input shifted by 1 forward.
                if length_idx < decoder_size - 1:
                    target = decoder_inputs[batch_idx][length_idx + 1]
                if length_idx == decoder_size - 1 or target == data_utils.PAD_ID:
                    batch_weight[batch_idx] = 0.0
            batch_weights.append(batch_weight)

        if self.use_copy:
            return batch_encoder_inputs, batch_decoder_inputs, batch_weights, \
                batch_original_encoder_inputs, batch_original_decoder_inputs, batch_copy_masks
        else:
            return batch_encoder_inputs, batch_decoder_inputs, batch_weights


    def get_batch(self, data, bucket_id):
        """Get a random batch of data from the specified bucket, prepare for step.

        To feed data in step(..) it must be a list of batch-major vectors, while
        data here contains single length-major cases. So the main logic of this
        function is to re-index data cases to be in the proper format for feeding.

        Args:
          data: a tuple of size len(self.buckets) in which each element contains
            lists of pairs of input and output data that we use to create a batch.
          bucket_id: integer, which bucket to get the batch for.
          add_extra_go: if set to True, add an extra "GO" symbol to decoder inputs.
        Returns:
          The triple (encoder_inputs, decoder_inputs, target_weights) for
          the constructed batch that has the proper format to call step(...) later.
        """
        encoder_size, decoder_size = self.buckets[bucket_id]
        encoder_inputs, decoder_inputs = [], []

        # Get a random batch of encoder and decoder inputs from data,
        # pad them if needed, reverse encoder inputs and add GO to decoder.
        for _ in xrange(self.batch_size):
            _, _, encoder_input, decoder_input = random.choice(data[bucket_id])

            # Encoder inputs are padded and then reversed.
            encoder_pad = [data_utils.PAD_ID] * (encoder_size - len(encoder_input))
            encoder_inputs.append(list(reversed(encoder_input + encoder_pad)))

            decoder_pad_size = decoder_size - len(decoder_input)
            decoder_inputs.append(decoder_input + [data_utils.PAD_ID] * decoder_pad_size)

        # Now we create batch-major vectors from the data selected above.
        batch_encoder_inputs, batch_decoder_inputs, batch_weights = [], [], []

        # Batch encoder inputs are just re-indexed encoder_inputs.
        for length_idx in xrange(encoder_size):
            batch_encoder_inputs.append(
                np.array([encoder_inputs[batch_idx][length_idx]
                          for batch_idx in xrange(self.batch_size)], dtype=np.int32))

        # Batch decoder inputs are re-indexed decoder_inputs, we create weights.
        for length_idx in xrange(decoder_size):
            batch_decoder_inputs.append(
                np.array([decoder_inputs[batch_idx][length_idx]
                          for batch_idx in xrange(self.batch_size)], dtype=np.int32))

            # Create target_weights to be 0 for targets that are padding.
            batch_weight = np.ones(self.batch_size, dtype=np.float32)
            for batch_idx in xrange(self.batch_size):
                # We set weight to 0 if the corresponding target is a PAD symbol.
                # The corresponding target is decoder_input shifted by 1 forward.
                if length_idx < decoder_size - 1:
                    target = decoder_inputs[batch_idx][length_idx + 1]
                if length_idx == decoder_size - 1 or target == data_utils.PAD_ID:
                    batch_weight[batch_idx] = 0.0
            batch_weights.append(batch_weight)
        return batch_encoder_inputs, batch_decoder_inputs, batch_weights


    def get_bucket(self, data, bucket_id=-1, feed_previous=False):
        """Get all elements of a bucket, prepare for step.

        To feed data in step(..) it must be a list of batch-major vectors, while
        data here contains single length-major cases. So the main logic of this
        function is to re-index data cases to be in the proper format for feeding.

        Args:
          data: a tuple of size len(self.buckets) in which each element contains
            lists of pairs of input and output data that we use to create a batch.
          bucket_id: integer, which bucket to get the batch for.
          feed_previous: if set to True, read the first decoder symbol only.
        Returns:
          The triple (encoder_inputs, decoder_inputs, target_weights) for
          the constructed batch that has the proper format to call step(...) later.
        """
        encoder_size, decoder_size = self.buckets[bucket_id]
        encoder_inputs, decoder_inputs = [], []

        # Get encoder and decoder inputs from a bucket,
        # pad them if needed, reverse encoder inputs.
        for i in xrange(len(data[bucket_id])):
            encoder_input, decoder_input = data[bucket_id][i]

            # Encoder inputs are padded and then reversed.
            encoder_pad = [data_utils.PAD_ID] * (encoder_size - len(encoder_input))
            encoder_inputs.append(list(reversed(encoder_input + encoder_pad)))

            # Decoder inputs are padded then.
            decoder_pad_size = decoder_size - len(decoder_input)
            decoder_inputs.append(decoder_input +
                                  [data_utils.PAD_ID] * decoder_pad_size)

        # Now we create batch-major vectors from the data selected above.
        batch_encoder_inputs, batch_decoder_inputs, batch_weights = [], [], []

        # Batch encoder inputs are just re-indexed encoder_inputs.
        for length_idx in xrange(encoder_size):
            batch_encoder_inputs.append(
                np.array([encoder_inputs[batch_idx][length_idx]
                          for batch_idx in xrange(self.batch_size)], dtype=np.int32))

        # Batch decoder inputs are re-indexed decoder_inputs, we create weights.
        for length_idx in xrange(decoder_size):
            batch_decoder_inputs.append(
                np.array([decoder_inputs[batch_idx][length_idx]
                          for batch_idx in xrange(self.batch_size)], dtype=np.int32))

            # Create target_weights to be 0 for targets that are padding.
            batch_weight = np.ones(self.batch_size, dtype=np.float32)
            for batch_idx in xrange(self.batch_size):
                # We set weight to 0 if the corresponding target is a PAD symbol.
                # The corresponding target is decoder_input shifted by 1 forward.
                if length_idx < decoder_size - 1:
                    target = decoder_inputs[batch_idx][length_idx + 1]
                if length_idx == decoder_size - 1 or target == data_utils.PAD_ID:
                    batch_weight[batch_idx] = 0.0
            batch_weights.append(batch_weight)
        return batch_encoder_inputs, batch_decoder_inputs, batch_weights


    @property
    def use_sampled_softmax(self):
        return self.num_samples > 0 and self.num_samples < self.target_vocab_size

    @property
    def use_attention(self):
        return self.hyperparams["use_attention"]

    @property
    def use_copy(self):
        return self.hyperparams["use_copy"]

    @property
    def encoder_topology(self):
        return self.hyperparams["encoder_topology"]

    @property
    def decoder_topology(self):
        return self.hyperparams["decoder_topology"]

    @property
    def dim(self):
        return self.hyperparams["dim"]

    @property
    def batch_size(self):
        return self.hyperparams["batch_size"]

    @property
    def input_keep_prob(self):
        return self.hyperparams["input_keep_prob"]

    @property
    def output_keep_prob(self):
        return self.hyperparams["output_keep_prob"]

    @property
    def rnn_cell(self):
        return self.hyperparams["rnn_cell"]

    @property
    def max_gradient_norm(self):
        return self.hyperparams["max_gradient_norm"]

    @property
    def num_samples(self):
        return self.hyperparams["num_samples"]

    @property
    def num_layers(self):
        return self.hyperparams["num_layers"]

    @property
    def source_vocab_size(self):
        return self.hyperparams["source_vocab_size"]

    @property
    def target_vocab_size(self):
        return self.hyperparams["target_vocab_size"]

    @property
    def max_source_length(self):
        return self.hyperparams["max_source_length"]

    @property
    def max_target_length(self):
        return self.hyperparams["max_target_length"]


class Seq2TreeModel(EncoderDecoderModel):
    """Sequence-to-tree model with attention.
    """
    def __init__(self, hyperparams, buckets=None, forward_only=False):
        """
        Create the model.
        :param hyperparams: learning hyperparameters
        :param buckets: if not None, train bucket model.
        :param forward_only: if set, we do not construct the backward pass.
        """
        super(Seq2TreeModel, self).__init__(hyperparams, buckets, forward_only)

        self.global_epoch = tf.Variable(0, trainable=False)

        self.define_graph(forward_only)


    def define_graph(self, forward_only):
        # Feeds for inputs.
        self.encoder_inputs = []  # encoder inputs.
        self.decoder_inputs = []  # decoder inputs (always start with "root").
        self.target_weights = []  # weights at each position of the target sequence.

        for i in xrange(self.max_source_length):
            self.encoder_inputs.append(tf.placeholder(tf.int32, shape=[None],
                                                      name="encoder{0}".format(i)))
        for i in xrange(self.max_target_length + 1):
            self.decoder_inputs.append(tf.placeholder(tf.int32, shape=[None],
                                                      name="decoder{0}".format(i)))
            self.target_weights.append(tf.placeholder(tf.float32, shape=[None],
                                                      name="weight{0}".format(i)))
        # Our targets are decoder inputs shifted by one.
        self.targets = [self.decoder_inputs[i + 1]
                        for i in xrange(self.max_target_length)]

        if self.use_copy:
            self.original_encoder_inputs = []   # original encoder inputs.
                                                # used for accurate detection of copy action.
            self.original_decoder_inputs = []   # original decoder inputs.
                                                # used for accurate detection of copy action.
            self.copy_masks = []                # copy masks.
                                                # mark position in the inputs that are copyable.
            for i in xrange(self.max_source_length):
                self.original_encoder_inputs.append(tf.placeholder(tf.int32, shape=[None],
                                                    name="original_encoder{0}".format(i)))
                self.copy_masks.append(tf.placeholder(tf.int32, shape=[None],
                                                      name="copy_mask{0}".format(i)))
            for i in xrange(self.max_target_length):
                self.original_decoder_inputs.append(tf.placeholder(tf.int32, shape=[None],
                                                    name="original_decoder{0}".format(i)))

        # Encoder.
        if self.encoder_topology == "rnn":
            self.encoder = encoder.RNNEncoder(self.dim, self.rnn_cell, self.num_layers)
        elif self.encoder_topology == "birnn":
            self.encoder = encoder.BiRNNEncoder(self.dim, self.rnn_cell, self.num_layers)
        else:
            raise ValueError("Unrecognized encoder type.")

        # Decoder.
        if self.decoder_topology == "basic_tree":
            self.decoder = decoder.BasicTreeDecoder(self.dim, self.batch_size, self.rnn_cell, self.num_layers,
                                                self.input_keep_prob, self.output_keep_prob,
                                                self.use_attention, self.use_copy,
                                                self.output_projection())
        else:
            raise ValueError("Unrecognized decoder type.")

        # Compute raining outputs and losses in the forward direction.
        if self.buckets:
            self.outputs = []
            self.losses = []
            for bucket_id, bucket in enumerate(self.buckets):
                print("creating bucket {} ({}, {})...".format(
                                       bucket_id, bucket[0], bucket[1]))
                bucket_outputs, bucket_losses = self.encode_decode(
                    self.encoder_inputs[:bucket[0]], self.source_embeddings(),
                    self.decoder_inputs[:bucket[1]], self.target_embeddings(),
                    forward_only=forward_only
                )
                self.outputs.append(bucket_outputs)
                self.losses.append(bucket_losses)
        else:
            self.outputs, self.losses = self.encode_decode(
                self.encoder_inputs, self.source_embeddings(),
                self.decoder_inputs, self.target_embeddings(),
                forward_only=forward_only
            )

        # Gradients and SGD updates in the backward direction.
        params = tf.trainable_variables()
        if not forward_only:
            if self.hyperparams["optimizer"] == "sgd":
                opt = tf.train.GradientDescentOptimizer(self.learning_rate)
            elif self.hyperparams["optimizer"] == "adam":
                opt = tf.train.AdamOptimizer(self.learning_rate, beta1=0.9,
                                             beta2=0.999, epsilon=1e-08)
            else:
                raise ValueError("Unrecognized optimizer type.")

            if self.buckets:
                self.gradient_norms = []
                self.updates = []
                for bucket_id, _ in enumerate(self.buckets):
                    gradients = tf.gradients(self.losses[bucket_id], params)
                    clipped_gradients, norm = tf.clip_by_global_norm(gradients,
                                                                     self.max_gradient_norm)
                    self.gradient_norms.append(norm)
                    self.updates.append(opt.apply_gradients(
                        zip(clipped_gradients, params)))
            else:
                gradients = tf.gradients(self.losses, params)
                clipped_gradients, norm = tf.clip_by_global_norm(gradients,
                                                             self.max_gradient_norm)
                self.gradient_norms = norm
                self.updates = opt.apply_gradients(zip(clipped_gradients, params))

        self.saver = tf.train.Saver(tf.all_variables())


    def encode_decode(self, encoder_inputs, source_embeddings, decoder_inputs,
                      target_embeddings, forward_only):
        encoder_outputs, encoder_state = self.encoder.define_graph(encoder_inputs,
                                                               source_embeddings)
        if self.use_attention:
            top_states = [tf.reshape(e, [-1, 1, self.dim]) for e in encoder_outputs]
            attention_states = tf.concat(1, top_states)
            outputs, state = self.decoder.define_graph(encoder_state, decoder_inputs, target_embeddings,
                                                       attention_states, feed_previous=forward_only)
        else:
            outputs, state = self.decoder.define_graph(encoder_state, decoder_inputs, target_embeddings,
                                                       feed_previous=forward_only)

        # Losses.
        losses = self.sequence_loss(outputs, self.softmax_loss())

        # Project decoder outputs for decoding.
        W, b = self.output_projection()
        projected_outputs = []
        for i in xrange(len(outputs)):
            projected_outputs.append((tf.matmul(outputs[i], W) + b))

        return projected_outputs, losses


    def output_projection(self):
        with tf.variable_scope("output_projection"):
            try:
                w = tf.get_variable("proj_w", [self.dim, self.target_vocab_size])
                b = tf.get_variable("proj_b", [self.target_vocab_size])
            except ValueError, e:
                tf.get_variable_scope().reuse_variables()
                w = tf.get_variable("proj_w", [self.dim, self.target_vocab_size])
                b = tf.get_variable("proj_b", [self.target_vocab_size])
        return (w, b)


    def sequence_loss(self, logits, loss_function):
        targets = self.targets[:len(logits)]
        weights = self.target_weights[:len(logits)]

        with tf.variable_scope("sequence_loss"):
            log_perp_list = []
            for logit, target, weight in zip(logits, targets, weights):
                crossent = loss_function(logit, target)
                log_perp_list.append(crossent * weight)
            log_perps = tf.add_n(log_perp_list)
            total_size = tf.add_n(weights)
            total_size += 1e-12  # Just to avoid division by 0 for all-0 weights.
            log_perps /= total_size
        return log_perps


    def softmax_loss(self):
        if self.use_sampled_softmax:
            w, b = self.output_projection()
            w_t = tf.transpose(w)

            def sampled_loss(inputs, labels):
                labels = tf.reshape(labels, [-1, 1])
                return tf.nn.sampled_softmax_loss(w_t, b, inputs, labels, self.num_samples,
                                                  self.target_vocab_size)

            loss_function = sampled_loss
        else:
            loss_function = tf.nn.softmax_cross_entropy_with_logits
        return loss_function


    def step(self, session, formatted_example, bucket_id=-1, forward_only=False):
        """Run a step of the model feeding the given inputs.
        :param session: tensorflow session to use.
        :param encoder_inputs: list of numpy int vectors to feed as encoder inputs.
        :param decoder_inputs: list of numpy int vectors to feed as decoder inputs.
        :param target_weights: list of numpy float vectors to feed as target weights.
        :param bucket_id: which bucket of the model to use.
        :param forward_only: whether to do the backward step or only forward.
        :return (gradient_norm, average_perplexity, outputs)
        """
        # Unwarp data tensors
        if self.use_copy:
            encoder_inputs, decoder_inputs, target_weights, \
            original_encoder_inputs, original_decoder_inputs, copy_masks = formatted_example
        else:
            encoder_inputs, decoder_inputs, target_weights = formatted_example

        # Check if the sizes match.
        if bucket_id == -1:
            encoder_size, decoder_size = len(encoder_inputs), len(decoder_inputs)
            assert(encoder_size == self.max_source_length)
            assert(decoder_size == self.max_target_length)
        else:
            encoder_size, decoder_size = self.buckets[bucket_id]
            if len(encoder_inputs) != encoder_size:
                raise ValueError("Encoder length must be equal to the one in bucket,"
                                 " %d != %d." % (len(encoder_inputs), encoder_size))
            if len(decoder_inputs) != decoder_size:
                raise ValueError("Decoder length must be equal to the one in bucket,"
                                 " %d != %d." % (len(decoder_inputs), decoder_size))
            if len(target_weights) != decoder_size:
                raise ValueError("Weights length must be equal to the one in bucket,"
                                 " %d != %d." % (len(target_weights), decoder_size))

        # Input feed: encoder inputs, decoder inputs, target_weights, as provided.
        input_feed = {}
        for l in xrange(encoder_size):
            input_feed[self.encoder_inputs[l].name] = encoder_inputs[l]
        for l in xrange(decoder_size):
            input_feed[self.decoder_inputs[l].name] = decoder_inputs[l]
            input_feed[self.target_weights[l].name] = target_weights[l]
        if self.use_copy:
            for l in xrange(encoder_size):
                input_feed[self.original_encoder_inputs[l].name] = original_encoder_inputs[l]
                input_feed[self.copy_masks[l].name] = copy_masks[l]
            for l in xrange(decoder_size):
                input_feed[self.original_decoder_inputs[l].name] = original_decoder_inputs[l]

        # Since our targets are decoder inputs shifted by one, we need one more.
        last_target = self.decoder_inputs[decoder_size].name
        input_feed[last_target] = np.zeros([self.batch_size], dtype=np.int32)

        # Output feed: depends on whether we do a backward step or not.
        if not forward_only:
            if bucket_id == -1:
                output_feed = [self.updates,            # Update Op that does SGD.
                               self.gradient_norms,     # Gradient norm.
                               self.losses]             # Loss for this batch.
            else:
                output_feed = [self.updates[bucket_id],            # Update Op that does SGD.
                               self.gradient_norms[bucket_id],     # Gradient norm.
                               self.losses[bucket_id]]             # Loss for this batch.
        else:
            if bucket_id == -1:
                output_feed = [self.losses]  # Loss for this batch.
                for l in xrange(decoder_size):  # Output logits.
                    output_feed.append(self.outputs[l])
            else:
                output_feed = [self.losses[bucket_id]]  # Loss for this batch.
                for l in xrange(decoder_size):  # Output logits.
                    output_feed.append(self.outputs[bucket_id][l])

        outputs = session.run(output_feed, input_feed)

        if not forward_only:
            return outputs[1], outputs[2], None  # Gradient norm, loss, no outputs.
        else:
            return None, outputs[0], outputs[1:]  # No gradient norm, loss, outputs.
