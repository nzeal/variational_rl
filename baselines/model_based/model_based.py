# import torch
from .config import get_model_args
from .models import Model
from .gradient_buffer import GradientBuffer
from .util import Viewer
from .util.plot_util import Plotter
from .util.log_util import Logger
from .util.print_util import print_step_metrics, print_episode_metrics


def learn(env, seed, total_timesteps, log_dir, device=None, **kwargs):

    # torch.manual_seed(seed)

    logger = Logger(log_dir)
    plotter = Plotter(logger.log_str)

    model_args = get_model_args(env)
    model = Model(**model_args)
    if device is not None:
        model.to(device)
    model.reset()
    model.training = True
    lr = {'state_inference_model': 0.001,
          'action_inference_model': 0.001,
          'state_prior_model': 0.001,
          'action_prior_model': 0.,
          'obs_likelihood_model': 0.001,
          'reward_likelihood_model': 0.001,
          'done_likelihood_model': 0.001}
    grad_buffer = GradientBuffer(model, lr=lr, capacity=5, batch_size=5, clip_grad=1)

    observation = env.reset()
    reward = None
    done=False
    episode = 0

    for step in range(total_timesteps):

        action = model.act(observation, reward, done)

        if step % 50 == 0:
            plotter.plot_image(observation, 'Observation')
            plotter.plot_image(model.obs_prediction, 'Prediction')
            plotter.plot_image(model.obs_reconstruction, 'Reconstruction')

        next_observation, reward, done, _ = env.step(action)
        logger.log_step(model, observation, reward, done)
        observation = next_observation

        if done:
            model.final_reward(reward, done)
            grad_buffer.evaluate()
            grads = grad_buffer.collect()
            episode_log = logger.log_episode(model, grads)
            plotter.plot(episode_log)
            print_episode_metrics(episode, episode_log)
            grad_buffer.update()

            episode += 1
            observation = env.reset()
            reward = None
            model.reset()

    return model
