import json
from abc import ABC, abstractmethod
import numpy as np
import gymnasium as gym
from gymnasium.envs.registration import EnvSpec
from gymnasium.wrappers import *
from gymnasium.vector import VectorEnv, SyncVectorEnv

WRAPPER_REGISTRY = {
    "AtariPreprocessing": {
        "cls": AtariPreprocessing,
        "default_params": {
            "frame_skip": 1,
            "grayscale_obs": True,
            "scale_obs": True
        }
    },
    "TimeLimit": {
        "cls": TimeLimit,
        "default_params": {
            "max_episode_steps": 1000
        }
    },
    "TimeAwareObservation": {
        "cls": TimeAwareObservation,
        "default_params": {
            "flatten": False,
            "normalize_time": False
        }
    },
    "FrameStackObservation": {
        "cls": FrameStackObservation,
        "default_params": {
            "stack_size": 4
        }
    },
    "ResizeObservation": {
        "cls": ResizeObservation,
        "default_params": {
            "shape": 84
        }
    }
}

# def atari_wrappers(env):
#     """
#     Wrap an Atari environment with preprocessing and frame stacking.

#     This function applies standard Atari preprocessing, including converting to grayscale,
#     resizing, scaling, and stacking multiple consecutive frames for better temporal
#     context.

#     Args:
#         env (gym.Env): The original Atari environment.

#     Returns:
#         gym.Env: The wrapped environment with preprocessing and frame stacking applied.
#     """
#     env = AtariPreprocessing(
#         env,
#         frame_skip=1,
#         grayscale_obs=True,
#         scale_obs=True,
#         screen_size=84
#     )
#     env = FrameStackObservation(env, stack_size=4)
#     return env

def wrap_env(vec_env, wrappers):
    wrapper_list = []
    for wrapper in wrappers:
        if wrapper['type'] in WRAPPER_REGISTRY:
            # print(f'wrapper type:{wrapper["type"]}')
            # Use a copy of default_params to avoid modifying the registry
            default_params = WRAPPER_REGISTRY[wrapper['type']]["default_params"].copy()
            
            if wrapper['type'] == "ResizeObservation":
                # Ensure shape is a tuple for ResizeObservation
                default_params['shape'] = (default_params['shape'], default_params['shape']) if isinstance(default_params['shape'], int) else default_params['shape']
            
            # print(f'default params:{default_params}')
            override_params = wrapper.get("params", {})
            
            if wrapper['type'] == "ResizeObservation":
                # Ensure override_params shape is a tuple
                if 'shape' in override_params:
                    override_params['shape'] = (override_params['shape'], override_params['shape']) if isinstance(override_params['shape'], int) else override_params['shape']
            
            # print(f'override params:{override_params}')
            final_params = {**default_params, **override_params}
            # print(f'final params:{final_params}')
            
            def wrapper_factory(env, cls=WRAPPER_REGISTRY[wrapper['type']]["cls"], params=final_params):
                return cls(env, **params)
            
            wrapper_list.append(wrapper_factory)
    
    # Define apply_wrappers outside the loop
    def apply_wrappers(env):
        for wrapper in wrapper_list:
            env = wrapper(env)
            # print(f'length of obs space:{len(env.observation_space.shape)}')
            # print(f'env obs space shape:{env.observation_space.shape}')
        return env
    
    # print(f'wrapper list:{wrapper_list}')
    envs = [lambda: apply_wrappers(gym.make(vec_env.spec.id, render_mode="rgb_array")) for _ in range(vec_env.num_envs)]    
    return SyncVectorEnv(envs)

