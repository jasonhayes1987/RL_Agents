"""Holds Model classes used for Reinforcement learning."""

# imports
import json
import os
from typing import List, Tuple
from pathlib import Path
# import time

import tensorflow as tf
from tensorflow import keras
from tensorflow.keras.layers import Dense, Input, Concatenate, BatchNormalization
from tensorflow.keras import Model, optimizers, initializers
from tensorflow.keras.initializers import HeNormal
from tensorflow.keras.models import save_model, load_model
import gymnasium as gym
import numpy as np


class PolicyModel(Model):
    """Policy model for predicting probability distribution of actions.

    Attributes:
      env: OpenAI gym environment.
      hidden_layers: List of hidden layer sizes and activations.
      optimizer: Optimizer for training.

    """

    def __init__(
        self,
        env: gym.Env,
        dense_layers: List[Tuple[int, str, initializers.Initializer]] = None,
        optimizer: keras.optimizers = optimizers.Adam(0.001),
    ):
        super().__init__()
        self.env = env
        self.layer_config = dense_layers
        
        # if dense_layers is None:
        #     dense_layers = [(100, "relu")]
        # self.hidden_layers = dense_layers
        self.optimizer = optimizer
        # self.optimizer.learning_rate = self.learning_rate

        # build model
        self.dense_layers = [Dense(units, activation, kernel_initializer=initializer, bias_initializer=initializer) for units, activation, initializer in dense_layers]
        self.out = Dense(env.action_space.n, activation=None, kernel_initializer=HeNormal(), bias_initializer=HeNormal())

        # compile model
        self.compile(optimizer=self.optimizer)
        
        # run sample data through model to initialize it
        _ = self(np.random.random((1, env.observation_space.shape[0])))
        
        # self.model = keras.Sequential()
        # for size, activation in dense_layers:
        #     self.model.add(
        #         Dense(size, activation=activation, kernel_initializer=HeNormal())
        #     )
        # self.model.add(
        #     Dense(env.action_space.n, activation=None, kernel_initializer=HeNormal())
        # )
        # self.build(np.expand_dims(env.observation_space.sample(), axis=0).shape)

    # @tf.function()
    def call(self, state, return_logits=False):
        """Forward Propogation."""
        x = state
        for dense_layer in self.dense_layers:
            x = dense_layer(x)
        logits = self.out(x)
        # logits = self.model(state)
        if return_logits:
            return logits
        return tf.nn.softmax(logits)

    def _set_learning_rate(self, learning_rate):
        """Sets learning rate of optimizer."""
        self.optimizer.learning_rate = learning_rate

    def get_config(self):
        """Get model config."""

        config = {
            'env': self.env.spec.id,
            'hidden_layers': len(self.dense_layers),
            'dense_layers': self.dense_layers,
            'optimizer': optimizers.serialize(self.optimizer),
        }

        config['dense_layers'] = [(units, activation, initializers.serialize(initializer)) for (units, activation, initializer) in self.layer_config]

        return config

    def save(self, folder):
        """Save model."""
        # Ensure the model directory exists
        model_dir = os.path.join(folder, "policy_model")
        os.makedirs(model_dir, exist_ok=True)

        # Save the TensorFlow model for the weights
        save_model(self, os.path.join(model_dir, 'tf_model'))
        # self.model.save(folder + "/policy_model")
        
        obj_config = self.get_config()
        with open(folder + "/policy_model/obj_config.json", "w", encoding="utf-8") as f:
            json.dump((obj_config), f)
        # policy_opt_config = optimizers.serialize(self.optimizer)
        # with open(
        #     folder + "/policy_model/policy_optimizer_config.json", "w", encoding="utf-8"
        # ) as f:
        #     json.dump((policy_opt_config), f)

    @classmethod
    def load(cls, folder, load_weights=True):
        """Load model."""
        with open(Path(folder) / "policy_model/obj_config.json", "r", encoding="utf-8") as f:
            obj_config = json.load(f)
        
        # Load the TensorFlow model
        tf_model_path = str(Path(folder) / "policy_model/tf_model")
        loaded_model = load_model(tf_model_path)
        policy_opt = loaded_model.optimizer
        # policy_opt = optimizers.deserialize(policy_opt_config)
        
        # Reconstruct the environment and other configurations
        env = gym.make(obj_config["env"])
        dense_layers = [(size, activation, tf.keras.initializers.deserialize(initializer))
                        for size, activation, initializer in obj_config["dense_layers"]]
        
        policy_model = cls(
            env=env,
            dense_layers=dense_layers,
            optimizer=policy_opt,
        )
        
        if load_weights:
            # Load the weights into the model
            loaded_weights = [layer.get_weights() for layer in loaded_model.layers]
            for layer, weights in zip(policy_model.layers, loaded_weights):
                layer.set_weights(weights)

        return policy_model


