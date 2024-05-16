import sys
import json
import logging
import argparse

from rl_agents import HER

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

parser = argparse.ArgumentParser(description='Train Agent')
parser.add_argument('--agent_config', type=str, required=True, help='Path to the agent configuration file')
parser.add_argument('--train_config', type=str, required=True, help='Path to the train configuration file')

args = parser.parse_args()

agent_config_path = args.agent_config
train_config_path = args.train_config

def train_agent(agent_config, train_config):
    try:
        agent_type = agent_config['agent_type']
        load_weights = train_config['load_weights']
        num_epochs = train_config['num_epochs']
        num_cycles = train_config['num_cycles']
        num_episodes = train_config['num_episodes']
        num_updates = train_config['num_updates']
        render = train_config['render']
        render_freq = train_config['render_freq']
        save_dir = agent_config['save_dir'] if train_config['save_dir'] is None else train_config['save_dir']
        run_number = train_config['run_number']

        assert agent_type == 'HER', f"Unsupported agent type: {agent_type}"

        if agent_type:
            agent = HER.load(agent_config, load_weights)
            agent.train(num_epochs, num_cycles, num_episodes, num_updates, render, render_freq, save_dir, run_number)

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

        with open(train_config_path, 'r', encoding="utf-8") as f:
            train_config = json.load(f)

        train_agent(agent_config, train_config)

    except FileNotFoundError as e:
        logging.error(f"Configuration file not found: {str(e)}")

    except json.JSONDecodeError as e:
        logging.error(f"Invalid JSON format in configuration file: {str(e)}")