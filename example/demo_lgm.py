#!/usr/bin/env python3
# -*- coding: utf-8 -*-
###############################################################################
# This file is part of LGM.
#
# Copyright 2018 Yuesong Shen
# Copyright 2018,2019 Technical University of Munich
#
# Developed by Yuesong Shen <yuesong dot shen at tum dot de>.
#
# If you use this file for your research, please cite the following paper:
#
# "Probabilistic Discriminative Learning with Layered Graphical Models" by
# Yuesong Shen, Tao Wu, Csaba Domokos and Daniel Cremers
#
# LGM is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# LGM is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with LGM. If not, see <http://www.gnu.org/licenses/>.
###############################################################################
"""
test script for learning with MNIST / KMNIST / FashionMNIST
"""
import os
import argparse
from typing import Dict, Tuple, Optional, Hashable
import torch.nn.functional as nnf
import torchvision.transforms.functional as trf
import torch.optim as optim
import lgm.msgpass as msgpass
from lgm.utils.common import (get_timestamp, Log, check_cuda, mkdirp,
                              rm, training_backup, training_resume,
                              EarlyStopper, display_param_stats)
from lgm.utils.train import (train_epoch, eval_epoch, test)
from lgm.utils.data.mnist import get_MNIST_dataloaders, AVAILABLE_FLAVORS
import mnist_models
from torch import nn
import numpy as np
import torch
from lgm.utils.common import Picklable, flip_local, get_local_links, SavableModel



def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-m', '--model',
                        choices=('dense', 'conv', 'local', 'res'),
                        default='conv',
                        help='Specify the LGM model to run. Can be either '
                             'dense, conv or local. By default conv.')
    parser.add_argument('-i', '--infer',
                        choices=('loopy', 'trw', 'seqtrw'),
                        default='trw',
                        help='Specify the inference method. Can be either '
                             'loopy, trw or seqtrw. By default trw.')
    parser.add_argument('-n', '--nbiter',
                        type=int,
                        default=5,
                        help='Specify the number of inference iterations. By '
                             'default 5.')
    parser.add_argument('-d', '--dataset',
                        choices=('MNIST', 'KMNIST', 'FashionMNIST'),
                        default='MNIST',
                        help='Specify the dataset to run. Can be either MNIST '
                             ', KMNIST or FashionMNIST. By default MNIST.')
    parser.add_argument('-g', '--gpu',
                        action='store_const',
                        const=True,
                        default=False,
                        help='Enable cuda usage. By default cpu only.')
    parser.add_argument('-b', '--batch',
                        type=int,
                        default=20,
                        help='Specify the batchsize. By default 20.')
    parser.add_argument('-e', '--epoch',
                        type=int,
                        help='Specify the number of epochs to run. Use early'
                             ' stopping by default.')
    parser.add_argument('-p', '--patience',
                        type=int,
                        default=5,
                        help='Specify the number of epochs to wait before '
                             'early stopping. By dafault 5. Will be ignored if'
                             ' --epoch is specified.')
    return parser.parse_args()

def flattened_crop(input, kernel_size=3, stride=3, padding=0):
    if input.ndimension() == 2:
        edited = input.reshape(input.size(0),np.sqrt(input.size(1)).astype(np.int8),np.sqrt(input.size(1)).astype(np.int8))
    else:
        edited = input.reshape(input.size(0),np.sqrt(input.size(1)).astype(np.int8),np.sqrt(input.size(1)).astype(np.int8), input.size(2))

    #cropped = edited.clone()[:,2:-2,2:-2]
    
    cropped=nnf.max_pool2d(edited.clone(), kernel_size=kernel_size, stride=stride, padding=padding)

    if input.ndimension() == 2:
        reshaped = cropped.reshape(cropped.size(0), cropped.size(1)**2, 1)
    else:
        reshaped = cropped.reshape(cropped.size(0), cropped.size(1)**2, cropped.size(3))

    return reshaped

