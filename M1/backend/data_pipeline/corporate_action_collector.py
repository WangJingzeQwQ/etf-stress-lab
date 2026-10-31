from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import argparse
import json
from pathlib import Path
import time
from typing import Any, Callable

import yaml

from backend.data_pipeline.fetch_master import load_master_records
from backend.data_pipeline.provider import RequestPacer
from backend.data_pipeline.storage import (
    read_json,
    write_json_atomic,
    write_ndjson_atomic,
)


PAGE_SIZE = 100


@dataclass(frozen=True)
class CorporateActionConfig:
    start_year: int
    end_year: int
    end_date: date | None
    master_path: Path
    output_path: Path
    cache_root: Path
    report_path: Path
    request_interval_seconds: float = 3.0
    max_retries: int = 3
    backoff_seconds: float = 1.0

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "CorporateActionConfig":
        config = cls(
            start_year=int(payload["start_year"]),
            end_year=int(payload["end_year"]),
            end_date=(
                date.fromisoformat(str(payload["end_date"]))
                if payload.get("end_date")
                else None
            ),
            master_path=Path(str(payload["master_path"])),
            output_path=Path(str(payload["output_path"])),
            cache_root=Path(str(payload["cache_root"])),
            report_path=Path(str(payload["report_path"])),
            request_interval_seconds=float(payload["request_interval_seconds"]),
            max_retries=int(payload["max_retries"]),
            backoff_seconds=float(payload["backoff_seconds"]),
        )
        if config.start_year > config.end_year:
            raise ValueError("Corporate action start_year is after end_year")
        if config.request_interval_seconds < 3.0:
            raise ValueError("Corporate action request interval must be >= 3 seconds")
        return config


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if hasattr(value, "date"):
        value = value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None
    return date.fromisoformat(text[:10])


