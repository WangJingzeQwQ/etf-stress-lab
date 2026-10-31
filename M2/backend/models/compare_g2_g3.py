from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from backend.data_pipeline.storage import write_json_atomic


METRICS = (
    "mean_rank_ic",
    "rank_ic_ir",
    "mean_top5_excess_return",
    "top20_precision",
)


def _paired_summary(values: list[float]) -> dict[str, float]:
    mean = sum(values) / len(values)
    variance = (
        sum((value - mean) ** 2 for value in values) / (len(values) - 1)
        if len(values) > 1
        else 0.0
    )
    standard_error = math.sqrt(variance) / math.sqrt(len(values))
    t_critical = 2.776 if len(values) == 5 else 1.96
    return {
        "mean": mean,
        "std": math.sqrt(variance),
        "ci95_low": mean - t_critical * standard_error,
        "ci95_high": mean + t_critical * standard_error,
        "min": min(values),
        "max": max(values),
    }


def compare_reports(
    g2_path: Path,
    g3_path: Path,
    *,
    output_path: Path,
    markdown_path: Path,
) -> dict[str, Any]:
    g2 = json.loads(g2_path.read_text(encoding="utf-8"))
    g3 = json.loads(g3_path.read_text(encoding="utf-8"))
    if g2["seeds"] != g3["seeds"]:
        raise ValueError("G2/G3 seeds are not paired")
    if g2.get("holdout_evaluated") is not False or g3.get(
        "holdout_evaluated"
    ) is not False:
        raise ValueError("Paired comparison must not include final holdout")
    g2_by_seed = {row["seed"]: row["validation"] for row in g2["seed_results"]}
    g3_by_seed = {row["seed"]: row["validation"] for row in g3["seed_results"]}
    comparisons = {}
    for metric in METRICS:
        differences = [
            float(g3_by_seed[seed][metric]) - float(g2_by_seed[seed][metric])
            for seed in g2["seeds"]
        ]
        comparisons[metric] = {
            "g2_seed_values": [
                float(g2_by_seed[seed][metric]) for seed in g2["seeds"]
            ],
            "g3_seed_values": [
                float(g3_by_seed[seed][metric]) for seed in g3["seeds"]
            ],
            "paired_g3_minus_g2": differences,
            "paired_summary": _paired_summary(differences),
        }
    report = {
        "status": "VALIDATION_ONLY",
        "holdout_evaluated": False,
        "seeds": g2["seeds"],
        "data_version": g3["data_version"],
        "decision_rule": (
            "Paired seed differences are reported with a 95% t interval. "
            "A final superiority claim requires the frozen one-time holdout "
            "evaluation and all downstream cost/leverage stress tests."
        ),
        "comparisons": comparisons,
    }
    write_json_atomic(output_path, report)
    lines = [
        "# G2 vs G3 Paired Validation Comparison",
        "",
        "Status: **VALIDATION_ONLY; final holdout not evaluated.**",
        "",
        f"Paired seeds: `{g2['seeds']}`",
        "",
        "| Metric | G3 - G2 mean | 95% CI |",
        "| :--- | ---: | :---: |",
    ]
    for metric, result in comparisons.items():
        summary = result["paired_summary"]
        lines.append(
            f"| `{metric}` | {summary['mean']:.6g} | "
            f"[{summary['ci95_low']:.6g}, {summary['ci95_high']:.6g}] |"
        )
    lines.extend(
        [
            "",
            "The interval is across five paired initialization seeds. "
            "It is not a trading-result confidence interval and does not "
            "replace chronological block bootstrap or final holdout evidence.",
            "",
        ]
    )
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare paired G2/G3 validation seeds")
    parser.add_argument("--g2", default="reports/g2_seed_results.json")
    parser.add_argument("--g3", default="reports/g3_seed_results.json")
    parser.add_argument(
        "--output",
        default="reports/g2_g3_paired_comparison.json",
    )
    parser.add_argument(
        "--markdown",
        default="reports/g2_g3_paired_comparison.md",
    )
    args = parser.parse_args(argv)
    root = Path(".").resolve()
    report = compare_reports(
        root / args.g2,
        root / args.g3,
        output_path=root / args.output,
        markdown_path=root / args.markdown,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "holdout_evaluated": report["holdout_evaluated"],
                "comparisons": {
                    metric: result["paired_summary"]
                    for metric, result in report["comparisons"].items()
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
