from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import argparse
import json
from pathlib import Path
import re
import time
from typing import Any, Callable

import yaml

from backend.data_pipeline.provider import RequestPacer
from backend.data_pipeline.storage import (
    read_json,
    read_ndjson,
    write_json_atomic,
    write_ndjson_atomic,
)


@dataclass(frozen=True)
class AnnouncementConfig:
    actions_path: Path
    output_path: Path
    cache_root: Path
    report_path: Path
    request_interval_seconds: float = 3.0
    max_retries: int = 3
    backoff_seconds: float = 1.0
    match_window_days: int = 120

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "AnnouncementConfig":
        config = cls(
            actions_path=Path(str(payload["actions_path"])),
            output_path=Path(str(payload["output_path"])),
            cache_root=Path(str(payload["cache_root"])),
            report_path=Path(str(payload["report_path"])),
            request_interval_seconds=float(payload["request_interval_seconds"]),
            max_retries=int(payload["max_retries"]),
            backoff_seconds=float(payload["backoff_seconds"]),
            match_window_days=int(payload["match_window_days"]),
        )
        if config.request_interval_seconds < 3:
            raise ValueError("Announcement request interval must be >= 3 seconds")
        if config.match_window_days <= 0:
            raise ValueError("Announcement match window must be positive")
        return config


class AnnouncementCollector:
    def __init__(
        self,
        config: AnnouncementConfig,
        *,
        fetch_func: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.fetch_func = fetch_func
        self.pacer = RequestPacer(
            config.request_interval_seconds,
            sleeper=sleeper,
        )
        self._sleep = sleeper
        self.request_count = 0
        self.cache_hits = 0
        self.failures: list[dict[str, str]] = []
        self.warnings: list[dict[str, str]] = []

    def _load_fetch_func(self) -> Callable[..., Any]:
        if self.fetch_func is not None:
            return self.fetch_func
        from akshare import fund_announcement_dividend_em  # type: ignore[import-not-found]

        return fund_announcement_dividend_em

    def _call_with_retry(self, code: str, call: Callable[[], Any]) -> Any:
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
        raise RuntimeError(
            f"Announcement fetch failed after retries for {code}: {last_error}"
        ) from last_error

    def _announcements_for(self, code: str) -> list[dict[str, Any]]:
        cache_path = self.config.cache_root / f"{code}.json"
        cached = read_json(cache_path)
        if cached is not None:
            self.cache_hits += 1
            return cached["announcements"]
        fetch_func = self._load_fetch_func()
        try:
            frame = self._call_with_retry(
                code,
                lambda: fetch_func(symbol=code),
            )
        except Exception as exc:
            if "Expected axis has 0 elements" not in str(exc):
                raise
            self.warnings.append(
                {
                    "code": code,
                    "warning": "provider returned an empty announcement table",
                }
            )
            cached = {"code": code, "announcements": []}
            write_json_atomic(cache_path, cached)
            return []
        rows = []
        for row in frame.to_dict("records"):
            announcement_date = row.get("公告日期")
            if hasattr(announcement_date, "date"):
                announcement_date = announcement_date.date()
            if not isinstance(announcement_date, date):
                announcement_date = date.fromisoformat(str(announcement_date)[:10])
            rows.append(
                {
                    "announcement_date": announcement_date.isoformat(),
                    "title": str(row.get("公告标题", "")),
                    "report_id": str(row.get("报告ID", "")),
                }
            )
        cached = {"code": code, "announcements": rows}
        write_json_atomic(cache_path, cached)
        return rows

    @staticmethod
    def _matches(action_type: str, title: str) -> bool:
        if action_type == "cash_dividend":
            return bool(re.search(r"分红|收益分配|利润分配|派息", title))
        if action_type == "split":
            return bool(re.search(r"拆分|份额折算|折算", title))
        return False

    def collect(self) -> dict[str, Any]:
        actions = read_ndjson(self.config.actions_path)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for action in actions:
            grouped.setdefault(str(action["code"]), []).append(action)
        output = []
        matched = 0
        fallback = 0
        for code in sorted(grouped):
            try:
                announcements = self._announcements_for(code)
            except Exception as exc:
                self.failures.append({"code": code, "error": str(exc)})
                announcements = []
            parsed = [
                {
                    **item,
                    "announcement_date": date.fromisoformat(
                        item["announcement_date"]
                    ),
                }
                for item in announcements
            ]
            for action in grouped[code]:
                effective = date.fromisoformat(str(action["effective_date"]))
                candidates = [
                    item
                    for item in parsed
                    if item["announcement_date"] <= effective
                    and item["announcement_date"]
                    >= effective - timedelta(days=self.config.match_window_days)
                    and self._matches(str(action["action_type"]), item["title"])
                ]
                if candidates:
                    evidence = max(
                        candidates,
                        key=lambda item: (
                            item["announcement_date"],
                            item["announcement_date"],
                        ),
                    )
                    annotated = {
                        **action,
                        "known_at": evidence["announcement_date"].isoformat(),
                        "known_at_quality": "announcement_match",
                        "announcement_title": evidence["title"],
                        "announcement_report_id": evidence["report_id"],
                    }
                    matched += 1
                else:
                    annotated = {
                        **action,
                        "known_at_quality": "effective_date_fallback",
                    }
                    fallback += 1
                output.append(annotated)
        output.sort(
            key=lambda item: (
                item["effective_date"],
                item["code"],
                item["action_type"],
            )
        )
        write_ndjson_atomic(self.config.output_path, output)
        report = {
            "status": "ANNOUNCEMENTS_ANNOTATED",
            "actions": len(output),
            "codes": len(grouped),
            "announcement_matched": matched,
            "effective_date_fallback": fallback,
            "network_requests": self.request_count,
            "cache_hits": self.cache_hits,
            "failures": self.failures,
            "warnings": self.warnings,
            "output": str(self.config.output_path),
            "formal_research_allowed": False,
        }
        write_json_atomic(self.config.report_path, report)
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Annotate action known_at from announcements")
    parser.add_argument("--config", default="config.m1_2.yaml")
    args = parser.parse_args(argv)
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    collector = AnnouncementCollector(
        AnnouncementConfig.from_mapping(config["announcements"])
    )
    report = collector.collect()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

