"""This module holds the Agent base class and all RL agents as subclasses  It also 
provides helper functions for loading any subclass of type Agent."""

# imports
import json
import os
from typing import List
from pathlib import Path
import time
from typing import Union
# import datetime
# import inspect
# import threading
from mpi4py import MPI
# import logging
from logging_config import logger
import copy
from encoder import CustomJSONEncoder, serialize_env_spec

import rl_callbacks
import models
import cnn_models
import wandb
import wandb_support
import helper
from helper import Buffer, ReplayBuffer, SharedReplayBuffer, Normalizer, SharedNormalizer
import dash_callbacks
import gym_helper

import torch as T
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Categorical
from torch.multiprocessing import spawn, Manager
import torch.distributed as dist
import gymnasium as gym
import gymnasium_robotics as gym_robo
import numpy as np
import random
import torch.profiler


# Agent class
class Agent:
    """Base class for all RL agents."""

    def get_action(self, state):
        """Returns an action given a state."""

    def train(
        self, num_episodes, render: bool = False, render_freq: int = None, save_dir=None
    ):
        """Trains the model for 'episodes' number of episodes."""

    def learn(self):
        """Updates the model."""

    def test(self, num_episodes=None, render=False, render_freq=10):
        """Runs a test over 'num_episodes'."""

    def save(self):
        """Saves the model."""

    @classmethod
    def load(cls, folder: str = "models"):
        """Loads the model."""


class ActorCritic(Agent):
    """Actor Critic Agent."""

    def __init__(
        self,
        env: gym.Env,
        policy_model: models.PolicyModel,
        value_model: models.ValueModel,
        discount=0.99,
        policy_trace_decay: float = 0.0,
        value_trace_decay: float = 0.0,
        callbacks: list = [],
        save_dir: str = "models/",
    ):
        
        # Set the device
        device = T.device("cuda" if T.cuda.is_available() else "cpu")
        self.device = device

        self.env = env
        self.policy_model = policy_model
        self.value_model = value_model
        self.discount = discount
        self.policy_trace_decay = policy_trace_decay
        self.value_trace_decay = value_trace_decay
        self.callbacks = callbacks
        self.save_dir = save_dir

        if callbacks:
            self._train_config = {}
            self._train_episode_config = {}
            self._train_step_config = {}
            self._test_config = {}
            self._test_episode_config = {}

            for callback in self.callbacks:
                self._config = callback._config(self)
                if isinstance(callback, rl_callbacks.WandbCallback):  
                    self._wandb = True

        else:
            self.callbacks = []
            self._wandb = False

        self._step = None
        # set self.action to None
        self.action = None
        # instantiate and set policy and value traces
        self.policy_trace = []
        self.value_trace = []
        self._set_traces()

    @classmethod
    def build(
        cls,
        env,
        policy_layers,
        value_layers,
        callbacks,
        config,#: wandb.config,
        save_dir: str = "models/",
    ):
        """Builds the agent."""
        policy_optimizer = wandb.config.policy_optimizer
        value_optimizer = wandb.config.value_optimizer
        policy_learning_rate = wandb.config.learning_rate
        value_learning_rate = wandb.config.learning_rate
        policy_model = models.PolicyModel(
            env, dense_layers=policy_layers, optimizer=policy_optimizer, learning_rate=policy_learning_rate
        )
        value_model = models.ValueModel(
            env, dense_layers=value_layers, optimizer=value_optimizer, learning_rate=value_learning_rate
        )

        agent = cls(
            env,
            policy_model,
            value_model,
            config.discount,
            config.policy_trace_decay,
            config.value_trace_decay,
            callbacks,
            save_dir=save_dir,
        )

        agent.save()

        return agent

    def _initialize_env(self, render=False, render_freq=10, context=None):
        """Initializes a new environment."""
        if render:
            env = gym.make(self.env.spec, render_mode="rgb_array")
            if context == "train":
                return gym.wrappers.RecordVideo(
                    env,
                    self.save_dir + "/renders/training",
                    episode_trigger=lambda episode_id: (episode_id+1) % render_freq == 0,
                )
            elif context == "test":
                return gym.wrappers.RecordVideo(
                    env,
                    self.save_dir + "/renders/testing",
                    episode_trigger=lambda episode_id: (episode_id+1) % render_freq == 0,
                )

        return gym.make(self.env.spec)

    def _set_traces(self):
        for weights in self.policy_model.parameters():
            self.policy_trace.append(T.zeros_like(weights, device=self.device))
            #DEBUG
            print(f'policy trace shape: {weights.size()}')

        for weights in self.value_model.parameters():
            self.value_trace.append(T.zeros_like(weights, device=self.device))
            #DEBUG
            print(f'value trace shape: {weights.size()}')


    def _update_traces(self):
        with T.no_grad():
            for i, weights in enumerate(self.policy_model.parameters()):
                self.policy_trace[i] = (
                    self.discount * self.policy_trace_decay * self.policy_trace[i]
                ) + (self.influence * weights.grad)

            for i, weights in enumerate(self.value_model.parameters()):
                self.value_trace[i] = (self.discount * self.value_trace_decay * self.value_trace[i]) + weights.grad

        # log to train step
        for i, (v_trace, p_trace) in enumerate(zip(self.value_trace, self.policy_trace)):
            self._train_step_config[f"value trace {i}"] = T.histc(v_trace, bins=20)
            self._train_step_config[f"policy trace {i}"] = T.histc(p_trace, bins=20)

    def get_action(self, state):
        state =  T.from_numpy(state).to(self.device)
        logits = self.policy_model(state)
        self.probabilities = F.softmax(logits, dim=-1)
        probabilities_dist = Categorical(probs=self.probabilities)
        action = probabilities_dist.sample()
        self.log_prob = probabilities_dist.log_prob(action)
        
        return action.item()

    def train(self, num_episodes, render: bool = False, render_freq: int = None, save_dir=None, run_number=None):
        """Trains the model for 'episodes' number of episodes."""
        # set models to train mode
        self.policy_model.train()
        self.value_model.train()
        # set save_dir if not None
        if save_dir:
            self.save_dir = save_dir
        if self.callbacks:
            for callback in self.callbacks:
                if isinstance(callback, rl_callbacks.WandbCallback):
                    callback.on_train_begin((self.policy_model, self.value_model,), logs=self._config, run_number=run_number)

                else:
                    callback.on_train_begin(logs=self._config)
        reward_history = []
        self.env = self._initialize_env(render, render_freq, 'train')
        # set step counter
        self._step = 1
        # set best reward
        best_reward = self.env.reward_range[0]
        
        for i in range(num_episodes):
            if self.callbacks:
                for callback in self.callbacks:
                    callback.on_train_epoch_begin(epoch=self._step, logs=None)
            state, _ = self.env.reset()
            done = False
            episode_reward = 0
            self.influence = 1
            while not done:
                if self.callbacks:
                    for callback in self.callbacks:
                        callback.on_train_step_begin(step=self._step, logs=None)
                action = self.get_action(state)
                next_state, reward, term, trunc, _ = self.env.step(action)
                if term or trunc:
                    done = True
                episode_reward += reward
                self.learn(state, reward, next_state, done)
                if self._wandb:
                    self._train_step_config["action"] = action
                    self._train_step_config["step reward"] = reward
                    self._train_step_config["influence"] = self.influence

                if self.callbacks:
                    for callback in self.callbacks:
                        callback.on_train_step_end(step=self._step, logs=self._train_step_config)
                state = next_state
                self.influence *= self.discount
                self._step += 1
            self._train_episode_config["episode"] = i+1
            reward_history.append(episode_reward)
            self._train_episode_config["episode reward"] = episode_reward
            avg_reward = np.mean(reward_history[-100:])
            self._train_episode_config["avg reward"] = avg_reward
            # check if best reward
            if avg_reward > best_reward:
                best_reward = avg_reward
                self._train_episode_config["best"] = True
                # save model
                self.save()
            else:
                self._train_episode_config["best"] = False

            if self.callbacks:
                for callback in self.callbacks:
                    callback.on_train_epoch_end(epoch=self._step, logs=self._train_episode_config)

            print(f"episode {i+1}, score {episode_reward}, avg_score {avg_reward}")

        # close the environment
        self.env.close()
        if self.callbacks:
            for callback in self.callbacks:
                callback.on_train_end(logs=self._train_episode_config)

    def learn(self, state, reward, next_state, done):
        self.policy_model.optimizer.zero_grad()
        self.value_model.optimizer.zero_grad()

        state = T.tensor(state, device=self.device)
        reward = T.tensor(reward, device=self.device)
        next_state = T.tensor(next_state, device=self.device)
        done = T.tensor(done, device=self.device, dtype=T.float32)

        state_value = self.value_model(state)
        next_state_value = self.value_model(next_state)
        temporal_difference = (
            reward + self.discount * next_state_value * (1 - done) - state_value)
        value_loss = temporal_difference.square()

        value_loss.backward(retain_graph=True)

        policy_loss = (-self.log_prob * temporal_difference).mean()
        policy_loss.backward()

        self._update_traces()

        #copy traces to weight gradients
        with T.no_grad():
            for i, weights in enumerate(self.policy_model.parameters()):
                weights.grad = self.policy_trace[i]

            for i, weights in enumerate(self.value_model.parameters()):
                weights.grad = self.value_trace[i]

        self.value_model.optimizer.step()
        self.policy_model.optimizer.step()

        
        if self._wandb:
            self._train_step_config["temporal difference"] = temporal_difference
            for i, probability in enumerate(self.probabilities.squeeze()):
                self._train_step_config[f"probability action {i}"] = probability.item()
            self._train_step_config["log probability"] = self.log_prob.item()
            self._train_step_config["policy loss"] = policy_loss.item()
            self._train_step_config["value loss"] = value_loss.item()


    def test(self, num_episodes, render=False, render_freq=10):
        """Runs a test over 'num_episodes'."""
        # set models to eval mode
        self.policy_model.eval()
        self.value_model.eval()
        # instantiate list to store reward history
        reward_history = []
        # instantiate new environment
        self.env = self._initialize_env(render, render_freq, 'test')
        if self.callbacks:
            for callback in self.callbacks:
                callback.on_test_begin(logs=self._config)

        self._step = 1
        for i in range(num_episodes):
            if self.callbacks:
                for callback in self.callbacks:
                    callback.on_test_step_begin(step=self._step, logs=None)
            states = []
            next_states = []
            actions = []
            rewards = []
            state, _ = self.env.reset()
            done = False
            episode_reward = 0
            while not done:
                action = self.get_action(state)
                next_state, reward, term, trunc, _ = self.env.step(action)
                # store trajectories
                states.append(state)
                actions.append(action)
                next_states.append(next_state)
                rewards.append(reward)
                if term or trunc:
                    done = True
                episode_reward += reward
                state = next_state
                self._step += 1
            reward_history.append(episode_reward)
            avg_reward = np.mean(reward_history[-100:])
            self._test_episode_config["episode reward"] = episode_reward
            self._test_episode_config["avg reward"] = avg_reward
            if self.callbacks:
                for callback in self.callbacks:
                    callback.on_test_step_end(step=self._step, logs=self._test_episode_config)

            print(f"episode {i+1}, score {episode_reward}, avg_score {avg_reward}")

        if self.callbacks:
            for callback in self.callbacks:
                callback.on_test_end(logs=self._test_episode_config)
        # close the environment
        self.env.close()

    def get_config(self):
        return {
            "agent_type": self.__class__.__name__,
            "env": self.env.spec.id,
            "policy_model": self.policy_model.get_config(),
            "value_model": self.value_model.get_config(),
            "discount": self.discount,
            "policy_trace_decay": self.policy_trace_decay,
            "value_trace_decay": self.value_trace_decay,
            "callbacks": [callback.__class__.__name__ for callback in (self.callbacks or []) if callback is not None],
            "save_dir": self.save_dir
        }


    def save(self):
        """Saves the model."""
        config = self.get_config()

        # makes directory if it doesn't exist
        os.makedirs(self.save_dir, exist_ok=True)

        # writes and saves JSON file of actor critic agent config
        with open(self.save_dir + "/config.json", "w", encoding="utf-8") as f:
            json.dump(config, f)

        # saves policy and value model
        self.policy_model.save(self.save_dir)
        self.value_model.save(self.save_dir)

        # # if wandb callback, save wandb config
        # if self._wandb:
        #     for callback in self.callbacks:
        #         if isinstance(callback, rl_callbacks.WandbCallback):
        #             callback.save(self.save_dir + "/wandb_config.json")

    @classmethod
    def load(cls, config, load_weights=True):
        """Loads the model."""
        # # load reinforce agent config
        # with open(
        #     Path(folder).joinpath(Path("obj_config.json")), "r", encoding="utf-8"
        # ) as f:
        #     obj_config = json.load(f)

        # load policy model
        policy_model = models.PolicyModel.load(config['save_dir'], load_weights)
        # load value model
        value_model = models.ValueModel.load(config['save_dir'], load_weights)
        # load callbacks
        callbacks = [rl_callbacks.load(callback_info['class_name'], callback_info['config']) for callback_info in config['callbacks']]

        # return Actor-Critic agent
        agent = cls(
            gym.make(config["env"]),
            policy_model=policy_model,
            value_model=value_model,
            discount=config["discount"],
            policy_trace_decay=config["policy_trace_decay"],
            value_trace_decay=config["value_trace_decay"],
            callbacks=callbacks,
            save_dir=config["save_dir"],
        )

        return agent


class Reinforce(Agent):
    def __init__(
        self,
        env: gym.Env,
        policy_model: models.PolicyModel,
        value_model: models.ValueModel = None,
        discount=0.99,
        callbacks: List = [],
        save_dir: str = "models/",
    ):
        # Set the device
        device = T.device("cuda" if T.cuda.is_available() else "cpu")
        self.device = device

        self.env = env
        self.policy_model = policy_model
        self.value_model = value_model
        self.discount = discount
        self.callbacks = callbacks
        self.save_dir = save_dir
        
        if callbacks:
            self._train_config = {}
            self._train_episode_config = {}
            self._train_step_config = {}
            self._test_config = {}
            self._test_episode_config = {}
            
            for callback in self.callbacks:
                self._config = callback._config(self)
                if isinstance(callback, rl_callbacks.WandbCallback):  
                    self._wandb = True

        else:
            self.callbacks = []
            self._wandb = False

        self._step = None
        self._cur_learning_steps = None

    @classmethod
    def build(
        cls,
        env,
        policy_layers,
        value_layers,
        callbacks,
        config,#: wandb.config,
        save_dir: str = "models/",
    ):
        """Builds the agent."""
        policy_optimizer = config.policy_optimizer
        value_optimizer = config.value_optimizer
        policy_model = models.PolicyModel(
            env, dense_layers=policy_layers, optimizer=policy_optimizer, learning_rate=config.learning_rate
        )
        value_model = models.ValueModel(
            env, dense_layers=value_layers, optimizer=value_optimizer, learning_rate=config.learning_rate
        )

        agent = cls(
            env,
            policy_model,
            value_model,
            config.discount,
            callbacks,
            save_dir=save_dir,
        )

        agent.save()

        return agent


    def _initialize_env(self, render=False, render_freq=10, context=None):
        """Initializes a new environment."""
        if render:
            env = gym.make(self.env.spec, render_mode="rgb_array")
            if context == "train":
                return gym.wrappers.RecordVideo(
                    env,
                    self.save_dir + "/renders/training",
                    episode_trigger=lambda episode_id: (episode_id+1) % render_freq == 0,
                )
            elif context == "test":
                return gym.wrappers.RecordVideo(
                    env,
                    self.save_dir + "/renders/testing",
                    episode_trigger=lambda episode_id: (episode_id+1) % render_freq == 0,
                )

        return gym.make(self.env.spec)

    def get_return(self, rewards):
        """Compute expected returns per timestep."""

        returns = []
        discounted_sum = T.tensor(0.0, dtype=T.float32, device=self.device)
        for reward in reversed(rewards):
            discounted_sum = reward + self.discount * discounted_sum
            returns.append(discounted_sum)
        return T.tensor(returns[::-1], dtype=T.float32, device=self.device)

    def get_action(self, state):
        state =  T.from_numpy(state).to(self.device)
        logits = self.policy_model(state)
        probabilities = F.softmax(logits, dim=-1)
        probabilities_dist = Categorical(probs=probabilities)
        action = probabilities_dist.sample()
        log_prob = probabilities_dist.log_prob(action).unsqueeze(0)

        for i, probability in enumerate(probabilities.squeeze()):
            self._train_step_config[f"probability action {i}"] = probability
        
        return action, log_prob

    def learn(self, states, log_probs, rewards):
        # Clear gradients
        self.policy_model.optimizer.zero_grad()
        self.value_model.optimizer.zero_grad()

        # convert to tensors
        states = T.tensor(states, dtype=T.float32, device=self.device)
        rewards = T.tensor(rewards, dtype=T.float32, device=self.device)
        returns = self.get_return(rewards)
        
        policy_loss = 0
        value_loss = 0
        
        for t, (state, log_prob, _return, reward, step) in enumerate(
            zip(states, log_probs, returns, rewards, self._cur_learning_steps)
        ):
            if self.callbacks:
                for callback in self.callbacks:
                    callback.on_train_step_begin(step=step, logs=None)
            
            # Calculate step loss for value model if present
            if self.value_model:
                state_value = self.value_model(state)
                # calculate performance
                advantage = _return - state_value
                # calculate value loss
                value_loss += advantage**2

            policy_loss += -log_prob * advantage * self.discount**t

            # log to wandb if using wandb callback
            if self._wandb:
                self._train_step_config["temporal difference"] = advantage
                self._train_step_config["policy loss"] = policy_loss
                self._train_step_config["value loss"] = value_loss


        # Calculate gradients
        policy_loss = policy_loss
        value_loss = value_loss

        policy_loss.backward(retain_graph=True)
        value_loss.backward()

        # Update weights
        self.policy_model.optimizer.step()
        self.value_model.optimizer.step()


    def train(self, num_episodes, render: bool = False, render_freq: int = None, save_dir=None, run_number=None):
        """Trains the model for 'episodes' number of episodes."""

        # set models to train mode
        self.policy_model.train()
        self.value_model.train()

        # set save dir if provided
        if save_dir:
            self.save_dir = save_dir

        if self.callbacks:
            for callback in self.callbacks:
                if isinstance(callback, rl_callbacks.WandbCallback):
                    callback.on_train_begin((self.policy_model, self.value_model,), logs=self._config, run_number=run_number)
        
        # instantiate new environment
        self.env = self._initialize_env(render, render_freq, 'train')
        # set step counter
        self._step = 0
        # set current learning steps
        self._cur_learning_steps = []
        # set best reward
        best_reward = self.env.reward_range[0]
        # instantiate list to store reward history
        reward_history = []
        for i in range(num_episodes):
            if self.callbacks:
                for callback in self.callbacks:
                    callback.on_train_epoch_begin(epoch=self._step, logs=None)
            states = []
            next_states = []
            # actions = []
            log_probs = []
            rewards = []
            state, _ = self.env.reset()
            done = False
            episode_reward = 0
            while not done:
                if self.callbacks:
                    for callback in self.callbacks:
                        callback.on_train_step_begin(step=self._step, logs=None)
                self._cur_learning_steps.append(self._step)
                action, log_prob = self.get_action(state)
                next_state, reward, term, trunc, _ = self.env.step(action.item())
                # store trajectories
                states.append(state)
                # actions.append(action)
                log_probs.append(log_prob)
                next_states.append(next_state)
                rewards.append(reward)
                if term or trunc:
                    done = True
                episode_reward += reward
                state = next_state
                
                # log to train log
                self._train_step_config["action"] = action
                self._train_step_config["step reward"] = reward
                self._train_step_config["log probability"] = log_prob

                if self.callbacks:
                    for callback in self.callbacks:
                        callback.on_train_step_end(step=self._step, logs=self._train_step_config)
                if not done:
                    self._step += 1
                
            self.learn(states, log_probs, rewards)
            # clear learning steps array
            self._cur_learning_steps = []
            reward_history.append(episode_reward)
            avg_reward = np.mean(reward_history[-100:])

            self._train_episode_config["episode"] = i
            self._train_episode_config["episode reward"] = episode_reward
            self._train_episode_config["avg reward"] = avg_reward
            # check if best reward
            if avg_reward > best_reward:
                best_reward = avg_reward
                self._train_episode_config["best"] = True
                # save model
                self.save()
            else:
                self._train_episode_config["best"] = False

            if self.callbacks:
                for callback in self.callbacks:
                    callback.on_train_epoch_end(
                    epoch=self._step, logs=self._train_episode_config
                )

            print(f"episode {i+1}, score {episode_reward}, avg_score {avg_reward}")

        if self.callbacks:
            for callback in self.callbacks:
                callback.on_train_end(logs=self._train_episode_config)
        # close the environment
        self.env.close()

    def test(self, num_episodes, render=False, render_freq=10):
        """Runs a test over 'num_episodes'."""

        # Set models to eval mode
        self.policy_model.eval()
        self.value_model.eval()
        # instantiate list to store reward history
        reward_history = []
        # instantiate new environment
        self.env = self._initialize_env(render, render_freq, 'test')
        if self.callbacks:
            for callback in self.callbacks:
                callback.on_test_begin(logs=None)
        self._step = 1
        for i in range(num_episodes):
            if self.callbacks:
                for callback in self.callbacks:
                    callback.on_test_step_begin(step=self._step, logs=None)
            states = []
            next_states = []
            actions = []
            rewards = []
            state, _ = self.env.reset()
            done = False
            episode_reward = 0
            while not done:
                action, _ = self.get_action(state)
                next_state, reward, term, trunc, _ = self.env.step(action.item())
                # store trajectories
                states.append(state)
                actions.append(action)
                next_states.append(next_state)
                rewards.append(reward)
                if term or trunc:
                    done = True
                episode_reward += reward
                state = next_state
                self._step += 1
            reward_history.append(episode_reward)
            avg_reward = np.mean(reward_history[-100:])
            self._test_episode_config["episode reward"] = episode_reward
            self._test_episode_config["avg reward"] = avg_reward
            if self.callbacks:
                for callback in self.callbacks:
                    callback.on_test_step_end(
                    step=self._step, logs=self._test_episode_config
                )

            print(f"episode {i+1}, score {episode_reward}, avg_score {avg_reward}")

        if self.callbacks:
            for callback in self.callbacks:
                callback.on_test_end(logs=self._test_episode_config)
        # close the environment
        self.env.close()

    def get_config(self):
        return {
            "agent_type": self.__class__.__name__,
            "env": self.env.spec.id,
            "policy_model": self.policy_model.get_config(),
            "value_model": self.value_model.get_config(),
            "discount": self.discount,
            "callbacks": [callback.get_config() for callback in self.callbacks if self.callbacks is not None],
            "save_dir": self.save_dir
        }

    def save(self):
        """Saves the model."""
        config = self.get_config()

        # makes directory if it doesn't exist
        os.makedirs(self.save_dir, exist_ok=True)

        # writes and saves JSON file of reinforce agent config
        with open(self.save_dir + "/config.json", "w", encoding="utf-8") as f:
            json.dump(config, f)

        # saves policy and value model
        self.policy_model.save(self.save_dir)
        if self.value_model:
            self.value_model.save(self.save_dir)

        # if wandb callback, save wandb config
        # if self._wandb:
        #     for callback in self.callbacks:
        #         if isinstance(callback, rl_callbacks.WandbCallback):
        #             callback.save(self.save_dir + "/wandb_config.json")

    @classmethod
    def load(cls, config):
        """Loads the model."""
        # # load reinforce agent config
        # with open(
        #     Path(folder).joinpath(Path("obj_config.json")), "r", encoding="utf-8"
        # ) as f:
        #     obj_config = json.load(f)

        # load policy model
        policy_model = models.PolicyModel.load(config['save_dir'])
        # load value model
        value_model = models.ValueModel.load(config['save_dir'])
        # load callbacks
        callbacks = [rl_callbacks.load(callback_info['class_name'], callback_info['config']) for callback_info in config['callbacks']]

        # return reinforce agent
        agent = cls(
            gym.make(config["env"]),
            policy_model=policy_model,
            value_model=value_model,
            discount=config["discount"],
            callbacks=callbacks,
            save_dir=config["save_dir"],
        )

        return agent
    

