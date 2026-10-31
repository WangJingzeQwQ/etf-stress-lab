from __future__ import annotations

from datetime import date
from decimal import Decimal
import argparse
import json
import math
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping

import numpy as np
import yaml

from backend.data_pipeline.bundle_store import load_bundle
from backend.data_pipeline.quality import require_quality, validate_bundle
from backend.data_pipeline.storage import write_json_atomic
from backend.engine.broker_ext import BrokerConfig
from backend.engine.cost_model import CostModel
from backend.engine.execution import ExecutionModel
from backend.engine.models import decimal_value
from backend.engine.serialization import to_primitive
from backend.engine.portfolio_backtest import (
    aggregate_group_results,
    metrics_from_environment,
    run_target_schedule,
    run_target_schedule_with_counterfactual,
)
from backend.models.cvar import positive_loss_cvar
from backend.models.env import (
    MarketStateNormalizer,
    PortfolioManagementEnv,
)
from backend.models.portfolio_signals import (
    build_g5_targets,
    g3_weight_schedule,
    market_states_by_cutoff,
    monthly_execution_dates,
    read_development_panel,
    read_feature_prefix,
    selected_panel_rows,
    weekly_execution_dates,
)
from backend.models.ppo_cvar import paired_budget_contract, train_cvar_ppo
from backend.models.ppo_standard import (
    DiscreteTotalRPolicy,
    PPOConfig,
    load_policy,
    run_deterministic_policy,
    train_ppo,
)
from backend.models.random_baseline import G5_SEEDS, validate_g5_seeds


def _execution_model(config: Mapping[str, Any]) -> ExecutionModel:
    payload = config["execution"]
    return ExecutionModel(
        execution_model=str(payload["execution_model"]),
        max_participation_rate=decimal_value(payload["max_participation_rate"]),
        adv_window=int(payload["adv_window"]),
        lot_size=int(payload["lot_size"]),
    )


def _cost_model(config: Mapping[str, Any]) -> CostModel:
    return CostModel.from_mapping(dict(config["cost"]))


def _broker_config(
    config: Mapping[str, Any],
    *,
    leverage: float,
) -> BrokerConfig:
    payload = dict(config["broker"])
    payload["leverage"] = leverage
    return BrokerConfig.from_mapping(payload)


def _policy_config(config: Mapping[str, Any]) -> PPOConfig:
    payload = dict(config.get("rl", {}).get("ppo", {}))
    return PPOConfig(**payload)


def _normalizer_from_train(
    train_states: Mapping[date, tuple[float, ...]],
) -> MarketStateNormalizer:
    if not train_states:
        raise ValueError("No training market states for the RL environment")
    return MarketStateNormalizer.fit(list(train_states.values()))


def _make_environment(
    *,
    dataset,
    schedule,
    market_states,
    normalizer,
    config,
    group: str,
    reward_mode: str,
    leverages: Iterable[float],
    random_start: bool,
    max_episode_steps: int,
    cvar_lambda: float = 1.0,
    end_date: date | None = None,
    seed: int = 0,
) -> PortfolioManagementEnv:
    return PortfolioManagementEnv(
        dataset=dataset,
        schedule=schedule,
        market_state_by_cutoff=market_states,
        state_normalizer=normalizer,
        broker_config=_broker_config(config, leverage=float(next(iter(leverages)))),
        cost_model=_cost_model(config),
        execution_model=_execution_model(config),
        group=group,
        reward_mode=reward_mode,
        hold_sessions=int(config.get("rl", {}).get("hold_sessions", 5)),
        random_start=random_start,
        max_episode_steps=max_episode_steps,
        cvar_window=int(config.get("rl", {}).get("cvar_window", 120)),
        cvar_alpha=float(config.get("rl", {}).get("cvar_alpha", 0.10)),
        cvar_lambda=cvar_lambda,
        leverage_levels=tuple(leverages),
        end_date=end_date,
        seed=seed,
    )


def _t_interval(values: Iterable[float]) -> dict[str, float | int | None]:
    array = np.asarray([float(value) for value in values], dtype=float)
    if len(array) == 0:
        return {
            "count": 0,
            "mean": None,
            "std": None,
            "ci95_low": None,
            "ci95_high": None,
        }
    mean = float(array.mean())
    std = float(array.std(ddof=1)) if len(array) > 1 else 0.0
    critical = 2.776 if len(array) == 5 else 1.96
    standard_error = std / math.sqrt(len(array)) if len(array) > 1 else 0.0
    return {
        "count": len(array),
        "mean": mean,
        "std": std,
        "ci95_low": mean - critical * standard_error,
        "ci95_high": mean + critical * standard_error,
    }


