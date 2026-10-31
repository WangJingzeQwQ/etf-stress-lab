from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from importlib.util import find_spec
import argparse
import json
from pathlib import Path
from typing import Any, Callable

from backend.data_pipeline.contracts import MasterRecord
from backend.data_pipeline.storage import (
    canonical_file_hash,
    read_ndjson,
    write_json_atomic,
    write_ndjson_atomic,
)


class MasterImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class CurrentEtfSnapshotRow:
    code: str
    name: str
    observed_at: date

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "name": self.name,
            "observed_at": self.observed_at.isoformat(),
        }


def load_master_records(path: Path) -> dict[str, MasterRecord]:
    records: dict[str, MasterRecord] = {}
    for row in read_ndjson(path):
        record = MasterRecord.from_dict(row)
        if record.code in records:
            raise MasterImportError(f"Duplicate master code: {record.code}")
        records[record.code] = record
    if not records:
        raise MasterImportError(f"No master records found in {path}")
    return records


def save_master_records(path: Path, records: dict[str, MasterRecord]) -> None:
    for code, record in records.items():
        if code != record.code:
            raise MasterImportError(f"Master key/code mismatch: {code}")
        record.validate()
    write_ndjson_atomic(
        path,
        (records[code].to_dict() for code in sorted(records)),
    )


def fetch_current_snapshot(
    observed_at: date,
    *,
    fetch_func: Callable[..., Any] | None = None,
) -> list[CurrentEtfSnapshotRow]:
    """Fetch current names only; never treat this as a historical master."""

    if fetch_func is None:
        if find_spec("akshare") is None:
            raise MasterImportError(
                "AKShare is unavailable. Current snapshot was not fetched."
            )
        import akshare as ak  # type: ignore[import-not-found]

        fetch_candidates = [ak.fund_etf_fund_daily_em, ak.fund_etf_spot_em]
    else:
        fetch_candidates = [fetch_func]
    rows: list[CurrentEtfSnapshotRow] = []
    last_error: Exception | None = None
    frame = None
    for candidate in fetch_candidates:
        try:
            frame = candidate()
            break
        except Exception as exc:
            last_error = exc
    if frame is None:
        raise MasterImportError(
            f"Current ETF snapshot endpoints failed: {last_error}"
        )
    for row in frame.to_dict("records"):
        code = str(
            row.get("代码")
            or row.get("基金代码")
            or row.get("code")
            or ""
        ).strip()
        name = str(
            row.get("名称")
            or row.get("基金简称")
            or row.get("name")
            or ""
        ).strip()
        if code and name:
            rows.append(
                CurrentEtfSnapshotRow(
                    code=code,
                    name=name,
                    observed_at=observed_at,
                )
            )
    if not rows:
        raise MasterImportError("Current ETF snapshot returned no code/name rows")
    return sorted(rows, key=lambda item: item.code)


def save_current_snapshot(path: Path, rows: list[CurrentEtfSnapshotRow]) -> None:
    write_ndjson_atomic(path, (row.to_dict() for row in rows))
    write_json_atomic(
        path.with_suffix(".manifest.json"),
        {
            "kind": "CURRENT_ETF_NAME_SNAPSHOT",
            "observed_at": rows[0].observed_at.isoformat() if rows else None,
            "rows": len(rows),
            "file_sha256": canonical_file_hash(path),
            "historical_master": False,
            "formal_research_allowed": False,
            "warning": (
                "Current names are not historical membership, rename or delisting "
                "evidence."
            ),
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate/import ETF master")
    parser.add_argument("--validate", default=None)
    parser.add_argument("--current-snapshot-output", default=None)
    parser.add_argument("--observed-at", default=date.today().isoformat())
    parser.add_argument(
        "--formal-source-confirmed",
        action="store_true",
        help="Confirm the imported file is a validated historical source.",
    )
    args = parser.parse_args(argv)
    if args.validate:
        records = load_master_records(Path(args.validate))
        incomplete = sorted(
            code for code, record in records.items() if not record.history_complete
        )
        print(
            json.dumps(
                {
                    "records": len(records),
                    "incomplete_history_codes": incomplete,
                    "sources": sorted({record.source for record in records.values()}),
                    "record_history_complete": not incomplete,
                    "formal_ready": bool(
                        args.formal_source_confirmed and not incomplete
                    ),
                    "formal_ready_note": (
                        "Formal readiness also requires complete raw data, delist, "
                        "corporate-action, trading-rule and cost coverage."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    if args.current_snapshot_output:
        rows = fetch_current_snapshot(date.fromisoformat(args.observed_at))
        save_current_snapshot(Path(args.current_snapshot_output), rows)
        print(
            json.dumps(
                {
                    "rows": len(rows),
                    "output": args.current_snapshot_output,
                    "warning": (
                        "Current spot names are not historical membership or "
                        "delisting evidence."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    if not args.validate and not args.current_snapshot_output:
        parser.error("Specify --validate or --current-snapshot-output")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

