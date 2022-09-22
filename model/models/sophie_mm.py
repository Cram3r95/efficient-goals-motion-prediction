import os
import math
import random
import numpy as np
import pdb
from model.modules.attention import MultiHeadAttention
from model.modules.layers import Linear

import torch
import torch.nn as nn
import torch.nn.functional as F

from torch_geometric.nn import conv
from torch_geometric.utils import from_scipy_sparse_matrix
from scipy import sparse

DATA_DIM = 2
NUM_MODES = 6

OBS_LEN = 20
PRED_LEN = 30

MLP_DIM = 64
H_DIM = 128
EMBEDDING_DIM = 16

APPLY_DROPOUT = True
DROPOUT = 0.4

HEAD = "SingleLinear" # SingleLinear, MultiLinear, Non-Autoregressive

def make_mlp(dim_list):
    layers = []
    for dim_in, dim_out in zip(dim_list[:-1], dim_list[1:]):
        layers.append(nn.Linear(dim_in, dim_out))
        layers.append(nn.GELU())
    return nn.Sequential(*layers)

class EncoderLSTM_CRAT(nn.Module):
    def __init__(self):
        super(EncoderLSTM_CRAT, self).__init__()

        self.input_size = DATA_DIM
        self.hidden_size = H_DIM
        self.num_layers = 1

        self.lstm = nn.LSTM(
            input_size=self.input_size,
            hidden_size=self.hidden_size,
            num_layers=self.num_layers
        )

    def forward(self, lstm_in):
        # lstm_in are all agents over all samples in the current batch
        # Format for LSTM has to be has to be (batch_size, timeseries_length, latent_size), because batch_first=True

        # Initialize the hidden state.
        # lstm_in.shape[0] corresponds to the number of all agents in the current batch
        lstm_hidden_state = torch.randn(
            self.num_layers, lstm_in.shape[1], self.hidden_size, device=lstm_in.device)
        lstm_cell_state = torch.randn(
            self.num_layers, lstm_in.shape[1], self.hidden_size, device=lstm_in.device)
        lstm_hidden = (lstm_hidden_state, lstm_cell_state)

        lstm_out, (lstm_hidden, lstm_cell) = self.lstm(lstm_in, lstm_hidden)
        
        # lstm_out is the hidden state over all time steps from the last LSTM layer
        # In this case, only the features of the last time step are used

        if APPLY_DROPOUT: lstm_hidden = F.dropout(lstm_hidden, p=DROPOUT, training=self.training)

        # return lstm_out[-1, :, :]
        return lstm_hidden.view(-1,self.hidden_size)

class AgentGnn_CRAT(nn.Module):
    def __init__(self):
        super(AgentGnn_CRAT, self).__init__()

        self.latent_size = H_DIM

        self.gcn1 = conv.CGConv(self.latent_size, dim=2, batch_norm=True)
        self.gcn2 = conv.CGConv(self.latent_size, dim=2, batch_norm=True)

    def build_fully_connected_edge_idx(self, agents_per_sample):
        edge_index = []

        # In the for loop one subgraph is built (no self edges!)
        # The subgraph gets offsetted and the full graph over all samples in the batch
        # gets appended with the offsetted subgraph

        offset = 0
        for i in range(len(agents_per_sample)):

            num_nodes = agents_per_sample[i]

            adj_matrix = torch.ones((num_nodes, num_nodes))
            adj_matrix = adj_matrix.fill_diagonal_(0)
   
            sparse_matrix = sparse.csr_matrix(adj_matrix.numpy())

            edge_index_subgraph, _ = from_scipy_sparse_matrix(sparse_matrix)

            # Offset the list

            edge_index_subgraph = torch.Tensor(np.asarray(edge_index_subgraph) + offset)

            offset += agents_per_sample[i]

            edge_index.append(edge_index_subgraph)

        # Concat the single subgraphs into one
        edge_index = torch.LongTensor(np.column_stack(edge_index))

        return edge_index

    def build_edge_attr(self, edge_index, data):
        edge_attr = torch.zeros((edge_index.shape[-1], 2), dtype=torch.float)

        rows, cols = edge_index
        # goal - origin
        edge_attr = data[cols] - data[rows]

        return edge_attr

    def forward(self, gnn_in, centers, agents_per_sample):
        # gnn_in is a batch and has the shape (batch_size, number_of_agents, latent_size)

        x, edge_index = gnn_in, self.build_fully_connected_edge_idx(agents_per_sample).to(gnn_in.device)
        edge_attr = self.build_edge_attr(edge_index, centers).to(gnn_in.device)

        x = F.relu(self.gcn1(x, edge_index, edge_attr))
        gnn_out = F.relu(self.gcn2(x, edge_index, edge_attr))

        return gnn_out

