import tensorflow as tf
from deepctr.layers.normalization import LayerNormalization
from deepctr.layers.utils import softmax, reduce_mean, reduce_sum
from deepctr.layers.interaction import activation_layer
from tensorflow.python.keras.initializers import TruncatedNormal
from tensorflow.python.keras.layers import Layer, Dense, Dropout


class DotAttention(Layer):
    """
    :param query: [batch_size, 1, C]
    :param key:   [batch_size, T, C]
    :return:      [batch_size, 1, T]
    """

    def __init__(self, scale=True, **kwargs):
        self.scale = scale
        super(DotAttention, self).__init__(**kwargs)

    def build(self, input_shape):
        if not isinstance(input_shape, list) or len(input_shape) != 2:
            raise ValueError('A `DotAttention` layer should be called '
                             'on a list of 2 tensors')
        if input_shape[0][-1] != input_shape[1][-1]:
            raise ValueError('query_size should keep the same dim with key_size')
        super(DotAttention, self).build(input_shape)

    def call(self, inputs, mask=None, **kwargs):
        query, key = inputs
        output = tf.matmul(query, tf.transpose(key, [0, 2, 1]))
        if self.scale == True:
            output = output / (key.get_shape().as_list()[-1] ** 0.5)
        return output

    def compute_output_shape(self, input_shape):
        return (None, 1, input_shape[1][1])

    def compute_mask(self, inputs, mask):
        return mask


class ConcatAttention(Layer):
    """
    :param query: [batch_size, T, C_q]
    :param key:   [batch_size, T, C_k]
    :return:      [batch_size, 1, T]
        query_size should keep the same dim with key_size
    """

    def __init__(self, scale=True, **kwargs):
        self.scale = scale
        super(ConcatAttention, self).__init__(**kwargs)

    def build(self, input_shape):
        if not isinstance(input_shape, list) or len(input_shape) != 2:
            raise ValueError('A `ConcatAttention` layer should be called '
                             'on a list of 2 tensors')
        self.projection_layer = Dense(units=1, activation='tanh')
        super(ConcatAttention, self).build(input_shape)

    def call(self, inputs, mask=None, **kwargs):
        query, key = inputs
        q_k = tf.concat([query, key], axis=-1)
        output = self.projection_layer(q_k)
        if self.scale == True:
            output = output / (key.get_shape().as_list()[-1] ** 0.5)
        output = tf.transpose(output, [0, 2, 1])
        return output

    def compute_output_shape(self, input_shape):
        return (None, 1, input_shape[1][1])

    def compute_mask(self, inputs, mask):
        return mask


