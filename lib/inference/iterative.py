import torch.nn as nn
from lib.models import get_model
from misc import clear_gradients


class IterativeInferenceModel(nn.Module):
    """
    Iterative amortized inference.

    Args:
        network_args (dict): network arguments for inference model
        n_inf_iters (int): number of inference iterations
    """
    def __init__(self, network_args, n_inf_iters):
        super(IterativeInferenceModel, self).__init__()
        self.inference_model = get_model(network_args)
        self.n_inf_iters = n_inf_iters
        # keep track of estimated objectives for reporting
        self.estimated_objectives = []
        # keep track of parameters for analysis
        self.dist_params = []

    def forward(self, agent, state, target=False, **kwargs):

        approx_post = agent.approx_post if not target else agent.target_approx_post
        self.dist_params.append({k: v.detach() for k, v in approx_post.get_dist_params().items()})

        for _ in range(self.n_inf_iters):
            # sample actions, evaluate objective, backprop to get gradients
            actions = approx_post.sample(agent.n_action_samples)
            obj = agent.estimate_objective(state, actions, target=target)
            obj = - obj.view(agent.n_action_samples, -1, 1).mean(dim=0)
            self.estimated_objectives.append(obj.detach())
            # TODO: should this be multiplied by valid and done?
            obj.sum().backward(retain_graph=True)

            # update the approximate posterior using the iterative inference model
            params, grads = approx_post.params_and_grads()
            inf_input = self.inference_model(params=params, grads=grads, state=state)
            approx_post.step(inf_input)
            approx_post.retain_grads()
            self.dist_params.append({k: v.detach() for k, v in approx_post.get_dist_params().items()})

        # clear any gradients in the generative parameters
        clear_gradients(agent.generative_parameters())

        if target:
            # clear model gradients if this is the target inference optimizer
            target_params = nn.ParameterList()
            target_params.extend(list(self.inference_model.parameters()))
            target_params.extend(list(agent.target_approx_post.parameters()))
            clear_gradients(target_params)

    def reset(self, batch_size):
        self.inference_model.reset(batch_size)
        self.estimated_objectives = []
        self.dist_params = []
