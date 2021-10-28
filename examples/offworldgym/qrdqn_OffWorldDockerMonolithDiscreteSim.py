import argparse
import os
import pprint
import datetime

import numpy as np
import torch
from atari.atari_network import QRDQN
from atari.atari_wrapper import wrap_offworld
from torch.utils.tensorboard import SummaryWriter

from tianshou.data import Collector, VectorReplayBuffer
from tianshou.env import ShmemVectorEnv, DummyVectorEnv
from tianshou.policy import QRDQNPolicy
from tianshou.trainer import offpolicy_trainer
from tianshou.utils import TensorboardLogger

import gym
import offworld_gym
from offworld_gym.envs.common.channels import Channels

def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', type=str, default='OffWorldDockerMonolithDiscreteSim-v0')
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--eps-test', type=float, default=0.005)
    parser.add_argument('--eps-train', type=float, default=1.)
    parser.add_argument('--eps-train-final', type=float, default=0.05)
    parser.add_argument('--buffer-size', type=int, default=25000)
    parser.add_argument('--lr', type=float, default=0.0001)
    parser.add_argument('--gamma', type=float, default=0.95)
    parser.add_argument('--num-quantiles', type=int, default=100)
    parser.add_argument('--n-step', type=int, default=3)
    parser.add_argument('--target-update-freq', type=int, default=500)
    parser.add_argument('--epoch', type=int, default=800)
    parser.add_argument('--step-per-epoch', type=int, default=2500)
    parser.add_argument('--step-per-collect', type=int, default=100) # 10, change it to 100 to sync train and test reward curve
    parser.add_argument('--update-per-step', type=float, default=0.1)
    parser.add_argument('--batch-size', type=int, default=256)
    parser.add_argument('--training-num', type=int, default=10)
    parser.add_argument('--test-num', type=int, default=10)
    parser.add_argument('--logdir', type=str, default='log')
    parser.add_argument('--render', type=float, default=0.)
    parser.add_argument('--tag', type=str, default=None)
    parser.add_argument(
        '--device', type=str, default='cuda' if torch.cuda.is_available() else 'cpu'
    )
    parser.add_argument('--frames-stack', type=int, default=4)
    parser.add_argument('--resume-path', type=str, default=None)
    parser.add_argument(
        '--watch',
        default=False,
        action='store_true',
        help='watch the play of pre-trained policy only'
    )
    parser.add_argument('--save-buffer-name', type=str, default=None)
    return parser.parse_args()

def make_offworld_env(args):
    return wrap_offworld(args.task)


def make_offworld_env_watch(args):
    return wrap_offworld(
        args.task
    ) 

def test_qrdqn(args=get_args()):
    args.state_shape = (4, 84, 84)
    args.action_shape = 4
    # should be N_FRAMES x H x W
    print("Observations shape:", args.state_shape)
    print("Actions shape:", args.action_shape)
    # make environments
    if args.training_num:
        train_envs = DummyVectorEnv(
            [lambda: make_offworld_env(args) for _ in range(args.training_num)]
        )
    
    if args.test_num:
        test_envs = DummyVectorEnv(
            [lambda: make_offworld_env_watch(args) for _ in range(args.test_num)]
        )

    # seed
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.training_num:
        train_envs.seed(args.seed)
    test_envs.seed(args.seed)
    # define model
    net = QRDQN(*args.state_shape, args.action_shape, args.num_quantiles, args.device)
    optim = torch.optim.Adam(net.parameters(), lr=args.lr)
    # define policy
    policy = QRDQNPolicy(
        net,
        optim,
        args.gamma,
        args.num_quantiles,
        args.n_step,
        target_update_freq=args.target_update_freq
    ).to(args.device)
    # load a previous policy
    if args.resume_path:
        policy.load_state_dict(torch.load(args.resume_path, map_location=args.device))
        print("Loaded agent from: ", args.resume_path)
    # replay buffer: `save_last_obs` and `stack_num` can be removed together
    # when you have enough RAM
    if args.training_num:
        buffer = VectorReplayBuffer(
            args.buffer_size,
            buffer_num=len(train_envs),
            ignore_obs_next=True,
            save_only_last_obs=True,
            stack_num=args.frames_stack
        )
    # collector
    if args.training_num:
        train_collector = Collector(policy, train_envs, buffer, exploration_noise=True)
    
    if args.test_num:
        test_collector = Collector(policy, test_envs, exploration_noise=True)
    # log
    log_path = None
    if args.tag is None:
        log_path = os.path.join(args.logdir, args.task, 'qrdqn', datetime.datetime.now().isoformat())
    else:
        log_path = os.path.join(args.logdir, args.task, 'qrdqn', args.task)

    if not os.path.exists(log_path):
        os.makedirs(log_path)

    writer = SummaryWriter(log_path)
    writer.add_text("args", str(args))
    logger = TensorboardLogger(writer)

    def save_fn(policy):
        print(f"Saving the value network here: {log_path}.")
        torch.save(policy.state_dict(), os.path.join(log_path, 'value.pth'))

    def stop_fn(mean_rewards):
        return False

    def train_fn(epoch, env_step):
        # nature DQN setting, linear decay in the first 1M steps
        if env_step <= 1e6:
            eps = args.eps_train - env_step / 1e6 * \
                (args.eps_train - args.eps_train_final)
        else:
            eps = args.eps_train_final
        policy.set_eps(eps)
        if env_step % 1000 == 0:
            logger.write("train/env_step", env_step, {"train/eps": eps})

    def test_fn(epoch, env_step):
        policy.set_eps(args.eps_test)

    # watch agent's performance
    def watch():
        print("Setup test envs ...")
        policy.eval()
        policy.set_eps(args.eps_test)
        test_envs.seed(args.seed)
        if args.save_buffer_name:
            print(f"Generate buffer with size {args.buffer_size}")
            buffer = VectorReplayBuffer(
                args.buffer_size,
                buffer_num=len(test_envs),
                ignore_obs_next=True,
                save_only_last_obs=True,
                stack_num=args.frames_stack
            )
            collector = Collector(policy, test_envs, buffer, exploration_noise=True)
            result = collector.collect(n_step=args.buffer_size)
            print(f"Save buffer into {args.save_buffer_name}")
            # Unfortunately, pickle will cause oom with 1M buffer size
            buffer.save_hdf5(args.save_buffer_name)
        else:
            print("Testing agent ...")
            test_collector.reset()
            result = test_collector.collect(
                n_episode=args.test_num, render=args.render
            )
        rew = result["rews"].mean()
        print(f'Mean reward (over {result["n/ep"]} episodes): {rew}')

    if args.watch:
        watch()
        exit(0)

    if args.training_num:
        # test train_collector and start filling replay buffer
        train_collector.collect(n_step=args.batch_size * args.training_num)
        # trainer
        result = offpolicy_trainer(
            policy,
            train_collector,
            test_collector,
            args.epoch,
            args.step_per_epoch,
            args.step_per_collect,
            args.test_num,
            args.batch_size,
            train_fn=train_fn,
            test_fn=test_fn,
            stop_fn=stop_fn,
            save_fn=save_fn,
            logger=logger,
            update_per_step=args.update_per_step,
            test_in_train=False
        )

        pprint.pprint(result)
        watch()


if __name__ == '__main__':
    test_qrdqn(get_args())