class ValueModel(Model):
    """Value model for predicting state values.

    Attributes:
      env: OpenAI gym environment.
      hidden_layers: List of hidden layer sizes and activations.
      optimizer: Optimizer for training.

    """

    def __init__(
        self,
        env: gym.Env,
        dense_layers: List[Tuple[int, str, initializers.Initializer]] = None,
        optimizer: keras.optimizers = optimizers.Adam(0.001),
    ):
        super().__init__()
        self.env = env
        self.layer_config = dense_layers
        # if hidden_layers is None:
        #     hidden_layers = [(100, "relu")]
        # self.hidden_layers = hidden_layers
        self.optimizer = optimizer
        # self.optimizer.learning_rate = self.learning_rate
        # self.model = keras.Sequential()
        # for size, activation in hidden_layers:
        #     self.model.add(
        #         Dense(size, activation=activation, kernel_initializer=HeNormal())
        #     )
        # self.model.add(Dense(1, activation=None, kernel_initializer=HeNormal()))
        # self.build(np.expand_dims(env.observation_space.sample(), axis=0).shape)

        # build model
        self.dense_layers = [Dense(units, activation, kernel_initializer=initializer, bias_initializer=initializer) for units, activation, initializer in dense_layers]
        self.out = Dense(1, activation=None, kernel_initializer=HeNormal())
        
        # compile model
        self.compile(optimizer=self.optimizer)
        
        # run sample data through model to initialize it
        _ = self(np.random.random((1, env.observation_space.shape[0])))

    # @tf.function()
    def call(self, state):
        """Forward propogation."""
        x = state
        for dense_layer in self.dense_layers:
            x = dense_layer(x)
        return self.out(x)

    def _set_learning_rate(self, learning_rate):
        """Sets learning rate of optimizer."""
        self.optimizer.learning_rate = learning_rate

    def get_config(self):
        """Get model config."""

        config = {
            'env': self.env.spec.id,
            'hidden_layers': len(self.dense_layers),
            'dense_layers': self.dense_layers,
            'optimizer': optimizers.serialize(self.optimizer),
        }

        config['dense_layers'] = [(units, activation, initializers.serialize(initializer)) for (units, activation, initializer) in self.layer_config]

        return config

    def save(self, folder):
        """Save model."""
        # Ensure the model directory exists
        model_dir = os.path.join(folder, "value_model")
        os.makedirs(model_dir, exist_ok=True)

        # Save the TensorFlow model for the weights
        save_model(self, os.path.join(model_dir, 'tf_model'))
        # self.model.save(folder + "/value_model")
        
        obj_config = self.get_config()
        with open(folder + "/value_model/obj_config.json", "w", encoding="utf-8") as f:
            json.dump((obj_config), f)

    @classmethod
    def load(cls, folder, load_weights=True):
        """Load model."""
        with open(Path(folder) / "value_model/obj_config.json", "r", encoding="utf-8") as f:
            obj_config = json.load(f)
        
        # Load the TensorFlow model
        tf_model_path = str(Path(folder) / "value_model/tf_model")
        loaded_model = load_model(tf_model_path)
        policy_opt = loaded_model.optimizer
        # policy_opt = optimizers.deserialize(policy_opt_config)
        
        # Reconstruct the environment and other configurations
        env = gym.make(obj_config["env"])
        dense_layers = [(size, activation, tf.keras.initializers.deserialize(initializer))
                        for size, activation, initializer in obj_config["dense_layers"]]
        
        value_model = cls(
            env=env,
            dense_layers=dense_layers,
            optimizer=policy_opt,
        )
        
        if load_weights:
            # Load the weights into the model
            loaded_weights = [layer.get_weights() for layer in loaded_model.layers]
            for layer, weights in zip(value_model.layers, loaded_weights):
                layer.set_weights(weights)

        return value_model
    