class MultiheadSelfAttention_CRAT(nn.Module):
    def __init__(self):
        super(MultiheadSelfAttention_CRAT, self).__init__()

        self.latent_size = H_DIM

        if APPLY_DROPOUT: self.multihead_attention = nn.MultiheadAttention(self.latent_size, 4, dropout=DROPOUT)
        else: self.multihead_attention = nn.MultiheadAttention(self.latent_size, 4)

    def forward(self, att_in, agents_per_sample):
        att_out_batch = []

        # Upper path is faster for multiple samples in the batch and vice versa
        if len(agents_per_sample) > 1:
            max_agents = max(agents_per_sample)

            padded_att_in = torch.zeros((len(agents_per_sample), max_agents, self.latent_size), device=att_in[0].device)
            mask = torch.arange(max_agents).to(att_in[0].device) < torch.tensor(agents_per_sample).to(att_in[0].device)[:, None]

            padded_att_in[mask] = att_in

            mask_inverted = ~mask
            mask_inverted = mask_inverted.to(att_in.device)

            padded_att_in_swapped = torch.swapaxes(padded_att_in, 0, 1)

            padded_att_in_swapped, _ = self.multihead_attention(
                padded_att_in_swapped, padded_att_in_swapped, padded_att_in_swapped, key_padding_mask=mask_inverted)

            padded_att_in_reswapped = torch.swapaxes(
                padded_att_in_swapped, 0, 1)

            att_out_batch = [x[0:agents_per_sample[i]]
                             for i, x in enumerate(padded_att_in_reswapped)]
        else:
            att_in = torch.split(att_in, agents_per_sample)
            for i, sample in enumerate(att_in):
                # Add the batch dimension (this has to be the second dimension, because attention requires it)
                att_in_formatted = sample.unsqueeze(1)
                att_out, weights = self.multihead_attention(
                    att_in_formatted, att_in_formatted, att_in_formatted)

                # Remove the "1" batch dimension
                att_out = att_out.squeeze()
                att_out_batch.append(att_out)

        return att_out_batch

class MMDecoderLSTM(nn.Module):
    def __init__(self):
        super().__init__()

        self.data_dim = DATA_DIM
        self.pred_len = PRED_LEN
        self.h_dim = H_DIM
        self.mlp_dim = MLP_DIM
        self.embedding_dim = EMBEDDING_DIM
        self.num_modes = NUM_MODES

        self.traj_points = self.num_modes*2 # Vector length per step (6 modes x 2 (x,y))
        self.spatial_embedding = nn.Linear(self.traj_points, self.embedding_dim)
        
        self.decoder = nn.LSTM(self.embedding_dim, self.h_dim, 1)
  
        if HEAD == "MultiLinear":
            pred = []
            for _ in range(self.num_modes):
                # pred.append(nn.Linear(self.h_dim, self.data_dim))
                pred.append(Linear(self.h_dim, self.data_dim))
            self.hidden2pos = nn.ModuleList(pred) 
        elif HEAD == "SingleLinear":
            self.hidden2pos = nn.Linear(self.h_dim, self.traj_points)
            # self.hidden2pos = Linear(self.h_dim, self.traj_points)
        elif HEAD == "Non-Autoregressive":
            pred = []
            for _ in range(self.num_modes):
                # pred.append(nn.Linear(self.h_dim, self.data_dim))
                pred.append(Linear(self.h_dim, self.data_dim*self.pred_len))
            self.hidden2pos = nn.ModuleList(pred) 

        self.confidences = nn.Linear(self.h_dim, self.num_modes)

    def forward(self, last_obs, last_obs_rel, state_tuple):
        """
        last_obs (1, b, 2)
        last_obs_rel (1, b, 2)
        state_tuple: h and c
            h : c : (1, b, self.h_dim)
        """

        batch_size, data_dim = last_obs.shape
        last_obs_rel = last_obs_rel.view(1,batch_size,1,data_dim)
        last_obs_rel = last_obs_rel.repeat_interleave(self.num_modes, dim=2)
        last_obs_rel = last_obs_rel.view(1, batch_size, -1)

        pred_traj_fake_rel = []
        decoder_input = F.leaky_relu(self.spatial_embedding(last_obs_rel.contiguous().view(batch_size, -1))) 
        if APPLY_DROPOUT: decoder_input = F.dropout(decoder_input, p=DROPOUT, training=self.training)
        decoder_input = decoder_input.contiguous().view(1, batch_size, self.embedding_dim)

        state_tuple_h, state_tuple_c = state_tuple

        if HEAD == "Non-Autoregressive":
            output, (state_tuple_h, state_tuple_c) = self.decoder(decoder_input, (state_tuple_h, state_tuple_c)) 

            pred_traj_fake_rel = []
            for num_mode in range(self.num_modes):
                rel_pos_ = self.hidden2pos[num_mode](state_tuple_h.contiguous().view(-1, self.h_dim))
                pred_traj_fake_rel.append(rel_pos_.contiguous().view(batch_size,self.pred_len,self.data_dim).unsqueeze(0))

            pred_traj_fake_rel = torch.stack(pred_traj_fake_rel,dim=0)
            pred_traj_fake_rel = pred_traj_fake_rel.view(self.pred_len, batch_size, self.num_modes, -1)
            pred_traj_fake_rel = pred_traj_fake_rel.permute(1,2,0,3) # batch_size, num_modes, pred_len, data_dim

        else:
            for _ in range(self.pred_len):
                output, (state_tuple_h, state_tuple_c) = self.decoder(decoder_input, (state_tuple_h, state_tuple_c)) 
                # if APPLY_DROPOUT: 
                #     state_tuple_h = F.dropout(state_tuple_h, p=DROPOUT, training=self.training)
                #     state_tuple_c = F.dropout(state_tuple_h, p=DROPOUT, training=self.training)

                if HEAD == "MultiLinear":
                    rel_pos = []
                    for num_mode in range(self.num_modes):
                        rel_pos_ = self.hidden2pos[num_mode](state_tuple_h.contiguous().view(-1, self.h_dim))
                        rel_pos.append(rel_pos_)

                    rel_pos = torch.cat(rel_pos,dim=1)   
                elif HEAD == "SingleLinear":
                    rel_pos = self.hidden2pos(state_tuple_h.contiguous().view(-1, self.h_dim))

                decoder_input = F.leaky_relu(self.spatial_embedding(rel_pos.contiguous().view(batch_size, -1)))
                if APPLY_DROPOUT: decoder_input = F.dropout(decoder_input, p=DROPOUT, training=self.training)
                decoder_input = decoder_input.contiguous().view(1, batch_size, self.embedding_dim)           
                pred_traj_fake_rel.append(rel_pos.contiguous().view(batch_size,-1))

            pred_traj_fake_rel = torch.stack(pred_traj_fake_rel, dim=0)
            pred_traj_fake_rel = pred_traj_fake_rel.view(self.pred_len, batch_size, self.num_modes, -1)
            pred_traj_fake_rel = pred_traj_fake_rel.permute(1,2,0,3) # batch_size, num_modes, pred_len, data_dim

        conf = self.confidences(state_tuple_h.contiguous().view(-1, self.h_dim))

        conf = torch.softmax(conf, dim=1) # batch_size, num_modes
        if not torch.allclose(torch.sum(conf, dim=1), conf.new_ones((batch_size,))):
            pdb.set_trace()

        return pred_traj_fake_rel, conf

