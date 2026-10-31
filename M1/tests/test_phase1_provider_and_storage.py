from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from backend.data_pipeline.contracts import DataQualityError, RawDailyRecord
from backend.data_pipeline.bundle_store import load_bundle, save_bundle
from backend.data_pipeline.fetch_daily import IncrementalEtfDownloader
from backend.data_pipeline.fetch_master import (
    CurrentEtfSnapshotRow,
    load_master_records,
    save_current_snapshot,
    save_master_records,
)
from backend.data_pipeline.provider import (
    AkshareETFProvider,
    BaoStockETFProvider,
    HistoricalMasterUnavailableError,
    RequestPacer,
    normalize_provider_frame,
)
from backend.data_pipeline.phase1_fixture import build_phase1_fixture
from backend.data_pipeline.small_batch import SmallBatchConfig, run_small_batch
from backend.data_pipeline.storage import load_raw_daily, read_json, write_json_atomic
from tests.test_phase1_contracts import _master


def _frame_for(day: date) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "日期": day.isoformat(),
                "开盘": 10.0,
                "最高": 10.2,
                "最低": 9.9,
                "收盘": 10.1,
                "成交量": 1000,
                "成交额": 10_100_000,
            }
        ]
    )


class FakeProvider:
    provider_name = "fake_raw_provider"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, date, date]] = []

    def fetch_daily(self, code, name, start_date, end_date):
        self.calls.append((code, start_date, end_date))
        if self.fail:
            raise RuntimeError("provider failure")
        rows = []
        cursor = start_date
        while cursor <= end_date:
            if cursor.weekday() < 5:
                rows.append(
                    RawDailyRecord(
                        code=code,
                        name=name,
                        trading_date=cursor,
                        raw_open=Decimal("10"),
                        raw_high=Decimal("10.2"),
                        raw_low=Decimal("9.9"),
                        raw_close=Decimal("10.1"),
                        volume=Decimal("1000"),
                        amount=Decimal("10100000"),
                        provider=self.provider_name,
                    )
                )
            cursor += timedelta(days=1)
        return rows


def test_request_pacer_enforces_three_second_minimum():
    elapsed = [0.0]
    sleeps: list[float] = []
    pacer = RequestPacer(
        3.0,
        clock=lambda: elapsed[0],
        sleeper=lambda delay: (sleeps.append(delay), elapsed.__setitem__(0, elapsed[0] + delay)),
    )
    pacer.before_request()
    elapsed[0] += 0.5
    pacer.before_request()
    assert sleeps == [2.5]
    with pytest.raises(ValueError, match="at least 3"):
        RequestPacer(2.99)


def test_provider_frame_is_normalized_with_raw_adjustment_only():
    records = normalize_provider_frame(
        _frame_for(date(2024, 1, 2)),
        code="A",
        name="甲ETF",
        provider="fake",
    )
    assert len(records) == 1
    assert records[0].adjustment == "none"
    assert records[0].raw_open == Decimal("10.0")
    assert records[0].volume == Decimal("1000")
    assert records[0].volume_unit == "shares"
    assert records[0].amount_currency == "CNY"
    eastmoney = normalize_provider_frame(
        _frame_for(date(2024, 1, 2)),
        code="A",
        name="甲ETF",
        provider="akshare_raw",
    )
    assert eastmoney[0].volume == Decimal("100000")
    with pytest.raises(DataQualityError, match="lacks Chinese name"):
        normalize_provider_frame(
            _frame_for(date(2024, 1, 2)),
            code="A",
            name="",
            provider="fake",
        )