class SoftmaxWeightedSum(Layer):
    """
    :param align:           [batch_size, 1, T]
    :param value:           [batch_size, T, units]
    :param key_masks:       [batch_size, 1, T]
                            2nd dim size with align
    :param drop_out:
    :param future_binding:
    :return:                weighted sum vector
                            [batch_size, 1, units]
    """

    def __init__(self, dropout_rate=0.2, future_binding=False, seed=2020, **kwargs):
        self.dropout_rate = dropout_rate
        self.future_binding = future_binding
        self.seed = seed
        super(SoftmaxWeightedSum, self).__init__(**kwargs)

    def build(self, input_shape):
        if not isinstance(input_shape, list) or len(input_shape) != 3:
            raise ValueError('A `SoftmaxWeightedSum` layer should be called '
                             'on a list of 3 tensors')
        if input_shape[0][-1] != input_shape[2][-1]:
            raise ValueError('query_size should keep the same dim with key_mask_size')
        self.dropout = Dropout(self.dropout_rate, seed=self.seed)
        super(SoftmaxWeightedSum, self).build(input_shape)

    def call(self, inputs, mask=None, training=None, **kwargs):
        align, value, key_masks = inputs
        paddings = tf.ones_like(align) * (-2 ** 32 + 1)
        align = tf.where(key_masks, align, paddings)
        if self.future_binding:
            length = value.get_shape().as_list()[1]
            lower_tri = tf.ones([length, length])
            try:
                lower_tri = tf.contrib.linalg.LinearOperatorTriL(lower_tri).to_dense()
            except:
                lower_tri = tf.linalg.LinearOperatorLowerTriangular(lower_tri).to_dense()
            masks = tf.tile(tf.expand_dims(lower_tri, 0), [tf.shape(align)[0], 1, 1])
            align = tf.where(tf.equal(masks, 0), paddings, align)
        align = softmax(align)
        align = self.dropout(align, training=training)
        output = tf.matmul(align, value)
        return output

    def compute_output_shape(self, input_shape):
        return (None, 1, input_shape[1][1])

    def get_config(self, ):
        config = {'dropout_rate': self.dropout_rate, 'future_binding': self.future_binding}
        base_config = super(SoftmaxWeightedSum, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

    def compute_mask(self, inputs, mask):
        return mask


class AttentionSequencePoolingLayer(Layer):
    """
    :param query:           [batch_size, 1, C_q]
    :param keys:            [batch_size, T, C_k]
    :param keys_length:      [batch_size, 1]
    :return:                [batch_size, 1, C_k]
    """

    def __init__(self, dropout_rate=0, **kwargs):
        self.dropout_rate = dropout_rate
        super(AttentionSequencePoolingLayer, self).__init__(**kwargs)

    def build(self, input_shape):
        if not isinstance(input_shape, list) or len(input_shape) != 3:
            raise ValueError('A `SequenceFeatureMask` layer should be called '
                             'on a list of 3 inputs')
        self.concat_att = ConcatAttention()
        self.softmax_weight_sum = SoftmaxWeightedSum(dropout_rate=self.dropout_rate, future_binding=False)
        super(AttentionSequencePoolingLayer, self).build(input_shape)

    def call(self, inputs, mask=None, **kwargs):
        queries, keys, keys_length = inputs
        hist_len = keys.get_shape()[1]
        key_masks = tf.sequence_mask(keys_length, hist_len)
        queries = tf.tile(queries, [1, hist_len, 1])  # [batch_size, T, units]
        attention_score = self.concat_att([queries, keys])  # [batch_size, 1, units]

        outputs = self.softmax_weight_sum([attention_score, keys, key_masks])
        # [batch_size, units]
        return outputs

    def compute_output_shape(self, input_shape):
        return (None, 1, input_shape[1][1])

    def get_config(self, ):
        config = {'dropout_rate': self.dropout_rate}
        base_config = super(AttentionSequencePoolingLayer, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

    def compute_mask(self, inputs, mask):
        return mask


class SelfAttention(Layer):
    """
      :param input: A 3d tensor with shape of  [batch_size, 1, C]
      :param key_masks: A 3d tensor with shape of  [batch_size, 1]
      :return: A 3d tensor with shape of  [batch_size, 1]
    """

    def __init__(self, scale=True, dropout_rate=0.2, future_binding=True, use_layer_norm=True, seed=2020, **kwargs):
        self.scale = scale
        self.dropout_rate = dropout_rate
        self.future_binding = future_binding
        self.use_layer_norm = use_layer_norm
        self.seed = seed
        super(SelfAttention, self).__init__(**kwargs)

    def build(self, input_shape):
        if not isinstance(input_shape, list) or len(input_shape) != 2:
            raise ValueError('A `SelfAttention` layer should be called '
                             'on a list of 2 tensors')
        self.layer_norm = LayerNormalization()
        self.attention = DotAttention(scale=self.scale)
        self.softmax_weight_sum = SoftmaxWeightedSum(dropout_rate=self.dropout_rate, future_binding=self.future_binding,
                                                     seed=self.seed)
        super(SelfAttention, self).build(input_shape)

    def call(self, inputs, mask=None, **kwargs):
        input, key_masks = inputs
        querys, keys, values = input, input, input
        align = self.attention([querys, keys])
        output = self.softmax_weight_sum([align, values, key_masks])
        if self.use_layer_norm:
            output = self.layer_norm(output)
        return reduce_mean(output, 1, keep_dims=True)

    def compute_output_shape(self, input_shape):
        return (None, 1, input_shape[0][-1])

    def compute_mask(self, inputs, mask):
        return mask


class SelfMultiHeadAttention(Layer):
    """
      :param query: A 3d tensor with shape of [batch_size, T, C]
      :param key_masks: A 3d tensor with shape of [batch_size, 1]
      :return: A 3d tensor with shape of  [batch_size, T, C]
    """

    def __init__(self, num_units=8, head_num=4, scale=True, dropout_rate=0.2, future_binding=True, use_layer_norm=True,
                 use_res=True,
                 seed=2020, **kwargs):
        if head_num <= 0:
            raise ValueError('head_num must be a int > 0')
        self.num_units = num_units
        self.head_num = head_num
        self.scale = scale
        self.dropout_rate = dropout_rate
        self.future_binding = future_binding
        self.use_layer_norm = use_layer_norm
        self.use_res = use_res
        self.seed = seed
        super(SelfMultiHeadAttention, self).__init__(**kwargs)

    def build(self, input_shape):
        if not isinstance(input_shape, list) or len(input_shape) != 2:
            raise ValueError('A `SelfMultiHeadAttention` layer should be called '
                             'on a list of 2 tensors')
        if len(input_shape[0]) != 3 or len(input_shape[1]) != 2:
            raise ValueError('input: [N, T_k, d_model], key masks: [N, key_seqlen]')
        embedding_size = int(input_shape[0][-1])
        if self.num_units == None:
            self.num_units = embedding_size
        self.W = self.add_weight(name='Q_K_V', shape=[embedding_size, self.num_units * 3],
                                 dtype=tf.float32,
                                 initializer=TruncatedNormal(seed=self.seed))
        self.W_output = self.add_weight(name='output_W', shape=[self.num_units, self.num_units],
                                        dtype=tf.float32,
                                        initializer=TruncatedNormal(seed=self.seed))

        self.layer_norm = LayerNormalization()
        self.attention = DotAttention(scale=self.scale)
        self.softmax_weight_sum = SoftmaxWeightedSum(dropout_rate=self.dropout_rate, future_binding=self.future_binding,
                                                     seed=self.seed)
        self.dropout = Dropout(self.dropout_rate, seed=self.seed)
        self.seq_len_max = int(input_shape[0][1])
        # Be sure to call this somewhere!
        super(SelfMultiHeadAttention, self).build(input_shape)

    def call(self, inputs, mask=None, training=None, **kwargs):
        input_info, keys_length = inputs

        hist_len = input_info.get_shape()[1]
        key_masks = tf.sequence_mask(keys_length, hist_len)
        key_masks = tf.squeeze(key_masks, axis=1)

        Q_K_V = tf.tensordot(input_info, self.W, axes=(-1, 0))  # [N T_q D*3]
        querys, keys, values = tf.split(Q_K_V, 3, -1)

        # head_num None F D
        querys = tf.concat(tf.split(querys, self.head_num, axis=2), axis=0)  # (h*N, T_q, C/h)
        keys = tf.concat(tf.split(keys, self.head_num, axis=2), axis=0)  # (h*N, T_k, C/h)
        values = tf.concat(tf.split(values, self.head_num, axis=2), axis=0)  # (h*N, T_k, C/h)

        # (h*N, T_q, T_k)
        align = self.attention([querys, keys])

        key_masks = tf.tile(key_masks, [self.head_num, 1])  # (h*N, T_k)
        key_masks = tf.tile(tf.expand_dims(key_masks, 1), [1, tf.shape(input_info)[1], 1])  # (h*N, T_q, T_k)

        outputs = self.softmax_weight_sum([align, values, key_masks])  # (h*N, T_q, C/h)
        outputs = tf.concat(tf.split(outputs, self.head_num, axis=0), axis=2)  # (N, T_q, C)

        outputs = tf.tensordot(outputs, self.W_output, axes=(-1, 0))  # (N, T_q, C)
        outputs = self.dropout(outputs, training=training)
        if self.use_res:
            outputs += input_info
        if self.use_layer_norm:
            outputs = self.layer_norm(outputs)

        return outputs

    def compute_output_shape(self, input_shape):
        return (None, input_shape[0][1], self.num_units)

    def get_config(self, ):
        config = {'num_units': self.num_units, 'head_num': self.head_num, 'scale': self.scale,
                  'dropout_rate': self.dropout_rate,
                  'future_binding': self.future_binding, 'use_layer_norm': self.use_layer_norm, 'use_res': self.use_res,
                  'seed': self.seed}
        base_config = super(SelfMultiHeadAttention, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

    def compute_mask(self, inputs, mask):
        return mask


class UserAttention(Layer):
    """
      :param query: A 3d tensor with shape of [batch_size, T, C]
      :param keys: A 3d tensor with shape of [batch_size, T, C]
      :param key_masks: A 3d tensor with shape of [batch_size, 1]
      :return: A 3d tensor with shape of  [batch_size, 1, C]
    """

    def __init__(self, num_units=None, activation='tanh', use_res=True, dropout_rate=0, scale=True, seed=2020,
                 **kwargs):
        self.scale = scale
        self.num_units = num_units
        self.activation = activation
        self.dropout_rate = dropout_rate
        self.seed = seed
        self.use_res = use_res
        super(UserAttention, self).__init__(**kwargs)

    def build(self, input_shape):
        if not isinstance(input_shape, list) or len(input_shape) != 3:
            raise ValueError('A `UserAttention` layer should be called '
                             'on a list of 3 tensors')
        if self.num_units == None:
            self.num_units = input_shape[0][-1]
        self.dense = Dense(self.num_units, activation=self.activation)
        self.attention = DotAttention(scale=self.scale)
        self.softmax_weight_sum = SoftmaxWeightedSum(dropout_rate=self.dropout_rate, seed=self.seed)
        super(UserAttention, self).build(input_shape)

    def call(self, inputs, mask=None, **kwargs):
        user_query, keys, keys_length = inputs
        hist_len = keys.get_shape()[1]
        key_masks = tf.sequence_mask(keys_length, hist_len)
        query = self.dense(user_query)

        align = self.attention([query, keys])

        output = self.softmax_weight_sum([align, keys, key_masks])

        if self.use_res:
            output += keys
        return reduce_mean(output, 1, keep_dims=True)

    def compute_output_shape(self, input_shape):
        return (None, 1, input_shape[1][2])

    def compute_mask(self, inputs, mask):
        return mask

    def get_config(self, ):
        config = {'num_units': self.num_units, 'activation': self.activation, 'use_res': self.use_res,
                  'dropout_rate': self.dropout_rate,
                  'scale': self.scale, 'seed': self.seed, }
        base_config = super(UserAttention, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class AdditiveAttention(Layer):
    """
    :param query: [batch_size, T_q, C]
    :param key:   [batch_size, T_k, C]
    :param seq_mask: [batch_size, 1]
    :return:      [batch_size, C]
    """

    def __init__(self, hidden_units=64, use_bias=True, activation="tanh", dropout_rate=0, seed=2020, **kwargs):
        self.hidden_units = hidden_units
        self.use_bias = use_bias
        self.activation = activation
        self.dropout_rate = dropout_rate
        self.seed = seed
        super(AdditiveAttention, self).__init__(**kwargs)

    def build(self, input_shape):
        if self.hidden_units != input_shape[0][-1] or self.hidden_units != input_shape[1][-1]:
            raise ValueError("The hidden units must be equal to the last dimension of queries and keys!")

        self.W_q = self.add_weight(name='W_q', shape=[self.hidden_units, self.hidden_units],
                                   dtype=tf.float32, initializer=TruncatedNormal(seed=self.seed))
        self.W_k = self.add_weight(name='W_k', shape=[self.hidden_units, self.hidden_units],
                                   dtype=tf.float32, initializer=TruncatedNormal(seed=self.seed))
        self.v = self.add_weight(name='v', shape=[self.hidden_units, 1],
                                 dtype=tf.float32, initializer=TruncatedNormal(seed=self.seed))
        if self.use_bias is True:
            self.b = self.add_weight(name='b', shape=[self.hidden_units],
                                     dtype=tf.float32, initializer=TruncatedNormal(seed=self.seed))
        self.activation_layer = activation_layer(self.activation)
        self.softmax_weighted_sum = SoftmaxWeightedSum(dropout_rate=self.dropout_rate, seed=self.seed)
        super(AdditiveAttention, self).build(input_shape)

    def call(self, inputs, mask=None, **kwargs):
        queries, keys, sequence_length = inputs
        values = keys
        queries_len = queries.get_shape().as_list()[1]
        keys_len, hidden_units = keys.get_shape().as_list()[1:]

        seq_mask = tf.sequence_mask(sequence_length, keys_len)
        seq_mask = tf.tile(seq_mask, multiples=[1, queries_len, 1])

        keys = tf.tensordot(tf.reshape(keys, shape=[-1, hidden_units]), self.W_k, axes=(-1, 0))
        keys_reshaped = tf.reshape(keys, shape=[-1, 1, keys_len, hidden_units])

        queries = tf.tensordot(tf.reshape(queries, shape=[-1, hidden_units]), self.W_q, axes=(-1, 0))
        queries_reshaped = tf.reshape(queries, shape=[-1, queries_len, 1, hidden_units])

        if self.use_bias is True:
            attn_value = self.activation_layer(tf.nn.bias_add(keys_reshaped + queries_reshaped, self.b))
        else:
            attn_value = self.activation_layer(keys_reshaped + queries_reshaped)

        scores = tf.tensordot(tf.reshape(attn_value, shape=[-1, hidden_units]), self.v, axes=(-1, 0))
        scores = tf.reshape(scores, shape=[-1, queries_len, keys_len])

        outputs = self.softmax_weighted_sum([scores, values, seq_mask])
        return outputs

    def compute_output_shape(self, input_shape):
        return input_shape[0]

    def compute_mask(self, inputs, mask=None):
        return mask

    def get_config(self):
        config = {'use_bias': self.use_bias, 'activation': self.activation_layer, 'dropout_rate': self.dropout_rate,
                  'seed': self.seed}
        base_config = super(AdditiveAttention, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class NARMEncoderLayer(Layer):
    """
    :inputs:  A 3d tensor with shape of  [batch_size, T, C]
    :param gru_hidden_units: tuple, hidden units of GRU layers.
    :return:  A 2d tensor with shape of  [batch_size, 2*last_gru_hidden_unit]
    """

    def __init__(self, gru_hidden_units=(64,), seed=2021, **kwargs):
        self.gru_hidden_units = gru_hidden_units
        self.seed = seed
        super(NARMEncoderLayer, self).__init__(**kwargs)

    def build(self, input_shape):
        self.gru_cells = []
        for i in range(len(self.gru_hidden_units)):
            try:
                self.gru_cell = tf.nn.rnn_cell.GRUCell(self.gru_hidden_units[i])
            except:
                self.gru_cell = tf.compat.v1.nn.rnn_cell.GRUCell(self.gru_hidden_units[i])
            self.gru_cells.append(self.gru_cell)
        self.local_encoder_layer = AdditiveAttention(hidden_units=self.gru_hidden_units[-1], use_bias=False,
                                                     activation="sigmoid", dropout_rate=0, seed=self.seed)
        super(NARMEncoderLayer, self).build(input_shape)

    def call(self, inputs, **kwargs):
        rnn_input, sequence_length = inputs
        for i in range(len(self.gru_hidden_units)):
            try:
                with tf.name_scope("rnn"), tf.variable_scope("rnn", reuse=tf.AUTO_REUSE):
                    rnn_output, hidden_state = tf.nn.dynamic_rnn(self.gru_cells[i], inputs=rnn_input,
                                                                 sequence_length=tf.squeeze(sequence_length),
                                                                 dtype=tf.float32, scope=self.name)
            except AttributeError:
                with tf.name_scope("rnn"), tf.compat.v1.variable_scope("rnn", reuse=tf.compat.v1.AUTO_REUSE):
                    rnn_output, hidden_state = tf.compat.v1.nn.dynamic_rnn(self.gru_cells[i], inputs=rnn_input,
                                                                           sequence_length=tf.squeeze(sequence_length),
                                                                           dtype=tf.float32, scope=self.name)
        user_global_output = hidden_state
        hidden_state = tf.expand_dims(hidden_state, axis=1)
        user_local_output = self.local_encoder_layer([rnn_output, hidden_state, sequence_length])
        user_local_output = reduce_sum(user_local_output, axis=1)
        user_output = tf.concat([user_global_output, user_local_output], axis=-1)

        return user_output

    def compute_output_shape(self, input_shape):
        return (None, 2 * self.gru_hidden_units[-1])

    def get_config(self):
        config = {'gru_hidden_units': self.gru_hidden_units, 'seed': self.seed}
        base_config = super(NARMEncoderLayer, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))
