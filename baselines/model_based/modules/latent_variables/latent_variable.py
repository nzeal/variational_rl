import torch
import torch.nn as nn


class LatentVariable(nn.Module):

    def __init__(self, prior_dist, approx_post_dist, n_variables, n_input):
        super(LatentVariable, self).__init__()
        self.prior_dist_type = getattr(torch.distributions, prior_dist)
        self.approx_post_dist_type = getattr(torch.distributions, approx_post_dist)

        prior_param_names = self.prior_dist_type.arg_constraints.keys()
        self.prior_models = nn.ModuleDict({name: None for name in prior_param_names})

        approx_post_param_names = self.approx_post_dist_type.arg_constraints.keys()
        self.approx_post_models = nn.ModuleDict({name: None for name in approx_post_param_names})

        self.initial_prior_params = nn.ParameterDict({name: None for name in prior_param_names})
        # TODO: fill in initial prior params
        for param in self.initial_prior_params:
            self.initial_prior_params[param] = nn.Parameter(torch.zeros(n_variables))

        # TODO: we will need to reshape the initial parameters
        self.prior_dist = None
        self.approx_post_dist = None
        self.reset()

        self._sample = None

    def infer(self, input):
        # infer the approximate posterior
        # TODO: allow this to be updated
        parameters = {}
        for parameter_name in self.approx_post_models:
            # calculate the value
            parameter_value = self.approx_post_models[parameter_name](input)
            # satisfy any constraints on the parameter value
            constraint = self.approx_post_dist.arg_constraints[parameter_name]
            if type(constraint) == constraints.greater_than:
                # positive value
                if constraint.lower_bound == 0:
                    parameter_value = torch.exp(parameter_value)
            elif constraint == constraints.simplex:
                # between 0 and 1
                parameter_value = nn.Softmax()(parameter_value)
            # set the parameter
            parameters[parameter_name] = parameter_value
        # create a new distribution with the parameters
        self.approx_post_dist = self.approx_post_dist_type(**parameters)
        self._sample = None

    def sample(self, input=None):
        # sample the latent variable
        if self._sample is None:
            if self.approx_post_dist.has_rsample:
                sample = self.approx_post_dist.rsample()
            else:
                sample = self.approx_post_dist.sample()
            self._sample = sample
        return self._sample

    def step(self, input):
        # set the prior
        parameters = {}
        for parameter_name in self.prior_models:
            # calculate the value
            parameter_value = self.prior_models[parameter_name](input)
            # satisfy any constraints on the parameter value
            constraint = self.prior_dist.arg_constraints[parameter_name]
            if type(constraint) == constraints.greater_than:
                # positive value
                if constraint.lower_bound == 0:
                    parameter_value = torch.exp(parameter_value)
            elif constraint == constraints.simplex:
                # between 0 and 1
                parameter_value = nn.Softmax()(parameter_value)
            # set the parameter
            parameters[parameter_name] = parameter_value
        # create a new distribution with the parameters
        self.prior_dist = self.prior_dist_type(**parameters)

    def init_approx_post(self):
        # initialize the approximate posterior from the prior
        # TODO: copy?
        self.approx_post_dist = self.prior_dist
        self._sample = None

    def reset(self):
        # reset the prior and approximate posterior
        self.prior_dist = self.prior_dist_type(**self.initial_prior_params)
        self.approx_post_dist = None
        self._sample = None

    def kl_divergence(self, analytical=True):
        if analytical:
            return dist.kl_divergence(self.approx_post_dist, self.prior_dist)
        else:
            # numerical approximation
            if self.approx_post_dist.has_rsample:
                sample = self.approx_post_dist.rsample()
            else:
                sample = self.approx_post_dist.sample()
            return self.approx_post_dist.log_prob(sample) - self.prior_dist.log_prob(sample)

    def grads_and_params(self):
        # get current gradients and parameters
        pass

    def inference_parameters(self):
        params = nn.ParameterList()
        for model_name in self.approx_post_models:
            params.extend(list(self.approx_post_models[model_name].parameters()))
        return params

    def generative_parameters(self):
        params = nn.ParameterList()
        for model_name in self.prior_models:
            params.extend(list(self.prior_models[model_name].parameters()))
        for param_name in self.initial_prior_params:
            params.append(self.initial_prior_params[param_name])
        return params
