import os
from typing import Tuple

import torch
import torch.nn as nn
from torch.optim.optimizer import Optimizer
from torch.utils.data.dataloader import DataLoader
import torchvision.datasets as tvds
from torch.nn.modules.loss import _Loss
from torch.optim.lr_scheduler import _LRScheduler

from ..common import utils
from ..common.common import get_logger, get_tb_writer
from ..common.train_test_utils import train_test
from ..common.data import get_dataloaders
from . import cnn_test_model
from ..common.optimizer import get_lr_scheduler, get_optimizer, get_lossfn
from . import genotypes

def test_arch(conf):
    logger, writer = get_logger(), get_tb_writer()

    # region conf vars
    conf_ds           = conf['dataset']
    dataroot          = conf['dataroot']
    chkptdir          = conf['chkptdir']
    conf_test         = conf['darts']['test']
    conf_train_lossfn = conf_test['train_lossfn']
    conf_test_lossfn  = conf_test['test_lossfn']
    conf_loader       = conf_test['loader']
    cutout            = conf_loader['cutout']
    test_genotype     = conf_test['genotype']
    ch_out_init       = conf_test['ch_out_init']
    n_layers          = conf_test['layers']
    aux_weight        = conf_test['aux_weight']
    drop_path_prob    = conf_test['drop_path_prob']
    ds_name           = conf_ds['name']
    ch_in             = conf_ds['ch_in']
    n_classes         = conf_ds['n_classes']
    max_batches       = conf_ds['max_batches']
    aug               = conf_loader['aug']
    cutout            = conf_loader['cutout']
    val_ratio         = conf_loader['val_ratio']
    batch_size        = conf_loader['batch']
    epochs            = conf_loader['epochs']
    n_workers         = conf_loader['n_workers']
    conf_opt          = conf_test['optimizer']
    conf_lr_sched     = conf_test['lr_schedule']
    model_classname   = conf_test['model_class']
    report_freq       = conf['report_freq']
    horovod           = conf['horovod']
    aux_weight        = conf_test['aux_weight']
    grad_clip         = conf_opt['clip']
    data_parallel     = conf_test['data_parallel']


    # endregion

    device = torch.device("cuda")

    train_dl, _, test_dl, _ = get_dataloaders(
        ds_name, batch_size, dataroot, aug, cutout,
        load_train=True, load_test=True,
        val_ratio=0., val_fold=0, # no validation set
        horovod=horovod, max_batches=max_batches, n_workers=n_workers)

    # load genotype we want to test
    genotype = eval("genotypes.%s" % test_genotype)
    logger.info('test genotype: {}'.format(genotype))

    train_lossfn = get_lossfn(conf_train_lossfn, conf_ds).to(device)
    test_lossfn = get_lossfn(conf_test_lossfn, conf_ds).to(device)

    # create model
    model_class = getattr(cnn_test_model, model_classname)
    model = model_class(ch_in, ch_out_init, n_classes, n_layers, aux_weight, genotype)
    logger.info("Model size = {:.3f} MB".format(utils.param_size(model)))
    if data_parallel:
        model = nn.DataParallel(model).to(device)
    else:
        model = model.to(device)

    optim = get_optimizer(conf_opt, model.parameters())
    lr_scheduler = get_lr_scheduler(conf_lr_sched, epochs, optim)

    best_top1 = train_test(train_dl, test_dl, model, device,
        train_lossfn, test_lossfn, optim,
        aux_weight, lr_scheduler, drop_path_prob, chkptdir, grad_clip,
        report_freq, epochs)
    logger.info('best_top1 %f', best_top1)




