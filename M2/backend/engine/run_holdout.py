from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Mapping
import argparse
import json
import math

import numpy as np
import pandas as pd
from scipy import stats
import yaml

from backend.data_pipeline.bundle_store import load_bundle
from backend.data_pipeline.models import MarketDataset
from backend.data_pipeline.quality import require_quality, validate_bundle
from backend.data_pipeline.storage import (
    canonical_file_hash,
    write_json_atomic,
)
from backend.engine.portfolio_backtest import (
    aggregate_group_results,
    metrics_from_environment,
    numeric_summary,
    run_target_schedule,
    run_target_schedule_with_counterfactual,
)
from backend.engine.preregister import (
    split_plan_from_preregistration,
    verify_preregistration,
)
from backend.engine.serialization import to_primitive
from backend.engine.survival import (
    SurvivalObservation,
    holm_adjust,
    kaplan_meier,
)
from backend.models.env import MarketStateNormalizer
from backend.models.portfolio_signals import (
    build_g1_targets,
    build_g2_g3_targets,
    build_g5_targets,
    g3_weight_schedule,
    market_states_by_cutoff,
    monthly_execution_dates,
    read_development_panel,
    read_feature_prefix,
    weekly_execution_dates,
)
from backend.models.ppo_standard import (
    load_policy,
    run_deterministic_policy,
)
from backend.models.random_baseline import G5_SEEDS, validate_g5_seeds
from backend.models.run_phase5 import _make_environment
from backend.split.splitter import (
    HoldoutAccessError,
    claim_final_holdout,
)


PRIMARY_GROUPS = ("G4", "G6")
MATRIX_GROUPS = ("G1", "G2", "G3", "G4", "G5", "G6")
METRIC_NAMES = (
    "total_return",
    "annual_return",
    "max_drawdown",
    "sharpe",
    "expected_shortfall_95",
    "fee_drag",
    "cost_ratio",
    "first_liquidation_probability",
    "time_to_first_liquidation_trading_days",
    "censored_paths",
    "total_commission",
    "total_other_fees",
    "slippage_cost",
    "total_financing_interest",
    "capitalized_interest",
    "minimum_fee_order_count",
    "unfilled_order_count",
    "bad_debt",
    "avg_pv_target_deviation",
    "terminal_wealth",
)


