from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from backend.data_pipeline.contracts import DataQualityError, RawDailyRecord
from backend.data_pipeline.fetch_master import load_master_records
from backend.data_pipeline.provider import (
    AkshareETFProvider,
    AkshareSinaETFProvider,
    BaoStockETFProvider,
    ETFDataProvider,
)
from backend.data_pipeline.storage import (
    load_raw_daily,
    read_json,
    save_raw_daily,
    write_json_atomic,
)


@dataclass
class DownloadSummary:
    completed_codes: list[str] = field(default_factory=list)
    skipped_up_to_date_codes: list[str] = field(default_factory=list)
    failed_codes: dict[str, str] = field(default_factory=dict)
    record_counts: dict[str, int] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return not self.failed_codes

    def to_dict(self) -> dict[str, Any]:
        return {
            "completed_codes": self.completed_codes,
            "skipped_up_to_date_codes": self.skipped_up_to_date_codes,
            "failed_codes": self.failed_codes,
            "record_counts": self.record_counts,
            "success": self.success,
        }


class IncrementalEtfDownloader:
    def __init__(
        self,
        provider: ETFDataProvider,
        root: Path,
        *,
        state_path: Path | None = None,
        failure_queue_path: Path | None = None,
        download_log_path: Path | None = None,
        progress_path: Path | None = None,
    ) -> None:
        self.provider = provider
        self.root = root
        self.state_path = state_path or root / "state" / "download_state.json"
        self.failure_queue_path = (
            failure_queue_path or root / "state" / "failure_queue.json"
        )
        self.download_log_path = (
            download_log_path or root / "download_log.json"
        )
        self.progress_path = (
            progress_path or root / "state" / "download_progress.json"
        )

    def _daily_path(self, code: str) -> Path:
        return self.root / "raw" / "etf" / f"{code}.ndjson"

    def download(
        self,
        codes: list[str],
        names: dict[str, str],
        start_date: date,
        end_date: date,
        effective_end_dates: dict[str, date] | None = None,
    ) -> DownloadSummary:
        if start_date > end_date:
            raise ValueError("start_date must not be after end_date")
        state = read_json(self.state_path, default={})
        if not isinstance(state, dict):
            raise DataQualityError("Download state must be a JSON object")
        failure_queue = read_json(self.failure_queue_path, default={})
        if not isinstance(failure_queue, dict):
            failure_queue = {}
        summary = DownloadSummary()
        events: list[dict[str, Any]] = []
        total = len(set(codes))
        processed = 0

        def write_progress(status: str, current_code: str | None = None) -> None:
            write_json_atomic(
                self.progress_path,
                {
                    "status": status,
                    "provider": self.provider.provider_name,
                    "requested_start": start_date.isoformat(),
                    "requested_end": end_date.isoformat(),
                    "total_codes": total,
                    "processed_codes": processed,
                    "completed_codes": len(summary.completed_codes),
                    "skipped_codes": len(summary.skipped_up_to_date_codes),
                    "failed_codes": len(summary.failed_codes),
                    "current_code": current_code,
                },
            )

        for code in sorted(set(codes)):
            write_progress("RUNNING", code)
            code_end_date = (effective_end_dates or {}).get(code, end_date)
            if code_end_date > end_date or code_end_date < start_date:
                code_end_date = end_date
            name = names.get(code)
            if not name:
                summary.failed_codes[code] = "missing_chinese_name"
                failure_queue[code] = {
                    "code": code,
                    "reason": "missing_chinese_name",
                    "requested_end": code_end_date.isoformat(),
                }
                processed += 1
                continue
            path = self._daily_path(code)
            existing = load_raw_daily(path)
            existing_by_date = {record.trading_date: record for record in existing}
            last_date = max(existing_by_date, default=None)
            first_date = min(existing_by_date, default=None)
            if (
                last_date is not None
                and first_date is not None
                and first_date <= start_date
                and last_date >= code_end_date
            ):
                summary.skipped_up_to_date_codes.append(code)
                failure_queue.pop(code, None)
                events.append(
                    {
                        "code": code,
                        "status": "skipped_up_to_date",
                        "last_date": last_date.isoformat(),
                    }
                )
                processed += 1
                continue
            request_start = (
                max(start_date, last_date + timedelta(days=1))
                if last_date is not None and first_date is not None and first_date <= start_date
                else start_date
            )
            try:
                new_records = self.provider.fetch_daily(
                    code,
                    name,
                    request_start,
                    code_end_date,
                )
                for record in new_records:
                    if record.code != code:
                        raise DataQualityError(
                            f"Provider returned code {record.code} for request {code}"
                        )
                    existing_record = existing_by_date.get(record.trading_date)
                    if existing_record is not None and existing_record.to_dict() != record.to_dict():
                        raise DataQualityError(
                            f"Conflicting resumed record for {code} on "
                            f"{record.trading_date}"
                        )
                    existing_by_date[record.trading_date] = record
                merged = sorted(
                    existing_by_date.values(), key=lambda item: item.trading_date
                )
                save_raw_daily(path, merged)
                final_date = max(existing_by_date, default=last_date)
                state[code] = {
                    "last_date": final_date.isoformat() if final_date else None,
                    "first_date": (
                        min(existing_by_date).isoformat() if existing_by_date else None
                    ),
                    "provider": self.provider.provider_name,
                    "record_count": len(merged),
                }
                summary.completed_codes.append(code)
                summary.record_counts[code] = len(new_records)
                failure_queue.pop(code, None)
                events.append(
                    {
                        "code": code,
                        "status": "completed",
                        "request_start": request_start.isoformat(),
                        "request_end": code_end_date.isoformat(),
                        "new_records": len(new_records),
                        "last_date": final_date.isoformat() if final_date else None,
                    }
                )
                processed += 1
            except Exception as exc:
                summary.failed_codes[code] = str(exc)
                failure_queue[code] = {
                    "code": code,
                    "reason": str(exc),
                    "request_start": request_start.isoformat(),
                    "requested_end": code_end_date.isoformat(),
                }
                events.append(
                    {
                        "code": code,
                        "status": "failed",
                        "reason": str(exc),
                    }
                )
                processed += 1

        write_progress("COMPLETED" if summary.success else "PARTIAL")
        write_json_atomic(self.state_path, state)
        write_json_atomic(self.failure_queue_path, failure_queue)
        write_json_atomic(
            self.download_log_path,
            {
                "provider": self.provider.provider_name,
                "requested_start": start_date.isoformat(),
                "requested_end": end_date.isoformat(),
                "events": events,
                "summary": summary.to_dict(),
            },
        )
        return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Incremental raw ETF downloader")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--master", default=None)
    parser.add_argument("--bundle-root", default=None)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--codes", nargs="*", default=None)
    parser.add_argument(
        "--provider",
        choices=("akshare_sina", "akshare_eastmoney", "baostock"),
        default=None,
    )
    args = parser.parse_args(argv)
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    data = config["data"]
    pipeline = config["data_pipeline"]
    master_path = Path(
        args.master
        or pipeline.get("master_file")
        or "data/meta/etf_master.ndjson"
    )
    root = Path(args.bundle_root or pipeline.get("bundle_root", "data"))
    master = load_master_records(master_path)
    codes = args.codes or sorted(master)
    names = {code: master[code].name_at(date.fromisoformat(args.end or data["end_date"])) for code in codes}
    effective_end_dates = {
        code: min(
            date.fromisoformat(args.end or str(data["end_date"])),
            master[code].delist_date,
        )
        for code in codes
        if master[code].delist_date is not None
    }
    provider_name = str(args.provider or pipeline.get("provider", "akshare_sina"))
    if provider_name == "akshare_sina":
        provider = AkshareSinaETFProvider(
            request_interval_seconds=float(pipeline["request_interval_seconds"]),
            max_retries=int(pipeline["max_retries"]),
            backoff_seconds=float(pipeline["backoff_seconds"]),
        )
    elif provider_name == "akshare_eastmoney":
        provider = AkshareETFProvider(
            request_interval_seconds=float(pipeline["request_interval_seconds"]),
            max_retries=int(pipeline["max_retries"]),
            backoff_seconds=float(pipeline["backoff_seconds"]),
        )
    elif provider_name == "baostock":
        provider = BaoStockETFProvider(
            request_interval_seconds=float(pipeline["request_interval_seconds"]),
            max_retries=int(pipeline["max_retries"]),
            backoff_seconds=float(pipeline["backoff_seconds"]),
        )
    else:
        raise ValueError(f"Unsupported data provider: {provider_name}")
    downloader = IncrementalEtfDownloader(
        provider,
        root,
        state_path=Path(pipeline["download_state"]) if pipeline.get("download_state") else None,
        failure_queue_path=Path(pipeline["failure_queue"]) if pipeline.get("failure_queue") else None,
        progress_path=Path(pipeline["progress_path"]) if pipeline.get("progress_path") else None,
    )
    summary = downloader.download(
        codes,
        names,
        date.fromisoformat(args.start or str(data["start_date"])),
        date.fromisoformat(args.end or str(data["end_date"])),
        effective_end_dates=effective_end_dates,
    )
    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    return 0 if summary.success else 1


if __name__ == "__main__":
    raise SystemExit(main())

