from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pytest
from torch import nn

from backend.features.build_features import FEATURE_NAMES
from backend.models.env import (
    ACTION_TOTAL_R,
    MarketStateNormalizer,
    PortfolioManagementEnv,
    SignalWeights,
)
from backend.models.ppo_cvar import paired_budget_contract
from backend.models.ppo_standard import (
    DiscreteTotalRPolicy,
    PPOConfig,
    policy_probability_summary,
)
from backend.models.random_baseline import G5_SEEDS, validate_g5_seeds
from backend.engine.broker_ext import Broker
from backend.engine.target_portfolio import TargetPortfolio
from tests.helpers import D, make_bar, make_broker, make_cost, make_dataset


def _environment(
    *,
    policy_environment: bool = True,
) -> tuple[PortfolioManagementEnv, date]:
    execution = date(2024, 1, 2)
    bar = make_bar(execution, "A", raw_open="10", raw_close="10.2")
    dataset = make_dataset([bar])
    schedule = {
        execution: SignalWeights(
            signal_date=date(2024, 1, 1),
            execution_date=execution,
            weights={"A": 1.0},
            allowed_codes=frozenset({"A"}),
        )
    }
    broker = make_broker(initial_cash="100000", leverage="1")
    normalizer = MarketStateNormalizer.fit([[0.0] * len(FEATURE_NAMES)])
    env = PortfolioManagementEnv(
        dataset=dataset,
        schedule=schedule,
        market_state_by_cutoff={
            date(2024, 1, 1): [0.0] * len(FEATURE_NAMES)
        },
        state_normalizer=normalizer,
        broker_config=broker.config,
        cost_model=broker.cost_model,
        execution_model=broker.execution_model,
        group="G4",
        random_start=False,
        max_episode_steps=1,
        hold_sessions=1,
    )
    return env, execution


def test_env_broker_consistency():
    env, execution = _environment()
    observation, _ = env.reset(seed=17)
    assert observation.shape[0] == len(FEATURE_NAMES) + 6
    env.step(2)

    bar = make_bar(execution, "A", raw_open="10", raw_close="10.2")
    dataset = make_dataset([bar])
    broker = make_broker(initial_cash="100000", leverage="1")
    target = TargetPortfolio(
        total_r=0.5,
        weights={"A": 1.0},
        signal_date=date(2024, 1, 1),
        execution_date=execution,
        data_cutoff=date(2024, 1, 1),
        group="G4",
    )
    broker.begin_day(dataset, execution)
    broker.execute_target(dataset, target, {"A"}, execution)
    direct = broker.finish_day(dataset)
    simulated = env.run.day_results[0]
    assert simulated.snapshot.equity == direct.snapshot.equity
    assert simulated.snapshot.cash == direct.snapshot.cash
    assert simulated.snapshot.debt == direct.snapshot.debt
    assert len(simulated.trades) == len(direct.trades) == 1
    assert simulated.trades[0].shares == direct.trades[0].shares


def test_ppo_outputs_five_discrete_total_r_probabilities():
    env, _ = _environment()
    observation, _ = env.reset(seed=29)
    model = DiscreteTotalRPolicy(env.observation_space.shape[0], hidden_size=16)
    probabilities = policy_probability_summary(model, observation)
    assert set(probabilities) == {str(value) for value in ACTION_TOTAL_R}
    assert len(probabilities) == 5
    assert sum(probabilities.values()) == pytest.approx(1.0)
    assert isinstance(model.actor, nn.Linear)
    assert model.actor.out_features == 5


def test_g4_g6_paired_budget_and_seeds():
    config = PPOConfig(total_steps=128).to_dict()
    base = {
        "seed": 43,
        "total_steps": 128,
        "config": config,
        "action_total_r": list(ACTION_TOTAL_R),
    }
    assert paired_budget_contract(base, dict(base))["paired"] is True
    with pytest.raises(ValueError):
        paired_budget_contract(base, {**base, "seed": 71})
    with pytest.raises(ValueError):
        paired_budget_contract(base, {**base, "total_steps": 129})


def test_g5_has_at_least_twenty_seeds():
    seeds = validate_g5_seeds(G5_SEEDS)
    assert len(seeds) >= 20
    assert len(set(seeds)) == len(seeds)


def test_environment_hard_stops_at_declared_period_end():
    sessions = [date(2024, 1, day) for day in (2, 3, 4, 5, 8)]
    bars = [
        make_bar(session, "A", raw_open="10", raw_close="10")
        for session in sessions
    ]
    dataset = make_dataset(bars)
    schedule = {
        sessions[1]: SignalWeights(
            signal_date=sessions[0],
            execution_date=sessions[1],
            weights={"A": 1.0},
            allowed_codes=frozenset({"A"}),
        )
    }
    base = make_broker(initial_cash="100000", leverage="1")
    env = PortfolioManagementEnv(
        dataset=dataset,
        schedule=schedule,
        market_state_by_cutoff={
            sessions[0]: [0.0] * len(FEATURE_NAMES)
        },
        state_normalizer=MarketStateNormalizer.fit(
            [[0.0] * len(FEATURE_NAMES)]
        ),
        broker_config=base.config,
        cost_model=base.cost_model,
        execution_model=base.execution_model,
        group="G4",
        random_start=False,
        max_episode_steps=1,
        hold_sessions=5,
        end_date=sessions[2],
    )
    env.reset(seed=17)
    env.step(1)
    assert [day.snapshot.trading_date for day in env.run.day_results] == sessions[1:3]

