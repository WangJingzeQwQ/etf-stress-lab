from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Mapping
import argparse
import json
import math

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import yaml

from backend.data_pipeline.storage import (
    canonical_file_hash,
    write_json_atomic,
)
from backend.engine.portfolio_backtest import (
    aggregate_group_results,
    numeric_summary,
)
from backend.engine.serialization import to_primitive
from backend.engine.survival import (
    SurvivalObservation,
    kaplan_meier,
    time_block_bootstrap_ci,
)


GROUPS = ("G1", "G2", "G3", "G4", "G5", "G6")
GROUP_COLORS = {
    "G1": "#5B8FF9",
    "G2": "#5AD8A6",
    "G3": "#F6BD16",
    "G4": "#E8684A",
    "G5": "#9270CA",
    "G6": "#269A99",
}


def _cell_key(cell: Mapping[str, Any]) -> tuple[int, int, str]:
    return (
        int(cell["leverage"]),
        int(cell["initial_cash"]),
        str(cell["cost_scenario"]),
    )


def _correct_cell_groups(cell: Mapping[str, Any]) -> dict[str, Any]:
    corrected = {}
    for group in GROUPS:
        adapters = [
            SimpleNamespace(metrics=row["metrics"])
            for row in cell["seed_results"][group]
        ]
        corrected[group] = aggregate_group_results(adapters)
    return corrected


def _group_survival_curve(
    cell: Mapping[str, Any],
    group: str,
) -> list[dict[str, Any]]:
    observations = [
        SurvivalObservation(
            path_id=row["survival"]["path_id"],
            event_observed=bool(row["survival"]["event_observed"]),
            time_sessions=int(row["survival"]["time_sessions"]),
        )
        for row in cell["seed_results"][group]
    ]
    return kaplan_meier(observations)


def _equity_curves_by_group(
    cell: Mapping[str, Any],
    group: str,
) -> list[list[dict[str, Any]]]:
    return [
        row["equity_curve"]
        for row in cell["seed_results"][group]
        if row.get("equity_curve")
    ]


def _mean_normalised_curve(curves: Iterable[list[dict[str, Any]]]) -> tuple[list[str], list[float]]:
    series = []
    valid_curves = [curve for curve in curves if curve]
    for curve in valid_curves:
        if not curve:
            continue
        values = np.asarray([float(point["value"]) for point in curve])
        if values.size == 0 or values[0] == 0:
            continue
        series.append(values / values[0])
    if not series:
        return [], []
    length = min(len(values) for values in series)
    matrix = np.vstack([values[:length] for values in series])
    dates = [point["date"] for point in valid_curves[0][:length]]
    return dates, matrix.mean(axis=0).tolist()


def _bootstrap_equity_curves(
    curves: Iterable[list[dict[str, Any]]],
) -> dict[str, Any]:
    all_returns = []
    for curve in curves:
        values = [float(point["value"]) for point in curve]
        returns = [
            current / previous - 1.0
            for previous, current in zip(values, values[1:])
            if previous > 0
        ]
        all_returns.extend(returns)
    return time_block_bootstrap_ci(
        all_returns,
        block_size=20,
        repetitions=2000,
        confidence=0.95,
        seed=20261031,
    )


