import sys
import json
import time
from logging_config import logger
import argparse
import subprocess

import random
import numpy as np
import torch as T
import wandb

from rl_agents import load_agent_from_config

# Configure logging
# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

parser = argparse.ArgumentParser(description='Train Agent')
parser.add_argument('--agent_config', type=str, required=True, help='Path to the agent configuration file')
parser.add_argument('--train_config', type=str, required=True, help='Path to the train configuration file')

args = parser.parse_args()

agent_config_path = args.agent_config
train_config_path = args.train_config

def train_agent(agent_config, train_config):

    # wandb_initialized = False  # Track if wandb is initialized
    try:
        agent_type = agent_config['agent_type']
        print(f'agent type:{agent_type}')
        load_weights = train_config.get('load_weights', False)
        num_episodes = train_config['num_episodes']
        render = train_config.get('render', False)
        render_freq = train_config.get('render_freq', 0)
        save_dir = train_config.get('save_dir', agent_config['save_dir'])
        #DEBUG
        # print(f'training save dir: {save_dir}')
        seed = train_config['seed']
        run_number = train_config.get('run_number', None)
        # num_runs = train_config.get('num_runs', 1)

        # MPI flag
        use_mpi = train_config.get('use_mpi', False)

        # set seed
        random.seed(seed)
        np.random.seed(seed)
        T.manual_seed(seed)
        T.cuda.manual_seed(seed)

        print(f'seed: {seed}')

        assert agent_type in ['Reinforce', 'ActorCritic', 'DDPG', 'TD3', 'HER'], f"Unsupported agent type: {agent_type}"

        # Check if WandbCallback is in the agent configuration
        # use_wandb = any(cb['class_name'] == 'WandbCallback' for cb in agent_config['agent']['callbacks'])

        # Initialize WandB run if WandbCallback is present
        # if use_wandb:
        #     logging.info("Initializing WandB run")
        #     wandb.init(
        #         project="your_project_name",
        #         name=f"train-{train_config['run_number']}",
        #         config=train_config,
        #         # settings=wandb.Settings(start_method='thread')
        #     )
        #     wandb_initialized = True

        if agent_type:
            agent = load_agent_from_config(agent_config, load_weights)
            print('agent config loaded')
            print(f'env:{agent.env.spec}')

            if agent_type == 'HER':

                if use_mpi:
                    num_workers = train_config['num_workers']
                    # Execute the MPI command for HER agent
                    mpi_command = f"mpirun -np {num_workers} python train_her_mpi.py --agent_config {agent_config_path} --train_config {train_config_path}"
                    # for i in range(num_runs):
                    subprocess.Popen(mpi_command, shell=True)
                    # print(f'training run {i+1} initiated')
                    # time.sleep(5)
                
                else:
                    num_epochs = train_config['num_epochs']
                    num_cycles = train_config['num_cycles']
                    num_updates = train_config['num_updates']
                    # for i in range(num_runs):
                    agent.train(num_epochs, num_cycles, num_episodes, num_updates, render, render_freq, save_dir, run_number)
                    # print(f'training run {i+1} initiated')
            
            elif agent_type == 'DDPG':

                if use_mpi:
                    num_workers = train_config['num_workers']
                    mpi_command = f"mpirun -np {num_workers} python train_ddpg_mpi.py --agent_config {agent_config_path} --train_config {train_config_path}"
                    # for i in range(num_runs):
                    subprocess.Popen(mpi_command, shell=True)
                    # print(f'training run {i+1} initiated')
                
                else:
                    # for i in range(num_runs):
                    agent.train(num_episodes, render, render_freq)
                    # print(f'training run {i+1} initiated')

            elif agent_type == 'TD3':

                if use_mpi:
                    num_workers = train_config['num_workers']
                    mpi_command = f"mpirun -np {num_workers} python train_td3_mpi.py --agent_config {agent_config_path} --train_config {train_config_path}"
                    # for i in range(num_runs):
                    subprocess.Popen(mpi_command, shell=True)
                    # print(f'training run {i+1} initiated')
                
                else:
                    # for i in range(num_runs):
                    agent.train(num_episodes, render, render_freq, run_number=run_number)
                    # print(f'training run {i+1} initiated')

    except KeyError as e:
        logger.error(f"Missing configuration parameter: {str(e)}")
        raise

    except AssertionError as e:
        logger.error(str(e))
        raise

    except Exception as e:
        logger.exception("An unexpected error occurred during training")
        raise
    # finally:
    #     # Ensure the WandB run is properly finished if it was initialized
    #     if wandb_initialized:
    #         wandb.finish()
    #         logging.info("WandB run finished")

if __name__ == '__main__':
    try:
        with open(agent_config_path, 'r', encoding="utf-8") as f:
            agent_config = json.load(f)

        with open(train_config_path, 'r', encoding="utf-8") as f:
            train_config = json.load(f)

        train_agent(agent_config, train_config)

    except FileNotFoundError as e:
        logger.error(f"Configuration file not found: {str(e)}")

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON format in configuration file: {str(e)}")