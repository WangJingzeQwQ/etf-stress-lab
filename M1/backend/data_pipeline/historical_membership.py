from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import argparse
import calendar
import json
from pathlib import Path
import time
from typing import Any, Callable

import yaml

from backend.data_pipeline.contracts import (
    MasterRecord,
    NameHistoryPoint,
    StatusHistoryPoint,
)
from backend.data_pipeline.fetch_master import (
    CurrentEtfSnapshotRow,
    save_master_records,
)
from backend.data_pipeline.provider import RequestPacer
from backend.data_pipeline.storage import (
    canonical_file_hash,
    read_json,
    read_ndjson,
    write_json_atomic,
    write_ndjson_atomic,
)


@dataclass(frozen=True)
class MembershipConfig:
    start_date: date
    end_date: date
    cache_root: Path
    master_output: Path
    report_output: Path
    current_snapshot: Path
    request_interval_seconds: float = 3.0
    max_retries: int = 3
    backoff_seconds: float = 1.0

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "MembershipConfig":
        config = cls(
            start_date=date.fromisoformat(str(payload["start_date"])),
            end_date=date.fromisoformat(str(payload["end_date"])),
            cache_root=Path(str(payload["cache_root"])),
            master_output=Path(str(payload["master_output"])),
            report_output=Path(str(payload["report_output"])),
            current_snapshot=Path(str(payload["current_snapshot"])),
            request_interval_seconds=float(payload["request_interval_seconds"]),
            max_retries=int(payload["max_retries"]),
            backoff_seconds=float(payload["backoff_seconds"]),
        )
        if config.start_date > config.end_date:
            raise ValueError("Membership start date is after end date")
        if config.request_interval_seconds < 3.0:
            raise ValueError("Exchange request interval must be at least 3 seconds")
        return config


@dataclass
class CodeHistory:
    code: str
    first_seen: date
    last_seen: date
    names: dict[str, tuple[date, date]]
    markets: set[str]

    def observe(self, observed_at: date, name: str, market: str) -> None:
        if observed_at < self.first_seen:
            self.first_seen = observed_at
        if observed_at > self.last_seen:
            self.last_seen = observed_at
        first_name_seen, _ = self.names.get(name, (observed_at, observed_at))
        self.names[name] = (min(first_name_seen, observed_at), max(self.names.get(name, (observed_at, observed_at))[1], observed_at))
        self.markets.add(market)


def _last_weekday(year: int, month: int) -> date:
    day = calendar.monthrange(year, month)[1]
    cursor = date(year, month, day)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def _month_cutoffs(start: date, end: date) -> list[date]:
    results: list[date] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        candidate = _last_weekday(cursor.year, cursor.month)
        while candidate > end:
            candidate -= timedelta(days=1)
        if candidate >= start:
            results.append(candidate)
        cursor = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
    return results


def _half_year_ranges(start: date, end: date) -> list[tuple[date, date]]:
    ranges: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        first_half_end = date(cursor.year, 6, 30)
        second_half_end = date(cursor.year, 12, 31)
        if cursor <= first_half_end:
            block_end = min(first_half_end, end)
        else:
            block_end = min(second_half_end, end)
        ranges.append((cursor, block_end))
        cursor = block_end + timedelta(days=1)
    return ranges


