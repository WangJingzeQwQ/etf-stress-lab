from __future__ import annotations

from datetime import date
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
from typing import Any, Iterable, Iterator

import yaml

from backend.data_pipeline.contracts import content_hash
from backend.data_pipeline.storage import canonical_file_hash, read_json, write_json_atomic


def _rows(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"Non-object row at {path}:{line_number}")
            yield row


def _groups(rows: Iterable[dict[str, Any]], field: str) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    current_key: str | None = None
    current_rows: list[dict[str, Any]] = []
    for row in rows:
        key = str(row[field])
        if current_key is None:
            current_key = key
        if key != current_key:
            yield current_key, current_rows
            current_key = key
            current_rows = []
        current_rows.append(row)
    if current_key is not None:
        yield current_key, current_rows


def _split_name(
    execution_date: date,
    label_end: date,
    plan: dict[str, Any],
) -> str:
    train_start = date.fromisoformat(plan["train_start"])
    train_end = date.fromisoformat(plan["train_end"])
    validation_start = date.fromisoformat(plan["validation_start"])
    validation_end = date.fromisoformat(plan["validation_end"])
    holdout_start = date.fromisoformat(plan["holdout_start"])
    holdout_end = date.fromisoformat(plan["holdout_end"])
    if execution_date < train_start:
        return "purged"
    if execution_date <= train_end:
        return "train" if label_end < validation_start else "purged"
    if execution_date < validation_start:
        return "purged"
    if execution_date <= validation_end:
        return "validation" if label_end < holdout_start else "purged"
    if execution_date < holdout_start:
        return "purged"
    if execution_date <= holdout_end:
        return "holdout"
    return "purged"


def build_modeling_dataset(
    *,
    features_path: Path,
    labels_path: Path,
    split_manifest_path: Path,
    output_dir: Path,
    report_path: Path,
) -> dict[str, Any]:
    feature_manifest = read_json(features_path.with_suffix(".json"), {})
    label_manifest = read_json(labels_path.with_suffix(".json"), {})
    split_manifest = read_json(split_manifest_path, {})
    if not (
        feature_manifest.get("source_kind") == "REAL_PROVIDER"
        and label_manifest.get("source_kind") == "REAL_PROVIDER"
        and feature_manifest.get("formal_research_allowed") is True
        and label_manifest.get("formal_research_allowed") is True
    ):
        raise ValueError("Modeling data requires formal feature and label artifacts")
    if feature_manifest.get("data_version") != label_manifest.get("data_version"):
        raise ValueError("Feature and label data versions differ")
    plan = split_manifest["split_plan"]
    if split_manifest.get("leakage_checks") and not all(
        split_manifest["leakage_checks"].values()
    ):
        raise ValueError("Split leakage checks did not all pass")

    output_dir.mkdir(parents=True, exist_ok=True)
    final_paths = {
        name: output_dir / f"{name}.ndjson"
        for name in ("train", "validation", "holdout", "purged")
    }
    temporary_paths = {
        name: path.with_suffix(path.suffix + ".tmp")
        for name, path in final_paths.items()
    }
    counts = {name: 0 for name in final_paths}
    matched_label_keys: set[tuple[str, str]] = set()
    feature_keys: set[tuple[str, str]] = set()

    with ExitStack() as stack:
        handles = {
            name: stack.enter_context(
                path.open("w", encoding="utf-8", newline="\n")
            )
            for name, path in temporary_paths.items()
        }
        label_groups = _groups(_rows(labels_path), "data_cutoff")
        current_label_key: str | None = None
        current_labels: list[dict[str, Any]] = []
        for cutoff, feature_rows in _groups(_rows(features_path), "data_cutoff"):
            while current_label_key is not None and current_label_key < cutoff:
                current_label_key, current_labels = next(
                    label_groups,
                    (None, []),
                )
            if current_label_key is None or current_label_key < cutoff:
                current_label_key, current_labels = next(
                    label_groups,
                    (None, []),
                )
            while current_label_key is not None and current_label_key < cutoff:
                current_label_key, current_labels = next(
                    label_groups,
                    (None, []),
                )
            labels_by_code = (
                {str(row["code"]): row for row in current_labels}
                if current_label_key == cutoff
                else {}
            )
            for feature in feature_rows:
                code = str(feature["code"])
                key = (cutoff, code)
                feature_keys.add(key)
                label = labels_by_code.get(code)
                if label is None:
                    continue
                execution_date = date.fromisoformat(str(label["execution_date"]))
                label_end = date.fromisoformat(str(label["label_end_date"]))
                split_name = _split_name(execution_date, label_end, plan)
                combined = {
                    **label,
                    **feature,
                    "split": split_name,
                    "horizon_sessions": int(label["horizon_sessions"]),
                }
                handles[split_name].write(
                    json.dumps(
                        combined,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                counts[split_name] += 1
                matched_label_keys.add(key)

        # Consume any label groups after the last feature group.
        for _, rows in label_groups:
            matched_label_keys.update(
                (str(row["data_cutoff"]), str(row["code"])) for row in rows
            )

    label_keys = {
        (str(row["data_cutoff"]), str(row["code"]))
        for row in _rows(labels_path)
    }
    missing_features = sorted(label_keys - feature_keys)
    unmatched_labels = sorted((label_keys & feature_keys) - matched_label_keys)
    if unmatched_labels:
        raise ValueError(
            f"{len(unmatched_labels)} labels were not assigned to a partition"
        )
    for name, path in temporary_paths.items():
        path.replace(final_paths[name])

    partitions = {
        name: {
            "path": str(path),
            "rows": counts[name],
            "artifact_sha256": canonical_file_hash(path),
        }
        for name, path in final_paths.items()
    }
    report = {
        "status": "READY",
        "source_kind": "REAL_PROVIDER",
        "data_version": feature_manifest["data_version"],
        "features_path": str(features_path),
        "labels_path": str(labels_path),
        "features_sha256": canonical_file_hash(features_path),
        "labels_sha256": canonical_file_hash(labels_path),
        "feature_keys": len(feature_keys),
        "label_keys": len(label_keys),
        "matched_keys": len(matched_label_keys),
        "labels_without_features": len(missing_features),
        "unmatched_feature_rows": len(feature_keys - label_keys),
        "partitions": partitions,
        "holdout_evaluated": False,
    }
    write_json_atomic(report_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Join formal feature and H=20 label panels into split datasets"
    )
    parser.add_argument("--config", default="config.m2_real.yaml")
    parser.add_argument(
        "--features",
        default="data/features/real/features_panel.ndjson",
    )
    parser.add_argument(
        "--labels",
        default="data/labels/real/labels_H20_panel.ndjson",
    )
    parser.add_argument("--output-dir", default="data/modeling/real")
    parser.add_argument(
        "--report",
        default="reports/modeling_dataset_manifest.json",
    )
    args = parser.parse_args(argv)
    root = Path(".").resolve()
    config = yaml.safe_load((root / args.config).read_text(encoding="utf-8"))
    split_manifest = root / str(config["split"]["report_path"])
    report = build_modeling_dataset(
        features_path=root / args.features,
        labels_path=root / args.labels,
        split_manifest_path=split_manifest,
        output_dir=root / args.output_dir,
        report_path=root / args.report,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
