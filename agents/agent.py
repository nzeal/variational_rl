import copy
import torch
import torch.nn as nn
import numpy as np
from abc import abstractmethod
from misc import one_hot_to_index
from misc.collector import Collector
from misc.normalization import Normalizer


class Agent(nn.Module):
    """
    Variational RL Agent
    """
    def __init__(self, misc_args):
        super(Agent, self).__init__()

        # models
        self.state_prior_model = None
        self.action_prior_model = None
        self.obs_likelihood_model = None
        self.reward_likelihood_model = None
        self.done_likelihood_model = None
        self.state_inference_model = None
        self.action_inference_model = None
        self.q_value_models = None
        self.target_q_value_models = None
        self.target_action_prior_model = None
        self.target_action_inference_model = None

        # variables
        self.state_variable = None
        self.action_variable = None
        self.observation_variable = None
        self.reward_variable = None
        self.done_variable = None
        self.q_value_variables = None
        self.target_q_value_variables = None
        self.target_action_variable = None

        self.log_alphas = nn.ParameterDict({'pi': nn.Parameter(torch.zeros(1)),
                                            'loc': nn.Parameter(torch.zeros(1)),
                                            'scale': nn.Parameter(torch.zeros(1))})

        # miscellaneous
        self.reward_scale = misc_args['reward_scale']
        self.n_action_samples = misc_args['n_action_samples']
        self.kl_scale = {'state': misc_args['kl_scale']['state'],
                         'action': misc_args['kl_scale']['action']}
        self.kl_min = {'state': misc_args['kl_min']['state'],
                       'action': misc_args['kl_min']['action']}
        self.kl_min_anneal_rate = {'state': misc_args['kl_min_anneal_rate']['state'],
                                   'action': misc_args['kl_min_anneal_rate']['action']}
        self.kl_factor = {'state': misc_args['kl_factor']['state'],
                          'action': misc_args['kl_factor']['action']}
        self.kl_factor_anneal_rate = {'state': misc_args['kl_factor_anneal_rate']['state'],
                                      'action': misc_args['kl_factor_anneal_rate']['action']}
        self.retrace_lambda = misc_args['retrace_lambda']
        self.epsilons = misc_args['epsilons']
        self.postprocess_action = misc_args['postprocess_action']

        # mode (either 'train' or 'eval')
        self._mode = 'train'

        # collects relevant quantities
        self.collector = Collector(self)

        self._prev_action = None
        self._prev_obs = None
        self.batch_size = 1
        self.reward_discount = misc_args['reward_discount']

        # normalizers for various quantities
        self.return_normalizer = None
        if misc_args['normalize_returns']:
            self.return_normalizer = Normalizer(shift=False, clip_value=10.)
        self.advantage_normalizer = None
        if misc_args['normalize_advantages']:
            self.advantage_normalizer = Normalizer(clip_value=10.)
        self.obs_normalizer = None
        if misc_args['normalize_observations']:
            observation_size = misc_args['observation_size']
            # TODO: should set this in a better way, in case of image input
            self.obs_normalizer = Normalizer(shape=(observation_size), clip_value=10.)

    def act(self, observation, reward=None, done=False, action=None, valid=None, log_prob=None):
        observation, reward, action, done, valid, log_prob = self._change_device(observation, reward, action, done, valid, log_prob)
        self.step_state(observation=observation, reward=reward, done=done, valid=valid)
        self.state_inference(observation=observation, reward=reward, done=done, valid=valid)
        self.step_action(observation=observation, reward=reward, done=done, valid=valid, action=action)
        self.action_inference(observation=observation, reward=reward, done=done, valid=valid, action=action)
        self.estimate_q_values(observation=observation, action=action, reward=reward, done=done, valid=valid)
        self._prev_obs = observation
        if self._mode == 'train':
            self._prev_action = action
        else:
            if observation is not None and action is None:
                action = self.action_variable.sample()
                self._prev_action = action
                action = self._convert_action(action)
        self.collector.collect(observation, reward, done, action, valid, log_prob)
        if self.postprocess_action:
            action = action.tanh()
        return action.cpu().numpy()

    @abstractmethod
    def step_action(self, observation, reward, done, valid, action=None):
        pass

    @abstractmethod
    def action_inference(self, observation, reward, done, valid, action=None):
        pass

    def step_state(self, observation, reward, done, valid):
        pass

    def state_inference(self, observation, reward, done, valid):
        pass

    def generate(self):
        self.generate_observation()
        self.generate_reward()
        self.generate_done()

    def generate_observation(self):
        # generate the conditional likelihood for the observation
        pass

    def generate_reward(self):
        # generate the conditional likelihood for the reward
        pass

    def generate_done(self):
        # generate the conditional likelihood for episode being done
        pass

    def estimate_q_values(self, done, observation, reward, action, **kwargs):
        # estimate the value of the current state
        state = self.state_variable.sample() if self.state_variable is not None else None
        if action is not None:
            # if self.postprocess_action:
            #     action = action.tanh()
            # estimate Q values for off-policy actions sample from the buffer
            q_value_input = [model(state=state, observation=observation, action=action, reward=reward) for model in self.q_value_models]
            q_values = [variable(inp) for variable, inp in zip(self.q_value_variables, q_value_input)]
            self.collector.qvalues1.append(q_values[0])
            self.collector.qvalues2.append(q_values[1])
            # self.collector.qvalues.append(torch.min(q_values[0], q_values[1]))

        # get on-policy actions and log probs
        new_action = self.action_variable.sample(self.n_action_samples)
        # new_action = self.target_action_variable.sample(self.n_action_samples)
        # new_action_log_prob = self.action_variable.approx_post.log_prob(new_action).mean(dim=0).sum(dim=1, keepdim=True)
        self.collector.new_actions.append(new_action)
        if self.postprocess_action:
            new_action = new_action.tanh()
        # self.collector.new_action_log_probs.append(new_action_log_prob)
        expanded_obs = observation.repeat(self.n_action_samples, 1)

        # estimate Q value for on-policy actions sampled from current policy
        new_q_value_models = copy.deepcopy(self.q_value_models)
        new_q_value_variables = copy.deepcopy(self.q_value_variables)
        new_q_value_input = [model(state=state, observation=expanded_obs, action=new_action, reward=reward) for model in new_q_value_models]
        new_q_values = [variable(inp) for variable, inp in zip(new_q_value_variables, new_q_value_input)]
        sample_new_q_values = torch.min(new_q_values[0], new_q_values[1])
        self.collector.sample_new_q_values.append(sample_new_q_values)
        avg_new_q_value = torch.min(new_q_values[0].view(self.n_action_samples, -1, 1).mean(dim=0), new_q_values[1].view(self.n_action_samples, -1, 1).mean(dim=0))
        self.collector.new_q_values.append(avg_new_q_value)

        # estimate target Q value for on-policy actions sampled from current policy
        target_q_value_input = [model(state=state, observation=expanded_obs, action=new_action, reward=reward) for model in self.target_q_value_models]
        target_q_values = [variable(inp).view(self.n_action_samples, -1, 1).mean(dim=0) for variable, inp in zip(self.target_q_value_variables, target_q_value_input)]
        target_q_value = torch.min(target_q_values[0], target_q_values[1])
        self.collector.target_q_values.append(target_q_value)

        # return torch.min(q_values[0], q_values[1])

    def evaluate(self):
        # evaluate the objective, collect various metrics for reporting
        objective = self.collector.evaluate()
        objective.backward()

        results = {}
        for k, v in self.collector.get_metrics().items():
            results[k] = v
        for k, v in self.collector.get_inf_imp().items():
            results[k] = v
        for k, v in self.collector.get_grads().items():
            results[k] = v

        results['kl_min'] = self.kl_min
        results['kl_factor'] = self.kl_factor
        # if self.observation_variable is not None:
        #     results['marginal_factor'] = self.marginal_factor

        return results

    def get_episode(self):
        return self.collector.get_episode()

    def _convert_action(self, action):
        # converts categorical action from one-hot encoding to the action index
        if self.action_variable.approx_post.dist_type == getattr(torch.distributions, 'Categorical'):
            action = one_hot_to_index(action)
        return action.detach()

    def _change_device(self, observation, reward, action, done, valid, log_prob):
        if observation is None:
            observation = torch.zeros(self.collector.episode['observation'][0].shape)
        elif type(observation) == np.ndarray:
            observation = torch.from_numpy(observation.astype('float32')).view(1, -1) # hack
        if observation.device != self.device:
            observation = observation.to(self.device)
        if type(reward) in [float, int]:
            reward = torch.tensor(reward).to(torch.float32).view(1, 1)
        elif type(reward) == np.ndarray:
            reward = torch.from_numpy(reward.astype('float32')).view(1, 1) # hack
        if reward.device != self.device:
            reward = reward.to(self.device)
        if action is not None:
            if type(action) == np.ndarray:
                action = torch.from_numpy(action).view(1, -1)
            if action.device != self.device:
                action = action.to(self.device)
        if type(done) == bool:
            done = torch.tensor(done).to(torch.float32).view(1, 1)
        elif type(done) == np.ndarray:
            done = torch.from_numpy(done.astype('float32')).view(1, 1) # hack
        if done.device != self.device:
            done = done.to(self.device)
        if valid is None:
            valid = torch.ones(done.shape[0], 1)
        if valid.device != self.device:
            valid = valid.to(self.device)
        if log_prob is not None:
            log_prob = log_prob.to(self.device)
        return observation, reward, action, done, valid, log_prob

    def reset(self, batch_size=1, prev_action=None, prev_obs=None):
        # reset the variables
        self.action_variable.reset(batch_size)
        if self.target_action_variable is not None:
            self.target_action_variable.reset(batch_size)
        if self.state_variable is not None:
            self.state_variable.reset(batch_size)
        if self.observation_variable is not None:
            self.observation_variable.reset(batch_size, prev_obs=prev_obs)
        if self.reward_variable is not None:
            self.reward_variable.reset(batch_size)
        if self.done_variable is not None:
            self.done_variable.reset(batch_size)

        # reset the networks
        if self.state_prior_model is not None:
            self.state_prior_model.reset(batch_size)
        if self.action_prior_model is not None:
            self.action_prior_model.reset(batch_size)
        if self.target_action_prior_model is not None:
            self.target_action_prior_model.reset(batch_size)
        if self.obs_likelihood_model is not None:
            self.obs_likelihood_model.reset(batch_size)
        if self.reward_likelihood_model is not None:
            self.reward_likelihood_model.reset(batch_size)
        if self.done_likelihood_model is not None:
            self.done_likelihood_model.reset(batch_size)

        # reset the collector
        self.collector.reset()

        self.batch_size = batch_size
        if prev_action is not None:
            self._prev_action = prev_action.to(self.device)
        else:
            act = self.action_variable.sample()
            self._prev_action = act.new(act.shape).zero_()
        if self.observation_variable is not None:
            if prev_obs is not None:
                self._prev_obs = prev_obs.to(self.device)
            else:
                obs = self.observation_variable.sample()
                self._prev_obs = obs.new(obs.shape).zero_()

        # clamp log-alphas to prevent collapse
        for name, log_alpha in self.log_alphas.items():
            log_alpha = torch.clamp(log_alpha, min=-15.)

    @property
    def device(self):
        p = self.parameters()
        return p[list(p.keys())[0]][0].device

    @property
    def alphas(self):
        return {name: self.log_alphas[name].exp().detach() for name in self.log_alphas}

    def train(self, *args):
        super(Agent, self).train(*args)
        self._mode = 'train'

    def eval(self, *args):
        super(Agent, self).eval(*args)
        self._mode = 'eval'

    def parameters(self):
        param_dict = {}

        if self.state_inference_model is not None:
            param_dict['state_inference_model'] = nn.ParameterList()
            param_dict['state_inference_model'].extend(list(self.state_inference_model.parameters()))
            param_dict['state_inference_model'].extend(list(self.state_variable.inference_parameters()))

        if self.action_inference_model is not None:
            param_dict['action_inference_model'] = nn.ParameterList()
            param_dict['action_inference_model'].extend(list(self.action_inference_model.parameters()))
            param_dict['action_inference_model'].extend(list(self.action_variable.inference_parameters()))

        if self.target_action_inference_model is not None:
            param_dict['target_action_inference_model'] = nn.ParameterList()
            param_dict['target_action_inference_model'].extend(list(self.target_action_inference_model.parameters()))
            param_dict['target_action_inference_model'].extend(list(self.target_action_variable.inference_parameters()))

        if self.state_prior_model is not None:
            param_dict['state_prior_model'] = nn.ParameterList()
            param_dict['state_prior_model'].extend(list(self.state_prior_model.parameters()))
            param_dict['state_prior_model'].extend(list(self.state_variable.generative_parameters()))

        if self.action_prior_model is not None:
            param_dict['action_prior_model'] = nn.ParameterList()
            param_dict['action_prior_model'].extend(list(self.action_prior_model.parameters()))
            param_dict['action_prior_model'].extend(list(self.action_variable.generative_parameters()))

        if self.target_action_prior_model is not None:
            param_dict['target_action_prior_model'] = nn.ParameterList()
            param_dict['target_action_prior_model'].extend(list(self.target_action_prior_model.parameters()))
            param_dict['target_action_prior_model'].extend(list(self.target_action_variable.generative_parameters()))

        if self.obs_likelihood_model is not None:
            param_dict['obs_likelihood_model'] = nn.ParameterList()
            param_dict['obs_likelihood_model'].extend(list(self.obs_likelihood_model.parameters()))
            param_dict['obs_likelihood_model'].extend(list(self.observation_variable.parameters()))

        if self.reward_likelihood_model is not None:
            param_dict['reward_likelihood_model'] = nn.ParameterList()
            param_dict['reward_likelihood_model'].extend(list(self.reward_likelihood_model.parameters()))
            param_dict['reward_likelihood_model'].extend(list(self.reward_variable.parameters()))

        if self.done_likelihood_model is not None:
            param_dict['done_likelihood_model'] = nn.ParameterList()
            param_dict['done_likelihood_model'].extend(list(self.done_likelihood_model.parameters()))
            param_dict['done_likelihood_model'].extend(list(self.done_variable.parameters()))

        if self.q_value_models is not None:
            param_dict['q_value_models'] = nn.ParameterList()
            param_dict['q_value_models'].extend(list(self.q_value_models.parameters()))
            param_dict['q_value_models'].extend(list(self.q_value_variables.parameters()))

        if self.target_q_value_models is not None:
            param_dict['target_q_value_models'] = nn.ParameterList()
            param_dict['target_q_value_models'].extend(list(self.target_q_value_models.parameters()))
            param_dict['target_q_value_models'].extend(list(self.target_q_value_variables.parameters()))

        if self.log_alphas is not None:
            param_dict['log_alphas'] = nn.ParameterList()
            for name in self.log_alphas:
                param_dict['log_alphas'].append(self.log_alphas[name])

        return param_dict

    def inference_parameters(self):
        params = nn.ParameterList()
        if self.state_inference_model is not None:
            params.extend(list(self.state_inference_model.parameters()))
            params.extend(list(self.state_variable.inference_parameters()))
        if self.action_inference_model is not None:
            params.extend(list(self.action_inference_model.parameters()))
            params.extend(list(self.action_variable.inference_parameters()))
        return params

    def generative_parameters(self):
        params = nn.ParameterList()
        if self.state_prior_model is not None:
            params.extend(list(self.state_prior_model.parameters()))
            params.extend(list(self.state_variable.generative_parameters()))
        if self.action_prior_model is not None:
            params.extend(list(self.action_prior_model.parameters()))
            params.extend(list(self.action_variable.generative_parameters()))
        if self.obs_likelihood_model is not None:
            params.extend(list(self.obs_likelihood_model.parameters()))
            params.extend(list(self.observation_variable.parameters()))
        if self.reward_likelihood_model is not None:
            params.extend(list(self.reward_likelihood_model.parameters()))
            params.extend(list(self.reward_variable.parameters()))
        if self.done_likelihood_model is not None:
            params.extend(list(self.done_likelihood_model.parameters()))
            params.extend(list(self.done_variable.parameters()))
        return params

    def inference_mode(self):
        self.action_variable.inference_mode()
        if self.state_variable is not None:
            self.state_variable.inference_mode()
        if self.state_prior_model is not None:
            self.state_prior_model.detach_hidden_state()
        if self.action_prior_model is not None:
            self.action_prior_model.detach_hidden_state()
        if self.obs_likelihood_model is not None:
            self.obs_likelihood_model.detach_hidden_state()
        if self.reward_likelihood_model is not None:
            self.reward_likelihood_model.detach_hidden_state()
        if self.done_likelihood_model is not None:
            self.done_likelihood_model.detach_hidden_state()

    def generative_mode(self):
        self.action_variable.generative_mode()
        if self.state_variable is not None:
            self.state_variable.generative_mode()
        if self.state_prior_model is not None:
            self.state_prior_model.attach_hidden_state()
        if self.action_prior_model is not None:
            self.action_prior_model.attach_hidden_state()
        if self.obs_likelihood_model is not None:
            self.obs_likelihood_model.attach_hidden_state()
        if self.reward_likelihood_model is not None:
            self.reward_likelihood_model.attach_hidden_state()
        if self.done_likelihood_model is not None:
            self.done_likelihood_model.attach_hidden_state()

    def load(self, state_dict):
        # load the state dictionary for the agent
        for k, v in state_dict.items():
            if hasattr(self, k):
                attr = getattr(self, k)
                try:
                    attr.load_state_dict(v)
                except:
                    print('WARNING: could not load ' + k + '.')
            else:
                raise ValueError