def test_akshare_provider_calls_raw_endpoint_and_retries():
    calls = []
    attempts = {"count": 0}

    def fetch_func(**kwargs):
        calls.append(kwargs)
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise TimeoutError("temporary")
        return _frame_for(date(2024, 1, 2))

    elapsed = [0.0]
    provider = AkshareETFProvider(
        fetch_func=fetch_func,
        max_retries=2,
        backoff_seconds=0.1,
        limiter=RequestPacer(
            3.0,
            clock=lambda: elapsed[0],
            sleeper=lambda delay: elapsed.__setitem__(0, elapsed[0] + delay),
        ),
        sleeper=lambda delay: elapsed.__setitem__(0, elapsed[0] + delay),
    )
    records = provider.fetch_daily(
        "A", "甲ETF", date(2024, 1, 1), date(2024, 1, 3)
    )
    assert len(records) == 1
    assert attempts["count"] == 2
    assert all(call["adjust"] == "" for call in calls)


def test_akshare_provider_refuses_to_present_spot_as_historical_master():
    provider = AkshareETFProvider(fetch_func=lambda **kwargs: _frame_for(date(2024, 1, 2)))
    with pytest.raises(HistoricalMasterUnavailableError, match="historical master"):
        provider.fetch_historical_master()


def test_baostock_provider_reads_unadjusted_daily_amount():
    class Login:
        error_code = "0"
        error_msg = "success"

    class Result:
        error_code = "0"
        error_msg = "success"
        fields = [
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "adjustflag",
        ]

        def __init__(self):
            self.rows = iter(
                [
                    [
                        "2018-01-02",
                        "100.0680",
                        "100.0840",
                        "100.0110",
                        "100.0210",
                        "19800",
                        "1981162",
                        "3",
                    ]
                ]
            )

        def next(self):
            try:
                self.current = next(self.rows)
                return True
            except StopIteration:
                return False

        def get_row_data(self):
            return self.current

    class BaoStock:
        def login(self):
            return Login()

        def logout(self):
            return None

        def query_history_k_data_plus(self, *args, **kwargs):
            assert kwargs["adjustflag"] == "3"
            return Result()

    provider = BaoStockETFProvider(bs_module=BaoStock())
    records = provider.fetch_daily(
        "511680",
        "安信货币",
        date(2018, 1, 1),
        date(2018, 1, 3),
    )
    assert len(records) == 1
    assert records[0].amount == Decimal("1981162")
    assert records[0].volume == Decimal("19800")
    assert records[0].adjustment == "none"


def test_incremental_download_resumes_from_last_date(tmp_path):
    provider = FakeProvider()
    downloader = IncrementalEtfDownloader(provider, tmp_path / "data")
    names = {"A": "甲ETF"}
    first = downloader.download(
        ["A"],
        names,
        date(2024, 1, 1),
        date(2024, 1, 3),
    )
    assert first.completed_codes == ["A"]
    assert provider.calls == [("A", date(2024, 1, 1), date(2024, 1, 3))]

    second = downloader.download(
        ["A"],
        names,
        date(2024, 1, 1),
        date(2024, 1, 3),
    )
    assert second.skipped_up_to_date_codes == ["A"]
    assert len(provider.calls) == 1

    third = downloader.download(
        ["A"],
        names,
        date(2024, 1, 1),
        date(2024, 1, 5),
    )
    assert provider.calls[-1] == ("A", date(2024, 1, 4), date(2024, 1, 5))
    assert (tmp_path / "data" / "state" / "download_state.json").exists()


def test_failed_download_enters_failure_queue(tmp_path):
    downloader = IncrementalEtfDownloader(
        FakeProvider(fail=True), tmp_path / "data"
    )
    summary = downloader.download(
        ["A"],
        {"A": "甲ETF"},
        date(2024, 1, 1),
        date(2024, 1, 2),
    )
    assert not summary.success
    assert "A" in summary.failed_codes
    queue = (tmp_path / "data" / "state" / "failure_queue.json").read_text(
        encoding="utf-8"
    )
    assert "provider failure" in queue


def test_incremental_download_backfills_earlier_start_date(tmp_path):
    provider = FakeProvider()
    downloader = IncrementalEtfDownloader(provider, tmp_path / "data")
    downloader.download(
        ["A"],
        {"A": "甲ETF"},
        date(2024, 1, 3),
        date(2024, 1, 5),
    )
    downloader.download(
        ["A"],
        {"A": "甲ETF"},
        date(2024, 1, 1),
        date(2024, 1, 5),
    )
    records = load_raw_daily(
        tmp_path / "data" / "raw" / "etf" / "A.ndjson"
    )
    assert min(record.trading_date for record in records) == date(2024, 1, 1)
    assert provider.calls[-1] == (
        "A",
        date(2024, 1, 1),
        date(2024, 1, 5),
    )


