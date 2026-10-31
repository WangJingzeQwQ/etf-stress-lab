from __future__ import annotations

from datetime import date
from decimal import Decimal

from backend.data_pipeline.audit_coverage import audit_code_coverage
from backend.data_pipeline.contracts import RawDailyRecord
from backend.data_pipeline.storage import save_raw_daily


def _row(day: date) -> RawDailyRecord:
    return RawDailyRecord(
        code="159927",
        name="A300ETF",
        trading_date=day,
        raw_open=Decimal("2.3"),
        raw_high=Decimal("2.4"),
        raw_low=Decimal("2.2"),
        raw_close=Decimal("2.35"),
        volume=Decimal("1000"),
        amount=Decimal("2350"),
        provider="TEST",
    )


def test_coverage_audit_detects_gap_and_post_delist_identity_conflict(tmp_path):
    path = tmp_path / "159927.ndjson"
    save_raw_daily(
        path,
        [
            _row(date(2018, 1, 2)),
            _row(date(2021, 6, 27)),
        ],
    )
    result = audit_code_coverage(
        code="159927",
        list_date=date(2018, 1, 2),
        delist_date=date(2018, 2, 12),
        requested_start=date(2018, 1, 1),
        requested_end=date(2026, 10, 29),
        path=path,
    )
    assert result["status"] == "REVIEW"
    assert result["after_delist_rows"] == 1
    assert result["maximum_gap_days"] > 1000
    assert any("inferred delist_date" in error for error in result["errors"])

