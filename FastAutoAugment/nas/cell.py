from typing import Callable, Iterable, List, Optional, Tuple
from abc import ABC, abstractmethod

from overrides import overrides, EnforceOverrides

import torch
from torch import nn

from ..common.common import get_logger
from .dag_edge import DagEdge
from .model_desc import AuxTowerDesc, CellDesc, EdgeDesc, NodeDesc
from .operations import Op

class Cell(nn.Module, ABC, EnforceOverrides):
    def __init__(self, desc:CellDesc,
                 affine:bool, droppath:bool,
                 alphas_cell:Optional['Cell']):
        super().__init__()

        self.shared_alphas = alphas_cell is not None
        self.desc = desc
        self._preprocess0 = Op.create(desc.s0_op, affine=affine)
        self._preprocess1 = Op.create(desc.s1_op, affine=affine)

        self._dag =  Cell._create_dag(desc.nodes,
            affine=affine, droppath=droppath,
            alphas_cell=alphas_cell)

    @staticmethod
    def _create_dag(nodes_desc:List[NodeDesc],
                    affine:bool, droppath:bool,
                    alphas_cell:Optional['Cell'])->nn.ModuleList:
        dag = nn.ModuleList()
        for i, node_desc in enumerate(nodes_desc):
            edges:nn.ModuleList = nn.ModuleList()
            dag.append(edges)
            assert len(node_desc.edges) > 0
            for j, edge_desc in enumerate(node_desc.edges):
                edges.append(DagEdge(edge_desc,
                    affine=affine, droppath=droppath,
                    alphas_edge=alphas_cell._dag[i][j] if alphas_cell else None))
        return dag

    def alphas(self)->Iterable[nn.Parameter]:
        for node in self._dag:
            for edge in node:
                for alpha in edge.alphas():
                    yield alpha

    def weights(self)->Iterable[nn.Parameter]:
        for node in self._dag:
            for edge in node:
                for p in edge.weights():
                    yield p

    @overrides
    def forward(self, s0, s1):
        s0 = self._preprocess0(s0)
        s1 = self._preprocess1(s1)

        states = [s0, s1]
        for node in self._dag:
            # TODO: we should probably do average here otherwise output will
            #   blow up as number of primitives grows
            o = sum(edge(states) for edge in node)
            states.append(o)

        # TODO: Below assumes same shape except for channels but this won't
        #   happen for max pool etc shapes?
        return torch.cat(states[-self.desc.out_nodes:], dim=1)

    def finalize(self)->CellDesc:
        nodes_desc:List[NodeDesc] = []
        for node in self._dag:
            edge_descs, edge_desc_ranks = [], []
            for edge in node:
                edge_desc, rank = edge.finalize()
                if rank is None:
                    edge_descs.append(edge_desc) # required edge
                else: # optional edge
                    edge_desc_ranks.append((edge_desc, rank))
            if len(edge_desc_ranks) > self.desc.max_final_edges:
                 edge_desc_ranks.sort(key=lambda d:d[1], reverse=True)
                 edge_desc_ranks = edge_desc_ranks[:self.desc.max_final_edges]
            edge_descs.extend((edr[0] for edr in edge_desc_ranks))
            nodes_desc.append(NodeDesc(edge_descs))

        return CellDesc(cell_type=self.desc.cell_type,
                        index=self.desc.index,
                        nodes=nodes_desc,
                        s0_op=self.desc.s0_op, s1_op=self.desc.s1_op,
                        out_nodes=self.desc.out_nodes,
                        node_ch_out=self.desc.node_ch_out,
                        alphas_from=self.desc.alphas_from,
                        max_final_edges=self.desc.max_final_edges)

