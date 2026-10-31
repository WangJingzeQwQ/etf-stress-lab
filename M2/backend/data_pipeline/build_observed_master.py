from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from backend.data_pipeline.contracts import (
    MasterRecord,
    NameHistoryPoint,
    StatusHistoryPoint,
)
from backend.data_pipeline.fetch_master import load_master_records, save_master_records
from backend.data_pipeline.storage import read_json, write_json_atomic


def build_observed_master(
    *,
    master_path: Path,
    coverage_path: Path,
    output_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    coverage = read_json(coverage_path)
    if (
        coverage.get("status") != "COMPLETE"
        or coverage.get("missing_files") != 0
        or coverage.get("codes_with_errors") != 0
    ):
        raise ValueError("Raw coverage must be COMPLETE with zero hard errors")
    details = {item["code"]: item for item in coverage["details"]}
    master = load_master_records(master_path)
    reconciled: dict[str, MasterRecord] = {}
    for code, record in sorted(master.items()):
        detail = details.get(code)
        if detail is None or not detail.get("first_date") or not detail.get("last_date"):
            raise ValueError(f"Missing valid raw span for {code}")
        first_quote = date.fromisoformat(detail["first_date"])
        last_quote = date.fromisoformat(detail["last_date"])
        list_date = min(record.list_date, first_quote)
        first_name = min(
            record.name_history,
            key=lambda point: (point.effective_from, point.known_at),
        )
        name_history = (
            NameHistoryPoint(
                name=first_name.name,
                effective_from=list_date,
                known_at=list_date,
            ),
            *record.name_history[1:],
        )
        if record.delist_date is None:
            delist_date = None
            statuses = (
                StatusHistoryPoint(
                    status="ACTIVE",
                    effective_from=list_date,
                    known_at=list_date,
                ),
            )
        else:
            delist_date = max(record.delist_date, last_quote)
            statuses = (
                StatusHistoryPoint(
                    status="ACTIVE",
                    effective_from=list_date,
                    known_at=list_date,
                ),
                StatusHistoryPoint(
                    status="DELISTED",
                    effective_from=delist_date + timedelta(days=1),
                    known_at=delist_date,
                ),
            )
        reconciled[code] = replace(
            record,
            list_date=list_date,
            delist_date=delist_date,
            name_history=name_history,
            status_history=statuses,
            history_complete=True,
            source="EXCHANGE_SNAPSHOT_RAW_RECONCILED",
        )
        reconciled[code].validate()
    save_master_records(output_path, reconciled)
    report = {
        "status": "OBSERVED_MASTER_READY",
        "records": len(reconciled),
        "complete_records": sum(record.history_complete for record in reconciled.values()),
        "current_records": sum(
            record.delist_date is None for record in reconciled.values()
        ),
        "delisted_records": sum(
            record.delist_date is not None for record in reconciled.values()
        ),
        "date_precision": {
            "list_date": "earliest valid raw quote or first exchange observation",
            "delist_date": "last valid raw quote or last exchange observation",
            "name_changes": "exchange snapshot granularity",
        },
        "official_listing_dates_complete": False,
        "official_delisting_evidence_complete": False,
        "master_output": str(output_path),
        "formal_research_allowed": False,
    }
    write_json_atomic(report_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build observed-complete ETF master")
    parser.add_argument("--config", default="config.m1_2.yaml")
    args = parser.parse_args(argv)
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    pipeline = config["data_pipeline"]
    report = build_observed_master(
        master_path=Path(pipeline["master_file"]),
        coverage_path=Path("reports/m1_2/raw_coverage_audit.json"),
        output_path=Path("data/m1_2/meta/etf_master.observed_complete.ndjson"),
        report_path=Path("reports/m1_2/observed_master_report.json"),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