def _table(rows: Iterable[dict[str, Any]], headers: list[str]) -> str:
    rows = list(rows)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(":---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(str(row.get(header, "")) for header in headers)
            + " |"
        )
    return "\n".join(lines)


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def analyze(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    report_root = root / "reports"
    figure_root = report_root / "figures"
    figure_root.mkdir(parents=True, exist_ok=True)
    raw_path = report_root / "holdout_results.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    if raw.get("status") != "HOLDOUT_EVALUATED" or len(raw["cells"]) != 72:
        raise ValueError("Expected a completed 72-cell Holdout matrix")
    modeling_manifest = json.loads(
        (report_root / "modeling_dataset_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    cells = {}
    corrected_cells = []
    aggregate_correction = {}
    for raw_cell in raw["cells"]:
        cell = dict(raw_cell)
        corrected = _correct_cell_groups(cell)
        aggregate_correction[
            "|".join(str(value) for value in _cell_key(cell))
        ] = {
            group: {
                "raw_paths": raw_cell["groups"][group]["paths"],
                "corrected_paths": corrected[group]["paths"],
                "changed": raw_cell["groups"][group] != corrected[group],
            }
            for group in GROUPS
        }
        cell["groups"] = corrected
        cell["survival_curves_by_group"] = {
            group: _group_survival_curve(cell, group)
            for group in GROUPS
        }
        cells[_cell_key(cell)] = cell
        corrected_cells.append(cell)

    base_l1 = cells[(1, 1_000_000, "base")]
    primary_cell = cells[
        (
            int(raw["primary"]["cell"]["leverage"]),
            int(raw["primary"]["cell"]["initial_cash"]),
            str(raw["primary"]["cell"]["cost_scenario"]),
        )
    ]
    primary = raw["primary"]

    # Validation-time block bootstrap is applied to the saved daily equity
    # curves of the base 1x cell. It is diagnostic, not used to select models.
    bootstrap = {}
    for group in GROUPS:
        curves = _equity_curves_by_group(base_l1, group)
        bootstrap[group] = _bootstrap_equity_curves(curves)

    secondary = {
        group: base_l1["groups"][group] for group in GROUPS
    }
    liquidation_grid = {
        group: {
            str(leverage): cells[
                (leverage, 1_000_000, "base")
            ]["groups"][group]["first_liquidation_probability"]
            for leverage in config["rl"].get(
                "report_leverages", [1, 2, 5, 10, 25, 50, 75, 100]
            )
        }
        for group in GROUPS
    }
    cash_grid = {
        group: {
            str(cash): cells[(1, cash, "base")]["groups"][group]
            for cash in (100_000, 1_000_000, 10_000_000)
        }
        for group in GROUPS
    }
    cost_grid = {
        group: {
            scenario: cells[(1, 1_000_000, scenario)]["groups"][group]
            for scenario in ("low", "base", "high")
        }
        for group in GROUPS
    }

    # Figure 1: 1x net asset curves.
    fig, ax = plt.subplots(figsize=(12, 6))
    for group in GROUPS:
        dates, values = _mean_normalised_curve(
            _equity_curves_by_group(base_l1, group)
        )
        if not dates:
            continue
        x = np.arange(len(values))
        ax.plot(x, values, label=group, color=GROUP_COLORS[group], linewidth=1.8)
    ax.set_title("Final Holdout: G1-G6 Validation-Frozen Strategies at 1x")
    ax.set_xlabel("Trading sessions")
    ax.set_ylabel("Mean cumulative equity multiple")
    ax.grid(alpha=0.2)
    ax.legend(ncol=3)
    fig.text(
        0.01,
        0.01,
        "Synthetic financing | synthetic_v1 liquidation | parameterized costs",
        fontsize=8,
        color="#666666",
    )
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    equity_png = figure_root / "holdout_g1_g6_1x_equity.png"
    fig.savefig(equity_png, dpi=200)
    plt.close(fig)

    # Figure 2: liquidation probability heatmap.
    leverages = config["rl"].get(
        "report_leverages", [1, 2, 5, 10, 25, 50, 75, 100]
    )
    matrix = np.asarray(
        [
            [
                liquidation_grid[group][str(leverage)]
                for leverage in leverages
            ]
            for group in GROUPS
        ],
        dtype=float,
    )
    fig, ax = plt.subplots(figsize=(10, 4.8))
    image = ax.imshow(matrix, aspect="auto", cmap="magma_r", vmin=0, vmax=1)
    ax.set_xticks(range(len(leverages)), [f"{value}x" for value in leverages])
    ax.set_yticks(range(len(GROUPS)), GROUPS)
    ax.set_title("First-liquidation probability | base cost | ¥1,000,000")
    for row in range(matrix.shape[0]):
        for column in range(matrix.shape[1]):
            value = matrix[row, column]
            ax.text(
                column,
                row,
                f"{value:.0%}",
                ha="center",
                va="center",
                color="white" if value > 0.55 else "black",
                fontsize=8,
            )
    fig.colorbar(image, ax=ax, label="Probability")
    fig.tight_layout()
    heatmap_png = figure_root / "holdout_liquidation_heatmap.png"
    fig.savefig(heatmap_png, dpi=200)
    plt.close(fig)

    # Figure 3: survival curves for G4 and G6 at selected leverage.
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8), sharey=True)
    for axis, group in zip(axes, ("G4", "G6")):
        for leverage in (1, 10, 50, 100):
            cell = cells[(leverage, 1_000_000, "base")]
            curve = cell["survival_curves_by_group"][group]
            if not curve:
                continue
            axis.step(
                [point["time_sessions"] for point in curve],
                [point["survival"] for point in curve],
                where="post",
                label=f"{leverage}x",
            )
        axis.set_title(group)
        axis.set_xlabel("Trading sessions")
        axis.grid(alpha=0.2)
        axis.legend()
    axes[0].set_ylabel("Survival probability")
    fig.suptitle("First-liquidation survival | base cost | ¥1,000,000")
    fig.tight_layout()
    survival_png = figure_root / "holdout_survival_g4_g6.png"
    fig.savefig(survival_png, dpi=200)
    plt.close(fig)

    # Figure 4: fees, slippage and fee drag by capital size at 1x.
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8))
    cash_values = [100_000, 1_000_000, 10_000_000]
    width = 0.13
    x = np.arange(len(cash_values))
    for offset, group in enumerate(GROUPS):
        axes[0].bar(
            x + (offset - 2.5) * width,
            [
                cash_grid[group][str(cash)]["total_commission"]["mean"]
                for cash in cash_values
            ],
            width=width,
            label=group,
            color=GROUP_COLORS[group],
        )
        axes[1].bar(
            x + (offset - 2.5) * width,
            [
                cash_grid[group][str(cash)]["slippage_cost"]["mean"]
                for cash in cash_values
            ],
            width=width,
            color=GROUP_COLORS[group],
        )
        axes[2].bar(
            x + (offset - 2.5) * width,
            [
                cash_grid[group][str(cash)]["fee_drag"]["mean"]
                for cash in cash_values
            ],
            width=width,
            color=GROUP_COLORS[group],
        )
    for axis, title in zip(
        axes,
        ("Commission", "Slippage cost", "Fee drag"),
    ):
        axis.set_xticks(x, ["¥100k", "¥1m", "¥10m"])
        axis.set_title(title)
        axis.grid(axis="y", alpha=0.2)
    axes[0].legend(ncol=2, fontsize=8)
    fig.suptitle("Capital-size friction at 1x | base cost | mean across saved paths")
    fig.tight_layout()
    fees_png = figure_root / "holdout_capital_fee_sensitivity.png"
    fig.savefig(fees_png, dpi=200)
    plt.close(fig)

    # Figure 5: multi-seed return distribution at 1x.
    fig, ax = plt.subplots(figsize=(10, 5))
    distributions = []
    labels = []
    for group in GROUPS:
        values = [
            float(row["metrics"]["total_return"])
            for row in base_l1["seed_results"][group]
        ]
        if values:
            distributions.append(values)
            labels.append(group)
    ax.boxplot(distributions, labels=labels, showfliers=True)
    ax.axhline(0, color="#999999", linewidth=0.8)
    ax.set_title("Final Holdout: seed distribution of total return at 1x")
    ax.set_ylabel("Total return")
    ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    seed_png = figure_root / "holdout_seed_distribution_1x.png"
    fig.savefig(seed_png, dpi=200)
    plt.close(fig)

    analysis = {
        "status": "HOLDOUT_ANALYSIS_COMPLETE",
        "holdout_evaluated": True,
        "raw_result_path": str(raw_path),
        "raw_result_sha256": canonical_file_hash(raw_path),
        "preregistration_hash": raw["preregistration_hash"],
        "data_version": modeling_manifest["data_version"],
        "date_from": raw["date_from"],
        "date_to": raw["date_to"],
        "path_sessions": raw["path_sessions"],
        "primary": primary,
        "secondary_1x_base_1m": secondary,
        "liquidation_grid_base_1m": liquidation_grid,
        "capital_grid_1x_base": cash_grid,
        "cost_grid_1x_base_1m": cost_grid,
        "time_block_bootstrap_1x": bootstrap,
        "g1_weekly_sensitivity_base": raw["g1_weekly_sensitivity_base"],
        "aggregate_correction": {
            "reason": (
                "Raw G4/G6 group aggregates retained the final seed only. "
                "Derived aggregates use every saved paired-seed record. "
                "Raw Holdout file and hash are unchanged."
            ),
            "cells": aggregate_correction,
        },
        "figures": {
            "equity": str(equity_png),
            "liquidation_heatmap": str(heatmap_png),
            "survival": str(survival_png),
            "capital_fees": str(fees_png),
            "seed_distribution": str(seed_png),
        },
        "research_boundary": (
            "Synthetic 100x leverage is an extreme stress scenario. Costs are "
            "parameterized assumptions. Values are not real broker financing "
            "capacity or guaranteed executable returns."
        ),
    }
    write_json_atomic(
        report_root / "holdout_analysis.json",
        to_primitive(analysis),
    )

    primary_rows = []
    for endpoint in (
        "first_liquidation_probability",
        "time_to_first_liquidation_trading_days",
        "expected_shortfall_95",
    ):
        row = primary[endpoint]
        primary_rows.append(
            {
                "Endpoint": endpoint,
                "Mean G4": _fmt(row["G4_seed_values"] and np.mean(row["G4_seed_values"])),
                "Mean G6": _fmt(row["G6_seed_values"] and np.mean(row["G6_seed_values"])),
                "G6-G4": _fmt(row["mean_difference"]),
                "Holm p": _fmt(row["holm_adjusted_p_value"]),
            }
        )
    secondary_rows = []
    for group in GROUPS:
        row = secondary[group]
        secondary_rows.append(
            {
                "Group": group,
                "Paths": row["paths"],
                "Return": _fmt(row["total_return"]["mean"]),
                "Max DD": _fmt(row["max_drawdown"]["mean"]),
                "Sharpe": _fmt(row["sharpe"]["mean"]),
                "Fee drag": _fmt(row["fee_drag"]["mean"]),
                "Liq. prob": _fmt(row["first_liquidation_probability"]),
            }
        )
    markdown = "\n".join(
        [
            "# Final Holdout 与压力矩阵报告",
            "",
            "日期：2026-10-31",
            "",
            "状态：**HOLDOUT_EVALUATED；ONE-TIME；NO RETRY**",
            "",
            f"- Holdout：{raw['date_from']} .. {raw['date_to']}，{raw['path_sessions']} 个交易日",
            f"- 原始结果 SHA-256：`{analysis['raw_result_sha256']}`",
            f"- 预注册哈希：`{analysis['preregistration_hash']}`",
            "- 72 个“杠杆 × 资金 × 费用”场景单元；每单元包含 G1-G6 多 seed 路径。",
            "",
            "## 主比较",
            "",
            "主比较单元为 50x、100 万、base。允许 G6 不优于 G4。",
            "",
            _table(primary_rows, ["Endpoint", "Mean G4", "Mean G6", "G6-G4", "Holm p"]),
            "",
            "结论：在该冻结样本外单元中，G4 与 G6 的强平概率、生存时间和 95% Expected Shortfall 没有可辨识差异；不能宣称 CVaR reward shaping 改善尾部风险。",
            "",
            "## 1x 消融对照",
            "",
            "G4/G6 在本表中使用五个 seed 的重新聚合；原始文件保持不变。",
            "",
            _table(
                secondary_rows,
                ["Group", "Paths", "Return", "Max DD", "Sharpe", "Fee drag", "Liq. prob"],
            ),
            "",
            "G1 月频与周频存在决策频率差异，不能把差异直接归因于模型能力。G5 的结果是 20 seed 分布均值。",
            "",
            "## 图表",
            "",
            f"![1x 净值]({equity_png.as_posix()})",
            "",
            f"![强平热图]({heatmap_png.as_posix()})",
            "",
            f"![生存曲线]({survival_png.as_posix()})",
            "",
            f"![资金费用]({fees_png.as_posix()})",
            "",
            f"![seed 分布]({seed_png.as_posix()})",
            "",
            "## 重要限制与审计事件",
            "",
            "- 强平与融资是 synthetic_v1 / synthetic_stress，不是任何券商事实；100x 仅是极端合成压力。",
            "- 费用是参数化研究假设；交易与强平使用 raw OHLC，特征和标签使用点时化总收益。",
            "- 结算遵守逐代码 T0/T1、ADV、停牌和涨跌停；未成交数量未被伪装成交。",
            "- 首次强平后主路径终止；未强平路径右截尾。",
            "- Holdout 执行期间发生两次实现故障：首次误读标签分区、第二次生存时间汇总类型错误。两次均在生成可用性能结论前失败；claim 未重置，之后仅从同一 claim 继续，并保留审计记录。最终原始结果哈希见上。",
            "- 派生汇总修正了 G4/G6 原始 group 聚合只保留最后一个 seed 的问题；主比较与 seed 级原始明细未改。",
            "",
            analysis["research_boundary"],
            "",
        ]
    )
    (report_root / "final_holdout_report.md").write_text(
        markdown,
        encoding="utf-8",
    )

    dashboard_cells = []
    for cell in corrected_cells:
        dashboard_cells.append(
            {
                "leverage": cell["leverage"],
                "initial_cash": cell["initial_cash"],
                "cost_scenario": cell["cost_scenario"],
                "groups": {
                    group: {
                        "paths": cell["groups"][group]["paths"],
                        "total_return": cell["groups"][group]["total_return"],
                        "max_drawdown": cell["groups"][group]["max_drawdown"],
                        "sharpe": cell["groups"][group]["sharpe"],
                        "first_liquidation_probability": cell["groups"][group][
                            "first_liquidation_probability"
                        ],
                        "censored_paths": cell["groups"][group]["censored_paths"],
                        "fee_drag": cell["groups"][group]["fee_drag"],
                    }
                    for group in GROUPS
                },
            }
        )
    dashboard = {
        "metadata": {
            "spec_version": config["spec_version"],
            "split_id": "final_holdout_v1",
            "data_source_kind": "REAL_PROVIDER",
            "data_version": modeling_manifest["data_version"],
            "financing_mode": "synthetic_stress",
            "liquidation_model": "synthetic_v1",
            "maintenance_fraction": 0.5,
            "maintenance_floor": 0.0025,
            "cost_model": "parameterized_etf_v1",
            "p0_1_frozen": True,
            "preregistration_hash": raw["preregistration_hash"],
        },
        "date_from": raw["date_from"],
        "date_to": raw["date_to"],
        "cells": dashboard_cells,
        "primary": primary,
        "report_sha256": canonical_file_hash(
            report_root / "final_holdout_report.md"
        ),
    }
    write_json_atomic(
        report_root / "dashboard.json",
        to_primitive(dashboard),
    )
    return analysis


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build Holdout report, figures and dashboard JSON"
    )
    parser.add_argument("--config", default="config.m2_real.yaml")
    args = parser.parse_args(argv)
    root = Path(".").resolve()
    config = yaml.safe_load(
        (root / args.config).read_text(encoding="utf-8")
    )
    report = analyze(root, config)
    print(
        json.dumps(
            {
                "status": report["status"],
                "date_from": report["date_from"],
                "date_to": report["date_to"],
                "raw_result_sha256": report["raw_result_sha256"],
                "figures": len(report["figures"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