class EnvWrapper(ABC):
    """
    Abstract base class for environment wrappers.

    This class defines the required interface for custom environment wrappers.
    """

    @abstractmethod
    def reset(self):
        """
        Reset the environment to an initial state.

        Returns:
            Any: Initial observation of the environment.
        """
        pass
    
    @abstractmethod
    def step(self, action):
        """
        Take an action in the environment.

        Args:
            action: The action to be taken.

        Returns:
            Tuple: Observation, reward, done flag, and additional info.
        """
        pass
    
    @abstractmethod
    def render(self, mode="rgb_array"):
        """
        Render the environment.

        Args:
            mode (str): The render mode (default: "rgb_array").

        Returns:
            Any: Rendered frame or visualization.
        """
        pass

    @abstractmethod
    def _initialize_env(self, render_freq: int = 0, num_envs: int = 1, seed: int = None):
        """
        Initialize the environment with optional rendering and seeding.

        Args:
            render_freq (int): Frequency of rendering (default: 0).
            num_envs (int): Number of parallel environments (default: 1).
            seed (int): Random seed for the environment (default: None).

        Returns:
            Any: The initialized environment.
        """
        pass
    
    @property
    @abstractmethod
    def observation_space(self):
        """
        Get the observation space of the environment.

        Returns:
            gym.Space: The observation space.
        """
        pass
    
    @property
    @abstractmethod
    def action_space(self):
        """
        Get the action space of the environment.

        Returns:
            gym.Space: The action space.
        """
        pass

    @abstractmethod
    def to_json(self) -> str:
        """
        Serialize the environment wrapper configuration to JSON.

        Returns:
            str: JSON string representing the environment configuration.
        """
        pass

    @classmethod
    def from_json(cls, json_string: str):
        """
        Create an environment wrapper instance from a JSON string.

        This method will delegate to the appropriate subclass's `from_json` method
        based on the type specified in the JSON.

        Args:
            json_string (str): JSON string representing the environment configuration.

        Returns:
            EnvWrapper: A new environment wrapper instance.

        Raises:
            ValueError: If the type in the JSON is not recognized or if instantiation fails.
        """
        config = json.loads(json_string)
        try:
            if config['type'] == 'GymnasiumWrapper':
                return GymnasiumWrapper.from_json(json_string)
            # Add more conditions here for other subclasses if they exist
            else:
                raise ValueError(f"Unknown environment wrapper type: {config['type']}")
        except KeyError as e:
            raise ValueError(f"Missing 'type' key in JSON configuration: {e}")
        except Exception as e:
            raise ValueError(f"Failed to instantiate environment from JSON: {e}")


