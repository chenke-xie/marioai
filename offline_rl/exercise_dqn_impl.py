from cmath import exp
import copy
from typing import Optional, Sequence, cast

import numpy as np
import torch
from torch.optim import Optimizer

from d3rlpy.gpu import Device
from d3rlpy.models.builders import create_discrete_q_function
from d3rlpy.models.encoders import EncoderFactory
from d3rlpy.models.optimizers import OptimizerFactory
from d3rlpy.models.q_functions import QFunctionFactory, DiscreteMeanQFunction
from d3rlpy.models.torch import EnsembleDiscreteQFunction, EnsembleQFunction
from d3rlpy.preprocessing import RewardScaler, Scaler
from d3rlpy.torch_utility import TorchMiniBatch, hard_sync, torch_api, train_api
from d3rlpy.algos.torch.base import TorchImplBase
from d3rlpy.algos.torch.utility import DiscreteQFunctionMixin
import torch.nn.functional as F


class DQNImpl(DiscreteQFunctionMixin, TorchImplBase):
    _learning_rate: float
    _optim_factory: OptimizerFactory
    _encoder_factory: EncoderFactory
    _q_func_factory: QFunctionFactory
    _gamma: float
    _n_critics: int
    _use_gpu: Optional[Device]
    _q_func: Optional[EnsembleDiscreteQFunction]
    _targ_q_func: Optional[EnsembleDiscreteQFunction]
    _optim: Optional[Optimizer]

    def __init__(
        self,
        huber_loss,
        observation_shape: Sequence[int],
        action_size: int,
        learning_rate: float,
        optim_factory: OptimizerFactory,
        encoder_factory: EncoderFactory,
        q_func_factory: QFunctionFactory,
        gamma: float,
        n_critics: int,
        use_gpu: Optional[Device],
        scaler: Optional[Scaler],
        reward_scaler: Optional[RewardScaler],
    ):
        super().__init__(
            observation_shape=observation_shape,
            action_size=action_size,
            scaler=scaler,
            action_scaler=None,
            reward_scaler=reward_scaler,
        )
        self._learning_rate = learning_rate
        self._optim_factory = optim_factory
        self._encoder_factory = encoder_factory
        self._q_func_factory = q_func_factory
        self._gamma = gamma
        self._n_critics = n_critics
        self._use_gpu = use_gpu
        self._huber_loss = huber_loss

        # initialized in build
        self._q_func = None
        self._targ_q_func = None
        self._optim = None

    def build(self) -> None:
        # setup torch models
        self._build_network()

        # setup target network
        self._targ_q_func = copy.deepcopy(self._q_func)

        if self._use_gpu:
            self.to_gpu(self._use_gpu)
        else:
            self.to_cpu()

        # setup optimizer after the parameters move to GPU
        self._build_optim()

    def _build_network(self) -> None:
        self._q_func = create_discrete_q_function(
            self._observation_shape,
            self._action_size,
            self._encoder_factory,
            self._q_func_factory,
            n_ensembles=self._n_critics,
        )

    def _build_optim(self) -> None:
        assert self._q_func is not None
        self._optim = self._optim_factory.create(
            self._q_func.parameters(), lr=self._learning_rate
        )


    @train_api
    @torch_api(scaler_targets=["obs_t", "obs_tpn"])
    def update(self, batch: TorchMiniBatch) -> np.ndarray:
        assert self._optim is not None

        self._optim.zero_grad()

        q_tpn = self.compute_target(batch)
     
        loss = self.compute_loss(batch, q_tpn)
    
        loss.backward()
        self._optim.step()

        return loss.cpu().detach().numpy()

    def compute_loss(
        self,
        batch: TorchMiniBatch,
        q_tpn: torch.Tensor,
    ) -> torch.Tensor:
        assert self._q_func is not None
        assert q_tpn.ndim == 2
        
        td_sum = torch.tensor(
            0.0, dtype=torch.float32, device=batch.observations.device
        )
        
        for q_func in self._q_func._q_funcs: 
            loss = self.compute_error(
                huber_loss=self._huber_loss,
                observations=batch.observations,
                actions=batch.actions.long(),
                rewards=batch.rewards,
                target=q_tpn,
                terminals=batch.terminals,
                gamma=self._gamma**batch.n_steps, 
                q_func=q_func,
            )  
            
            td_sum += loss.mean()

        return td_sum
    
    
    def compute_error(
        self,
        huber_loss,
        observations: torch.Tensor,
        actions: torch.Tensor,
        rewards: torch.Tensor,
        target: torch.Tensor,
        terminals: torch.Tensor,
        q_func: DiscreteMeanQFunction,
        gamma: float = 0.99,
        beta: float = 1.0
    ) -> torch.Tensor:
        one_hot = F.one_hot(actions.view(-1), num_classes=self.action_size)
        value = (q_func.forward(observations) * one_hot.float()).sum(
            dim=1, keepdim=True
        )
        
        
        loss = huber_loss(beta, gamma, rewards, target, value)
    

        return loss


    def compute_target(self, batch: TorchMiniBatch) -> torch.Tensor:
        assert self._targ_q_func is not None
        with torch.no_grad():
            next_actions = self._targ_q_func(batch.next_observations)
            max_action = next_actions.argmax(dim=1)
            return self._targ_q_func.compute_target(
                batch.next_observations,
                max_action,
                reduction="min",
            )

    def compute_next_state(self, batch: TorchMiniBatch, q_func: DiscreteMeanQFunction) -> torch.Tensor:
        assert self._q_func is not None
        one_hot = F.one_hot(self._q_func(batch.next_observations).argmax(dim=1), num_classes=self.action_size)
        next_state = (q_func.forward(batch.next_observations) * one_hot.float()).sum(
            dim=1, keepdim=True
        )
        
        return next_state

    def _predict_best_action(self, x: torch.Tensor) -> torch.Tensor:
        assert self._q_func is not None
        return self._q_func(x).argmax(dim=1)

    def _sample_action(self, x: torch.Tensor) -> torch.Tensor:
        return self._predict_best_action(x)

    def update_target(self) -> None:
        assert self._q_func is not None
        assert self._targ_q_func is not None
        hard_sync(self._targ_q_func, self._q_func)

    @property
    def q_function(self) -> EnsembleQFunction:
        assert self._q_func
        return self._q_func

