from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Mapping
import argparse
import json

import yaml

from backend.data_pipeline.contracts import content_hash
from backend.data_pipeline.storage import (
    canonical_file_hash,
    write_json_atomic,
)
from backend.split.splitter import ChronologicalSplitPlan


PREREGISTRATION_VERSION = "master-comparison-v1"


def _artifact_list(root: Path, config: Mapping[str, Any]) -> dict[str, str]:
    paths = {
        "formal_gate": root / "reports/../.." / "M1/reports/m1_2/formal_gate_status.json",
        "modeling_manifest": root / "reports/modeling_dataset_manifest.json",
        "phase3_manifest": root / str(config["split"]["report_path"]),
        "development_backtest": root / "reports/development_backtest.json",
        "g2_results": root / "reports/g2_seed_results.json",
        "g3_results": root / "reports/g3_seed_results.json",
        "g4_g6_results": root / "reports/g4_g6_validation.json",
        "g5_results": root / "reports/g5_seed_results.json",
        "g6_lambda_selection": root / "reports/g6_lambda_selection.json",
    }
    for seed in config["models"]["paired_seeds"]:
        paths[f"g2_seed_{seed}"] = root / f"data/models/g2/g2_seed_{seed}.txt"
        paths[f"g3_a_seed_{seed}"] = root / f"data/models/g3/g3_a_seed_{seed}.txt"
        paths[f"g3_c_seed_{seed}"] = root / f"data/models/g3/g3_c_seed_{seed}.pt"
        paths[f"g4_seed_{seed}"] = root / f"data/models/phase5/g4_seed_{seed}.pt"
        paths[f"g6_seed_{seed}"] = root / f"data/models/phase5/g6_seed_{seed}.pt"
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Preregistration artifacts are missing: {missing}")
    return {
        name: canonical_file_hash(path)
        for name, path in sorted(paths.items())
    }


