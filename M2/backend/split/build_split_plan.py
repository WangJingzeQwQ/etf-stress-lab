from __future__ import annotations

from datetime import date
import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from backend.data_pipeline.contracts import content_hash
from backend.data_pipeline.storage import (
    read_ndjson,
    write_json_atomic,
    write_ndjson_atomic,
)
from backend.split.splitter import (
    build_expanding_walk_forward,
    build_fractional_chronological_split,
    freeze_split_plan,
    split_label_records,
    validate_split_plan,
)


def build_split_artifacts(
    root: Path,
    config: dict[str, Any],
    *,
    labels_path: Path,
) -> dict[str, Any]:
    split_config = config["split"]
    horizon = int(config["labels"]["horizon_sessions"])
    labels = read_ndjson(labels_path)
    if not labels:
        raise ValueError(f"No labels found in {labels_path}")
    execution_dates = sorted(
        {date.fromisoformat(str(row["execution_date"])) for row in labels}
    )
    plan = build_fractional_chronological_split(
        execution_dates,
        horizon_sessions=horizon,
        train_fraction=float(split_config.get("train_fraction", 0.65)),
        validation_fraction=float(split_config.get("validation_fraction", 0.15)),
        embargo_sessions=int(split_config.get("embargo_sessions", 0)),
        holdout_id=str(split_config.get("holdout_id", "final_holdout_v1")),
    )
    validate_split_plan(plan, execution_dates, horizon_sessions=horizon)
    freeze_path = root / str(split_config["plan_path"])
    frozen = freeze_split_plan(plan, freeze_path)
    assigned = split_label_records(labels, plan)
    train_label_end = max(
        date.fromisoformat(str(row["label_end_date"]))
        for row in assigned.train
    )
    validation_label_end = max(
        date.fromisoformat(str(row["label_end_date"]))
        for row in assigned.validation
    )
    keys = {
        "train": {
            (str(row["execution_date"]), str(row["code"]))
            for row in assigned.train
        },
        "validation": {
            (str(row["execution_date"]), str(row["code"]))
            for row in assigned.validation
        },
        "holdout": {
            (str(row["execution_date"]), str(row["code"]))
            for row in assigned.holdout
        },
        "purged": {
            (str(row["execution_date"]), str(row["code"]))
            for row in assigned.purged
        },
    }
    all_partition_keys = set().union(*keys.values())
    source_keys = {
        (str(row["execution_date"]), str(row["code"]))
        for row in labels
    }
    leakage_checks = {
        "train_label_end_before_validation_start": (
            train_label_end < plan.validation_start
        ),
        "validation_label_end_before_holdout_start": (
            validation_label_end < plan.holdout_start
        ),
        "holdout_starts_after_validation": (
            plan.holdout_start > plan.validation_end
        ),
        "partitions_are_disjoint": (
            sum(len(values) for values in keys.values()) == len(all_partition_keys)
        ),
        "all_source_rows_assigned_once": all_partition_keys == source_keys,
        "purge_sessions_match_horizon_plus_one": (
            plan.purge_sessions == horizon + 1 + plan.embargo_sessions
        ),
    }
    if not all(leakage_checks.values()):
        raise ValueError(f"Split leakage audit failed: {leakage_checks}")
    partition_root = root / str(split_config["partition_root"])
    partition_paths = {
        "train": partition_root / "train.ndjson",
        "validation": partition_root / "validation.ndjson",
        "holdout": partition_root / "holdout.ndjson",
        "purged": partition_root / "purged.ndjson",
    }
    write_ndjson_atomic(partition_paths["train"], assigned.train)
    write_ndjson_atomic(partition_paths["validation"], assigned.validation)
    write_ndjson_atomic(partition_paths["holdout"], assigned.holdout)
    write_ndjson_atomic(partition_paths["purged"], assigned.purged)

    walk_config = split_config.get("walk_forward", {})
    development_dates = [
        value for value in execution_dates if value <= plan.validation_end
    ]
    folds = build_expanding_walk_forward(
        development_dates,
        initial_train_sessions=int(
            walk_config.get("initial_train_sessions", 250)
        ),
        validation_sessions=int(walk_config.get("validation_sessions", 60)),
        step_sessions=int(walk_config.get("step_sessions", 60)),
        horizon_sessions=horizon,
        embargo_sessions=int(split_config.get("embargo_sessions", 0)),
        development_end=plan.validation_end,
    )
    partitions = {
        name: {
            "rows": len(rows),
            "path": str(path),
            "rows_sha256": content_hash([dict(row) for row in rows]),
            "date_from": (
                min(str(row["execution_date"]) for row in rows) if rows else None
            ),
            "date_to": (
                max(str(row["execution_date"]) for row in rows) if rows else None
            ),
        }
        for (name, path), rows in zip(
            partition_paths.items(),
            (
                assigned.train,
                assigned.validation,
                assigned.holdout,
                assigned.purged,
            ),
        )
    }
    report = {
        "status": "FROZEN",
        "holdout_evaluated": False,
        "source_labels": str(labels_path),
        "source_labels_sha256": content_hash(labels),
        "horizon_sessions": horizon,
        "unique_execution_dates": len(execution_dates),
        "split_plan": frozen,
        "leakage_checks": leakage_checks,
        "train_max_label_end": train_label_end.isoformat(),
        "validation_max_label_end": validation_label_end.isoformat(),
        "partitions": partitions,
        "walk_forward": {
            "count": len(folds),
            "folds": [fold.to_dict() for fold in folds],
        },
    }
    report_path = root / str(
        split_config.get("report_path", "reports/phase3_split_manifest.json")
    )
    write_json_atomic(report_path, report)
    return {
        "status": report["status"],
        "plan": frozen,
        "partitions": partitions,
        "walk_forward_folds": len(folds),
        "report": str(report_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Freeze chronological train/validation/holdout splits"
    )
    parser.add_argument("--config", default="config.m2_real.yaml")
    parser.add_argument(
        "--labels",
        default="data/labels/real/labels_H20_panel.ndjson",
    )
    args = parser.parse_args(argv)
    root = Path(".").resolve()
    config = yaml.safe_load((root / args.config).read_text(encoding="utf-8"))
    result = build_split_artifacts(
        root,
        config,
        labels_path=root / args.labels,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
