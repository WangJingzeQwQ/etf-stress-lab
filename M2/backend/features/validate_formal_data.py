from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable

import yaml

from backend.data_pipeline.build_total_return import (
    materialize_point_in_time_total_returns,
)
from backend.data_pipeline.build_universe import (
    DailyUniverseBuilder,
    UniverseConfig,
)
from backend.data_pipeline.bundle_store import load_bundle
from backend.data_pipeline.contracts import DataQualityError, content_hash
from backend.data_pipeline.quality import require_quality, validate_bundle
from backend.data_pipeline.storage import (
    canonical_file_hash,
    read_json,
    read_ndjson,
    write_json_atomic,
)
from backend.features.build_features import (
    FEATURE_NAMES,
    build_feature_snapshot,
)
from backend.features.label_builder import forward_open_return


@dataclass(frozen=True)
class ValidationResult:
    name: str
    passed: bool
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            **self.details,
        }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DataQualityError(message)


def _rows_hash(rows: Iterable[dict[str, Any]]) -> str:
    return content_hash(list(rows))


def validate_artifact_manifest(
    *,
    data_path: Path,
    manifest_path: Path,
    data_version: str,
    expected_kind: str,
    horizon_sessions: int | None = None,
) -> ValidationResult:
    _require(data_path.exists(), f"Missing artifact: {data_path}")
    _require(manifest_path.exists(), f"Missing manifest: {manifest_path}")
    rows = read_ndjson(data_path)
    manifest = read_json(manifest_path)
    _require(isinstance(manifest, dict), f"Invalid manifest: {manifest_path}")
    _require(
        manifest.get("source_kind") == "REAL_PROVIDER",
        f"{expected_kind} artifact is not REAL_PROVIDER",
    )
    _require(
        manifest.get("formal_research_allowed") is True,
        f"{expected_kind} artifact is not formal-research enabled",
    )
    _require(
        manifest.get("data_version") == data_version,
        f"{expected_kind} artifact has a stale bundle version",
    )
    _require(
        int(manifest.get("rows", -1)) == len(rows),
        f"{expected_kind} manifest row count mismatch",
    )
    _require(
        manifest.get("artifact_sha256") == canonical_file_hash(data_path),
        f"{expected_kind} artifact SHA-256 mismatch",
    )
    _require(
        manifest.get("rows_sha256") == _rows_hash(rows),
        f"{expected_kind} semantic row SHA-256 mismatch",
    )
    details: dict[str, Any] = {
        "rows": len(rows),
        "data_path": str(data_path),
        "manifest_path": str(manifest_path),
        "artifact_sha256": canonical_file_hash(data_path),
        "rows_sha256": manifest.get("rows_sha256"),
    }
    if expected_kind == "feature":
        _require(
            manifest.get("feature_names") == list(FEATURE_NAMES),
            "Feature manifest schema mismatch",
        )
        keys = [(row.get("data_cutoff"), row.get("code")) for row in rows]
        _require(len(keys) == len(set(keys)), "Duplicate feature keys")
        for row in rows:
            for name in FEATURE_NAMES:
                value = row.get(name)
                _require(
                    isinstance(value, (int, float)) and math.isfinite(float(value)),
                    f"Non-finite feature {name} for {row.get('code')}",
                )
        details.update(
            {
                "feature_count": len(FEATURE_NAMES),
                "cutoffs": sorted({str(row["data_cutoff"]) for row in rows}),
            }
        )
    elif expected_kind == "label":
        _require(horizon_sessions is not None, "Label horizon is required")
        keys = [(row.get("execution_date"), row.get("code")) for row in rows]
        _require(len(keys) == len(set(keys)), "Duplicate label keys")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            _require(
                int(row.get("horizon_sessions", -1)) == horizon_sessions,
                "Label horizon mismatch",
            )
            _require(
                str(row["data_cutoff"]) < str(row["execution_date"])
                <= str(row["label_end_date"]),
                f"Invalid label dates for {row.get('code')}",
            )
            grouped.setdefault(str(row["execution_date"]), []).append(row)
        for execution_date, group in grouped.items():
            expected_top = max(1, math.ceil(len(group) * 0.20))
            actual_top = sum(bool(row["label_top20"]) for row in group)
            _require(
                actual_top == expected_top,
                f"Top-20 label count mismatch on {execution_date}",
            )
        details.update(
            {
                "horizon_sessions": horizon_sessions,
                "date_from": min(grouped) if grouped else None,
                "date_to": max(grouped) if grouped else None,
                "dates": len(grouped),
            }
        )
    else:
        raise ValueError(f"Unsupported artifact kind: {expected_kind}")
    return ValidationResult(f"validate_{expected_kind}_artifact", True, details)


