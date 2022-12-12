#!/usr/bin/env python3.8
# -*- coding: utf-8 -*-

## MAPFE4MP: Map Features For Efficient Motion Prediction 

"""
Created on Fri Feb 25 12:19:38 2022
@author: Carlos Gómez-Huélamo
"""

# General purpose imports

import math
import numpy as np
import pdb
import time

# DL & Math imports

import torch 
import torch.nn as nn
import torch.nn.functional as F

from torch_geometric.nn import conv
from torch_geometric.utils import from_scipy_sparse_matrix
from scipy import sparse

# Custom imports

from model.modules.layers import Linear, LinearRes

#######################################

# Global variables

DATA_DIM = 2
NUM_MODES = 6

OBS_LEN = 20
PRED_LEN = 30
NUM_PLAUSIBLE_AREA_POINTS = 512
CENTERLINE_LENGTH = 40
NUM_CENTERLINES = 3

NUM_ATTENTION_HEADS = 4
H_DIM_PHYSICAL = 128
H_DIM_SOCIAL = 128
INIT_ZEROS = True
EMBEDDING_DIM = 16
CONV_FILTERS = 60 # 60

APPLY_DROPOUT = True
DROPOUT = 0.25

HEAD = "MultiLinear" # SingleLinear, MultiLinear, Non-Autoregressive

def make_mlp(dim_list, activation_function="ReLU", batch_norm=False, dropout=0.0):
    """
    Generates MLP network:
    Parameters
    ----------
    dim_list : list, list of number for each layer
    activation_function: str, activation function for all layers TODO: Different AF for every layer?
    batch_norm : boolean, use batchnorm at each layer, default: False
    dropout : float [0, 1], dropout probability applied on each layer (except last layer)
    Returns
    -------
    nn.Sequential with layers
    """
    layers = []
    index = 0
    for dim_in, dim_out in zip(dim_list[:-1], dim_list[1:]):
        layers.append(nn.Linear(dim_in, dim_out))

        if batch_norm:
            layers.append(nn.BatchNorm1d(dim_out))

        if activation_function == "ReLU":
            layers.append(nn.ReLU())
        elif activation_function == "GELU":
            layers.append(nn.GELU())
        elif activation_function == "Tanh":
            layers.append(nn.Tanh())
        elif activation_function == "LeakyReLU":
            layers.append(nn.LeakyReLU())

        if dropout > 0 and index < len(dim_list) - 2:
            layers.append(nn.Dropout(p=dropout))

        index += 1
    return nn.Sequential(*layers)

class MotionEncoder(nn.Module):
    def __init__(self, h_dim, current_device):
        super(MotionEncoder, self).__init__()

        self.input_size = DATA_DIM
        self.hidden_size = h_dim
        self.num_layers = 1
        self.current_device = current_device

        self.lstm = nn.LSTM(
            input_size=self.input_size,
            hidden_size=self.hidden_size,
            num_layers=self.num_layers
        ).cuda(self.current_device)

    def forward(self, lstm_in):
        """_summary_

        Args:
            lstm_in (_type_): _description_

        Returns:
            _type_: _description_
        """
        
        if INIT_ZEROS:
            lstm_hidden_state = torch.zeros(self.num_layers, 
                                            lstm_in.shape[1], 
                                            self.hidden_size, 
                                            dtype=torch.float,
                                            device=lstm_in.device)
            lstm_cell_state = torch.zeros(self.num_layers, 
                                        lstm_in.shape[1], 
                                        self.hidden_size,
                                        dtype=torch.float, 
                                        device=lstm_in.device)
        else:
            lstm_hidden_state = torch.randn(self.num_layers, 
                                            lstm_in.shape[1], 
                                            self.hidden_size, 
                                            dtype=torch.float,
                                            device=lstm_in.device)
            lstm_cell_state = torch.randn(self.num_layers, 
                                        lstm_in.shape[1], 
                                        self.hidden_size,
                                        dtype=torch.float, 
                                        device=lstm_in.device)

        lstm_out, (lstm_hidden_state_, lstm_cell_state_) = self.lstm(lstm_in, (lstm_hidden_state, lstm_cell_state))

        if APPLY_DROPOUT: lstm_hidden_state_ = F.dropout(lstm_hidden_state_, p=DROPOUT, training=self.training)
        lstm_hidden_state_ = lstm_hidden_state_.squeeze(dim=0)
        return lstm_hidden_state_

