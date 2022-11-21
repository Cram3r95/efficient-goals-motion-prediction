#!/usr/bin/env python3.8
# -*- coding: utf-8 -*-

## LSTM based Encoder-Decoder with Multi-Head Self Attention

"""
Created on Fri Feb 25 12:19:38 2022
@author: Carlos Gómez-Huélamo, Miguel Eduardo Ortiz Huamaní and Marcos V. Conde
"""

# General purpose imports 

from os import path
import pdb
import math

# DL & Math imports

import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.transforms.functional as TF

# Custom imports

from model.modules.encoders import EncoderLSTM as Encoder
from model.modules.attention import MultiHeadAttention
from model.modules.decoders import TemporalDecoderLSTM as TemporalDecoder
from model.modules.decoders import MM_DecoderLSTM as MM_Decoder

# Aux functions

def make_mlp(dim_list):
    layers = []
    for dim_in, dim_out in zip(dim_list[:-1], dim_list[1:]):
        layers.append(nn.Linear(dim_in, dim_out))
        # layers.append(nn.LeakyReLU())
        layers.append(nn.Tanh())
    return nn.Sequential(*layers)

def get_noise(shape,current_cuda):
    """
    Only for adversarial model
    """
    return torch.randn(*shape).cuda(current_cuda)

class TrajectoryGenerator(nn.Module):
    def __init__(self, config_encoder_lstm, config_decoder_lstm, config_mhsa, 
                       current_cuda="cuda:0", adversarial_training=False, use_social_attention=True,
                       ):
        super(TrajectoryGenerator, self).__init__() # initialize nn.Module with default parameters

        # Aux variables

        self.current_cuda = current_cuda
        self.adversarial_training = adversarial_training
        
        # Encoder

        self.encoder_use_rel_disp = config_encoder_lstm.use_rel_disp
        self.encoder_use_social_attention = config_encoder_lstm.use_social_attention
        self.encoder_use_prev_traj_encoded = config_encoder_lstm.use_prev_traj_encoded

        if adversarial_training: self.noise_dim = config_encoder_lstm.noise_dim

        self.encoder = Encoder(h_dim=config_encoder_lstm.h_dim, # Num units
                               bidirectional=config_encoder_lstm.bidirectional, 
                               num_layers=config_encoder_lstm.num_layers,
                               dropout=config_encoder_lstm.dropout,
                               current_cuda=self.current_cuda,
                               use_rel_disp=config_encoder_lstm.use_rel_disp,
                               conv_filters=config_encoder_lstm.conv_filters)
                               
        self.lne = nn.LayerNorm(config_encoder_lstm.h_dim) # Layer normalization encoder

        # Attention

        self.sattn = MultiHeadAttention(key_size=config_mhsa.h_dim, 
                                        query_size=config_mhsa.h_dim, 
                                        value_size=config_mhsa.h_dim,
                                        num_hiddens=config_mhsa.h_dim, 
                                        num_heads=config_mhsa.num_heads, 
                                        dropout=config_mhsa.dropout)

        ## Final context

        assert config_mhsa.h_dim == config_encoder_lstm.h_dim
        self.context_h_dim = config_mhsa.h_dim

        if self.encoder_use_social_attention and self.encoder_use_prev_traj_encoded:
            mlp_context_input = config_encoder_lstm.h_dim*2 # Encoded trajectories + social context
        else:
            assert self.encoder_use_social_attention or self.encoder_use_prev_traj_encoded, print("No context provided!")
            mlp_context_input = config_encoder_lstm.h_dim
       
        self.lnd = nn.LayerNorm(mlp_context_input) # Layer normalization context decoder

        # Decoder

        ## Decoder input context

        assert config_decoder_lstm.h_dim == config_encoder_lstm.h_dim

        if self.adversarial_training:
            mlp_decoder_context_dims = [mlp_context_input, *config_decoder_lstm.mlp_dim, config_decoder_lstm.h_dim - self.noise_dim]
        else:
            mlp_decoder_context_dims = [mlp_context_input, *config_decoder_lstm.mlp_dim, config_decoder_lstm.h_dim]
        self.mlp_decoder_context = make_mlp(mlp_decoder_context_dims)

        ## Final prediction
        
        self.decoder_use_rel_disp = config_decoder_lstm.use_rel_disp

        if config_decoder_lstm.num_modes == 1:
            self.decoder = TemporalDecoder(h_dim=config_decoder_lstm.h_dim,
                                        embedding_dim=config_decoder_lstm.embedding_dim,
                                        num_layers=config_decoder_lstm.num_layers,
                                        bidirectional=config_decoder_lstm.bidirectional,
                                        dropout=config_decoder_lstm.dropout,
                                        current_cuda=self.current_cuda,
                                        use_rel_disp=config_decoder_lstm.use_rel_disp)
        else:
            self.decoder = MM_Decoder(h_dim=config_decoder_lstm.h_dim,
                                        embedding_dim=config_decoder_lstm.embedding_dim,
                                        num_modes=config_decoder_lstm.num_modes,
                                        num_layers=config_decoder_lstm.num_layers,
                                        bidirectional=config_decoder_lstm.bidirectional,
                                        dropout=config_decoder_lstm.dropout,
                                        current_cuda=self.current_cuda,
                                        use_rel_disp=config_decoder_lstm.use_rel_disp)

    def add_noise(self, _input):
        """
        """
        num_objs = _input.size(0)
        noise_shape = (self.noise_dim,)
        z_decoder = get_noise(noise_shape,self.current_cuda)
        vec = z_decoder.view(1, -1).repeat(num_objs, 1)
        return torch.cat((_input, vec), dim=1)

    def forward(self, obs_traj, obs_traj_rel, start_end_seq, agent_idx=None):
        """
        Assuming obs_len = 20, pred_len = 30:

        n: number of objects in all the scenes of the batch
        b: batch
        obs_traj: (20,n,2)
        obs_traj_rel: (20,n,2)
        start_end_seq: (b,2)
        agent_idx: (b, 1) -> index of AGENT (of interest) in every sequence.
            None: trajectories for every object in the scene will be generated
            Not None: just trajectories for the agent in the scene will be generated
        -----------------------------------------------------------------------------
        pred_traj_fake_rel:
            (30,n,2) -> if agent_idx is None
            (30,b,2)
        """

        batch_features = []

        for start, end in start_end_seq.data: # Iterate per sequence
            if self.encoder_use_rel_disp:
                curr_obs_traj_rel = obs_traj_rel[:,start:end,:] # 20 x num_agents x 2
                curr_final_encoder_h = self.encoder(curr_obs_traj_rel) 
            else:
                curr_obs_traj = obs_traj[:,start:end,:] # 20 x num_agents x 2
                curr_final_encoder_h = self.encoder(curr_obs_traj) 

            curr_final_encoder_h = self.lne(curr_final_encoder_h)
            curr_final_encoder_h = torch.unsqueeze(curr_final_encoder_h, 0) # Required by Attention

            if self.encoder_use_social_attention:
                curr_social_attn = self.sattn(
                                            curr_final_encoder_h, # Key
                                            curr_final_encoder_h, # Query
                                            curr_final_encoder_h, # Value
                                            None
                                            )
                if self.encoder_use_prev_traj_encoded:
                    concat_features = torch.cat(
                        [
                            curr_final_encoder_h.contiguous().view(-1,self.context_h_dim),
                            curr_social_attn.contiguous().view(-1,self.context_h_dim)
                        ],
                        dim=1
                    )
                else:
                    concat_features = curr_social_attn.contiguous().view(-1,self.context_h_dim)
            elif self.encoder_use_prev_traj_encoded:
                concat_features = curr_final_encoder_h.contiguous().view(-1,self.context_h_dim)
    
            batch_features.append(concat_features)

        # Here num_inputs_decoder refers to the number of sources of information. If we are computing
        # for each scene (sequence) the encoded trajectories and the social attention, we have 2, 
        # so the second dimension is 2 · hidden_dim (ej: 2 · 256 -> 512) 
        mlp_decoder_context_input = torch.cat(batch_features,axis=0) # batch_size · num_agents x num_inputs_decoder · hidden_dim

        # Take the encoded trajectory and attention regarding only the AGENT for each sequence

        if agent_idx is not None:
            mlp_decoder_context_input = mlp_decoder_context_input[agent_idx,:]

        decoder_h = self.mlp_decoder_context(self.lnd(mlp_decoder_context_input))

        if self.adversarial_training:
            decoder_h = self.add_noise(decoder_h)

        decoder_h = torch.unsqueeze(decoder_h, 0) 

        # Get agent last observations (both abs (around 0,0) and rel-rel)

        if agent_idx is not None: # for single agent prediction
            last_pos = obs_traj[:, agent_idx, :]
            last_pos_rel = obs_traj_rel[:, agent_idx, :]
        else:
            last_pos = obs_traj[-1, :, :]
            last_pos_rel = obs_traj_rel[-1, :, :]

        # Decode trajectories

        pred_traj_fake_rel = self.decoder(last_pos, last_pos_rel, decoder_h)
        
        return pred_traj_fake_rel

# Only for adversarial training

class TrajectoryDiscriminator(nn.Module):
    def __init__(self, mlp_dim=64, h_dim=64):
        super(TrajectoryDiscriminator, self).__init__()

        self.mlp_dim = mlp_dim
        self.h_dim = h_dim

        self.encoder = Encoder()
        real_classifier_dims = [self.h_dim, self.mlp_dim, 1]
        self.real_classifier = make_mlp(real_classifier_dims)

    def forward(self, traj):
        """
        traj can be either absolute coordinates (around 0,0) or relatives
        """

        final_h = self.encoder(traj)
        scores = self.real_classifier(final_h)
        return scores