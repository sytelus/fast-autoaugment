import  torch.nn as nn
import torch
from torch import optim
import  torchvision.datasets as tvds
from  torch.utils.data import DataLoader
import numpy as np
import os

from ..common.config import Config
from .model_arch import Network
from .arch import Arch
from ..common.data import get_dataloaders
from ..common.common import get_logger, create_tb_writers
from ..common import utils
from ..common.optimizer import get_lr_scheduler, get_optimizer

def search_arch(conf:Config)->None:
    logger = get_logger()

    if not conf['darts']['bilevel']:
        logger.warn('bilevel arg is NOT true. This is useful only for abalation study for bilevel optimization!')

    device = torch.device('cuda')
    writer = create_tb_writers(conf)[0]

    # CIFAR classification task
    criterion = nn.CrossEntropyLoss().to(device)

    # 16 inital channels, num_classes=10, 8 cells (layers)
    model = Network(conf['darts']['init_ch'], conf['num_classes'], conf['darts']['layers'], criterion).to(device)
    logger.info("Total param size = %f MB", utils.count_parameters_in_MB(model))

    # this is the optimizer to optimize
    optimizer = get_optimizer(conf['optimizer'], model.parameters())

    # note that we get only train set here and break it down in 1/2 to get validation set
    # cifar10 has 60K images in 10 classes, 50k in train, 10k in test
    # so ultimately we have 25K train, 25K val, 10k test
    _, train_dl, valid_dl, _ = get_dataloaders(conf['dataset'], conf['batch'],
        conf['dataroot'], conf['aug'], conf['darts']['search_cutout'],
        val_ratio=conf['val_ratio'], val_fold=conf['val_fold'], horovod=conf['horovod'])

    scheduler = get_lr_scheduler(conf, optimizer)

    # arch is sort of meta model that would update theta and alpha parameters
    arch = Arch(model, conf)

    # in this phase we only run 50 epochs
    for epoch in range(conf['epochs']):
        scheduler.step()
        lr = scheduler.get_lr()[0]
        logger.info('\nEpoch: %d lr: %e', epoch, lr)

        # genotype extracts the highest weighted two primitives per node
        # this is for information dump only
        genotype = model.genotype()
        logger.info('Genotype: %s', genotype)

        # print(F.softmax(model.alphas_normal, dim=-1))
        # print(F.softmax(model.alphas_reduce, dim=-1))

        # training
        train_acc, train_obj = _train_epoch(train_dl, valid_dl, model, arch, criterion, optimizer, lr,
            device, conf['optimizer']['clip'], conf['report_freq'])
        logger.info('train acc: %f', train_acc)

        # validation
        valid_acc, valid_obj = _infer(valid_dl, model, criterion, device, conf['report_freq'])
        logger.info('valid acc: %f', valid_acc)

        model_filepath = os.path.join(conf['logdir'], conf['darts']['test_model_filename'])
        utils.save(model, model_filepath)


def _train_epoch(train_dl:DataLoader, valid_dl:DataLoader, model, arch, criterion, optimizer, lr,
    device, grad_clip, report_freq):

    logger = get_logger()

    losses = utils.AverageMeter()
    top1 = utils.AverageMeter()
    top5 = utils.AverageMeter()

    valid_iter = iter(valid_dl)

    for step, (x, target) in enumerate(train_dl):

        batchsz = x.size(0)
        model.train() # put model into train mode

        # [b, 3, 32, 32], [40]
        x, target = x.to(device), target.cuda(non_blocking=True)
        x_search, target_search = next(valid_iter) # [b, 3, 32, 32], [b]
        x_search, target_search = x_search.to(device), target_search.cuda(non_blocking=True)

        # 1. update alpha
        arch.step(x, target, x_search, target_search, lr, optimizer)

        logits = model(x)
        loss = criterion(logits, target)

        # 2. update weight
        optimizer.zero_grad()
        loss.backward()
        # apparently gradient clipping is important
        nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
        # as our arch parameters (i.e. alpha) is kept seperate, they don't get updated
        optimizer.step()

        prec1, prec5 = utils.accuracy(logits, target, topk=(1, 5))
        losses.update(loss.item(), batchsz)
        top1.update(prec1.item(), batchsz)
        top5.update(prec5.item(), batchsz)

        if step % report_freq == 0:
            logger.info('Step:%03d loss:%f acc1:%f acc5:%f', step, losses.avg, top1.avg, top5.avg)

    return top1.avg, losses.avg


def _infer(valid_dl, model, criterion, device, report_freq):
    """
    For a given model we just evaluate metrics on validation set.
    Note that this model is not final, i.e., each node i has i+2 edges
    and each edge with 8 primitives and associated wieghts.

    :param valid_dl:
    :param model:
    :param criterion:
    :return:
    """

    logger = get_logger()
    losses = utils.AverageMeter()
    top1 = utils.AverageMeter()
    top5 = utils.AverageMeter()

    model.eval()

    with torch.no_grad():
        for step, (x, target) in enumerate(valid_dl):

            x, target = x.to(device), target.cuda(non_blocking=True)
            batchsz = x.size(0)

            logits = model(x)
            loss = criterion(logits, target)

            prec1, prec5 = utils.accuracy(logits, target, topk=(1, 5))
            losses.update(loss.item(), batchsz)
            top1.update(prec1.item(), batchsz)
            top5.update(prec5.item(), batchsz)

            if step % report_freq == 0:
                logger.info('>> Validation: %3d %e %f %f', step, losses.avg, top1.avg, top5.avg)

    return top1.avg, losses.avg