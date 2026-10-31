from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import argparse
import json
from pathlib import Path
from typing import Callable

import yaml

from backend.data_pipeline.fetch_daily import IncrementalEtfDownloader
from backend.data_pipeline.fetch_master import fetch_current_snapshot
from backend.data_pipeline.provider import (
    AkshareETFProvider,
    AkshareSinaETFProvider,
    ETFDataProvider,
)
from backend.data_pipeline.storage import (
    canonical_file_hash,
    load_raw_daily,
    write_json_atomic,
)


@dataclass(frozen=True)
class SmallBatchConfig:
    codes: tuple[str, ...]
    start_date: date
    end_date: date
    output_root: Path
    report_path: Path
    request_interval_seconds: float = 3.0
    max_retries: int = 3
    backoff_seconds: float = 1.0
    provider: str = "akshare_sina"

    @classmethod
    def from_mapping(cls, payload: dict) -> "SmallBatchConfig":
        config = cls(
            codes=tuple(str(code) for code in payload["codes"]),
            start_date=date.fromisoformat(str(payload["start_date"])),
            end_date=date.fromisoformat(str(payload["end_date"])),
            output_root=Path(str(payload["output_root"])),
            report_path=Path(str(payload["report_path"])),
            request_interval_seconds=float(payload["request_interval_seconds"]),
            max_retries=int(payload["max_retries"]),
            backoff_seconds=float(payload["backoff_seconds"]),
            provider=str(payload.get("provider", "akshare_sina")),
        )
        if not config.codes:
            raise ValueError("M1.1 requires at least one code")
        if config.request_interval_seconds < 3.0:
            raise ValueError("M1.1 request interval must be at least 3 seconds")
        if config.start_date > config.end_date:
            raise ValueError("M1.1 start date is after end date")
        if config.provider not in {"akshare_sina", "akshare_eastmoney"}:
            raise ValueError(f"Unsupported M1.1 provider: {config.provider}")
        return config


def run_small_batch(
    config: SmallBatchConfig,
    *,
    provider: ETFDataProvider | None = None,
    snapshot_fetcher: Callable | None = None,
) -> dict:
    snapshot = fetch_current_snapshot(config.end_date, fetch_func=snapshot_fetcher)
    names = {row.code: row.name for row in snapshot}
    missing_names = [code for code in config.codes if code not in names]
    if missing_names:
        raise ValueError(f"Current snapshot missing selected codes: {missing_names}")
    selected_names = {code: names[code] for code in config.codes}
    if provider is not None:
        selected_provider = provider
    elif config.provider == "akshare_sina":
        selected_provider = AkshareSinaETFProvider(
            request_interval_seconds=config.request_interval_seconds,
            max_retries=config.max_retries,
            backoff_seconds=config.backoff_seconds,
        )
    else:
        selected_provider = AkshareETFProvider(
            request_interval_seconds=config.request_interval_seconds,
            max_retries=config.max_retries,
            backoff_seconds=config.backoff_seconds,
        )
    downloader = IncrementalEtfDownloader(selected_provider, config.output_root)
    summary = downloader.download(
        list(config.codes),
        selected_names,
        config.start_date,
        config.end_date,
    )
    file_details = []
    for code in config.codes:
        path = config.output_root / "raw" / "etf" / f"{code}.ndjson"
        if not path.exists():
            file_details.append(
                {
                    "code": code,
                    "name": selected_names[code],
                    "status": "missing",
                }
            )
            continue
        records = load_raw_daily(path)
        file_details.append(
            {
                "code": code,
                "name": selected_names[code],
                "status": "downloaded",
                "rows": len(records),
                "first_date": min((item.trading_date for item in records), default=None).isoformat()
                if records
                else None,
                "last_date": max((item.trading_date for item in records), default=None).isoformat()
                if records
                else None,
                "sha256": canonical_file_hash(path),
                "adjustment": sorted({item.adjustment for item in records}),
                "provider": sorted({item.provider for item in records}),
                "volume_unit": sorted({item.volume_unit for item in records}),
                "amount_currency": sorted({item.amount_currency for item in records}),
            }
        )
    report = {
        "milestone": "M1.1",
        "status": "PASS" if summary.success else "PARTIAL",
        "source_kind": "REAL_PROVIDER_PROVISIONAL",
        "provider": selected_provider.provider_name,
        "configured_provider": config.provider,
        "requested_start": config.start_date.isoformat(),
        "requested_end": config.end_date.isoformat(),
        "request_interval_seconds": config.request_interval_seconds,
        "codes": [
            {"code": code, "current_name": selected_names[code]}
            for code in config.codes
        ],
        "download_summary": summary.to_dict(),
        "files": file_details,
        "formal_research_allowed": False,
        "real_market_evidence_allowed": False,
        "missing_for_formal_bundle": [
            "audited historical ETF master including delisted and renamed funds",
            "historical trading-status intervals",
            "code-level T+0/T+1 and price-limit rule history",
            "corporate actions with effective_date and known_at",
            "broker fee evidence with effective dates",
        ],
        "warning": (
            "M1.1 is a real raw-data ingestion spike only. Current names and "
            "available quote history do not establish a survivorship-free "
            "historical universe."
        ),
    }
    write_json_atomic(config.report_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the M1.1 real-data small batch")
    parser.add_argument("--config", default="config.m1_1.yaml")
    args = parser.parse_args(argv)
    payload = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    config = SmallBatchConfig.from_mapping(payload["m1_1"])
    report = run_small_batch(config)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

