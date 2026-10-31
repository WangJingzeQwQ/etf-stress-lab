from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import argparse
import json
import math
from pathlib import Path
from statistics import mean, stdev
import subprocess
from typing import Any

import yaml

from backend.data_pipeline.fixture import build_synthetic_fixture
from backend.data_pipeline.models import MarketDataset
from backend.data_pipeline.point_in_time import eligible_universe
from backend.engine.broker_ext import (
    Broker,
    BrokerConfig,
    DayResult,
    TargetExecutionSummary,
)
from backend.engine.cost_model import CostModel
from backend.engine.execution import ExecutionModel
from backend.engine.models import AuditEvent, TradeExecution, decimal_value
from backend.engine.serialization import stable_hash, to_primitive, write_json, write_ndjson
from backend.engine.snapshot import Snapshot
from backend.engine.strategy import g1_momentum_target
from backend.engine.target_portfolio import TargetPortfolio


@dataclass
class BacktestResult:
    config: dict[str, Any]
    dataset: MarketDataset
    day_results: list[DayResult]
    targets: list[TargetPortfolio]
    target_summaries: list[TargetExecutionSummary]
    trades: list[TradeExecution]
    snapshots: list[Snapshot]
    rejected_targets: list[dict[str, Any]]
    audit_events: list[AuditEvent]
    metrics: dict[str, Any]
    metadata: dict[str, Any]
    result_hash: str

    def manifest(self) -> dict[str, Any]:
        return {
            **self.metadata,
            "result_hash": self.result_hash,
            "metrics": self.metrics,
            "counts": {
                "trading_days": len(self.snapshots),
                "trades": len(self.trades),
                "target_portfolios": len(self.targets),
                "invalid_targets": len(self.rejected_targets),
            },
        }


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Configuration root must be a mapping")
    return payload


def _repository_metadata() -> tuple[str, str]:
    project_root = Path(__file__).resolve().parents[2]
    if not (project_root / ".git").exists():
        return "not_a_git_repository", "unversioned_m0"
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    commit_text = commit.stdout.strip() if commit.returncode == 0 else "unknown"
    status_text = (
        "clean"
        if status.returncode == 0 and not status.stdout.strip()
        else "dirty_or_status_unavailable"
    )
    return commit_text, status_text


def _build_dataset(config: dict[str, Any]) -> MarketDataset:
    data = config["data"]
    source = str(data["source"])
    if source != "synthetic_fixture":
        raise ValueError(
            "M0 CLI accepts only data.source=synthetic_fixture until a validated "
            "real point-in-time dataset is supplied"
        )
    return build_synthetic_fixture(
        date.fromisoformat(str(data["start_date"])),
        date.fromisoformat(str(data["end_date"])),
        seed=int(data["fixture_seed"]),
    )


