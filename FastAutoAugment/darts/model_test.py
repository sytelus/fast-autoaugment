import torch
import torch.nn as nn

from .operations import *
from ..common.utils import drop_path_


class _Cell(nn.Module):

    def __init__(self, genotype, C_prev_prev, C_prev, ch_out_init, reduction, reduction_prev):
        """
        This class is different then arch_cnn_model._Cell. Here we recieve genotype and build a cell
        that has 4 nodes, each with exactly two edges and only one primitive attached to this edge.
        This cell then would be "compiled" to produce PyTorch module.

        :param genotype:
        :param C_prev_prev:
        :param C_prev:
        :param ch_out_init:
        :param reduction:
        :param reduction_prev:
        """
        super().__init__()

        print(C_prev_prev, C_prev, ch_out_init)

        # if previous layer was reduction layer
        if reduction_prev:
            self.preprocess0 = FactorizedReduce(C_prev_prev, ch_out_init)
        else:
            self.preprocess0 = ReLUConvBN(C_prev_prev, ch_out_init, 1, 1, 0)
        self.preprocess1 = ReLUConvBN(C_prev, ch_out_init, 1, 1, 0)

        if reduction:
            op_names, indices = zip(*genotype.reduce)
            concat = genotype.reduce_concat
        else:
            op_names, indices = zip(*genotype.normal)
            concat = genotype.normal_concat
        self._compile(ch_out_init, op_names, indices, concat, reduction)

    def _compile(self, ch_out_init, op_names, indices, concat, reduction):
        """

        :param ch_out_init:
        :param op_names:
        :param indices:
        :param concat:
        :param reduction:
        :return:
        """
        assert len(op_names) == len(indices)

        self._steps = len(op_names) // 2
        self._concat = concat
        self.multiplier = len(concat)

        self._ops = nn.ModuleList()
        for name, index in zip(op_names, indices):
            stride = 2 if reduction and index < 2 else 1
            op = OPS[name](ch_out_init, stride, True)
            self._ops += [op]
        self._indices = indices

    def forward(self, s0, s1, drop_prob):
        """

        :param s0:
        :param s1:
        :param drop_prob:
        :return:
        """
        s0 = self.preprocess0(s0)
        s1 = self.preprocess1(s1)

        states = [s0, s1]
        for i in range(self._steps):
            # for each noce i, find which previous two node we
            # connect to and corresponding ops for them
            h1 = states[self._indices[2 * i]]
            h2 = states[self._indices[2 * i + 1]]
            op1 = self._ops[2 * i]
            op2 = self._ops[2 * i + 1]
            h1 = op1(h1)
            h2 = op2(h2)

            if self.training and drop_prob > 0.:
                if not isinstance(op1, Identity):
                    h1 = drop_path_(h1, drop_prob)
                if not isinstance(op2, Identity):
                    h2 = drop_path_(h2, drop_prob)

            # aggregation of ops result is arithmatic sum
            s = h1 + h2
            states += [s]

        # concatenate outputs of all node which becomes the result of the cell
        # this makes it necessory that wxh is same for all outputs
        return torch.cat([states[i] for i in self._concat], dim=1)


class AuxiliaryHeadCIFAR(nn.Module):
    # Auxiliary head is just hard coded good known network
    def __init__(self, ch_out_init, n_classes):
        """assuming input size 8x8"""
        super(AuxiliaryHeadCIFAR, self).__init__()

        self.features = nn.Sequential(
            nn.ReLU(inplace=True),
            nn.AvgPool2d(5, stride=3, padding=0, count_include_pad=False),  # image size = 2 x 2
            nn.Conv2d(ch_out_init, 128, 1, bias=False),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(128, 768, 2, bias=False),
            nn.BatchNorm2d(768),
            nn.ReLU(inplace=True)
        )
        self.classifier = nn.Linear(768, n_classes)

    def forward(self, x):
        x = self.features(x)
        x = self.classifier(x.view(x.size(0), -1))
        return x


class AuxiliaryHeadImageNet(nn.Module):

    def __init__(self, ch_out_init, n_classes):
        """assuming input size 14x14"""
        super(AuxiliaryHeadImageNet, self).__init__()
        self.features = nn.Sequential(
            nn.ReLU(inplace=True),
            nn.AvgPool2d(5, stride=2, padding=0, count_include_pad=False),
            nn.Conv2d(ch_out_init, 128, 1, bias=False),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(128, 768, 2, bias=False),
            # NOTE: This batchnorm was omitted in my earlier implementation due to a typo.
            # Commenting it out for consistency with the experiments in the paper.
            # nn.BatchNorm2d(768),
            nn.ReLU(inplace=True)
        )
        self.classifier = nn.Linear(768, n_classes)

    def forward(self, x):
        x = self.features(x)
        x = self.classifier(x.view(x.size(0), -1))
        return x


