from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import random
from typing import Any

import numpy as np
import torch
from torch import nn

from backend.data_pipeline.storage import canonical_file_hash
from backend.models.env import ACTION_TOTAL_R, PortfolioManagementEnv


@dataclass(frozen=True)
class PPOConfig:
    total_steps: int = 4096
    rollout_steps: int = 256
    epochs: int = 4
    batch_size: int = 64
    learning_rate: float = 3e-4
    gamma: float = 0.99
    gae_lambda: float = 0.95
    clip_ratio: float = 0.20
    entropy_coefficient: float = 0.01
    value_coefficient: float = 0.50
    max_grad_norm: float = 0.50
    hidden_size: int = 64

    def validate(self) -> None:
        for name in (
            "total_steps",
            "rollout_steps",
            "epochs",
            "batch_size",
            "hidden_size",
        ):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.clip_ratio <= 0:
            raise ValueError("clip_ratio must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_steps": self.total_steps,
            "rollout_steps": self.rollout_steps,
            "epochs": self.epochs,
            "batch_size": self.batch_size,
            "learning_rate": self.learning_rate,
            "gamma": self.gamma,
            "gae_lambda": self.gae_lambda,
            "clip_ratio": self.clip_ratio,
            "entropy_coefficient": self.entropy_coefficient,
            "value_coefficient": self.value_coefficient,
            "max_grad_norm": self.max_grad_norm,
            "hidden_size": self.hidden_size,
        }