def _build_cost_model(config: dict[str, Any], *, counterfactual: bool) -> CostModel:
    cost_payload = dict(config["cost"])
    if counterfactual:
        cost_payload.update(
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
    return CostModel.from_mapping(cost_payload)


def _build_execution_model(config: dict[str, Any]) -> ExecutionModel:
    payload = config["execution"]
    return ExecutionModel(
        execution_model=str(payload["execution_model"]),
        max_participation_rate=decimal_value(payload["max_participation_rate"]),
        adv_window=int(payload["adv_window"]),
        lot_size=int(payload["lot_size"]),
    )


def _monthly_schedule(
    config: dict[str, Any],
    dataset: MarketDataset,
) -> dict[date, tuple[TargetPortfolio, set[str]]]:
    strategy = config["strategy"]
    if str(strategy["group"]).upper() != "G1":
        raise ValueError("M0 supports only strategy.group=G1")
    if str(strategy["frequency"]) != "monthly":
        raise ValueError("M0 supports monthly G1; weekly sensitivity is deferred")
    universe_cfg = config["universe"]
    schedule: dict[date, tuple[TargetPortfolio, set[str]]] = {}
    previous_month: tuple[int, int] | None = None
    for index, session in enumerate(dataset.sessions):
        current_month = (session.year, session.month)
        if index > 0 and current_month != previous_month:
            signal_date = dataset.sessions[index - 1]
            view = dataset.as_of(signal_date)
            target = g1_momentum_target(
                view,
                signal_date=signal_date,
                execution_date=session,
                data_cutoff=signal_date,
                min_listed_sessions=int(universe_cfg["min_listed_sessions"]),
                adv_window=int(universe_cfg["adv_window"]),
                min_adv_amount=float(universe_cfg["min_adv_amount"]),
                momentum_lookback_sessions=int(
                    strategy["momentum_lookback_sessions"]
                ),
                top_n=int(strategy["top_n"]),
            )
            allowed = set(
                eligible_universe(
                    view,
                    signal_date,
                    min_listed_sessions=int(universe_cfg["min_listed_sessions"]),
                    adv_window=int(universe_cfg["adv_window"]),
                    min_adv_amount=decimal_value(universe_cfg["min_adv_amount"]),
                )
            )
            schedule[session] = (target, allowed)
        previous_month = current_month
    return schedule


def _run_days(
    config: dict[str, Any],
    dataset: MarketDataset,
    schedule: dict[date, tuple[TargetPortfolio, set[str]]],
    *,
    cost_model: CostModel | None = None,
    execution_model: ExecutionModel | None = None,
) -> tuple[Broker, list[DayResult]]:
    broker = Broker(
        BrokerConfig.from_mapping(config["broker"]),
        cost_model or _build_cost_model(config, counterfactual=False),
        execution_model or _build_execution_model(config),
    )
    day_results: list[DayResult] = []
    for session in dataset.sessions:
        context = broker.begin_day(dataset, session)
        item = schedule.get(session)
        if item is not None:
            target, allowed_codes = item
            broker.execute_target(dataset, target, allowed_codes, session)
        day_results.append(broker.finish_day(dataset))
    return broker, day_results


def _drawdown(values: list[Decimal]) -> float:
    peak: Decimal | None = None
    worst = 0.0
    for value in values:
        if peak is None or value > peak:
            peak = value
        if peak and peak > 0:
            drawdown = float(value / peak - Decimal("1"))
            worst = min(worst, drawdown)
    return worst


def _expected_shortfall_95(values: list[Decimal], initial: Decimal) -> float | None:
    if not values or initial <= 0:
        return None
    returns: list[float] = []
    previous = initial
    for value in values:
        if previous > 0:
            returns.append(float(value / previous - Decimal("1")))
        previous = value
    if not returns:
        return None
    losses = sorted((-item for item in returns), reverse=True)
    tail_count = max(1, math.ceil(len(losses) * 0.05))
    return mean(losses[:tail_count])


def _metrics(
    broker: Broker,
    day_results: list[DayResult],
    config: dict[str, Any],
) -> dict[str, Any]:
    initial = decimal_value(config["broker"]["initial_cash"])
    snapshots = [day.snapshot for day in day_results]
    equities = [snapshot.equity for snapshot in snapshots]
    final = equities[-1] if equities else initial
    daily_returns: list[float] = []
    previous = initial
    for snapshot in snapshots:
        if previous > 0:
            daily_returns.append(float(snapshot.equity / previous - Decimal("1")))
        previous = snapshot.equity
    total_return = float(final / initial - Decimal("1"))
    annual_return = (
        (float(final / initial) ** (252.0 / max(1, len(snapshots))) - 1.0)
        if final > 0
        else -1.0
    )
    sharpe = None
    if len(daily_returns) >= 2:
        volatility = stdev(daily_returns)
        if volatility > 0:
            sharpe = mean(daily_returns) / volatility * math.sqrt(252)
    trades = [trade for day in day_results for trade in day.trades]
    total_commission = sum((trade.commission for trade in trades), Decimal("0"))
    total_other = sum((trade.other_fees for trade in trades), Decimal("0"))
    total_slippage = sum((trade.slippage_cost for trade in trades), Decimal("0"))
    turnover_notional = sum((trade.gross_amount for trade in trades), Decimal("0"))
    average_equity = sum(equities, Decimal("0")) / Decimal(len(equities))
    explicit_costs = (
        total_commission
        + total_other
        + total_slippage
        + broker.account.total_interest_accrued
    )
    deviations = [
        summary.pv_target_total - summary.pv_actual_total
        for summary in broker.target_summaries
        if not summary.rejected
    ]
    value_scales = [
        abs(float(snapshot.equity)) for snapshot in snapshots if snapshot.equity != 0
    ]
    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "max_drawdown": _drawdown(equities),
        "sharpe": sharpe,
        "daily_return_volatility": (
            stdev(daily_returns) if len(daily_returns) >= 2 else None
        ),
        "first_liquidation_probability": (
            1.0 if broker.account.first_liquidation_date is not None else 0.0
        ),
        "first_liquidation_date": (
            broker.account.first_liquidation_date.isoformat()
            if broker.account.first_liquidation_date
            else None
        ),
        "time_to_first_liquidation_trading_days": (
            broker.account.first_liquidation_day_index
        ),
        "censored_paths": 1 if broker.account.first_liquidation_date is None else 0,
        "terminal_wealth": final,
        "terminal_wealth_median": final,
        "expected_shortfall_95": _expected_shortfall_95(equities, initial),
        "turnover": (
            float(turnover_notional / average_equity)
            if average_equity > 0
            else None
        ),
        "total_commission": total_commission,
        "total_other_fees": total_other,
        "slippage_cost": total_slippage,
        "total_financing_interest": broker.account.total_interest_accrued,
        "capitalized_interest": broker.account.total_interest_capitalized,
        "cost_ratio": (
            float(explicit_costs / average_equity)
            if average_equity > 0
            else None
        ),
        "minimum_fee_order_count": broker.account.minimum_fee_order_count,
        "unfilled_order_count": broker.account.unfilled_order_count,
        "bad_debt": broker.account.total_bad_debt,
        "avg_pv_target_deviation": (
            sum(deviations, Decimal("0")) / Decimal(len(deviations))
            if deviations
            else Decimal("0")
        ),
        "account_status": broker.account.status.value,
        "rejected_target_count": len(broker.rejected_targets),
        "liquidation_event_count": len(broker.liquidations),
        "fee_drag": None,
        "fee_drag_method": "not_computed",
        "max_abs_equity": max(value_scales) if value_scales else 0.0,
    }