def _risk_sensitivity(
    returns: list[float],
    *,
    windows: Iterable[int],
    alphas: Iterable[float],
) -> dict[str, Any]:
    output = {}
    for window in windows:
        for alpha in alphas:
            estimate = positive_loss_cvar(
                returns,
                alpha=alpha,
                window=window,
            )
            output[f"w{window}_a{alpha:.2f}"] = estimate.to_dict()
    return output


def _evaluate_policy(
    model: DiscreteTotalRPolicy,
    *,
    dataset,
    validation_schedule,
    validation_states,
    normalizer,
    config,
    group: str,
    reward_mode: str,
    seed: int,
    leverage: float,
    cvar_lambda: float = 1.0,
    end_date: date | None = None,
) -> tuple[PortfolioManagementEnv, dict[str, Any]]:
    env = _make_environment(
        dataset=dataset,
        schedule=validation_schedule,
        market_states=validation_states,
        normalizer=normalizer,
        config=config,
        group=group,
        reward_mode=reward_mode,
        leverages=(leverage,),
        random_start=False,
        max_episode_steps=len(validation_schedule),
        cvar_lambda=cvar_lambda,
        end_date=end_date,
        seed=seed,
    )
    policy_run = run_deterministic_policy(
        env,
        model,
        start_schedule_index=0,
        max_steps=len(validation_schedule),
    )
    metrics = metrics_from_environment(
        env,
        config,
        group=group,
        seed=seed,
        leverage=leverage,
    )
    targets = {target.execution_date: target for target in env.run.targets}
    counterfactual = run_target_schedule(
        dataset,
        targets,
        config,
        start=min(targets),
        end=max(targets),
        group=group,
        seed=seed,
        leverage=leverage,
        zero_cost=True,
    )
    metrics["fee_drag"] = (
        float(counterfactual.metrics["total_return"])
        - float(metrics["total_return"])
    )
    metrics["fee_drag_method"] = (
        "same_policy_action_path_zero_fee_zero_slippage_counterfactual;"
        "ADV_and_price_barriers_retained"
    )
    metrics["policy_run"] = policy_run
    metrics["risk_sensitivity"] = _risk_sensitivity(
        list(env.run.daily_returns),
        windows=(60, 120, 250),
        alphas=(0.05, 0.10),
    )
    return env, metrics


