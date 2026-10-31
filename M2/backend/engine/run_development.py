from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
import argparse
import json

import yaml

from backend.data_pipeline.storage import write_json_atomic
from backend.engine.portfolio_backtest import (
    aggregate_group_results,
    metrics_from_environment,
    run_target_schedule_with_counterfactual,
)
from backend.engine.serialization import to_primitive
from backend.models.env import MarketStateNormalizer
from backend.models.portfolio_signals import (
    build_g1_targets,
    build_g2_g3_targets,
    build_g5_targets,
    monthly_execution_dates,
    weekly_execution_dates,
)
from backend.models.ppo_standard import load_policy, run_deterministic_policy
from backend.models.random_baseline import G5_SEEDS, validate_g5_seeds
from backend.models.run_phase5 import (
    _evaluate_policy,
    _make_environment,
    _prepare_development,
    _seed_training_inputs,
)


def _metrics_projection(metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: metrics.get(key)
        for key in (
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
    }


def run_development_comparison(
    root: Path,
    config: Mapping[str, Any],
    *,
    output_path: Path | None = None,
) -> dict[str, Any]:
    prepared = _prepare_development(root, config)
    dataset = prepared["dataset"]
    validation = prepared["validation"]
    validation_start = prepared["validation_start"]
    validation_end = prepared["validation_end"]
    seeds = [int(value) for value in config["models"]["paired_seeds"]]
    top_n = int(config.get("strategy", {}).get("top_n", 5))
    leverage = float(config.get("development", {}).get("leverage", 1.0))
    report_seed_results: dict[str, list[dict[str, Any]]] = {
        group: [] for group in ("G1_monthly", "G1_weekly", "G2", "G3", "G4", "G5", "G6")
    }
    adapters: dict[str, list[Any]] = {
        group: [] for group in report_seed_results
    }
    equity_curves: dict[str, list[list[dict[str, Any]]]] = {
        group: [] for group in report_seed_results
    }

    monthly_dates = monthly_execution_dates(
        dataset,
        start=validation_start,
        end=validation_end,
    )
    weekly_dates = weekly_execution_dates(
        dataset,
        start=validation_start,
        end=validation_end,
        every_sessions=int(config.get("rl", {}).get("hold_sessions", 5)),
    )
    for label, dates, frequency in (
        ("G1_monthly", monthly_dates, "monthly"),
        ("G1_weekly", weekly_dates, "weekly_sensitivity"),
    ):
        targets = build_g1_targets(
            rows=validation,
            dataset=dataset,
            execution_dates=dates,
            top_n=top_n,
            frequency=frequency,
        )
        run = run_target_schedule_with_counterfactual(
            dataset,
            targets,
            config,
            start=validation_start,
            end=validation_end,
            group="G1",
            seed=0,
            leverage=leverage,
        )
        adapters[label].append(run)
        report_seed_results[label].append(
            {
                "seed": None,
                "frequency": frequency,
                "metrics": _metrics_projection(run.metrics),
            }
        )
        equity_curves[label].append(
            [
                {
                    "date": snapshot.trading_date.isoformat(),
                    "value": snapshot.equity,
                }
                for snapshot in run.snapshots
            ]
        )

    for group in ("G2", "G3"):
        for seed in seeds:
            targets, _ = build_g2_g3_targets(
                root,
                group=group,
                seed=seed,
                rows=validation,
                feature_panel=prepared["feature_panel"],
                execution_dates=weekly_dates,
                dataset=dataset,
                top_n=top_n,
            )
            run = run_target_schedule_with_counterfactual(
                dataset,
                targets,
                config,
                start=validation_start,
                end=validation_end,
                group=group,
                seed=seed,
                leverage=leverage,
            )
            adapters[group].append(run)
            report_seed_results[group].append(
                {
                    "seed": seed,
                    "metrics": _metrics_projection(run.metrics),
                }
            )
            equity_curves[group].append(
                [
                    {
                        "date": snapshot.trading_date.isoformat(),
                        "value": snapshot.equity,
                    }
                    for snapshot in run.snapshots
                ]
            )

    phase5_report = json.loads(
        (root / "reports/g4_g6_validation.json").read_text(encoding="utf-8")
    )
    phase5_by_seed = {
        int(row["seed"]): row for row in phase5_report["seed_results"]
    }
    for group in ("G4", "G6"):
        for seed in seeds:
            model, payload = load_policy(
                root / f"data/models/phase5/{group.lower()}_seed_{seed}.pt"
            )
            inputs = _seed_training_inputs(prepared, root, seed)
            normalizer = MarketStateNormalizer.from_dict(
                phase5_by_seed[seed]["normalizer"]
            )
            env, metrics = _evaluate_policy(
                model,
                dataset=dataset,
                validation_schedule=inputs["validation_schedule"],
                validation_states=inputs["validation_states"],
                normalizer=normalizer,
                config=config,
                group=group,
                reward_mode="sharpe20" if group == "G4" else "cvar",
                seed=seed,
                leverage=leverage,
                end_date=validation_end,
                cvar_lambda=float(
                    phase5_report["cvar_lambda_selected_on_validation"]
                ),
            )
            adapters[group].append(SimpleNamespace(metrics=metrics))
            report_seed_results[group].append(
                {
                    "seed": seed,
                    "metrics": _metrics_projection(metrics),
                    "policy_actions": metrics.get("policy_run", {}).get(
                        "action_counts"
                    ),
                    "model_sha256": payload.get("model_sha256"),
                }
            )
            equity_curves[group].append(
                [
                    {
                        "date": snapshot.trading_date.isoformat(),
                        "value": snapshot.equity,
                    }
                    for snapshot in env.run.day_results
                    for snapshot in [snapshot.snapshot]
                ]
            )

    g5_seeds = validate_g5_seeds(
        config["models"].get("g5_seeds", G5_SEEDS)
    )
    for seed in g5_seeds:
        targets = build_g5_targets(
            rows=validation,
            dataset=dataset,
            execution_dates=weekly_dates,
            seed=seed,
            top_n=top_n,
        )
        run = run_target_schedule_with_counterfactual(
            dataset,
            targets,
            config,
            start=validation_start,
            end=validation_end,
            group="G5",
            seed=seed,
            leverage=leverage,
        )
        adapters["G5"].append(run)
        report_seed_results["G5"].append(
            {
                "seed": seed,
                "metrics": _metrics_projection(run.metrics),
                "action_counts": {
                    str(level): sum(
                        target.total_r == level for target in targets.values()
                    )
                    for level in (0.0, 0.25, 0.5, 0.75, 1.0)
                },
            }
        )
        equity_curves["G5"].append(
            [
                {
                    "date": snapshot.trading_date.isoformat(),
                    "value": snapshot.equity,
                }
                for snapshot in run.snapshots
            ]
        )

    aggregates = {
        group: aggregate_group_results(runs)
        for group, runs in adapters.items()
    }
    report = {
        "status": "DEVELOPMENT_COMPLETE",
        "source_kind": "REAL_PROVIDER",
        "data_version": prepared["bundle"].manifest_data.data_version,
        "holdout_evaluated": False,
        "date_from": validation_start.isoformat(),
        "date_to": validation_end.isoformat(),
        "leverage": leverage,
        "initial_cash": float(config["broker"]["initial_cash"]),
        "cost_model": str(config["cost"]["cost_model"]),
        "financing_mode": str(config["broker"]["financing_mode"]),
        "liquidation_model": str(config["broker"]["liquidation_model"]),
        "paired_seeds": seeds,
        "g5_seeds": list(g5_seeds),
        "groups": aggregates,
        "seed_results": report_seed_results,
        "equity_curves": equity_curves,
        "interpretation": (
            "Validation-only development comparison. Synthetic financing and "
            "synthetic-v1 liquidation are stress assumptions, not broker facts."
        ),
    }
    write_json_atomic(
        output_path or (root / "reports/development_backtest.json"),
        to_primitive(report),
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run G1-G6 on the validation development period"
    )
    parser.add_argument("--config", default="config.m2_real.yaml")
    parser.add_argument(
        "--output",
        default=None,
        help="Optional output path; use a new path for post-Holdout audits",
    )
    parser.add_argument(
        "--leverage",
        type=float,
        default=None,
        help="Optional leverage override for a post-Holdout audit",
    )
    args = parser.parse_args(argv)
    root = Path(".").resolve()
    config = yaml.safe_load(
        (root / args.config).read_text(encoding="utf-8")
    )
    if args.leverage is not None:
        config.setdefault("development", {})["leverage"] = args.leverage
    report = run_development_comparison(
        root,
        config,
        output_path=Path(args.output) if args.output else None,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "date_from": report["date_from"],
                "date_to": report["date_to"],
                "groups": {
                    group: {
                        "paths": value["paths"],
                        "mean_total_return": value["total_return"]["mean"],
                        "liquidation_probability": value[
                            "first_liquidation_probability"
                        ],
                    }
                    for group, value in report["groups"].items()
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