def run_backtest(config: dict[str, Any]) -> BacktestResult:
    dataset = _build_dataset(config)
    schedule = _monthly_schedule(config, dataset)
    broker, day_results = _run_days(config, dataset, schedule)
    metrics = _metrics(broker, day_results, config)

    fee_drag_counterfactual: dict[str, Any] | None = None
    if bool(config.get("output", {}).get("compute_fee_drag", False)):
        counter_broker, counter_days = _run_days(
            config,
            dataset,
            schedule,
            cost_model=_build_cost_model(config, counterfactual=True),
        )
        counter_metrics = _metrics(counter_broker, counter_days, config)
        metrics["fee_drag"] = (
            counter_metrics["total_return"] - metrics["total_return"]
        )
        metrics["fee_drag_method"] = (
            "same_signals_zero_fee_zero_slippage_counterfactual;"
            "ADV_and_price_barriers_retained"
        )
        fee_drag_counterfactual = {
            "cost_model": "counterfactual_zero_fee_slippage",
            "slippage_bps": 0,
            "commission_rate": 0,
            "minimum_commission_per_order": 0,
            "ADV_constraints_retained": True,
            "total_return": counter_metrics["total_return"],
        }

    targets = [target for target, _ in schedule.values()]
    snapshots = [day.snapshot for day in day_results]
    trades = [trade for day in day_results for trade in day.trades]
    config_core = deepcopy(config)
    config_core.pop("output", None)
    code_commit, code_worktree_status = _repository_metadata()
    api_metadata = {
        "spec_version": str(config["spec_version"]),
        "group_name": str(config["strategy"]["group"]),
        "leverage": float(config["broker"]["leverage"]),
        "initial_cash": float(config["broker"]["initial_cash"]),
        "capital_base_mode": str(config["broker"]["capital_base_mode"]),
        "financing_mode": str(config["broker"]["financing_mode"]),
        "liquidation_model": str(config["broker"]["liquidation_model"]),
        "maintenance_fraction": float(config["broker"]["maintenance_fraction"]),
        "maintenance_floor": float(config["broker"]["maintenance_floor"]),
        "cost_model": str(config["cost"]["cost_model"]),
        "execution_model": str(config["execution"]["execution_model"]),
        "p0_1_frozen": bool(config["broker"]["p0_1_frozen"]),
        "data_version": dataset.data_version,
        "config_hash": stable_hash(config_core),
        "engine_version": "etf-stress-m0/0.1.0",
        "code_commit": code_commit,
        "code_worktree_status": code_worktree_status,
        "split_id": "m0_synthetic_fixture",
        "seed": int(config["data"]["fixture_seed"]),
        "data_source_kind": dataset.source_kind,
        "reentry_policy": str(config["broker"]["reentry_policy"]),
        "interest_rate": float(config["broker"]["annual_interest_rate"]),
        "cost_schedule": _build_cost_model(
            config, counterfactual=False
        ).metadata(),
        "execution_schedule": _build_execution_model(config).metadata(),
        "fee_drag_counterfactual": fee_drag_counterfactual,
        "research_warning": (
            "SYNTHETIC_FIXTURE and parameterized costs; not a real-market or "
            "broker-financing conclusion."
        ),
        "not_implemented_m0": [
            "LSTM",
            "PPO",
            "G2-G6",
            "144-cell matrix",
            "FastAPI",
            "Vue frontend",
            "supervised labels, purge/holdout split and scaler freeze",
            "real historical ETF master and delisting coverage",
        ],
    }
    core_payload = {
        "metadata": api_metadata,
        "metrics": metrics,
        "targets": [target.to_dict() for target in targets],
        "trades": [to_primitive(trade) for trade in trades],
        "snapshots": [to_primitive(snapshot) for snapshot in snapshots],
        "rejected_targets": broker.rejected_targets,
    }
    result_hash = stable_hash(core_payload)
    return BacktestResult(
        config=config,
        dataset=dataset,
        day_results=day_results,
        targets=targets,
        target_summaries=broker.target_summaries,
        trades=trades,
        snapshots=snapshots,
        rejected_targets=broker.rejected_targets,
        audit_events=broker.account.audit_events,
        metrics=metrics,
        metadata=api_metadata,
        result_hash=result_hash,
    )


