from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import math
from typing import Any, Iterable, Mapping

import numpy as np

from backend.data_pipeline.models import MarketDataset
from backend.engine.broker_ext import Broker, BrokerConfig
from backend.engine.cost_model import CostModel
from backend.engine.execution import ExecutionModel
from backend.engine.models import TradeExecution
from backend.engine.run_backtest import _metrics
from backend.engine.snapshot import Snapshot
from backend.engine.target_portfolio import TargetPortfolio


@dataclass
class ScheduleBacktest:
    group: str
    seed: int
    broker: Broker
    targets: list[TargetPortfolio]
    day_results: list[Any]
    trades: list[TradeExecution]
    snapshots: list[Snapshot]
    metrics: dict[str, Any]
    fee_drag_counterfactual: dict[str, Any] | None = None


def _override_config(
    config: Mapping[str, Any],
    *,
    initial_cash: float | None = None,
    leverage: float | None = None,
) -> dict[str, Any]:
    copied = dict(config)
    broker = dict(copied["broker"])
    if initial_cash is not None:
        broker["initial_cash"] = initial_cash
    if leverage is not None:
        broker["leverage"] = leverage
    copied["broker"] = broker
    copied["data"] = dict(copied.get("data", {}))
    copied["strategy"] = dict(copied.get("strategy", {}))
    return copied


def _cost_model_from_config(
    config: Mapping[str, Any],
    *,
    zero_cost: bool,
) -> CostModel:
    payload = dict(config["cost"])
    if zero_cost:
        payload.update(
            {
                "cost_model": "counterfactual_zero_fee_slippage",
                "commission_rate": 0,
                "minimum_commission_per_order": 0,
                "exchange_fee_rate": 0,
                "stamp_duty_rate": 0,
                "fixed_other_fee": 0,
                "buy_slippage_bps": 0,
                "sell_slippage_bps": 0,
            }
        )
    return CostModel.from_mapping(payload)


def _execution_model_from_config(config: Mapping[str, Any]) -> ExecutionModel:
    payload = config["execution"]
    return ExecutionModel(
        execution_model=str(payload["execution_model"]),
        max_participation_rate=Decimal(str(payload["max_participation_rate"])),
        adv_window=int(payload["adv_window"]),
        lot_size=int(payload["lot_size"]),
    )


def run_target_schedule(
    dataset: MarketDataset,
    targets: Mapping[date, TargetPortfolio],
    config: Mapping[str, Any],
    *,
    start: date,
    end: date,
    group: str,
    seed: int,
    initial_cash: float | None = None,
    leverage: float | None = None,
    zero_cost: bool = False,
) -> ScheduleBacktest:
    effective_config = _override_config(
        config,
        initial_cash=initial_cash,
        leverage=leverage,
    )
    broker = Broker(
        BrokerConfig.from_mapping(effective_config["broker"]),
        _cost_model_from_config(effective_config, zero_cost=zero_cost),
        _execution_model_from_config(effective_config),
    )
    sessions = [
        session for session in dataset.sessions if start <= session <= end
    ]
    day_results = []
    for session in sessions:
        broker.begin_day(dataset, session)
        target = targets.get(session)
        if target is not None and broker.account.status.value == "ACTIVE":
            broker.execute_target(
                dataset,
                target,
                set(target.weights) | set(broker.account.positions),
                session,
            )
        day_results.append(broker.finish_day(dataset))
        if broker.account.status.is_terminal:
            break
    metrics = _metrics(broker, day_results, effective_config)
    trades = [trade for day in day_results for trade in day.trades]
    snapshots = [day.snapshot for day in day_results]
    return ScheduleBacktest(
        group=group,
        seed=seed,
        broker=broker,
        targets=list(targets.values()),
        day_results=day_results,
        trades=trades,
        snapshots=snapshots,
        metrics=metrics,
    )


def run_target_schedule_with_counterfactual(
    dataset: MarketDataset,
    targets: Mapping[date, TargetPortfolio],
    config: Mapping[str, Any],
    *,
    start: date,
    end: date,
    group: str,
    seed: int,
    initial_cash: float | None = None,
    leverage: float | None = None,
) -> ScheduleBacktest:
    result = run_target_schedule(
        dataset,
        targets,
        config,
        start=start,
        end=end,
        group=group,
        seed=seed,
        initial_cash=initial_cash,
        leverage=leverage,
    )
    counterfactual = run_target_schedule(
        dataset,
        targets,
        config,
        start=start,
        end=end,
        group=group,
        seed=seed,
        initial_cash=initial_cash,
        leverage=leverage,
        zero_cost=True,
    )
    result.metrics["fee_drag"] = (
        float(counterfactual.metrics["total_return"])
        - float(result.metrics["total_return"])
    )
    result.metrics["fee_drag_method"] = (
        "same_signals_zero_fee_zero_slippage_counterfactual;"
        "ADV_and_price_barriers_retained"
    )
    result.fee_drag_counterfactual = {
        "cost_model": "counterfactual_zero_fee_slippage",
        "slippage_bps": 0,
        "commission_rate": 0,
        "minimum_commission_per_order": 0,
        "ADV_constraints_retained": True,
        "total_return": counterfactual.metrics["total_return"],
        "snapshots": counterfactual.snapshots,
    }
    return result


def metrics_from_environment(
    env,
    config: Mapping[str, Any],
    *,
    group: str,
    seed: int,
    initial_cash: float | None = None,
    leverage: float | None = None,
) -> dict[str, Any]:
    effective = _override_config(
        config,
        initial_cash=initial_cash,
        leverage=leverage,
    )
    metrics = _metrics(env.broker, env.run.day_results, effective)
    metrics["policy_steps"] = len(env.run.reward)
    metrics["policy_total_r"] = list(env.run.total_r)
    metrics["policy_rewards"] = list(env.run.reward)
    metrics["group"] = group
    metrics["seed"] = seed
    return metrics


def numeric_summary(values: Iterable[float]) -> dict[str, float | int | None]:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if not finite:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "std": None,
            "p05": None,
            "p95": None,
        }
    array = np.asarray(finite, dtype=float)
    return {
        "count": len(finite),
        "mean": float(np.mean(array)),
        "median": float(np.median(array)),
        "std": float(np.std(array, ddof=1)) if len(array) > 1 else 0.0,
        "p05": float(np.quantile(array, 0.05)),
        "p95": float(np.quantile(array, 0.95)),
    }


def aggregate_group_results(
    runs: Iterable[ScheduleBacktest],
) -> dict[str, Any]:
    rows = list(runs)
    metric_names = (
        "total_return",
        "annual_return",
        "max_drawdown",
        "sharpe",
        "expected_shortfall_95",
        "fee_drag",
        "cost_ratio",
        "total_commission",
        "total_other_fees",
        "slippage_cost",
        "total_financing_interest",
        "minimum_fee_order_count",
        "unfilled_order_count",
        "bad_debt",
    )
    output = {}
    for name in metric_names:
        values = [
            row.metrics.get(name)
            for row in rows
            if row.metrics.get(name) is not None
        ]
        output[name] = numeric_summary(values)
    output["paths"] = len(rows)
    output["first_liquidation_probability"] = (
        sum(float(row.metrics["first_liquidation_probability"]) for row in rows)
        / len(rows)
        if rows
        else None
    )
    output["censored_paths"] = sum(
        int(row.metrics.get("censored_paths", 0)) for row in rows
    )
    return output