class DiscreteTotalRPolicy(nn.Module):
    """Actor-critic head with exactly five ``total_r`` action logits."""

    def __init__(self, observation_size: int, hidden_size: int = 64) -> None:
        super().__init__()
        self.observation_size = observation_size
        self.hidden_size = hidden_size
        self.trunk = nn.Sequential(
            nn.Linear(observation_size, hidden_size),
            nn.Tanh(),
            nn.Linear(hidden_size, hidden_size),
            nn.Tanh(),
        )
        self.actor = nn.Linear(hidden_size, len(ACTION_TOTAL_R))
        self.critic = nn.Linear(hidden_size, 1)

    def forward(self, observations: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        hidden = self.trunk(observations)
        return self.actor(hidden), self.critic(hidden).squeeze(-1)

    def probabilities(self, observations: torch.Tensor) -> torch.Tensor:
        logits, _ = self(observations)
        return torch.softmax(logits, dim=-1)


@dataclass
class Rollout:
    observations: list[np.ndarray] = field(default_factory=list)
    actions: list[int] = field(default_factory=list)
    old_log_probabilities: list[float] = field(default_factory=list)
    rewards: list[float] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    dones: list[bool] = field(default_factory=list)
    episode_boundaries: list[int] = field(default_factory=list)

    def append(
        self,
        observation: np.ndarray,
        action: int,
        old_log_probability: float,
        reward: float,
        value: float,
        done: bool,
    ) -> None:
        self.observations.append(observation.copy())
        self.actions.append(action)
        self.old_log_probabilities.append(old_log_probability)
        self.rewards.append(reward)
        self.values.append(value)
        self.dones.append(done)
        if done:
            self.episode_boundaries.append(len(self.actions))

    def __len__(self) -> int:
        return len(self.actions)


def _seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.set_num_threads(min(4, torch.get_num_threads()))
    torch.use_deterministic_algorithms(True)


def _choose_action(
    model: DiscreteTotalRPolicy,
    observation: np.ndarray,
    generator: torch.Generator,
    *,
    deterministic: bool = False,
) -> tuple[int, float, float]:
    with torch.no_grad():
        tensor = torch.as_tensor(observation, dtype=torch.float32).unsqueeze(0)
        logits, value = model(tensor)
        distribution = torch.distributions.Categorical(logits=logits)
        if deterministic:
            action_tensor = torch.argmax(logits, dim=-1)
        else:
            action_tensor = torch.multinomial(
                distribution.probs,
                num_samples=1,
                generator=generator,
            ).squeeze(-1)
        action = int(action_tensor.item())
        log_probability = float(distribution.log_prob(action_tensor).item())
        return action, log_probability, float(value.item())


def _collect_rollout(
    env: PortfolioManagementEnv,
    model: DiscreteTotalRPolicy,
    *,
    steps: int,
    seed: int,
    episode_offset: int,
    initial_observation: np.ndarray | None = None,
) -> tuple[Rollout, np.ndarray, int]:
    observation = (
        initial_observation
        if initial_observation is not None
        else env.reset(seed=seed + episode_offset)[0]
    )
    generator = torch.Generator().manual_seed(seed + episode_offset + 97)
    rollout = Rollout()
    while len(rollout) < steps:
        action, old_log_probability, value = _choose_action(
            model,
            observation,
            generator,
        )
        next_observation, reward, terminated, truncated, _ = env.step(action)
        done = bool(terminated or truncated)
        rollout.append(
            observation,
            action,
            old_log_probability,
            float(reward),
            value,
            done,
        )
        if done:
            episode_offset += 1
            observation, _ = env.reset(
                seed=seed + episode_offset,
            )
        else:
            observation = next_observation
    return rollout, observation, episode_offset


def _compute_returns_and_advantages(
    rollout: Rollout,
    *,
    gamma: float,
    gae_lambda: float,
    final_value: float,
) -> tuple[torch.Tensor, torch.Tensor]:
    advantages = np.zeros(len(rollout), dtype=np.float32)
    returns = np.zeros(len(rollout), dtype=np.float32)
    start = 0
    boundaries = rollout.episode_boundaries + [len(rollout)]
    for end in boundaries:
        next_value = float(final_value) if end == len(rollout) else 0.0
        gae = 0.0
        for index in range(end - 1, start - 1, -1):
            next_nonterminal = 0.0 if rollout.dones[index] else 1.0
            delta = (
                rollout.rewards[index]
                + gamma * next_value * next_nonterminal
                - rollout.values[index]
            )
            gae = (
                delta
                + gamma
                * gae_lambda
                * next_nonterminal
                * gae
            )
            advantages[index] = gae
            returns[index] = gae + rollout.values[index]
            next_value = rollout.values[index]
        start = end
    return (
        torch.as_tensor(returns, dtype=torch.float32),
        torch.as_tensor(advantages, dtype=torch.float32),
    )


def _optimise_rollout(
    model: DiscreteTotalRPolicy,
    optimizer: torch.optim.Optimizer,
    rollout: Rollout,
    *,
    config: PPOConfig,
    final_value: float,
    generator: torch.Generator,
) -> dict[str, float]:
    observations = torch.as_tensor(
        np.asarray(rollout.observations),
        dtype=torch.float32,
    )
    actions = torch.as_tensor(rollout.actions, dtype=torch.long)
    old_log_probabilities = torch.as_tensor(
        rollout.old_log_probabilities,
        dtype=torch.float32,
    )
    returns, advantages = _compute_returns_and_advantages(
        rollout,
        gamma=config.gamma,
        gae_lambda=config.gae_lambda,
        final_value=final_value,
    )
    advantages = (advantages - advantages.mean()) / (
        advantages.std(unbiased=False) + 1e-8
    )
    losses = []
    for _ in range(config.epochs):
        order = torch.randperm(len(rollout), generator=generator)
        for start in range(0, len(rollout), config.batch_size):
            indices = order[start : start + config.batch_size]
            logits, values = model(observations[indices])
            distribution = torch.distributions.Categorical(logits=logits)
            new_log_probabilities = distribution.log_prob(actions[indices])
            ratio = torch.exp(
                new_log_probabilities - old_log_probabilities[indices]
            )
            clipped_ratio = torch.clamp(
                ratio,
                1.0 - config.clip_ratio,
                1.0 + config.clip_ratio,
            )
            policy_loss = -torch.min(
                ratio * advantages[indices],
                clipped_ratio * advantages[indices],
            ).mean()
            value_loss = torch.mean(
                (returns[indices] - values) ** 2
            )
            entropy = distribution.entropy().mean()
            loss = (
                policy_loss
                + config.value_coefficient * value_loss
                - config.entropy_coefficient * entropy
            )
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(
                model.parameters(),
                config.max_grad_norm,
            )
            optimizer.step()
            losses.append(float(loss.detach().item()))
    with torch.no_grad():
        _, final_values = model(observations)
        explained_variance = 1.0 - float(
            torch.var(returns - final_values) / (torch.var(returns) + 1e-8)
        )
    return {
        "loss": float(np.mean(losses)) if losses else 0.0,
        "explained_variance": explained_variance,
        "mean_reward": float(np.mean(rollout.rewards)) if rollout else 0.0,
    }


def train_ppo(
    env: PortfolioManagementEnv,
    *,
    seed: int,
    output_path: Path,
    config: PPOConfig | None = None,
) -> tuple[DiscreteTotalRPolicy, dict[str, Any]]:
    config = config or PPOConfig()
    config.validate()
    _seed_everything(seed)
    model = DiscreteTotalRPolicy(
        env.observation_space.shape[0],
        hidden_size=config.hidden_size,
    )
    optimizer = torch.optim.Adam(model.parameters(), lr=config.learning_rate)
    completed = 0
    episode_offset = 0
    update_history = []
    step_budget = 0
    observation, _ = env.reset(seed=seed)
    while completed < config.total_steps:
        steps = min(config.rollout_steps, config.total_steps - completed)
        rollout, observation, episode_offset = _collect_rollout(
            env,
            model,
            steps=steps,
            seed=seed,
            episode_offset=episode_offset,
            initial_observation=observation,
        )
        with torch.no_grad():
            _, final_value = model(
                torch.as_tensor(observation, dtype=torch.float32).unsqueeze(0)
            )
        history = _optimise_rollout(
            model,
            optimizer,
            rollout,
            config=config,
            final_value=float(final_value.item()),
            generator=torch.Generator().manual_seed(seed + step_budget),
        )
        completed += len(rollout)
        step_budget += 1
        update_history.append(
            {
                "update": step_budget,
                "steps_completed": completed,
                "episodes_completed": episode_offset,
                **history,
            }
        )
    model.eval()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    torch.save(
        {
            "state_dict": model.state_dict(),
            "observation_size": env.observation_space.shape[0],
            "hidden_size": config.hidden_size,
            "action_total_r": list(ACTION_TOTAL_R),
            "group": env.group,
            "reward_mode": env.reward_mode,
            "seed": seed,
        },
        temporary,
    )
    temporary.replace(output_path)
    return model, {
        "seed": seed,
        "group": env.group,
        "reward_mode": env.reward_mode,
        "total_steps": config.total_steps,
        "updates": len(update_history),
        "episodes_seen": episode_offset,
        "config": config.to_dict(),
        "history": update_history,
        "model_path": str(output_path),
        "model_sha256": canonical_file_hash(output_path),
        "action_total_r": list(ACTION_TOTAL_R),
    }


def load_policy(path: Path) -> tuple[DiscreteTotalRPolicy, dict[str, Any]]:
    payload = torch.load(path, map_location="cpu", weights_only=False)
    model = DiscreteTotalRPolicy(
        int(payload["observation_size"]),
        hidden_size=int(payload["hidden_size"]),
    )
    model.load_state_dict(payload["state_dict"])
    model.eval()
    if tuple(float(value) for value in payload["action_total_r"]) != ACTION_TOTAL_R:
        raise ValueError("Saved policy action grid does not match total_r levels")
    return model, payload


def run_deterministic_policy(
    env: PortfolioManagementEnv,
    model: DiscreteTotalRPolicy,
    *,
    start_schedule_index: int = 0,
    max_steps: int | None = None,
) -> dict[str, Any]:
    observation, _ = env.reset(
        options={"start_schedule_index": start_schedule_index}
    )
    steps = 0
    total_reward = 0.0
    action_counts = [0] * len(ACTION_TOTAL_R)
    while True:
        action, _, _ = _choose_action(
            model,
            observation,
            torch.Generator(),
            deterministic=True,
        )
        observation, reward, terminated, truncated, info = env.step(action)
        action_counts[action] += 1
        total_reward += reward
        steps += 1
        if terminated or truncated or (max_steps is not None and steps >= max_steps):
            break
    return {
        "steps": steps,
        "total_reward": total_reward,
        "action_counts": {
            str(ACTION_TOTAL_R[index]): count
            for index, count in enumerate(action_counts)
        },
        "final_info": info,
    }


def policy_probability_summary(model: DiscreteTotalRPolicy, observation: np.ndarray) -> dict[str, float]:
    with torch.no_grad():
        probabilities = model.probabilities(
            torch.as_tensor(observation, dtype=torch.float32).unsqueeze(0)
        )[0]
    if probabilities.shape != (5,):
        raise AssertionError("The policy must output exactly five total_r probabilities")
    if float(probabilities.sum()) < 1.0 - 1e-6:
        raise AssertionError("Discrete action probabilities must sum to one")
    return {
        str(total_r): float(probability)
        for total_r, probability in zip(ACTION_TOTAL_R, probabilities)
    }