def _trade_dict(trade: TradeExecution) -> dict[str, Any]:
    payload = to_primitive(trade)
    required = (
        "signal_date",
        "execution_date",
        "code",
        "name",
        "reference_price",
        "execution_price",
        "shares",
        "gross_amount",
        "commission",
        "other_fees",
        "slippage_cost",
        "net_cash_flow",
        "unfilled_shares",
        "account_status",
    )
    missing = [field for field in required if field not in payload]
    if missing:
        raise AssertionError(f"Trade output missing required fields: {missing}")
    return payload


def write_result(result: BacktestResult, output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "trades": output_dir / "trades.ndjson",
        "daily_snapshot": output_dir / "daily_snapshot.ndjson",
        "metrics": output_dir / "metrics.json",
        "target_portfolio": output_dir / "target_portfolio.ndjson",
        "target_execution": output_dir / "target_execution.ndjson",
        "run_manifest": output_dir / "run_manifest.json",
        "config_metadata": output_dir / "config_metadata.json",
        "audit_events": output_dir / "audit_events.ndjson",
        "rejected_targets": output_dir / "rejected_targets.ndjson",
    }
    write_ndjson(paths["trades"], [_trade_dict(trade) for trade in result.trades])
    write_ndjson(
        paths["daily_snapshot"],
        [to_primitive(snapshot) for snapshot in result.snapshots],
    )
    write_json(paths["metrics"], result.metrics)
    write_ndjson(
        paths["target_portfolio"],
        [target.to_dict() for target in result.targets],
    )
    write_ndjson(
        paths["target_execution"],
        [summary.to_dict() for summary in result.target_summaries],
    )
    write_json(paths["config_metadata"], result.metadata)
    write_ndjson(
        paths["audit_events"],
        result.audit_events,
    )
    write_ndjson(paths["rejected_targets"], result.rejected_targets)
    manifest = result.manifest()
    manifest["output_files"] = sorted(path.name for path in paths.values())
    write_json(paths["run_manifest"], manifest)
    return paths


def run_cli(config_path: Path, output_dir: Path) -> BacktestResult:
    config = load_config(config_path)
    result = run_backtest(config)
    write_result(result, output_dir)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the M0 G1 vertical slice")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)
    config = load_config(Path(args.config))
    result = run_backtest(config)
    output = (
        Path(args.output)
        if args.output
        else Path(str(config["output"]["root"])) / result.result_hash.split(":")[-1][:12]
    )
    write_result(result, output)
    print(
        json.dumps(
            to_primitive(
                {
                    "result_hash": result.result_hash,
                    "output": str(output.resolve()),
                    "trades": len(result.trades),
                    "snapshots": len(result.snapshots),
                    "metrics": {
                        key: result.metrics[key]
                        for key in (
                            "total_return",
                            "max_drawdown",
                            "total_commission",
                            "total_other_fees",
                            "slippage_cost",
                            "total_financing_interest",
                            "minimum_fee_order_count",
                            "unfilled_order_count",
                            "account_status",
                        )
                    },
                }
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

