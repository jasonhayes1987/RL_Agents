import sys
import json
import logging
import argparse
import subprocess

import random
import numpy as np
import torch as T

from rl_agents import load_agent_from_config

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
parser = argparse.ArgumentParser(description='Test Agent')
parser.add_argument('--agent_config', type=str, required=True, help='Path to the agent configuration file')
parser.add_argument('--test_config', type=str, required=True, help='Path to the test configuration file')

args = parser.parse_args()

agent_config_path = args.agent_config
test_config_path = args.test_config

def test_agent(agent_config, test_config):
    try:
        
        agent_type = agent_config['agent_type']
        load_weights = test_config['load_weights']
        num_episodes = test_config['num_episodes']
        render = test_config['render']
        render_freq = test_config['render_freq']
        seed = test_config['seed']
        run_number = test_config['run_number']
        num_runs = test_config['num_runs']

        # set seed
        random.seed(seed)
        np.random.seed(seed)
        T.manual_seed(seed)
        T.cuda.manual_seed(seed)

        print(f'seed: {seed}')

        assert agent_type in ['Reinforce', 'ActorCritic', 'DDPG', 'HER'], f"Unsupported agent type: {agent_type}"

        if agent_type:
            agent = load_agent_from_config(agent_config, load_weights)
            print('agent config loaded')
            for i in range(num_runs):
                agent.test(num_episodes, render, render_freq, run_number)
                print(f'testing run {i+1} initiated')

    except KeyError as e:
        logging.error(f"Missing configuration parameter: {str(e)}")
        raise

    except AssertionError as e:
        logging.error(str(e))
        raise

    except Exception as e:
        logging.exception("An unexpected error occurred during training")
        raise

if __name__ == '__main__':
    try:
        with open(agent_config_path, 'r', encoding="utf-8") as f:
            agent_config = json.load(f)

        with open(test_config_path, 'r', encoding="utf-8") as f:
            test_config = json.load(f)

        test_agent(agent_config, test_config)

    except FileNotFoundError as e:
        logging.error(f"Configuration file not found: {str(e)}")

    except json.JSONDecodeError as e:
        logging.error(f"Invalid JSON format in configuration file: {str(e)}")