class ActorModel(Model):
    """Actor model for predicting action values."""
    
    def __init__(
            self,
            env: gym.Env,
            dense_layers: List[Tuple[int, str, initializers.Initializer]] = None,
            learning_rate: float = 0.0001,
            optimizer: optimizers = optimizers.Adam(),
    ):
        super().__init__()
        self.env = env
        self.learning_rate = learning_rate
        self.layer_config = dense_layers
        self.optimizer = optimizer
        self.optimizer.learning_rate = self.learning_rate

        # build model
        self.dense_layers = [Dense(units, activation, kernel_initializer=initializer, bias_initializer=initializer) for units, activation, initializer in dense_layers]
        self.mu = Dense(env.action_space.shape[0], activation='tanh', kernel_initializer=initializers.RandomUniform(-3e-3, 3e-3), bias_initializer=initializers.RandomUniform(-3e-3, 3e-3))
        
        # compile model
        self.compile(optimizer=self.optimizer)
        
        # run sample data through model to initialize it
        _ = self(np.random.random((1, *env.observation_space.shape)))


    def call(self, state):
        """Forward Propogation."""
        
        x = state
        for dense_layer in self.dense_layers:
            x = dense_layer(x)
        return self.mu(x) * self.env.action_space.high
    
    def _set_learning_rate(self, learning_rate):
        """Sets learning rate of optimizer."""
        self.optimizer.learning_rate = learning_rate

    def get_config(self):
        """Get model config."""

        config = {
            'env': self.env.spec.id,
            'hidden_layers': len(self.dense_layers),
            'dense_layers': self.dense_layers,
            'learning_rate': self.learning_rate,
            'optimizer': optimizers.serialize(self.optimizer),
        }

        config['dense_layers'] = [(units, activation, initializers.serialize(initializer)) for (units, activation, initializer) in self.layer_config]

        return config
    
    def get_clone(self, weights=True):
        """Creates and returns a clone of the model."""
        # Step 2: Reconstruct the model from its configuration
        cloned_model = ActorModel(
            env = self.env,
            dense_layers=self.layer_config,
            learning_rate=self.learning_rate,
            optimizer=self.optimizer
        )

        # copy weights if weights is True
        if weights:
            cloned_model.set_weights(self.get_weights())

        return cloned_model

    def save(self, folder):
        # Ensure the model directory exists
        model_dir = os.path.join(folder, "policy_model")
        os.makedirs(model_dir, exist_ok=True)

        # Save the TensorFlow model for the weights
        save_model(self, os.path.join(model_dir, 'tf_model'))

        # # Save additional configuration as before
        # obj_config = {
        #     "env_name": self.env.spec.id,
        #     "dense_layers": [
        #         (size, activation, {"class_name": initializer.__class__.__name__, "config": initializer.get_config()})
        #         for size, activation, initializer in self.layer_config  # Make sure this matches your class attribute
        #     ],
        # }

        # save model config
        obj_config = self.get_config()
        with open(os.path.join(model_dir, "obj_config.json"), "w", encoding="utf-8") as f:
            json.dump(obj_config, f)

        # # save optimizer
        # opt_config = self.optimizer.get_config()
        # with open(os.path.join(model_dir, "optimizer_config.json"), "w", encoding="utf-8") as f:
        #     json.dump(opt_config, f)


    @classmethod
    def load(cls, folder, load_weights=True):
        """Load model."""

        with open(Path(folder) / "policy_model/obj_config.json", "r", encoding="utf-8") as f:
            obj_config = json.load(f)

        # No need to load optimizer config separately if model.save is used, as it's included in the saved model
        # Load the TensorFlow model
        tf_model_path = str(Path(folder) / "policy_model/tf_model")
        loaded_model = load_model(tf_model_path)

        # Reconstruct the environment and other configurations
        env = gym.make(obj_config["env"])
        dense_layers = [(size, activation, tf.keras.initializers.deserialize(initializer))
                        for size, activation, initializer in obj_config["dense_layers"]]

        # with open(
        #     Path(folder).joinpath(Path("policy_model/optimizer_config.json")),
        #     "r",
        #     encoding="utf-8",
        # ) as f:
        #     opt_config = json.load(f)
        opt = optimizers.deserialize(obj_config['optimizer'])

        # Initialize your class with the configurations
        actor_model = cls(env=env, dense_layers=dense_layers, learning_rate=obj_config['learning_rate'], optimizer=opt)

        if load_weights:
            # Load the weights into the model
            loaded_weights = [layer.get_weights() for layer in loaded_model.layers]
            for layer, weights in zip(actor_model.layers, loaded_weights):
                layer.set_weights(weights)


        return actor_model

