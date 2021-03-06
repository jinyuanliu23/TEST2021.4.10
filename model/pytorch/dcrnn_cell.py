import numpy as np
import torch
# from MultiGAT import GAT
from lib import utils

from gatmodels import GATSubNet
device = torch.device("cuda:2" if torch.cuda.is_available() else "cpu")
import logging

class LayerParams:

    def __init__(self, rnn_network: torch.nn.Module, layer_type: str ):
        super().__init__()
        self._rnn_network = rnn_network
        self._params_dict = {}
        self._biases_dict = {}
        self._type = layer_type

    def get_weights(self, shape):
        if shape not in self._params_dict:
            nn_param = torch.nn.Parameter(torch.empty(*shape, device=device))
            torch.nn.init.xavier_normal_(nn_param)
            self._params_dict[shape] = nn_param
            self._rnn_network.register_parameter('{}_weight_{}'.format(self._type, str(shape)),
                                                 nn_param)
        return self._params_dict[shape]

    def get_biases(self, length, bias_start=0.0):
        if length not in self._biases_dict:
            biases = torch.nn.Parameter(torch.empty(length, device=device))
            torch.nn.init.constant_(biases, bias_start)
            self._biases_dict[length] = biases
            self._rnn_network.register_parameter('{}_biases_{}'.format(self._type, str(length)),
                                                 biases)

        return self._biases_dict[length]