class DDPG(Agent):
    """Deep Deterministic Policy Gradient Agent."""

    def __init__(
        self,
        env: gym.Env,
        actor_model: models.ActorModel,
        critic_model: models.CriticModel,
        discount=0.99,
        tau=0.001,
        action_epsilon: float = 0.0,
        replay_buffer: helper.ReplayBuffer = None,
        batch_size: int = 64,
        noise = None,
        normalize_inputs: bool = False,
        # normalize_kwargs: dict = {},
        normalizer_clip:float=None,
        normalizer_eps:float=0.01,
        warmup:int=1000,
        callbacks: List = [],
        use_mpi = False,
        save_dir: str = "models",
        comm = None,
        device = 'cuda'
    ):
        try:
            self.use_mpi = use_mpi
            if self.use_mpi:
                if comm is not None:
                    self.comm = comm
                    self.rank = comm.Get_rank()
                else:
                    self.comm = MPI.COMM_WORLD
                    self.rank = MPI.COMM_WORLD.Get_rank()
                    logger.error(f'rank:{self.rank}')
                self.group = self.comm.Get_name()
            self.env = env
            self.actor_model = actor_model
            self.critic_model = critic_model
            # set target actor and critic models
            self.target_actor_model = self.clone_model(actor_model)
            self.target_critic_model = self.clone_model(critic_model)
            self.discount = discount
            self.tau = tau
            self.action_epsilon = action_epsilon
            self.replay_buffer = replay_buffer
            self.batch_size = batch_size
            self.noise = noise
            self.normalize_inputs = normalize_inputs
            # self.normalize_kwargs = normalize_kwargs
            self.normalizer_clip = normalizer_clip
            self.normalizer_eps = normalizer_eps
            self.warmup = warmup
            self.device = device
            # logger.debug(f"rank {self.rank} DDPG init attributes set")
        except Exception as e:
            if self.use_mpi:
                logger.error(f"rank {self.rank} Error in DDPG init: {e}", exc_info=True)
            else:
                logger.error(f"Error in DDPG init: {e}", exc_info=True)
        
        # set internal attributes
        try:
            if isinstance(env.observation_space, gym.spaces.dict.Dict):
                self._obs_space_shape = env.observation_space['observation'].shape
            else:
                self._obs_space_shape = env.observation_space.shape

            if self.normalize_inputs:
                self.state_normalizer = helper.Normalizer(self._obs_space_shape, self.normalizer_eps, self.normalizer_clip, self.device)
            
            # self.save_dir = save_dir + "/ddpg/"
            if save_dir is not None and "/ddpg/" not in save_dir:
                    self.save_dir = save_dir + "/ddpg/"
            elif save_dir is not None and "/ddpg/" in save_dir:
                    self.save_dir = save_dir

            # instantiate internal attribute use_her to be switched by HER class if using DDPG
            self._use_her = False
            # logger.debug(f"rank {self.rank} DDPG init: internal attributes set")
        except Exception as e:
            if self.use_mpi:
                logger.error(f"rank {self.rank} Error in DDPG init internal attributes: {e}", exc_info=True)
            else:
                logger.error(f"Error in DDPG init internal attributes: {e}", exc_info=True)

        # Set callbacks
        try:
            self.callbacks = callbacks
            if callbacks:
                for callback in self.callbacks:
                    self._config = callback._config(self)
                    if isinstance(callback, rl_callbacks.WandbCallback):  
                        self._wandb = True

            else:
                self.callback_list = None
                self._wandb = False
            # logger.debug(f"rank {self.rank} DDPG init: callbacks set")
        except Exception as e:
            if self.use_mpi:
                logger.error(f"rank {self.rank} Error in DDPG init set callbacks: {e}", exc_info=True)
            else:
                logger.error(f"Error in DDPG init set callbacks: {e}", exc_info=True)
        self._train_config = {}
        self._train_episode_config = {}
        self._train_step_config = {}
        self._test_config = {}
        self._test_episode_config = {}

        self._step = None

    def clone(self):
        env = gym.make(self.env.spec)
        actor = self.clone_model(self.actor_model)
        critic = self.clone_model(self.critic_model)
        replay_buffer = self.replay_buffer.clone()
        noise = self.noise.clone()

        return DDPG(
            env,
            actor,
            critic,
            self.discount,
            self.tau,
            self.action_epsilon,
            replay_buffer,
            self.batch_size,
            noise,
            self.normalize_inputs,
            # self.normalize_kwargs,
            self.normalizer_clip,
            self.normalizer_eps,
            comm = self.comm,
            device = self.device
        )
        
    
    def clone_model(self, model):
        """Clones a model."""
        return model.get_clone()
    
    @classmethod
    def build(
        cls,
        env,
        actor_cnn_layers,
        critic_cnn_layers,
        actor_layers,
        critic_state_layers,
        critic_merged_layers,
        kernels,
        callbacks,
        config,#: wandb.config,
        save_dir: str = "models/",
    ):
        """Builds the agent."""
        # Actor
        actor_learning_rate=config[config.model_type][f"{config.model_type}_actor_learning_rate"]
        actor_optimizer = config[config.model_type][f"{config.model_type}_actor_optimizer"]
        # get optimizer params
        actor_optimizer_params = {}
        if actor_optimizer == "Adam":
            actor_optimizer_params['weight_decay'] = \
                config[config.model_type][f"{config.model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_weight_decay']
        
        elif actor_optimizer == "Adagrad":
            actor_optimizer_params['weight_decay'] = \
                config[config.model_type][f"{config.model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_weight_decay']
            actor_optimizer_params['lr_decay'] = \
                config[config.model_type][f"{config.model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_lr_decay']
        
        elif actor_optimizer == "RMSprop" or actor_optimizer == "SGD":
            actor_optimizer_params['weight_decay'] = \
                config[config.model_type][f"{config.model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_weight_decay']
            actor_optimizer_params['momentum'] = \
                config[config.model_type][f"{config.model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_momentum']

        actor_normalize_layers = config[config.model_type][f"{config.model_type}_actor_normalize_layers"]

        # Critic
        critic_learning_rate=config[config.model_type][f"{config.model_type}_critic_learning_rate"]
        critic_optimizer = config[config.model_type][f"{config.model_type}_critic_optimizer"]
        critic_optimizer_params = {}
        if critic_optimizer == "Adam":
            critic_optimizer_params['weight_decay'] = \
                config[config.model_type][f"{config.model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_weight_decay']
        
        elif critic_optimizer == "Adagrad":
            critic_optimizer_params['weight_decay'] = \
                config[config.model_type][f"{config.model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_weight_decay']
            critic_optimizer_params['lr_decay'] = \
                config[config.model_type][f"{config.model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_lr_decay']
        
        elif critic_optimizer == "RMSprop" or critic_optimizer == "SGD":
            critic_optimizer_params['weight_decay'] = \
                config[config.model_type][f"{config.model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_weight_decay']
            critic_optimizer_params['momentum'] = \
                config[config.model_type][f"{config.model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_momentum']
        
        critic_normalize_layers = config[config.model_type][f"{config.model_type}_critic_normalize_layers"]

        # Check if CNN layers and if so, build CNN model
        if actor_cnn_layers:
            actor_cnn_model = cnn_models.CNN(actor_cnn_layers, env)
        else:
            actor_cnn_model = None

        if critic_cnn_layers:
            critic_cnn_model = cnn_models.CNN(critic_cnn_layers, env)
        else:
            critic_cnn_model = None

        # Set device
        device = config[config.model_type][f"{config.model_type}_device"]

        # get desired, achieved, reward func for env
        desired_goal_func, achieved_goal_func, reward_func = gym_helper.get_her_goal_functions(env)
        goal_shape = desired_goal_func(env).shape

        # Get actor clamp value
        # clamp_output = config[config.model_type][f"{config.model_type}_actor_clamp_output"]
        
        actor_model = models.ActorModel(env = env,
                                        cnn_model = actor_cnn_model,
                                        dense_layers = actor_layers,
                                        output_layer_kernel=kernels[f'actor_output_kernel'],
                                        goal_shape=goal_shape,
                                        optimizer = actor_optimizer,
                                        optimizer_params = actor_optimizer_params,
                                        learning_rate = actor_learning_rate,
                                        normalize_layers = actor_normalize_layers,
                                        # clamp_output=clamp_output,
                                        device=device,
        )
        critic_model = models.CriticModel(env = env,
                                          cnn_model = critic_cnn_model,
                                          state_layers = critic_state_layers,
                                          merged_layers = critic_merged_layers,
                                          output_layer_kernel=kernels[f'critic_output_kernel'],
                                          goal_shape=goal_shape,
                                          optimizer = critic_optimizer,
                                          optimizer_params = critic_optimizer_params,
                                          learning_rate = critic_learning_rate,
                                          normalize_layers = critic_normalize_layers,
                                          device=device,
        )

        # action epsilon
        action_epsilon = config[config.model_type][f"{config.model_type}_epsilon_greedy"]

        # normalize inputs
        normalize_inputs = config[config.model_type][f"{config.model_type}_normalize_input"]
        # normalize_kwargs = {}
        if "True" in normalize_inputs:
            # normalize_kwargs = config[config.model_type][f"{config.model_type}_normalize_clip"]
            normalizer_clip = config[config.model_type][f"{config.model_type}_normalize_clip"]

        agent = cls(
            env = env,
            actor_model = actor_model,
            critic_model = critic_model,
            discount = config[config.model_type][f"{config.model_type}_discount"],
            tau = config[config.model_type][f"{config.model_type}_tau"],
            action_epsilon = action_epsilon,
            replay_buffer = helper.ReplayBuffer(env=env),
            batch_size = config[config.model_type][f"{config.model_type}_batch_size"],
            noise = helper.Noise.create_instance(config[config.model_type][f"{config.model_type}_noise"], shape=env.action_space.shape, **config[config.model_type][f"{config.model_type}_noise_{config[config.model_type][f'{config.model_type}_noise']}"]),
            normalize_inputs = normalize_inputs,
            # normalize_kwargs = normalize_kwargs,
            normalizer_clip = normalizer_clip,
            callbacks = callbacks,
            save_dir = save_dir,
        )

        agent.save()

        return agent

    def _init_her(self):
            # self.normalize_inputs = True
            self._use_her = True
            # self.state_normalizer = helper.SharedNormalizer(size=self._obs_space_shape, eps=eps, clip_range=clip_range)
            # self.goal_normalizer = helper.SharedNormalizer(size=goal_shape, eps=eps, clip_range=clip_range)
            # set clamp for targets
            # self.target_clamp = 1 / (1 - self.discount)

    def _initialize_env(self, render=False, render_freq=10, context=None):
        """Initializes a new environment."""
        # logger.debug(f"rank {self.rank} DDPG._initialize_env called")
        try:
            if render:
                env = gym.make(self.env.spec, render_mode="rgb_array")
                # logger.debug(f"rank {self.rank} DDPG._initialize_env: env built with rendering")
                if context == "train":
                    os.makedirs(self.save_dir + "/renders/training", exist_ok=True)
                    return gym.wrappers.RecordVideo(
                        env,
                        self.save_dir + "/renders/training",
                        episode_trigger=lambda episode_id: (episode_id+1) % render_freq == 0,
                    )
                elif context == "test":
                    os.makedirs(self.save_dir + "/renders/testing", exist_ok=True)
                    return gym.wrappers.RecordVideo(
                        env,
                        self.save_dir + "/renders/testing",
                        episode_trigger=lambda episode_id: (episode_id+1) % render_freq == 0,
                    )
                else:
                    logger.warning(f"rank {self.rank} Unknown context: {context}, environment will not be recorded")

            return gym.make(self.env.spec)
        except Exception as e:
            logger.error(f"rank {self.rank} Error in DDPG._initialize_env: {e}", exc_info=True)
            raise

    
    def get_action(self, state, goal=None, grad=True, test=False,
                   state_normalizer:SharedNormalizer=None,
                   goal_normalizer:SharedNormalizer=None):
        
        # print('state')
        # print(state)
        # print('goal')
        # print(goal)
        # print('state normalizer')
        # print(state_normalizer.get_config())
        # print('goal normalizer')
        # print(goal_normalizer.get_config())

        # make sure state is a tensor and on correct device
        state = T.tensor(state, dtype=T.float32, device=self.actor_model.device)

        # check if get action is for testing
        if test:
            # print(f'action test fired')
            with T.no_grad():
                # print('no grad fired')
                # normalize state if self.normalize_inputs
                if self.normalize_inputs:
                    state = self.state_normalizer.normalize(state)
                # (HER) else if using HER, normalize using passed normalizer
                elif self._use_her:
                    #DEBUG
                    print('used passed state normalizer')
                    state = state_normalizer.normalize(state)

                # (HER) normalize goal if self._use_her using passed normalizer
                if self._use_her:
                    goal = goal_normalizer.normalize(goal)
                    # make sure goal is a tensor and on correct device
                    goal = T.tensor(goal, dtype=T.float32, device=self.actor_model.device)
                    #DEBUG
                    print('used passed goal normalizer')
                
                # permute state to (C,H,W) if actor using cnn model
                if self.actor_model.cnn_model:
                    state = state.permute(2, 0, 1).unsqueeze(0)

                # get action
                # _, action = self.actor_model(state, goal)
                _, action = self.target_actor_model(state, goal) # use target network for testing
                # transfer action to cpu, detach from any graphs, tranform to numpy, and flatten
                action_np = action.cpu().detach().numpy().flatten()
        
        # check if using epsilon greedy
        else: #self.action_epsilon > 0.0:
            # print('action train fired')
            # if random number is less than epsilon, sample random action
            if np.random.random() < self.action_epsilon:
                # print('epsilon greedy action')
                action_np = self.env.action_space.sample()
                noise_np = np.zeros_like(action_np)
            
            else:
                # if gradient tracking is true
                if grad:
                    # print('with grad fired')
                    # normalize state if self.normalize_inputs
                    if self.normalize_inputs:
                        state = self.state_normalizer.normalize(state)
                    # (HER) use passed state normalizer if using HER
                    elif self._use_her:
                        state = state_normalizer.normalize(state)
                    
                    # (HER) normalize goal if self._use_her using passed normalizer
                    if self._use_her:
                        goal = goal_normalizer.normalize(goal)
                        # print(f'normalized goal: {goal}')
                        # make sure goal is a tensor and on correct device
                        goal = T.tensor(goal, dtype=T.float32, device=self.actor_model.device)

                    # permute state to (C,H,W) if actor using cnn model
                    if self.actor_model.cnn_model:
                        state = state.permute(2, 0, 1).unsqueeze(0)

                    noise = self.noise()
                    # print(f'noise: {noise}')

                    # Check if in warmup
                    if self._step <= self.warmup:
                        action = T.tensor(self.env.action_space.sample(), dtype=T.float32, device=self.actor_model.device)

                    else:
                        _, pi = self.actor_model(state, goal)
                        # print(f'pi: {pi}')

                        # Convert the action space bounds to a tensor on the same device
                        action_space_high = T.tensor(self.env.action_space.high, dtype=T.float32, device=self.actor_model.device)
                        action_space_low = T.tensor(self.env.action_space.low, dtype=T.float32, device=self.actor_model.device)
                        # print(f'actor model device:{self.actor_model.device}')
                        action = (pi + noise).clip(action_space_low, action_space_high)
                        # print(f'action + noise: {action}')

                    noise_np = noise.cpu().detach().numpy().flatten()
                    action_np = action.cpu().detach().numpy().flatten()
                    # print(f'action np: {action_np}')

                else:
                    with T.no_grad():
                        # print('without grad fired')
                        # normalize state if self.normalize_inputs
                        if self.normalize_inputs:
                            state = self.state_normalizer.normalize(state)
                        # (HER) use passed state normalizer if using HER
                        elif self._use_her:
                            state = state_normalizer.normalize(state)

                        # normalize goal if self._use_her
                        if self._use_her:
                            goal = goal_normalizer.normalize(goal)
                            # make sure goal is a tensor and on correct device
                            goal = T.tensor(goal, dtype=T.float32, device=self.actor_model.device)
                        
                        # permute state to (C,H,W) if actor using cnn model
                        if self.actor_model.cnn_model:
                            state = state.permute(2, 0, 1).unsqueeze(0)

                        noise = self.noise()

                        # Check if in warmup
                        if self._step <= self.warmup:
                            action = T.tensor(self.env.action_space.sample(), dtype=T.float32, device=self.actor_model.device)

                        else:
                            _, pi = self.actor_model(state, goal)

                            # Convert the action space bounds to a tensor on the same device
                            action_space_high = T.tensor(self.env.action_space.high, dtype=T.float32, device=self.actor_model.device)
                            action_space_low = T.tensor(self.env.action_space.low, dtype=T.float32, device=self.actor_model.device)

                            action = (pi + noise).clip(action_space_low, action_space_high)

                        noise_np = noise.cpu().detach().numpy().flatten()
                        action_np = action.cpu().detach().numpy().flatten()

        if test:
            # loop over all actions to log to wandb
            for i, a in enumerate(action_np):
                # Log the values to wandb
                self._train_step_config[f'action_{i}'] = a

        else:
            # Loop over the noise and action values and log them to wandb
            for i, (a,n) in enumerate(zip(action_np, noise_np)):
                # Log the values to wandb
                self._train_step_config[f'action_{i}'] = a
                self._train_step_config[f'noise_{i}'] = n
        
        # print(f'pi: {pi}; noise: {noise}; action_np: {action_np}')

        return action_np


    def learn(self, replay_buffer:Buffer=None,
              state_normalizer:Union[Normalizer, SharedNormalizer]=None,
              goal_normalizer:Union[Normalizer, SharedNormalizer]=None,
              ):

        # sample a batch of experiences from the replay buffer
        if self._use_her: # if using HER
            states, actions, rewards, next_states, dones, achieved_goals, next_achieved_goals, desired_goals = replay_buffer.sample(self.batch_size)
        else:
            states, actions, rewards, next_states, dones = self.replay_buffer.sample(self.batch_size)
        
        # normalize states if self.normalize_inputs
        if self.normalize_inputs:
            states = self.state_normalizer.normalize(states)
            next_states = self.state_normalizer.normalize(next_states)

        # (HER) Use passed normalizers to normalize states and goals
        if self._use_her:
            states = state_normalizer.normalize(states)
            next_states = state_normalizer.normalize(next_states)
            desired_goals = goal_normalizer.normalize(desired_goals)
        
        # Convert to tensors
        # states = T.tensor(states, dtype=T.float32, device=self.actor_model.device)
        # actions = T.tensor(actions, dtype=T.float32, device=self.actor_model.device)
        # rewards = T.tensor(rewards, dtype=T.float32, device=self.actor_model.device)
        # next_states = T.tensor(next_states, dtype=T.float32, device=self.actor_model.device)
        # dones = T.tensor(dones, dtype=T.int8, device=self.actor_model.device)
        # if using HER, convert desired goals to tensors
        # if self._use_her:
        #     desired_goals = T.tensor(desired_goals, dtype=T.float32, device=self.actor_model.device)
        # else:
        #     # set desired goals to None
        #     desired_goals = None
        if not self._use_her:
            desired_goals = None

        # permute states and next states if using cnn
        if self.actor_model.cnn_model:
            states = states.permute(0, 3, 1, 2)
            next_states = next_states.permute(0, 3, 1, 2)

        # convert rewards and dones to 2d tensors
        rewards = rewards.unsqueeze(1)
        dones = dones.unsqueeze(1)

        # get target values 
        _, target_actions = self.target_actor_model(next_states, desired_goals)
        # print(f'Agent {dist.get_rank()}: target_actions: {target_actions}')
        target_critic_values = self.target_critic_model(next_states, target_actions, desired_goals)
        # print(f'Agent {dist.get_rank()}: target_critic_values: {target_critic_values}')
        targets = rewards + (1 - dones) * self.discount * target_critic_values

        if self._use_her:
            targets = T.clamp(targets, min=-1/(1-self.discount), max=0)

        # get current critic values and calculate critic loss
        prediction = self.critic_model(states, actions, desired_goals)
        critic_loss = F.mse_loss(prediction, targets)
        
        # update critic
        self.critic_model.optimizer.zero_grad()
        critic_loss.backward()
        # if self._use_her:
            # print(f'agent {MPI.COMM_WORLD.Get_rank()} reached critic optimization')
            # Synchronize gradients
            ## T.DIST CUDA ##
            # print(f'agent {MPI.COMM_WORLD.Get_rank()} param grad before all reduce:')
            # for param in self.critic_model.parameters():
            #     if param.grad is not None:
            #         # print(f'agent {MPI.COMM_WORLD.Get_rank()} param shape: {param.shape}')
            #         print(param.grad)
            #         dist.all_reduce(param.grad.data, op=dist.ReduceOp.SUM)
                    # print(f'agent {dist.get_rank()} param grad after all reduce:')
                    # print(param.grad)
            #         # param.grad.data /= dist.get_world_size()
            #         # print(f'agent {dist.get_rank()} param grad after divide by world size')
            #         # print(param.grad)

        ## MPI CPU ##
        if self.use_mpi:
            helper.sync_grads_sum(self.critic_model, self.comm)
        self.critic_model.optimizer.step()

        # update actor
        pre_act_values, action_values = self.actor_model(states, desired_goals)
        # print(f'Agent {dist.get_rank()}: action_values: {action_values}')
        critic_values = self.critic_model(states, action_values, desired_goals)
        # print(f'Agent {dist.get_rank()}: critic_values: {critic_values}')
        actor_loss = -critic_values.mean()
        if self._use_her:
            actor_loss += pre_act_values.pow(2).mean()

        self.actor_model.optimizer.zero_grad()
        actor_loss.backward()
        # if self._use_her:
            # Synchronize Gradients
            ## T.DIST CUDA ##
            # print(f'agent {MPI.COMM_WORLD.Get_rank()} reached actor optimization')
            # print(f'agent {MPI.COMM_WORLD.Get_rank()} param grad before all reduce:')
            # for param in self.actor_model.parameters():
            #     if param.grad is not None:
                    # print(f'agent {MPI.COMM_WORLD.Get_rank()} param shape: {param.shape}')
                    # print(param.grad)
            #         dist.all_reduce(param.grad.data, op=dist.ReduceOp.SUM)
            #         # print(f'agent {dist.get_rank()} param grad after reduce:')
            #         # print(param.grad)
            #         # print(f'agent {dist.get_rank()} world size {dist.get_world_size()}')
            #         param.grad.data /= dist.get_world_size()
            #         # print(f'agent {dist.get_rank()} param grad after divide by world size')
            #         # print(param.grad)

        ## MPI CPU ##
        if self.use_mpi:
            helper.sync_grads_sum(self.actor_model, self.comm)
        self.actor_model.optimizer.step()

        # add metrics to step_logs
        self._train_step_config['actor_predictions'] = action_values.mean()
        self._train_step_config['critic_predictions'] = critic_values.mean()
        self._train_step_config['target_actor_predictions'] = target_actions.mean()
        self._train_step_config['target_critic_predictions'] = target_critic_values.mean()
        
        return actor_loss.item(), critic_loss.item()
        
    
    def soft_update(self, current, target):
        with T.no_grad():
            for current_params, target_params in zip(current.parameters(), target.parameters()):
                target_params.data.copy_(self.tau * current_params.data + (1 - self.tau) * target_params.data)

    @classmethod
    def sweep_train(
        cls,
        config, # wandb.config,
        train_config,
        env_spec,
        callbacks,
        run_number,
        comm=None,
    ):
        """Builds and trains agents from sweep configs. Works with MPI"""
        rank = MPI.COMM_WORLD.rank

        if comm is not None:
            logger.debug(f"Rank {rank} comm detected")
            rank = comm.Get_rank()
            logger.debug(f"Global rank {MPI.COMM_WORLD.Get_rank()} in {comm.Get_name()} set to comm rank {rank}")
            logger.debug(f"init_sweep fired: global rank {MPI.COMM_WORLD.rank}, group rank {rank}, {comm.Get_name()}")
        else:
            logger.debug(f"init_sweep fired")
        try:
            # rank = MPI.COMM_WORLD.rank
            # Instantiate env from env_spec
            env = gym.make(gym.envs.registration.EnvSpec.from_json(env_spec))
            # agent_config_path = f'sweep/agent_config_{run_number}.json'
            # logger.debug(f"rank {rank} agent config path: {agent_config_path}")
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} train config: {train_config}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} env spec id: {env.spec.id}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} callbacks: {callbacks}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} run number: {run_number}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} config set: {config}")
            else:
                logger.debug(f"train config: {train_config}")
                logger.debug(f"env spec id: {env.spec.id}")
                logger.debug(f"callbacks: {callbacks}")
                logger.debug(f"run number: {run_number}")
                logger.debug(f"config set: {config}")
            model_type = list(config.keys())[0]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} model type: {model_type}")
            else:
                logger.debug(f"model type: {model_type}")

            actor_cnn_layers, critic_cnn_layers, actor_layers, critic_state_layers, critic_merged_layers, kernels = wandb_support.build_layers(config)
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} layers built")
            else:
                logger.debug(f"layers built")
            # Actor
            actor_learning_rate=config[model_type][f"{model_type}_actor_learning_rate"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} actor learning rate set")
            else:
                logger.debug(f"actor learning rate set")
            actor_optimizer = config[model_type][f"{model_type}_actor_optimizer"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} actor optimizer set")
            else:
                logger.debug(f"actor optimizer set")
            # get optimizer params
            actor_optimizer_params = {}
            if actor_optimizer == "Adam":
                actor_optimizer_params['weight_decay'] = \
                    config[model_type][f"{model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_weight_decay']
            
            elif actor_optimizer == "Adagrad":
                actor_optimizer_params['weight_decay'] = \
                    config[model_type][f"{model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_weight_decay']
                actor_optimizer_params['lr_decay'] = \
                    config[model_type][f"{model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_lr_decay']
            
            elif actor_optimizer == "RMSprop" or actor_optimizer == "SGD":
                actor_optimizer_params['weight_decay'] = \
                    config[model_type][f"{model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_weight_decay']
                actor_optimizer_params['momentum'] = \
                    config[model_type][f"{model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_momentum']

            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} actor optimizer params set")
            else:
                logger.debug(f"actor optimizer params set")
            actor_normalize_layers = config[model_type][f"{model_type}_actor_normalize_layers"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} actor normalize layers set")
            else:
                logger.debug(f"actor normalize layers set")
            # Critic
            critic_learning_rate=config[model_type][f"{model_type}_critic_learning_rate"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} critic learning rate set")
            else:
                logger.debug(f"critic learning rate set")
            critic_optimizer = config[model_type][f"{model_type}_critic_optimizer"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} critic optimizer set")
            else:
                logger.debug(f"critic optimizer set")
            critic_optimizer_params = {}
            if critic_optimizer == "Adam":
                critic_optimizer_params['weight_decay'] = \
                    config[model_type][f"{model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_weight_decay']
            
            elif critic_optimizer == "Adagrad":
                critic_optimizer_params['weight_decay'] = \
                    config[model_type][f"{model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_weight_decay']
                critic_optimizer_params['lr_decay'] = \
                    config[model_type][f"{model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_lr_decay']
            
            elif critic_optimizer == "RMSprop" or critic_optimizer == "SGD":
                critic_optimizer_params['weight_decay'] = \
                    config[model_type][f"{model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_weight_decay']
                critic_optimizer_params['momentum'] = \
                    config[model_type][f"{model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_momentum']
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} critic optimizer params set")
            else:
                logger.debug(f"critic optimizer params set")

            critic_normalize_layers = config[model_type][f"{model_type}_critic_normalize_layers"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} critic normalize layers set")
            else:
                logger.debug(f"critic normalize layers set")
            # Set device
            device = config[model_type][f"{model_type}_device"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} device set")
            else:
                logger.debug(f"device set")
            # Check if CNN layers and if so, build CNN model
            if actor_cnn_layers:
                actor_cnn_model = cnn_models.CNN(actor_cnn_layers, env)
            else:
                actor_cnn_model = None
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} actor cnn layers set: {actor_cnn_layers}")
            else:
                logger.debug(f"actor cnn layers set: {actor_cnn_layers}")

            if critic_cnn_layers:
                critic_cnn_model = cnn_models.CNN(critic_cnn_layers, env)
            else:
                critic_cnn_model = None
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} critic cnn layers set: {critic_cnn_layers}")
            else:
                logger.debug(f"critic cnn layers set: {critic_cnn_layers}")
            # Get actor clamp value
            # clamp_output = config[model_type][f"{model_type}_actor_clamp_output"]
            # if comm is not None:
            #     logger.debug(f"{comm.Get_name()}; Rank {rank} clamp output set: {clamp_output}")
            # else:
            #     logger.debug(f"clamp output set: {clamp_output}")
            actor_model = models.ActorModel(env = env,
                                            cnn_model = actor_cnn_model,
                                            dense_layers = actor_layers,
                                            output_layer_kernel=kernels[f'actor_output_kernel'],
                                            optimizer = actor_optimizer,
                                            optimizer_params = actor_optimizer_params,
                                            learning_rate = actor_learning_rate,
                                            normalize_layers = actor_normalize_layers,
                                            # clamp_output=clamp_output,
                                            device=device,
            )
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} actor model built: {actor_model.get_config()}")
            else:
                logger.debug(f"actor model built: {actor_model.get_config()}")
            critic_model = models.CriticModel(env = env,
                                            cnn_model = critic_cnn_model,
                                            state_layers = critic_state_layers,
                                            merged_layers = critic_merged_layers,
                                            output_layer_kernel=kernels[f'critic_output_kernel'],
                                            optimizer = critic_optimizer,
                                            optimizer_params = critic_optimizer_params,
                                            learning_rate = critic_learning_rate,
                                            normalize_layers = critic_normalize_layers,
                                            device=device,
            )
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} critic model built: {critic_model.get_config()}")
            else:
                logger.debug(f"critic model built: {critic_model.get_config()}")
            # get normalizer clip value
            normalizer_clip = config[model_type][f"{model_type}_normalizer_clip"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} normalizer clip set: {normalizer_clip}")
            else:
                logger.debug(f"normalizer clip set: {normalizer_clip}")
            # get action epsilon
            action_epsilon = config[model_type][f"{model_type}_epsilon_greedy"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} action epsilon set: {action_epsilon}")
            else:
                logger.debug(f"action epsilon set: {action_epsilon}")
            # Replay buffer size
            replay_buffer_size = config[model_type][f"{model_type}_replay_buffer_size"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} replay buffer size set: {replay_buffer_size}")
            else:
                logger.debug(f"replay buffer size set: {replay_buffer_size}")
            # Save dir
            save_dir = config[model_type][f"{model_type}_save_dir"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} save dir set: {save_dir}")
            else:
                logger.debug(f"save dir set: {save_dir}")

            # create replay buffer
            replay_buffer = ReplayBuffer(env, replay_buffer_size, device=device)
            # create DDPG agent
            ddpg_agent= cls(
                env = env,
                actor_model = actor_model,
                critic_model = critic_model,
                discount = config[model_type][f"{model_type}_discount"],
                tau = config[model_type][f"{model_type}_tau"],
                action_epsilon = action_epsilon,
                replay_buffer = replay_buffer,
                batch_size = config[model_type][f"{model_type}_batch_size"],
                noise = helper.Noise.create_instance(config[model_type][f"{model_type}_noise"], shape=env.action_space.shape, **config[model_type][f"{model_type}_noise_{config[model_type][f'{model_type}_noise']}"], device=device),
                warmup = config[model_type][f"{model_type}_warmup"],
                callbacks = callbacks,
                comm = comm,
                device = device,
            )
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} ddpg agent built: {ddpg_agent.get_config()}")
            else:
                logger.debug(f"ddpg agent built: {ddpg_agent.get_config()}")

            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} train barrier called")
            else:
                logger.debug(f"train barrier called")

            if comm is not None:
                comm.Barrier()
                logger.debug(f"{comm.Get_name()}; Rank {rank} train barrier passed")

            ddpg_agent.train(
                    num_episodes=train_config['num_episodes'],
                    render=False,
                    render_freq=0,
                    )

        except Exception as e:
            logger.error(f"An error occurred: {e}", exc_info=True)

    def train(
        self, num_episodes, render: bool = False, render_freq: int = None, save_dir=None, run_number=None):
        """Trains the model for 'episodes' number of episodes."""

        # set models to train mode
        self.actor_model.train()
        self.critic_model.train()

        # Update save_dir if passed
        if save_dir is not None and save_dir.split("/")[-2] != "ddpg":
            self.save_dir = save_dir + "/ddpg/"
            print(f'new save dir: {self.save_dir}')
        elif save_dir is not None and save_dir.split("/")[-2] == "ddpg":
            self.save_dir = save_dir
            print(f'new save dir: {self.save_dir}')
        
        if self.callbacks:
            for callback in self.callbacks:
                    self._config = callback._config(self)
            if self.use_mpi:
                if self.rank == 0:
                    for callback in self.callbacks:
                        if isinstance(callback, rl_callbacks.WandbCallback):
                            callback.on_train_begin((self.critic_model, self.actor_model,), logs=self._config)
                            # logger.debug(f'{self.group}; Rank {self.rank} DDPG.train on train begin callback complete')
                        else:
                            callback.on_train_begin(logs=self._config)
            else:
                for callback in self.callbacks:
                    if isinstance(callback, rl_callbacks.WandbCallback):
                        callback.on_train_begin((self.critic_model, self.actor_model,), logs=self._config)
                        # logger.debug(f'DDPG.train on train begin callback complete')
                    else:
                        callback.on_train_begin(logs=self._config)
        
        if self.use_mpi:
            try:
                # instantiate new environment. Only rank 0 env will render episodes if render==True
                if self.rank == 0:
                    self.env = self._initialize_env(render, render_freq, context='train')
                    # logger.debug(f'{self.group}; Rank {self.rank} initiating environment with render {render}')
                else:
                    self.env = self._initialize_env(False, 0, context='train')
                    # logger.debug(f'{self.group}; Rank {self.rank} initializing environment')
            except Exception as e:
                logger.error(f"{self.group}; Rank {self.rank} Error in DDPG.train agent._initialize_env process: {e}", exc_info=True)
        
        else:
            try:
                # instantiate new environment. Only rank 0 env will render episodes if render==True
                self.env = self._initialize_env(render, render_freq, context='train')
                # logger.debug(f'initiating environment with render {render}')
            except Exception as e:
                logger.error(f"Error in DDPG.train agent._initialize_env process: {e}", exc_info=True)
        
        # initialize step counter (for logging)
        self._step = 1
        # set best reward
        # best_reward = self.env.reward_range[0]
        best_reward = -np.inf
        # instantiate list to store reward history
        reward_history = []
        # instantiate lists to store time history
        episode_time_history = []
        step_time_history = []
        learning_time_history = []
        steps_per_episode_history = []  # List to store steps per episode
        for i in range(num_episodes):
            episode_start_time = time.time()
            if self.callbacks:
                if self.use_mpi:
                    if self.rank == 0:
                        for callback in self.callbacks:
                            callback.on_train_epoch_begin(epoch=self._step, logs=None)
                            # logger.debug(f'{self.group}; Rank {self.rank} DDPG.train on train epoch begin callback completed')
                else:
                    for callback in self.callbacks:
                        callback.on_train_epoch_begin(epoch=self._step, logs=None)
                        # logger.debug(f'DDPG.train on train epoch begin callback completed')
            # reset noise
            if type(self.noise) == helper.OUNoise:
                self.noise.reset()
            # reset environment
            state, _ = self.env.reset()
            done = False
            episode_reward = 0
            episode_steps = 0  # Initialize steps counter for the episode
            while not done:
                # run callbacks on train batch begin
                # if self.callbacks:
                #     for callback in self.callbacks:
                #         callback.on_train_step_begin(step=self._step, logs=None)
                step_start_time = time.time()
                action = self.get_action(state)
                next_state, reward, term, trunc, _ = self.env.step(action)
                # extract observation from next state if next_state is dict (robotics)
                if isinstance(next_state, dict):
                    next_state = next_state['observation']
                step_time = time.time() - step_start_time
                step_time_history.append(step_time)
                # store trajectory in replay buffer
                self.replay_buffer.add(state, action, reward, next_state, done)
                if term or trunc:
                    done = True
                episode_reward += reward
                state = next_state
                episode_steps += 1
                
                # check if enough samples in replay buffer and if so, learn from experiences
                if self.replay_buffer.counter > self.batch_size:
                    learn_time = time.time()
                    actor_loss, critic_loss = self.learn()
                    self._train_step_config["actor_loss"] = actor_loss
                    self._train_step_config["critic_loss"] = critic_loss
                    # perform soft update on target networks
                    self.soft_update(self.actor_model, self.target_actor_model)
                    self.soft_update(self.critic_model, self.target_critic_model)

                    learning_time_history.append(time.time() - learn_time)

                self._train_step_config["step_reward"] = reward
                self._train_step_config["step_time"] = step_time
                
                # log to wandb if using wandb callback
                if self.callbacks:
                    if self.use_mpi:
                        # only have the main process log callback values to avoid multiple callback calls
                        if self.rank == 0:
                            for callback in self.callbacks:
                                callback.on_train_step_end(step=self._step, logs=self._train_step_config)
                                # logger.debug(f'{self.group}; Rank {self.rank} DDPG.train on train step end callback completed')
                    else:
                        for callback in self.callbacks:
                            callback.on_train_step_end(step=self._step, logs=self._train_step_config)
                            # logger.debug(f'DDPG.train on train step end callback completed')
                if not done:
                    self._step += 1
            
            episode_time = time.time() - episode_start_time
            episode_time_history.append(episode_time)
            reward_history.append(episode_reward)
            steps_per_episode_history.append(episode_steps) 
            avg_reward = np.mean(reward_history[-100:])
            avg_episode_time = np.mean(episode_time_history[-100:])
            avg_step_time = np.mean(step_time_history[-100:])
            avg_learn_time = np.mean(learning_time_history[-100:])
            avg_steps_per_episode = np.mean(steps_per_episode_history[-100:])  # Calculate average steps per episode

            self._train_episode_config['episode'] = i
            self._train_episode_config["episode_reward"] = episode_reward
            self._train_episode_config["avg_reward"] = avg_reward
            self._train_episode_config["episode_time"] = episode_time

            # check if best reward
            if avg_reward > best_reward:
                best_reward = avg_reward
                self._train_episode_config["best"] = True
                # save model
                self.save()
            else:
                self._train_episode_config["best"] = False

            if self.callbacks:
                if self.use_mpi:
                    if self.rank == 0:
                        for callback in self.callbacks:
                            callback.on_train_epoch_end(epoch=self._step, logs=self._train_episode_config)
                            # logger.debug(f'{self.group}; Rank {self.rank} DDPG.train on train epoch callback completed')
                else:
                    for callback in self.callbacks:
                        callback.on_train_epoch_end(epoch=self._step, logs=self._train_episode_config)
                        # logger.debug(f'DDPG.train on train epoch callback completed')

            print(f"episode {i+1}, score {episode_reward}, avg_score {avg_reward}, episode_time {episode_time:.2f}s, avg_episode_time {avg_episode_time:.2f}s, avg_step_time {avg_step_time:.6f}s, avg_learn_time {avg_learn_time:.6f}s, avg_steps_per_episode {avg_steps_per_episode:.2f}")

        if self.callbacks:
            if self.use_mpi:
                if self.rank == 0:
                    for callback in self.callbacks:
                        callback.on_train_end(logs=self._train_episode_config)
                        # logger.debug(f'{self.group}; Rank {self.rank} DDPG.train on train end callback complete')
            else:
                for callback in self.callbacks:
                    callback.on_train_end(logs=self._train_episode_config)
                    # logger.debug(f'DDPG.train on train end callback complete')
        # close the environment
        self.env.close()

       
    def test(self, num_episodes, render, render_freq, save_dir):
        """Runs a test over 'num_episodes'."""

        # set model in eval mode
        self.actor_model.eval()
        self.critic_model.eval()

        # Update save_dir if passed
        if save_dir is not None and save_dir.split("/")[-2] != "ddpg":
            self.save_dir = save_dir + "/ddpg/"
            print(f'new save dir: {self.save_dir}')
        elif save_dir is not None and save_dir.split("/")[-2] == "ddpg":
            self.save_dir = save_dir
            print(f'new save dir: {self.save_dir}')

        # instantiate list to store reward history
        reward_history = []
        # instantiate new environment
        self.env = self._initialize_env(render, render_freq, context='test')
        if self.callbacks:
            for callback in self.callbacks:
                callback.on_test_begin(logs=self._config)

        self._step = 1
        # set the model to calculate no gradients during evaluation
        with T.no_grad():
            for i in range(num_episodes):
                if self.callbacks:
                    for callback in self.callbacks:
                        callback.on_test_epoch_begin(epoch=self._step, logs=None) # update to pass any logs if needed
                states = []
                next_states = []
                actions = []
                rewards = []
                state, _ = self.env.reset()
                done = False
                episode_reward = 0
                while not done:
                    action = self.get_action(state, test=True)
                    next_state, reward, term, trunc, _ = self.env.step(action.numpy(force=True))
                    # extract observation from next state if next_state is dict (robotics)
                    if isinstance(next_state, dict):
                        next_state = next_state['observation']
                    # store trajectories
                    states.append(state)
                    actions.append(action)
                    next_states.append(next_state)
                    rewards.append(reward)
                    if term or trunc:
                        done = True
                    episode_reward += reward
                    state = next_state
                    self._step += 1
                reward_history.append(episode_reward)
                avg_reward = np.mean(reward_history[-100:])
                self._test_episode_config["episode_reward"] = episode_reward
                self._test_episode_config["avg_reward"] = avg_reward
                if self.callbacks:
                    for callback in self.callbacks:
                        callback.on_test_epoch_end(epoch=self._step, logs=self._test_episode_config)

                print(f"episode {i+1}, score {episode_reward}, avg_score {avg_reward}")

            if self.callbacks:
                for callback in self.callbacks:
                    callback.on_test_end(logs=self._test_episode_config)
            # close the environment
            self.env.close()


    def get_config(self):
        return {
                "agent_type": self.__class__.__name__,
                "env": serialize_env_spec(self.env.spec),
                "actor_model": self.actor_model.get_config(),
                "critic_model": self.critic_model.get_config(),
                "discount": self.discount,
                "tau": self.tau,
                "action_epsilon": self.action_epsilon,
                "replay_buffer": self.replay_buffer.get_config() if self.replay_buffer is not None else None,
                "batch_size": self.batch_size,
                "noise": self.noise.get_config(),
                'normalize_inputs': self.normalize_inputs,
                # 'normalize_kwargs': self.normalize_kwargs,
                'normalizer_clip': self.normalizer_clip,
                'normalizer_eps': self.normalizer_eps,
                'warmup': self.warmup,
                "callbacks": [callback.get_config() for callback in self.callbacks if self.callbacks is not None],
                "use_mpi": self.use_mpi,
                "save_dir": self.save_dir,
                "device": self.device,
            }


    def save(self, save_dir=None):
        """Saves the model."""

        # Change self.save_dir if save_dir
        # if save_dir is not None:
        #     self.save_dir = save_dir + "/ddpg/"

        config = self.get_config()

        # makes directory if it doesn't exist
        os.makedirs(self.save_dir, exist_ok=True)

        # writes and saves JSON file of DDPG agent config
        with open(self.save_dir + "/config.json", "w", encoding="utf-8") as f:
            json.dump(config, f)

        # saves policy and value model
        self.actor_model.save(self.save_dir)
        self.critic_model.save(self.save_dir)

        if self.normalize_inputs:
            self.state_normalizer.save_state(self.save_dir + "state_normalizer.npz")

        # if wandb callback, save wandb config
        # if self._wandb:
        #     for callback in self.callbacks:
        #         if isinstance(callback, rl_callbacks.WandbCallback):
        #             callback.save(self.save_dir + "/wandb_config.json")


    @classmethod
    def load(cls, config, load_weights=True):
        """Loads the model."""
        # # load reinforce agent config
        # with open(
        #     Path(folder).joinpath(Path("obj_config.json")), "r", encoding="utf-8"
        # ) as f:
        #     config = json.load(f)

        # create EnvSpec from config
        env_spec_json = json.dumps(config["env"])
        env_spec = gym.envs.registration.EnvSpec.from_json(env_spec_json)

        # load policy model
        actor_model = models.ActorModel.load(config['save_dir'], load_weights)
        # load value model
        critic_model = models.CriticModel.load(config['save_dir'], load_weights)
        # load replay buffer if not None
        if config['replay_buffer'] is not None:
            config['replay_buffer']['config']['env'] = gym.make(env_spec)
            replay_buffer = helper.ReplayBuffer(**config["replay_buffer"]["config"])
        else:
            replay_buffer = None
        # load noise
        noise = helper.Noise.create_instance(config["noise"]["class_name"], **config["noise"]["config"])
        # if normalizer, load
        normalize_inputs = config['normalize_inputs']
        # normalize_kwargs = obj_config['normalize_kwargs']
        normalizer_clip = config['normalizer_clip']
        # load callbacks
        callbacks = [rl_callbacks.load(callback_info['class_name'], callback_info['config']) for callback_info in config['callbacks']]

        # return DDPG agent
        agent = cls(
            gym.make(env_spec),
            actor_model = actor_model,
            critic_model = critic_model,
            discount=config["discount"],
            tau=config["tau"],
            action_epsilon=config["action_epsilon"],
            replay_buffer=replay_buffer,
            batch_size=config["batch_size"],
            noise=noise,
            normalize_inputs = normalize_inputs,
            normalizer_clip = normalizer_clip,
            warmup = config['warmup'],
            callbacks=callbacks,
            save_dir=config["save_dir"],
            device=config["device"],
        )

        if agent.normalize_inputs:
            agent.state_normalizer = helper.Normalizer.load_state(config['save_dir'] + "state_normalizer.npz")

        return agent
    

class TD3(Agent):
    """Twin Delayed Deep Deterministic Policy Gradient Agent."""

    def __init__(
        self,
        env: gym.Env,
        actor_model: models.ActorModel,
        critic_model: models.CriticModel,
        discount=0.99,
        tau=0.005,
        action_epsilon: float = 0.0,
        replay_buffer: helper.ReplayBuffer = None,
        batch_size: int = 256,
        noise = None,
        target_noise_stddev: float = 0.2,
        target_noise_clip: float = 0.5,
        actor_update_delay: int = 2,
        normalize_inputs: bool = False,
        # normalize_kwargs: dict = {},
        normalizer_clip:float=None,
        normalizer_eps:float=0.01,
        warmup:int=1000,
        callbacks: List = [],
        save_dir: str = "models",
        use_mpi = False,
        comm=None,
        device='cuda'
    ):
        try:
            self.use_mpi = use_mpi
            if self.use_mpi:
                if comm is not None:
                    self.comm = comm
                    self.rank = comm.Get_rank()
                else:
                    self.comm = MPI.COMM_WORLD
                    self.rank = MPI.COMM_WORLD.Get_rank()
                self.group = self.comm.Get_name()
            self.env = env
            self.actor_model = actor_model
            self.critic_model_a = critic_model
            # clone critic model to create second critic
            self.critic_model_b = self.clone_model(critic_model, weights=False)
            # set target actor and critic models
            self.target_actor_model = self.clone_model(self.actor_model)
            self.target_critic_model_a = self.clone_model(self.critic_model_a)
            self.target_critic_model_b = self.clone_model(self.critic_model_b)
            self.discount = discount
            self.tau = tau
            self.action_epsilon = action_epsilon
            self.replay_buffer = replay_buffer
            self.batch_size = batch_size
            self.noise = noise
            self.target_noise_stddev = target_noise_stddev
            # self.target_action_noise = helper.NormalNoise(shape=env.action_space.shape, mean=0.0,
            #                                               stddev=target_action_stddev, device=device)
            self.target_noise_clip = target_noise_clip
            self.actor_update_delay = actor_update_delay
            self.normalize_inputs = normalize_inputs
            # self.normalize_kwargs = normalize_kwargs
            self.normalizer_clip = normalizer_clip
            self.normalizer_eps = normalizer_eps
            self.warmup = warmup
            self.device = device
            # if self.use_mpi:
            #     logger.debug(f"rank {self.rank} TD3 init attributes set")
            # else:
            #     logger.debug(f"TD3 init attributes set")
        except Exception as e:
            if self.use_mpi:
                logger.error(f"rank {self.rank} Error in TD3 init: {e}", exc_info=True)
            else:
                logger.error(f"Error in TD3 init: {e}", exc_info=True)
        
        # set internal attributes
        try:
            if isinstance(env.observation_space, gym.spaces.dict.Dict):
                self._obs_space_shape = env.observation_space['observation'].shape
            else:
                self._obs_space_shape = env.observation_space.shape

            if self.normalize_inputs:
                self.state_normalizer = helper.Normalizer(self._obs_space_shape, self.normalizer_eps, self.normalizer_clip, self.device)
            
            # self.save_dir = save_dir + "/ddpg/"
            if save_dir is not None and "/td3/" not in save_dir:
                    self.save_dir = save_dir + "/td3/"
            elif save_dir is not None and "/td3/" in save_dir:
                    self.save_dir = save_dir

            # instantiate internal attribute use_her to be switched by HER class if using DDPG
            self._use_her = False
            # if self.use_mpi:
            #     logger.debug(f"rank {self.rank} TD3 init: internal attributes set")
            # else:
            #     logger.debug(f"TD3 init: internal attributes set")
        except Exception as e:
            if self.use_mpi:
                logger.error(f"rank {self.rank} Error in TD3 init internal attributes: {e}", exc_info=True)
            else:
                logger.error(f"Error in TD3 init internal attributes: {e}", exc_info=True)

        # Set callbacks
        try:
            self.callbacks = callbacks
            if callbacks:
                for callback in self.callbacks:
                    self._config = callback._config(self)
                    if isinstance(callback, rl_callbacks.WandbCallback):  
                        self._wandb = True

            else:
                self.callback_list = None
                self._wandb = False
            # if self.use_mpi:
            #     logger.debug(f"rank {self.rank} TD3 init: callbacks set")
            # else:
            #     logger.debug(f"TD3 init: callbacks set")
        except Exception as e:
            if self.use_mpi:
                logger.error(f"rank {self.rank} Error in TD3 init set callbacks: {e}", exc_info=True)
            else:
                logger.error(f"Error in TD3 init set callbacks: {e}", exc_info=True)
        self._train_config = {}
        self._train_episode_config = {}
        self._train_step_config = {}
        self._test_config = {}
        self._test_episode_config = {}

        self._step = None

    def clone(self):
        env = gym.make(self.env.spec)
        actor = self.clone_model(self.actor_model)
        critic = self.clone_model(self.critic_model_a)
        replay_buffer = self.replay_buffer.clone()
        noise = self.noise.clone()

        return TD3(
            env,
            actor,
            critic,
            self.discount,
            self.tau,
            self.action_epsilon,
            replay_buffer,
            self.batch_size,
            noise,
            self.normalize_inputs,
            # self.normalize_kwargs,
            self.normalizer_clip,
            self.normalizer_eps,
        )
        
    
    def clone_model(self, model, weights=True):
        """Clones a model."""
        return model.get_clone(weights)
    
    @classmethod
    def build(
        cls,
        env,
        actor_cnn_layers,
        critic_cnn_layers,
        actor_layers,
        critic_state_layers,
        critic_merged_layers,
        kernels,
        callbacks,
        config,#: wandb.config,
        save_dir: str = "models/",
    ):
        """Builds the agent."""
        # Actor
        actor_learning_rate=config[config.model_type][f"{config.model_type}_actor_learning_rate"]
        actor_optimizer = config[config.model_type][f"{config.model_type}_actor_optimizer"]
        # get optimizer params
        actor_optimizer_params = {}
        if actor_optimizer == "Adam":
            actor_optimizer_params['weight_decay'] = \
                config[config.model_type][f"{config.model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_weight_decay']
        
        elif actor_optimizer == "Adagrad":
            actor_optimizer_params['weight_decay'] = \
                config[config.model_type][f"{config.model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_weight_decay']
            actor_optimizer_params['lr_decay'] = \
                config[config.model_type][f"{config.model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_lr_decay']
        
        elif actor_optimizer == "RMSprop" or actor_optimizer == "SGD":
            actor_optimizer_params['weight_decay'] = \
                config[config.model_type][f"{config.model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_weight_decay']
            actor_optimizer_params['momentum'] = \
                config[config.model_type][f"{config.model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_momentum']

        actor_normalize_layers = config[config.model_type][f"{config.model_type}_actor_normalize_layers"]

        # Critic
        critic_learning_rate=config[config.model_type][f"{config.model_type}_critic_learning_rate"]
        critic_optimizer = config[config.model_type][f"{config.model_type}_critic_optimizer"]
        critic_optimizer_params = {}
        if critic_optimizer == "Adam":
            critic_optimizer_params['weight_decay'] = \
                config[config.model_type][f"{config.model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_weight_decay']
        
        elif critic_optimizer == "Adagrad":
            critic_optimizer_params['weight_decay'] = \
                config[config.model_type][f"{config.model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_weight_decay']
            critic_optimizer_params['lr_decay'] = \
                config[config.model_type][f"{config.model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_lr_decay']
        
        elif critic_optimizer == "RMSprop" or critic_optimizer == "SGD":
            critic_optimizer_params['weight_decay'] = \
                config[config.model_type][f"{config.model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_weight_decay']
            critic_optimizer_params['momentum'] = \
                config[config.model_type][f"{config.model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_momentum']
        
        critic_normalize_layers = config[config.model_type][f"{config.model_type}_critic_normalize_layers"]

        # Check if CNN layers and if so, build CNN model
        if actor_cnn_layers:
            actor_cnn_model = cnn_models.CNN(actor_cnn_layers, env)
        else:
            actor_cnn_model = None

        if critic_cnn_layers:
            critic_cnn_model = cnn_models.CNN(critic_cnn_layers, env)
        else:
            critic_cnn_model = None

        # Set device
        device = config[config.model_type][f"{config.model_type}_device"]

        # get desired, achieved, reward func for env
        desired_goal_func, achieved_goal_func, reward_func = gym_helper.get_her_goal_functions(env)
        goal_shape = desired_goal_func(env).shape

        # Get actor clamp value
        # clamp_output = config[config.model_type][f"{config.model_type}_actor_clamp_output"]
        
        actor_model = models.ActorModel(env = env,
                                        cnn_model = actor_cnn_model,
                                        dense_layers = actor_layers,
                                        output_layer_kernel=kernels[f'actor_output_kernel'],
                                        goal_shape=goal_shape,
                                        optimizer = actor_optimizer,
                                        optimizer_params = actor_optimizer_params,
                                        learning_rate = actor_learning_rate,
                                        normalize_layers = actor_normalize_layers,
                                        # clamp_output=clamp_output,
                                        device=device,
        )
        critic_model = models.CriticModel(env = env,
                                          cnn_model = critic_cnn_model,
                                          state_layers = critic_state_layers,
                                          merged_layers = critic_merged_layers,
                                          output_layer_kernel=kernels[f'critic_output_kernel'],
                                          goal_shape=goal_shape,
                                          optimizer = critic_optimizer,
                                          optimizer_params = critic_optimizer_params,
                                          learning_rate = critic_learning_rate,
                                          normalize_layers = critic_normalize_layers,
                                          device=device,
        )

        # action epsilon
        action_epsilon = config[config.model_type][f"{config.model_type}_epsilon_greedy"]

        # normalize inputs
        normalize_inputs = config[config.model_type][f"{config.model_type}_normalize_input"]
        # normalize_kwargs = {}
        if "True" in normalize_inputs:
            # normalize_kwargs = config[config.model_type][f"{config.model_type}_normalize_clip"]
            normalizer_clip = config[config.model_type][f"{config.model_type}_normalize_clip"]

        agent = cls(
            env = env,
            actor_model = actor_model,
            critic_model = critic_model,
            discount = config[config.model_type][f"{config.model_type}_discount"],
            tau = config[config.model_type][f"{config.model_type}_tau"],
            action_epsilon = action_epsilon,
            replay_buffer = helper.ReplayBuffer(env=env),
            batch_size = config[config.model_type][f"{config.model_type}_batch_size"],
            noise = helper.Noise.create_instance(config[config.model_type][f"{config.model_type}_noise"], shape=env.action_space.shape, **config[config.model_type][f"{config.model_type}_noise_{config[config.model_type][f'{config.model_type}_noise']}"]),
            normalize_inputs = normalize_inputs,
            # normalize_kwargs = normalize_kwargs,
            normalizer_clip = normalizer_clip,
            callbacks = callbacks,
            save_dir = save_dir,
        )

        agent.save()

        return agent

    def _init_her(self):
            # self.normalize_inputs = True
            self._use_her = True
            # self.state_normalizer = helper.SharedNormalizer(size=self._obs_space_shape, eps=eps, clip_range=clip_range)
            # self.goal_normalizer = helper.SharedNormalizer(size=goal_shape, eps=eps, clip_range=clip_range)
            # set clamp for targets
            # self.target_clamp = 1 / (1 - self.discount)

    def _initialize_env(self, render=False, render_freq=10, context=None):
        """Initializes a new environment."""
        if self.use_mpi:
            # logger.debug(f"rank {self.rank} TD3._initialize_env called")
            try:
                if render:
                    env = gym.make(self.env.spec, render_mode="rgb_array")
                    # logger.debug(f"rank {self.rank} TD3._initialize_env: env built with rendering")
                    if context == "train":
                        os.makedirs(self.save_dir + "/renders/training", exist_ok=True)
                        return gym.wrappers.RecordVideo(
                            env,
                            self.save_dir + "/renders/training",
                            episode_trigger=lambda episode_id: (episode_id+1) % render_freq == 0,
                        )
                    elif context == "test":
                        os.makedirs(self.save_dir + "/renders/testing", exist_ok=True)
                        return gym.wrappers.RecordVideo(
                            env,
                            self.save_dir + "/renders/testing",
                            episode_trigger=lambda episode_id: (episode_id+1) % render_freq == 0,
                        )
                    else:
                        logger.warning(f"rank {self.rank} Unknown context: {context}, environment will not be recorded")

                return gym.make(self.env.spec)
            except Exception as e:
                logger.error(f"rank {self.rank} Error in TD3._initialize_env: {e}", exc_info=True)
                raise
        else:
            # logger.debug(f"TD3._initialize_env called")
            try:
                if render:
                    env = gym.make(self.env.spec, render_mode="rgb_array")
                    # logger.debug(f"TD3._initialize_env: env built with rendering")
                    if context == "train":
                        os.makedirs(self.save_dir + "/renders/training", exist_ok=True)
                        return gym.wrappers.RecordVideo(
                            env,
                            self.save_dir + "/renders/training",
                            episode_trigger=lambda episode_id: (episode_id+1) % render_freq == 0,
                        )
                    elif context == "test":
                        os.makedirs(self.save_dir + "/renders/testing", exist_ok=True)
                        return gym.wrappers.RecordVideo(
                            env,
                            self.save_dir + "/renders/testing",
                            episode_trigger=lambda episode_id: (episode_id+1) % render_freq == 0,
                        )
                    else:
                        logger.warning(f"Unknown context: {context}, environment will not be recorded")

                return gym.make(self.env.spec)
            except Exception as e:
                logger.error(f"Error in TD3._initialize_env: {e}", exc_info=True)
                raise

    
    def get_action(self, state, goal=None, grad=True, test=False,
                   state_normalizer:SharedNormalizer=None,
                   goal_normalizer:SharedNormalizer=None):
        
        # print('state')
        # print(state)
        # print('goal')
        # print(goal)
        # print('state normalizer')
        # print(state_normalizer.get_config())
        # print('goal normalizer')
        # print(goal_normalizer.get_config())

        # check if get action is for testing
        if test:
            # print(f'action test fired')
            with T.no_grad():
                # print('no grad fired')
                # normalize state if self.normalize_inputs
                if self.normalize_inputs:
                    state = self.state_normalizer.normalize(state)
                # (HER) else if using HER, normalize using passed normalizer
                elif self._use_her:
                    #DEBUG
                    print('used passed state normalizer')
                    state = state_normalizer.normalize(state)

                # make sure state is a tensor and on correct device
                state = T.tensor(state, dtype=T.float32, device=self.actor_model.device)
                
                # (HER) normalize goal if self._use_her using passed normalizer
                if self._use_her:
                    goal = goal_normalizer.normalize(goal)
                    # make sure goal is a tensor and on correct device
                    goal = T.tensor(goal, dtype=T.float32, device=self.actor_model.device)
                    #DEBUG
                    print('used passed goal normalizer')
                
                # permute state to (C,H,W) if actor using cnn model
                if self.actor_model.cnn_model:
                    state = state.permute(2, 0, 1).unsqueeze(0)

                # get action
                # _, action = self.actor_model(state, goal)
                _, action = self.target_actor_model(state, goal) # use target network for testing
                # transfer action to cpu, detach from any graphs, tranform to numpy, and flatten
                action_np = action.cpu().detach().numpy().flatten()
        
        # check if using epsilon greedy
        else: #self.action_epsilon > 0.0:
            # print('action train fired')
            # if random number is less than epsilon, sample random action
            if np.random.random() < self.action_epsilon:
                # print('epsilon greedy action')
                action_np = self.env.action_space.sample()
                noise_np = np.zeros_like(action_np)
            
            else:
                # if gradient tracking is true
                if grad:
                    # print('with grad fired')
                    # normalize state if self.normalize_inputs
                    if self.normalize_inputs==True or self._use_her==True:
                        state = self.state_normalizer.normalize(state)
                    # (HER) use passed state normalizer if using HER
                    # elif self._use_her:
                    #     state = state_normalizer.normalize(state)
                    
                    # make sure state is a tensor and on correct device
                    state = T.tensor(state, dtype=T.float32, device=self.actor_model.device)
                    
                    # (HER) normalize goal if self._use_her using passed normalizer
                    if self._use_her==True:
                        goal = goal_normalizer.normalize(goal)
                        # print(f'normalized goal: {goal}')
                        # make sure goal is a tensor and on correct device
                        goal = T.tensor(goal, dtype=T.float32, device=self.actor_model.device)

                    # permute state to (C,H,W) if actor using cnn model
                    if self.actor_model.cnn_model:
                        state = state.permute(2, 0, 1).unsqueeze(0)

                    # Create noise
                    noise = self.noise()
                    # print(f'noise: {noise}')

                    # Check if in warmup
                    if self._step <= self.warmup:
                        action = T.tensor(self.env.action_space.sample(), dtype=T.float32, device=self.actor_model.device)

                    else:
                        _, pi = self.actor_model(state, goal)
                        # print(f'pi: {pi}')

                        # Convert the action space bounds to a tensor on the same device
                        action_space_high = T.tensor(self.env.action_space.high, dtype=T.float32, device=self.actor_model.device)
                        action_space_low = T.tensor(self.env.action_space.low, dtype=T.float32, device=self.actor_model.device)
                        # print(f'actor model device:{self.actor_model.device}')
                        action = (pi + noise).clip(action_space_low, action_space_high)
                        # print(f'action + noise: {action}')

                    noise_np = noise.cpu().detach().numpy().flatten()
                    action_np = action.cpu().detach().numpy().flatten()
                    # print(f'action np: {action_np}')

                else:
                    with T.no_grad():
                        # print('without grad fired')
                        # normalize state if self.normalize_inputs
                        if self.normalize_inputs==True:
                            state = self.state_normalizer.normalize(state)
                        # (HER) use passed state normalizer if using HER
                        elif self._use_her==True:
                            state = state_normalizer.normalize(state)

                        # make sure state is a tensor and on correct device
                        state = T.tensor(state, dtype=T.float32, device=self.actor_model.device)
                        # normalize goal if self._use_her
                        if self._use_her==True:
                            goal = goal_normalizer.normalize(goal)
                            # make sure goal is a tensor and on correct device
                            goal = T.tensor(goal, dtype=T.float32, device=self.actor_model.device)
                        
                        # permute state to (C,H,W) if actor using cnn model
                        if self.actor_model.cnn_model:
                            state = state.permute(2, 0, 1).unsqueeze(0)

                         # Create noise
                        noise = self.noise()

                        # Check if in warmup
                        if self._step <= self.warmup:
                            action = T.tensor(self.env.action_space.sample(), dtype=T.float32, device=self.actor_model.device)

                        else:
                            _, pi = self.actor_model(state, goal)

                            # Convert the action space bounds to a tensor on the same device
                            action_space_high = T.tensor(self.env.action_space.high, dtype=T.float32, device=self.actor_model.device)
                            action_space_low = T.tensor(self.env.action_space.low, dtype=T.float32, device=self.actor_model.device)

                            action = (pi + noise).clip(action_space_low, action_space_high)

                        noise_np = noise.cpu().detach().numpy().flatten()
                        action_np = action.cpu().detach().numpy().flatten()

        if test:
            # loop over all actions to log to wandb
            for i, a in enumerate(action_np):
                # Log the values to wandb
                self._train_step_config[f'action_{i}'] = a

        else:
            # Loop over the noise and action values and log them to wandb
            for i, (a,n) in enumerate(zip(action_np, noise_np)):
                # Log the values to wandb
                self._train_step_config[f'action_{i}'] = a
                self._train_step_config[f'noise_{i}'] = n
        
        # print(f'pi: {pi}; noise: {noise}; action_np: {action_np}')

        return action_np


    # def learn(self, replay_buffer:Buffer=None,
    #           state_normalizer:Union[Normalizer, SharedNormalizer]=None,
    #           goal_normalizer:Union[Normalizer, SharedNormalizer]=None,
    #           ):
    #     # time batch sampling
    #     # timer = time.time()
    #     # sample a batch of experiences from the replay buffer
    #     if self._use_her: # if using HER
    #         states, actions, rewards, next_states, dones, achieved_goals, next_achieved_goals, desired_goals = replay_buffer.sample(self.batch_size)
    #     else:
    #         states, actions, rewards, next_states, dones = self.replay_buffer.sample(self.batch_size)
    #     # print time taking to batch sample
    #     # print(f'time to sample batch: {time.time() - timer}')
    #     # normalize states if self.normalize_inputs
    #     if self.normalize_inputs:
    #         states = self.state_normalizer.normalize(states)
    #         next_states = self.state_normalizer.normalize(next_states)

    #     # (HER) Use passed normalizers to normalize states and goals
    #     if self._use_her:
    #         states = state_normalizer.normalize(states)
    #         next_states = state_normalizer.normalize(next_states)
    #         desired_goals = goal_normalizer.normalize(desired_goals)
        
    #     # time conversion to tensors
    #     # timer = time.time()
    #     # Convert to tensors
    #     # states = T.tensor(states, dtype=T.float32, device=self.actor_model.device)
    #     # actions = T.tensor(actions, dtype=T.float32, device=self.actor_model.device)
    #     # rewards = T.tensor(rewards, dtype=T.float32, device=self.actor_model.device)
    #     # next_states = T.tensor(next_states, dtype=T.float32, device=self.actor_model.device)
    #     # dones = T.tensor(dones, dtype=T.int8, device=self.actor_model.device)
    #     # logger.debug(f"states: {states}")
    #     # logger.debug(f"actions: {actions}")
    #     # logger.debug(f"rewards: {rewards}")
    #     # logger.debug(f"next states: {next_states}")
    #     # logger.debug(f"dones: {dones}")
    #     # if using HER, convert desired goals to tensors
    #     # if self._use_her:
    #     #     desired_goals = T.tensor(desired_goals, dtype=T.float32, device=self.actor_model.device)
    #     # else:
    #     #     # set desired goals to None
    #     #     desired_goals = None
    #     if not self._use_her:
    #         desired_goals = None
    #     # print time to convert to tensors
    #     # print(f'time to convert to tensors: {time.time() - timer}')

    #     # permute states and next states if using cnn
    #     if self.actor_model.cnn_model:
    #         states = states.permute(0, 3, 1, 2)
    #         next_states = next_states.permute(0, 3, 1, 2)

    #     # convert rewards and dones to 2d tensors
    #     rewards = rewards.unsqueeze(1)
    #     dones = dones.unsqueeze(1)

    #     # with T.no_grad():
    #     # get target values
    #     # timer = time.time()
    #     _, target_actions = self.target_actor_model(next_states, desired_goals)
    #     # print(f'time to get target actions: {time.time() - timer}')
    #     logger.debug(f"target actions: {target_actions}")
    #     # Add gaussian noise to target actions
    #     # timer = time.time()
    #     noise = self.target_action_noise()
    #     # print(f'time to get noise: {time.time() - timer}')
    #     logger.debug(f"target action noise: {noise}")
    #     # timer = time.time()
    #     target_actions = target_actions + T.clamp(noise, min=-self.target_action_clip, max=self.target_action_clip)
    #     # print(f'time add noise: {time.time() - timer}')
    #     logger.debug(f"target actions after added noise: {target_actions}")
    #     # Clamp targets between target action clip values
    #     # timer = time.time()
    #     target_actions = T.clamp(target_actions, min=T.tensor(self.env.action_space.low[0], dtype=T.float, device=self.device), max=T.tensor(self.env.action_space.high[0], dtype=T.float, device=self.device))
    #     # print(f'time to clamp target actions: {time.time() - timer}')
    #     logger.debug(f"target actions after clamped: {target_actions}")
    #     # print(f'Agent {dist.get_rank()}: target_actions: {target_actions}')
    #     # timer = time.time()
    #     target_critic_values_a = self.target_critic_model_a(next_states, target_actions, desired_goals)
    #     # print(f'time to get critic a values: {time.time() - timer}')
    #     logger.debug(f"target critic a values: {target_critic_values_a}")
    #     # print(f'Agent {dist.get_rank()}: target_critic_values_a: {target_critic_values_a}')
    #     # timer = time.time()
    #     target_critic_values_b = self.target_critic_model_b(next_states, target_actions, desired_goals)
    #     # print(f'time to get critic b values: {time.time() - timer}')
    #     logger.debug(f"target critic b values: {target_critic_values_b}")
    #     # Take minimum target critic value and set it as critic value
    #     # timer = time.time()
    #     target_critic_values = T.min(target_critic_values_a, target_critic_values_b)
    #     # print(f'time to get min of critic values: {time.time() - timer}')
    #     logger.debug(f"minimum target critic values: {target_critic_values}")
    #     # print(f'Agent {dist.get_rank()}: target_critic_values_b: {target_critic_values_b}')
    #     # timer = time.time()
    #     targets = rewards + self.discount * target_critic_values * (1 - dones)
    #     # print(f'time to calc targets: {time.time() - timer}')
    #     logger.debug(f"target: {targets}")

    #     if self._use_her:
    #         targets = T.clamp(targets, min=-1/(1-self.discount), max=0)

    #     self.critic_model_a.optimizer.zero_grad()
    #     self.critic_model_b.optimizer.zero_grad()

    #     # get current critic values and calculate critic losses
    #     # timer = time.time()
    #     predictions_a = self.critic_model_a(states, actions, desired_goals)
    #     # print(f'time for predictions a: {time.time() - timer}')
    #     logger.debug(f"predictions a: {predictions_a}")
    #     # timer = time.time()
    #     predictions_b = self.critic_model_b(states, actions, desired_goals)
    #     # print(f'time for predictions b: {time.time() - timer}')
    #     logger.debug(f"predictions b: {predictions_b}")
    #     # timer = time.time()
    #     critic_loss_a = F.mse_loss(targets, predictions_a)
    #     # print(f'time for critic loss a: {time.time() - timer}')
    #     logger.debug(f"critic loss a: {critic_loss_a}")
    #     # timer = time.time()
    #     critic_loss_b = F.mse_loss(targets, predictions_b)
    #     # print(f'time for critic loss b: {time.time() - timer}')
    #     logger.debug(f"critic loss b: {critic_loss_b}")
        
    #     # add losses to get total critic loss
    #     critic_loss = critic_loss_a + critic_loss_b
    #     logger.debug(f"combined loss: {critic_loss}")
        
    #     # update critics
    #     # critic_loss_a.backward(retain_graph=True)
    #     # critic_loss_b.backward()
    #     # timer = time.time()
    #     critic_loss.backward()
    #     # print(f'time for critics backwards pass: {time.time() - timer}')

    #     # Print gradients
    #     for name, param in self.critic_model_a.named_parameters():
    #         if param.grad is not None:
    #             logger.debug(f"Gradient for {name}: {param.grad}")
    #         else:
    #             logger.degub(f"No gradient calculated for {name}")
    #     for name, param in self.critic_model_b.named_parameters():
    #         if param.grad is not None:
    #             logger.debug(f"Gradient for {name}: {param.grad}")
    #         else:
    #             logger.debug(f"No gradient calculated for {name}")

    #     # if self._use_her:
    #         # print(f'agent {MPI.COMM_WORLD.Get_rank()} reached critic optimization')
    #         # Synchronize gradients
    #         ## T.DIST CUDA ##
    #         # print(f'agent {MPI.COMM_WORLD.Get_rank()} param grad before all reduce:')
    #         # for param in self.critic_model.parameters():
    #         #     if param.grad is not None:
    #         #         # print(f'agent {MPI.COMM_WORLD.Get_rank()} param shape: {param.shape}')
    #         #         print(param.grad)
    #         #         dist.all_reduce(param.grad.data, op=dist.ReduceOp.SUM)
    #                 # print(f'agent {dist.get_rank()} param grad after all reduce:')
    #                 # print(param.grad)
    #         #         # param.grad.data /= dist.get_world_size()
    #         #         # print(f'agent {dist.get_rank()} param grad after divide by world size')
    #         #         # print(param.grad)

    #     ## MPI CPU ##
    #     if self.use_mpi:
    #         helper.sync_grads_sum(self.critic_model_a, self.comm)
    #         helper.sync_grads_sum(self.critic_model_b, self.comm)
        
    #     # timer = time.time()
    #     self.critic_model_a.optimizer.step()
    #     self.critic_model_b.optimizer.step()
    #     # print(f'time to optimize critics: {time.time() - timer}')

    #     self.actor_model.optimizer.zero_grad()
    #     # timer = time.time()
    #     pre_act_values, action_values = self.actor_model(states, desired_goals)
    #     # print(f'time for action values: {time.time() - timer}')
    #     # logger.debug(f"action values: {action_values}")
    #     # print(f'Agent {dist.get_rank()}: action_values: {action_values}')
    #     # timer = time.time()
    #     critic_values = self.critic_model_a(states, action_values, desired_goals)
    #     # print(f'time for critic values: {time.time() - timer}')
    #     # logger.debug(f"critic values: {critic_values}")
    #     # Calculate actor loss
    #     # timer = time.time()
    #     actor_loss = -T.mean(critic_values)
    #     # print(f'time for actor loss: {time.time() - timer}')
    #     # logger.debug(f"actor loss: {actor_loss}")
    #     if self._use_her:
    #         actor_loss += pre_act_values.pow(2).mean()
        
    #     # Update actor if actor update delay mod self._step = 0
    #     if self._step % self.actor_update_delay == 0:
    #         # logger.debug(f"updating actor: step {self._step}")
    #         # timer = time.time()
    #         actor_loss.backward()

    #         # print gradients
    #         for name, param in self.actor_model.named_parameters():
    #             if param.grad is not None:
    #                 logger.debug(f"Gradient for {name}: {param.grad}")
    #             else:
    #                 logger.debug(f"No gradient calculated for {name}")
    #         # print(f'time for actor loss: {time.time() - timer}')
    #         # if self._use_her:
    #             # Synchronize Gradients
    #             ## T.DIST CUDA ##
    #             # print(f'agent {MPI.COMM_WORLD.Get_rank()} reached actor optimization')
    #             # print(f'agent {MPI.COMM_WORLD.Get_rank()} param grad before all reduce:')
    #             # for param in self.actor_model.parameters():
    #             #     if param.grad is not None:
    #                     # print(f'agent {MPI.COMM_WORLD.Get_rank()} param shape: {param.shape}')
    #                     # print(param.grad)
    #             #         dist.all_reduce(param.grad.data, op=dist.ReduceOp.SUM)
    #             #         # print(f'agent {dist.get_rank()} param grad after reduce:')
    #             #         # print(param.grad)
    #             #         # print(f'agent {dist.get_rank()} world size {dist.get_world_size()}')
    #             #         param.grad.data /= dist.get_world_size()
    #             #         # print(f'agent {dist.get_rank()} param grad after divide by world size')
    #             #         # print(param.grad)
    #         ## MPI CPU ##
    #         if self.use_mpi:
    #             helper.sync_grads_sum(self.actor_model, self.comm)
            
    #         # timer = time.time()
    #         self.actor_model.optimizer.step()
    #         # print(f'time to optimize actor: {time.time() - timer}')

    #         # perform soft update on target networks
    #         # timer = time.time()
    #         self.soft_update(self.actor_model, self.target_actor_model)
    #         self.soft_update(self.critic_model_a, self.target_critic_model_a)
    #         self.soft_update(self.critic_model_b, self.target_critic_model_b)
    #         # print(f'time to perform soft update: {time.time() - timer}')

    #     # add metrics to step_logs
    #     self._train_step_config['actor_predictions'] = action_values.mean()
    #     self._train_step_config['critic_predictions'] = critic_values.mean()
    #     self._train_step_config['target_actor_predictions'] = target_actions.mean()
    #     self._train_step_config['target_critic_predictions'] = target_critic_values_a.mean()
        
    #     return actor_loss.item(), critic_loss_a.item()

    def learn(self, replay_buffer: Buffer = None,
          state_normalizer: Union[Normalizer, SharedNormalizer] = None,
          goal_normalizer: Union[Normalizer, SharedNormalizer] = None):
        # Timer for the entire function
        total_start_time = time.time()
        
        # Sample a batch of experiences from the replay buffer
        sample_start_time = time.time()
        if self._use_her:  # if using HER
            states, actions, rewards, next_states, dones, achieved_goals, next_achieved_goals, desired_goals = replay_buffer.sample(self.batch_size)
        else:
            states, actions, rewards, next_states, dones = self.replay_buffer.sample(self.batch_size)
        # print(f'Time to sample batch: {time.time() - sample_start_time} seconds')
        
        # Normalize states if self.normalize_inputs
        normalize_start_time = time.time()
        if self.normalize_inputs==True:
            states = self.state_normalizer.normalize(states)
            next_states = self.state_normalizer.normalize(next_states)
        if self._use_her==True:
            states = state_normalizer.normalize(states)
            next_states = state_normalizer.normalize(next_states)
            desired_goals = goal_normalizer.normalize(desired_goals)
        else:
            desired_goals = None
        # print(f'Time to normalize inputs: {time.time() - normalize_start_time} seconds')

        # Permute states and next states if using CNN
        permute_start_time = time.time()
        if self.actor_model.cnn_model:
            states = states.permute(0, 3, 1, 2)
            next_states = next_states.permute(0, 3, 1, 2)
        # print(f'Time to permute states: {time.time() - permute_start_time} seconds')

        # Convert rewards and dones to 2D tensors
        conversion_start_time = time.time()
        rewards = rewards.unsqueeze(1)
        dones = dones.unsqueeze(1)
        # print(f'Time to convert rewards and dones: {time.time() - conversion_start_time} seconds')

        # Get target values
        target_start_time = time.time()
        with T.no_grad():
            _, target_actions = self.target_actor_model(next_states, desired_goals)
            noise = (T.randn_like(target_actions) * self.target_noise_stddev).clamp(-self.target_noise_clip, self.target_noise_clip)
            target_actions = (target_actions + noise).clamp(self.env.action_space.low[0], self.env.action_space.high[0])
            # target_actions = T.clamp(target_actions, min=T.tensor(self.env.action_space.low[0], dtype=T.float, device=self.device), max=T.tensor(self.env.action_space.high[0], dtype=T.float, device=self.device))
            target_critic_values_a = self.target_critic_model_a(next_states, target_actions, desired_goals)
            target_critic_values_b = self.target_critic_model_b(next_states, target_actions, desired_goals)
            target_critic_values = T.min(target_critic_values_a, target_critic_values_b)
            #DEBUG
            # print(f'rewards device:{rewards.device}')
            # print(f'target critic values device:{target_critic_values.device}')
            targets = rewards + (1 - dones) * self.discount * target_critic_values
            if self._use_her:
                targets = T.clamp(targets, min=-1/(1-self.discount), max=0)
        # print(f'Time to get target values: {time.time() - target_start_time} seconds')

        # Zero gradients for the optimizers
        zero_grad_start_time = time.time()
        self.critic_model_a.optimizer.zero_grad()
        self.critic_model_b.optimizer.zero_grad()
        # print(f'Time to zero gradients: {time.time() - zero_grad_start_time} seconds')

        # Get current critic values and calculate critic losses
        critic_loss_start_time = time.time()
        predictions_a = self.critic_model_a(states, actions, desired_goals)
        predictions_b = self.critic_model_b(states, actions, desired_goals)
        # critic_loss_a = F.mse_loss(predictions_a, targets)
        # critic_loss_b = F.mse_loss(predictions_b, targets)
        critic_loss = F.mse_loss(predictions_a, targets) + F.mse_loss(predictions_b, targets)
        # print(f'Time to calculate critic losses: {time.time() - critic_loss_start_time} seconds')

        # Backward pass and optimization for critics
        critic_backward_start_time = time.time()
        critic_loss.backward()
        if self.use_mpi==True:
            helper.sync_grads_sum(self.critic_model_a, self.comm)
            helper.sync_grads_sum(self.critic_model_b, self.comm)
        self.critic_model_a.optimizer.step()
        self.critic_model_b.optimizer.step()
        # print(f'Time for critic backward pass and optimization: {time.time() - critic_backward_start_time} seconds')

        # Zero gradients for the actor optimizer
        actor_zero_grad_start_time = time.time()
        self.actor_model.optimizer.zero_grad()
        # print(f'Time to zero actor gradients: {time.time() - actor_zero_grad_start_time} seconds')

        # Get current actor values and calculate actor loss
        actor_loss_start_time = time.time()
        pre_act_values, action_values = self.actor_model(states, desired_goals)
        critic_values = self.critic_model_a(states, action_values, desired_goals)
        actor_loss = -T.mean(critic_values)
        if self._use_her==True:
            actor_loss += pre_act_values.pow(2).mean()
        # print(f'Time to calculate actor loss: {time.time() - actor_loss_start_time} seconds')

        # Backward pass and optimization for the actor
        actor_backward_start_time = time.time()
        if self._step % self.actor_update_delay == 0:
            actor_loss.backward()
            if self.use_mpi==True:
                helper.sync_grads_sum(self.actor_model, self.comm)
            self.actor_model.optimizer.step()
            self.soft_update(self.actor_model, self.target_actor_model)
            self.soft_update(self.critic_model_a, self.target_critic_model_a)
            self.soft_update(self.critic_model_b, self.target_critic_model_b)
        # print(f'Time for actor backward pass and optimization: {time.time() - actor_backward_start_time} seconds')

        # Total time for the learn function
        # print(f'Total time for learn function: {time.time() - total_start_time} seconds')

        # Add metrics to step_logs
        self._train_step_config['actor_predictions'] = action_values.mean()
        self._train_step_config['critic_predictions'] = critic_values.mean()
        self._train_step_config['target_actor_predictions'] = target_actions.mean()
        self._train_step_config['target_critic_predictions'] = target_critic_values_a.mean()

        return actor_loss.item(), critic_loss.item()
        
    
    def soft_update(self, current, target):
        with T.no_grad():
            for current_params, target_params in zip(current.parameters(), target.parameters()):
                target_params.data.copy_(self.tau * current_params.data + (1 - self.tau) * target_params.data)

    @classmethod
    def sweep_train(
        cls,
        config, # wandb.config,
        train_config,
        env_spec,
        callbacks,
        run_number,
        comm=None,
    ):
        """Builds and trains agents from sweep configs. Works with MPI"""
        rank = MPI.COMM_WORLD.rank

        if comm is not None:
            logger.debug(f"Rank {rank} comm detected")
            rank = comm.Get_rank()
            logger.debug(f"Global rank {MPI.COMM_WORLD.Get_rank()} in {comm.Get_name()} set to comm rank {rank}")
            logger.debug(f"init_sweep fired: global rank {MPI.COMM_WORLD.rank}, group rank {rank}, {comm.Get_name()}")
        else:
            logger.debug(f"init_sweep fired: global rank")
        try:
            # rank = MPI.COMM_WORLD.rank
            # Instantiate env from env_spec
            env = gym.make(gym.envs.registration.EnvSpec.from_json(env_spec))
            # agent_config_path = f'sweep/agent_config_{run_number}.json'
            # logger.debug(f"rank {rank} agent config path: {agent_config_path}")
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} train config: {train_config}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} env spec id: {env.spec.id}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} callbacks: {callbacks}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} run number: {run_number}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} config set: {config}")
            else:
                logger.debug(f"train config: {train_config}")
                logger.debug(f"env spec id: {env.spec.id}")
                logger.debug(f"callbacks: {callbacks}")
                logger.debug(f"run number: {run_number}")
                logger.debug(f"config set: {config}")
            model_type = list(config.keys())[0]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} model type: {model_type}")
            else:
                logger.debug(f"model type: {model_type}")
            # Only primary process (rank 0) calls wandb.init() to build agent and log data

            actor_cnn_layers, critic_cnn_layers, actor_layers, critic_state_layers, critic_merged_layers, kernels = wandb_support.build_layers(config)
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} layers built")
            else:
                logger.debug(f"layers built")
            # Actor
            actor_learning_rate=config[model_type][f"{model_type}_actor_learning_rate"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} actor learning rate set")
            else:
                logger.debug(f"actor learning rate set")
            actor_optimizer = config[model_type][f"{model_type}_actor_optimizer"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} actor optimizer set")
            else:
                logger.debug(f"actor optimizer set")
            # get optimizer params
            actor_optimizer_params = {}
            if actor_optimizer == "Adam":
                actor_optimizer_params['weight_decay'] = \
                    config[model_type][f"{model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_weight_decay']
            
            elif actor_optimizer == "Adagrad":
                actor_optimizer_params['weight_decay'] = \
                    config[model_type][f"{model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_weight_decay']
                actor_optimizer_params['lr_decay'] = \
                    config[model_type][f"{model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_lr_decay']
            
            elif actor_optimizer == "RMSprop" or actor_optimizer == "SGD":
                actor_optimizer_params['weight_decay'] = \
                    config[model_type][f"{model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_weight_decay']
                actor_optimizer_params['momentum'] = \
                    config[model_type][f"{model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_momentum']
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} actor optimizer params set")
            else:
                logger.debug(f"actor optimizer params set")
            actor_normalize_layers = config[model_type][f"{model_type}_actor_normalize_layers"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} actor normalize layers set")
            else:
                logger.debug(f"actor normalize layers set")
            # Critic
            critic_learning_rate=config[model_type][f"{model_type}_critic_learning_rate"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} critic learning rate set")
            else:
                logger.debug(f"critic learning rate set")
            critic_optimizer = config[model_type][f"{model_type}_critic_optimizer"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} critic optimizer set")
            else:
                logger.debug(f"critic optimizer set")
            critic_optimizer_params = {}
            if critic_optimizer == "Adam":
                critic_optimizer_params['weight_decay'] = \
                    config[model_type][f"{model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_weight_decay']
            
            elif critic_optimizer == "Adagrad":
                critic_optimizer_params['weight_decay'] = \
                    config[model_type][f"{model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_weight_decay']
                critic_optimizer_params['lr_decay'] = \
                    config[model_type][f"{model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_lr_decay']
            
            elif critic_optimizer == "RMSprop" or critic_optimizer == "SGD":
                critic_optimizer_params['weight_decay'] = \
                    config[model_type][f"{model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_weight_decay']
                critic_optimizer_params['momentum'] = \
                    config[model_type][f"{model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_momentum']
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} critic optimizer params set")
            else:
                logger.debug(f"critic optimizer params set")

            critic_normalize_layers = config[model_type][f"{model_type}_critic_normalize_layers"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} critic normalize layers set")
            else:
                logger.debug(f"critic normalize layers set")
            # Set device
            device = config[model_type][f"{model_type}_device"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} device set")
            else:
                logger.debug(f"device set")
            # Check if CNN layers and if so, build CNN model
            if actor_cnn_layers:
                actor_cnn_model = cnn_models.CNN(actor_cnn_layers, env)
            else:
                actor_cnn_model = None
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} actor cnn layers set: {actor_cnn_layers}")
            else:
                logger.debug(f"actor cnn layers set: {actor_cnn_layers}")

            if critic_cnn_layers:
                critic_cnn_model = cnn_models.CNN(critic_cnn_layers, env)
            else:
                critic_cnn_model = None
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} critic cnn layers set: {critic_cnn_layers}")
            else:
                logger.debug(f"critic cnn layers set: {critic_cnn_layers}")
            # # Get actor clamp value
            # clamp_output = config[model_type][f"{model_type}_actor_clamp_output"]
            # if comm is not None:
            #     logger.debug(f"{comm.Get_name()}; Rank {rank} clamp output set: {clamp_output}")
            # else:
            #     logger.debug(f"clamp output set: {clamp_output}")
            actor_model = models.ActorModel(env = env,
                                            cnn_model = actor_cnn_model,
                                            dense_layers = actor_layers,
                                            output_layer_kernel=kernels[f'actor_output_kernel'],
                                            optimizer = actor_optimizer,
                                            optimizer_params = actor_optimizer_params,
                                            learning_rate = actor_learning_rate,
                                            normalize_layers = actor_normalize_layers,
                                            # clamp_output=clamp_output,
                                            device=device,
            )
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} actor model built: {actor_model.get_config()}")
            else:
                logger.debug(f"actor model built: {actor_model.get_config()}")
            critic_model = models.CriticModel(env = env,
                                            cnn_model = critic_cnn_model,
                                            state_layers = critic_state_layers,
                                            merged_layers = critic_merged_layers,
                                            output_layer_kernel=kernels[f'critic_output_kernel'],
                                            optimizer = critic_optimizer,
                                            optimizer_params = critic_optimizer_params,
                                            learning_rate = critic_learning_rate,
                                            normalize_layers = critic_normalize_layers,
                                            device=device,
            )
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} critic model built: {critic_model.get_config()}")
            else:
                logger.debug(f"critic model built: {critic_model.get_config()}")
            # get normalizer clip value
            normalizer_clip = config[model_type][f"{model_type}_normalizer_clip"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} normalizer clip set: {normalizer_clip}")
            else:
                logger.debug(f"normalizer clip set: {normalizer_clip}")
            # get action epsilon
            action_epsilon = config[model_type][f"{model_type}_epsilon_greedy"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} action epsilon set: {action_epsilon}")
            else:
                logger.debug(f"action epsilon set: {action_epsilon}")
            # Replay buffer size
            replay_buffer_size = config[model_type][f"{model_type}_replay_buffer_size"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} replay buffer size set: {replay_buffer_size}")
            else:
                logger.debug(f"replay buffer size set: {replay_buffer_size}")
            # Save dir
            save_dir = config[model_type][f"{model_type}_save_dir"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} save dir set: {save_dir}")
            else:
                logger.debug(f"save dir set: {save_dir}")

            # create replay buffer
            replay_buffer = ReplayBuffer(env, replay_buffer_size, device=device)

            # create TD3 agent
            td3_agent= cls(
                env = env,
                actor_model = actor_model,
                critic_model = critic_model,
                discount = config[model_type][f"{model_type}_discount"],
                tau = config[model_type][f"{model_type}_tau"],
                action_epsilon = action_epsilon,
                replay_buffer = replay_buffer,
                batch_size = config[model_type][f"{model_type}_batch_size"],
                noise = helper.Noise.create_instance(config[model_type][f"{model_type}_noise"], shape=env.action_space.shape, **config[model_type][f"{model_type}_noise_{config[model_type][f'{model_type}_noise']}"], device=device),
                target_noise_stddev = config[model_type][f"{model_type}_target_action_stddev"],
                target_noise_clip = config[model_type][f"{model_type}_target_action_clip"],
                actor_update_delay = config[model_type][f"{model_type}_actor_update_delay"],
                warmup = config[model_type][f"{model_type}_warmup"],
                callbacks = callbacks,
                comm = comm,
                device = device,
            )
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} TD3 agent built: {td3_agent.get_config()}")
            else:
                logger.debug(f"TD3 agent built: {td3_agent.get_config()}")
            
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} train barrier called")
            else:
                logger.debug(f"train barrier called")

            if comm is not None:
                comm.Barrier()
                logger.debug(f"{comm.Get_name()}; Rank {rank} train barrier passed")

            td3_agent.train(
                    num_episodes=train_config['num_episodes'],
                    render=False,
                    render_freq=0,
                    )

        except Exception as e:
            logger.error(f"An error occurred: {e}", exc_info=True)

    def train(
        self, num_episodes, render: bool = False, render_freq: int = None, save_dir=None, run_number=None):
        """Trains the model for 'episodes' number of episodes."""

        # set models to train mode
        self.actor_model.train()
        self.critic_model_a.train()
        self.critic_model_b.train()

        # Update save_dir if passed
        if save_dir is not None and save_dir.split("/")[-2] != "td3":
            self.save_dir = save_dir + "/td3/"
            print(f'new save dir: {self.save_dir}')
        elif save_dir is not None and save_dir.split("/")[-2] == "td3":
            self.save_dir = save_dir
            print(f'new save dir: {self.save_dir}')
        
        if self.callbacks:
            for callback in self.callbacks:
                    self._config = callback._config(self)
            if self.use_mpi:
                if self.rank == 0:
                    for callback in self.callbacks:
                        if isinstance(callback, rl_callbacks.WandbCallback):
                            callback.on_train_begin((self.critic_model_a, self.critic_model_b, self.actor_model,), logs=self._config)
                            # logger.debug(f'{self.group}; Rank {self.rank} TD3.train on train begin callback complete')
                        else:
                            callback.on_train_begin(logs=self._config)
            else:
                for callback in self.callbacks:
                    if isinstance(callback, rl_callbacks.WandbCallback):
                        callback.on_train_begin((self.critic_model_a, self.critic_model_b, self.actor_model,), logs=self._config)
                        # logger.debug(f'TD3.train on train begin callback complete')
                    else:
                        callback.on_train_begin(logs=self._config)

        
        if self.use_mpi:
            try:
                # instantiate new environment. Only rank 0 env will render episodes if render==True
                if self.rank == 0:
                    self.env = self._initialize_env(render, render_freq, context='train')
                    # logger.debug(f'{self.group}; Rank {self.rank} initiating environment with render {render}')
                else:
                    self.env = self._initialize_env(False, 0, context='train')
                    # logger.debug(f'{self.group}; Rank {self.rank} initializing environment')
            except Exception as e:
                logger.error(f"{self.group}; Rank {self.rank} Error in TD3.train agent._initialize_env process: {e}", exc_info=True)
        
        else:
            try:
                # instantiate new environment. Only rank 0 env will render episodes if render==True
                self.env = self._initialize_env(render, render_freq, context='train')
                # logger.debug(f'initiating environment with render {render}')
            except Exception as e:
                logger.error(f"Error in TD3.train agent._initialize_env process: {e}", exc_info=True)

        # initialize step counter (for logging)
        self._step = 1
        # set best reward
        try:
            best_reward = self.env.reward_range[0]
        except:
            best_reward = -np.inf
        # instantiate list to store reward history
        reward_history = []
        # instantiate lists to store time history
        episode_time_history = []
        step_time_history = []
        learning_time_history = []
        steps_per_episode_history = []  # List to store steps per episode

        # Calculate total_steps and wait_steps
        # max_episode_steps = self.env.spec.max_episode_steps
        # total_steps = num_episodes * max_episode_steps
        # profiling_steps = (self.profiler_active_steps + self.profiler_warmup_steps) * self.profiler_repeat
        # wait_steps = (total_steps - profiling_steps) // self.profiler_repeat

        # Profile setup
        # with torch.profiler.profile(
        #     activities=[
        #         torch.profiler.ProfilerActivity.CPU,
        #         torch.profiler.ProfilerActivity.CUDA,
        #     ],
        #     schedule=torch.profiler.schedule(
        #         wait=wait_steps,
        #         warmup=self.profiler_warmup_steps,
        #         active=self.profiler_active_steps,
        #         repeat=self.profiler_repeat
        #     ),
        #     on_trace_ready=torch.profiler.tensorboard_trace_handler('./log/td3'),
        #     record_shapes=True,
        #     profile_memory=True,
        #     with_stack=True
        # ) as prof:
        for i in range(num_episodes):
            episode_start_time = time.time()
            if self.callbacks:
                if self.use_mpi:
                    if self.rank == 0:
                        for callback in self.callbacks:
                            callback.on_train_epoch_begin(epoch=self._step, logs=None)
                            # logger.debug(f'{self.group}; Rank {self.rank} TD3.train on train epoch begin callback completed')
                else:
                    for callback in self.callbacks:
                        callback.on_train_epoch_begin(epoch=self._step, logs=None)
                        # logger.debug(f'TD3.train on train epoch begin callback completed')
            # reset noise
            if type(self.noise) == helper.OUNoise:
                self.noise.reset()
            # reset environment
            state, _ = self.env.reset()
            done = False
            episode_reward = 0
            episode_steps = 0  # Initialize steps counter for the episode
            while not done:
                # run callbacks on train batch begin
                # if self.callbacks:
                #     for callback in self.callbacks:
                #         callback.on_train_step_begin(step=self._step, logs=None)
                step_start_time = time.time()
                action = self.get_action(state)
                next_state, reward, term, trunc, _ = self.env.step(action)
                # extract observation from next state if next_state is dict (robotics)
                if isinstance(next_state, dict):
                    next_state = next_state['observation']

                # store trajectory in replay buffer
                self.replay_buffer.add(state, action, reward, next_state, done)
                if term or trunc:
                    done = True
                episode_reward += reward
                state = next_state
                episode_steps += 1
                
                # check if enough samples in replay buffer and if so, learn from experiences
                if self.replay_buffer.counter > self.batch_size and self.replay_buffer.counter > self.warmup:
                    learn_time = time.time()
                    actor_loss, critic_loss = self.learn()
                    self._train_step_config["actor_loss"] = actor_loss
                    self._train_step_config["critic_loss"] = critic_loss

                    learning_time_history.append(time.time() - learn_time)
                
                step_time = time.time() - step_start_time
                step_time_history.append(step_time)

                self._train_step_config["step_reward"] = reward
                self._train_step_config["step_time"] = step_time
                
                # log to wandb if using wandb callback
                if self.callbacks:
                    if self.use_mpi:
                        # only have the main process log callback values to avoid multiple callback calls
                        if self.rank == 0:
                            for callback in self.callbacks:
                                callback.on_train_step_end(step=self._step, logs=self._train_step_config)
                                # logger.debug(f'{self.group}; Rank {self.rank} TD3.train on train step end callback completed')
                    else:
                        for callback in self.callbacks:
                            callback.on_train_step_end(step=self._step, logs=self._train_step_config)
                            # logger.debug(f'TD3.train on train step end callback completed')
                
                # prof.step()

                if not done:
                    self._step += 1
            
            episode_time = time.time() - episode_start_time
            episode_time_history.append(episode_time)
            reward_history.append(episode_reward)
            steps_per_episode_history.append(episode_steps) 
            avg_reward = np.mean(reward_history[-100:])
            avg_episode_time = np.mean(episode_time_history[-100:])
            avg_step_time = np.mean(step_time_history[-100:])
            avg_learn_time = np.mean(learning_time_history[-100:])
            avg_steps_per_episode = np.mean(steps_per_episode_history[-100:])  # Calculate average steps per episode

            self._train_episode_config['episode'] = i
            self._train_episode_config["episode_reward"] = episode_reward
            self._train_episode_config["avg_reward"] = avg_reward
            self._train_episode_config["episode_time"] = episode_time

            # check if best reward
            if avg_reward > best_reward:
                best_reward = avg_reward
                self._train_episode_config["best"] = True
                # save model
                self.save()
            else:
                self._train_episode_config["best"] = False

            if self.callbacks:
                if self.use_mpi:
                    if self.rank == 0:
                        for callback in self.callbacks:
                            callback.on_train_epoch_end(epoch=self._step, logs=self._train_episode_config)
                            # logger.debug(f'{self.group}; Rank {self.rank} TD3.train on train epoch callback completed')
                else:
                    for callback in self.callbacks:
                        callback.on_train_epoch_end(epoch=self._step, logs=self._train_episode_config)
                        # logger.debug(f'TD3.train on train epoch callback completed')

            print(f"episode {i+1}, score {episode_reward}, avg_score {avg_reward}, episode_time {episode_time:.2f}s, avg_episode_time {avg_episode_time:.2f}s, avg_step_time {avg_step_time:.6f}s, avg_learn_time {avg_learn_time:.6f}s, avg_steps_per_episode {avg_steps_per_episode:.2f}")

        if self.callbacks:
            if self.use_mpi:
                if self.rank == 0:
                    for callback in self.callbacks:
                        callback.on_train_end(logs=self._train_episode_config)
                        # logger.debug(f'{self.group}; Rank {self.rank} TD3.train on train end callback complete')
            else:
                for callback in self.callbacks:
                    callback.on_train_end(logs=self._train_episode_config)
                    # logger.debug(f'TD3.train on train end callback complete')
        # close the environment
        self.env.close()

       
    def test(self, num_episodes, render, render_freq, save_dir=None):
        """Runs a test over 'num_episodes'."""

        # set model in eval mode
        self.actor_model.eval()
        self.critic_model_a.eval()
        self.critic_model_b.eval()

        # Update save_dir if passed
        if save_dir is not None and save_dir.split("/")[-2] != "td3":
            self.save_dir = save_dir + "/td3/"
            print(f'new save dir: {self.save_dir}')
        elif save_dir is not None and save_dir.split("/")[-2] == "td3":
            self.save_dir = save_dir
            print(f'new save dir: {self.save_dir}')

        # instantiate list to store reward history
        reward_history = []
        # instantiate new environment
        self.env = self._initialize_env(render, render_freq, context='test')
        if self.callbacks:
            for callback in self.callbacks:
                callback.on_test_begin(logs=self._config)

        self._step = 1
        # set the model to calculate no gradients during evaluation
        with T.no_grad():
            for i in range(num_episodes):
                if self.callbacks:
                    for callback in self.callbacks:
                        callback.on_test_epoch_begin(epoch=self._step, logs=None) # update to pass any logs if needed
                states = []
                next_states = []
                actions = []
                rewards = []
                state, _ = self.env.reset()
                done = False
                episode_reward = 0
                while not done:
                    action = self.get_action(state, test=True)
                    next_state, reward, term, trunc, _ = self.env.step(action)
                    # extract observation from next state if next_state is dict (robotics)
                    if isinstance(next_state, dict):
                        next_state = next_state['observation']
                    # store trajectories
                    states.append(state)
                    actions.append(action)
                    next_states.append(next_state)
                    rewards.append(reward)
                    if term or trunc:
                        done = True
                    episode_reward += reward
                    state = next_state
                    self._step += 1
                reward_history.append(episode_reward)
                avg_reward = np.mean(reward_history[-100:])
                self._test_episode_config["episode_reward"] = episode_reward
                self._test_episode_config["avg_reward"] = avg_reward
                if self.callbacks:
                    for callback in self.callbacks:
                        callback.on_test_epoch_end(epoch=self._step, logs=self._test_episode_config)

                print(f"episode {i+1}, score {episode_reward}, avg_score {avg_reward}")

            if self.callbacks:
                for callback in self.callbacks:
                    callback.on_test_end(logs=self._test_episode_config)
            # close the environment
            self.env.close()


    def get_config(self):
        return {
                "agent_type": self.__class__.__name__,
                "env": serialize_env_spec(self.env.spec),
                "actor_model": self.actor_model.get_config(),
                "critic_model": self.critic_model_a.get_config(),
                "discount": self.discount,
                "tau": self.tau,
                "action_epsilon": self.action_epsilon,
                "replay_buffer": self.replay_buffer.get_config() if self.replay_buffer is not None else None,
                "batch_size": self.batch_size,
                "noise": self.noise.get_config(),
                "target_noise_stddev": self.target_noise_stddev,
                "target_noise_clip": self.target_noise_clip,
                "actor_update_delay": self.actor_update_delay,
                'normalize_inputs': self.normalize_inputs,
                # 'normalize_kwargs': self.normalize_kwargs,
                'normalizer_clip': self.normalizer_clip,
                'normalizer_eps': self.normalizer_eps,
                'warmup': self.warmup,
                "callbacks": [callback.get_config() for callback in self.callbacks if self.callbacks is not None],
                "save_dir": self.save_dir,
                "use_mpi": self.use_mpi,
                "device": self.device,
            }


    def save(self, save_dir=None):
        """Saves the model."""

        # Change self.save_dir if save_dir
        # if save_dir is not None:
        #     self.save_dir = save_dir + "/ddpg/"

        config = self.get_config()

        # makes directory if it doesn't exist
        os.makedirs(self.save_dir, exist_ok=True)

        # writes and saves JSON file of DDPG agent config
        with open(self.save_dir + "/config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, cls=CustomJSONEncoder)

        # saves policy and value model
        self.actor_model.save(self.save_dir)
        self.critic_model_a.save(self.save_dir)
        self.critic_model_b.save(self.save_dir)

        if self.normalize_inputs:
            self.state_normalizer.save_state(self.save_dir + "state_normalizer.npz")

        # if wandb callback, save wandb config
        # if self._wandb:
        #     for callback in self.callbacks:
        #         if isinstance(callback, rl_callbacks.WandbCallback):
        #             callback.save(self.save_dir + "/wandb_config.json")


    @classmethod
    def load(cls, config, load_weights=True):
        """Loads the model."""
        # # load reinforce agent config
        # with open(
        #     Path(folder).joinpath(Path("obj_config.json")), "r", encoding="utf-8"
        # ) as f:
        #     config = json.load(f)

        # create EnvSpec from config
        env_spec_json = json.dumps(config["env"])
        env_spec = gym.envs.registration.EnvSpec.from_json(env_spec_json)

        # load policy model
        actor_model = models.ActorModel.load(config['save_dir'], load_weights)
        # load value model
        critic_model = models.CriticModel.load(config['save_dir'], load_weights)
        # load replay buffer if not None
        if config['replay_buffer'] is not None:
            config['replay_buffer']['config']['env'] = gym.make(env_spec)
            replay_buffer = helper.ReplayBuffer(**config["replay_buffer"]["config"])
        else:
            replay_buffer = None
        # load noise
        noise = helper.Noise.create_instance(config["noise"]["class_name"], **config["noise"]["config"])
        # load callbacks
        callbacks = [rl_callbacks.load(callback_info['class_name'], callback_info['config']) for callback_info in config['callbacks']]

        # return TD3 agent
        agent = cls(
            gym.make(env_spec),
            actor_model = actor_model,
            critic_model = critic_model,
            discount=config["discount"],
            tau=config["tau"],
            action_epsilon=config["action_epsilon"],
            replay_buffer=replay_buffer,
            batch_size=config["batch_size"],
            noise=noise,
            target_noise_stddev = config['target_noise_stddev'],
            target_noise_clip = config['target_noise_clip'],
            actor_update_delay = config['actor_update_delay'],
            normalize_inputs = config['normalize_inputs'],
            normalizer_clip = config['normalizer_clip'],
            warmup = config['warmup'],
            callbacks=callbacks,
            save_dir=config["save_dir"],
            device=config["device"],
        )

        if agent.normalize_inputs:
            agent.state_normalizer = helper.Normalizer.load_state(config['save_dir'] + "state_normalizer.npz")

        return agent


class HER(Agent):

    def __init__(self,
                 agent:Agent,
                 strategy:str='final',
                 tolerance:float=0.5,
                 num_goals:int=4,
                 desired_goal:callable=None,
                 achieved_goal:callable=None,
                 reward_fn:callable=None,
                 normalizer_clip:float=5.0,
                 normalizer_eps:float=0.01,
                 replay_buffer_size:int=1_000_000,
                 device:str='cuda',
                 save_dir: str = "models",
                 comm=None):
        super().__init__()
        try:
            if comm is not None:
                self.comm = comm
                self.rank = comm.Get_rank()
            else:
                self.comm = MPI.COMM_WORLD
                self.rank = MPI.COMM_WORLD.Get_rank()
            self.group = self.comm.Get_name()
            self.agent = agent
            self.strategy = strategy
            self.tolerance = tolerance
            self.num_goals = num_goals
            self.desired_goal_func = desired_goal
            self.achieved_goal_func = achieved_goal
            self.reward_fn = reward_fn
            self.normalizer_clip = normalizer_clip
            self.normalizer_eps = normalizer_eps
            self.replay_buffer_size = replay_buffer_size
            self.device = device
            if save_dir is not None and "/her/" not in save_dir:
                self.save_dir = save_dir + "/her/"
                # change save dir of agent to be in save dir of HER
                agent_name = self.agent.save_dir.split("/")[-2]
                #DEBUG
                # print(f'agent name: {agent_name}')
                self.agent.save_dir = self.save_dir + agent_name + "/"
                # print(f'new save dir: {self.agent.save_dir}')
            elif save_dir is not None and "her" in save_dir:
                self.save_dir = save_dir
                # change save dir of agent to be in save dir of HER
                agent_name = self.agent.save_dir.split("/")[-2]
                self.agent.save_dir = self.save_dir + agent_name + "/"

            # update callback configs b/c changed save_dir
            if self.agent.callbacks:
                for callback in self.agent.callbacks:
                    self.agent._config = callback._config(self.agent)

            # Instantiate self.num_workers as placeholder (set in train)
            self.num_workers = None
            # logger.debug(f'rank {self.rank} attributes set')
        except Exception as e:
            logger.error(f"{self.group} rank {self.rank} attribute set failed: {e}", exc_info=True)

        ## SET INTERNAL ATTRIBUTES ##
        try:
            # Observation space
            if isinstance(self.agent.env.observation_space, gym.spaces.dict.Dict):
                self._obs_space_shape = self.agent.env.observation_space['observation'].shape
            else:
                self._obs_space_shape = self.agent.env.observation_space.shape
            
            # Reset state environment to get goal shape of env
            _,_ = self.agent.env.reset()
            
            # Get goal shape to pass to agent to initialize normalizers
            self._goal_shape = self.desired_goal_func(self.agent.env).shape
            
            # Turn use her flag on in agent
            self.agent._init_her()

            # if agent env is gymnasium-robotics env, should set distance-threshold
            # attr to tolerance
            if self.agent.env.get_wrapper_attr("distance_threshold"):
                self.agent.env.__setattr__("distance_threshold", self.tolerance)
            # logger.debug(f"rank {self.rank} internal attributes set")
        except Exception as e:
            logger.error(f"{self.group} rank {self.rank} failed to set internal attributes: {e}", exc_info=True)

        ## T.DIST for CUDA ##
        # Capture the actor and critic state_dicts to pass to worker agents models
        # self._actor_params = [value.cpu().numpy() for key, value in self.agent.actor_model.state_dict().items()]
        # self._critic_params = [value.cpu().numpy() for key, value in self.agent.critic_model.state_dict().items()]
        # print actor and critic state dicts
        # print("HER actor params")
        # print(self._actor_params)
        # print("HER critic params")
        # print(self._critic_params)
        
        # Create a Manager to manage the sharing and locking of shared object
        # manager = Manager()
        # print('creating shared replay buffer')
        # Instantiate replay buffer
        # self.replay_buffer = SharedReplayBuffer(manager=None,
                                                # env=self.agent.env,
                                                # buffer_size=self.replay_buffer_size,
                                                # goal_shape=self._goal_shape)


        # print('creating shared normalizers')
        # Instantiate state and goal normalizers
        # self.state_normalizer = SharedNormalizer(manager=None,
        #                                          size=self._obs_space_shape,
        #                                          eps=self.normalizer_eps,
        #                                          clip_range=self.normalizer_clip)
        
        # self.goal_normalizer = SharedNormalizer(manager=None,
        #                                         size=self._goal_shape,
        #                                         eps=self.normalizer_eps,
        #                                         clip_range=self.normalizer_clip)
        
        ## MPI for CPU ##
        try:
            #sync networks
            helper.sync_networks(self.agent.actor_model, self.comm)
            helper.sync_networks(self.agent.critic_model, self.comm)
            helper.sync_networks(self.agent.target_actor_model, self.comm)
            helper.sync_networks(self.agent.target_critic_model, self.comm)
            # logger.debug(f"rank {self.rank} networks synced")
        except Exception as e:
            logger.error(f"{self.group} rank {self.rank} failed to sync networks: {e}", exc_info=True)

        # Instantiate replay buffer
        try:
            self.replay_buffer = ReplayBuffer(env=self.agent.env,
                                            buffer_size=self.replay_buffer_size,
                                            goal_shape=self._goal_shape)
            # logger.debug(f"rank {self.rank} replay buffer instantiated")
        except Exception as e:
            logger.error(f"{self.group} rank {self.rank} error instantiating replay buffer: {e}", exc_info=True)

        # Instantiate state and goal normalizers
        try:
            self.state_normalizer = Normalizer(size=self._obs_space_shape,
                                            eps=self.normalizer_eps,
                                            clip_range=self.normalizer_clip)
            
            self.goal_normalizer = Normalizer(size=self._goal_shape,
                                            eps=self.normalizer_eps,
                                            clip_range=self.normalizer_clip)
            # logger.debug(f"rank {self.rank} normalizers instantiated")
        except Exception as e:
            logger.error(f"{self.group} rank {self.rank} error instantiating normalizers: {e}", exc_info=True)


        
    @classmethod
    def sweep_train(
        cls,
        config, # wandb.config,
        train_config,
        env_spec,
        callbacks,
        run_number,
        comm=None,
    ):
        """Builds and trains agents from sweep configs. Works with MPI"""
        rank = MPI.COMM_WORLD.rank

        if comm is not None:
            logger.debug(f"Rank {rank} comm detected")
            rank = comm.Get_rank()
            logger.debug(f"Global rank {MPI.COMM_WORLD.Get_rank()} in {comm.Get_name()} set to comm rank {rank}")

            logger.debug(f"init_sweep fired: global rank {MPI.COMM_WORLD.rank}, group rank {rank}, {comm.Get_name()}")
        else:
            logger.debug(f"init_sweep fired: global rank")
        try:
            # rank = MPI.COMM_WORLD.rank
            # Instantiate her variable 
            her = None
            # Instantiate env from env_spec
            env = gym.make(gym.envs.registration.EnvSpec.from_json(env_spec))
            # agent_config_path = f'sweep/agent_config_{run_number}.json'
            # logger.debug(f"rank {rank} agent config path: {agent_config_path}")
            model_type = list(config.keys())[0]
            # config = wandb.config
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} train config: {train_config}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} env spec id: {env.spec.id}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} callbacks: {callbacks}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} run number: {run_number}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} config set: {config}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} model type: {model_type}")
                # Only primary process (rank 0) calls wandb.init() to build agent and log data
            else:
                logger.debug(f"train config: {train_config}")
                logger.debug(f"env spec id: {env.spec.id}")
                logger.debug(f"callbacks: {callbacks}")
                logger.debug(f"run number: {run_number}")
                logger.debug(f"config set: {config}")
                logger.debug(f"model type: {model_type}")

            actor_cnn_layers, critic_cnn_layers, actor_layers, critic_state_layers, critic_merged_layers, kernels = wandb_support.build_layers(config)
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} layers built")
            else:
                logger.debug(f"layers built")
            # Actor
            actor_learning_rate=config[model_type][f"{model_type}_actor_learning_rate"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} actor learning rate set")
            else:
                logger.debug(f"actor learning rate set")
            actor_optimizer = config[model_type][f"{model_type}_actor_optimizer"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} actor optimizer set")
            else:
                logger.debug(f"actor optimizer set")
            # get optimizer params
            actor_optimizer_params = {}
            if actor_optimizer == "Adam":
                actor_optimizer_params['weight_decay'] = \
                    config[model_type][f"{model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_weight_decay']
            
            elif actor_optimizer == "Adagrad":
                actor_optimizer_params['weight_decay'] = \
                    config[model_type][f"{model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_weight_decay']
                actor_optimizer_params['lr_decay'] = \
                    config[model_type][f"{model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_lr_decay']
            
            elif actor_optimizer == "RMSprop" or actor_optimizer == "SGD":
                actor_optimizer_params['weight_decay'] = \
                    config[model_type][f"{model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_weight_decay']
                actor_optimizer_params['momentum'] = \
                    config[model_type][f"{model_type}_actor_optimizer_{actor_optimizer}_options"][f'{actor_optimizer}_momentum']

            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} actor optimizer params set")
            else:
                logger.debug(f"actor optimizer params set")
            actor_normalize_layers = config[model_type][f"{model_type}_actor_normalize_layers"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} actor normalize layers set")
            else:
                logger.debug(f"actor normalize layers set")
            # Critic
            critic_learning_rate=config[model_type][f"{model_type}_critic_learning_rate"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} critic learning rate set")
            else:
                logger.debug(f"critic learning rate set")
            critic_optimizer = config[model_type][f"{model_type}_critic_optimizer"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} critic optimizer set")
            else:
                logger.debug(f"critic optimizer set")
            critic_optimizer_params = {}
            if critic_optimizer == "Adam":
                critic_optimizer_params['weight_decay'] = \
                    config[model_type][f"{model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_weight_decay']
            
            elif critic_optimizer == "Adagrad":
                critic_optimizer_params['weight_decay'] = \
                    config[model_type][f"{model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_weight_decay']
                critic_optimizer_params['lr_decay'] = \
                    config[model_type][f"{model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_lr_decay']
            
            elif critic_optimizer == "RMSprop" or critic_optimizer == "SGD":
                critic_optimizer_params['weight_decay'] = \
                    config[model_type][f"{model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_weight_decay']
                critic_optimizer_params['momentum'] = \
                    config[model_type][f"{model_type}_critic_optimizer_{critic_optimizer}_options"][f'{critic_optimizer}_momentum']
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} critic optimizer params set")
            else:
                logger.debug(f"critic optimizer params set")

            critic_normalize_layers = config[model_type][f"{model_type}_critic_normalize_layers"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} critic normalize layers set")
            else:
                logger.debug(f"critic normalize layers set")
            # Set device
            device = config[model_type][f"{model_type}_device"]
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} device set")
            else:
                logger.debug(f"device set")
            # Check if CNN layers and if so, build CNN model
            if actor_cnn_layers:
                actor_cnn_model = cnn_models.CNN(actor_cnn_layers, env)
            else:
                actor_cnn_model = None
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} actor cnn layers set: {actor_cnn_layers}")
            else:
                logger.debug(f"actor cnn layers set: {actor_cnn_layers}")

            if critic_cnn_layers:
                critic_cnn_model = cnn_models.CNN(critic_cnn_layers, env)
            else:
                critic_cnn_model = None
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} critic cnn layers set: {critic_cnn_layers}")
            else:
                logger.debug(f"critic cnn layers set: {critic_cnn_layers}")
            # get desired, achieved, reward func for env
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} second call env.spec: {env.spec.id}")
            else:
                logger.debug(f"second call env.spec: {env.spec.id}")
            desired_goal_func, achieved_goal_func, reward_func = gym_helper.get_her_goal_functions(env)
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} goal function set")
            else:
                logger.debug(f"goal function set")
            # Reset env state to initiate state to detect correct goal shape
            _,_ = env.reset()
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} env reset")
            else:
                logger.debug(f"env reset")
            goal_shape = desired_goal_func(env).shape
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} goal shape set: {goal_shape}")
            else:
                logger.debug(f"goal shape set: {goal_shape}")
            # Get actor clamp value
            # clamp_output = config[model_type][f"{model_type}_actor_clamp_output"]
            # logger.debug(f"{comm.Get_name()}; Rank {rank} clamp output set: {clamp_output}")
            actor_model = models.ActorModel(env = env,
                                            cnn_model = actor_cnn_model,
                                            dense_layers = actor_layers,
                                            output_layer_kernel=kernels[f'actor_output_kernel'],
                                            goal_shape=goal_shape,
                                            optimizer = actor_optimizer,
                                            optimizer_params = actor_optimizer_params,
                                            learning_rate = actor_learning_rate,
                                            normalize_layers = actor_normalize_layers,
                                            # clamp_output=clamp_output,
                                            device=device,
            )
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} actor model built: {actor_model.get_config()}")
            else:
                logger.debug(f"actor model built: {actor_model.get_config()}")
            critic_model = models.CriticModel(env = env,
                                            cnn_model = critic_cnn_model,
                                            state_layers = critic_state_layers,
                                            merged_layers = critic_merged_layers,
                                            output_layer_kernel=kernels[f'critic_output_kernel'],
                                            goal_shape=goal_shape,
                                            optimizer = critic_optimizer,
                                            optimizer_params = critic_optimizer_params,
                                            learning_rate = critic_learning_rate,
                                            normalize_layers = critic_normalize_layers,
                                            device=device,
            )
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} critic model built: {critic_model.get_config()}")
            else:
                logger.debug(f"critic model built: {critic_model.get_config()}")
            # get goal metrics
            strategy = config[model_type][f"{model_type}_goal_strategy"]
            
            tolerance = config[model_type][f"{model_type}_goal_tolerance"]
            
            num_goals = config[model_type][f"{model_type}_num_goals"]
            
            # get normalizer clip value
            normalizer_clip = config[model_type][f"{model_type}_normalizer_clip"]
            
            # get action epsilon
            action_epsilon = config[model_type][f"{model_type}_epsilon_greedy"]
            
            # Replay buffer size
            replay_buffer_size = config[model_type][f"{model_type}_replay_buffer_size"]
            
            # Save dir
            save_dir = config[model_type][f"{model_type}_save_dir"]
            
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} strategy set: {strategy}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} tolerance set: {tolerance}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} num goals set: {num_goals}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} normalizer clip set: {normalizer_clip}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} action epsilon set: {action_epsilon}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} replay buffer size set: {replay_buffer_size}")
                logger.debug(f"{comm.Get_name()}; Rank {rank} save dir set: {save_dir}")
            else:
                logger.debug(f"strategy set: {strategy}")
                logger.debug(f"tolerance set: {tolerance}")
                logger.debug(f"num goals set: {num_goals}")
                logger.debug(f"normalizer clip set: {normalizer_clip}")
                logger.debug(f"action epsilon set: {action_epsilon}")
                logger.debug(f"replay buffer size set: {replay_buffer_size}")
                logger.debug(f"save dir set: {save_dir}")
            
            
            if model_type == "HER_DDPG":
                ddpg_agent= DDPG(
                    env = env,
                    actor_model = actor_model,
                    critic_model = critic_model,
                    discount = config[model_type][f"{model_type}_discount"],
                    tau = config[model_type][f"{model_type}_tau"],
                    action_epsilon = action_epsilon,
                    replay_buffer = None,
                    batch_size = config[model_type][f"{model_type}_batch_size"],
                    noise = helper.Noise.create_instance(config[model_type][f"{model_type}_noise"], shape=env.action_space.shape, **config[model_type][f"{model_type}_noise_{config[model_type][f'{model_type}_noise']}"], device=device),
                    callbacks = callbacks,
                    comm = comm
                )
                if comm is not None:
                    logger.debug(f"{comm.Get_name()}; Rank {rank} ddpg agent built: {ddpg_agent.get_config()}")
                else:
                    logger.debug(f"ddpg agent built: {ddpg_agent.get_config()}")

            elif model_type == "HER_TD3":
                ddpg_agent= TD3(
                    env = env,
                    actor_model = actor_model,
                    critic_model = critic_model,
                    discount = config[model_type][f"{model_type}_discount"],
                    tau = config[model_type][f"{model_type}_tau"],
                    action_epsilon = action_epsilon,
                    replay_buffer = None,
                    batch_size = config[model_type][f"{model_type}_batch_size"],
                    noise = helper.Noise.create_instance(config[model_type][f"{model_type}_noise"], shape=env.action_space.shape, **config[model_type][f"{model_type}_noise_{config[model_type][f'{model_type}_noise']}"], device=device),
                    target_noise_stddev= config[model_type][f"{model_type}_target_action_stddev"],
                    target_noise_clip= config[model_type][f"{model_type}_target_action_clip"],
                    actor_update_delay= config[model_type][f"{model_type}_actor_update_delay"],
                    callbacks = callbacks,
                    comm = comm
                )
                if comm is not None:
                    logger.debug(f"{comm.Get_name()}; Rank {rank} ddpg agent built: {ddpg_agent.get_config()}")
                else:
                    logger.debug(f"ddpg agent built: {ddpg_agent.get_config()}")

            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} build barrier called")
                comm.Barrier()
                logger.debug(f"{comm.Get_name()}; Rank {rank} build barrier passed")

            her = cls(
                agent = ddpg_agent,
                strategy = strategy,
                tolerance = tolerance,
                num_goals = num_goals,
                desired_goal = desired_goal_func,
                achieved_goal = achieved_goal_func,
                reward_fn = reward_func,
                normalizer_clip = normalizer_clip,
                replay_buffer_size = replay_buffer_size,
                device = device,
                save_dir = save_dir,
                comm = comm
            )
            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} her agent built: {her.get_config()}")
            else:
                logger.debug(f"her agent built: {her.get_config()}")

            if comm is not None:
                logger.debug(f"{comm.Get_name()}; Rank {rank} train barrier called")
                comm.Barrier()
                logger.debug(f"{comm.Get_name()}; Rank {rank} train barrier passed")

            her.train(
                    num_epochs=train_config['num_epochs'],
                    num_cycles=train_config['num_cycles'],
                    num_episodes=train_config['num_episodes'],
                    num_updates=train_config['num_updates'],
                    render=False,
                    render_freq=0,
                    )

        except Exception as e:
            logger.error(f"An error occurred: {e}", exc_info=True)

    def train(self, num_epochs:int, num_cycles:int, num_episodes:int, num_updates:int,
              render:bool, render_freq:int, save_dir=None):
        try:
            logger.debug(f"{self.group}; Rank {self.rank} train fired")

            if save_dir is not None and len(save_dir.split("/")) >= 2:
                if save_dir.split("/")[-2] != "her":
                    self.save_dir = save_dir + "/her/"
                    # change save dir of agent to be in save dir of HER
                    agent_name = self.agent.save_dir.split("/")[-2]
                    self.agent.save_dir = self.save_dir + agent_name + "/"
            elif save_dir is not None and len(save_dir.split("/")) >= 2:
                if save_dir.split("/")[-2] == "her":
                    self.save_dir = save_dir
                    # change save dir of agent to be in save dir of HER
                    agent_name = self.agent.save_dir.split("/")[-2]
                    self.agent.save_dir = self.save_dir + agent_name + "/"
            
            # set models to train mode
            self.agent.actor_model.train()
            self.agent.critic_model.train()
            self.agent.target_actor_model.train()
            self.agent.target_critic_model.train()

            # Add train config setting to wandb config
            self.agent._config['num_epochs'] = num_epochs
            self.agent._config['num_cycles'] = num_cycles
            self.agent._config['num_episode'] =num_episodes
            self.agent._config['num_updates'] = num_updates
            self.agent._config['tolerance'] = self.tolerance
            logger.debug(f"{self.group}; Rank {self.rank} HER.train: train config added to wandb config")

            # Check if MPI is active with more than one worker
            # mpi_active = MPI.COMM_WORLD.Get_size() > 1
            # logger.debug(f"{self.group}; Rank {self.rank} HER.train mpi active set: {mpi_active}")

            if self.agent.callbacks:
                # if mpi_active:
                if self.rank == 0:
                    for callback in self.agent.callbacks:
                        if isinstance(callback, rl_callbacks.WandbCallback):
                            callback.on_train_begin((self.agent.critic_model, self.agent.actor_model,), logs=self.agent._config)
                            logger.debug(f'{self.group}; Rank {self.rank} HER.train on train begin callback complete')
                        else:
                            callback.on_train_begin(logs=self.agent._config)
                # else:
                # # MPI is not active or there is only one worker
                #     for callback in self.agent.callbacks:
                #         if isinstance(callback, rl_callbacks.WandbCallback):
                #             callback.on_train_begin((self.agent.critic_model, self.agent.actor_model,), logs=self.agent._config)
                #         else:
                #             callback.on_train_begin(logs=self.agent._config)
            try:
                # if mpi_active:
                # instantiate new environment. Only rank 0 env will render episodes if render==True
                if self.rank == 0:
                    self.agent.env = self.agent._initialize_env(render, render_freq, context='train')
                    logger.debug(f'{self.group}; Rank {self.rank} initiating environment with render {render}')
                else:
                    self.agent.env = self.agent._initialize_env(False, 0, context='train')
                    logger.debug(f'{self.group}; Rank {self.rank} initializing environment')
                # else:
                #     self.agent.env = self.agent._initialize_env(render, render_freq, context='train')
            except Exception as e:
                logger.error(f"{self.group}; Rank {self.rank} Error in HER.train agent._initialize_env process: {e}", exc_info=True)
            
            # initialize step counter (for logging)
            self._step = 0
            self._episode = 0
            self._cycle = 0
            self._successes = 0.0
            # set best reward
            # best_reward = self.agent.env.reward_range[0] # substitute with -np.inf
            best_reward = -np.inf
            # instantiate list to store reward history
            reward_history = []
            # instantiate lists to store time history
            episode_time_history = []
            step_time_history = []
            learning_time_history = []
            steps_per_episode_history = []  # List to store steps per episode
            for epoch in range(num_epochs):
                logger.debug(f'{self.group}; Rank {self.rank} HER.train starting epoch {epoch+1}')
                for cycle in range(num_cycles):
                    self._cycle += 1
                    # print(f'agent rank {rank} starting cycle {cycle_counter}')
                    for episode in range(num_episodes):
                        logger.debug(f'{self.group}; Rank {self.rank} episode: {episode}')
                        self._episode += 1
                        if self.agent.callbacks:
                            # if mpi_active:
                            if self.rank == 0:
                                for callback in self.agent.callbacks:
                                    callback.on_train_epoch_begin(epoch=self._step, logs=None)
                                    logger.debug(f'{self.group}; Rank {self.rank} HER.train on train epoch begin callback completed')
                            # else:
                            #     for callback in self.agent.callbacks:
                            #         callback.on_train_epoch_begin(epoch=step_counter, logs=None)

                        episode_start_time = time.time()
                        
                        # reset noise
                        if type(self.agent.noise) == helper.OUNoise:
                            self.agent.noise.reset()

                        # reset environment
                        state, _ = self.agent.env.reset()
                        # print(f'state: {state}' )
                        if isinstance(state, dict): # if state is a dict, extract observation (robotics)
                            state = state["observation"]
                        
                        # instantiate empty lists to store current episode trajectory
                        states, actions, next_states, dones, state_achieved_goals, \
                        next_state_achieved_goals, desired_goals = [], [], [], [], [], [], []
                        
                        # set desired goal
                        desired_goal = self.desired_goal_func(self.agent.env)
                        # print(f'desired goal: {desired_goal}')
                        
                        # set achieved goal
                        state_achieved_goal = self.achieved_goal_func(self.agent.env)

                        # add initial state and goals to local normalizer stats
                        self.state_normalizer.update_local_stats(state)
                        self.goal_normalizer.update_local_stats(desired_goal)
                        self.goal_normalizer.update_local_stats(state_achieved_goal)
                        
                        # set done flag
                        done = False
                        
                        # reset episode reward to 0
                        episode_reward = 0
                        
                        # reset steps counter for the episode
                        episode_steps = 0

                        while not done:
                            # increase step counter
                            self._step += 1
                            
                            # start step timer
                            step_start_time = time.time()
                            
                            # get action
                            action = self.agent.get_action(state, desired_goal, grad=True,
                                                    state_normalizer=self.state_normalizer,
                                                    goal_normalizer=self.goal_normalizer)
                            
                            # take action
                            next_state, reward, term, trunc, _ = self.agent.env.step(action)
                            
                            # extract observation from next state if next_state is dict (robotics)
                            if isinstance(next_state, dict):
                                next_state = next_state["observation"]
                            
                            # calculate and log step time
                            step_time = time.time() - step_start_time
                            step_time_history.append(step_time)
                            
                            # get next state achieved goal
                            next_state_achieved_goal = self.achieved_goal_func(self.agent.env)
                            
                            # add next state and next state achieved goal to normalizers
                            self.state_normalizer.update_local_stats(next_state)
                            self.goal_normalizer.update_local_stats(next_state_achieved_goal)
                            
                            # calculate distance from achieved goal to desired goal
                            distance_to_goal = np.linalg.norm(desired_goal - next_state_achieved_goal)
                            
                            # store distance in step config to send to wandb
                            self.agent._train_step_config["goal_distance"] = distance_to_goal
                            
                            # store trajectory in replay buffer (non normalized!)
                            self.replay_buffer.add(state, action, reward, next_state, done,\
                                                            state_achieved_goal, next_state_achieved_goal, desired_goal)
                            
                            # append step state, action, next state, and goals to respective lists
                            states.append(state)
                            actions.append(action)
                            next_states.append(next_state)
                            dones.append(done)
                            state_achieved_goals.append(state_achieved_goal)
                            next_state_achieved_goals.append(next_state_achieved_goal)
                            desired_goals.append(desired_goal)

                            # add to episode reward and increment steps counter
                            episode_reward += reward
                            episode_steps += 1
                            # update state and state achieved goal
                            state = next_state
                            state_achieved_goal = next_state_achieved_goal
                            # update done flag
                            if term or trunc:
                                done = True
                            # log step metrics
                            self.agent._train_step_config["step_reward"] = reward
                            self.agent._train_step_config["step_time"] = step_time
                            
                            
                            # log to wandb if using wandb callback
                            if self.agent.callbacks:
                                # only have the main process log callback values to avoid multiple callback calls
                                # if mpi_active:
                                if self.rank == 0:
                                    for callback in self.agent.callbacks:
                                        callback.on_train_step_end(step=self._step, logs=self.agent._train_step_config)
                                        logger.debug(f'{self.group}; Rank {self.rank} HER.train on train step end callback completed')
                                # else:
                                #     for callback in self.agent.callbacks:
                                #         callback.on_train_step_end(step=step_counter, logs=self.agent._train_step_config)

                        # calculate success rate
                        success = (distance_to_goal <= self.tolerance).astype(np.float32)
                        self._successes += success
                        success_perc = self._successes / self._episode
                        # store success rate to train episode config
                        self.agent._train_episode_config["success_rate"] = success_perc

                        # Update global normalizer stats (main process only)
                        self.state_normalizer.update_global_stats()
                        self.goal_normalizer.update_global_stats()
                        
                        # package episode states, actions, next states, and goals into trajectory tuple
                        trajectory = (states, actions, next_states, dones, state_achieved_goals, next_state_achieved_goals, desired_goals)

                        # store hindsight experience replay trajectory using current episode trajectory and goal strategy
                        self.store_hindsight_trajectory(trajectory)
                            
                        # check if enough samples in replay buffer and if so, learn from experiences
                        if self.replay_buffer.counter > self.agent.batch_size:
                            learn_time = time.time()
                            for _ in range(num_updates):
                                actor_loss, critic_loss = self.agent.learn(replay_buffer=self.replay_buffer,
                                                                    state_normalizer=self.state_normalizer,
                                                                    goal_normalizer=self.goal_normalizer,
                                                                    )
                            self.agent._train_episode_config["actor_loss"] = actor_loss
                            self.agent._train_episode_config["critic_loss"] = critic_loss
                    
                            learning_time_history.append(time.time() - learn_time)
                        
                        episode_time = time.time() - episode_start_time
                        episode_time_history.append(episode_time)
                        reward_history.append(episode_reward)
                        steps_per_episode_history.append(episode_steps) 
                        avg_reward = np.mean(reward_history[-100:])
                        avg_episode_time = np.mean(episode_time_history[-100:])
                        avg_step_time = np.mean(step_time_history[-100:])
                        avg_learn_time = np.mean(learning_time_history[-100:])
                        avg_steps_per_episode = np.mean(steps_per_episode_history[-100:])  # Calculate average steps per episode

                        self.agent._train_episode_config['episode'] = episode
                        self.agent._train_episode_config["episode_reward"] = episode_reward
                        self.agent._train_episode_config["avg_reward"] = avg_reward
                        self.agent._train_episode_config["episode_time"] = episode_time
                        
                        # check if best reward and save model if it is
                        if avg_reward > best_reward:
                            best = True
                            best_reward = avg_reward
                            # save model
                            self.save()
                        else:
                            best = False
                        
                        if self.agent.callbacks:
                            self.agent._train_episode_config["best"] = best
                            # if mpi_active:
                            if self.rank == 0:
                                for callback in self.agent.callbacks:
                                    callback.on_train_epoch_end(epoch=self._step, logs=self.agent._train_episode_config)
                                    logger.debug(f'{self.group}; Rank {self.rank} HER.train on train epoch callback completed')
                            # else:
                            #     for callback in self.agent.callbacks:
                            #         callback.on_train_epoch_end(epoch=step_counter, logs=self.agent._train_episode_config)

                    # perform soft update on target networks
                    try:
                        self.agent.soft_update(self.agent.actor_model, self.agent.target_actor_model)
                        self.agent.soft_update(self.agent.critic_model, self.agent.target_critic_model)
                        logger.debug(f"{self.group}; Rank {self.rank} HER.train target network soft update complete")
                    except Exception as e:
                        logger.error(f"{self.group}; Rank {self.rank} Error in HER.train target network soft update process: {e}", exc_info=True)

                if self.rank == 0: # only use main process
                    logger.info(f"{self.group}; epoch {epoch} cycle {self._cycle} episode {self._episode}, success percentage {success_perc}, reward {episode_reward}, avg reward {avg_reward}, avg episode time {avg_episode_time:.2f}s")

            if self.agent.callbacks:
                # if mpi_active:
                if self.rank == 0:
                    for callback in self.agent.callbacks:
                        callback.on_train_end(logs=self.agent._train_episode_config)
                        logger.debug(f'{self.group}; Rank {self.rank} HER.train on train end callback complete')
                # else:
                #     for callback in self.agent.callbacks:
                #         callback.on_train_end(logs=self.agent._train_episode_config)
            # close the environment
            self.agent.env.close()
        except Exception as e:
            logger.error(f"{self.group}; Rank {self.rank} Error during train process: {e}", exc_info=True)
    
    
    # def train_worker(self, rank, agent:Agent, actor_params, critic_params, epochs:int, 
    #                  num_cycles:int, num_episodes:int, num_updates:int, num_workers:int,
    #                  render:bool, render_freq:int, save_dir:str):
    #     # Register work to distribution group
    #     self.setup_worker(rank)

    #     # Copy parameters from main agent models to worker models
    #     self.numpy_to_model(agent.actor_model, self._actor_params)
    #     self.numpy_to_model(agent.target_actor_model, self._actor_params)
    #     self.numpy_to_model(agent.critic_model, self._critic_params)
    #     self.numpy_to_model(agent.target_critic_model, self._critic_params)

    #     # Reset the noise to self.agent.noise b/c noise not copying to worker correctly
    #     # agent.noise = self.agent.noise

    #     #DEBUG
    #     # config of actor and critic networks
    #     # print('actor model')
    #     # print(agent.actor_model.get_config())
    #     # print(agent.actor_model)
    #     # print('')
    #     # print('critic model')
    #     # print((agent.critic_model.get_config()))
    #     # print(agent.critic_model)
    #     # print('agent config')
    #     # print(agent.get_config())
    #     # print(f'agent use her: {agent._use_her}')
    #     # print('agent noise config')
    #     # print(agent.noise.get_config())
    #     # print(f'agent noise mean: {agent.noise.mean}')
    #     # print(f'agent noise stddev: {agent.noise.stddev}')
    #     # Print each agent parameters
    #     # print(f'agent {rank} actor model params:')
    #     # print([param for param in agent.actor_model.parameters()])
    #     # print(f'agent {rank} critic model params:')
    #     # print([param for param in agent.critic_model.parameters()])
    #     # Print device of each model
    #     # for param in agent.actor_model.parameters():
    #     #     print(f"Actor Parameter is on device: {param.device}")
    #     # for param in agent.critic_model.parameters():
    #     #     print(f"Critic Parameter is on device: {param.device}")

        

    #     # T.cuda.set_device(self.device)
    #     # agent.actor_model.to(self.device)
    #     # agent.critic_model.to(self.device)
    #     # set models to train mode
    #     agent.actor_model.train()
    #     agent.critic_model.train()

    #     # Add train config setting to wandb config
    #     agent._config['num workers'] = num_workers
    #     agent._config['num epochs'] = epochs
    #     agent._config['num cycles'] = num_cycles
    #     agent._config['num episode'] =num_episodes
    #     agent._config['num updates'] = num_updates
    #     agent._config['tolerance'] = self.tolerance

    #     if agent.callbacks:
    #         if MPI.COMM_WORLD.Get_rank() == 0:
    #             # print(f'agent rank {rank} firing callback')
    #             for callback in agent.callbacks:
    #                 if isinstance(callback, rl_callbacks.WandbCallback):
    #                     # print(f'agent {rank} config:')
    #                     # print(agent._config)
    #                     callback.on_train_begin((agent.critic_model, agent.actor_model,), logs=agent._config)
    #                     # print('on train begin callback fired')
    #                 else:
    #                     callback.on_train_begin(logs=agent._config)

    #     # instantiate new environment. Only rank 0 env will render episodes if render==True
    #     if rank == 0:
    #         agent.env = agent._initialize_env(render, render_freq, context='train')
    #         # print(f'agent rank {rank} initiating environment with render {render}')
    #     else:
    #         agent.env = agent._initialize_env(False, 0, context='train')
    #         # print(f'agent rank {rank} initializing environment')
        
    #     # initialize step counter (for logging)
    #     step_counter = 0
    #     episode_counter = 0
    #     cycle_counter = 0
    #     success_counter = 0.0
    #     # set best reward
    #     # best_reward = self.agent.env.reward_range[0] # substitute with -np.inf
    #     best_reward = -np.inf
    #     # instantiate list to store reward history
    #     reward_history = []
    #     # instantiate lists to store time history
    #     episode_time_history = []
    #     step_time_history = []
    #     learning_time_history = []
    #     steps_per_episode_history = []  # List to store steps per episode
    #     for epoch in range(epochs):
    #         # print(f'agent rank {rank} starting epoch {epoch+1}')
    #         for cycle in range(num_cycles):
    #             cycle_counter += 1
    #             # print(f'agent rank {rank} starting cycle {cycle_counter}')
    #             for episode in range(num_episodes):
    #                 # print(f'episode: {episode}')
    #                 episode_counter += 1
    #                 # print(f'agent {rank} begin episode {episode_counter}')
    #                 # print('state normalizer config')
    #                 # print(self.state_normalizer.get_config())
    #                 # print('')
    #                 # print('goal normalizer config')
    #                 # print(self.goal_normalizer.get_config())
    #                 # print('')
    #                 # print(f'agent rank {rank} starting episode {episode_counter}')
    #                 if agent.callbacks:
    #                     if MPI.COMM_WORLD.Get_rank() == 0:
    #                         for callback in agent.callbacks:
    #                             callback.on_train_epoch_begin(epoch=step_counter, logs=None)
    #                 episode_start_time = time.time()
                    
    #                 # reset noise
    #                 if type(agent.noise) == helper.OUNoise:
    #                     agent.noise.reset()
                    


    #                 # RUN_EPISODE()
    #                 # reset environment
    #                 obs, _ = agent.env.reset()
    #                 # print(f'state: {state}' )
    #                 if isinstance(obs, dict): # if state is a dict, extract observation (robotics)
    #                     state = obs["observation"]
    #                     state_achieved_goal = obs["achieved_goal"]
    #                     desired_goal = obs["desired_goal"]
    #                     # print(f'state: {state}')
    #                     # print(f'state achieved goal: {state_achieved_goal}')
    #                     # print(f'desired goal: {desired_goal}')
    #                 else:
    #                     state = obs
                    
    #                 # instantiate empty lists to store current episode trajectory
    #                 states, actions, next_states, dones, state_achieved_goals, \
    #                 next_state_achieved_goals, desired_goals = [], [], [], [], [], [], []
                    
    #                 # set desired goal
    #                 # desired_goal = self.desired_goal_func(agent.env)
    #                 # print(f'desired goal: {desired_goal}')
                    
    #                 # set achieved goal
    #                 # state_achieved_goal = self.achieved_goal_func(agent.env)
    #                 # print(f'achieved goal: {state_achieved_goal}')
                    
    #                 # add initial state and goals to local normalizer stats
    #                 # print(f'agent rank {rank} updating normalizer local stats...')
    #                 self.state_normalizer.update_local_stats(state)
    #                 self.goal_normalizer.update_local_stats(desired_goal)
    #                 self.goal_normalizer.update_local_stats(state_achieved_goal)
    #                 # print(f'agent rank {rank} updated normalizer local stats')
                    
    #                 # set done flag
    #                 done = False
                    
    #                 # reset episode reward to 0
    #                 episode_reward = 0
                    
    #                 # reset steps counter for the episode
    #                 episode_steps = 0

    #                 while not done:
    #                     # increase step counter
    #                     step_counter += 1
                        
    #                     # start step timer
    #                     step_start_time = time.time()
                        
    #                     # get action
    #                     action = agent.get_action(state, desired_goal, grad=True,
    #                                               state_normalizer=self.state_normalizer,
    #                                               goal_normalizer=self.goal_normalizer)
                        
    #                     # take action
    #                     next_obs, reward, term, trunc, _ = agent.env.step(action)
    #                     # print(f'next state: {next_state}')
                        
    #                     # extract observation from next state if next_state is dict (robotics)
    #                     if isinstance(next_obs, dict):
    #                         next_state = next_obs["observation"]
    #                         next_state_achieved_goal = next_obs["achieved_goal"]
    #                         desired_goal = next_obs["desired_goal"]
    #                         # print(f'next state: {next_state}')
    #                         # print(f'next state achieved goal: {next_state_achieved_goal}')
    #                         # print(f'desired goal: {desired_goal}')
    #                     else:
    #                         next_state = next_obs
                        
    #                     # calculate and log step time
    #                     step_time = time.time() - step_start_time
    #                     step_time_history.append(step_time)
                        
    #                     # get next state achieved goal
    #                     # next_state_achieved_goal = self.achieved_goal_func(agent.env)
    #                     # print(f'next state achieved goal: {next_state_achieved_goal}')
                        
    #                     # add next state and next state achieved goal to normalizers
    #                     # print(f'agent rank {rank} updating normalizer local stats...')
    #                     self.state_normalizer.update_local_stats(next_state)
    #                     self.goal_normalizer.update_local_stats(next_state_achieved_goal)
                        
    #                     # calculate distance from achieved goal to desired goal
    #                     # distance_to_goal = np.linalg.norm(
    #                     #     self.desired_goal_func(agent.env) - self.achieved_goal_func(agent.env)
    #                     # )
    #                     distance_to_goal = np.linalg.norm(desired_goal - next_state_achieved_goal)
                        
    #                     # store distance in step config to send to wandb
    #                     agent._train_step_config["goal distance"] = distance_to_goal
                        
    #                     # store trajectory in replay buffer (non normalized!)
    #                     self.replay_buffer.add(state, action, reward, next_state, done,\
    #                                                     state_achieved_goal, next_state_achieved_goal, desired_goal)
    #                     # print(f'agent rank {rank} successfully stored trajectory in replay buffer')

    #                     # append step state, action, next state, and goals to respective lists
    #                     states.append(state)
    #                     actions.append(action)
    #                     next_states.append(next_state)
    #                     dones.append(done)
    #                     state_achieved_goals.append(state_achieved_goal)
    #                     next_state_achieved_goals.append(next_state_achieved_goal)
    #                     desired_goals.append(desired_goal)

    #                     # add to episode reward and increment steps counter
    #                     episode_reward += reward
    #                     episode_steps += 1
    #                     # update state and state achieved goal
    #                     state = next_state
    #                     state_achieved_goal = next_state_achieved_goal
    #                     # update done flag
    #                     if term or trunc:
    #                         done = True
    #                     # log step metrics
    #                     agent._train_step_config["step reward"] = reward
    #                     agent._train_step_config["step time"] = step_time
                        
                        
    #                     # log to wandb if using wandb callback
    #                     if agent.callbacks:
    #                         # average step logs across all agents
    #                         # averaged_metrics = helper.sync_metrics(agent._train_step_config)
    #                         # only have the main process log callback values to avoid multiple callback calls
    #                        if MPI.COMM_WORLD.Get_rank() == 0:
    #                             # print(f'agent {rank} train step config:')
    #                             # print(agent._train_step_config)
    #                             for callback in agent.callbacks:
    #                                 callback.on_train_step_end(step=step_counter, logs=agent._train_step_config)
    #                     if not done:
    #                         step_counter += 1

    #                 # calculate success rate
    #                 # goal_distance = np.linalg.norm(next_state_achieved_goal - desired_goal, axis=-1)
    #                 success = (distance_to_goal <= self.tolerance).astype(np.float32)
    #                 success_counter += success
    #                 success_perc = success_counter / episode_counter
    #                 # store success rate to train episode config
    #                 agent._train_episode_config["success rate"] = success_perc

    #                 # Update global normalizer stats (main process only)
    #                 if MPI.COMM_WORLD.Get_rank() == 0:
    #                     # print(f'agent {rank} updating global stats...')
    #                     self.state_normalizer.update_global_stats()
    #                     self.goal_normalizer.update_global_stats()

    #                 # print(f'end episode {episode_counter}')
    #                 # print('state normalizer config')
    #                 # print(self.state_normalizer.get_config())
    #                 # print('')
    #                 # print('goal normalizer config')
    #                 # print(self.goal_normalizer.get_config())
    #                 # print('')
                    
    #                 # package episode states, actions, next states, and goals into trajectory tuple
    #                 trajectory = (states, actions, next_states, dones, state_achieved_goals, next_state_achieved_goals, desired_goals)

    #                 # store hindsight experience replay trajectory using current episode trajectory and goal strategy
    #                 # print(f'agent rank {rank} storing hindsight trajectory...')
    #                 self.store_hindsight_trajectory(trajectory, agent, rank)
    #                 # print(f'agent rank {rank} successfully stored hindsight trajectory')
                        
    #                 # check if enough samples in replay buffer and if so, learn from experiences
    #                 if self.replay_buffer.counter > agent.batch_size:
    #                     learn_time = time.time()
    #                     for _ in range(num_updates):
    #                         actor_loss, critic_loss = agent.learn(replay_buffer=self.replay_buffer,
    #                                                               state_normalizer=self.state_normalizer,
    #                                                               goal_normalizer=self.goal_normalizer,
    #                                                               )
    #                     agent._train_episode_config["actor loss"] = actor_loss
    #                     agent._train_episode_config["critic loss"] = critic_loss
                
    #                     learning_time_history.append(time.time() - learn_time)
                    
    #                 episode_time = time.time() - episode_start_time
    #                 episode_time_history.append(episode_time)
    #                 reward_history.append(episode_reward)
    #                 steps_per_episode_history.append(episode_steps) 
    #                 avg_reward = np.mean(reward_history[-100:])
    #                 avg_episode_time = np.mean(episode_time_history[-100:])
    #                 avg_step_time = np.mean(step_time_history[-100:])
    #                 avg_learn_time = np.mean(learning_time_history[-100:])
    #                 avg_steps_per_episode = np.mean(steps_per_episode_history[-100:])  # Calculate average steps per episode

    #                 agent._train_episode_config['episode'] = episode
    #                 agent._train_episode_config["episode reward"] = episode_reward
    #                 agent._train_episode_config["avg reward"] = avg_reward
    #                 agent._train_episode_config["episode time"] = episode_time

    #                 # # log to wandb if using wandb callback
    #                 # if agent.callbacks:
    #                 #     # average episode logs across all agents
    #                 #     averaged_metrics = helper.sync_metrics(agent._train_episode_config)
                    
    #                 # check if best reward
    #                 if MPI.COMM_WORLD.Get_rank() == 0: # only use main process
    #                     if avg_reward > best_reward:
    #                         best_reward = avg_reward
    #                         agent._train_episode_config["best"] = True
    #                         # save model
    #                         self.save()
    #                     else:
    #                         agent._train_episode_config["best"] = False

    #                     if agent.callbacks:
    #                         for callback in agent.callbacks:
    #                             # print(f'agent {rank} train episode config')
    #                             # print(agent._train_episode_config)
    #                             callback.on_train_epoch_end(epoch=step_counter, logs=agent._train_episode_config)


    #         # perform soft update on target networks
    #         agent.soft_update(agent.actor_model, agent.target_actor_model)
    #         agent.soft_update(agent.critic_model, agent.target_critic_model)

    #         # print metrics to terminal log
    #         if MPI.COMM_WORLD.Get_rank() == 0: # only use main process
    #             print(f"epoch {epoch} cycle {cycle_counter} episode {episode_counter}, success percentage {success_perc}, reward {episode_reward}, avg reward {avg_reward}, avg episode time {avg_episode_time:.2f}s")

    #     # if callbacks, call on train end
    #     if MPI.COMM_WORLD.Get_rank() == 0:
    #         if agent.callbacks:
    #             for callback in agent.callbacks:
    #                 # print(f'agent {rank} train end train episode config')
    #                 # print(agent._train_episode_config)
    #                 callback.on_train_end(logs=agent._train_episode_config)
    #     # close the environment
    #     agent.env.close()

    def test(self, num_episodes, render, render_freq):
        """Runs a test over 'num_episodes'."""

        # set model in eval mode
        self.agent.actor_model.eval()
        self.agent.critic_model.eval()
        self.agent.target_actor_model.eval()
        self.agent.target_critic_model.eval()

        if self.agent.callbacks:
            for callback in self.agent.callbacks:
                callback.on_test_begin(logs=self.agent._config)
                #DEBUG
                print('on test begin callback called...')

        # instantiate new environment
        self.agent.env = self.agent._initialize_env(render, render_freq, context='test')

        # instantiate list to store reward, step time, and episode time history
        reward_history = []
        self._step = 1
        success_counter = 0.0
        # set the model to calculate no gradients during evaluation
        with T.no_grad():
            for i in range(num_episodes):
                if self.agent.callbacks:
                    for callback in self.agent.callbacks:
                        callback.on_test_epoch_begin(epoch=self._step, logs=None)

                state, _ = self.agent.env.reset()
                if isinstance(state, dict): # if state is a dict, extract observation (robotics)
                    state = state["observation"]
                # set desired goal
                desired_goal = self.desired_goal_func(self.agent.env)
                done = False
                episode_reward = 0
                while not done:
                    # get action
                    action = self.agent.get_action(state, desired_goal, grad=False, test=True,
                                                   state_normalizer=self.state_normalizer,
                                                   goal_normalizer=self.goal_normalizer)
                    next_state, reward, term, trunc, _ = self.agent.env.step(action)
                    # extract observation from next state if next_state is dict (robotics)
                    if isinstance(next_state, dict):
                        next_state = next_state['observation']
                    if term or trunc:
                        done = True
                    episode_reward += reward
                    state = next_state
                    self._step += 1

                reward_history.append(episode_reward)
                avg_reward = np.mean(reward_history[-100:])
                self.agent._test_episode_config["episode reward"] = episode_reward
                self.agent._test_episode_config["avg reward"] = avg_reward
                # calculate success rate
                goal_distance = np.linalg.norm(self.achieved_goal_func(self.agent.env) - desired_goal, axis=-1)
                success = (goal_distance <= self.tolerance).astype(np.float32)
                success_counter += success
                success_perc = success_counter / (i+1)
                # store success rate to train episode config
                self.agent._test_episode_config["success rate"] = success_perc
                if self.agent.callbacks:
                    for callback in self.agent.callbacks:
                        callback.on_test_epoch_end(epoch=self._step, logs=self.agent._test_episode_config)

                print(f"episode {i+1}, score {episode_reward}, avg_score {avg_reward}, success {success_perc}")

            if self.agent.callbacks:
                for callback in self.agent.callbacks:
                    callback.on_test_end(logs=self.agent._test_episode_config)
            # close the environment
            self.agent.env.close()

    def store_hindsight_trajectory(self, trajectory):
        """
        Stores a hindsight experience replay trajectory in the replay buffer.
        Args:
            trajectory: tuple of states, actions, next states, dones, state achieved goals, next state achieved goals, and desired goals
        """
        # with self.lock:
        states, actions, next_states, dones, state_achieved_goals, next_state_achieved_goals, desired_goals = trajectory
        # print(f'rank: {rank}')
        # print(f'states: {states}')
        # print(f'actions: {actions}')
        # print(f'next states: {next_states}')
        # print(f'dones: {dones}')
        # print(f'state achieved goals: {state_achieved_goals}')
        # print(f'next state achieved goals: {next_state_achieved_goals}')
        # print(f'desired goals: {desired_goals}')
        # print('')
        # instantiate variable to keep track of times tolerance is hit
        tol_count = 0

        # loop over each step in the trajectory to set new achieved goals, calculate new reward, and save to replay buffer
        for idx, (state, action, next_state, done, state_achieved_goal, next_state_achieved_goal, desired_goal) in enumerate(zip(states, actions, next_states, dones, state_achieved_goals, next_state_achieved_goals, desired_goals)):
            
            # normalize state and next states goals
            # state_achieved_goal_norm = self.agent.goal_normalizer.normalize(state_achieved_goal)
            # next_state_achieved_goal_norm = self.agent.goal_normalizer.normalize(next_state_achieved_goal)

            

            if self.strategy == "final":
                new_desired_goal = next_state_achieved_goals[-1]
                # normalize desired goal to pass to reward func
                # new_desired_goal_norm = self.agent.goal_normalizer.normalize(new_desired_goal)
                new_reward, within_tol = self.reward_fn(self.agent.env, action, state_achieved_goal, next_state_achieved_goal, new_desired_goal, self.tolerance)
                # DEBUG
                # print(f'reward: {new_reward}; within_tol: {within_tol}')
                # increment tol_count
                tol_count += within_tol

                # store non normalized trajectory
                self.replay_buffer.add(state, action, new_reward, next_state, done, state_achieved_goal, next_state_achieved_goal, new_desired_goal)

            elif self.strategy == 'future':
                for i in range(self.num_goals):
                    if idx + i >= len(states) -1:
                        break
                    goal_idx = np.random.randint(idx + 1, len(states))
                    new_desired_goal = next_state_achieved_goals[goal_idx]
                    # normalize desired goal to pass to reward func
                    # new_desired_goal_norm = self.agent.goal_normalizer.normalize(new_desired_goal)
                    #DEBUG
                    # print(f'sent next state achieved goal: {next_state_achieved_goal}')
                    # print(f'sent new desired goal: {new_desired_goal}')
                    new_reward, within_tol = self.reward_fn(self.agent.env, action, state_achieved_goal, next_state_achieved_goal, new_desired_goal, self.tolerance)
                    # DEBUG
                    # print(f'reward: {new_reward}; within_tol: {within_tol}')
                    # increment tol_count
                    tol_count += within_tol
                    # print(f'tol count: {tol_count}')
                    # store non normalized trajectory
                    self.replay_buffer.add(state, action, new_reward, next_state, done, state_achieved_goal, next_state_achieved_goal, new_desired_goal)

            elif self.strategy == 'none':
                break

        # add tol count to train step config for callbacks
        if self.agent.callbacks:
            if MPI.COMM_WORLD.Get_rank() == 0:
                self.agent._train_episode_config["tolerance count"] = tol_count
                
        

    def set_normalizer_state(self, config):
        self.agent.state_normalizer.set_state(config)

    # def cleanup(self):
    #     self.replay_buffer.cleanup()
    #     self.state_normalizer.cleanup()
    #     self.goal_normalizer.cleanup()
    #     T.cuda.empty_cache()
    #     if dist.is_initialized():
    #         dist.destroy_process_group()
    #         print("Process group destroyed")
    #     print("Cleanup complete")


    def get_config(self):
        config = {
            "agent_type": self.__class__.__name__,
            "agent": self.agent.get_config(),
            "strategy": self.strategy,
            "tolerance":self.tolerance,
            "num_goals": self.num_goals,
            "desired_goal": self.desired_goal_func.__name__,
            "achieved_goal": self.achieved_goal_func.__name__,
            "reward_fn": self.reward_fn.__name__,
            "normalizer_clip": self.normalizer_clip,
            "normalizer_eps": self.normalizer_eps,
            "replay_buffer_size": self.replay_buffer_size,
            "device": self.device,
            "save_dir": self.save_dir,
        }

        # if callable(self.reward_fn) and self.reward_fn.__name__ == '<lambda>':
        #     config["reward_fn"] = inspect.getsource(self.reward_fn).strip()
        # else:
        #     config["reward_fn"] = self.reward_fn.__name__

        return config
    
    def save(self):
        """Saves the model."""

        # Change self.save_dir if save_dir 
        # if save_dir is not None:
        #     self.save_dir = save_dir + "/her/"
        #     print(f'new save dir: {self.save_dir}')

        config = self.get_config()

        # makes directory if it doesn't exist
        os.makedirs(self.save_dir, exist_ok=True)

        # writes and saves JSON file of DDPG agent config
        with open(self.save_dir + "config.json", "w", encoding="utf-8") as f:
            json.dump(config, f)

        # save agent
        # if save_dir is not None:
        #     self.agent.save(self.save_dir)
        #     print(f'new agent save dir: {self.agent.save_dir}')
        # else:
        self.agent.save()

        self.state_normalizer.save_state(self.save_dir + "state_normalizer.npz")
        self.goal_normalizer.save_state(self.save_dir + "goal_normalizer.npz")

    @classmethod
    def load(cls, config, load_weights=True):
        """Loads the model."""
        logger.debug(f'rank {MPI.COMM_WORLD.rank} HER.load called')
        # # load reinforce agent config
        # with open(
        #     Path(folder).joinpath(Path("obj_config.json")), "r", encoding="utf-8"
        # ) as f:
        #     obj_config = json.load(f)

        # Resolve function names to actual functions
        try:
            config["desired_goal"] = getattr(gym_helper, config["desired_goal"])
            config["achieved_goal"] = getattr(gym_helper, config["achieved_goal"])
            config["reward_fn"] = getattr(gym_helper, config["reward_fn"])
            logger.debug(f"rank {MPI.COMM_WORLD.rank} HER.load successfully loaded gym goal functions")
        except Exception as e:
            logger.error(f"rank {MPI.COMM_WORLD.rank} HER.load failed to load gym goal functions: {e}", exc_info=True)

        # load agent
        try:
            agent = load_agent_from_config(config["agent"], load_weights)
            logger.debug(f"rank {MPI.COMM_WORLD.rank} HER.load successfully loaded Agent")
            logger.debug(f'rank {MPI.COMM_WORLD.rank} agent config:{agent.get_config()}')
        except Exception as e:
            logger.error(f"rank {MPI.COMM_WORLD.rank} HER.load failed to load Agent: {e}", exc_info=True)

        # instantiate HER model
        try:
            her = cls(agent, config["strategy"], config["tolerance"], config["num_goals"],
                    config["desired_goal"], config["achieved_goal"], config["reward_fn"],
                    config['normalizer_clip'], config['normalizer_eps'], config["replay_buffer_size"],
                    config["device"], config["save_dir"])
            logger.debug(f"rank {MPI.COMM_WORLD.rank} HER.load successfully loaded HER")
        except Exception as e:
            logger.error(f"rank {MPI.COMM_WORLD.rank} HER.load failed to load HER: {e}", exc_info=True)

        # load agent normalizers
        try:
            agent.state_normalizer = helper.Normalizer.load_state(config['save_dir'] + "state_normalizer.npz")
            agent.goal_normalizer = helper.Normalizer.load_state(config['save_dir'] + "goal_normalizer.npz")
            logger.debug(f"rank {MPI.COMM_WORLD.rank} HER.load successfully loaded normalizers")
        except Exception as e:
            logger.error(f"rank {MPI.COMM_WORLD.rank} HER.load failed to load normalizers: {e}", exc_info=True)
        
        return her
    

