import copy
import os

import torch
import torch.nn as nn
import torch.optim as optim
from torch.distributions import Categorical

# Hyperparameters
from reinforcement_learning.policy import Policy

device = torch.device("cpu")  # "cuda:0" if torch.cuda.is_available() else "cpu")
print("device:", device)


class DataBuffers:
    def __init__(self):
        self.reset()

    def __len__(self):
        """Return the current size of internal memory."""
        return len(self.memory)

    def reset(self):
        self.memory = {}

    def get_transitions(self, handle):
        return self.memory.get(handle, [])

    def push_transition(self, handle, transition):
        transitions = self.get_transitions(handle)
        transitions.append(transition)
        self.memory.update({handle: transitions})


class ActorCriticModel(nn.Module):

    def __init__(self, state_size, action_size, hidsize1=128, hidsize2=128):
        super(ActorCriticModel, self).__init__()
        self.actor = nn.Sequential(
            nn.Linear(state_size, hidsize1),
            nn.Tanh(),
            nn.Linear(hidsize1, hidsize2),
            nn.Tanh(),
            nn.Linear(hidsize2, action_size),
            nn.Softmax(dim=-1)
        )

        self.critic = nn.Sequential(
            nn.Linear(state_size, hidsize1),
            nn.Tanh(),
            nn.Linear(hidsize1, hidsize2),
            nn.Tanh(),
            nn.Linear(hidsize2, 1)
        )

    def forward(self, x):
        raise NotImplementedError

    def get_actor_dist(self, state):
        action_probs = self.actor(state)
        dist = Categorical(action_probs)
        return dist

    def evaluate(self, states, actions):
        action_probs = self.actor(states)
        dist = Categorical(action_probs)
        action_logprobs = dist.log_prob(actions)
        dist_entropy = dist.entropy()
        state_value = self.critic(states)
        return action_logprobs, torch.squeeze(state_value), dist_entropy

    def save(self, filename):
        # print("Saving model from checkpoint:", filename)
        torch.save(self.actor.state_dict(), filename + ".actor")
        torch.save(self.critic.state_dict(), filename + ".value")

    def _load(self, obj, filename):
        if os.path.exists(filename):
            print(' >> ', filename)
            try:
                obj.load_state_dict(torch.load(filename, map_location=device))
            except:
                print(" >> failed!")
        return obj

    def load(self, filename):
        print("load policy from file", filename)
        self.actor = self._load(self.actor, filename + ".actor")
        self.critic = self._load(self.critic, filename + ".critic")