if __name__ == '__main__':

    # global parameters

    args = get_args()
    use_cuda = args.gpu

    epochs = args.epoch  # int or None: set to None to enjoy early stopping
    patience = args.patience

    proto_name = args.model  # 'dense' / 'local'
    infer_method = args.infer  # 'loopy' / 'seqloopy' / 'trw' / 'seqtrw'
    dataset_flavor = args.dataset  # 'MNIST' / 'FashionMNIST'
    frequency = args.nbiter

    optim_type = 'Adam'
    optim_kwargs = {}

    train_val_split = 0.8
    train_batch = args.batch
    val_batch = args.batch
    test_batch = args.batch

    data_dir = {'MNIST': 'data/MNIST/',
                'KMNIST': 'data/KMNIST/',
                'FashionMNIST': 'data/FashionMNIST/'}[dataset_flavor]
    base_dir = __file__[:-3] + '/'
    save_dir = base_dir + 'model/'
    log_dir = base_dir + 'log/'
    resume_from = None  # None / save_dir + 'xxx.pickle'

    # create dirs if not there already

    mkdirp(data_dir)
    mkdirp(save_dir)
    mkdirp(log_dir)

    # prepare proper loss function

    lossfunc = nnf.nll_loss

    # start logger

    log_file = '{0}_{1}_{2}_{3}.log'.format(
            proto_name,
            '-'.join([str(i) for i in (frequency, train_batch, val_batch,
                                       test_batch)]),
            '-'.join([infer_method, dataset_flavor]),
            get_timestamp())
    log_title = log_file[:-4]

    logger = Log(log_dir + log_file)
    logger.start(log_title)
    logger.start_intercept()

    # check cuda availablility when needed

    if use_cuda:
        check_cuda()
    else:
        print('Currently using cpu device')

    # set up dataset

    if dataset_flavor in AVAILABLE_FLAVORS:
        ((train_loader, val_loader, test_loader),
         (nb_train, nb_val, nb_test)) = get_MNIST_dataloaders(
            data_dir, train_batch, val_batch, test_batch, train_val_split,
            use_cuda, dataset_flavor)
    else:
        raise Exception('Unknown dataset: {}'.format(dataset_flavor))
    print('dataset: {}, location: {}'.format(dataset_flavor, data_dir))
    print('sample / batch number for training:  ',
          nb_train, len(train_loader))
    print('sample / batch number for validation:',
          nb_val, len(val_loader))
    print('sample / batch number for testing:   ',
          nb_test, len(test_loader))
    print('train / val / test batchsizes: {} / {} / {}'.format(
            train_batch, val_batch, test_batch))
    print('frequency: {}'.format(frequency))
    print('method: {}'.format(infer_method))

    # load the model and optimizer

    # if resume_from is None or not os.path.exists(resume_from):
    #     print('initializing model ...')
    #     # set up model
    #     layer_graph = getattr(mnist_models, proto_name)()
    #     msg_model = msgpass.layerModelWrapper(
    #       layer_graph, 'step', 'pro', infer_method, frequency, ('o',))
    #     if use_cuda:
    #         msg_model.cuda()
    #     # set up optimizer
    #     optimizer = optim.__dict__[optim_type](msg_model.parameters(),
    #                                            **optim_kwargs)
    # else:
    #     print('Resume training from {0} ...'.format(resume_from))
    #     msg_model, optimizer = training_resume(resume_from, use_cuda,
    #                                            msgpass.loadWrapper)
    # msg_model.show_summary()
    # display_param_stats(msg_model)

    layer_graph = getattr(mnist_models, 'conv')()
    conv = msgpass.layerModelWrapper(
        layer_graph, 'step', 'pro', infer_method, frequency, ('o',))
    if use_cuda:
        conv.cuda()

    layer_graph = getattr(mnist_models, 'res1')()
    block1 = msgpass.layerModelWrapper(
        layer_graph, 'step', 'pro', infer_method, frequency, ('c2',))
    if use_cuda:
        block1.cuda()

    layer_graph = getattr(mnist_models, 'res2')()
    block2 = msgpass.layerModelWrapper(
        layer_graph, 'step', 'pro', infer_method, frequency, ('c2',))
    if use_cuda:
        block2.cuda()

    layer_graph = getattr(mnist_models, 'res3')()
    block3 = msgpass.layerModelWrapper(
        layer_graph, 'step', 'pro', infer_method, frequency, ('c2',))
    if use_cuda:
        block3.cuda()

    layer_graph = getattr(mnist_models, 'res4')()
    block4 = msgpass.layerModelWrapper(
        layer_graph, 'step', 'pro', infer_method, frequency, ('c2',))
    if use_cuda:
        block4.cuda()            

    layer_graph = getattr(mnist_models, 'densify')()
    dense = msgpass.layerModelWrapper(
        layer_graph, 'step', 'pro', infer_method, frequency, ('o',))
    if use_cuda:
        dense.cuda()    

    class ResModel(nn.Module, SavableModel):
        def __init__(self, block1, block2, block3, block4, dense):
            super(ResModel, self).__init__()
            self.block1 = block1
            self.block2 = block2
            self.block3 = block3
            self.block4 = block4
            self.dense = dense
            # self.relu = nn.ReLU()
            # self.conv = conv

        def forward(self, input):
            hidden = self.block1(input) #[0][:,:,1:]
            res_out = hidden[0] + flattened_crop(input, kernel_size=3, stride=3)
            hidden = self.block2(res_out)
            res_out = hidden[0] + flattened_crop(res_out[0], kernel_size=3, stride=2, padding=1)
            # res_out = flattened_crop(res_out)+hidden
            # hidden = self.block3(res_out)[0][:,:,1:]
            # res_out = flattened_crop(res_out)+hidden
            # hidden = self.block4(res_out)[0][:,:,1:]
            # res_out = flattened_crop(res_out)+hidden
            o = self.dense(res_out)
            return o
            # return self.conv(input)

        def save_model(self, path: str,
                   addons: Optional[Dict[Hashable, Picklable]] = None) -> None:
            if addons is None:
                addons = {}
            dic = {'state_dict': self.state_dict(),
                **addons}
            torch.save(dic, path)  
            
        @classmethod
        def load_model(cls, path: str
                    ) -> ('LayerModel', Dict[Hashable, Picklable]):
            dic = torch.load(path)
            protocol = dic['proto']
            state_dict = dic['state_dict']
            model = cls(protocol)
            if state_dict:
                model.load_state_dict(state_dict)
            del dic['proto']
            del dic['state_dict']
            return model, dic



    msg_model = ResModel(block1, block2, block3, block4, dense)

    optimizer = optim.__dict__[optim_type](msg_model.parameters(),
                                               **optim_kwargs)
    # training part

    def update_backup(backup: Optional[str], i: int, time_stamp: str) -> str:
        tmp = save_dir + '{0}_{1}_{2}.pickle'.format(log_title, i, time_stamp)
        training_backup(msg_model, optimizer, tmp)
        if backup is not None:
            if not rm(backup):
                print('Failed to delete {0}'.format(backup))
        return tmp

    def do_train_epoch(i: int) -> Tuple[float, str]:
        train_epoch(msg_model, optimizer, train_loader, i, use_cuda,
                    loss_func=lossfunc,
                    output_ops=lambda x: x[0].squeeze(1), log_interval=100)
        time_stamp = get_timestamp()
        avg_loss, _ = eval_epoch(msg_model, val_loader, i, use_cuda,
                                 loss_func=lossfunc,
                                 input_ops=lambda x: x.unsqueeze(-1),
                                 output_ops=lambda x: x[0].squeeze(1))
        return avg_loss, time_stamp


    backup = None
    if epochs is None:  # use early stopping and backup only the best one
        i = 1
        earlystop = EarlyStopper(patience=patience, should_decrease=True)
        while earlystop.passes():
            avg_loss, time_stamp = do_train_epoch(i)
            isbest = earlystop.update(avg_loss)
            if isbest:
                backup = update_backup(backup, i, time_stamp)
            i += 1
        # revert to the best one for testing
        msg_model, _ = training_resume(backup, use_cuda, msgpass.loadWrapper)
    else:  # learning with fixed epochs and backup weights for each epoch
        for i in range(1, epochs + 1):
            _, time_stamp = do_train_epoch(i)
            backup = update_backup(None, i, time_stamp)

    # testing part

    test(msg_model, test_loader, use_cuda, loss_func=lossfunc,
         input_ops=lambda x: x.unsqueeze(-1),
         output_ops=lambda x: x[0].squeeze(1))

    # stop logger

    logger.close()