class GymnasiumWrapper(EnvWrapper):
    """
    Wrapper for Gymnasium environments with additional utilities.

    This wrapper supports initialization, resetting, stepping, rendering,
    and JSON-based serialization of Gymnasium environments.
    """
    def __init__(self, env_spec: EnvSpec, wrappers: list[dict] = None):
        self.env_spec = env_spec
        self.wrappers = wrappers
        self.env = self._initialize_env()

    def _initialize_env(self, render_freq: int = 0, num_envs: int = 1, seed: int = None):
        """
        Initialize the Gymnasium environment with unique seeds for each environment.

        Args:
            render_freq (int): Frequency of rendering (default: 0).
            num_envs (int): Number of parallel environments (default: 1).
            seed (int): Base random seed for the environment (default: None).

        Returns:
            gym.Env: The initialized Gymnasium environment.
        """
        self.seed = seed
        if self.seed is None:
            seeds = [None] * num_envs
        else:
            seeds = [self.seed + i for i in range(num_envs)]  # Create different seeds for each environment
        
        # Create a list of environment factories, each with its unique seed
        env_fns = []
        for i in range(num_envs):
            def make_env(i=i):  # Use default argument to capture i
                env = gym.make(self.env_spec.id, render_mode="rgb_array" if render_freq > 0 else None)
                if seeds[i] is not None:
                    env.reset(seed=seeds[i])  # Set seed for each environment
                    env.action_space.seed(seeds[i])  # Also seed the action space
                if self.wrappers:
                    for wrapper in self.wrappers:
                        if wrapper['type'] in WRAPPER_REGISTRY:
                            default_params = WRAPPER_REGISTRY[wrapper['type']]["default_params"].copy()
                            override_params = wrapper.get("params", {})
                            final_params = {**default_params, **override_params}
                            env = WRAPPER_REGISTRY[wrapper['type']]["cls"](env, **final_params)
                return env
            
            env_fns.append(make_env)

        vec_env = SyncVectorEnv(env_fns)

        return vec_env

    def reset(self):
        """
        Reset the environment.

        Returns:
            Any: Initial observation of the environment.
        """
        if self.seed is not None:
            return self.env.reset(seed=self.seed)
        return self.env.reset()
    
    def step(self, action):
        """
        Take an action in the environment.

        Args:
            action: The action to be taken.

        Returns:
            Tuple: Observation, reward, done flag, and additional info.
        """
        return self.env.step(action)
    
    def render(self, mode="rgb_array"):
        """
        Render the environment.

        Args:
            mode (str): The render mode (default: "rgb_array").

        Returns:
            Any: Rendered frame or visualization.
        """
        return self.env.render(mode=mode)
    
    def format_actions(self, actions: np.ndarray, testing=False):
        if isinstance(self.action_space, gym.spaces.Box):
            if testing:
                num_envs = 1
            else:
                num_envs = self.env.num_envs
            num_actions = self.action_space.shape[-1]
            return actions.reshape(num_envs, num_actions)
        if isinstance(self.action_space, gym.spaces.Discrete) or isinstance(self.action_space, gym.spaces.MultiDiscrete):
            return actions.ravel()
        
    def get_base_env(self, env_idx:int=0):
        """Recursively unwrap an environment to get the base environment."""
        env = self.env.envs[env_idx]
        while hasattr(env, 'env'):
            env = env.env
        return env
    
    def close(self):
        """
        Close the environment.
        """
        self.env.close()
    
    @property
    def observation_space(self):
        """
        Get the observation space of the environment.

        Returns:
            gym.Space: The observation space.
        """
        return self.env.observation_space
    
    @property
    def action_space(self):
        """
        Get the action space of the environment.

        Returns:
            gym.Space: The action space.
        """
        return self.env.action_space
    
    @property
    def single_action_space(self):
        """
        Get the single action space for vectorized environments.

        Returns:
            gym.Space: The single action space.
        """
        return self.env.single_action_space

    @property
    def single_observation_space(self):
        """
        Get the single observation space for vectorized environments.

        Returns:
            gym.Space: The single observation space.
        """
        return self.env.single_observation_space
    
    @property
    def config(self):
        """
        Get the configuration of the wrapper.

        Returns:
            dict: Configuration dictionary.
        """
        return {
            "type": self.__class__.__name__,
            "env": self.env_spec.to_json(),
            "wrappers": self.wrappers,
        }
    
    def to_json(self):
        """
        Serialize the wrapper configuration to JSON.

        Returns:
            str: JSON string representing the configuration.
        """
        return json.dumps(self.config)

    @classmethod
    def from_json(cls, json_env_spec):
        """
        Create a Gymnasium wrapper instance from a JSON string.

        Args:
            json_env_spec (str): JSON string representing the configuration.

        Returns:
            GymnasiumWrapper: A new Gymnasium wrapper instance.
        """
        #DEBUG
        # print('GymnasiumWrapper from_json called')
        # print(f'from json env spec:{json_env_spec}, type:{type(json_env_spec)}')
        config = json.loads(json_env_spec)
        #DEBUG
        # print(f'from json config:{config}, type:{type(config)}')
        env_spec = EnvSpec.from_json(config['env'])
        #DEBUG
        # print(f'wrappers in gym from json:{config["wrappers"]}')
        try:
            return cls(env_spec, config["wrappers"])
        except Exception as e:
            raise ValueError(f"Environment wrapper error: {config}, {e}")
    
class IsaacSimWrapper(EnvWrapper):
    def __init__(self, env_spec):
        """
        Placeholder wrapper for Isaac Sim environments.

        This class is a template and needs implementation based on Isaac Sim's API.
        """
        pass