from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from backend.data_pipeline.contracts import (
    CoverageFlags,
    DataManifest,
    DataQualityError,
    Phase1Bundle,
    RawDailyRecord,
    bundle_fingerprint,
)
from backend.data_pipeline.dataset_factory import build_dataset_from_raw
from backend.data_pipeline.fetch_master import load_master_records, save_master_records
from backend.data_pipeline.quality import validate_bundle
from backend.data_pipeline.storage import (
    read_json,
    read_ndjson,
    save_raw_daily,
    write_json_atomic,
    write_ndjson_atomic,
)
from backend.data_pipeline.models import CorporateAction, TradingRule


def _rule_to_dict(rule: TradingRule) -> dict[str, Any]:
    return {
        "code": rule.code,
        "settlement": rule.settlement,
        "effective_from": rule.effective_from.isoformat(),
        "effective_to": rule.effective_to.isoformat(),
        "limit_pct": str(rule.limit_pct),
        "known_at": rule.known_at.isoformat() if rule.known_at else None,
        "trading_status": rule.trading_status,
        "source": rule.source,
        "evidence_id": rule.evidence_id,
        "evidence_sha256": rule.evidence_sha256,
    }


def _rule_from_dict(payload: dict[str, Any]) -> TradingRule:
    return TradingRule(
        code=str(payload["code"]),
        settlement=str(payload["settlement"]),
        effective_from=date.fromisoformat(str(payload["effective_from"])),
        effective_to=date.fromisoformat(str(payload["effective_to"])),
        limit_pct=Decimal(str(payload["limit_pct"])),
        known_at=(
            date.fromisoformat(str(payload["known_at"]))
            if payload.get("known_at")
            else None
        ),
        trading_status=str(payload.get("trading_status", "ACTIVE")),
        source=str(payload.get("source", "")),
        evidence_id=str(payload.get("evidence_id", "")),
        evidence_sha256=(
            str(payload["evidence_sha256"])
            if payload.get("evidence_sha256")
            else None
        ),
    )


def _action_to_dict(action: CorporateAction) -> dict[str, Any]:
    return {
        "code": action.code,
        "effective_date": action.effective_date.isoformat(),
        "known_at": action.known_at.isoformat(),
        "action_type": action.action_type,
        "cash_per_share": str(action.cash_per_share),
        "split_ratio": str(action.split_ratio),
    }


def _action_from_dict(payload: dict[str, Any]) -> CorporateAction:
    return CorporateAction(
        code=str(payload["code"]),
        effective_date=date.fromisoformat(str(payload["effective_date"])),
        known_at=date.fromisoformat(str(payload["known_at"])),
        action_type=str(payload["action_type"]),
        cash_per_share=Decimal(str(payload.get("cash_per_share", "0"))),
        split_ratio=Decimal(str(payload.get("split_ratio", "1"))),
    )


def save_bundle(root: Path, bundle: Phase1Bundle) -> dict[str, Path]:
    report = validate_bundle(bundle)
    if not report.backtest_allowed:
        raise DataQualityError("Bundle failed quality validation before save")
    meta = root / "meta"
    raw = root / "raw" / "etf"
    meta.mkdir(parents=True, exist_ok=True)
    raw.mkdir(parents=True, exist_ok=True)
    master_path = meta / "etf_master.ndjson"
    rules_path = meta / "trading_rules.ndjson"
    actions_path = meta / "corporate_actions.ndjson"
    manifest_path = meta / "data_manifest.json"
    save_master_records(master_path, bundle.master)
    write_ndjson_atomic(
        rules_path,
        (
            _rule_to_dict(rule)
            for rule in sorted(
                bundle.dataset.trading_rules,
                key=lambda item: (item.code, item.effective_from),
            )
        ),
    )
    write_ndjson_atomic(
        actions_path,
        (
            _action_to_dict(action)
            for action in sorted(
                bundle.dataset.corporate_actions,
                key=lambda item: (item.effective_date, item.code),
            )
        ),
    )
    for code in bundle.dataset.codes:
        rows = [
            RawDailyRecord(
                code=code,
                name=bundle.master[code].name_at(bar.trading_date),
                trading_date=bar.trading_date,
                raw_open=bar.raw_open,
                raw_high=bar.raw_high,
                raw_low=bar.raw_low,
                raw_close=bar.raw_close,
                volume=bar.volume,
                amount=bar.amount,
                provider=bundle.manifest_data.source_name,
                adjustment="none",
                volume_unit="shares",
                amount_currency="CNY",
            )
            for (session, bar_code), bar in bundle.dataset.bars.items()
            if bar_code == code
        ]
        save_raw_daily(raw / f"{code}.ndjson", rows)
    write_json_atomic(manifest_path, bundle.manifest_data.to_dict())
    return {
        "master": master_path,
        "trading_rules": rules_path,
        "corporate_actions": actions_path,
        "manifest": manifest_path,
        "raw": raw,
    }


def load_bundle(root: Path) -> Phase1Bundle:
    meta = root / "meta"
    raw_dir = root / "raw" / "etf"
    master = load_master_records(meta / "etf_master.ndjson")
    rules = tuple(
        _rule_from_dict(row) for row in read_ndjson(meta / "trading_rules.ndjson")
    )
    actions = tuple(
        _action_from_dict(row)
        for row in read_ndjson(meta / "corporate_actions.ndjson")
    )
    records = []
    for path in sorted(raw_dir.glob("*.ndjson")):
        from backend.data_pipeline.storage import load_raw_daily

        records.extend(load_raw_daily(path))
    manifest_payload = read_json(meta / "data_manifest.json")
    manifest = DataManifest(
        source_kind=str(manifest_payload["source_kind"]),
        source_name=str(manifest_payload["source_name"]),
        data_version=str(manifest_payload["data_version"]),
        coverage=CoverageFlags(**manifest_payload["coverage"]),
        notes=tuple(manifest_payload.get("notes", [])),
        real_market_evidence_allowed=bool(
            manifest_payload.get("real_market_evidence_allowed", False)
        ),
    )
    dataset = build_dataset_from_raw(
        records,
        master,
        rules,
        actions,
        source_kind=manifest.source_kind,
        metadata={"loaded_from": str(root), "manifest_data_version": manifest.data_version},
        allow_incomplete_master=(
            manifest.source_kind == "REAL_PROVIDER_PROVISIONAL"
        ),
    )
    actual_version = bundle_fingerprint(dataset, master.values())
    if actual_version != manifest.data_version:
        raise DataQualityError(
            "Stored Phase 1 bundle hash does not match data_manifest.json"
        )
    bundle = Phase1Bundle(dataset=dataset, master=master, manifest_data=manifest)
    bundle.validate()
    return bundle

