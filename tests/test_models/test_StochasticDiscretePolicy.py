import pytest
import logging
from models import StochasticDiscretePolicy
from env_wrapper import EnvWrapper, GymnasiumWrapper
import torch as T

# from abc import ABC, abstractmethod
# import json
# import os
# from typing import List, Tuple, Dict
# from pathlib import Path
# import time

import torch.nn as nn
# from torch import optim
# import torch.nn.functional as F
from torch.distributions import Categorical
# from torch.optim.lr_scheduler import CosineAnnealingLR, StepLR, ExponentialLR
# import numpy as np
import gymnasium as gym
# from gymnasium.envs.registration import EnvSpec
# from gymnasium.wrappers import *
# from gymnasium.vector import VectorEnv, SyncVectorEnv

# Configure logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

class MockEnvWrapper(EnvWrapper):
    def __init__(self, observation_space):
        super().__init__()
        self._observation_space = observation_space
    def reset(self):
        return T.zeros(self._observation_space)  # Example return
    def step(self, action):
        # Example implementation
        return T.zeros(self._observation_space), 0, False, {}
    def render(self, mode="rgb_array"):
        return None  # Dummy return
    def _initialize_env(self, render_freq=0, num_envs=1, seed=None):
        # This method's implementation isn't strictly needed for your tests but can be mocked if required
        return None
    @property
    def observation_space(self):
        return self._observation_space
    @property
    def action_space(self):
        # Since you didn't define this in your mock, let's assume it's a simple space
        return gym.spaces.Box(low=0, high=1, shape=(1,))
    def to_json(self):
        return json.dumps({"type": "MockEnvWrapper", "observation_space": str(self._observation_space)})
    @classmethod
    def from_json(cls, json_string):
        config = json.loads(json_string)
        return cls(eval(config['observation_space']))  # Note: Using eval for simplicity, not recommended in production

class MockGymnasiumWrapper(GymnasiumWrapper):
    def __init__(self, num_envs, observation_shape, action_shape):
        # super().__init__(MockEnvSpec(observation_space))
        # self.env = MockEnvWrapper(observation_space)
        self.env = MockVecEnv(num_envs, observation_shape, action_shape)
        # Implement other required methods here or leave them as is
        logger.info(f'env observation space:{self.env.observation_space}')
        logger.info(f'env single observation space:{self.env.single_observation_space}')
        logger.info(f'env action space:{self.env.action_space}')
        logger.info(f'env single action space:{self.env.single_action_space}')

    def reset(self):
        single_obs = self.single_observation_space.sample()
        return T.stack([single_obs for _ in range(self.observation_space.shape[0])]), {}
    
    def step(self, action):
        single_obs = T.tensor(self.single_observation_space.sample())
        observation = T.stack([single_obs for _ in range(self.observation_space.shape[0])])
        reward = T.zeros(self.observation_space.shape[0])
        terminated = T.zeros(self.observation_space.shape[0], dtype=T.bool)
        truncated = T.zeros(self.observation_space.shape[0], dtype=T.bool)
        info = {}
        return observation, reward, terminated, truncated, info

    @property
    def observation_space(self):
        return self.env.observation_space
    
    @property
    def action_space(self):
        return self.env.action_space
    
    @property
    def single_action_space(self):
        return self.env.single_action_space

    @property
    def single_observation_space(self):
        return self.env.single_observation_space

# Mocking an environment for ValueModel
# Mocking an environment
class MockVecEnv:
    def __init__(self, num_envs, obs_shape, action_shape):
        self.observation_space = gym.spaces.Box(low=0, high=1, shape=(num_envs, *obs_shape))
        self.single_observation_space = gym.spaces.Box(low=0, high=1, shape=obs_shape)
        
        # Determine action space based on action_shape
        if isinstance(action_shape, (list, tuple)) and len(action_shape) > 1:
            # Multi-dimensional action space, treat as Box
            self.action_space = gym.spaces.Box(low=0, high=1, shape=(num_envs, action_shape[0]))
            self.single_action_space = gym.spaces.Box(low=0, high=1, shape=(action_shape[0],))
        elif isinstance(action_shape, int) or (isinstance(action_shape, (list, tuple)) and len(action_shape) == 1):
            # Single number or single-length tuple/list, assume Discrete or MultiDiscrete
            if isinstance(action_shape, int):
                # Discrete space where action_shape is number of choices
                self.action_space = gym.spaces.Discrete(action_shape)
            else:
                # MultiDiscrete where each element in action_shape is number of choices for that action dimension
                self.action_space = gym.spaces.MultiDiscrete([action_shape[0]] * num_envs)
                self.single_action_space = gym.spaces.Discrete(action_shape[0])
        else:
            raise ValueError("Unsupported action shape format")

    def reset(self):
        # Return an initial observation
        return self.observation_space.sample(), {}
    
    def step(self, action):
        # For simplicity, just return something
        observation = self.observation_space.sample()
        reward = 0.0
        terminated = False
        truncated = False
        info = {}
        return observation, reward, terminated, truncated, info

def check_shape(module, input, output=None):
    if output is None:  # Forward pre-hook for input check
        logger.info(f"Input shape to {module.__class__.__name__}: {input[0].shape}")
    else:  # Forward hook for output check
        logger.info(f"Output shape from {module.__class__.__name__}: {output.shape}")