class GNN(nn.Module):
    def __init__(self, h_dim):
        super(GNN, self).__init__()

        self.latent_size = h_dim

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
        """_summary_

        Args:
            gnn_in (_type_): _description_
            centers (_type_): _description_
            agents_per_sample (_type_): _description_

        Returns:
            _type_: _description_
        """

        x, edge_index = gnn_in, self.build_fully_connected_edge_idx(agents_per_sample).to(gnn_in.device)
        edge_attr = self.build_edge_attr(edge_index, centers).to(gnn_in.device)

        x = F.relu(self.gcn1(x, edge_index, edge_attr)) # relu
        gnn_out = F.relu(self.gcn2(x, edge_index, edge_attr)) # relu

        return gnn_out

class MultiheadSelfAttention(nn.Module):
    def __init__(self, num_heads, h_dim):
        super(MultiheadSelfAttention, self).__init__()

        self.num_heads = num_heads
        self.latent_size = h_dim

        if APPLY_DROPOUT: self.multihead_attention = nn.MultiheadAttention(self.latent_size, self.num_heads, dropout=DROPOUT)
        else: self.multihead_attention = nn.MultiheadAttention(self.latent_size, self.num_heads)

    def forward(self, att_in, agents_per_sample):
        att_out_batch = []

        # Upper path is faster for multiple samples in the batch and vice versa

        test = True
        if len(agents_per_sample) > 1 or test:
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
            pdb.set_trace()
            agents_per_sample_ = tuple(np.ones(agents_per_sample,dtype=int))
            att_in = torch.split(att_in, agents_per_sample_)
            for i, sample in enumerate(att_in):
                # Add the batch dimension (this has to be the second dimension, because attention requires it)
                att_in_formatted = sample.unsqueeze(1)
                att_out, weights = self.multihead_attention(
                    att_in_formatted, att_in_formatted, att_in_formatted)

                # Remove the "1" batch dimension
                att_out = att_out.squeeze()
                att_out_batch.append(att_out)

        return att_out_batch

class Centerline_Encoder(nn.Module):
    def __init__(self, h_dim, kernel_size=3):
        super(Centerline_Encoder, self).__init__()

        self.data_dim = DATA_DIM
        self.num_filters = CONV_FILTERS
        self.h_dim = h_dim
        self.kernel_size = kernel_size
        self.lane_length = CENTERLINE_LENGTH
        self.num_centerlines = NUM_CENTERLINES
        
        mid_dim = math.ceil((self.num_centerlines*self.lane_length*self.data_dim + self.h_dim)/2)
        dims = [self.num_centerlines*self.lane_length*self.data_dim, mid_dim, self.h_dim]
        self.mlp_centerlines = make_mlp(dims,
                             activation_function="ReLU",
                             batch_norm=True,
                             dropout=DROPOUT)

    def forward(self, phy_info):
        """_summary_

        Args:
            phy_info (_type_): _description_

        Returns:
            _type_: _description_
        """

        # num_centerlines = phy_info.shape[0]
        # phy_info_ = phy_info.permute(0,2,1)
        # phy_info_ = phy_info_.contiguous().view(num_centerlines, -1)
        # phy_info_ = self.mlp_centerlines(phy_info_)

        phy_info_ = self.mlp_centerlines(phy_info)
        
        if torch.any(phy_info_.isnan()):
            pdb.set_trace()
            
        return phy_info_
        