def _next_sessions(
    sessions: tuple[date, ...],
    cutoff: date,
    count: int,
) -> list[date]:
    index = sessions.index(cutoff)
    return list(sessions[index + 1 : index + 1 + count])


def validate_truncation_invariance(
    bundle,
    *,
    cutoffs: tuple[date, ...],
    universe_config: UniverseConfig,
    truncation_count: int = 2,
) -> ValidationResult:
    samples: list[dict[str, Any]] = []
    for cutoff in cutoffs:
        _require(cutoff in bundle.dataset.sessions, f"Unknown cutoff {cutoff}")
        if cutoff == bundle.dataset.sessions[-1]:
            raise DataQualityError("Truncation cutoff requires a later session")
        execution = _next_sessions(bundle.dataset.sessions, cutoff, 1)[0]
        decisions = DailyUniverseBuilder(bundle, universe_config).build(
            data_cutoff=cutoff,
            execution_date=execution,
        )
        codes = {item.code for item in decisions if item.eligible}
        baseline = build_feature_snapshot(
            bundle.dataset,
            data_cutoff=cutoff,
            universe_codes=codes,
        )
        baseline_payload = [row.to_dict() for row in baseline]
        baseline_hash = content_hash(baseline_payload)
        for truncation in _next_sessions(
            bundle.dataset.sessions,
            cutoff,
            truncation_count,
        ):
            truncated = bundle.dataset.as_of(truncation)
            actual = build_feature_snapshot(
                truncated,
                data_cutoff=cutoff,
                universe_codes=codes,
            )
            actual_payload = [row.to_dict() for row in actual]
            if actual_payload != baseline_payload:
                raise DataQualityError(
                    f"Feature truncation mismatch at {cutoff} using {truncation}"
                )
        samples.append(
            {
                "cutoff": cutoff.isoformat(),
                "execution_date": execution.isoformat(),
                "eligible_codes": len(codes),
                "feature_rows": len(baseline),
                "feature_rows_sha256": baseline_hash,
                "truncations_checked": truncation_count,
            }
        )
    return ValidationResult(
        "validate_multi_cutoff_truncation",
        True,
        {"samples": samples},
    )


def validate_rule_visibility(
    bundle,
    *,
    cutoffs: tuple[date, ...],
    universe_config: UniverseConfig,
) -> ValidationResult:
    samples: list[dict[str, Any]] = []
    for cutoff in cutoffs:
        execution = _next_sessions(bundle.dataset.sessions, cutoff, 1)[0]
        decisions = DailyUniverseBuilder(bundle, universe_config).build(
            data_cutoff=cutoff,
            execution_date=execution,
        )
        future_rules = []
        unresolved_eligible = []
        settlements = {"T0": 0, "T1": 0}
        for item in decisions:
            rule, error = bundle.dataset.rule_lookup_known_by(
                item.code,
                execution,
                cutoff,
            )
            if rule is not None and (
                rule.known_at is not None and rule.known_at > cutoff
            ):
                future_rules.append(item.code)
            if item.eligible and (rule is None or error is not None):
                unresolved_eligible.append(item.code)
            if item.eligible and rule is not None:
                settlements[rule.settlement] += 1
        _require(
            not future_rules,
            f"Future-known rule reached the {cutoff} universe",
        )
        _require(
            not unresolved_eligible,
            f"Eligible codes lack a rule known by {cutoff}",
        )
        samples.append(
            {
                "data_cutoff": cutoff.isoformat(),
                "execution_date": execution.isoformat(),
                "eligible_codes": sum(item.eligible for item in decisions),
                "eligible_settlements": settlements,
                "future_known_rules": len(future_rules),
            }
        )
    return ValidationResult(
        "validate_rule_visibility",
        True,
        {"samples": samples},
    )


