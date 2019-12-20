from typing import Union, Any

import  torch
import  torch.nn as nn
import  numpy as np
from    torch import autograd
from torch.optim import Optimizer
from torch.nn.modules.loss import _Loss

import copy

from .model import Model
from ..common.config import Config
from ..common.optimizer import get_optimizer


def _get_loss(model, lossfn, x, y):
    logits, *_ = model(x)
    return lossfn(logits, y)

# t.view(-1) reshapes tensor to 1 row N columnsstairs
#   w - model parameters
#   alphas - arch parameters
#   w' - updated w using grads from the loss
class Arch:
    def __init__(self, conf:Config, model:Union[nn.DataParallel, Model],
                 lossfn:_Loss)->None:
        # region conf vars
        conf_search = conf['darts']['search']
        conf_w_opt  = conf_search['weights']['optimizer']
        conf_a_opt  = conf_search['alphas']['optimizer']
        w_momentum  = conf_w_opt['momentum']
        w_decay     = conf_w_opt['decay']
        bilevel     = conf_search['bilevel']
        # endregion

        self._w_momentum = w_momentum # momentum for w
        self._w_weight_decay = w_decay # weight decay for w
        self._lossfn = lossfn
        self._model = model # main model with respect to w and alpha
        self._bilevel:bool = bilevel

        # create a copy of model which we will use
        # to compute grads for alphas without disturbing
        # original weights
        # TODO: see if there are any issues in deepcopy for pytorch
        self._vmodel = copy.deepcopy(model)

        # this is the optimizer to optimize alphas parameter
        self._alpha_optim = get_optimizer(conf_a_opt, model.alphas())

    def _update_vmodel(self, x, y, lr:float, w_optim:Optimizer)->None:
        """ Update vmodel with w' (main model has w) """

        # TODO: should this loss be stored for later use?
        loss = _get_loss(self._model, self._lossfn, x, y)
        gradients = autograd.grad(loss, self._model.weights())

        """update weights in vmodel so we leave main model undisturbed
        The main technical difficulty computing w' without affecting alphas is
        that you can't simply do backward() and step() on loss because loss
        tracks alphas as well as w. So, we compute gradients using autograd and
        do manual sgd update."""
        # TODO: other alternative may be to (1) copy model
        #   (2) set require_grads = False on alphas
        #   (3) loss and step on vmodel (4) set back require_grades = True
        with torch.no_grad(): # no need to track gradient for these operations
            for w, vw, g in zip(
                    self._model.weights(), self._vmodel.weights(), gradients):
                # simulate mometum update on model but put this update in vmodel
                m = w_optim.state[w].get('momentum_buffer', 0.)*self._w_momentum
                vw.copy_(w - lr * (m + g + self._w_weight_decay*w))

            # synchronize alphas
            for a, va in zip(self._model.alphas(), self._vmodel.alphas()):
                va.copy_(a)

    def step(self, x_train, y_train, x_valid, y_valid, lr:float, w_optim:Optimizer):
        self._alpha_optim.zero_grad()

        # compute the gradient and write it into tensor.grad
        # instead of generated by loss.backward()
        if self._bilevel:
            self._backward_bilevel(x_train, y_train, x_valid, y_valid, lr, w_optim)
        else:
            # directly optimize alpha on w, instead of w_pi
            self._backward_classic(x_valid, y_valid)

        # at this point we should have model with updated grades for w and alpha
        self._alpha_optim.step()

    def _backward_classic(self, x_valid, y_valid):
        """
        This function is used only for experimentation to see how much better
        is bilevel optimization vs simply doing it naively
        simply train on validate set and backward
        :param x_valid:
        :param y_valid:
        :return:
        """
        loss = _get_loss(self._model, self._lossfn, x_valid, y_valid)
        # both alphas and w require grad but only alphas optimizer will
        # step in current phase.
        loss.backward()

    def _backward_bilevel(self, x_train, y_train, x_valid, y_valid, lr, w_optim):
        """ Compute unrolled loss and backward its gradients """

        # update vmodel with w', but leave alphas as-is
        # w' = w - lr * grad
        self._update_vmodel(x_train, y_train, lr, w_optim)

        # compute loss on validation set for model with w'
        # wrt alphas. The autograd.grad is used instead of backward()
        # to avoid having to loop through params
        vloss = _get_loss(self._vmodel, self._lossfn, x_valid, y_valid)

        v_alphas = tuple(self._vmodel.alphas())
        v_weights = tuple(self._vmodel.weights())
        v_grads = autograd.grad(vloss, v_alphas + v_weights)

        # grad(L(w', a), a), part of Eq. 6
        dalpha = v_grads[:len(v_alphas)]
        # get grades for w' params which we will use it to compute w+ and w-
        dw = v_grads[len(v_alphas):]

        hessian = self._hessian_vector_product(dw, x_train, y_train)

        # dalpha we have is from the unrolled model so we need to
        # transfer those grades back to our main model
        # update final gradient = dalpha - xi*hessian
        with torch.no_grad():
            for alpha, da, h in zip(self._model.alphas(), dalpha, hessian):
                alpha.grad = da - lr*h
        # now that model has both w and alpha grads,
        # we can run w_optim.step() to update the param values

    def _hessian_vector_product(self, dw, x, y, epsilon_unit=1e-2):
        """
        Implements equation 8

        dw = dw` {L_val(w`, alpha)}
        w+ = w + eps * dw
        w- = w - eps * dw
        hessian = (dalpha {L_trn(w+, alpha)} -dalpha {L_trn(w-, alpha)})/(2*eps)
        eps = 0.01 / ||dw||
        """

        """scale epsilon with grad magnitude. The dw
        is a multiplier on RHS of eq 8. So this scalling is essential
        in making sure that finite differences approximation is not way off
        Below, we flatten each w, concate all and then take norm"""
        dw_norm = torch.cat([w.view(-1) for w in dw]).norm()
        epsilon = epsilon_unit / dw_norm

        # w+ = w + epsilon * grad(w')
        with torch.no_grad():
            for p, v in zip(self._model.weights(), dw):
                p += epsilon * v

        # Now that we have model with w+, we need to compute grads wrt alphas
        # This loss needs to be on train set, not validation set
        loss = _get_loss(self._model, self._lossfn, x, y)
        dalpha_plus = autograd.grad(loss, self._model.alphas()) #dalpha{L_trn(w+)}

        # get model with w- and then compute grads wrt alphas
        # w- = w - eps*dw`
        with torch.no_grad():
            for p, v in zip(self._model.weights(), dw):
                # we had already added dw above so sutracting twice gives w-
                p -= 2. * epsilon * v

        # similarly get dalpha_minus
        loss = _get_loss(self._model, self._lossfn, x, y)
        dalpha_minus = autograd.grad(loss, self._model.alphas())

        # reset back params to original values by adding dw
        with torch.no_grad():
            for p, v in zip(self._model.weights(), dw):
                p += epsilon * v

        # apply eq 8, final difference to compute hessian
        h= [(p - m) / (2. * epsilon) for p, m in zip(dalpha_plus, dalpha_minus)]
        return h