def _read_ndjson_records(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError(f"Non-object NDJSON row at {path}:{line_number}")
            rows.append(payload)
    return rows


def _metrics_projection(metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {name: metrics.get(name) for name in METRIC_NAMES}


def _scenario_config(
    config: Mapping[str, Any],
    *,
    cost_scenario: str,
    preregistration: Mapping[str, Any],
) -> dict[str, Any]:
    scenario = preregistration["scenario_grid"]["cost_scenarios"][cost_scenario]
    effective = deepcopy(config)
    cost = dict(effective["cost"])
    cost.update(
        {
            "cost_model": "parameterized_etf_v1",
            "commission_rate": scenario["commission_rate"],
            "minimum_commission_per_order": scenario[
                "minimum_commission_per_order"
            ],
            "exchange_fee_rate": scenario["exchange_fee_rate"],
            "stamp_duty_rate": scenario["stamp_duty_rate"],
            "fixed_other_fee": scenario["fixed_other_fee"],
            "buy_slippage_bps": scenario["buy_slippage_bps"],
            "sell_slippage_bps": scenario["sell_slippage_bps"],
        }
    )
    effective["cost"] = cost
    execution = dict(effective["execution"])
    execution["max_participation_rate"] = scenario["max_participation_rate"]
    effective["execution"] = execution
    return effective


def _survival_record(
    *,
    path_id: str,
    metrics: Mapping[str, Any],
    path_sessions: int,
) -> dict[str, Any]:
    event = metrics.get("first_liquidation_date") is not None
    time_sessions = metrics.get("time_to_first_liquidation_trading_days")
    if time_sessions is None:
        time_sessions = path_sessions
    return {
        "path_id": path_id,
        "event_observed": bool(event),
        "time_sessions": int(time_sessions),
    }


def _policy_scenario(
    *,
    group: str,
    seed: int,
    model,
    dataset: MarketDataset,
    weight_schedule,
    market_states,
    normalizer: MarketStateNormalizer,
    config: Mapping[str, Any],
    leverage: float,
    cvar_lambda: float,
    end_date: date,
    path_id: str,
    path_sessions: int,
    keep_curve: bool,
) -> dict[str, Any]:
    env = _make_environment(
        dataset=dataset,
        schedule=weight_schedule,
        market_states=market_states,
        normalizer=normalizer,
        config=config,
        group=group,
        reward_mode="sharpe20" if group == "G4" else "cvar",
        leverages=(leverage,),
        random_start=False,
        max_episode_steps=len(weight_schedule),
        cvar_lambda=cvar_lambda,
        end_date=end_date,
        seed=seed,
    )
    run_deterministic_policy(
        env,
        model,
        start_schedule_index=0,
        max_steps=len(weight_schedule),
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
        "same_policy_action_path_zero_fee_zero_slippage_counterfactual"
    )
    return {
        "seed": seed,
        "metrics": _metrics_projection(metrics),
        "survival": _survival_record(
            path_id=path_id,
            metrics=metrics,
            path_sessions=path_sessions,
        ),
        "policy_actions": metrics.get("policy_run", {}).get("action_counts"),
        "risk_sensitivity": metrics.get("risk_sensitivity"),
        "equity_curve": (
            [
                {
                    "date": day.snapshot.trading_date.isoformat(),
                    "value": day.snapshot.equity,
                }
                for day in env.run.day_results
            ]
            if keep_curve
            else None
        ),
    }


def _prepare_signal_bundle(
    *,
    root: Path,
    config: Mapping[str, Any],
    panel: pd.DataFrame,
    feature_panel: pd.DataFrame,
    dataset: MarketDataset,
    period_start: date,
    period_end: date,
    phase5: Mapping[str, Any],
) -> dict[str, Any]:
    seeds = [int(value) for value in config["models"]["paired_seeds"]]
    g5_seeds = validate_g5_seeds(
        config["models"].get("g5_seeds", G5_SEEDS)
    )
    weekly = weekly_execution_dates(
        dataset,
        start=period_start,
        end=period_end,
        every_sessions=int(config["rl"].get("hold_sessions", 5)),
    )
    monthly = monthly_execution_dates(
        dataset,
        start=period_start,
        end=period_end,
    )
    top_n = int(config.get("strategy", {}).get("top_n", 5))
    targets: dict[str, Any] = {
        "G1": build_g1_targets(
            rows=panel,
            dataset=dataset,
            execution_dates=monthly,
            top_n=top_n,
            frequency="monthly",
        ),
        "G1_weekly": build_g1_targets(
            rows=panel,
            dataset=dataset,
            execution_dates=weekly,
            top_n=top_n,
            frequency="weekly_sensitivity",
        ),
        "G2": {},
        "G3": {},
        "G5": {},
        "G4_weights": {},
        "G6_weights": {},
    }
    states: dict[str, dict[date, tuple[float, ...]]] = {}
    for seed in seeds:
        for group in ("G2", "G3"):
            target_map, _ = build_g2_g3_targets(
                root,
                group=group,
                seed=seed,
                rows=panel,
                feature_panel=feature_panel,
                execution_dates=weekly,
                dataset=dataset,
                top_n=top_n,
            )
            targets[group][seed] = target_map
        weight_schedule = g3_weight_schedule(
            root,
            seed=seed,
            rows=panel,
            feature_panel=feature_panel,
            execution_dates=weekly,
            dataset=dataset,
            top_n=top_n,
        )
        targets["G4_weights"][seed] = weight_schedule
        targets["G6_weights"][seed] = weight_schedule
        states[seed] = market_states_by_cutoff(
            panel,
            [value.signal_date for value in weight_schedule.values()],
        )
    for seed in g5_seeds:
        targets["G5"][seed] = build_g5_targets(
            rows=panel,
            dataset=dataset,
            execution_dates=weekly,
            seed=seed,
            top_n=top_n,
        )
    phase5_by_seed = {
        int(row["seed"]): row for row in phase5["seed_results"]
    }
    policies = {}
    normalizers = {}
    for seed in seeds:
        normalizers[seed] = MarketStateNormalizer.from_dict(
            phase5_by_seed[seed]["normalizer"]
        )
        policies[seed] = {
            group: load_policy(
                root / f"data/models/phase5/{group.lower()}_seed_{seed}.pt"
            )[0]
            for group in PRIMARY_GROUPS
        }
    return {
        "seeds": seeds,
        "g5_seeds": list(g5_seeds),
        "weekly_dates": weekly,
        "monthly_dates": monthly,
        "targets": targets,
        "states": states,
        "policies": policies,
        "normalizers": normalizers,
        "cvar_lambda": float(
            phase5["cvar_lambda_selected_on_validation"]
        ),
    }


def _run_matrix(
    *,
    root: Path,
    config: Mapping[str, Any],
    preregistration: Mapping[str, Any],
    dataset: MarketDataset,
    panel: pd.DataFrame,
    feature_panel: pd.DataFrame,
    period_start: date,
    period_end: date,
    phase5: Mapping[str, Any],
    data_role: str,
    checkpoint_root: Path | None = None,
) -> dict[str, Any]:
    grid = preregistration["scenario_grid"]
    expected_cells = (
        len(grid["leverage"]) * len(grid["initial_cash"]) * 3
    )
    all_checkpoints_present = bool(
        checkpoint_root is not None
        and all(
            (checkpoint_root / f"cell_{index:03d}.json").exists()
            for index in range(1, expected_cells + 1)
        )
    )
    if all_checkpoints_present:
        weekly = weekly_execution_dates(
            dataset,
            start=period_start,
            end=period_end,
            every_sessions=int(config["rl"].get("hold_sessions", 5)),
        )
        signals = {
            "seeds": [int(value) for value in config["models"]["paired_seeds"]],
            "targets": {
                "G1_weekly": build_g1_targets(
                    rows=panel,
                    dataset=dataset,
                    execution_dates=weekly,
                    top_n=int(config.get("strategy", {}).get("top_n", 5)),
                    frequency="weekly_sensitivity",
                )
            },
        }
    else:
        signals = _prepare_signal_bundle(
            root=root,
            config=config,
            panel=panel,
            feature_panel=feature_panel,
            dataset=dataset,
            period_start=period_start,
            period_end=period_end,
            phase5=phase5,
        )
    path_sessions = len(
        [session for session in dataset.sessions if period_start <= session <= period_end]
    )
    cells = []
    primary_cell = None
    secondary_cell = None
    cell_counter = 0
    if checkpoint_root is not None:
        checkpoint_root.mkdir(parents=True, exist_ok=True)
        context_path = checkpoint_root / "context.json"
        context = {
            "preregistration_hash": verify_preregistration(root)[1],
            "data_role": data_role,
            "date_from": period_start.isoformat(),
            "date_to": period_end.isoformat(),
        }
        if context_path.exists():
            existing_context = json.loads(
                context_path.read_text(encoding="utf-8")
            )
            if existing_context != context:
                raise RuntimeError(
                    "Checkpoint context differs from frozen Holdout protocol"
                )
        else:
            write_json_atomic(context_path, context)
    for leverage in grid["leverage"]:
        for initial_cash in grid["initial_cash"]:
            for cost_scenario in ("low", "base", "high"):
                cell_counter += 1
                cell_checkpoint = (
                    checkpoint_root / f"cell_{cell_counter:03d}.json"
                    if checkpoint_root is not None
                    else None
                )
                if cell_checkpoint is not None and cell_checkpoint.exists():
                    cell = json.loads(
                        cell_checkpoint.read_text(encoding="utf-8")
                    )
                    if (
                        cell["leverage"] != leverage
                        or cell["initial_cash"] != initial_cash
                        or cell["cost_scenario"] != cost_scenario
                    ):
                        raise RuntimeError(
                            f"Checkpoint cell {cell_counter} does not match the frozen grid"
                        )
                    primary = preregistration["primary_comparison"]["cell"]
                    if (
                        leverage == primary["leverage"]
                        and initial_cash == primary["initial_cash"]
                        and cost_scenario == primary["cost_scenario"]
                    ):
                        primary_cell = cell
                    secondary = preregistration["secondary_comparison"]["cell"]
                    if (
                        leverage == secondary["leverage"]
                        and initial_cash == secondary["initial_cash"]
                        and cost_scenario == secondary["cost_scenario"]
                    ):
                        secondary_cell = cell
                    cells.append(cell)
                    print(
                        f"checkpoint cell L={leverage} cash={initial_cash} "
                        f"cost={cost_scenario} ({cell_counter}/72)",
                        flush=True,
                    )
                    continue
                scenario_config = _scenario_config(
                    config,
                    cost_scenario=cost_scenario,
                    preregistration=preregistration,
                )
                cell_seed_results: dict[str, list[dict[str, Any]]] = {
                    group: [] for group in MATRIX_GROUPS
                }
                cell_adapters: dict[str, list[Any]] = {
                    group: [] for group in MATRIX_GROUPS
                }
                keep_curve = (
                    cost_scenario == "base"
                    and initial_cash == 1000000
                    and leverage in {1, 50, 100}
                )
                for group in MATRIX_GROUPS:
                    if group in {"G4", "G6"}:
                        for seed in signals["seeds"]:
                            path_id = (
                                f"{data_role}-{group}-{seed}-L{leverage}-"
                                f"C{initial_cash}-{cost_scenario}"
                            )
                            record = _policy_scenario(
                                group=group,
                                seed=seed,
                                model=signals["policies"][seed][group],
                                dataset=dataset,
                                weight_schedule=signals["targets"][
                                    f"{group}_weights"
                                ][seed],
                                market_states=signals["states"][seed],
                                normalizer=signals["normalizers"][seed],
                                config=scenario_config,
                                leverage=leverage,
                                cvar_lambda=signals["cvar_lambda"],
                                end_date=period_end,
                                path_id=path_id,
                                path_sessions=path_sessions,
                                keep_curve=keep_curve,
                            )
                            cell_seed_results[group].append(record)
                            cell_adapters[group].append(
                                SimpleNamespace(metrics=record["metrics"])
                            )
                    else:
                        if group == "G1":
                            seeds_for_group = (0,)
                            target_map = signals["targets"]["G1"]
                        elif group == "G2":
                            seeds_for_group = tuple(signals["seeds"])
                            target_map = signals["targets"]["G2"]
                        elif group == "G3":
                            seeds_for_group = tuple(signals["seeds"])
                            target_map = signals["targets"]["G3"]
                        elif group == "G5":
                            seeds_for_group = tuple(signals["g5_seeds"])
                            target_map = signals["targets"]["G5"]
                        else:
                            raise AssertionError(group)
                        for seed in seeds_for_group:
                            path_id = (
                                f"{data_role}-{group}-{seed}-L{leverage}-"
                                f"C{initial_cash}-{cost_scenario}"
                            )
                            targets = (
                                target_map
                                if group == "G1"
                                else target_map[seed]
                            )
                            run = run_target_schedule_with_counterfactual(
                                dataset,
                                targets,
                                scenario_config,
                                start=period_start,
                                end=period_end,
                                group=group,
                                seed=seed,
                                initial_cash=initial_cash,
                                leverage=leverage,
                            )
                            cell_adapters[group].append(run)
                            cell_seed_results[group].append(
                                {
                                    "seed": seed,
                                    "metrics": _metrics_projection(run.metrics),
                                    "survival": _survival_record(
                                        path_id=path_id,
                                        metrics=run.metrics,
                                        path_sessions=path_sessions,
                                    ),
                                    "equity_curve": (
                                        [
                                            {
                                                "date": snapshot.trading_date.isoformat(),
                                                "value": snapshot.equity,
                                            }
                                            for snapshot in run.snapshots
                                        ]
                                        if keep_curve
                                        else None
                                    ),
                                }
                            )
                group_aggregates = {
                    group: aggregate_group_results(cell_adapters[group])
                    for group in MATRIX_GROUPS
                }
                observations = [
                    SurvivalObservation(
                        path_id=row["survival"]["path_id"],
                        event_observed=bool(
                            row["survival"]["event_observed"]
                        ),
                        time_sessions=int(row["survival"]["time_sessions"]),
                    )
                    for group in MATRIX_GROUPS
                    for row in cell_seed_results[group]
                ]
                cell = {
                    "leverage": leverage,
                    "initial_cash": initial_cash,
                    "cost_scenario": cost_scenario,
                    "date_from": period_start.isoformat(),
                    "date_to": period_end.isoformat(),
                    "path_sessions": path_sessions,
                    "groups": group_aggregates,
                    "seed_results": cell_seed_results,
                    "survival_curve": kaplan_meier(observations),
                }
                primary = preregistration["primary_comparison"]["cell"]
                if (
                    leverage == primary["leverage"]
                    and initial_cash == primary["initial_cash"]
                    and cost_scenario == primary["cost_scenario"]
                ):
                    primary_cell = cell
                secondary = preregistration["secondary_comparison"]["cell"]
                if (
                    leverage == secondary["leverage"]
                    and initial_cash == secondary["initial_cash"]
                    and cost_scenario == secondary["cost_scenario"]
                ):
                    secondary_cell = cell
                cells.append(cell)
                if cell_checkpoint is not None:
                    write_json_atomic(
                        cell_checkpoint,
                        to_primitive(cell),
                    )
                print(
                    "matrix cell "
                    f"L={leverage} cash={initial_cash} cost={cost_scenario} "
                    f"({len(cells)}/{len(grid['leverage']) * len(grid['initial_cash']) * 3})",
                    flush=True,
                )

    primary = _primary_paired_analysis(
        primary_cell,
        preregistration=preregistration,
    )
    g1_weekly = {}
    base_config = _scenario_config(
        config,
        cost_scenario="base",
        preregistration=preregistration,
    )
    for leverage in grid["leverage"]:
        run = run_target_schedule_with_counterfactual(
            dataset,
            signals["targets"]["G1_weekly"],
            base_config,
            start=period_start,
            end=period_end,
            group="G1",
            seed=0,
            initial_cash=1000000,
            leverage=leverage,
        )
        g1_weekly[str(leverage)] = _metrics_projection(run.metrics)
    result = {
        "status": "HOLDOUT_EVALUATED" if data_role == "holdout" else "DRY_RUN",
        "holdout_evaluated": data_role == "holdout",
        "data_role": data_role,
        "date_from": period_start.isoformat(),
        "date_to": period_end.isoformat(),
        "path_sessions": path_sessions,
        "preregistration_hash": verify_preregistration(root)[1],
        "cells": cells,
        "primary": primary,
        "secondary": secondary_cell,
        "g1_weekly_sensitivity_base": g1_weekly,
        "model_artifacts": {
            "g4": {
                str(seed): canonical_file_hash(
                    root / f"data/models/phase5/g4_seed_{seed}.pt"
                )
                for seed in signals["seeds"]
            },
            "g6": {
                str(seed): canonical_file_hash(
                    root / f"data/models/phase5/g6_seed_{seed}.pt"
                )
                for seed in signals["seeds"]
            },
        },
        "interpretation_boundary": (
            "Synthetic financing, synthetic-v1 liquidation, raw execution "
            "prices and parameterized costs. Not a real broker capability."
        ),
    }
    return result


def _primary_paired_analysis(
    cell: Mapping[str, Any] | None,
    *,
    preregistration: Mapping[str, Any],
) -> dict[str, Any]:
    if cell is None:
        raise ValueError("Primary preregistered cell was not evaluated")
    g4_rows = {
        int(row["seed"]): row["metrics"]
        for row in cell["seed_results"]["G4"]
    }
    g6_rows = {
        int(row["seed"]): row["metrics"]
        for row in cell["seed_results"]["G6"]
    }
    seeds = [int(value) for value in preregistration["models"]["paired_seeds"]]
    endpoints = preregistration["primary_comparison"]["endpoints"]
    output = {}
    raw_p_values = []
    endpoint_names = []
    for endpoint in endpoints:
        g4_values = []
        g6_values = []
        for seed in seeds:
            g4_value = g4_rows[seed].get(endpoint)
            g6_value = g6_rows[seed].get(endpoint)
            if g4_value is None and endpoint == "time_to_first_liquidation_trading_days":
                g4_value = cell["path_sessions"]
            if g6_value is None and endpoint == "time_to_first_liquidation_trading_days":
                g6_value = cell["path_sessions"]
            if g4_value is None or g6_value is None:
                raise ValueError(
                    f"Primary endpoint {endpoint} is missing for seed {seed}"
                )
            g4_values.append(float(g4_value))
            g6_values.append(float(g6_value))
        differences = [
            g6 - g4 for g6, g4 in zip(g6_values, g4_values)
        ]
        if np.allclose(differences, 0.0):
            p_value = 1.0
        else:
            p_value = float(
                stats.ttest_rel(g6_values, g4_values, nan_policy="omit").pvalue
            )
        output[endpoint] = {
            "G4_seed_values": g4_values,
            "G6_seed_values": g6_values,
            "G6_minus_G4": differences,
            "mean_difference": float(np.mean(differences)),
            "paired_p_value": p_value,
        }
        raw_p_values.append(p_value)
        endpoint_names.append(endpoint)
    adjusted = holm_adjust(raw_p_values)
    for endpoint, adjusted_value in zip(endpoint_names, adjusted):
        output[endpoint]["holm_adjusted_p_value"] = adjusted_value
    output["cell"] = {
        "leverage": cell["leverage"],
        "initial_cash": cell["initial_cash"],
        "cost_scenario": cell["cost_scenario"],
    }
    output["decision_rule"] = (
        "Report paired intervals and Holm-adjusted p-values; no superiority "
        "claim is forced and G6 is allowed not to improve."
    )
    return output


def _load_period_data(
    root: Path,
    config: Mapping[str, Any],
    preregistration: Mapping[str, Any],
    *,
    data_role: str,
) -> tuple[MarketDataset, pd.DataFrame, pd.DataFrame, date, date]:
    bundle = load_bundle(root / str(config["data_pipeline"]["bundle_root"]))
    quality = validate_bundle(bundle, formal_research=True)
    require_quality(quality)
    split = preregistration["data"]
    if data_role == "dry_run_validation":
        panel_train, panel = read_development_panel(root)
        period_start = date.fromisoformat(split["validation"]["start"])
        period_end = date.fromisoformat(split["validation"]["end"])
    elif data_role == "holdout":
        claim_path = root / "data/splits/final_holdout_v1.claim.json"
        if not claim_path.exists():
            raise HoldoutAccessError("Holdout claim file is missing")
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        prereg_hash = verify_preregistration(root)[1]
        if claim.get("preregistration_hash") != prereg_hash:
            raise HoldoutAccessError("Claim/preregistration hash mismatch")
        modeling_manifest = json.loads(
            (root / "reports/modeling_dataset_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        holdout_path = Path(
            modeling_manifest["partitions"]["holdout"]["path"]
        )
        expected_hash = modeling_manifest["partitions"]["holdout"][
            "artifact_sha256"
        ]
        actual_hash = canonical_file_hash(holdout_path)
        if actual_hash != expected_hash:
            raise HoldoutAccessError(
                "Holdout content hash differs from the frozen partition"
            )
        records = _read_ndjson_records(holdout_path)
        panel = pd.DataFrame.from_records(records)
        if set(panel["split"].astype(str)) != {"holdout"}:
            raise HoldoutAccessError(
                "Modeling holdout panel contains non-holdout rows"
            )
        period_start = date.fromisoformat(split["holdout"]["start"])
        period_end = date.fromisoformat(split["holdout"]["end"])
    else:
        raise ValueError("Unknown data role")
    max_cutoff = date.fromisoformat(
        max(panel["data_cutoff"].astype(str))
    )
    feature_panel = read_feature_prefix(
        root / str(config["features"]["panel_path"]),
        end_cutoff=max_cutoff,
    )
    return bundle.dataset, panel, feature_panel, period_start, period_end


def evaluate(
    root: Path,
    config: Mapping[str, Any],
    *,
    claim: bool,
    resume_claimed: bool = False,
) -> dict[str, Any]:
    preregistration, prereg_hash = verify_preregistration(root)
    plan = split_plan_from_preregistration(preregistration)
    claim_path = root / "data/splits/final_holdout_v1.claim.json"
    data_role = "dry_run_validation"
    if claim:
        if claim_path.exists():
            raise HoldoutAccessError(
                "Final holdout has already been claimed; no retry is permitted"
            )
        claim_final_holdout(
            plan,
            frozen_plan_path=root / str(config["split"]["plan_path"]),
            claim_path=claim_path,
            evaluator_id="codex-primary-2026-10-31",
            preregistration_path=(
                root
                / "reports/preregistration/master_comparison_v1.json"
            ),
            preregistration_hash=prereg_hash,
        )
        data_role = "holdout"
    elif resume_claimed:
        if not claim_path.exists():
            raise HoldoutAccessError(
                "Resume requested but no existing Holdout claim was found"
            )
        claim_record = json.loads(claim_path.read_text(encoding="utf-8"))
        if claim_record.get("preregistration_hash") != prereg_hash:
            raise HoldoutAccessError(
                "Existing claim does not match the frozen preregistration"
            )
        if claim_record.get("status") != "CLAIMED_ONCE":
            raise HoldoutAccessError("Existing claim has an invalid state")
        data_role = "holdout"
    dataset, panel, feature_panel, start, end = _load_period_data(
        root,
        config,
        preregistration,
        data_role=data_role,
    )
    phase5 = json.loads(
        (root / "reports/g4_g6_validation.json").read_text(encoding="utf-8")
    )
    result = _run_matrix(
        root=root,
        config=config,
        preregistration=preregistration,
        dataset=dataset,
        panel=panel,
        feature_panel=feature_panel,
        period_start=start,
        period_end=end,
        phase5=phase5,
        data_role=data_role,
        checkpoint_root=(
            root
            / "reports/runs/holdout_checkpoints"
            / ("holdout" if data_role == "holdout" else "dry_run")
        ),
    )
    output_path = (
        root / "reports/holdout_results.json"
        if data_role == "holdout"
        else root / "reports/runs/holdout_dry_run.json"
    )
    write_json_atomic(output_path, to_primitive(result))
    if data_role == "holdout":
        manifest = {
            "status": "COMPLETE",
            "holdout_evaluated": True,
            "claim_path": str(claim_path),
            "claim": json.loads(claim_path.read_text(encoding="utf-8")),
            "preregistration_hash": prereg_hash,
            "result_path": str(output_path),
            "result_sha256": canonical_file_hash(output_path),
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "retry_allowed": False,
        }
        write_json_atomic(
            root / "reports/holdout_evaluation_manifest.json",
            manifest,
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run or perform the one-time final Holdout evaluation"
    )
    parser.add_argument("--config", default="config.m2_real.yaml")
    parser.add_argument(
        "--evaluate-holdout",
        action="store_true",
        help="Consume the one-time Holdout claim after all dry runs pass",
    )
    parser.add_argument(
        "--resume-claimed",
        action="store_true",
        help="Continue an existing claim after an implementation fault",
    )
    args = parser.parse_args(argv)
    root = Path(".").resolve()
    config = yaml.safe_load(
        (root / args.config).read_text(encoding="utf-8")
    )
    if args.evaluate_holdout and args.resume_claimed:
        parser.error("choose either a new claim or resume of the existing claim")
    result = evaluate(
        root,
        config,
        claim=args.evaluate_holdout,
        resume_claimed=args.resume_claimed,
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "data_role": result["data_role"],
                "cells": len(result["cells"]),
                "primary": result["primary"]["cell"],
                "preregistration_hash": result["preregistration_hash"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

