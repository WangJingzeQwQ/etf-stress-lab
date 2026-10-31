from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
import argparse
import json
from pathlib import Path
import shutil
from typing import Any

import yaml

from backend.data_pipeline.contracts import RawDailyRecord
from backend.data_pipeline.fetch_master import load_master_records, save_master_records
from backend.data_pipeline.storage import (
    canonical_file_hash,
    load_raw_daily,
    save_raw_daily,
    write_json_atomic,
)


def _price_signature(record: RawDailyRecord) -> tuple:
    return (
        record.raw_open,
        record.raw_high,
        record.raw_low,
        record.raw_close,
        record.volume,
        record.amount,
    )


def find_spurious_duplicate_tail(
    records: list[RawDailyRecord],
    *,
    minimum_calendar_gap: int = 365,
) -> tuple[RawDailyRecord, RawDailyRecord] | None:
    if len(records) < 2:
        return None
    tail = records[-1]
    for previous in reversed(records[:-1]):
        if (tail.trading_date - previous.trading_date).days < minimum_calendar_gap:
            continue
        if _price_signature(previous) == _price_signature(tail):
            return previous, tail
    return None


def quarantine_duplicate_tail(
    *,
    code: str,
    raw_root: Path,
    quarantine_root: Path,
    report_path: Path,
) -> dict[str, Any]:
    raw_path = raw_root / f"{code}.ndjson"
    records = load_raw_daily(raw_path)
    finding = find_spurious_duplicate_tail(records)
    if finding is None:
        raise ValueError(f"No spurious duplicate tail found for {code}")
    original, tail = finding
    original_hash = canonical_file_hash(raw_path)
    quarantine_root.mkdir(parents=True, exist_ok=True)
    quarantine_path = quarantine_root / f"{code}.original.ndjson"
    if not quarantine_path.exists():
        shutil.copy2(raw_path, quarantine_path)
    cleaned = records[:-1]
    save_raw_daily(raw_path, cleaned)
    report = {
        "code": code,
        "action": "QUARANTINED_DUPLICATE_TAIL",
        "original_file": str(raw_path),
        "quarantine_copy": str(quarantine_path),
        "original_sha256": original_hash,
        "cleaned_sha256": canonical_file_hash(raw_path),
        "duplicated_from": original.trading_date.isoformat(),
        "removed_date": tail.trading_date.isoformat(),
        "gap_days": (tail.trading_date - original.trading_date).days,
        "matching_fields": [
            "raw_open",
            "raw_high",
            "raw_low",
            "raw_close",
            "volume",
            "amount",
        ],
    }
    write_json_atomic(report_path, report)
    return report


def reconcile_master_delist_from_raw(
    *,
    master_path: Path,
    raw_root: Path,
    code: str,
    output_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    master = load_master_records(master_path)
    record = master[code]
    records = load_raw_daily(raw_root / f"{code}.ndjson")
    if not records:
        raise ValueError(f"Cannot infer last quote for empty {code}")
    last_quote = max(record.trading_date for record in records)
    statuses = list(record.status_history)
    if record.delist_date is not None:
        for index, status in enumerate(statuses):
            if status.status == "DELISTED":
                statuses[index] = replace(
                    status,
                    effective_from=last_quote + timedelta(days=1),
                    known_at=last_quote,
                )
    corrected = replace(
        record,
        delist_date=last_quote,
        status_history=tuple(statuses),
    )
    corrected.validate()
    master[code] = corrected
    save_master_records(output_path, master)
    old_effectives = [
        status.effective_from.isoformat()
        for status in statuses
        if status.status == "DELISTED"
    ]
    report = {
        "code": code,
        "action": "INFERRED_DELIST_FROM_LAST_VALID_QUOTE",
        "old_delist_date": (
            record.delist_date.isoformat() if record.delist_date is not None else None
        ),
        "new_delist_date": last_quote.isoformat(),
        "delisted_status_effective_from": old_effectives,
        "master_output": str(output_path),
        "history_complete": False,
        "formal_research_allowed": False,
    }
    write_json_atomic(report_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quarantine known raw and identity anomalies")
    parser.add_argument("--config", default="config.m1_2.yaml")
    args = parser.parse_args(argv)
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    pipeline = config["data_pipeline"]
    raw_root = Path(pipeline["raw_directory"])
    master_path = Path(pipeline["master_file"])
    quarantine_root = Path("data/m1_2/raw/quarantine")
    quarantine_report = quarantine_duplicate_tail(
        code="159927",
        raw_root=raw_root,
        quarantine_root=quarantine_root,
        report_path=Path("reports/m1_2/159927_quarantine.json"),
    )
    reconciled_master = Path("data/m1_2/meta/etf_master.reconciled.ndjson")
    master_report = reconcile_master_delist_from_raw(
        master_path=master_path,
        raw_root=raw_root,
        code="159927",
        output_path=reconciled_master,
        report_path=Path("reports/m1_2/159927_master_reconciliation.json"),
    )
    print(
        json.dumps(
            {
                "quarantine": quarantine_report,
                "master_reconciliation": master_report,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