# def load_agent_from_config_path(config_path, load_weights=True):
#     """Loads an agent from a config file path."""
#     with open(
#         Path(config_path).joinpath(Path("obj_config.json")), "r", encoding="utf-8"
#     ) as f:
#         config = json.load(f)

#     agent_type = config["agent_type"]

#     # Use globals() to get a reference to the class
#     agent_class = globals().get(agent_type)

#     if agent_class:
#         return agent_class.load(config_path, load_weights)

#     raise ValueError(f"Unknown agent type: {agent_type}")

def load_agent_from_config(config, load_weights=True):
    """Loads an agent from a loaded config file."""
    agent_type = config["agent_type"]

    # Use globals() to get a reference to the class
    agent_class = globals().get(agent_type)

    if agent_class:
        return agent_class.load(config, load_weights)

    raise ValueError(f"Unknown agent type: {agent_type}")


def get_agent_class_from_type(agent_type: str):
    """Builds an agent from a passed agent type str."""

    types = {"Actor Critic": "ActorCritic",
             "Reinforce": "Reinforce",
             "DDPG": "DDPG",
             "HER_DDPG": "HER",
             "HER": "HER",
             "TD3": "TD3",
            }

    # Use globals() to get a reference to the class
    agent_class = globals().get(types[agent_type])

    if agent_class:
        return agent_class

    raise ValueError(f"Unknown agent type: {agent_type}")

