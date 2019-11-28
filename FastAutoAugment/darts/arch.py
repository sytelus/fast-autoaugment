import  torch
import  numpy as np
from    torch import autograd
from torch.optim import Optimizer


from .model_arch import Network
from ..common.config import Config
from ..common.optimizer import get_optimizer


def concat(xs):
    """
    flatten all tensor from [d1,d2,...dn] to [d]
    and then concat all [d_1] to [d_1+d_2+d_3+...]

    :param xs:
    :return:
    """

    # t.view(-1) reshapes tensor to 1 row N columnsstairs
#   theta - model parameters (refered to as w in paper)
#   alpha - arch parameters
#   theta' - updated theta using grads from the loss
class Arch:

    def __init__(self, model:Network, conf:Config)->None:
        self.momentum = conf['optimizer']['momentum'] # momentum for optimizer of theta
        self.wd =conf['optimizer']['decay'] # weight decay for optimizer of theta
        self.model = model # main model with respect to theta and alpha
        self.bilevel:bool = conf['darts']['bilevel']

        # this is the optimizer to optimize alpha parameter
        self.arch_optimizer = get_optimizer(conf['optimizer_arch'], model.parameters())

    def _comp_unrolled_model(self, x, target, eta, optimizer:Optimizer):
        """
        loss on train set and then update w_pi, not-in-place
        :param x:
        :param target:
        :param eta:
        :param optimizer: optimizer of theta, not optimizer of alpha
        :return:
        """
        # forward to get loss
        # pass training input through cells and FCs, compute loss with label
        loss = self.model.loss(x, target)

        # we couldn't do sgd on loss because that will affect arch parameters as well.
        # We want to sgd only on model paramters. To do this, we do manual sgd here by
        # getting model parameters, detaching them and computing grads only on them.
        # We then have updated parameters from which we construct new duplicate model object.
        # This is our "unrolled" model. It is essentially the updated model m' from m if we
        # had frozen the arch parameters.

        # flatten current weights
        # these are not the arch parameters (weights for the ops), but actual model params
        # model parameters are simply list of tensors with different shapes. Because of different shapes
        # they must be looped through to apply manual SGD however that would be very slow. To circumvent that
        # we flatten them here and then reshape later
        theta = concat(self.model.parameters()).detach()
        # theta: torch.Size([1930618])
        # print('theta:', theta.shape)
        try:
            # fetch momentum data from theta optimizer
            moment = concat(optimizer.state[v]['momentum_buffer'] for v in self.model.parameters())
            moment.mul_(self.momentum) # TODO: is this correct?
        except:
            moment = torch.zeros_like(theta)

        # flatten all gradients
        dtheta = concat(autograd.grad(loss, self.model.parameters())).data
        # indeed, here we implement a simple SGD with momentum and weight decay
        # theta = theta - eta * (moment + weight decay + dtheta)
        theta = theta.sub(eta, moment + dtheta + self.wd * theta)
        # construct a new model
        unrolled_model = self._construct_model_from_theta(theta)

        return unrolled_model

    def step(self, x_train, target_train, x_valid, target_valid, eta:float, optimizer:Optimizer):
        """
        update alpha parameter by manually computing the gradients
        :param x_train: training input
        :param target_train: training labels
        :param x_valid: validation input
        :param target_valid: validation labels
        :param eta:
        :param optimizer: theta optimizer
        :param unrolled:
        :return:
        """
        # alpha optimizer
        self.arch_optimizer.zero_grad()

        # compute the gradient and write it into tensor.grad
        # instead of generated by loss.backward()
        if self.bilevel: # this should be True unless we need to do abalation study of bilevel vs naive optimization
            self._backward_step_bilevel(x_train, target_train, x_valid, target_valid, eta, optimizer)
        else:
            # directly optimize alpha on w, instead of w_pi
            self._backward_step(x_valid, target_valid)

        # at this point we should have model with updated grades for theta and alpha
        self.arch_optimizer.step()

    def _backward_step(self, x_valid, target_valid):
        """
        This function is used only for experimentation to see how much better
        is bilevel optimization vs simply doing it naively
        simply train on validate set and backward
        :param x_valid:
        :param target_valid:
        :return:
        """
        loss = self.model.loss(x_valid, target_valid)
        # both alpha and theta require grad but only alpha optimizer will
        # step in current phase.
        loss.backward()

    def _backward_step_bilevel(self, x_train, target_train, x_valid, target_valid, eta, optimizer):
        """
        train on validate set based on update w_pi
        :param x_train:
        :param target_train:
        :param x_valid:
        :param target_valid:
        :param eta: 0.01, according to author's comments
        :param optimizer: theta optimizer
        :return:
        """

        # get a model with updated theta, but leave alpha as-is
        # theta' = theta - lr * grad
        unrolled_model = self._comp_unrolled_model(x_train, target_train, eta, optimizer)

        # compute loss on validation set
        # calculate loss on model with theta'
        unrolled_loss = unrolled_model.loss(x_valid, target_valid)

        # this will update model with theta' model, but not model with theta
        # also notice that this will generate grads on theta as well as alpha parameters
        unrolled_loss.backward()

        # grad(L(w', a), a), part of Eq. 6
        # get grades for alpha parameters
        dalpha = [v.grad for v in unrolled_model.arch_parameters()]
        # get grades for theta' params which we will use it to compute theta+ and theta-
        # theta+ = theta + epsilon * grad(theta')
        theta2_grad = [v.grad.data for v in unrolled_model.parameters()]
        hessian = self._hessian_vector_product(theta2_grad, x_train, target_train)

        # subtract hessian from grad of alpa (Eq 7)
        for da, h in zip(dalpha, hessian):
            da.data.sub_(eta, h.data)

        # dalpha we have is from the unrolled model so we need to
        # transfer those grades back to our main model
        for a, d in zip(self.model.arch_parameters(), dalpha):
            if a.grad is None: # this would be the case first time?
                a.grad = d.data
            else:
                a.grad.data.copy_(d.data)
        # now that model has both theta and alpha grads,
        # we can run optimizer.step() to update the param values

    def _construct_model_from_theta(self, theta):
        """
        construct a new model with initialized weight from theta
        it use .state_dict() and load_state_dict() instead of
        .parameters() + fill_()
        :param theta: flatten weights, need to reshape to original shape
        :return:
        """

        # first clone the model such as alpha params have same state as current model
        # but theta params would still be set to initial values
        model_new = self.model.new()
        model_dict = self.model.state_dict()

        # note that theta are flattened so before we put them back in model,
        # we need to reshape them
        params, offset = {}, 0
        for k, v in self.model.named_parameters():
            v_length = v.numel()
            # restore theta[] value to original shape
            params[k] = theta[offset: offset + v_length].view(v.size())
            offset += v_length

        # put theta back in our new model so its now same as clone of
        # current model but with updated theta
        assert offset == len(theta)
        model_dict.update(params)
        model_new.load_state_dict(model_dict)
        return model_new.cuda()

    def _hessian_vector_product(self, theta2_grad, x, target, epsilon_unit=1e-2):
        """
        Implements equation 8
        :param theta2_grad: gradient.data of parameters theta
        :param x:
        :param target:
        :param epsilon_unit:
        :return:
        """

        # scale epsilon with grad magnitude. Notice that theta2_grad
        # is a multiplier on RHS of eq 8. So this scalling is essential in maing sure
        # that finite differences approximation is not way off
        epsilon = epsilon_unit / concat(theta2_grad).norm()

        # theta+ = theta + epsilon * grad(theta')
        for p, v in zip(self.model.parameters(), theta2_grad):
            p.data.add_(epsilon, v)

        # Now that we have model with theta+, we need to compute grads wrt alpha
        # note that this loss needs to be on train set, not validation set
        loss = self.model.loss(x, target)
        grads_p = autograd.grad(loss, self.model.arch_parameters())

        # get model with theta- and then compute grads wrt alpha
        for p, v in zip(self.model.parameters(), theta2_grad):
            # we had already added theta2_grad above so sutracting twice gives theta-
            p.data.sub_(2 * epsilon, v)
        loss = self.model.loss(x, target)
        grads_n = autograd.grad(loss, self.model.arch_parameters())

        # reset back params to original values by adding theta2_grad
        for p, v in zip(self.model.parameters(), theta2_grad):
            p.data.add_(epsilon, v)

        # apply eq 8, final difference to compute hessian
        h= [(x - y).div_(2 * epsilon) for x, y in zip(grads_p, grads_n)]
        # h len: 2 h0 torch.Size([14, 8])
        # print('h len:', len(h), 'h0', h[0].shape)
        return h