@pytest.fixture
def policy_model(request):
    num_envs, obs_shape, action_shape, layer_config = request.param
    logger.info(f'num_envs:{num_envs}')
    logger.info(f'obs_shape:{obs_shape}')
    logger.info(f'action_shape:{action_shape}')
    logger.info(f'layer_config:{layer_config}')
    env = MockGymnasiumWrapper(num_envs, obs_shape, action_shape)
    model = StochasticDiscretePolicy(env=env, layer_config=layer_config)
    for name, layer in model.named_modules():
        if isinstance(layer, (nn.Conv2d, nn.Flatten, nn.Linear)):  # Add other layer types if needed
            layer.register_forward_hook(lambda m, i, o: check_shape(m, i, o))

    # Set weights and biases directly after model creation
    with T.no_grad():
        for layer in model.layers.values():
            if hasattr(layer, 'weight') and layer.weight is not None:
                layer.weight.copy_(T.full_like(layer.weight, 0.5))
            if hasattr(layer, 'bias') and layer.bias is not None:
                layer.bias.copy_(T.full_like(layer.bias, 0.1))

        # Handle the output layer similarly
        if 'policy_dense_output' in model.output_layer:
            output_layer = model.output_layer['policy_dense_output']
            if hasattr(output_layer, 'weight') and output_layer.weight is not None:
                output_layer.weight.copy_(T.full_like(output_layer.weight, 0.5))
            if hasattr(output_layer, 'bias') and output_layer.bias is not None:
                output_layer.bias.copy_(T.full_like(output_layer.bias, 0.1))
    return model


@pytest.mark.parametrize("policy_model", [(2, (3,), (3,), [{'type': 'dense', 'params': {'units': 1, 'kernel': 'default', 'kernel params':{}}}])], indirect=True)
def test_1d_observation_output(policy_model):
    # Now, we need to match the input shape to (num_envs, *obs_shape)
    obs = T.tensor([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
    policy_dist, logits = policy_model(obs)
    # Expected output should be (num_envs,) with the same value 
    expected_logits = T.tensor([[1.65, 1.65, 1.65], [3.9, 3.9, 3.9]]).to(policy_model.device)
    expected_dist = Categorical(logits=expected_logits)
    
    logger.info(f'logits:{logits}')
    logger.info(f'expected_logits:{expected_logits}')
    assert T.allclose(logits, expected_logits, atol=1e-5), "Output logits does not match expected"
    # Check if the distributions are correct
    assert isinstance(policy_dist, Categorical), "Policy should return a Categorical distribution"
    assert T.allclose(policy_dist.logits, expected_dist.logits, atol=1e-5), "Distribution logits does not match expected"
    # Check sampling for each environment
    samples = policy_dist.sample()
    logger.info(f'policy sample:{samples}')
    assert len(samples) == 2, "Should have 2 samples, one for each environment"
    assert all(sample.shape == T.Size([]) for sample in samples), "Each sampled action should be a scalar"
    # Check if the sampled actions are within the action space
    for sample in samples:
        assert 0 <= sample.item() < 3, "Sampled action should be within the range of the action space"

@pytest.mark.parametrize("policy_model", [(2,(5, 5), (3,), [{'type': 'conv2d', 'params': {'out_channels': 1, 'kernel_size': 5, 'stride': 1, 'padding': 0}},
                                                           {'type': 'flatten'}])], indirect=True)
def test_2d_gray_scale_output(policy_model):
    obs = T.ones(2, 1, 5, 5)  # Assuming the model expects this shape
    policy_dist, logits = policy_model(obs)
    expected_logits = T.tensor([[6.4, 6.4, 6.4],[6.4, 6.4, 6.4]]).to(policy_model.device)
    expected_dist = Categorical(logits=expected_logits)
    
    logger.info(f'logits:{logits}')
    logger.info(f'expected_logits:{expected_logits}')
    assert T.allclose(logits, expected_logits, atol=1e-5), "Output logits does not match expected"
    # Check if the distributions are correct
    assert isinstance(policy_dist, Categorical), "Policy should return a Categorical distribution"
    assert T.allclose(policy_dist.logits, expected_dist.logits, atol=1e-5), "Distribution logits does not match expected"
    # Check sampling for each environment
    samples = policy_dist.sample()
    logger.info(f'policy sample:{samples}')
    assert len(samples) == 2, "Should have 2 samples, one for each environment"
    assert all(sample.shape == T.Size([]) for sample in samples), "Each sampled action should be a scalar"
    # Check if the sampled actions are within the action space
    for sample in samples:
        assert 0 <= sample.item() < 3, "Sampled action should be within the range of the action space"

@pytest.mark.parametrize("policy_model", [(2,(4, 5, 5), (3,), [{'type': 'conv2d', 'params': {'out_channels': 1, 'kernel_size': 5, 'stride': 1, 'padding': 0}},
                                                           {'type': 'flatten'}])], indirect=True)
def test_3d_gray_scale_stacked_output(policy_model):
    obs = T.ones(2, 4, 5, 5)  # Assuming the model expects this shape
    policy_dist, logits = policy_model(obs)
    expected_logits = T.tensor([[25.15, 25.15, 25.15],[25.15, 25.15, 25.15]]).to(policy_model.device)
    expected_dist = Categorical(logits=expected_logits)
    
    logger.info(f'logits:{logits}')
    logger.info(f'expected_logits:{expected_logits}')
    assert T.allclose(logits, expected_logits, atol=1e-5), "Output logits does not match expected"
    # Check if the distributions are correct
    assert isinstance(policy_dist, Categorical), "Policy should return a Categorical distribution"
    assert T.allclose(policy_dist.logits, expected_dist.logits, atol=1e-5), "Distribution logits does not match expected"
    # Check sampling for each environment
    samples = policy_dist.sample()
    logger.info(f'policy sample:{samples}')
    assert len(samples) == 2, "Should have 2 samples, one for each environment"
    assert all(sample.shape == T.Size([]) for sample in samples), "Each sampled action should be a scalar"
    # Check if the sampled actions are within the action space
    for sample in samples:
        assert 0 <= sample.item() < 3, "Sampled action should be within the range of the action space"