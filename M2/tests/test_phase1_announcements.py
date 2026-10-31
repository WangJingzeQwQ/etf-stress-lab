from __future__ import annotations

from datetime import date
import json

import pandas as pd

from backend.data_pipeline.announcement_collector import (
    AnnouncementCollector,
    AnnouncementConfig,
)
from backend.data_pipeline.storage import write_ndjson_atomic


def test_announcement_known_at_is_matched_and_cached(tmp_path):
    actions_path = tmp_path / "actions.ndjson"
    write_ndjson_atomic(
        actions_path,
        [
            {
                "code": "510300",
                "effective_date": "2024-02-01",
                "known_at": "2024-02-01",
                "action_type": "cash_dividend",
                "cash_per_share": "0.03",
                "split_ratio": "1",
                "known_at_quality": "effective_date_fallback",
            },
            {
                "code": "510300",
                "effective_date": "2024-06-01",
                "known_at": "2024-06-01",
                "action_type": "cash_dividend",
                "cash_per_share": "0.04",
                "split_ratio": "1",
                "known_at_quality": "effective_date_fallback",
            },
        ],
    )
    config = AnnouncementConfig(
        actions_path=actions_path,
        output_path=tmp_path / "annotated.ndjson",
        cache_root=tmp_path / "cache",
        report_path=tmp_path / "report.json",
        request_interval_seconds=3,
        max_retries=1,
        backoff_seconds=0,
        match_window_days=120,
    )
    calls = {"count": 0}

    def fetch_announcements(**kwargs):
        calls["count"] += 1
        return pd.DataFrame(
            [
                {
                    "基金代码": "510300",
                    "公告标题": "沪深300ETF分红公告",
                    "基金名称": "沪深300ETF",
                    "公告日期": date(2024, 1, 11),
                    "报告ID": "AN001",
                }
            ]
        )

    first = AnnouncementCollector(
        config,
        fetch_func=fetch_announcements,
        sleeper=lambda _: None,
    ).collect()
    assert first["announcement_matched"] == 1
    assert first["effective_date_fallback"] == 1
    rows = [
        json.loads(line)
        for line in config.output_path.read_text(encoding="utf-8").splitlines()
    ]
    matched = next(row for row in rows if row["effective_date"] == "2024-02-01")
    assert matched["known_at"] == "2024-01-11"
    assert matched["known_at_quality"] == "announcement_match"
    unmatched = next(row for row in rows if row["effective_date"] == "2024-06-01")
    assert unmatched["known_at_quality"] == "effective_date_fallback"

    second = AnnouncementCollector(
        config,
        fetch_func=fetch_announcements,
        sleeper=lambda _: None,
    ).collect()
    assert second["network_requests"] == 0
    assert second["cache_hits"] == 1
    assert calls["count"] == 1


def test_empty_announcement_provider_response_is_cached_as_empty(tmp_path):
    actions_path = tmp_path / "actions.ndjson"
    write_ndjson_atomic(
        actions_path,
        [
            {
                "code": "560010",
                "effective_date": "2024-02-01",
                "known_at": "2024-02-01",
                "action_type": "cash_dividend",
                "known_at_quality": "effective_date_fallback",
            }
        ],
    )
    config = AnnouncementConfig(
        actions_path=actions_path,
        output_path=tmp_path / "annotated.ndjson",
        cache_root=tmp_path / "cache",
        report_path=tmp_path / "report.json",
        request_interval_seconds=3,
        max_retries=1,
        backoff_seconds=0,
        match_window_days=120,
    )

    def empty_provider(**kwargs):
        raise ValueError("Expected axis has 0 elements, new values have 8 elements")

    report = AnnouncementCollector(
        config,
        fetch_func=empty_provider,
        sleeper=lambda _: None,
    ).collect()
    assert report["failures"] == []
    assert report["warnings"]
    assert report["effective_date_fallback"] == 1