class Multimodal_Decoder(nn.Module):
    def __init__(self, decoder_h_dim):
        super(Multimodal_Decoder, self).__init__()

        self.data_dim = DATA_DIM
        self.pred_len = PRED_LEN

        self.decoder_h_dim = decoder_h_dim
        self.embedding_dim = EMBEDDING_DIM
        self.num_modes = NUM_MODES

        self.spatial_embedding = nn.Linear(self.data_dim, self.embedding_dim)
        self.decoder = nn.LSTM(self.embedding_dim, 
                               self.decoder_h_dim, 
                               num_layers=1)
  
        if HEAD == "MultiLinear":
            pred = []
            for _ in range(self.num_modes):
                # pred.append(PredictionNet(self.decoder_h_dim,self.data_dim))
                pred.append(nn.Linear(self.decoder_h_dim,self.data_dim))
            self.hidden2pos = nn.ModuleList(pred) 

        # self.confidences = nn.Sequential(nn.Linear(PRED_LEN*DATA_DIM,32),
        #                                  nn.ReLU(),
        #                                  nn.Linear(32,1))  
        self.confidences = PredictionNet(PRED_LEN*DATA_DIM,1,norm=False)
        
    def forward(self, last_obs, last_obs_rel, state_tuple):
        """_summary_

        Args:
            last_obs (_type_): _description_
            last_obs_rel (_type_): _description_
            state_tuple (_type_): _description_

        Returns:
            _type_: _description_
        """

        batch_size, data_dim = last_obs.shape
 
        state_tuple_h, state_tuple_c = state_tuple

        pred_traj_fake_rel = []
        for num_mode in range(self.num_modes):
            last_obs_rel_ = torch.clone(last_obs_rel)
            decoder_input = F.leaky_relu(self.spatial_embedding(last_obs_rel_)).unsqueeze(0)
            if APPLY_DROPOUT: decoder_input = F.dropout(decoder_input, p=DROPOUT, training=self.training)
        
            state_tuple_h_ = torch.clone(state_tuple_h)
            state_tuple_c_ = torch.clone(state_tuple_c)
        
            curr_pred_traj_fake_rel = []
            for _ in range(self.pred_len):
                output, (state_tuple_h_, state_tuple_c_) = self.decoder(decoder_input, (state_tuple_h_, state_tuple_c_)) 

                rel_pos = self.hidden2pos[num_mode](output.contiguous().view(-1, self.decoder_h_dim))
                curr_pred_traj_fake_rel.append(rel_pos)

                decoder_input = F.leaky_relu(self.spatial_embedding(rel_pos)).unsqueeze(0)
                if APPLY_DROPOUT: decoder_input = F.dropout(decoder_input, p=DROPOUT, training=self.training)
         
            curr_pred_traj_fake_rel = torch.stack(curr_pred_traj_fake_rel,dim=0)
            curr_pred_traj_fake_rel = curr_pred_traj_fake_rel.permute(1,0,2)
            pred_traj_fake_rel.append(curr_pred_traj_fake_rel)
   
        pred_traj_fake_rel = torch.stack(pred_traj_fake_rel, dim=0)
        pred_traj_fake_rel = pred_traj_fake_rel.permute(1,0,2,3) # batch_size, num_modes, pred_len, data_dim

        conf = self.confidences(pred_traj_fake_rel.contiguous().view(batch_size,NUM_MODES,-1))
        conf = torch.softmax(conf.view(batch_size,-1), dim=1) # batch_size, num_modes
        if not torch.allclose(torch.sum(conf, dim=1), conf.new_ones((batch_size,))):
            pdb.set_trace()

        return pred_traj_fake_rel, conf

class DecoderResidual(nn.Module):
    def __init__(self, h_dim, output_dim):
        super(DecoderResidual, self).__init__()

        self.pred_len = PRED_LEN
        self.h_dim = h_dim
        self.output_dim = output_dim
        
        output = []
        for i in range(NUM_MODES):
            output.append(PredictionNet(self.h_dim, self.pred_len*self.output_dim))
        self.output = nn.ModuleList(output)
        
        self.confidences = PredictionNet(PRED_LEN*DATA_DIM,1,norm=False)

    def forward(self, decoder_in):
        batch_size = decoder_in.shape[0]
        pred = []

        for out_subnet in self.output:
            pred.append(out_subnet(decoder_in))

        decoder_out = torch.stack(pred)
        decoder_out = decoder_out.permute(1,0,2)
        
        pred_traj_fake_rel = decoder_out.view(batch_size,NUM_MODES,PRED_LEN,DATA_DIM)

        conf = self.confidences(pred_traj_fake_rel.contiguous().view(batch_size,NUM_MODES,-1))
        conf = torch.softmax(conf.view(batch_size,-1), dim=1) # batch_size, num_modes
        if not torch.allclose(torch.sum(conf, dim=1), conf.new_ones((batch_size,))):
            pdb.set_trace()

        return pred_traj_fake_rel, conf
    