class GAGRUCell(torch.nn.Module):
    def __init__(self, num_units, adj_mx,  num_nodes,input_dim, nonlinearity='tanh', use_ga_for_ru=True,**model_kwargs):
        """
        :param num_units:
        :param adj_mx:
        :param max_diffusion_step:
        :param num_nodes:
        :param nonlinearity:
        :param filter_type: "laplacian", "random_walk", "dual_random_walk".
        :param use_gc_for_ru: whether to use Graph convolution to calculate the reset and update gates.
        """
        super().__init__()
        self._activation = torch.tanh if nonlinearity == 'tanh' else torch.relu
        # support other nonlinearities up here?
        self._num_nodes = num_nodes
        self._num_units = num_units
        self.input_dim = input_dim
        self.multi_head_nums = int(model_kwargs.get('multi_head_nums', 4))
        self._supports = []
        self._use_ga_for_ru = use_ga_for_ru
        self.adj_mx = adj_mx
        self._fc_params = LayerParams(self, 'fc')
        self._gat1_params = LayerParams(self, 'gat1')
        self._gat2_params = LayerParams(self, 'gat2')
        # self.batch_size = 5
        # num_dim=(self._num_units + self.input_dim) * (self.batch_size)
        self.dim = (self._num_units + self.input_dim)
        self.batch_size = 1
        # self.dims = int(self.dim /4)
        # self.model1 = GATSubNet(num_dim,  num_dim,  num_dim, self.multi_head_nums )
        self.model1 = GATSubNet(int(self.dim ), int(self.dim ),int(self.dim ), self.multi_head_nums, dropout=0.6, alpha=0.2)
        # self.model11 = GATSubNet(num_dim, num_dim, num_dim, self.multi_head_nums)
        # self.model2 = GATSubNet(num_dim,  num_dim,  num_dim, self.multi_head_nums)
        self.model2 = GATSubNet(int(self.dim ), int(self.dim ), int(self.dim ), self.multi_head_nums, dropout=0.6, alpha=0.2)
        # self.model22 = GATSubNet(num_dim, num_dim, num_dim, self.multi_head_nums)
        self.linear1 = torch.nn.Linear(self.dim * self.batch_size,int(self.dim ))
        self.linear11 = torch.nn.Linear( int(self.dim ),self.dim * self.batch_size)
        self.linear2 = torch.nn.Linear(self.dim * self.batch_size,int(self.dim ))
        self.linear22 = torch.nn.Linear(int(self.dim ), self.dim* self.batch_size)

    def forward(self, inputs, hx):
        """Gated recurrent unit (GRU) with Graph Convolution.
        :param inputs: (B, num_nodes * input_dim)
        :param hx: (B, num_nodes * rnn_units)

        :return
        - Output: A `2-D` tensor with shape `(B, num_nodes * rnn_units)`.
        """
        output_size = 2 * self._num_units
        # logging.warning('cell_inputs{}'.format(inputs.shape))
        # logging.warning('cell_hx{}'.format(hx.shape))


        value = torch.sigmoid(self._GAT1(inputs, hx, output_size ))
        # logging.warning('cell_value1{}'.format(value.shape))
        value = torch.reshape(value, (-1, self._num_nodes, output_size))
        # logging.warning('cell_value2{}'.format(value.shape))
        # loss = value.sum()
        # loss.backward()
        # assert False
        r, u = torch.split(tensor=value, split_size_or_sections=self._num_units, dim=-1)
        r = torch.reshape(r, (-1, self._num_nodes , self._num_units))
        u = torch.reshape(u, (-1, self._num_nodes , self._num_units))

        c = self._GAT2(inputs, (r * hx), self._num_units )
        # logging.warning('cell_r*hx{}'.format((r * hx).shape))
        if self._activation is not None:
            c = self._activation(c)
        # print('u-shape',u.shape)
        # print('hx.shape',hx.shape)
        # print('c.shape',c.shape)
        new_state = u * hx + (1.0 - u) * c
        # logging.warning('cell_new_state{}'.format(new_state.shape))
        return new_state


    def _concat(x, x_):
        x_ = x_.unsqueeze(0)
        return torch.cat([x, x_], dim=0)

    def _fc(self, inputs, state, output_size, bias_start=0.0):
        batch_size = inputs.shape[0]
        inputs = torch.reshape(inputs, (batch_size * self._num_nodes, -1))
        state = torch.reshape(state, (batch_size * self._num_nodes, -1))
        inputs_and_state = torch.cat([inputs, state], dim=-1)
        input_size = inputs_and_state.shape[-1]
        weights = self._fc_params.get_weights((input_size, output_size))
        value = torch.sigmoid(torch.matmul(inputs_and_state, weights))
        biases = self._fc_params.get_biases(output_size, bias_start)
        value += biases
        return value



    def _GAT1(self, inputs, state, output_size, bias_start=0.0):
        # Reshape input and state to (batch_size, num_nodes, input_dim/state_dim)
        batch_size = inputs.shape[0]
        inputs = torch.reshape(inputs, (batch_size, self._num_nodes, -1))
        state = torch.reshape(state, (batch_size, self._num_nodes, -1))
        inputs_and_state = torch.cat([inputs, state], dim=-1)
        input_size = inputs_and_state.size(2)

        x = inputs_and_state
        x  = torch.reshape(x,(self._num_nodes,-1))
        # logging.warning('GATl_x1{}'.format(x.shape))
        # x = self.linear1(x)
        x = torch.squeeze(x,0)
        # logging.warning('GATl_x1{}'.format(x.shape))
        # logging.warning('GATl_x1{}'.format(x.shape))

        x = self.model1(x, self.adj_mx)
        # logging.warning('GATl_x1{}'.format(x.shape))
        # x = self.linear11(x)
        # logging.warning('GATl_x1{}'.format(x.shape))
        # logging.warning('GAT2_x2{}'.format(x.shape))
        x = torch.unsqueeze(x, 0)

        # x = self.model1x(x, self.adj_mx)

        # logging.warning('GAT2_x2{}'.format(x.shape))
        # x = torch.tensor(x)
        x = torch.reshape(x,(batch_size * self._num_nodes,input_size))
        weights = self._gat1_params.get_weights((input_size, output_size))
        x = torch.matmul(x, weights) # (batch_size * self._num_nodes, output_size)
        # x = torch.sigmoid(torch.matmul(x, weights))# (batch_size * self._num_nodes, output_size)
        biases = self._gat1_params.get_biases(output_size, bias_start)
        x =  x + biases

        x = torch.reshape(x, (batch_size , self._num_nodes, output_size))
        return  x

    def _GAT2(self, inputs, state, output_size, bias_start=0.0):
        # Reshape input and state to (batch_size, num_nodes, input_dim/state_dim)
        batch_size = inputs.shape[0]
        inputs = torch.reshape(inputs, (batch_size, self._num_nodes, -1))
        state = torch.reshape(state, (batch_size, self._num_nodes, -1))
        inputs_and_state = torch.cat([inputs, state], dim=2)
        input_size = inputs_and_state.size(2)


        x = inputs_and_state
        # logging.warning('GAT2_x1{}'.format(x.shape))
        # x  = torch.reshape(x,(self._num_nodes,-1))
        x = torch.squeeze(x, 0)
        # x = torch.reshape(x, (self._num_nodes, -1))
        # x = self.linear2(x)
        x = self.model2(x, self.adj_mx)
        # x = self.linear22(x)


        x = torch.unsqueeze(x, 0)
        # x = self.model22(x, self.adj_mx)
        # logging.warning('GAT2_x2{}'.format(x.shape))
        # x = torch.tensor(x)
        x = torch.reshape(x, (batch_size * self._num_nodes, input_size))
        weights = self._gat2_params.get_weights((input_size, output_size))
        x = torch.matmul(x, weights)
        # x = torch.sigmoid(torch.matmul(x, weights))# (batch_size * self._num_nodes, output_size)
        biases = self._gat2_params.get_biases(output_size, bias_start)
        x = x + biases
        # Reshape res back to 2D: (batch_size, num_node, state_dim) -> (batch_size, num_node * state_dim)
        # return torch.reshape(x, [batch_size, self._num_nodes * output_size])
        x = torch.reshape(x, [batch_size, self._num_nodes ,output_size])
        return x