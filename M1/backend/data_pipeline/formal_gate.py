from __future__ import annotations

from datetime import date
import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from backend.data_pipeline.bundle_store import load_bundle
from backend.data_pipeline.fetch_master import load_master_records
from backend.data_pipeline.storage import (
    canonical_file_hash,
    read_json,
    read_ndjson,
    write_json_atomic,
)
from backend.data_pipeline.trading_rule_audit import audit_trading_rules


def evaluate_formal_gate(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    def resolve(value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else root / path

    pipeline = config["data_pipeline"]
    data = config["data"]
    start_date = date.fromisoformat(str(data["start_date"]))
    end_date = date.fromisoformat(str(data["end_date"]))
    master = load_master_records(resolve(pipeline["master_file"]))
    coverage = read_json(resolve("reports/m1_2/raw_coverage_audit.json"), {})
    actions_path = resolve(pipeline.get("corporate_actions_file", ""))
    actions = read_ndjson(actions_path) if actions_path.exists() else []
    cost_path = resolve(pipeline.get("cost_schedules_file", ""))
    costs = read_ndjson(cost_path) if cost_path.exists() else []
    rules_path = resolve(pipeline.get("trading_rules_file", ""))
    rules = read_ndjson(rules_path) if rules_path.exists() else []
    raw_validation = read_json(
        resolve("reports/m1_2/trading_rules_raw_validation.json"),
        {},
    )
    bundle_root = resolve(pipeline.get("bundle_root", "data"))
    manifest_path = bundle_root / "meta" / "data_manifest.json"
    manifest = read_json(manifest_path)
    rule_audit = audit_trading_rules(
        master,
        rules,
        start_date=start_date,
        end_date=end_date,
    )

    action_matched = sum(
        action.get("known_at_quality") == "announcement_match"
        for action in actions
    )
    action_fallback = sum(
        action.get("known_at_quality") == "effective_date_fallback"
        for action in actions
    )
    action_known_at_valid = bool(actions) and all(
        bool(action.get("known_at"))
        and date.fromisoformat(str(action["known_at"]))
        <= date.fromisoformat(str(action["effective_date"]))
        for action in actions
    )
    cost_scenarios = {str(item.get("scenario")) for item in costs}
    cost_intervals_complete = True
    for scenario in ("low", "base", "high"):
        scenario_rows = [
            item for item in costs if str(item.get("scenario")) == scenario
        ]
        cursor = start_date
        for item in sorted(
            scenario_rows,
            key=lambda value: str(value.get("effective_from", "")),
        ):
            try:
                effective_from = date.fromisoformat(str(item["effective_from"]))
                effective_to = date.fromisoformat(str(item["effective_to"]))
            except (KeyError, ValueError):
                cost_intervals_complete = False
                break
            if effective_to < cursor:
                continue
            if effective_from > cursor:
                cost_intervals_complete = False
                break
            cursor = effective_to + date.resolution
            if cursor > end_date:
                break
        if cursor <= end_date:
            cost_intervals_complete = False
    complete_master_codes = sum(record.history_complete for record in master.values())
    required_coverage_keys = (
        "raw_ohlcv",
        "historical_master",
        "delist_history",
        "corporate_actions",
        "trading_rules",
        "cost_schedules",
    )
    manifest_coverage = (
        manifest.get("coverage", {}) if isinstance(manifest, dict) else {}
    )
    manifest_error: str | None = None
    loaded_bundle = None
    if manifest_path.exists():
        try:
            loaded_bundle = load_bundle(bundle_root)
        except Exception as exc:
            manifest_error = str(exc)

    canonical_master_matches = (
        loaded_bundle is not None
        and {
            code: record.to_dict()
            for code, record in loaded_bundle.master.items()
        }
        == {code: record.to_dict() for code, record in master.items()}
    )
    checks = {
        "raw_coverage": (
            coverage.get("status") == "COMPLETE"
            and coverage.get("missing_files") == 0
            and coverage.get("codes_with_errors") == 0
            and coverage.get("files_present") == len(master)
        ),
        "historical_master_complete": (
            bool(master) and complete_master_codes == len(master)
        ),
        "company_actions_known": (
            action_known_at_valid
            and all(
                start_date <= date.fromisoformat(str(action["effective_date"])) <= end_date
                for action in actions
            )
        ),
        "trading_rules_complete": rule_audit.ready,
        "trading_rules_raw_validated": bool(
            rules_path.exists()
            and raw_validation.get("status") == "VALIDATED"
            and raw_validation.get("rule_audit_status") == "READY"
            and raw_validation.get("error_count") == 0
            and raw_validation.get("totals", {}).get(
                "price_limit_violations", 0
            )
            == 0
            and raw_validation.get("rules_sha256")
            == canonical_file_hash(rules_path)
        ),
        "cost_schedule_present": (
            {"low", "base", "high"}.issubset(cost_scenarios)
            and cost_intervals_complete
        ),
        "real_bundle_manifest": bool(manifest)
        and isinstance(manifest, dict)
        and manifest.get("source_kind") == "REAL_PROVIDER"
        and manifest.get("real_market_evidence_allowed") is True
        and str(manifest.get("data_version", "")).startswith("sha256:")
        and all(manifest_coverage.get(key) is True for key in required_coverage_keys)
        and loaded_bundle is not None
        and loaded_bundle.manifest_data.to_dict() == manifest
        and canonical_master_matches,
    }
    reasons = [name for name, passed in checks.items() if not passed]
    return {
        "status": "READY" if not reasons else "BLOCKED",
        "formal_research_ready": not reasons,
        "checks": checks,
        "blocking_reasons": reasons,
        "counts": {
            "master_codes": len(master),
            "complete_master_codes": complete_master_codes,
            "raw_files_present": coverage.get("files_present"),
            "raw_files_missing": coverage.get("missing_files"),
            "raw_error_codes": coverage.get("codes_with_errors"),
            "company_actions": len(actions),
            "announcement_matched": action_matched,
            "effective_date_fallback": action_fallback,
            "trading_rule_rows": len(rules),
            "valid_trading_rule_rows": rule_audit.valid_rule_rows,
            "raw_validation_status": raw_validation.get("status"),
            "raw_price_limit_violations": raw_validation.get(
                "totals", {}
            ).get("price_limit_violations", 0),
            "complete_rule_codes": rule_audit.complete_codes,
            "incomplete_rule_codes": len(rule_audit.incomplete_codes),
            "missing_rule_codes": len(rule_audit.missing_rule_codes),
            "cost_scenarios": len(costs),
        },
        "trading_rule_audit": {
            "status": "READY" if rule_audit.ready else "BLOCKED",
            "coverage_contract": "full_required_lifecycle_calendar_coverage",
            "known_at_policy": "known_at <= effective_from",
            "complete_codes": rule_audit.complete_codes,
            "incomplete_code_count": len(rule_audit.incomplete_codes),
            "missing_rule_code_count": len(rule_audit.missing_rule_codes),
            "issue_counts": rule_audit.issue_counts,
        },
        "manifest_error": manifest_error,
        "paths": {
            "master": str(pipeline["master_file"]),
            "corporate_actions": str(actions_path),
            "trading_rules": str(rules_path),
            "cost_schedules": str(cost_path),
            "data_manifest": str(manifest_path),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate M1.2 formal research gate")
    parser.add_argument("--config", default="config.m1_2.yaml")
    parser.add_argument("--output", default="reports/m1_2/formal_gate_status.json")
    args = parser.parse_args(argv)
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    report = evaluate_formal_gate(Path("."), config)
    write_json_atomic(Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["formal_research_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