def build_preregistration(
    root: Path,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    phase3 = json.loads(
        (root / str(config["split"]["report_path"])).read_text(encoding="utf-8")
    )
    if phase3.get("holdout_evaluated") is not False:
        raise PermissionError("Holdout has already been evaluated")
    development = json.loads(
        (root / "reports/development_backtest.json").read_text(encoding="utf-8")
    )
    modeling_manifest = json.loads(
        (root / "reports/modeling_dataset_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    if development.get("holdout_evaluated") is not False:
        raise PermissionError("Development report has accessed the final Holdout")
    phase5 = json.loads(
        (root / "reports/g4_g6_validation.json").read_text(encoding="utf-8")
    )
    artifacts = _artifact_list(root, config)
    seeds = [int(value) for value in config["models"]["paired_seeds"]]
    g5_seeds = [int(value) for value in config["models"]["g5_seeds"]]
    cost = {
        "low": {
            "commission_rate": 0.00010,
            "minimum_commission_per_order": 5.0,
            "exchange_fee_rate": 0.0,
            "stamp_duty_rate": 0.0,
            "fixed_other_fee": 0.0,
            "buy_slippage_bps": 2,
            "sell_slippage_bps": 2,
            "max_participation_rate": 0.20,
        },
        "base": {
            "commission_rate": float(config["cost"]["commission_rate"]),
            "minimum_commission_per_order": float(
                config["cost"]["minimum_commission_per_order"]
            ),
            "exchange_fee_rate": float(config["cost"]["exchange_fee_rate"]),
            "stamp_duty_rate": float(config["cost"]["stamp_duty_rate"]),
            "fixed_other_fee": float(config["cost"]["fixed_other_fee"]),
            "buy_slippage_bps": float(config["cost"]["buy_slippage_bps"]),
            "sell_slippage_bps": float(config["cost"]["sell_slippage_bps"]),
            "max_participation_rate": float(
                config["execution"]["max_participation_rate"]
            ),
        },
        "high": {
            "commission_rate": 0.00060,
            "minimum_commission_per_order": 5.0,
            "exchange_fee_rate": 0.0,
            "stamp_duty_rate": 0.0,
            "fixed_other_fee": 0.0,
            "buy_slippage_bps": 10,
            "sell_slippage_bps": 10,
            "max_participation_rate": 0.05,
        },
    }
    return {
        "schema": "etf_stress_preregistration_v1",
        "version": PREREGISTRATION_VERSION,
        "frozen_at": "2026-10-31",
        "status": "FROZEN_BEFORE_FINAL_HOLDOUT",
        "holdout_evaluated": False,
        "spec_version": str(config["spec_version"]),
        "research_claim_limit": (
            "Synthetic financing, synthetic_v1 liquidation and parameterized "
            "costs. No conclusion is a real broker financing capability."
        ),
        "data": {
            "source_kind": "REAL_PROVIDER",
            "data_version": development.get(
                "data_version",
                modeling_manifest["data_version"],
            ),
            "split_plan_hash": phase3["split_plan"]["plan_hash"],
            "train": {
                "start": phase3["split_plan"]["train_start"],
                "end": phase3["split_plan"]["train_end"],
            },
            "validation": {
                "start": phase3["split_plan"]["validation_start"],
                "end": phase3["split_plan"]["validation_end"],
            },
            "holdout": {
                "start": phase3["split_plan"]["holdout_start"],
                "end": phase3["split_plan"]["holdout_end"],
            },
            "purge_sessions": phase3["split_plan"]["purge_sessions"],
        },
        "models": {
            "paired_seeds": seeds,
            "g5_seeds": g5_seeds,
            "artifact_hashes": artifacts,
            "g4_g6_training_budget_steps": int(
                phase5["training_budget_steps_per_policy"]
            ),
            "cvar": {
                "window": 120,
                "alpha": 0.10,
                "lambda": float(
                    phase5["cvar_lambda_selected_on_validation"]
                ),
                "lambda_selection_scope": "validation_only",
                "windows_sensitivity": [60, 120, 250],
                "alpha_sensitivity": [0.05, 0.10],
                "interpretation": "CVaR reward shaping, not a constrained MDP",
            },
        },
        "scenario_grid": {
            "leverage": [1, 2, 5, 10, 25, 50, 75, 100],
            "initial_cash": [100000, 1000000, 10000000],
            "cost_scenarios": cost,
            "min_commission_sensitivity": [0, 5, 10],
            "financing_mode": "synthetic_stress",
            "capital_base_mode": "initial_equity",
            "liquidation_model": "synthetic_v1",
            "maintenance_fraction": 0.50,
            "maintenance_floor": 0.0025,
            "annual_interest_sensitivity": [0.03, 0.06, 0.10],
        },
        "primary_comparison": {
            "question": (
                "Does CVaR reward shaping change high-leverage survival or "
                "tail outcomes relative to paired standard PPO?"
            ),
            "groups": ["G4", "G6"],
            "cell": {
                "leverage": 50,
                "initial_cash": 1000000,
                "cost_scenario": "base",
            },
            "endpoints": [
                "first_liquidation_probability",
                "time_to_first_liquidation_trading_days",
                "expected_shortfall_95",
            ],
            "paired_unit": "seed",
            "seed_count": len(seeds),
            "winner_may_be_G4_or_G6_or_neither": True,
        },
        "secondary_comparison": {
            "groups": [
                "G1_monthly",
                "G1_weekly",
                "G2",
                "G3",
                "G4",
                "G5",
                "G6",
            ],
            "cell": {
                "leverage": 1,
                "initial_cash": 1000000,
                "cost_scenario": "base",
            },
            "endpoints": [
                "total_return",
                "max_drawdown",
                "sharpe",
                "fee_drag",
                "cost_ratio",
            ],
            "g1_frequency_treated_as_sensitivity": True,
            "g5_reported_as_seed_distribution": True,
        },
        "survival": {
            "event": "first_liquidation",
            "censoring": "right_censored_if_no_first_liquidation",
            "main_policy": "first_liquidation_terminates",
            "curve": "Kaplan-Meier",
            "confidence_level": 0.95,
            "bootstrap": {
                "method": "moving_time_block",
                "block_sessions": 20,
                "repetitions": 2000,
                "seed": 20261031,
            },
            "matrix_cells_not_independent_samples": True,
        },
        "multiple_testing": {
            "family": "primary_group_endpoint_tests",
            "method": "Holm",
            "alpha": 0.05,
        },
        "execution_accounting": {
            "price": "unadjusted raw OHLC",
            "lot_size": 100,
            "t_plus_one": "code-and-date rule table",
            "adv": "T-1 ADV20 participation cap",
            "stop_and_limit": "no synthetic fill through missing or locked bars",
            "debt": "event-based; interest based on prior close debt",
            "liquidation_day": "skip strategy rebalance",
            "terminal": "no reentry or new position",
        },
        "fixed_choices": {
            "do_not_change_threshold_seed_window_or_metric_after_holdout": True,
            "do_not_modify_split_dates": True,
            "do_not_fit_on_holdout": True,
            "do_not_retry_holdout": True,
        },
    }


def _markdown(payload: Mapping[str, Any], json_hash: str) -> str:
    primary = payload["primary_comparison"]
    cost_grid = payload["scenario_grid"]["cost_scenarios"]
    lines = [
        "# 主对比预注册 v1",
        "",
        "状态：**FROZEN_BEFORE_FINAL_HOLDOUT**",
        "",
        f"规范 JSON 哈希：`{json_hash}`",
        "",
        "## 研究问题",
        "",
        str(primary["question"]),
        "",
        "该问题允许 G4 胜出、G6 胜出或没有可辨识差异。任何 CVaR 改善声明必须来自冻结后的 Holdout，并受配对 seed 与时间块 bootstrap 区间约束。",
        "",
        "## 主比较单元",
        "",
        f"- 杠杆：{primary['cell']['leverage']}x（极端合成压力）",
        f"- 初始资金：¥{primary['cell']['initial_cash']:,}",
        f"- 费用：{primary['cell']['cost_scenario']}",
        f"- 配对 seed：{', '.join(str(value) for value in payload['models']['paired_seeds'])}",
        f"- 端点：{', '.join(primary['endpoints'])}",
        "",
        "## 冻结矩阵",
        "",
        f"- 杠杆：{payload['scenario_grid']['leverage']}",
        f"- 资金：{payload['scenario_grid']['initial_cash']}",
        f"- 费用情景：{', '.join(cost_grid)}",
        "- G1 月频为主规则频率，周频为独立频率敏感性。",
        "- G5 使用 20 个独立 seed；G2-G6 使用五组配对 seed。",
        "",
        "## CVaR",
        "",
        f"- 主窗口：{payload['models']['cvar']['window']} 个交易日",
        f"- Alpha：{payload['models']['cvar']['alpha']}",
        f"- Lambda：{payload['models']['cvar']['lambda']}（仅 validation 选择）",
        "- 损失定义为 `-return`；CVaR 为最差尾部正损失均值。",
        "- 实现是 reward-shaped PPO，不是严格约束型 CVaR MDP。",
        "",
        "## 生存与推断",
        "",
        "- 事件为首次强平；未发生事件为右截尾。",
        "- 使用 20 个交易日移动时间块 bootstrap，95%，2000 次。",
        "- 主比较多端点采用 Holm 校正，α=0.05。",
        "- 矩阵单元格不作为独立样本。",
        "",
        "## 冻结承诺",
        "",
        "在读取 Holdout 前冻结模型、scaler、seed、CVaR 参数、费用、杠杆、资金、指标和统计规则。Holdout 首次 claim 后不因结果修改阈值、seed、窗口或重试。",
        "",
        "## 声明边界",
        "",
        str(payload["research_claim_limit"]),
        "",
    ]
    return "\n".join(lines)


def freeze_preregistration(
    root: Path,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    payload = build_preregistration(root, config)
    directory = root / "reports/preregistration"
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "master_comparison_v1.json"
    markdown_path = directory / "master_comparison_v1.md"
    checksum_path = directory / "master_comparison_v1.sha256"
    expected_hash = content_hash(payload)
    if json_path.exists():
        existing = json.loads(json_path.read_text(encoding="utf-8"))
        if existing != payload:
            raise RuntimeError("Existing preregistration differs from current protocol")
        if content_hash(existing) != expected_hash:
            raise RuntimeError("Existing preregistration hash mismatch")
        return {
            "status": "ALREADY_FROZEN",
            "preregistration_hash": expected_hash,
            "json_path": str(json_path),
            "markdown_path": str(markdown_path),
            "checksum_path": str(checksum_path),
        }
    markdown = _markdown(payload, expected_hash)
    write_json_atomic(json_path, payload)
    markdown_path.write_text(markdown + "\n", encoding="utf-8")
    checksum = (
        f"{canonical_file_hash(json_path)}  master_comparison_v1.json\n"
        f"{canonical_file_hash(markdown_path)}  master_comparison_v1.md\n"
        f"canonical-json {expected_hash}\n"
    )
    checksum_path.write_text(checksum, encoding="utf-8")
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    if content_hash(loaded) != expected_hash:
        raise RuntimeError("Preregistration failed read-back hash verification")
    return {
        "status": "FROZEN",
        "preregistration_hash": expected_hash,
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
        "checksum_path": str(checksum_path),
    }


def verify_preregistration(root: Path) -> tuple[dict[str, Any], str]:
    json_path = root / "reports/preregistration/master_comparison_v1.json"
    checksum_path = root / "reports/preregistration/master_comparison_v1.sha256"
    if not json_path.exists() or not checksum_path.exists():
        raise FileNotFoundError("Preregistration is not frozen")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    actual = content_hash(payload)
    checksum = checksum_path.read_text(encoding="utf-8")
    if actual not in checksum or payload.get("holdout_evaluated") is not False:
        raise RuntimeError("Preregistration checksum or state is invalid")
    return payload, actual


def split_plan_from_preregistration(
    payload: Mapping[str, Any],
) -> ChronologicalSplitPlan:
    data = payload["data"]
    return ChronologicalSplitPlan(
        train_start=date.fromisoformat(data["train"]["start"]),
        train_end=date.fromisoformat(data["train"]["end"]),
        validation_start=date.fromisoformat(data["validation"]["start"]),
        validation_end=date.fromisoformat(data["validation"]["end"]),
        holdout_start=date.fromisoformat(data["holdout"]["start"]),
        holdout_end=date.fromisoformat(data["holdout"]["end"]),
        horizon_sessions=20,
        embargo_sessions=0,
        holdout_id="final_holdout_v1",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze Holdout preregistration")
    parser.add_argument("--config", default="config.m2_real.yaml")
    args = parser.parse_args(argv)
    root = Path(".").resolve()
    config = yaml.safe_load(
        (root / args.config).read_text(encoding="utf-8")
    )
    result = freeze_preregistration(root, config)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