class PPOAgent(Policy):
    def __init__(self, state_size, action_size):
        super(PPOAgent, self).__init__()

        # parameters
        self.learning_rate = 0.1e-4
        self.gamma = 0.99
        self.surrogate_eps_clip = 0.2
        self.K_epoch = 30
        self.weight_loss = 1.0
        self.weight_entropy = 0.01

        # objects
        self.memory = DataBuffers()
        self.loss = 0
        self.actor_critic_model = ActorCriticModel(state_size, action_size)
        self.optimizer = optim.Adam(self.actor_critic_model.parameters(), lr=self.learning_rate)
        self.loss_function = nn.SmoothL1Loss()  # nn.MSELoss()

    def reset(self):
        pass

    def act(self, state, eps=None):
        # sample a action to take
        torch_state = torch.tensor(state, dtype=torch.float).to(device)
        dist = self.actor_critic_model.get_actor_dist(torch_state)
        action = dist.sample()
        return action.item()

    def step(self, handle, state, action, reward, next_state, done):
        # record transitions ([state] -> [action] -> [reward, next_state, done])
        torch_action = torch.tensor(action, dtype=torch.float).to(device)
        torch_state = torch.tensor(state, dtype=torch.float).to(device)
        # evaluate actor
        dist = self.actor_critic_model.get_actor_dist(torch_state)
        action_logprobs = dist.log_prob(torch_action)
        transition = (state, action, reward, next_state, action_logprobs.item(), done)
        self.memory.push_transition(handle, transition)

    def _convert_transitions_to_torch_tensors(self, transitions_array):
        # build empty lists(arrays)
        state_list, action_list, reward_list, state_next_list, prob_a_list, done_list = [], [], [], [], [], []

        # set discounted_reward to zero
        discounted_reward = 0
        for transition in transitions_array[::-1]:
            state_i, action_i, reward_i, state_next_i, prob_action_i, done_i = transition

            state_list.insert(0, state_i)
            action_list.insert(0, action_i)
            if done_i:
                discounted_reward = 0
                done_list.insert(0, 1)
            else:
                discounted_reward = reward_i + self.gamma * discounted_reward
                done_list.insert(0, 0)
            reward_list.insert(0, discounted_reward)
            state_next_list.insert(0, state_next_i)
            prob_a_list.insert(0, prob_action_i)

        # convert data to torch tensors
        states, actions, rewards, states_next, dones, prob_actions = \
            torch.tensor(state_list, dtype=torch.float).to(device), \
            torch.tensor(action_list).to(device), \
            torch.tensor(reward_list, dtype=torch.float).to(device), \
            torch.tensor(state_next_list, dtype=torch.float).to(device), \
            torch.tensor(done_list, dtype=torch.float).to(device), \
            torch.tensor(prob_a_list).to(device)

        # standard-normalize rewards
        rewards = (rewards - rewards.mean()) / (rewards.std() + 1.e-5)

        return states, actions, rewards, states_next, dones, prob_actions

    def train_net(self):
        # Optimize policy for K epochs:
        for _ in range(self.K_epoch):
            # All agents have to propagate their experiences made during past episode
            for handle in range(len(self.memory)):
                # Extract agent's episode history (list of all transitions)
                agent_episode_history = self.memory.get_transitions(handle)
                if len(agent_episode_history) > 0:
                    # Convert the replay buffer to torch tensors (arrays)
                    states, actions, rewards, states_next, dones, probs_action = \
                        self._convert_transitions_to_torch_tensors(agent_episode_history)

                    # Evaluating actions (actor) and values (critic)
                    logprobs, state_values, dist_entropy = self.actor_critic_model.evaluate(states, actions)

                    # Finding the ratios (pi_thetas / pi_thetas_replayed):
                    ratios = torch.exp(logprobs - probs_action.detach())

                    # Finding Surrogate Loos
                    advantages = rewards - state_values.detach()
                    surr1 = ratios * advantages
                    surr2 = torch.clamp(ratios, 1. - self.surrogate_eps_clip, 1. + self.surrogate_eps_clip) * advantages

                    # The loss function is used to estimate the gardient and use the entropy function based
                    # heuristic to penalize the gradient function when the policy becomes deterministic this would let
                    # the gardient to become very flat and so the gradient is no longer useful.
                    loss = \
                        -torch.min(surr1, surr2) \
                        + self.weight_loss * self.loss_function(state_values, rewards) \
                        - self.weight_entropy * dist_entropy

                    # Make a gradient step
                    self.optimizer.zero_grad()
                    loss.mean().backward()
                    self.optimizer.step()

                    # Transfer the current loss to the agents loss (information) for debug purpose only
                    self.loss = loss.mean().detach().numpy()

        # Reset all collect transition data
        self.memory.reset()

    def end_episode(self, train):
        if train:
            self.train_net()

    # Checkpointing methods
    def save(self, filename):
        # print("Saving model from checkpoint:", filename)
        self.actor_critic_model.save(filename)
        torch.save(self.optimizer.state_dict(), filename + ".optimizer")

    def _load(self, obj, filename):
        if os.path.exists(filename):
            print(' >> ', filename)
            try:
                obj.load_state_dict(torch.load(filename, map_location=device))
            except:
                print(" >> failed!")
        return obj

    def load(self, filename):
        print("load policy from file", filename)
        self.actor_critic_model.load(filename)
        print("load optimizer from file", filename)
        self.optimizer = self._load(self.optimizer, filename + ".optimizer")

    def clone(self):
        policy = PPOAgent(self.state_size, self.action_size)
        policy.actor_critic_model = copy.deepcopy(self.actor_critic_model)
        policy.optimizer = copy.deepcopy(self.optimizer)
        return self