class TrajectoryGenerator(nn.Module):
    def __init__(self):
        super(TrajectoryGenerator, self).__init__()

        self.obs_len = OBS_LEN
        self.pred_len = PRED_LEN
        self.mlp_dim = MLP_DIM
        self.h_dim = H_DIM

        # pdb.set_trace()
        self.encoder = EncoderLSTM_CRAT()

        self.agent_gnn = AgentGnn_CRAT()
        self.sattn = MultiheadSelfAttention_CRAT()
   
        self.decoder = MMDecoderLSTM()

        mlp_context_input_dim = self.h_dim # After GNN and MHSA
 
        mlp_decoder_context_dims = [mlp_context_input_dim, self.mlp_dim, self.h_dim]
        self.mlp_decoder_context = make_mlp(mlp_decoder_context_dims)

    def forward(self, obs_traj, obs_traj_rel, seq_start_end, agent_idx):
        # Encoder

        final_encoder_h = self.encoder(obs_traj_rel)

        # GNN + MHSA

        centers = obs_traj[-1,:,:] # x,y (abs coordinates)
        agents_per_sample = (seq_start_end[:,1] - seq_start_end[:,0]).cpu().detach().numpy()

        out_agent_gnn = self.agent_gnn(final_encoder_h, centers, agents_per_sample)
        out_self_attention = self.sattn(out_agent_gnn, agents_per_sample)
        # pdb.set_trace()
        # Decoder

        mlp_decoder_context_input = torch.cat(out_self_attention,dim=0) # batch_size · num_agents x num_inputs_decoder · hidden_dim

        ## Get agent last observations (both abs (around 0,0) and rel-rel)

        last_pos = obs_traj[:, agent_idx, :]
        last_pos_rel = obs_traj_rel[:, agent_idx, :]

        decoder_h = mlp_decoder_context_input[agent_idx,:].unsqueeze(0)
        decoder_c = torch.randn(tuple(decoder_h.shape)).cuda(obs_traj.device)
        state_tuple = (decoder_h, decoder_c)

        last_pos = obs_traj[-1, agent_idx, :]
        last_pos_rel = obs_traj_rel[-1, agent_idx, :]

        ## Predict trajectories

        pred_traj_fake_rel, conf = self.decoder(last_pos, last_pos_rel, state_tuple)

        return pred_traj_fake_rel, conf