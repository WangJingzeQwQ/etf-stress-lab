from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
from threading import Lock
from typing import Any, Mapping
import json

import yaml

from backend.data_pipeline.bundle_store import load_bundle
from backend.data_pipeline.quality import require_quality, validate_bundle
from backend.data_pipeline.storage import canonical_file_hash
from backend.engine.portfolio_backtest import (
    metrics_from_environment,
    run_target_schedule,
)
from backend.engine.serialization import to_primitive
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
from backend.models.ppo_standard import load_policy, run_deterministic_policy
from backend.models.random_baseline import G5_SEEDS
from backend.models.run_phase5 import _make_environment


class DashboardService:
    def __init__(self, root: Path, config_path: Path) -> None:
        self.root = root
        self.config = yaml.safe_load(
            config_path.read_text(encoding="utf-8")
        )
        self._lock = Lock()
        self._bundle = None
        self._panel = None
        self._feature_panel = None
        self._phase5 = None
        self._phase5_by_seed = None
        self._cache: dict[str, dict[str, Any]] = {}
        self.dashboard = json.loads(
            (root / "reports/dashboard.json").read_text(encoding="utf-8")
        )

    def _ensure_loaded(self) -> None:
        if self._bundle is not None:
            return
        with self._lock:
            if self._bundle is not None:
                return
            bundle = load_bundle(
                self.root / str(self.config["data_pipeline"]["bundle_root"])
            )
            quality = validate_bundle(bundle, formal_research=True)
            require_quality(quality)
            _, validation = read_development_panel(self.root)
            max_cutoff = date.fromisoformat(
                max(validation["data_cutoff"].astype(str))
            )
            feature_panel = read_feature_prefix(
                self.root / str(self.config["features"]["panel_path"]),
                end_cutoff=max_cutoff,
            )
            phase5 = json.loads(
                (self.root / "reports/g4_g6_validation.json").read_text(
                    encoding="utf-8"
                )
            )
            self._bundle = bundle
            self._panel = validation
            self._feature_panel = feature_panel
            self._phase5 = phase5
            self._phase5_by_seed = {
                int(row["seed"]): row
                for row in phase5["seed_results"]
            }

    def _scenario_config(
        self,
        *,
        leverage: float,
        initial_cash: float,
        cost_scenario: str,
    ) -> dict[str, Any]:
        config = deepcopy(self.config)
        config["broker"]["leverage"] = leverage
        config["broker"]["initial_cash"] = initial_cash
        values = {
            "low": (0.0001, 2, 0.20),
            "base": (
                float(self.config["cost"]["commission_rate"]),
                float(self.config["cost"]["buy_slippage_bps"]),
                float(self.config["execution"]["max_participation_rate"]),
            ),
            "high": (0.0006, 10, 0.05),
        }[cost_scenario]
        config["cost"]["commission_rate"] = values[0]
        config["cost"]["buy_slippage_bps"] = values[1]
        config["cost"]["sell_slippage_bps"] = values[1]
        config["execution"]["max_participation_rate"] = values[2]
        return config

    def _schedule(
        self,
        group: str,
        seed: int,
        weekly_dates,
        monthly_dates,
    ):
        dataset = self._bundle.dataset
        panel = self._panel
        top_n = int(self.config.get("strategy", {}).get("top_n", 5))
        if group == "G1":
            return build_g1_targets(
                rows=panel,
                dataset=dataset,
                execution_dates=monthly_dates,
                top_n=top_n,
                frequency="monthly",
            )
        if group in {"G2", "G3"}:
            targets, _ = build_g2_g3_targets(
                self.root,
                group=group,
                seed=seed,
                rows=panel,
                feature_panel=self._feature_panel,
                execution_dates=weekly_dates,
                dataset=dataset,
                top_n=top_n,
            )
            return targets
        if group == "G5":
            return build_g5_targets(
                rows=panel,
                dataset=dataset,
                execution_dates=weekly_dates,
                seed=seed,
                top_n=top_n,
            )
        return g3_weight_schedule(
            self.root,
            seed=seed,
            rows=panel,
            feature_panel=self._feature_panel,
            execution_dates=weekly_dates,
            dataset=dataset,
            top_n=top_n,
        )

    def backtest(
        self,
        *,
        group: str,
        leverage: float,
        initial_cash: float,
        cost_scenario: str,
        seed: int | None = None,
    ) -> dict[str, Any]:
        self._ensure_loaded()
        group = group.upper()
        if group not in {"G1", "G2", "G3", "G4", "G5", "G6"}:
            raise ValueError("group must be G1-G6")
        if cost_scenario not in {"low", "base", "high"}:
            raise ValueError("cost_scenario must be low, base or high")
        seed = seed or (G5_SEEDS[0] if group == "G5" else 17)
        cache_key = f"{group}-{leverage}-{initial_cash}-{cost_scenario}-{seed}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        dataset = self._bundle.dataset
        split = json.loads(
            (self.root / str(self.config["split"]["plan_path"])).read_text(
                encoding="utf-8"
            )
        )
        start = date.fromisoformat(str(split["validation_start"]))
        end = date.fromisoformat(str(split["validation_end"]))
        weekly = weekly_execution_dates(
            dataset,
            start=start,
            end=end,
            every_sessions=int(self.config["rl"].get("hold_sessions", 5)),
        )
        monthly = monthly_execution_dates(dataset, start=start, end=end)
        scenario_config = self._scenario_config(
            leverage=leverage,
            initial_cash=initial_cash,
            cost_scenario=cost_scenario,
        )
        counterfactual = None
        if group in {"G4", "G6"}:
            schedule = self._schedule(group, seed, weekly, monthly)
            states = market_states_by_cutoff(
                self._panel,
                [item.signal_date for item in schedule.values()],
            )
            normalizer = MarketStateNormalizer.from_dict(
                self._phase5_by_seed[seed]["normalizer"]
            )
            model_path = (
                self.root
                / f"data/models/phase5/{group.lower()}_seed_{seed}.pt"
            )
            model, policy_payload = load_policy(model_path)
            env = _make_environment(
                dataset=dataset,
                schedule=schedule,
                market_states=states,
                normalizer=normalizer,
                config=scenario_config,
                group=group,
                reward_mode="sharpe20" if group == "G4" else "cvar",
                leverages=(leverage,),
                random_start=False,
                max_episode_steps=len(schedule),
                cvar_lambda=float(
                    self._phase5["cvar_lambda_selected_on_validation"]
                ),
                end_date=end,
                seed=seed,
            )
            run_deterministic_policy(
                env,
                model,
                start_schedule_index=0,
                max_steps=len(schedule),
            )
            metrics = metrics_from_environment(
                env,
                scenario_config,
                group=group,
                seed=seed,
                leverage=leverage,
                initial_cash=initial_cash,
            )
            targets = env.run.targets
            day_results = env.run.day_results
            trades = [trade for day in day_results for trade in day.trades]
            snapshots = [day.snapshot for day in day_results]
            model_hash = canonical_file_hash(model_path)
        else:
            schedule = self._schedule(group, seed, weekly, monthly)
            run = run_target_schedule(
                dataset,
                schedule,
                scenario_config,
                start=start,
                end=end,
                group=group,
                seed=seed,
                initial_cash=initial_cash,
                leverage=leverage,
            )
            metrics = run.metrics
            targets = list(schedule.values())
            day_results = run.day_results
            trades = run.trades
            snapshots = run.snapshots
            model_hash = None
        if metrics.get("fee_drag") is None:
            targets_by_date = {
                target.execution_date: target for target in targets
            }
            counterfactual = run_target_schedule(
                dataset,
                targets_by_date,
                scenario_config,
                start=start,
                end=end,
                group=group,
                seed=seed,
                initial_cash=initial_cash,
                leverage=leverage,
                zero_cost=True,
            )
            metrics["fee_drag"] = (
                float(counterfactual.metrics["total_return"])
                - float(metrics["total_return"])
            )
            metrics["fee_drag_method"] = (
                "same_signal_path_zero_fee_zero_slippage_counterfactual"
            )
        metrics = to_primitive(metrics)
        payload = {
            "metadata": {
                "spec_version": str(self.config["spec_version"]),
                "group_name": group,
                "leverage": leverage,
                "initial_cash": initial_cash,
                "capital_base_mode": str(
                    scenario_config["broker"]["capital_base_mode"]
                ),
                "financing_mode": str(
                    scenario_config["broker"]["financing_mode"]
                ),
                "liquidation_model": str(
                    scenario_config["broker"]["liquidation_model"]
                ),
                "maintenance_fraction": float(
                    scenario_config["broker"]["maintenance_fraction"]
                ),
                "maintenance_floor": float(
                    scenario_config["broker"]["maintenance_floor"]
                ),
                "cost_model": str(scenario_config["cost"]["cost_model"]),
                "cost_scenario": cost_scenario,
                "execution_model": str(
                    scenario_config["execution"]["execution_model"]
                ),
                "p0_1_frozen": bool(
                    scenario_config["broker"]["p0_1_frozen"]
                ),
                "data_version": self.dashboard["metadata"]["data_version"],
                "split_id": "validation",
                "seed": seed,
                "model_sha256": model_hash,
                "holdout_evaluated": True,
                "research_warning": (
                    "Validation replay for visualization. Synthetic financing "
                    "and parameterized costs are not broker facts."
                ),
            },
            "metrics": metrics,
            "equity_curve": [
                {
                    "date": snapshot.trading_date.isoformat(),
                    "value": snapshot.equity,
                }
                for snapshot in snapshots
            ],
            "counterfactual_equity_curve": (
                [
                    {
                        "date": snapshot.trading_date.isoformat(),
                        "value": snapshot.equity,
                    }
                    for snapshot in counterfactual.snapshots
                ]
                if counterfactual is not None
                else []
            ),
            "trades": [to_primitive(trade) for trade in trades],
            "daily_snapshot": [to_primitive(snapshot) for snapshot in snapshots],
            "target_portfolio": [target.to_dict() for target in targets],
        }
        self._cache[cache_key] = payload
        return payload

    def daily_bars(self, code: str) -> dict[str, Any]:
        self._ensure_loaded()
        split = json.loads(
            (self.root / str(self.config["split"]["plan_path"])).read_text(
                encoding="utf-8"
            )
        )
        start = date.fromisoformat(str(split["validation_start"]))
        end = date.fromisoformat(str(split["validation_end"]))
        rows = []
        for session in self._bundle.dataset.sessions:
            if not start <= session <= end:
                continue
            bar = self._bundle.dataset.bar(session, code)
            if bar is None:
                continue
            rows.append(
                {
                    "date": session.isoformat(),
                    "timestamp": session.isoformat(),
                    "code": code,
                    "name": bar.name,
                    "open": bar.raw_open,
                    "high": bar.raw_high,
                    "low": bar.raw_low,
                    "close": bar.raw_close,
                    "volume": bar.volume,
                    "amount": bar.amount,
                }
            )
        return {
            "code": code,
            "name": rows[0]["name"] if rows else None,
            "price_adjustment": "none",
            "split_id": "validation",
            "bars": to_primitive(rows),
        }

    def matrix(self) -> dict[str, Any]:
        return self.dashboard

