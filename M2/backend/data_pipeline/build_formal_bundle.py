from __future__ import annotations

from datetime import date
from decimal import Decimal
import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from backend.data_pipeline.bundle_store import load_bundle, save_bundle
from backend.data_pipeline.contracts import (
    CoverageFlags,
    DataManifest,
    DataQualityError,
    Phase1Bundle,
    bundle_fingerprint,
)
from backend.data_pipeline.dataset_factory import build_dataset_from_raw
from backend.data_pipeline.fetch_master import load_master_records
from backend.data_pipeline.models import CorporateAction, TradingRule
from backend.data_pipeline.quality import require_quality, validate_bundle
from backend.data_pipeline.storage import (
    load_raw_daily,
    read_json,
    read_ndjson,
    write_json_atomic,
)
from backend.data_pipeline.trading_rule_audit import audit_trading_rules


def _rule_from_payload(payload: dict[str, Any]) -> TradingRule:
    return TradingRule(
        code=str(payload["code"]),
        settlement=str(payload["settlement"]),
        effective_from=date.fromisoformat(str(payload["effective_from"])),
        effective_to=date.fromisoformat(str(payload["effective_to"])),
        limit_pct=Decimal(str(payload["limit_pct"])),
        known_at=date.fromisoformat(str(payload["known_at"])),
        trading_status=str(payload["trading_status"]),
        source=str(payload["source"]),
        evidence_id=str(payload["evidence_id"]),
        evidence_sha256=(
            str(payload["evidence_sha256"])
            if payload.get("evidence_sha256")
            else None
        ),
    )


def _action_from_payload(payload: dict[str, Any]) -> CorporateAction:
    return CorporateAction(
        code=str(payload["code"]),
        effective_date=date.fromisoformat(str(payload["effective_date"])),
        known_at=date.fromisoformat(str(payload["known_at"])),
        action_type=str(payload["action_type"]),
        cash_per_share=Decimal(str(payload.get("cash_per_share", "0"))),
        split_ratio=Decimal(str(payload.get("split_ratio", "1"))),
    )


def build_formal_bundle(
    root: Path,
    config: dict[str, Any],
    *,
    write: bool = False,
) -> dict[str, Any]:
    pipeline = config["data_pipeline"]
    data = config["data"]
    start_date = date.fromisoformat(str(data["start_date"]))
    end_date = date.fromisoformat(str(data["end_date"]))
    bundle_root = root / str(pipeline["bundle_root"])
    master = load_master_records(root / str(pipeline["master_file"]))
    rule_payloads = read_ndjson(root / str(pipeline["trading_rules_file"]))
    rule_audit = audit_trading_rules(
        master,
        rule_payloads,
        start_date=start_date,
        end_date=end_date,
    )
    if not rule_audit.ready:
        raise DataQualityError(
            "Trading-rule audit is blocked: "
            f"{rule_audit.complete_codes}/{rule_audit.master_codes} codes complete; "
            f"issues={rule_audit.issue_counts}"
        )

    coverage = read_json(root / "reports" / "m1_2" / "raw_coverage_audit.json", {})
    if not (
        coverage.get("status") == "COMPLETE"
        and coverage.get("missing_files") == 0
        and coverage.get("codes_with_errors") == 0
    ):
        raise DataQualityError("Raw coverage audit is not COMPLETE")
    if not master or any(not item.history_complete for item in master.values()):
        raise DataQualityError("Master history is incomplete")

    action_payloads = read_ndjson(
        root / str(pipeline["corporate_actions_file"])
    )
    actions = [_action_from_payload(item) for item in action_payloads]
    if not actions:
        raise DataQualityError("Corporate-action evidence is missing")
    for action in actions:
        action.validate()
        if not start_date <= action.effective_date <= end_date:
            raise DataQualityError(
                f"Corporate action outside configured range: {action.code}"
            )

    cost_payloads = read_ndjson(root / str(pipeline["cost_schedules_file"]))
    scenarios = {
        str(item.get("scenario")) for item in cost_payloads if item.get("scenario")
    }
    if not {"low", "base", "high"}.issubset(scenarios):
        raise DataQualityError("low/base/high dated cost scenarios are required")

    raw_directory = root / str(pipeline["raw_directory"])
    raw_paths = sorted(raw_directory.glob("*.ndjson"))
    if len(raw_paths) != len(master):
        raise DataQualityError(
            f"Expected {len(master)} raw files, found {len(raw_paths)}"
        )
    raw_records = []
    for path in raw_paths:
        if path.stem not in master:
            raise DataQualityError(f"Raw file has no master record: {path.stem}")
        raw_records.extend(load_raw_daily(path))

    rules = [_rule_from_payload(item) for item in rule_payloads]
    dataset = build_dataset_from_raw(
        raw_records,
        master,
        rules,
        actions,
        source_kind="REAL_PROVIDER",
        metadata={
            "provider": str(pipeline.get("provider", "unknown")),
            "master_precision": "observed_exchange_or_raw_quote",
            "cost_evidence": "parameterized_non_broker_specific",
        },
    )
    manifest = DataManifest(
        source_kind="REAL_PROVIDER",
        source_name=str(pipeline.get("provider", "unknown")),
        data_version=bundle_fingerprint(dataset, master.values()),
        coverage=CoverageFlags(
            raw_ohlcv=True,
            historical_master=True,
            delist_history=True,
            corporate_actions=True,
            trading_rules=True,
            cost_schedules=True,
        ),
        notes=(
            "Master boundaries use exchange observation or valid raw quote dates.",
            "Cost schedules are dated parameterized research assumptions, not broker facts.",
            "Trading rules require source and evidence_id on every row.",
        ),
        real_market_evidence_allowed=True,
    )
    bundle = Phase1Bundle(
        dataset=dataset,
        master=master,
        manifest_data=manifest,
    )
    quality = validate_bundle(bundle, formal_research=True)
    require_quality(quality)

    result: dict[str, Any] = {
        "status": "VALIDATED_NOT_WRITTEN" if not write else "WRITTEN",
        "write_requested": write,
        "bundle_root": str(bundle_root),
        "data_version": manifest.data_version,
        "master_codes": len(master),
        "raw_files": len(raw_paths),
        "raw_rows": len(raw_records),
        "trading_rule_rows": len(rules),
        "corporate_actions": len(actions),
        "cost_scenarios": sorted(scenarios),
        "formal_research_allowed": quality.formal_research_allowed,
    }
    if write:
        paths = save_bundle(bundle_root, bundle)
        loaded = load_bundle(bundle_root)
        if loaded.manifest_data.data_version != manifest.data_version:
            raise DataQualityError("Reloaded formal bundle hash does not match")
        result["files"] = {key: str(value) for key, value in paths.items()}
        report_path = root / "reports" / "m1_2" / "formal_bundle_build.json"
        result["report"] = str(report_path)
        write_json_atomic(report_path, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate and optionally write the formal M1.2 bundle"
    )
    parser.add_argument("--config", default="config.m1_2.yaml")
    parser.add_argument(
        "--write",
        action="store_true",
        help="write canonical meta files and data_manifest.json after all checks",
    )
    args = parser.parse_args(argv)
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    try:
        result = build_formal_bundle(Path("."), config, write=args.write)
    except DataQualityError as exc:
        result = {
            "status": "BLOCKED",
            "write_requested": args.write,
            "error": str(exc),
            "formal_research_allowed": False,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