def _prepare_development(
    root: Path,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    bundle = load_bundle(root / str(config["data_pipeline"]["bundle_root"]))
    quality = validate_bundle(bundle, formal_research=True)
    require_quality(quality)
    train, validation = read_development_panel(root)
    split_path = root / str(config["split"]["plan_path"])
    split_plan = json.loads(split_path.read_text(encoding="utf-8"))
    phase3_manifest = json.loads(
        (root / str(config["split"]["report_path"])).read_text(encoding="utf-8")
    )
    if (
        split_plan.get("holdout_evaluated") is True
        or phase3_manifest.get("holdout_evaluated") is not False
    ):
        raise PermissionError("Development setup requires an unevaluated holdout")
    split_start = date.fromisoformat(str(split_plan["holdout_start"]))
    development_cutoff = max(
        pd_date for pd_date in validation["data_cutoff"].astype(str)
    )
    feature_path = root / str(config["features"].get("panel_path", "data/features/real/features_panel.ndjson"))
    feature_panel = read_feature_prefix(
        feature_path,
        end_cutoff=date.fromisoformat(development_cutoff),
    )
    rl_config = config.get("rl", {})
    training_start = date.fromisoformat(
        str(rl_config.get("training_start", "2022-01-04"))
    )
    validation_start = date.fromisoformat(str(split_plan["validation_start"]))
    validation_end = date.fromisoformat(str(split_plan["validation_end"]))
    training_end = date.fromisoformat(str(split_plan["train_end"]))
    train_executions = weekly_execution_dates(
        bundle.dataset,
        start=training_start,
        end=training_end,
        every_sessions=int(rl_config.get("hold_sessions", 5)),
    )
    validation_executions = weekly_execution_dates(
        bundle.dataset,
        start=validation_start,
        end=validation_end,
        every_sessions=int(rl_config.get("hold_sessions", 5)),
    )
    return {
        "bundle": bundle,
        "dataset": bundle.dataset,
        "train": train,
        "validation": validation,
        "feature_panel": feature_panel,
        "split_plan": split_plan,
        "train_executions": train_executions,
        "validation_executions": validation_executions,
        "train_end": training_end,
        "validation_start": validation_start,
        "validation_end": validation_end,
    }


def _seed_training_inputs(
    prepared: Mapping[str, Any],
    root: Path,
    seed: int,
) -> dict[str, Any]:
    train = prepared["train"]
    validation = prepared["validation"]
    feature_panel = prepared["feature_panel"]
    dataset = prepared["dataset"]
    train_schedule = g3_weight_schedule(
        root,
        seed=seed,
        rows=train,
        feature_panel=feature_panel,
        execution_dates=prepared["train_executions"],
        dataset=dataset,
    )
    validation_schedule = g3_weight_schedule(
        root,
        seed=seed,
        rows=validation,
        feature_panel=feature_panel,
        execution_dates=prepared["validation_executions"],
        dataset=dataset,
    )
    if len(train_schedule) < 20 or len(validation_schedule) < 10:
        raise ValueError("Insufficient weekly signal dates for PPO")
    train_signals = [value.signal_date for value in train_schedule.values()]
    validation_signals = [
        value.signal_date for value in validation_schedule.values()
    ]
    train_states = market_states_by_cutoff(train, train_signals)
    validation_states = market_states_by_cutoff(validation, validation_signals)
    normalizer = _normalizer_from_train(train_states)
    return {
        "train_schedule": train_schedule,
        "validation_schedule": validation_schedule,
        "train_states": train_states,
        "validation_states": validation_states,
        "normalizer": normalizer,
    }


def _train_pair(
    prepared: Mapping[str, Any],
    root: Path,
    config: Mapping[str, Any],
    *,
    seed: int,
    inputs: Mapping[str, Any],
    total_steps: int,
    cvar_lambda: float,
    artifact_root: Path,
) -> dict[str, Any]:
    rl_config = config["rl"]
    policy_config = _policy_config(config)
    policy_config = PPOConfig(
        **{
            **policy_config.to_dict(),
            "total_steps": total_steps,
        }
    )
    leverage_values = tuple(
        float(value)
        for value in rl_config.get("training_leverages", [1, 10, 25, 50, 100])
    )
    standard_env = _make_environment(
        dataset=prepared["dataset"],
        schedule=inputs["train_schedule"],
        market_states=inputs["train_states"],
        normalizer=inputs["normalizer"],
        config=config,
        group="G4",
        reward_mode="sharpe20",
        leverages=leverage_values,
        random_start=True,
        max_episode_steps=int(rl_config.get("training_episode_steps", 24)),
        end_date=prepared["train_end"],
        seed=seed,
    )
    cvar_env = _make_environment(
        dataset=prepared["dataset"],
        schedule=inputs["train_schedule"],
        market_states=inputs["train_states"],
        normalizer=inputs["normalizer"],
        config=config,
        group="G6",
        reward_mode="cvar",
        leverages=leverage_values,
        random_start=True,
        max_episode_steps=int(rl_config.get("training_episode_steps", 24)),
        cvar_lambda=cvar_lambda,
        end_date=prepared["train_end"],
        seed=seed,
    )
    artifact_root.mkdir(parents=True, exist_ok=True)
    g4_path = artifact_root / f"g4_seed_{seed}.pt"
    g6_path = artifact_root / f"g6_seed_{seed}.pt"
    g4_model, g4_training = train_ppo(
        standard_env,
        seed=seed,
        output_path=g4_path,
        config=policy_config,
    )
    g6_model, g6_training = train_cvar_ppo(
        cvar_env,
        seed=seed,
        output_path=g6_path,
        config=policy_config,
    )
    pair_contract = paired_budget_contract(g4_training, g6_training)
    validation_leverage = float(rl_config.get("validation_leverage", 10.0))
    g4_env, g4_metrics = _evaluate_policy(
        g4_model,
        dataset=prepared["dataset"],
        validation_schedule=inputs["validation_schedule"],
        validation_states=inputs["validation_states"],
        normalizer=inputs["normalizer"],
        config=config,
        group="G4",
        reward_mode="sharpe20",
        seed=seed,
        leverage=validation_leverage,
    )
    g6_env, g6_metrics = _evaluate_policy(
        g6_model,
        dataset=prepared["dataset"],
        validation_schedule=inputs["validation_schedule"],
        validation_states=inputs["validation_states"],
        normalizer=inputs["normalizer"],
        config=config,
        group="G6",
        reward_mode="cvar",
        seed=seed,
        leverage=validation_leverage,
        cvar_lambda=cvar_lambda,
        end_date=prepared["validation_end"],
    )
    return {
        "seed": seed,
        "pair_contract": pair_contract,
        "normalizer": inputs["normalizer"].to_dict(),
        "train_schedule_dates": [
            value.isoformat() for value in inputs["train_schedule"]
        ],
        "validation_schedule_dates": [
            value.isoformat() for value in inputs["validation_schedule"]
        ],
        "G4": {
            "training": g4_training,
            "validation": g4_metrics,
        },
        "G6": {
            "training": g6_training,
            "validation": g6_metrics,
            "cvar_lambda": cvar_lambda,
        },
    }


def select_cvar_lambda(
    root: Path,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    prepared = _prepare_development(root, config)
    seeds = [int(value) for value in config["models"]["paired_seeds"]]
    if len(seeds) < 5:
        raise ValueError("Paired seed contract requires at least five seeds")
    seed = seeds[0]
    inputs = _seed_training_inputs(prepared, root, seed)
    rl_config = config["rl"]
    selection_steps = int(rl_config.get("lambda_selection_steps", 2048))
    candidates = [
        float(value)
        for value in rl_config.get("cvar_lambda_candidates", [0.5, 1.0, 2.0])
    ]
    selection_root = root / "reports/runs/phase5_lambda_selection"
    records = []
    g4_reference = None
    for lambda_value in candidates:
        pair = _train_pair(
            prepared,
            root,
            config,
            seed=seed,
            inputs=inputs,
            total_steps=selection_steps,
            cvar_lambda=lambda_value,
            artifact_root=selection_root / f"lambda_{lambda_value:.2f}",
        )
        g4_metrics_pair = pair["G4"]["validation"]
        g6_metrics = pair["G6"]["validation"]
        if g4_reference is None:
            g4_reference = {
                key: g4_metrics_pair.get(key)
                for key in (
                    "total_return",
                    "max_drawdown",
                    "sharpe",
                    "expected_shortfall_95",
                )
            }
        records.append(
            {
                "lambda": lambda_value,
                "validation": {
                    key: g6_metrics.get(key)
                    for key in (
                        "total_return",
                        "max_drawdown",
                        "sharpe",
                        "expected_shortfall_95",
                        "first_liquidation_probability",
                        "policy_run",
                        "risk_sensitivity",
                    )
                },
                "model_path": pair["G6"]["training"]["model_path"],
                "model_sha256": pair["G6"]["training"]["model_sha256"],
            }
        )
    g4_drawdown = (
        float(g4_reference["max_drawdown"])
        if g4_reference and g4_reference["max_drawdown"] is not None
        else 0.0
    )
    eligible = [
        record
        for record in records
        if float(record["validation"]["max_drawdown"]) >= g4_drawdown - 0.02
    ]
    pool = eligible or records
    selected = max(
        pool,
        key=lambda record: (
            float(record["validation"]["total_return"]),
            float(record["validation"]["sharpe"] or 0.0),
        ),
    )
    report = {
        "status": "SELECTED_VALIDATION_ONLY",
        "fit_scope": "validation_only",
        "selection_seed": seed,
        "selection_steps": selection_steps,
        "candidates": candidates,
        "selection_rule": (
            "Among candidates whose validation max drawdown is no more than "
            "2 percentage points worse than paired G4, choose the highest "
            "total return, then Sharpe."
        ),
        "g4_reference": g4_reference,
        "selected_lambda": selected["lambda"],
        "records": records,
        "holdout_evaluated": False,
    }
    write_json_atomic(
        root / "reports/g6_lambda_selection.json",
        report,
    )
    return report


def train_validation_models(
    root: Path,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    prepared = _prepare_development(root, config)
    seeds = [int(value) for value in config["models"]["paired_seeds"]]
    if len(seeds) < 5 or len(set(seeds)) != len(seeds):
        raise ValueError("G4/G6 require at least five unique paired seeds")
    selection_path = root / "reports/g6_lambda_selection.json"
    if selection_path.exists():
        selected_lambda = float(
            json.loads(selection_path.read_text(encoding="utf-8"))[
                "selected_lambda"
            ]
        )
    else:
        selected_lambda = float(config["rl"].get("cvar_lambda", 1.0))
    total_steps = int(config["rl"]["ppo"].get("total_steps", 4096))
    results = []
    for seed in seeds:
        inputs = _seed_training_inputs(prepared, root, seed)
        result = _train_pair(
            prepared,
            root,
            config,
            seed=seed,
            inputs=inputs,
            total_steps=total_steps,
            cvar_lambda=selected_lambda,
            artifact_root=root / "data/models/phase5",
        )
        results.append(result)

    metrics = (
        "total_return",
        "max_drawdown",
        "sharpe",
        "expected_shortfall_95",
        "fee_drag",
        "first_liquidation_probability",
    )
    paired = {}
    for metric in metrics:
        g4_values = [float(row["G4"]["validation"][metric]) for row in results]
        g6_values = [float(row["G6"]["validation"][metric]) for row in results]
        differences = [
            g6 - g4 for g6, g4 in zip(g6_values, g4_values)
        ]
        paired[metric] = {
            "G4_seed_values": g4_values,
            "G6_seed_values": g6_values,
            "G6_minus_G4": differences,
            "paired_interval": _t_interval(differences),
        }
    report = {
        "status": "VALIDATION_COMPLETE",
        "source_kind": "REAL_PROVIDER",
        "data_version": prepared["bundle"].manifest_data.data_version,
        "holdout_evaluated": False,
        "seeds": seeds,
        "training_budget_steps_per_policy": total_steps,
        "cvar_lambda_selected_on_validation": selected_lambda,
        "cvar_primary": {"window": 120, "alpha": 0.10},
        "validation_leverage": float(
            config["rl"].get("validation_leverage", 10.0)
        ),
        "seed_results": results,
        "paired": paired,
        "interpretation": (
            "Validation-only path results. Costs, T+1, ADV, synthetic-v1 "
            "liquidation and event debt are included; final Holdout is not."
        ),
    }
    write_json_atomic(
        root / "reports/g4_g6_validation.json",
        to_primitive(report),
    )


    # G5 is a separate random baseline and is never merged with the paired
    # G4/G6 distribution.
    g5_seeds = validate_g5_seeds(
        config["models"].get("g5_seeds", G5_SEEDS)
    )
    g5_execution_dates = prepared["validation_executions"]
    g5_runs = []
    g5_schedule_runs = []
    for seed in g5_seeds:
        targets = build_g5_targets(
            rows=prepared["validation"],
            dataset=prepared["dataset"],
            execution_dates=g5_execution_dates,
            seed=seed,
            top_n=int(config.get("strategy", {}).get("top_n", 5)),
        )
        run = run_target_schedule_with_counterfactual(
            prepared["dataset"],
            targets,
            config,
            start=prepared["validation_start"],
            end=prepared["validation_end"],
            group="G5",
            seed=seed,
            leverage=float(config["rl"].get("validation_leverage", 10.0)),
        )
        g5_schedule_runs.append(run)
        action_counts = {
            str(level): sum(
                1
                for target in targets.values()
                if target.total_r == level
            )
            for level in (0.0, 0.25, 0.5, 0.75, 1.0)
        }
        g5_runs.append(
            {
                "seed": seed,
                "metrics": {
                    key: run.metrics.get(key)
                    for key in (
                        "total_return",
                        "annual_return",
                        "max_drawdown",
                        "sharpe",
                        "expected_shortfall_95",
                        "fee_drag",
                        "first_liquidation_probability",
                        "bad_debt",
                        "minimum_fee_order_count",
                        "unfilled_order_count",
                    )
                },
                "action_counts": action_counts,
                "targets": [target.to_dict() for target in targets.values()],
            }
        )
    g5_report = {
        "status": "VALIDATION_COMPLETE",
        "group": "G5",
        "holdout_evaluated": False,
        "seeds": list(g5_seeds),
        "seed_count": len(g5_seeds),
        "leverage": float(config["rl"].get("validation_leverage", 10.0)),
        "aggregate": aggregate_group_results(g5_schedule_runs),
        "seed_results": g5_runs,
    }
    write_json_atomic(
        root / "reports/g5_seed_results.json",
        to_primitive(g5_report),
    )
    return {
        "status": report["status"],
        "cvar_lambda": selected_lambda,
        "seeds": seeds,
        "g5_seeds": list(g5_seeds),
        "holdout_evaluated": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train and validate G4/G5/G6")
    parser.add_argument(
        "--mode",
        choices=("select-lambda", "train"),
        default="train",
    )
    parser.add_argument("--config", default="config.m2_real.yaml")
    args = parser.parse_args(argv)
    root = Path(".").resolve()
    config = yaml.safe_load(
        (root / args.config).read_text(encoding="utf-8")
    )
    if args.mode == "select-lambda":
        report = select_cvar_lambda(root, config)
        summary = {
            "status": report["status"],
            "selected_lambda": report["selected_lambda"],
            "selection_seed": report["selection_seed"],
        }
    else:
        summary = train_validation_models(root, config)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