def validate_delisting(
    bundle,
    *,
    universe_config: UniverseConfig,
) -> ValidationResult:
    sessions = bundle.dataset.sessions
    execution = sessions[-1]
    cutoff = sessions[-2]
    builder = DailyUniverseBuilder(bundle, universe_config)
    decisions = {
        item.code: item
        for item in builder.build(
            data_cutoff=cutoff,
            execution_date=execution,
        )
    }
    post_delist = [
        code
        for code, record in bundle.master.items()
        if record.delist_date is not None and record.delist_date < execution
    ]
    failures = [
        code
        for code in post_delist
        if decisions[code].eligible
        or "delisted_before_execution" not in decisions[code].reasons
    ]
    _require(
        not failures,
        "Delisted codes remain eligible after their recorded delist date: "
        + ", ".join(failures[:10]),
    )

    historical_examples: list[dict[str, Any]] = []
    bars_by_code: dict[str, list[Any]] = {}
    for (_, code), bar in bundle.dataset.bars.items():
        bars_by_code.setdefault(code, []).append(bar)
    for bars in bars_by_code.values():
        bars.sort(key=lambda bar: bar.trading_date)
    for code in sorted(post_delist):
        record = bundle.master[code]
        if record.delist_date is None:
            continue
        code_bars = [
            bar
            for bar in bars_by_code.get(code, [])
            if bar.trading_date < record.delist_date
        ]
        best_candidate: tuple[Any, int] | None = None
        for end in range(59, len(code_bars)):
            execution = code_bars[end].trading_date
            try:
                exec_index = sessions.index(execution)
            except ValueError:
                continue
            if exec_index == 0:
                continue
            trailing = code_bars[end - 19 : end + 1]
            average_amount = sum(
                (bar.amount for bar in trailing),
                start=type(trailing[0].amount)(0),
            ) / type(trailing[0].amount)(20)
            if best_candidate is None or average_amount > best_candidate[0]:
                best_candidate = (average_amount, exec_index)
        if best_candidate is None:
            continue
        _, exec_index = best_candidate
        signal = sessions[exec_index - 1]
        item = {
            decision.code: decision
            for decision in builder.build(
                data_cutoff=signal,
                execution_date=sessions[exec_index],
            )
        }[code]
        if item.eligible:
            historical_examples.append(
                {
                    "code": code,
                    "execution_date": sessions[exec_index].isoformat(),
                    "delist_date": record.delist_date.isoformat(),
                    "eligible": True,
                }
            )
            if len(historical_examples) >= 5:
                break
    return ValidationResult(
        "validate_delisting",
        True,
        {
            "execution_date": execution.isoformat(),
            "checked_after_delist": len(post_delist),
            "after_delist_failures": 0,
            "eligible_before_delist_examples": historical_examples,
        },
    )


def validate_corporate_action_returns(bundle) -> ValidationResult:
    sessions = bundle.dataset.sessions
    samples: list[dict[str, Any]] = []
    for action_type in ("cash_dividend", "split"):
        candidates = [
            action
            for action in bundle.dataset.corporate_actions
            if action.action_type == action_type
        ]
        checked = 0
        for action in candidates:
            prior = [
                session
                for session in sessions
                if session < action.effective_date
                and bundle.dataset.bar(session, action.code) is not None
            ]
            after = [
                session
                for session in sessions
                if session > action.effective_date
                and bundle.dataset.bar(session, action.code) is not None
            ]
            if not prior or not after:
                continue
            execution = prior[-1]
            label_end = after[0]
            horizon = sessions.index(label_end) - sessions.index(execution)
            actual, actual_end = forward_open_return(
                bundle.dataset,
                code=action.code,
                execution_date=execution,
                horizon_sessions=horizon,
            )
            visible = materialize_point_in_time_total_returns(
                bundle.dataset,
                label_end,
                codes={action.code},
            )
            start_open = (
                visible.require_bar(execution, action.code).total_return_close
                / visible.require_bar(execution, action.code).raw_close
                * visible.require_bar(execution, action.code).raw_open
            )
            end_open = (
                visible.require_bar(label_end, action.code).total_return_close
                / visible.require_bar(label_end, action.code).raw_close
                * visible.require_bar(label_end, action.code).raw_open
            )
            expected = float(end_open / start_open - 1)
            _require(
                actual_end == label_end and abs(actual - expected) <= 1e-12,
                f"Corporate-action return mismatch for {action.code}",
            )
            samples.append(
                {
                    "code": action.code,
                    "action_type": action.action_type,
                    "effective_date": action.effective_date.isoformat(),
                    "execution_date": execution.isoformat(),
                    "label_end_date": label_end.isoformat(),
                    "forward_return": actual,
                }
            )
            checked += 1
            if checked >= 3:
                break
        _require(checked > 0, f"No testable {action_type} event found")
    return ValidationResult(
        "validate_corporate_action_returns",
        True,
        {"samples": samples},
    )


def validate_modeling_manifest(
    root: Path,
    *,
    data_version: str,
) -> ValidationResult:
    path = root / "reports/modeling_dataset_manifest.json"
    manifest = read_json(path, {})
    _require(manifest.get("status") == "READY", "Modeling dataset is not ready")
    _require(
        manifest.get("source_kind") == "REAL_PROVIDER",
        "Modeling dataset is not REAL_PROVIDER",
    )
    _require(
        manifest.get("data_version") == data_version,
        "Modeling dataset has a stale bundle version",
    )
    _require(
        manifest.get("holdout_evaluated") is False,
        "Modeling manifest claims an evaluated holdout",
    )
    checked = []
    for name, partition in manifest.get("partitions", {}).items():
        partition_path = Path(str(partition["path"]))
        if not partition_path.is_absolute():
            partition_path = root / partition_path
        _require(partition_path.exists(), f"Missing modeling partition {name}")
        _require(
            canonical_file_hash(partition_path)
            == partition["artifact_sha256"],
            f"Modeling partition hash mismatch: {name}",
        )
        checked.append(
            {
                "partition": name,
                "rows": int(partition["rows"]),
                "artifact_sha256": partition["artifact_sha256"],
            }
        )
    _require(
        {"train", "validation", "holdout", "purged"}
        == {row["partition"] for row in checked},
        "Modeling partitions are incomplete",
    )
    return ValidationResult(
        "validate_modeling_dataset",
        True,
        {
            "rows": int(manifest["matched_keys"]),
            "labels_without_features": int(
                manifest["labels_without_features"]
            ),
            "partitions": checked,
            "holdout_evaluated": False,
        },
    )


