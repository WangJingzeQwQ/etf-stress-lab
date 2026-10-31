from __future__ import annotations

from datetime import date
import json

import pandas as pd

from backend.data_pipeline.fetch_master import (
    CurrentEtfSnapshotRow,
    save_current_snapshot,
)
from backend.data_pipeline.historical_membership import (
    HistoricalMembershipCollector,
    MembershipConfig,
)


def _config(tmp_path) -> MembershipConfig:
    return MembershipConfig(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 2, 29),
        cache_root=tmp_path / "cache",
        master_output=tmp_path / "meta" / "master.ndjson",
        report_output=tmp_path / "reports" / "membership.json",
        current_snapshot=tmp_path / "meta" / "current.ndjson",
        request_interval_seconds=3,
        max_retries=2,
        backoff_seconds=0,
    )


def test_exchange_snapshots_build_provisional_historical_master(tmp_path):
    config = _config(tmp_path)
    save_current_snapshot(
        config.current_snapshot,
        [
            CurrentEtfSnapshotRow(
                code="510001",
                name="甲ETF",
                observed_at=date(2026, 10, 29),
            )
        ],
    )
    sse_calls = []
    szse_calls = []

    def fetch_sse(**kwargs):
        date_text = kwargs["date"]
        sse_calls.append(date_text)
        date_value = date(
            int(date_text[:4]), int(date_text[4:6]), int(date_text[6:])
        )
        if date_text == "20240131":
            rows = [
                {"基金代码": "510001", "基金简称": "甲ETF旧名", "统计日期": date_value},
                {"基金代码": "510002", "基金简称": "乙ETF", "统计日期": date_value},
            ]
        else:
            rows = [
                {"基金代码": "510001", "基金简称": "甲ETF", "统计日期": date_value},
            ]
        return pd.DataFrame(rows)

    def fetch_szse(**kwargs):
        szse_calls.append(kwargs)
        return pd.DataFrame(
            [
                {
                    "日期": date(2024, 1, 31),
                    "基金代码": "159001",
                    "基金简称": "深市退市ETF",
                    "基金份额": 100,
                }
            ]
        )

    collector = HistoricalMembershipCollector(
        config,
        fetch_sse=fetch_sse,
        fetch_szse=fetch_szse,
        sleeper=lambda _: None,
    )
    report = collector.collect()
    assert report["unique_codes"] == 3
    assert report["inferred_delisted_codes"] == 2
    assert len(sse_calls) == 2
    assert len(szse_calls) == 1
    master_rows = [
        json.loads(line)
        for line in config.master_output.read_text(encoding="utf-8").splitlines()
    ]
    masters = {row["code"]: row for row in master_rows}
    assert masters["510001"]["delist_date"] is None
    assert masters["510002"]["delist_date"] == "2024-01-31"
    assert masters["159001"]["delist_date"] == "2024-01-31"
    assert all(not row["history_complete"] for row in master_rows)
    assert report["formal_research_allowed"] is False


def test_membership_cache_resumes_without_network(tmp_path):
    config = _config(tmp_path)
    save_current_snapshot(
        config.current_snapshot,
        [
            CurrentEtfSnapshotRow(
                code="510001",
                name="甲ETF",
                observed_at=date(2026, 10, 29),
            )
        ],
    )
    calls = {"count": 0}

    def fetch_sse(**kwargs):
        date_text = kwargs["date"]
        calls["count"] += 1
        value = date(
            int(date_text[:4]), int(date_text[4:6]), int(date_text[6:])
        )
        return pd.DataFrame(
            [{"基金代码": "510001", "基金简称": "甲ETF", "统计日期": value}]
        )

    def fetch_szse(**kwargs):
        calls["count"] += 1
        return pd.DataFrame(
            [
                {
                    "日期": date(2024, 1, 31),
                    "基金代码": "159001",
                    "基金简称": "退市ETF",
                    "基金份额": 100,
                }
            ]
        )

    first = HistoricalMembershipCollector(
        config,
        fetch_sse=fetch_sse,
        fetch_szse=fetch_szse,
        sleeper=lambda _: None,
    ).collect()
    first_calls = calls["count"]
    second = HistoricalMembershipCollector(
        config,
        fetch_sse=fetch_sse,
        fetch_szse=fetch_szse,
        sleeper=lambda _: None,
    ).collect()
    assert calls["count"] == first_calls
    assert second["cache_hits"] == first["network_requests"]