def test_delisted_code_uses_effective_delist_end(tmp_path):
    provider = FakeProvider()
    downloader = IncrementalEtfDownloader(provider, tmp_path / "data")
    downloader.download(
        ["A"],
        {"A": "退市ETF"},
        date(2024, 1, 1),
        date(2024, 1, 5),
        effective_end_dates={"A": date(2024, 1, 3)},
    )
    assert provider.calls == [("A", date(2024, 1, 1), date(2024, 1, 3))]


def test_master_round_trip_ndjson(tmp_path):
    path = tmp_path / "master.ndjson"
    records = {"A": _master()}
    save_master_records(path, records)
    loaded = load_master_records(path)
    assert loaded["A"] == records["A"]


def test_current_snapshot_manifest_is_explicitly_nonhistorical(tmp_path):
    path = tmp_path / "current_snapshot.ndjson"
    save_current_snapshot(
        path,
        [
            CurrentEtfSnapshotRow(
                code="A",
                name="甲ETF",
                observed_at=date(2026, 10, 29),
            )
        ],
    )
    manifest = read_json(tmp_path / "current_snapshot.manifest.json")
    assert manifest["kind"] == "CURRENT_ETF_NAME_SNAPSHOT"
    assert manifest["historical_master"] is False
    assert manifest["formal_research_allowed"] is False


def test_phase1_bundle_round_trip_preserves_raw_and_history(tmp_path):
    original = build_phase1_fixture()
    paths = save_bundle(tmp_path / "bundle", original)
    assert paths["master"].exists()
    assert paths["trading_rules"].exists()
    assert paths["corporate_actions"].exists()
    loaded = load_bundle(tmp_path / "bundle")
    assert loaded.dataset.codes == original.dataset.codes
    assert len(loaded.dataset.bars) == len(original.dataset.bars)
    assert loaded.dataset.corporate_actions == original.dataset.corporate_actions
    assert loaded.dataset.trading_rules == original.dataset.trading_rules
    assert loaded.master["159999"].delist_date == original.master["159999"].delist_date
    assert all(
        bar.total_return_close == bar.raw_close
        for bar in loaded.dataset.bars.values()
    )


def test_bundle_reload_detects_manifest_hash_tampering(tmp_path):
    root = tmp_path / "bundle"
    save_bundle(root, build_phase1_fixture())
    manifest_path = root / "meta" / "data_manifest.json"
    manifest = read_json(manifest_path)
    manifest["data_version"] = "sha256:tampered"
    write_json_atomic(manifest_path, manifest)
    with pytest.raises(DataQualityError, match="does not match"):
        load_bundle(root)


def test_m1_1_small_batch_is_real_raw_but_formal_blocked(tmp_path):
    snapshot_frame = pd.DataFrame(
        [
            {"基金代码": "A", "基金简称": "甲ETF"},
            {"基金代码": "B", "基金简称": "乙ETF"},
        ]
    )
    config = SmallBatchConfig.from_mapping(
        {
            "codes": ["A", "B"],
            "start_date": "2024-01-01",
            "end_date": "2024-01-03",
            "output_root": str(tmp_path / "raw"),
            "report_path": str(tmp_path / "report.json"),
            "request_interval_seconds": 3,
            "max_retries": 1,
            "backoff_seconds": 0,
        }
    )
    report = run_small_batch(
        config,
        provider=FakeProvider(),
        snapshot_fetcher=lambda: snapshot_frame,
    )
    assert report["status"] == "PASS"
    assert report["source_kind"] == "REAL_PROVIDER_PROVISIONAL"
    assert report["formal_research_allowed"] is False
    assert all(item["adjustment"] == ["none"] for item in report["files"])

