from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
import math
import random
from typing import Any, Mapping, Sequence

import gymnasium as gym
from gymnasium import spaces
import numpy as np

from backend.data_pipeline.models import MarketDataset
from backend.engine.broker_ext import Broker, BrokerConfig, DayResult
from backend.engine.cost_model import CostModel
from backend.engine.execution import ExecutionModel
from backend.engine.models import decimal_value
from backend.engine.target_portfolio import TargetPortfolio
from backend.models.cvar import positive_loss_cvar, rolling_sharpe


ACTION_TOTAL_R = (0.0, 0.25, 0.5, 0.75, 1.0)
STATE_EXTRA_NAMES = (
    "r_prev",
    "historical_drawdown",
    "debt_to_equity",
    "margin_ratio",
    "normalised_leverage",
    "cvar_positive_loss",
)


@dataclass(frozen=True)
class SignalWeights:
    signal_date: date
    execution_date: date
    weights: Mapping[str, float]
    allowed_codes: frozenset[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_date": self.signal_date.isoformat(),
            "execution_date": self.execution_date.isoformat(),
            "weights": dict(self.weights),
            "allowed_codes": sorted(self.allowed_codes),
        }


@dataclass(frozen=True)
class MarketStateNormalizer:
    mean: tuple[float, ...]
    std: tuple[float, ...]

    @classmethod
    def fit(cls, values: Sequence[Sequence[float]]) -> "MarketStateNormalizer":
        if not values:
            raise ValueError("Cannot fit a state normalizer without training states")
        array = np.asarray(values, dtype=float)
        if array.ndim != 2 or not np.isfinite(array).all():
            raise ValueError("Training states must be a finite 2-D matrix")
        std = array.std(axis=0, ddof=0)
        std[std < 1e-8] = 1.0
        return cls(
            mean=tuple(float(value) for value in array.mean(axis=0)),
            std=tuple(float(value) for value in std),
        )

    def transform(self, values: Sequence[float]) -> np.ndarray:
        array = np.asarray(values, dtype=np.float32)
        mean = np.asarray(self.mean, dtype=np.float32)
        std = np.asarray(self.std, dtype=np.float32)
        if array.shape != mean.shape:
            raise ValueError(
                f"Expected {len(mean)} market-state values, received {array.size}"
            )
        return np.clip((array - mean) / std, -12.0, 12.0).astype(np.float32)

    def to_dict(self) -> dict[str, list[float] | str]:
        return {
            "mean": list(self.mean),
            "std": list(self.std),
            "fit_scope": "training_schedule_only",
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MarketStateNormalizer":
        return cls(
            mean=tuple(float(value) for value in payload["mean"]),
            std=tuple(float(value) for value in payload["std"]),
        )


@dataclass
class EnvironmentRun:
    targets: list[TargetPortfolio] = field(default_factory=list)
    day_results: list[DayResult] = field(default_factory=list)
    equity: list[Decimal] = field(default_factory=list)
    daily_returns: list[float] = field(default_factory=list)
    total_r: list[float] = field(default_factory=list)
    reward: list[float] = field(default_factory=list)


class PortfolioManagementEnv(gym.Env[np.ndarray, int]):
    """Daily ETF portfolio environment backed by the authoritative Broker.

    The A layer supplies target weights.  This environment only selects one of
    five discrete ``total_r`` levels.  Cash, debt, T+1 locks, fees, ADV,
    corporate actions, liquidation and terminal-account states are all handled
    by ``Broker``; no second accounting implementation exists here.
    """

    metadata = {"render_modes": []}

    def __init__(
        self,
        *,
        dataset: MarketDataset,
        schedule: Mapping[date, SignalWeights],
        market_state_by_cutoff: Mapping[date, Sequence[float]],
        state_normalizer: MarketStateNormalizer,
        broker_config: BrokerConfig,
        cost_model: CostModel,
        execution_model: ExecutionModel,
        group: str,
        reward_mode: str = "sharpe20",
        hold_sessions: int = 5,
        random_start: bool = True,
        max_episode_steps: int = 24,
        cvar_window: int = 120,
        cvar_alpha: float = 0.10,
        cvar_lambda: float = 1.0,
        leverage_levels: Sequence[float] | None = None,
        end_date: date | None = None,
        seed: int | None = None,
    ) -> None:
        super().__init__()
        if group not in {"G4", "G6"}:
            raise ValueError("The management environment supports G4 or G6 only")
        if reward_mode not in {"sharpe20", "cvar"}:
            raise ValueError("reward_mode must be sharpe20 or cvar")
        if hold_sessions <= 0 or max_episode_steps <= 0:
            raise ValueError("Episode and holding windows must be positive")
        self.dataset = dataset
        self.schedule = dict(sorted(schedule.items()))
        if not self.schedule:
            raise ValueError("Portfolio environment requires a non-empty signal schedule")
        self.decision_dates = tuple(self.schedule)
        self.market_state_by_cutoff = market_state_by_cutoff
        self.state_normalizer = state_normalizer
        self.broker_config = broker_config
        self.leverage_levels = tuple(
            decimal_value(value)
            for value in (leverage_levels or (broker_config.leverage,))
        )
        if (
            not self.leverage_levels
            or len(set(self.leverage_levels)) != len(self.leverage_levels)
            or any(value < Decimal("1") for value in self.leverage_levels)
        ):
            raise ValueError("leverage_levels must be unique and at least one")
        self.active_broker_config = broker_config
        self.cost_model = cost_model
        self.execution_model = execution_model
        self.group = group
        self.reward_mode = reward_mode
        self.hold_sessions = hold_sessions
        self.random_start = random_start
        self.max_episode_steps = max_episode_steps
        self.cvar_window = cvar_window
        self.cvar_alpha = cvar_alpha
        self.cvar_lambda = cvar_lambda
        self._rng = random.Random(seed)
        self._session_index = {
            session: index for index, session in enumerate(dataset.sessions)
        }
        if end_date is not None and end_date not in self._session_index:
            raise ValueError(f"Environment end_date {end_date} is not a session")
        self.end_date = end_date
        self._end_session_index = (
            self._session_index[end_date]
            if end_date is not None
            else len(dataset.sessions) - 1
        )
        for execution_date in self.decision_dates:
            if execution_date not in self._session_index:
                raise ValueError(f"Schedule date {execution_date} is not a session")
            entry = self.schedule[execution_date]
            if entry.signal_date >= execution_date:
                raise ValueError("Schedule signal_date must precede execution_date")

        state_size = len(next(iter(market_state_by_cutoff.values()))) + len(
            STATE_EXTRA_NAMES
        )
        self.observation_space = spaces.Box(
            low=-12.0,
            high=12.0,
            shape=(state_size,),
            dtype=np.float32,
        )
        self.action_space = spaces.Discrete(len(ACTION_TOTAL_R))
        self.broker: Broker | None = None
        self.run = EnvironmentRun()
        self._schedule_index = 0
        self._step_count = 0
        self._peak_equity = broker_config.initial_cash
        self._last_r = 0.0
        self._done = False
        self._last_observation = np.zeros(state_size, dtype=np.float32)

    @property
    def account(self):
        if self.broker is None:
            raise RuntimeError("Environment has not been reset")
        return self.broker.account

    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        super().reset(seed=seed)
        if seed is not None:
            self._rng.seed(seed)
        options = options or {}
        if "start_schedule_index" in options:
            start = int(options["start_schedule_index"])
        elif self.random_start:
            latest_start = max(
                0,
                len(self.decision_dates) - self.max_episode_steps,
            )
            start = self._rng.randint(0, latest_start) if latest_start else 0
        else:
            start = 0
        if not 0 <= start < len(self.decision_dates):
            raise ValueError("start_schedule_index is outside the schedule")
        if "leverage" in options:
            requested_leverage = decimal_value(options["leverage"])
            if requested_leverage not in self.leverage_levels:
                raise ValueError("Requested leverage is outside the training grid")
            leverage = requested_leverage
        else:
            leverage = self._rng.choice(self.leverage_levels)
        self.active_broker_config = replace(
            self.broker_config,
            leverage=leverage,
        )
        self.broker = Broker(
            self.active_broker_config,
            self.cost_model,
            self.execution_model,
        )
        self.run = EnvironmentRun()
        self._schedule_index = start
        self._step_count = 0
        self._peak_equity = self.active_broker_config.initial_cash
        self._last_r = 0.0
        self._done = False
        self._last_observation = self._observation(self.decision_dates[start])
        return self._last_observation.copy(), {
            "schedule_index": start,
            "execution_date": self.decision_dates[start].isoformat(),
            "leverage": float(self.active_broker_config.leverage),
        }

    def step(
        self, action: int
    ) -> tuple[np.ndarray, float, bool, bool, dict[str, Any]]:
        if self.broker is None:
            raise RuntimeError("reset() must be called before step()")
        if self._done:
            raise RuntimeError("step() called after episode termination")
        action_index = int(action)
        if not self.action_space.contains(action_index):
            raise ValueError(f"Invalid total_r action index: {action}")
        entry = self.schedule[self.decision_dates[self._schedule_index]]
        total_r = ACTION_TOTAL_R[action_index]
        target = TargetPortfolio(
            total_r=total_r,
            weights=dict(entry.weights) if total_r > 0 else {},
            signal_date=entry.signal_date,
            execution_date=entry.execution_date,
            data_cutoff=entry.signal_date,
            group=self.group,
            meta={
                "policy_action_index": action_index,
                "weights_source": "A_layer_G3",
                "reward_mode": self.reward_mode,
            },
        )
        self.run.targets.append(target)
        self.run.total_r.append(total_r)

        next_index = self._schedule_index + 1
        end_exclusive = (
            self._session_index[self.decision_dates[next_index]]
            if next_index < len(self.decision_dates)
            else min(
                self._end_session_index + 1,
                self._session_index[entry.execution_date] + self.hold_sessions,
            )
        )
        end_exclusive = min(end_exclusive, self._end_session_index + 1)
        start_day = self._session_index[entry.execution_date]
        block_returns: list[float] = []
        previous_equity = self.account.equity()
        for day_index in range(start_day, end_exclusive):
            session = self.dataset.sessions[day_index]
            self.broker.begin_day(self.dataset, session)
            if day_index == start_day:
                self.broker.execute_target(
                    self.dataset,
                    target,
                    set(entry.allowed_codes) | set(self.account.positions),
                    session,
                )
            result = self.broker.finish_day(self.dataset)
            self.run.day_results.append(result)
            equity = result.snapshot.equity
            self.run.equity.append(equity)
            self._peak_equity = max(self._peak_equity, equity)
            if previous_equity > 0:
                daily_return = float(equity / previous_equity - Decimal("1"))
                self.run.daily_returns.append(daily_return)
                block_returns.append(daily_return)
            previous_equity = equity
            if self.account.status.is_terminal:
                break

        reward = rolling_sharpe(self.run.daily_returns, window=20)
        if self.reward_mode == "cvar":
            risk = positive_loss_cvar(
                self.run.daily_returns,
                alpha=self.cvar_alpha,
                window=self.cvar_window,
            )
            reward -= self.cvar_lambda * risk.cvar
        self.run.reward.append(float(reward))
        self._last_r = total_r
        self._step_count += 1
        self._schedule_index = next_index
        terminated = bool(self.account.status.is_terminal)
        truncated = bool(
            self._step_count >= self.max_episode_steps
            or self._schedule_index >= len(self.decision_dates)
        )
        self._done = terminated or truncated
        if self._done:
            observation = np.zeros(
                self.observation_space.shape,
                dtype=np.float32,
            )
        else:
            observation = self._observation(
                self.decision_dates[self._schedule_index]
            )
        self._last_observation = observation
        info = {
            "total_r": total_r,
            "block_return": (
                float(np.prod([1.0 + value for value in block_returns]) - 1.0)
                if block_returns
                else 0.0
            ),
            "equity": str(self.account.equity()),
            "debt": str(self.account.debt),
            "cash": str(self.account.cash),
            "account_status": self.account.status.value,
            "first_liquidation_date": (
                self.account.first_liquidation_date.isoformat()
                if self.account.first_liquidation_date
                else None
            ),
        }
        return observation, float(reward), terminated, truncated, info

    def _observation(self, execution_date: date) -> np.ndarray:
        if self.broker is None:
            raise RuntimeError("Broker is unavailable")
        entry = self.schedule[execution_date]
        market_values = self.market_state_by_cutoff.get(entry.signal_date)
        if market_values is None:
            raise ValueError(
                f"Missing point-in-time market state for {entry.signal_date}"
            )
        market = self.state_normalizer.transform(market_values)
        equity = self.broker.account.equity()
        debt_ratio = (
            float(self.broker.account.debt / equity)
            if equity > 0
            else 1.0
        )
        margin = self.broker.account.margin_ratio()
        margin_value = 1.0 if margin is None else float(margin)
        drawdown = (
            float(equity / self._peak_equity - Decimal("1"))
            if self._peak_equity > 0
            else -1.0
        )
        leverage_normalised = math.log1p(float(self.active_broker_config.leverage)) / math.log(
            101.0
        )
        cvar = positive_loss_cvar(
            self.run.daily_returns,
            alpha=self.cvar_alpha,
            window=self.cvar_window,
        ).cvar
        extras = np.asarray(
            [
                self._last_r,
                max(-1.0, min(0.0, drawdown)),
                max(0.0, min(3.0, debt_ratio)),
                max(-3.0, min(3.0, margin_value)),
                leverage_normalised,
                max(0.0, min(3.0, cvar)),
            ],
            dtype=np.float32,
        )
        observation = np.concatenate([market, extras]).astype(np.float32)
        return np.clip(observation, -12.0, 12.0)