class CorporateActionCollector:
    def __init__(
        self,
        config: CorporateActionConfig,
        *,
        fetch_dividend: Callable[..., Any] | None = None,
        fetch_split: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.fetch_dividend = fetch_dividend
        self.fetch_split = fetch_split
        self.pacer = RequestPacer(
            config.request_interval_seconds,
            sleeper=sleeper,
        )
        self._sleep = sleeper
        self.request_count = 0
        self.cache_hits = 0
        self.failures: list[dict[str, str]] = []
        self.warnings: list[dict[str, str]] = []

    def _load_fetchers(self) -> tuple[Callable[..., Any], Callable[..., Any]]:
        if self.fetch_dividend is not None and self.fetch_split is not None:
            return self.fetch_dividend, self.fetch_split
        import akshare as ak  # type: ignore[import-not-found]

        return (
            self.fetch_dividend or ak.fund_fh_em,
            self.fetch_split or ak.fund_cf_em,
        )

    def _call_with_retry(self, label: str, call: Callable[[], Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries):
            self.pacer.before_request()
            self.request_count += 1
            try:
                return call()
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.config.max_retries:
                    self._sleep(self.config.backoff_seconds * (2**attempt))
        raise RuntimeError(f"{label} failed after retries: {last_error}") from last_error

    def _fetch_pages(
        self,
        *,
        kind: str,
        year: int,
        fetch_func: Callable[..., Any],
    ) -> list[dict[str, Any]]:
        cache_dir = self.config.cache_root / kind / str(year)
        cache_dir.mkdir(parents=True, exist_ok=True)
        page = 1
        rows: list[dict[str, Any]] = []
        previous_page_size = PAGE_SIZE
        while True:
            cache_path = cache_dir / f"page_{page:03d}.json"
            cached = read_json(cache_path)
            if cached is None:
                try:
                    frame = self._call_with_retry(
                        f"{kind} {year} page {page}",
                        lambda page=page: fetch_func(
                            year=str(year),
                            typ="",
                            page=page,
                        ),
                    )
                except Exception as exc:
                    if (
                        page > 1
                        and previous_page_size == PAGE_SIZE
                        and isinstance(exc.__cause__, SyntaxError)
                    ):
                        write_json_atomic(
                            cache_path,
                            {
                                "kind": kind,
                                "year": year,
                                "page": page,
                                "rows": [],
                            },
                        )
                        self.warnings.append(
                            {
                                "kind": kind,
                                "year": str(year),
                                "page": str(page),
                                "warning": (
                                    "empty page after a full page; treated as "
                                    "end of pagination"
                                ),
                            }
                        )
                        break
                    self.failures.append(
                        {
                            "kind": kind,
                            "year": str(year),
                            "page": str(page),
                            "error": str(exc),
                        }
                    )
                    raise
                page_rows = frame.to_dict("records") if frame is not None else []
                cached = {
                    "kind": kind,
                    "year": year,
                    "page": page,
                    "rows": [
                        {
                            key: (
                                value.isoformat()
                                if hasattr(value, "isoformat")
                                else value
                            )
                            for key, value in row.items()
                        }
                        for row in page_rows
                    ],
                }
                write_json_atomic(cache_path, cached)
            else:
                self.cache_hits += 1
            rows.extend(cached["rows"])
            previous_page_size = len(cached["rows"])
            if len(cached["rows"]) < PAGE_SIZE:
                break
            page += 1
        return rows

    def collect(self) -> dict[str, Any]:
        master = load_master_records(self.config.master_path)
        wanted_codes = set(master)
        fetch_dividend, fetch_split = self._load_fetchers()
        events: dict[tuple[str, date, str], dict[str, Any]] = {}
        dividend_raw_count = 0
        split_raw_count = 0
        for year in range(self.config.start_year, self.config.end_year + 1):
            dividends = self._fetch_pages(
                kind="dividend",
                year=year,
                fetch_func=fetch_dividend,
            )
            split_rows = self._fetch_pages(
                kind="split",
                year=year,
                fetch_func=fetch_split,
            )
            dividend_raw_count += len(dividends)
            split_raw_count += len(split_rows)
            for row in dividends:
                code = str(row.get("基金代码", "")).strip().zfill(6)
                if code not in wanted_codes:
                    continue
                effective = _as_date(row.get("除息日期"))
                cash = Decimal(str(row.get("分红", "0")))
                if (
                    effective is None
                    or cash <= 0
                    or (
                        self.config.end_date is not None
                        and effective > self.config.end_date
                    )
                ):
                    continue
                key = (code, effective, "cash_dividend")
                events[key] = {
                    "code": code,
                    "effective_date": effective.isoformat(),
                    "known_at": effective.isoformat(),
                    "action_type": "cash_dividend",
                    "cash_per_share": str(cash),
                    "split_ratio": "1",
                    "source": "eastmoney_fund_dividend",
                    "known_at_quality": "effective_date_fallback",
                }
            for row in split_rows:
                code = str(row.get("基金代码", "")).strip().zfill(6)
                if code not in wanted_codes:
                    continue
                effective = _as_date(row.get("拆分折算日"))
                ratio = Decimal(str(row.get("拆分折算", "0")))
                if (
                    effective is None
                    or ratio <= 0
                    or (
                        self.config.end_date is not None
                        and effective > self.config.end_date
                    )
                ):
                    continue
                key = (code, effective, "split")
                events[key] = {
                    "code": code,
                    "effective_date": effective.isoformat(),
                    "known_at": effective.isoformat(),
                    "action_type": "split",
                    "cash_per_share": "0",
                    "split_ratio": str(ratio),
                    "source": "eastmoney_fund_split",
                    "known_at_quality": "effective_date_fallback",
                }
        ordered = [
            events[key]
            for key in sorted(events, key=lambda item: (item[1], item[0], item[2]))
        ]
        outside_window = [
            event
            for event in ordered
            if date.fromisoformat(event["effective_date"]) < master[event["code"]].list_date
            or (
                master[event["code"]].delist_date is not None
                and date.fromisoformat(event["effective_date"])
                > master[event["code"]].delist_date
            )
        ]
        write_ndjson_atomic(self.config.output_path, ordered)
        report = {
            "status": "PROVISIONAL_ACTIONS_READY",
            "years": [self.config.start_year, self.config.end_year],
            "master_codes": len(master),
            "dividend_source_rows": dividend_raw_count,
            "split_source_rows": split_raw_count,
            "matched_events": len(ordered),
            "cash_dividends": sum(
                event["action_type"] == "cash_dividend" for event in ordered
            ),
            "splits": sum(event["action_type"] == "split" for event in ordered),
            "outside_provisional_master_window": len(outside_window),
            "network_requests": self.request_count,
            "cache_hits": self.cache_hits,
            "failures": self.failures,
            "warnings": self.warnings,
            "known_at_policy": (
                "Conservative fallback to effective_date because announcement "
                "known_at is unavailable from this source."
            ),
            "formal_research_allowed": False,
            "output": str(self.config.output_path),
        }
        write_json_atomic(self.config.report_path, report)
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect provisional ETF actions")
    parser.add_argument("--config", default="config.m1_2.yaml")
    args = parser.parse_args(argv)
    payload = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    config = CorporateActionConfig.from_mapping(payload["corporate_actions"])
    report = CorporateActionCollector(config).collect()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

