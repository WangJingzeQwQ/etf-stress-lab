from __future__ import annotations

from datetime import date
import json

import pandas as pd

from backend.data_pipeline.corporate_action_collector import (
    CorporateActionCollector,
    CorporateActionConfig,
)
from backend.data_pipeline.fetch_master import save_master_records
from tests.test_phase1_contracts import _master


def _config(tmp_path) -> CorporateActionConfig:
    master_path = tmp_path / "meta" / "master.ndjson"
    save_master_records(master_path, {"510300": _master("510300")})
    return CorporateActionConfig(
        start_year=2024,
        end_year=2024,
        end_date=date(2024, 12, 31),
        master_path=master_path,
        output_path=tmp_path / "meta" / "actions.ndjson",
        cache_root=tmp_path / "cache",
        report_path=tmp_path / "reports" / "actions.json",
        request_interval_seconds=3,
        max_retries=2,
        backoff_seconds=0,
    )


def test_corporate_actions_are_filtered_and_cached(tmp_path):
    config = _config(tmp_path)
    calls = {"count": 0}

    def dividends(**kwargs):
        calls["count"] += 1
        return pd.DataFrame(
            [
                {
                    "基金代码": "510300",
                    "基金简称": "沪深300ETF",
                    "权益登记日": date(2024, 1, 31),
                    "除息日期": date(2024, 2, 1),
                    "分红": 0.03,
                    "分红发放日": date(2024, 2, 2),
                },
                {
                    "基金代码": "000001",
                    "基金简称": "非ETF",
                    "权益登记日": date(2024, 1, 31),
                    "除息日期": date(2024, 2, 1),
                    "分红": 0.1,
                    "分红发放日": date(2024, 2, 2),
                },
            ]
        )

    def splits(**kwargs):
        calls["count"] += 1
        return pd.DataFrame(
            [
                {
                    "基金代码": "510300",
                    "基金简称": "沪深300ETF",
                    "拆分折算日": date(2024, 3, 1),
                    "拆分类型": "份额折算",
                    "拆分折算": 1.5,
                }
            ]
        )

    first = CorporateActionCollector(
        config,
        fetch_dividend=dividends,
        fetch_split=splits,
        sleeper=lambda _: None,
    ).collect()
    assert first["matched_events"] == 2
    assert first["cash_dividends"] == 1
    assert first["splits"] == 1
    assert calls["count"] == 2
    rows = [
        json.loads(line)
        for line in config.output_path.read_text(encoding="utf-8").splitlines()
    ]
    assert {row["code"] for row in rows} == {"510300"}
    assert all(row["known_at"] == row["effective_date"] for row in rows)
    assert all(row["known_at_quality"] == "effective_date_fallback" for row in rows)

    second = CorporateActionCollector(
        config,
        fetch_dividend=dividends,
        fetch_split=splits,
        sleeper=lambda _: None,
    ).collect()
    assert second["network_requests"] == 0
    assert second["cache_hits"] == 2
    assert calls["count"] == 2


def test_full_page_followed_by_empty_source_is_treated_as_end(tmp_path):
    config = _config(tmp_path)

    def dividends(**kwargs):
        if kwargs["page"] == 1:
            return pd.DataFrame(
                [
                    {
                        "基金代码": "510300",
                        "基金简称": "沪深300ETF",
                        "权益登记日": date(2024, 1, 31),
                        "除息日期": date(2024, 2, 1),
                        "分红": 0.03,
                        "分红发放日": date(2024, 2, 2),
                    }
                    for _ in range(100)
                ]
            )
        raise SyntaxError("empty response")

    def splits(**kwargs):
        return pd.DataFrame()

    report = CorporateActionCollector(
        config,
        fetch_dividend=dividends,
        fetch_split=splits,
        sleeper=lambda _: None,
    ).collect()
    assert report["failures"] == []
    assert report["warnings"]