def run_validation(
    root: Path,
    config_path: Path,
    *,
    feature_path: Path | None = None,
    feature_manifest_path: Path | None = None,
    label_path: Path | None = None,
    label_manifest_path: Path | None = None,
) -> dict[str, Any]:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    bundle_root = Path(str(config["data_pipeline"]["bundle_root"]))
    if not bundle_root.is_absolute():
        bundle_root = root / bundle_root
    bundle = load_bundle(bundle_root)
    quality = validate_bundle(bundle, formal_research=True)
    require_quality(quality)
    _require(
        quality.formal_research_allowed,
        "Formal bundle quality gate is not ready",
    )
    universe_config = UniverseConfig.from_mapping(config["universe"])
    horizon = int(config["labels"]["horizon_sessions"])
    feature_path = feature_path or (
        root / "data/features/real/features_20240131.ndjson"
    )
    feature_manifest_path = feature_manifest_path or feature_path.with_suffix(
        ".json"
    )
    label_path = label_path or (
        root / "data/labels/real/labels_H20_20240201.ndjson"
    )
    label_manifest_path = label_manifest_path or label_path.with_suffix(".json")
    feature_panel_path = root / "data/features/real/features_panel.ndjson"
    label_panel_path = root / "data/labels/real/labels_H20_panel.ndjson"
    cutoffs = (
        date(2020, 12, 31),
        date(2024, 1, 31),
        date(2026, 6, 30),
    )
    _require(
        all(cutoff in bundle.dataset.sessions for cutoff in cutoffs),
        "A configured validation cutoff is absent from the formal calendar",
    )
    checks = [
        validate_artifact_manifest(
            data_path=feature_path,
            manifest_path=feature_manifest_path,
            data_version=bundle.manifest_data.data_version,
            expected_kind="feature",
        ),
        validate_artifact_manifest(
            data_path=label_path,
            manifest_path=label_manifest_path,
            data_version=bundle.manifest_data.data_version,
            expected_kind="label",
            horizon_sessions=horizon,
        ),
        validate_artifact_manifest(
            data_path=feature_panel_path,
            manifest_path=feature_panel_path.with_suffix(".json"),
            data_version=bundle.manifest_data.data_version,
            expected_kind="feature",
        ),
        validate_artifact_manifest(
            data_path=label_panel_path,
            manifest_path=label_panel_path.with_suffix(".json"),
            data_version=bundle.manifest_data.data_version,
            expected_kind="label",
            horizon_sessions=horizon,
        ),
        validate_truncation_invariance(
            bundle,
            cutoffs=cutoffs,
            universe_config=universe_config,
        ),
        validate_rule_visibility(
            bundle,
            cutoffs=cutoffs,
            universe_config=universe_config,
        ),
        validate_delisting(bundle, universe_config=universe_config),
        validate_corporate_action_returns(bundle),
        validate_modeling_manifest(
            root,
            data_version=bundle.manifest_data.data_version,
        ),
    ]
    return {
        "status": "READY" if all(item.passed for item in checks) else "BLOCKED",
        "formal_research_allowed": True,
        "source_kind": bundle.manifest_data.source_kind,
        "data_version": bundle.manifest_data.data_version,
        "bundle_quality": quality.to_dict(),
        "check_count": len(checks),
        "checks": [item.to_dict() for item in checks],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate formal Phase 2 features, labels and point-in-time behavior"
    )
    parser.add_argument("--config", default="config.m2_real.yaml")
    parser.add_argument(
        "--output",
        default="reports/phase2_formal_validation.json",
    )
    parser.add_argument("--features", default=None)
    parser.add_argument("--labels", default=None)
    args = parser.parse_args(argv)
    root = Path(".").resolve()
    try:
        report = run_validation(
            root,
            root / args.config,
            feature_path=Path(args.features) if args.features else None,
            label_path=Path(args.labels) if args.labels else None,
        )
    except Exception as exc:
        report = {
            "status": "BLOCKED",
            "formal_research_allowed": False,
            "error": str(exc),
        }
        output = root / args.output
        write_json_atomic(output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    output = root / args.output
    write_json_atomic(output, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "checks": report["check_count"],
                "data_version": report["data_version"],
                "output": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