class HistoricalMembershipCollector:
    def __init__(
        self,
        config: MembershipConfig,
        *,
        fetch_sse: Callable[..., Any] | None = None,
        fetch_szse: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.fetch_sse = fetch_sse
        self.fetch_szse = fetch_szse
        self._sleep = sleeper
        self.pacer = RequestPacer(
            config.request_interval_seconds,
            sleeper=sleeper,
        )
        self.histories: dict[str, CodeHistory] = {}
        self.failures: list[dict[str, str]] = []
        self.warnings: list[dict[str, str]] = []
        self.request_count = 0
        self.cache_hits = 0

    def _load_sse(self) -> Callable[..., Any]:
        if self.fetch_sse is not None:
            return self.fetch_sse
        from akshare import fund_etf_scale_sse  # type: ignore[import-not-found]

        return fund_etf_scale_sse

    def _load_szse(self) -> Callable[..., Any]:
        if self.fetch_szse is not None:
            return self.fetch_szse
        from akshare import fund_scale_daily_szse  # type: ignore[import-not-found]

        return fund_scale_daily_szse

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

    def _observe(self, code: str, name: str, observed_at: date, market: str) -> None:
        if code in self.histories:
            self.histories[code].observe(observed_at, name, market)
        else:
            self.histories[code] = CodeHistory(
                code=code,
                first_seen=observed_at,
                last_seen=observed_at,
                names={name: (observed_at, observed_at)},
                markets={market},
            )

    def collect(self) -> dict[str, Any]:
        sse_fetch = self._load_sse()
        szse_fetch = self._load_szse()
        sse_dir = self.config.cache_root / "sse_monthly"
        szse_dir = self.config.cache_root / "szse_half_year"
        sse_dir.mkdir(parents=True, exist_ok=True)
        szse_dir.mkdir(parents=True, exist_ok=True)

        for cutoff in _month_cutoffs(self.config.start_date, self.config.end_date):
            cache_path = sse_dir / f"{cutoff.strftime('%Y%m')}.json"
            cached = read_json(cache_path)
            if cached is None:
                latest_error: Exception | None = None
                rows = []
                # CNY and National Day holidays can exceed one week.
                for offset in range(21):
                    query_date = cutoff - timedelta(days=offset)
                    if query_date < self.config.start_date:
                        break
                    try:
                        frame = self._call_with_retry(
                            f"SSE {query_date.isoformat()}",
                            lambda query_date=query_date: sse_fetch(
                                date=query_date.strftime("%Y%m%d")
                            ),
                        )
                        rows = frame.to_dict("records")
                        if rows:
                            break
                    except Exception as exc:
                        latest_error = exc
                        self.warnings.append(
                            {
                                "source": "SSE",
                                "period": query_date.isoformat(),
                                "warning": f"fallback date unavailable: {exc}",
                            }
                        )
                if not rows:
                    raise RuntimeError(
                        f"No SSE ETF snapshot for month ending {cutoff}: "
                        f"{latest_error}"
                    )
                cached = {
                    "requested_cutoff": cutoff.isoformat(),
                    "query_date": query_date.isoformat(),
                    "rows": [
                        {
                            "code": str(row.get("基金代码", "")).strip().zfill(6),
                            "name": str(row.get("基金简称", "")).strip(),
                            "observed_at": (
                                row.get("统计日期").date()
                                if hasattr(row.get("统计日期"), "date")
                                else date.fromisoformat(
                                    str(row.get("统计日期") or query_date)[:10]
                                )
                            ).isoformat(),
                        }
                        for row in rows
                    ],
                }
                write_json_atomic(cache_path, cached)
            else:
                self.cache_hits += 1
            for row in cached["rows"]:
                code = str(row.get("code", "")).strip().zfill(6)
                name = str(row.get("name", "")).strip()
                observed = row.get("observed_at") or cached["query_date"]
                observed_date = date.fromisoformat(str(observed)[:10])
                if code and name and code != "000000":
                    self._observe(code, name, observed_date, "SSE")

        for range_start, range_end in _half_year_ranges(
            self.config.start_date, self.config.end_date
        ):
            cache_path = szse_dir / (
                f"{range_start.strftime('%Y%m%d')}_{range_end.strftime('%Y%m%d')}.json"
            )
            cached = read_json(cache_path)
            if cached is None:
                frame = self._call_with_retry(
                    f"SZSE {range_start}..{range_end}",
                    lambda range_start=range_start, range_end=range_end: szse_fetch(
                        start_date=range_start.strftime("%Y%m%d"),
                        end_date=range_end.strftime("%Y%m%d"),
                        symbol="ETF",
                    ),
                )
                if frame is None or frame.empty:
                    raise RuntimeError(
                        f"SZSE returned no ETF snapshot for "
                        f"{range_start}..{range_end}"
                    )
                summaries: dict[str, dict[str, Any]] = {}
                for row in frame.to_dict("records"):
                    code = str(row.get("基金代码", "")).strip().zfill(6)
                    name = str(row.get("基金简称", "")).strip()
                    observed = row.get("日期")
                    if observed is None or not code or not name:
                        continue
                    observed_date = (
                        observed.date()
                        if hasattr(observed, "date")
                        else date.fromisoformat(str(observed)[:10])
                    )
                    summary = summaries.setdefault(
                        code,
                        {
                            "first_seen": observed_date.isoformat(),
                            "last_seen": observed_date.isoformat(),
                            "names": {},
                        },
                    )
                    summary["first_seen"] = min(
                        summary["first_seen"], observed_date.isoformat()
                    )
                    summary["last_seen"] = max(
                        summary["last_seen"], observed_date.isoformat()
                    )
                    name_entry = summary["names"].setdefault(
                        name,
                        {
                            "first_seen": observed_date.isoformat(),
                            "last_seen": observed_date.isoformat(),
                        },
                    )
                    name_entry["first_seen"] = min(
                        name_entry["first_seen"], observed_date.isoformat()
                    )
                    name_entry["last_seen"] = max(
                        name_entry["last_seen"], observed_date.isoformat()
                    )
                cached = {
                    "range_start": range_start.isoformat(),
                    "range_end": range_end.isoformat(),
                    "codes": summaries,
                }
                write_json_atomic(cache_path, cached)
            else:
                self.cache_hits += 1
            for code, summary in cached["codes"].items():
                for name, name_entry in summary["names"].items():
                    self._observe(
                        code,
                        name,
                        date.fromisoformat(name_entry["first_seen"]),
                        "SZSE",
                    )
                history = self.histories[code]
                history.first_seen = min(
                    history.first_seen, date.fromisoformat(summary["first_seen"])
                )
                history.last_seen = max(
                    history.last_seen, date.fromisoformat(summary["last_seen"])
                )
        return self._materialize()

    def _materialize(self) -> dict[str, Any]:
        current_rows = read_ndjson(self.config.current_snapshot)
        auxiliary_snapshot_codes = {
            str(row["code"]).strip().zfill(6) for row in current_rows
        }
        latest_exchange_date = max(
            history.last_seen for history in self.histories.values()
        )
        # Membership is authoritative from exchange snapshots. The current
        # fund-list endpoint omits some money ETFs and must not define delisting.
        current_codes = {
            code
            for code, history in self.histories.items()
            if history.last_seen == latest_exchange_date
        }
        auxiliary_only_codes = sorted(auxiliary_snapshot_codes - set(self.histories))
        masters: dict[str, MasterRecord] = {}
        for code in sorted(self.histories):
            history = self.histories[code]
            name_points = tuple(
                NameHistoryPoint(name=name, effective_from=first_seen, known_at=first_seen)
                for name, (first_seen, _) in sorted(
                    history.names.items(),
                    key=lambda item: (item[1][0], item[0]),
                )
            )
            statuses = [
                StatusHistoryPoint(
                    status="ACTIVE",
                    effective_from=history.first_seen,
                    known_at=history.first_seen,
                )
            ]
            is_current = code in current_codes
            delist_date = None
            if not is_current:
                delist_date = history.last_seen
                statuses.append(
                    StatusHistoryPoint(
                        status="DELISTED",
                        effective_from=history.last_seen + timedelta(days=1),
                        known_at=history.last_seen,
                    )
                )
            masters[code] = MasterRecord(
                code=code,
                list_date=history.first_seen,
                delist_date=delist_date,
                name_history=name_points,
                status_history=tuple(statuses),
                history_complete=False,
                source="EXCHANGE_SNAPSHOT_INFERRED",
            )
        save_master_records(self.config.master_output, masters)
        master_sha256 = canonical_file_hash(self.config.master_output)
        report = {
            "status": "PROVISIONAL_MASTER_READY",
            "source_kind": "REAL_PROVIDER_PROVISIONAL",
            "start_date": self.config.start_date.isoformat(),
            "end_date": self.config.end_date.isoformat(),
            "unique_codes": len(masters),
            "current_codes": len(current_codes),
            "latest_exchange_date": latest_exchange_date.isoformat(),
            "auxiliary_snapshot_only_codes": auxiliary_only_codes,
            "inferred_delisted_codes": sum(
                1 for record in masters.values() if record.delist_date is not None
            ),
            "sse_months": len(_month_cutoffs(self.config.start_date, self.config.end_date)),
            "szse_ranges": len(_half_year_ranges(self.config.start_date, self.config.end_date)),
            "network_requests": self.request_count,
            "cache_hits": self.cache_hits,
            "run_mode": (
                "network_collection" if self.request_count else "cache_replay"
            ),
            "warnings": self.warnings,
            "failures": self.failures,
            "master_output": str(self.config.master_output),
            "master_sha256": master_sha256,
            "limitations": [
                "list_date is inferred from first monthly exchange observation",
                "delist_date is inferred from last observation and requires official verification",
                "historical name changes are observed at snapshot granularity",
                "trading rules, corporate actions and fees are still missing",
            ],
            "formal_research_allowed": False,
        }
        write_json_atomic(self.config.report_output, report)
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build provisional historical ETF master")
    parser.add_argument("--config", default="config.m1_2.yaml")
    args = parser.parse_args(argv)
    payload = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    config = MembershipConfig.from_mapping(payload["membership"])
    report = HistoricalMembershipCollector(config).collect()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

