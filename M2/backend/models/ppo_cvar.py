from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.models.env import PortfolioManagementEnv
from backend.models.ppo_standard import (
    DiscreteTotalRPolicy,
    PPOConfig,
    train_ppo,
)


def train_cvar_ppo(
    env: PortfolioManagementEnv,
    *,
    seed: int,
    output_path: Path,
    config: PPOConfig | None = None,
) -> tuple[DiscreteTotalRPolicy, dict[str, Any]]:
    """Train reward-shaping PPO; this is not a constrained CVaR-MDP."""

    if env.group != "G6" or env.reward_mode != "cvar":
        raise ValueError("G6 must use a cvar-shaped portfolio environment")
    if env.cvar_alpha != 0.10:
        raise ValueError("The primary CVaR alpha must be frozen at 0.10")
    if env.cvar_window != 120:
        raise ValueError("The primary CVaR window must be frozen at 120 sessions")
    model, metadata = train_ppo(
        env,
        seed=seed,
        output_path=output_path,
        config=config,
    )
    metadata["algorithm"] = "CVaR-reward-shaped PPO"
    metadata["constrained_cvar_mdp"] = False
    metadata["cvar_window"] = env.cvar_window
    metadata["cvar_alpha"] = env.cvar_alpha
    metadata["cvar_lambda"] = env.cvar_lambda
    return model, metadata


def paired_budget_contract(
    g4_metadata: dict[str, Any],
    g6_metadata: dict[str, Any],
) -> dict[str, Any]:
    required = (
        "seed",
        "total_steps",
        "config",
    )
    for field in required:
        if g4_metadata.get(field) != g6_metadata.get(field):
            raise ValueError(f"G4/G6 paired field differs: {field}")
    if g4_metadata.get("action_total_r") != g6_metadata.get("action_total_r"):
        raise ValueError("G4/G6 action grids differ")
    return {
        "paired": True,
        "same_seed": g4_metadata["seed"] == g6_metadata["seed"],
        "same_total_steps": (
            g4_metadata["total_steps"] == g6_metadata["total_steps"]
        ),
        "same_network_and_optimizer": (
            g4_metadata["config"] == g6_metadata["config"]
        ),
        "same_action_grid": (
            g4_metadata["action_total_r"] == g6_metadata["action_total_r"]
        ),
    }