class CriticModel(Model):
    """Critic model for predicting state/action values."""
    
    def __init__(
            self,
            env: gym.Env,
            state_layers: List[Tuple[int, str, initializers.Initializer]] = None,
            merged_layers: List[Tuple[int, str, initializers.Initializer]] = None,
            learning_rate: float = 0.001,
            optimizer: optimizers = optimizers.Adam(),
    ):
        super().__init__()
        self.env = env
        self.optimizer = optimizer
        self.learning_rate = learning_rate
        self.optimizer.learning_rate = self.learning_rate
        self.state_config = state_layers
        self.merged_config = merged_layers
        
        # build model
        self.state_layers = [Dense(units, activation, kernel_initializer=initializer, bias_initializer=initializer) for units, activation, initializer in state_layers]
        self.merged_layers = [Dense(units, activation, kernel_initializer=initializer, bias_initializer=initializer) for units, activation, initializer in merged_layers]
        self.q = Dense(1, activation=None, kernel_initializer=initializers.RandomUniform(-3e-3, 3e-3), bias_initializer=initializers.RandomUniform(-3e-3, 3e-3))
        
        # compile model
        self.compile(optimizer=self.optimizer)

        # run sample data through model to initialize it
        _ = self([np.random.random((1, *env.observation_space.shape)), np.random.random((1, *env.action_space.shape))])

    

    def call(self, inputs):
        state, action = inputs
        x = state
        for state_layer in self.state_layers:
            x = state_layer(x)
        z = tf.concat([x, action], axis=1)
        for merged_layer in self.merged_layers:
            z = merged_layer(z)

        q = self.q(z)

        return q
    
    def _set_learning_rate(self, learning_rate):
        """Sets learning rate of optimizer."""
        self.optimizer.learning_rate = learning_rate

    def get_config(self):
        config = {
            'env': self.env.spec.id,
            'hidden_layers': len(self.state_config) + len(self.merged_config),
            'state_layers': self.state_config,
            'merged_layers': self.merged_config,
            'learning_rate': self.learning_rate,
            'optimizer': optimizers.serialize(self.optimizer),
        }
    
        config['state_layers'] = [(units, activation, initializers.serialize(initializer)) for (units, activation, initializer) in self.state_config]
        config['merged_layers'] = [(units, activation, initializers.serialize(initializer)) for (units, activation, initializer) in self.merged_config]

        return config
    
    def get_clone(self, weights=True):
        """Creates and returns a clone of the model."""
        # Step 2: Reconstruct the model from its configuration
        cloned_model = CriticModel(
            env = self.env,
            state_layers = self.state_config,
            merged_layers=self.merged_config,
            learning_rate=self.learning_rate,
            optimizer=self.optimizer
        )

        # copy weights if weights is True
        if weights:
            cloned_model.set_weights(self.get_weights())

        return cloned_model
    
    def save(self, folder):
        """Save model."""
        # Ensure the model directory exists
        model_dir = os.path.join(folder, "value_model")
        os.makedirs(model_dir, exist_ok=True)

        # Save the TensorFlow model
        save_model(self, os.path.join(model_dir, 'tf_model'))  # This saves architecture, weights, and optimizer
    #     obj_config = {
    #     "env_name": self.env.spec.id,
    #     "state_layers": [
    #         (units, activation, {"class_name": initializer.__class__.__name__, "config": initializer.get_config()})
    #         for units, activation, initializer in self.state_layers
    #     ],
    #     "merged_layers": [
    #         (units, activation, {"class_name": initializer.__class__.__name__, "config": initializer.get_config()})
    #         for units, activation, initializer in self.merged_layers
    #     ],
    #     "learning_rate": self.learning_rate,
    # }
        # save model config
        obj_config = self.get_config()
        with open(os.path.join(model_dir, "obj_config.json"), "w", encoding="utf-8") as f:
            json.dump(obj_config, f)
        
        # opt_config = optimizers.serialize(self.optimizer)
        # with open(
        #     folder + "/value_model/optimizer_config.json", "w", encoding="utf-8"
        # ) as f:
        #     json.dump((opt_config), f)

    @classmethod
    def load(cls, folder, load_weights=True):
        """Load model."""

        with open(Path(folder) / "value_model/obj_config.json", "r", encoding="utf-8") as f:
            obj_config = json.load(f)

        # Load the TensorFlow model
        tf_model_path = str(Path(folder) / "value_model/tf_model")
        loaded_model = load_model(tf_model_path)

        # Reconstruct the environment and other configurations
        env = gym.make(obj_config["env"])
        state_layers = [(size, activation, tf.keras.initializers.deserialize(initializer))
                        for size, activation, initializer in obj_config["state_layers"]]
        merged_layers = [(size, activation, tf.keras.initializers.deserialize(initializer))
                        for size, activation, initializer in obj_config["merged_layers"]]

        # with open(
        #     Path(folder).joinpath(Path("policy_model/optimizer_config.json")),
        #     "r",
        #     encoding="utf-8",
        # ) as f:
        #     opt_config = json.load(f)
        opt = optimizers.deserialize(obj_config['optimizer'])

        # Initialize your class with the configurations
        critic_model = cls(env=env, state_layers=state_layers, merged_layers=merged_layers, learning_rate=obj_config['learning_rate'], optimizer=opt)

        if load_weights:
            # Load the weights into the model
            loaded_weights = [layer.get_weights() for layer in loaded_model.layers]
            # critic_layers = critic_model.state_layers + critic_model.merged_layers
            for layer, weights in zip(critic_model.layers, loaded_weights):
                layer.set_weights(weights)


        return critic_model


def build_layers(units_per_layer: List[int], activation: str, initializer: str):
    """formats sweep_config into policy and value layers"""
    # get policy layers
    layers = []
    for units in units_per_layer:
        layers.append((units, activation, initializer))
    return layers
