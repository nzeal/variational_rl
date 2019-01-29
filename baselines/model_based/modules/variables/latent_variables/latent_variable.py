import torch
import torch.nn as nn
import torch.distributions.constraints as constraints


class LatentVariable(nn.Module):

    def __init__(self, prior_dist, approx_post_dist, n_variables, n_input):
        super(LatentVariable, self).__init__()
        self.n_variables = n_variables
        self.prior_dist_type = getattr(torch.distributions, prior_dist)
        self.approx_post_dist_type = getattr(torch.distributions, approx_post_dist)

        if prior_dist == 'Categorical':
            prior_param_names = ['logits']
        else:
            prior_param_names = self.prior_dist_type.arg_constraints.keys()
        self.prior_models = nn.ModuleDict({name: None for name in prior_param_names})

        if approx_post_dist == 'Categorical':
            approx_post_param_names = ['logits']
        else:
            approx_post_param_names = self.approx_post_dist_type.arg_constraints.keys()
        self.approx_post_models = nn.ModuleDict({name: None for name in approx_post_param_names})
        self.approx_post_gates = nn.ModuleDict({name: None for name in approx_post_param_names})

        self.initial_prior_params = nn.ParameterDict({name: None for name in prior_param_names})

        for param in self.initial_prior_params:
            constraint = self.prior_dist_type.arg_constraints[param]
            if type(constraint) == constraints.greater_than and constraint.lower_bound == 0:
                self.initial_prior_params[param] = nn.Parameter(torch.ones(1, n_variables))
            else:
                self.initial_prior_params[param] = nn.Parameter(torch.zeros(1, n_variables))

        # TODO: we will need to reshape the initial parameters
        self.prior_dist = None
        self.approx_post_dist = None
        self.reinitialized = True
        self.reset()

        self._sample = None
        self._detach_latent = True
        self.layer_norm = nn.LayerNorm(self.n_variables)
        self._log_var_limits = [-5, 5]

    def infer(self, input):
        # infer the approximate posterior
        parameters = {}
        for param_name in self.approx_post_models:
            # calculate the parameter update and the gate
            param_update = self.approx_post_models[param_name](input)
            param_gate = self.approx_post_gates[param_name](input)

            # update the parameter value
            param = getattr(self.approx_post_dist, param_name).detach()
            constraint = self.approx_post_dist.arg_constraints[param_name]
            if type(constraint) == constraints.greater_than and constraint.lower_bound == 0:
                # convert to log-space (for scale parameters)
                param = torch.log(param)
            param = param_gate * param + (1. - param_gate) * param_update
            # param = param_update

            # satisfy any constraints on the parameter value
            if type(constraint) == constraints.greater_than and constraint.lower_bound == 0:
                # positive value
                param = torch.clamp(param, self._log_var_limits[0], self._log_var_limits[1])
                param = torch.exp(param)
            elif constraint == constraints.simplex:
                # between 0 and 1
                param = nn.Softmax()(param)

            # set the parameter
            parameters[param_name] = param

        # create a new distribution with the parameters
        self.approx_post_dist = self.approx_post_dist_type(**parameters)
        self._sample = None
        self.reinitialized = False

    def sample(self, input=None):
        # sample the latent variable
        if self._sample is None:
            if self.approx_post_dist.has_rsample:
                sample = self.approx_post_dist.rsample()
                sample = self.layer_norm(sample)
            else:
                sample = self.approx_post_dist.sample()
            if self.approx_post_dist_type == getattr(torch.distributions, 'Categorical'):
                # convert to one-hot encoding
                device = self.initial_prior_params['logits'].device
                one_hot_sample = torch.zeros(sample.shape[0], self.n_variables).to(device)
                one_hot_sample[:, sample] = 1.
                sample = one_hot_sample
            self._sample = sample
        sample = self._sample
        if self._detach_latent:
            sample = sample.detach()
        return sample

    def step(self, input):
        # set the prior
        parameters = {}
        for param_name in self.prior_models:
            # calculate the value
            param = self.prior_models[param_name](input)
            # satisfy any constraints on the parameter value
            constraint = self.prior_dist.arg_constraints[param_name]
            if type(constraint) == constraints.greater_than:
                # positive value
                if constraint.lower_bound == 0:
                    param = torch.clamp(param, self._log_var_limits[0], self._log_var_limits[1])
                    param = torch.exp(param)
            elif constraint == constraints.simplex:
                # between 0 and 1
                param = nn.Softmax()(param)
            # set the parameter
            parameters[param_name] = param
        # create a new distribution with the parameters
        self.prior_dist = self.prior_dist_type(**parameters)
        self.reinitialized = False

    def init_approx_post(self):
        # initialize the approximate posterior from the prior
        # copies over a detached version of each parameter
        assert self.prior_dist_type == self.approx_post_dist_type, 'Only currently support same type.'
        parameters = {}
        for parameter_name in self.initial_prior_params:
            parameters[parameter_name] = getattr(self.prior_dist, parameter_name).detach().requires_grad_()
        self.approx_post_dist = self.approx_post_dist_type(**parameters)
        self._sample = None

    def reset(self, batch_size=1):
        # reset the prior and approximate posterior
        prior_params = {k: v.repeat(batch_size, 1) for k, v in self.initial_prior_params.items()}
        self.prior_dist = self.prior_dist_type(**prior_params)
        self.init_approx_post()
        self.reinitialized = True
        # self.approx_post_dist = None
        self._sample = None

    def kl_divergence(self, analytical=True):
        if analytical:
            return torch.distributions.kl_divergence(self.approx_post_dist, self.prior_dist)
        else:
            # numerical approximation
            if self.approx_post_dist.has_rsample:
                sample = self.approx_post_dist.rsample()
            else:
                sample = self.approx_post_dist.sample()
            return self.approx_post_dist.log_prob(sample) - self.prior_dist.log_prob(sample)

    def params_and_grads(self, concat=True, normalize=True, norm_type='layer'):
        # get current gradients and parameters
        params = [getattr(self.approx_post_dist, param).detach() for param in self.approx_post_models]
        grads = [getattr(self.approx_post_dist, param).grad.detach() for param in self.approx_post_models]
        if normalize:
            norm_dim = -1
            if norm_type == 'batch':
                norm_dim = 0
            elif norm_type == 'layer':
                norm_dim = 1
            else:
                raise NotImplementedError
            for ind, grad in enumerate(grads):
                mean = grad.mean(dim=norm_dim, keepdim=True)
                std = grad.std(dim=norm_dim, keepdim=True)
                grads[ind] = (grad - mean) / (std + 1e-7)
            for ind, param in enumerate(params):
                mean = param.mean(dim=norm_dim, keepdim=True)
                std = param.std(dim=norm_dim, keepdim=True)
                params[ind] = (param - mean) / (std + 1e-7)
        if concat:
            return torch.cat(params + grads, dim=1)
        else:
            return params, grads

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

    def inference_mode(self):
        self._detach_latent = False

    def generative_mode(self):
        self._detach_latent = True