class NetworkCIFAR(nn.Module):

    def __init__(self, ch_in:int, ch_out_init:int, n_classes:int, layers:int,
        auxiliary, genotype, stem_multiplier=3):
        super(NetworkCIFAR, self).__init__()

        self._layers = layers
        self._auxiliary = auxiliary

        stem_multiplier = 3
        C_curr = stem_multiplier * ch_out_init
        self.stem = nn.Sequential(
            nn.Conv2d(3, C_curr, 3, padding=1, bias=False),
            nn.BatchNorm2d(C_curr)
        )

        C_prev_prev, C_prev, C_curr = C_curr, C_curr, ch_out_init
        self.cells = nn.ModuleList()
        reduction_prev = False
        for i in range(layers):
            if i in [layers // 3, 2 * layers // 3]:
                C_curr *= 2
                reduction = True
            else:
                reduction = False
            cell = _Cell(genotype, C_prev_prev, C_prev, C_curr, reduction, reduction_prev)
            reduction_prev = reduction
            self.cells += [cell]
            C_prev_prev, C_prev = C_prev, cell.multiplier * C_curr
            if i == 2 * layers // 3:
                C_to_auxiliary = C_prev

        if auxiliary:
            self.auxiliary_head = AuxiliaryHeadCIFAR(C_to_auxiliary, n_classes)
        self.global_pooling = nn.AdaptiveAvgPool2d(1)
        self.classifier = nn.Linear(C_prev, n_classes)

    def forward(self, input):
        logits_aux = None
        s0 = s1 = self.stem(input)
        for i, cell in enumerate(self.cells):
            s0, s1 = s1, cell(s0, s1)
            if i == 2 * self._layers // 3:
                # if asked, also provide logits of good known hand coded model
                if self._auxiliary and self.training:
                    logits_aux = self.auxiliary_head(s1)
        out = self.global_pooling(s1)
        logits = self.classifier(out.view(out.size(0), -1))
        return logits, logits_aux

    def drop_path_prob(self, p):
        """ Set drop path probability """
        for module in self.modules():
            if isinstance(module, DropPath_):
                module.p = p


class NetworkImageNet(nn.Module):

    def __init__(self, ch_out_init, n_classes, layers, auxiliary, genotype):
        super(NetworkImageNet, self).__init__()
        self._layers = layers
        self._auxiliary = auxiliary

        self.stem0 = nn.Sequential(
            nn.Conv2d(3, ch_out_init // 2, kernel_size=3, stride=2, padding=1, bias=False),
            nn.BatchNorm2d(ch_out_init // 2),
            nn.ReLU(inplace=True),
            nn.Conv2d(ch_out_init // 2, ch_out_init, 3, stride=2, padding=1, bias=False),
            nn.BatchNorm2d(ch_out_init),
        )

        self.stem1 = nn.Sequential(
            nn.ReLU(inplace=True),
            nn.Conv2d(ch_out_init, ch_out_init, 3, stride=2, padding=1, bias=False),
            nn.BatchNorm2d(ch_out_init),
        )

        C_prev_prev, C_prev, C_curr = ch_out_init, ch_out_init, ch_out_init

        self.cells = nn.ModuleList()
        reduction_prev = True
        for i in range(layers):
            if i in [layers // 3, 2 * layers // 3]:
                C_curr *= 2
                reduction = True
            else:
                reduction = False
            cell = _Cell(genotype, C_prev_prev, C_prev, C_curr, reduction, reduction_prev)
            reduction_prev = reduction
            self.cells += [cell]
            C_prev_prev, C_prev = C_prev, cell.multiplier * C_curr
            if i == 2 * layers // 3:
                C_to_auxiliary = C_prev

        if auxiliary:
            self.auxiliary_head = AuxiliaryHeadImageNet(C_to_auxiliary, n_classes)
        self.global_pooling = nn.AvgPool2d(7)
        self.classifier = nn.Linear(C_prev, n_classes)

    def forward(self, input):
        logits_aux = None
        s0 = self.stem0(input)
        s1 = self.stem1(s0)
        for i, cell in enumerate(self.cells):
            s0, s1 = s1, cell(s0, s1, self.drop_path_prob)
            if i == 2 * self._layers // 3:
                if self._auxiliary and self.training:
                    logits_aux = self.auxiliary_head(s1)
        out = self.global_pooling(s1)
        logits = self.classifier(out.view(out.size(0), -1))
        return logits, logits_aux