class PredictionNet(nn.Module):
    def __init__(self, h_dim, output_dim, norm=True):
        super(PredictionNet, self).__init__()

        self.latent_size = h_dim
        self.output_dim = output_dim
        self.norm = norm

        self.weight1 = nn.Linear(self.latent_size, self.latent_size)
        self.norm1 = nn.GroupNorm(1, self.latent_size)

        self.weight2 = nn.Linear(self.latent_size, self.latent_size)
        self.norm2 = nn.GroupNorm(1, self.latent_size)

        self.output_fc = nn.Linear(self.latent_size, self.output_dim)

    def forward(self, prednet_in):
        """_summary_

        Args:
            prednet_in (_type_): _description_

        Returns:
            _type_: _description_
        """
        # Residual layer

        x = self.weight1(prednet_in)
        if self.norm: x = self.norm1(x)
        x = F.relu(x) # relu
        x = self.weight2(x)
        if self.norm: x = self.norm2(x)

        x += prednet_in

        x = F.relu(x) # relu

        # Last layer has no activation function
        
        prednet_out = self.output_fc(x)

        return prednet_out

class TrajectoryGenerator(nn.Module):
    def __init__(self, PHYSICAL_CONTEXT="social", CURRENT_DEVICE="cpu"):
        super(TrajectoryGenerator, self).__init__()

        self.physical_context = PHYSICAL_CONTEXT

        self.obs_len = OBS_LEN
        self.pred_len = PRED_LEN
        self.h_dim_social = H_DIM_SOCIAL
        self.h_dim_physical = H_DIM_PHYSICAL
        self.num_attention_heads = NUM_ATTENTION_HEADS
        self.data_dim = DATA_DIM
        self.num_modes = NUM_MODES
        self.num_plausible_area_points = NUM_PLAUSIBLE_AREA_POINTS
        self.num_centerlines = NUM_CENTERLINES

        # Encoder

        ## Social 

        self.motion_encoder = MotionEncoder(h_dim=self.h_dim_social,current_device=CURRENT_DEVICE)
        self.agent_gnn = GNN(h_dim=self.h_dim_social)
        self.sattn = MultiheadSelfAttention(h_dim=self.h_dim_social,
                                            num_heads=self.num_attention_heads)

        ## Physical 

        self.centerline_encoder = Centerline_Encoder(h_dim=self.h_dim_physical)
        # self.centerline_gnn = GNN(h_dim=self.h_dim_physical)
        # self.pattn = MultiheadSelfAttention(h_dim=self.h_dim_physical,
        #                                     num_heads=self.num_attention_heads)
        # mid_dim = math.ceil((self.num_centerlines*self.h_dim_physical + self.h_dim_physical)/2)
        # mlp_phy_attn_dims = [self.num_centerlines*self.h_dim_physical, mid_dim, self.h_dim_physical]
        # self.mlp_phy_attn = make_mlp(mlp_phy_attn_dims,
        #                              activation_function="Tanh")
        
        # Decoder

        if PHYSICAL_CONTEXT == "social":
            self.concat_h_dim = 3 * self.h_dim_social 
        elif PHYSICAL_CONTEXT == "oracle":
            # self.concat_h_dim = 3 * self.h_dim_social + self.h_dim_physical
            self.concat_h_dim = self.h_dim_social + self.h_dim_physical
            
        elif PHYSICAL_CONTEXT == "plausible_centerlines":
            # self.concat_h_dim = 3 * self.h_dim_social + 3 * self.h_dim_physical
            self.concat_h_dim = self.h_dim_social + self.h_dim_physical

        self.decoder = Multimodal_Decoder(decoder_h_dim=self.concat_h_dim)  
        # self.decoder = DecoderResidual(h_dim=self.concat_h_dim,output_dim=self.data_dim)

    def add_noise(self, input, factor=1):
        """_summary_

        Args:
            input (_type_): _description_
            factor (int, optional): _description_. Defaults to 1.

        Returns:
            _type_: _description_
        """

        noise = factor * torch.randn(input.shape).to(input)
        noisy_input = input + noise
        return noisy_input
    
    def forward(self, obs_traj, obs_traj_rel, seq_start_end, agent_idx, phy_info=None, relevant_centerlines=None):
        """_summary_

        Args:
            obs_traj (_type_): _description_
            obs_traj_rel (_type_): _description_
            seq_start_end (_type_): _description_
            agent_idx (_type_): _description_
            phy_info (_type_, optional): _description_. Defaults to None.
            relevant_centerlines (_type_, optional): _description_. Defaults to None.

        Returns:
            _type_: _description_
        """

        start = time.time()
        batch_size = seq_start_end.shape[0]
 
        # Motion Encoder
        
        encoded_obs_traj = self.motion_encoder(obs_traj)
        encoded_obs_traj_rel = self.motion_encoder(obs_traj_rel)

        target_agent_encoded_obs_traj = encoded_obs_traj[agent_idx,:]
        target_agent_encoded_obs_traj_rel = encoded_obs_traj_rel[agent_idx,:]

        ## Social information

        centers = obs_traj[-1,:,:] # x,y (abs coordinates)
        agents_per_sample = (seq_start_end[:,1] - seq_start_end[:,0]).cpu().detach().numpy()

        out_agent_gnn = self.agent_gnn(encoded_obs_traj_rel, centers, agents_per_sample)
        out_self_attention = self.sattn(out_agent_gnn, agents_per_sample)
        encoded_social_info = torch.cat(out_self_attention,dim=0) # batch_size · num_agents x hidden_dim_social

        if np.any(agent_idx): # single agent
            encoded_social_info = encoded_social_info[agent_idx,:]
 
        ## Physical information

        last_pos = obs_traj[-1, agent_idx, :]
        last_pos_rel = obs_traj_rel[-1, agent_idx, :]

        if self.physical_context == "social":
            mlp_decoder_context_input = torch.cat([target_agent_encoded_obs_traj.contiguous().view(-1,self.h_dim_social), 
                                                   target_agent_encoded_obs_traj_rel.contiguous().view(-1,self.h_dim_social), 
                                                   encoded_social_info.contiguous().view(-1,self.h_dim_social)], 
                                                   dim=1)

            decoder_h = mlp_decoder_context_input.unsqueeze(0)
            decoder_c = torch.randn(tuple(decoder_h.shape)).cuda(obs_traj.device)
            state_tuple = (decoder_h, decoder_c)

        elif self.physical_context == "oracle":
            encoded_phy_info = self.centerline_encoder(relevant_centerlines)

            mlp_decoder_context_input = torch.cat([encoded_social_info, 
                                                   encoded_phy_info], 
                                                   dim=1)
  
            decoder_h = mlp_decoder_context_input.unsqueeze(0)
            decoder_c = torch.randn(tuple(decoder_h.shape)).cuda(obs_traj.device)
            state_tuple = (decoder_h, decoder_c)

        elif self.physical_context == "plausible_centerlines":
            centerlines_per_sample = self.num_centerlines * np.ones(batch_size,dtype=int)

            relevant_centerlines_concat = relevant_centerlines.contiguous().view(batch_size,-1)
            encoded_centerlines = self.centerline_encoder(relevant_centerlines_concat)
       
            mlp_decoder_context_input = torch.cat([encoded_social_info.contiguous(),
                                                   encoded_centerlines.contiguous()],
                                                   dim=1)

            decoder_h = mlp_decoder_context_input.unsqueeze(0)
            if INIT_ZEROS: decoder_c = torch.zeros(tuple(decoder_h.shape)).cuda(obs_traj.device)
            else: decoder_c = torch.randn(tuple(decoder_h.shape)).cuda(obs_traj.device)

            state_tuple = (decoder_h, decoder_c)

        pred_traj_fake_rel, conf = self.decoder(last_pos, last_pos_rel, state_tuple) # LSTM
        # pred_traj_fake_rel, conf = self.decoder(mlp_decoder_context_input) # Residual
        
        if torch.any(pred_traj_fake_rel.isnan()) or torch.any(conf.isnan()):
            pdb.set_trace()
            
        end = time.time()
        # print("Time consumed by forward: ", end-start)

        return pred_traj_fake_rel, conf