def init_sweep(sweep_config, train_config, comm=None):
    rank = MPI.COMM_WORLD.Get_rank()
    if comm is not None:
        logger.debug(f"Rank {rank} comm detected")
        rank = comm.Get_rank()
        logger.debug(f"Global rank {MPI.COMM_WORLD.Get_rank()} set to comm rank {rank}")
        logger.debug(f"Rank {rank} in {comm.Get_name()}, name {comm.Get_name()}")
    
    try:
        # Set the environment variable
        os.environ['WANDB_DISABLE_SERVICE'] = 'true'
        # logger.debug(f"{comm.Get_name()}; Rank {rank} WANDB_DISABLE_SERVICE set to true")

        # Set seeds
        random.seed(train_config['seed'])
        np.random.seed(train_config['seed'])
        T.manual_seed(train_config['seed'])
        T.cuda.manual_seed(train_config['seed'])
        # logger.debug(f'{comm.Get_name()}; Rank {rank} random seeds set')

        # Only primary process (rank 0) calls wandb.init() to build agent and log data
        if comm is not None:
            if rank == 0:
                # logger.debug('MPI rank 0 process fired')
                # try:
                run_number = wandb_support.get_next_run_number(sweep_config["project"])
                logger.debug(f"{comm.Get_name()}; Rank {rank} run number set: {run_number}")
                
                run = wandb.init(
                    project=sweep_config["project"],
                    settings=wandb.Settings(start_method='thread'),
                    job_type="train",
                    name=f"train-{run_number}",
                    tags=["train"],
                    group=f"group-{run_number}",
                    # dir=run_dir
                )
                logger.debug("wandb.init() fired")
                wandb_config = dict(wandb.config)
                model_type = list(wandb_config.keys())[0]
                
                # Wait for configuration to be populated
                max_retries = 10
                retry_interval = 1  # in seconds

                for _ in range(max_retries):
                    if "model_type" in wandb.config:
                        break
                    logger.debug(f"{comm.Get_name()}; Rank {rank} Waiting for wandb.config to be populated...")
                    time.sleep(retry_interval)

                if "model_type" in wandb.config:
                    logger.debug(f'{comm.Get_name()}; Rank {rank} wandb.config: {wandb.config}')
                    run.tags = run.tags + (model_type,)
                else:
                    logger.error("wandb.config did not populate with model_type within the expected time", exc_info=True)
                
                run.tags = run.tags + (model_type,)
                logger.debug(f"{comm.Get_name()}; Rank {rank} run.tag set")
                env = gym.make(**{param: value["value"] for param, value in sweep_config["parameters"]["env"]["parameters"].items()})
                # save env spec to string
                env_spec = env.spec.to_json()
                logger.debug(f"{comm.Get_name()}; Rank {rank} env built: {env.spec}")
                callbacks = []
                callbacks.append(rl_callbacks.WandbCallback(project_name=sweep_config["project"], run_name=f"train-{run_number}", _sweep=True))
                logger.debug(f"{comm.Get_name()}; Rank {rank} callbacks created")

            else:
                env_spec = None
                callbacks = None
                run_number = None
                wandb_config = None
            
            # Use MPI Barrier to sync processes
            logger.debug(f"{comm.Get_name()}; Rank {rank} init_sweep calling MPI Barrier")
            comm.Barrier()
            logger.debug(f"{comm.Get_name()}; Rank {rank} init_sweep MPI Barrier passed")

            env_spec = comm.bcast(env_spec, root=0)
            callbacks = comm.bcast(callbacks, root=0)
            run_number = comm.bcast(run_number, root=0)
            wandb_config = comm.bcast(wandb_config, root=0)
            model_type = list(wandb_config.keys())[0]
            logger.debug(f"{comm.Get_name()}; Rank {rank} broadcasts complete")

            agent = get_agent_class_from_type(model_type)
            logger.debug(f"{comm.Get_name()}; Rank {rank} agent class found. Calling sweep_train")
            agent.sweep_train(wandb_config, train_config, env_spec, callbacks, run_number, comm)
        
        else:
            print('else fired')
            run_number = wandb_support.get_next_run_number(sweep_config["project"])
            logger.debug(f"run number set: {run_number}")
            print(f'run number:{run_number}')
            
            run = wandb.init(
                project=sweep_config["project"],
                settings=wandb.Settings(start_method='thread'),
                job_type="train",
                name=f"train-{run_number}",
                tags=["train"],
                group=f"group-{run_number}",
                # dir=run_dir
            )
            logger.debug("wandb.init() fired")
            wandb_config = dict(wandb.config)
            print(f'wandb config: {wandb_config}')
            model_type = list(wandb_config.keys())[0]
            
            # Wait for configuration to be populated
            max_retries = 10
            retry_interval = 1  # in seconds

            for _ in range(max_retries):
                if "model_type" in wandb.config:
                    break
                logger.debug(f"Waiting for wandb.config to be populated...")
                time.sleep(retry_interval)

            if "model_type" in wandb.config:
                logger.debug(f'wandb.config: {wandb.config}')
                run.tags = run.tags + (model_type,)
            else:
                logger.error("wandb.config did not populate with model_type within the expected time", exc_info=True)
            
            run.tags = run.tags + (model_type,)
            logger.debug(f"run.tag set")
            env = gym.make(**{param: value["value"] for param, value in sweep_config["parameters"]["env"]["parameters"].items()})
            # save env spec to string
            env_spec = env.spec.to_json()
            logger.debug(f"env built: {env.spec}")
            callbacks = []
            callbacks.append(rl_callbacks.WandbCallback(project_name=sweep_config["project"], run_name=f"train-{run_number}", _sweep=True))
            logger.debug(f"callbacks created")
            agent = get_agent_class_from_type(model_type)
            logger.debug(f"agent class found. Calling sweep_train")
            agent.sweep_train(wandb_config, train_config, env_spec, callbacks, run_number)

    except Exception as e:
        logger.error(f"Error in rl_agent.init_sweep: {e}", exc_info=True)