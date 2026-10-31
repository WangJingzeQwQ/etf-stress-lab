from __future__ import annotations

from datetime import date
import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from backend.data_pipeline.fetch_master import load_master_records
from backend.data_pipeline.storage import (
    canonical_file_hash,
    load_raw_daily,
    write_json_atomic,
)


def audit_code_coverage(
    *,
    code: str,
    list_date: date,
    delist_date: date | None,
    requested_start: date,
    requested_end: date,
    path: Path,
    gap_warning_days: int = 30,
    listing_is_provisional: bool = False,
) -> dict[str, Any]:
    if not path.exists():
        return {
            "code": code,
            "status": "MISSING",
            "file": str(path),
            "errors": ["raw file missing"],
            "warnings": [],
        }
    records = load_raw_daily(path)
    errors: list[str] = []
    warnings: list[str] = []
    if not records:
        errors.append("raw file is empty")
    dates = sorted({record.trading_date for record in records})
    duplicates = len(records) - len(dates)
    if duplicates:
        errors.append(f"{duplicates} duplicate trading dates")
    before_listing = sum(1 for value in dates if value < list_date)
    after_delist = (
        sum(1 for value in dates if value > delist_date)
        if delist_date is not None
        else 0
    )
    if before_listing:
        message = f"{before_listing} rows precede master list_date"
        if listing_is_provisional:
            warnings.append(message + " (provisional lower-bound date)")
        else:
            errors.append(message)
    if after_delist:
        errors.append(f"{after_delist} rows follow inferred delist_date")
    gaps = []
    maximum_gap = 0
    for previous, current in zip(dates, dates[1:]):
        gap = (current - previous).days
        maximum_gap = max(maximum_gap, gap)
        if gap > gap_warning_days:
            gaps.append(
                {
                    "from": previous.isoformat(),
                    "to": current.isoformat(),
                    "calendar_days": gap,
                }
            )
    if gaps:
        warnings.append(f"{len(gaps)} gaps exceed {gap_warning_days} calendar days")
    first_date = dates[0] if dates else None
    last_date = dates[-1] if dates else None
    if records and first_date is not None and first_date > requested_start:
        if first_date > list_date:
            warnings.append("first quote is later than inferred list_date")
    if delist_date is None and last_date is not None:
        if (requested_end - last_date).days > 10:
            warnings.append("current ETF has no recent quote near requested end")
    status = "PASS" if not errors and not gaps else "REVIEW"
    return {
        "code": code,
        "status": status,
        "file": str(path),
        "sha256": canonical_file_hash(path),
        "rows": len(records),
        "first_date": first_date.isoformat() if first_date else None,
        "last_date": last_date.isoformat() if last_date else None,
        "list_date": list_date.isoformat(),
        "delist_date": delist_date.isoformat() if delist_date else None,
        "before_listing_rows": before_listing,
        "after_delist_rows": after_delist,
        "maximum_gap_days": maximum_gap,
        "long_gaps": gaps,
        "errors": errors,
        "warnings": warnings,
    }


def audit_bundle_coverage(
    *,
    master_path: Path,
    raw_root: Path,
    requested_start: date,
    requested_end: date,
    output_path: Path,
) -> dict[str, Any]:
    master = load_master_records(master_path)
    details = [
        audit_code_coverage(
            code=code,
            list_date=record.list_date,
            delist_date=record.delist_date,
            requested_start=requested_start,
            requested_end=requested_end,
            path=raw_root / f"{code}.ndjson",
            listing_is_provisional=not record.history_complete,
        )
        for code, record in sorted(master.items())
    ]
    errors = [item for item in details if item["status"] == "MISSING" or item.get("errors")]
    reviews = [item for item in details if item.get("long_gaps")]
    covered = [item for item in details if item["status"] != "MISSING"]
    report = {
        "status": "COMPLETE" if not errors and len(covered) == len(master) else "PARTIAL",
        "master_path": str(master_path),
        "master_sha256": canonical_file_hash(master_path),
        "raw_root": str(raw_root),
        "requested_start": requested_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "master_codes": len(master),
        "files_present": len(covered),
        "missing_files": len(master) - len(covered),
        "codes_with_errors": len(errors),
        "codes_requiring_review": len(reviews),
        "details": details,
        "formal_research_allowed": False,
        "note": (
            "Coverage audit for provisional membership. It does not validate "
            "official listing/delisting or code identity."
        ),
    }
    write_json_atomic(output_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit provisional raw data coverage")
    parser.add_argument("--config", default="config.m1_2.yaml")
    args = parser.parse_args(argv)
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    data = config["data"]
    pipeline = config["data_pipeline"]
    report = audit_bundle_coverage(
        master_path=Path(pipeline["master_file"]),
        raw_root=Path(pipeline["raw_directory"]),
        requested_start=date.fromisoformat(str(data["start_date"])),
        requested_end=date.fromisoformat(str(data["end_date"])),
        output_path=Path("reports/m1_2/raw_coverage_audit.json"),
    )
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "master_codes",
                    "files_present",
                    "missing_files",
                    "codes_with_errors",
                    "codes_requiring_review",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

