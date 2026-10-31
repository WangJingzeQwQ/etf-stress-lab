from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from backend.data_pipeline.contracts import (
    CoverageFlags,
    DataManifest,
    DataQualityError,
    MasterRecord,
    NameHistoryPoint,
    RawDailyRecord,
    StatusHistoryPoint,
)
from backend.data_pipeline.dataset_factory import build_dataset_from_raw
from backend.data_pipeline.models import TradingRule


def _master(
    code: str = "A",
    *,
    delist_date: date | None = None,
    complete: bool = True,
) -> MasterRecord:
    return MasterRecord(
        code=code,
        list_date=date(2020, 1, 1),
        delist_date=delist_date,
        name_history=(
            NameHistoryPoint("旧名称", date(2020, 1, 1), date(2020, 1, 1)),
            NameHistoryPoint("新名称", date(2024, 1, 2), date(2023, 12, 20)),
        ),
        status_history=(
            StatusHistoryPoint("ACTIVE", date(2020, 1, 1), date(2020, 1, 1)),
        ),
        history_complete=complete,
        source="TEST",
    )


def _raw(
    day: date,
    *,
    code: str = "A",
    open_price: str = "10",
    close_price: str = "10.2",
    amount: str = "30000000",
) -> RawDailyRecord:
    return RawDailyRecord(
        code=code,
        name="测试ETF",
        trading_date=day,
        raw_open=Decimal(open_price),
        raw_high=Decimal(close_price) + Decimal("0.1"),
        raw_low=Decimal(open_price) - Decimal("0.1"),
        raw_close=Decimal(close_price),
        volume=Decimal("1000"),
        amount=Decimal(amount),
        provider="TEST",
    )


def test_master_name_is_point_in_time_and_rename_uses_known_at():
    record = _master()
    assert record.name_at(date(2023, 12, 19)) == "旧名称"
    assert record.name_at(date(2024, 1, 1)) == "旧名称"
    assert record.name_at(date(2024, 1, 2)) == "新名称"
    assert record.former_names(date(2024, 1, 2)) == ["旧名称", "新名称"]


def test_master_status_requires_knowledge_by_cutoff():
    record = MasterRecord(
        code="A",
        list_date=date(2020, 1, 1),
        delist_date=None,
        name_history=(
            NameHistoryPoint("甲", date(2020, 1, 1), date(2020, 1, 1)),
        ),
        status_history=(
            StatusHistoryPoint("ACTIVE", date(2020, 1, 1), date(2020, 1, 1)),
            StatusHistoryPoint("SUSPENDED", date(2024, 1, 2), date(2024, 1, 3)),
        ),
        history_complete=True,
        source="TEST",
    )
    assert record.status_at(date(2024, 1, 2), date(2024, 1, 1)) == "ACTIVE"
    assert record.status_at(date(2024, 1, 2), date(2024, 1, 3)) == "SUSPENDED"


def test_incomplete_real_manifest_is_rejected():
    with pytest.raises(DataQualityError, match="incomplete"):
        DataManifest(
            source_kind="REAL_PROVIDER",
            source_name="provider",
            data_version="sha256:x",
            coverage=CoverageFlags(
                raw_ohlcv=True,
                historical_master=False,
                delist_history=False,
                corporate_actions=False,
                trading_rules=False,
                cost_schedules=False,
            ),
            real_market_evidence_allowed=True,
        ).validate()


def test_raw_record_rejects_adjusted_price_marker():
    row = _raw(date(2024, 1, 2))
    with pytest.raises(DataQualityError, match="Adjusted price"):
        RawDailyRecord(
            **{
                **row.__dict__,
                "adjustment": "qfq",
            }
        ).validate()


def test_dataset_factory_keeps_raw_prices_and_excludes_current_day_from_adv():
    first = date(2024, 1, 2)
    second = date(2024, 1, 3)
    third = date(2024, 1, 4)
    rule = TradingRule(
        code="A",
        settlement="T1",
        effective_from=date(2020, 1, 1),
        effective_to=date(2099, 12, 31),
        limit_pct=Decimal("0.10"),
        known_at=date(2020, 1, 1),
    )
    records = [
        _raw(first, amount="10000000"),
        _raw(second, amount="20000000"),
        _raw(third, amount="90000000"),
    ]
    dataset = build_dataset_from_raw(
        records,
        {"A": _master()},
        [rule],
        [],
        source_kind="TEST_FIXTURE",
    )
    third_bar = dataset.require_bar(third, "A")
    assert third_bar.raw_open == Decimal("10")
    assert third_bar.adv20_amount == Decimal("15000000")
    assert third_bar.provider_qfq_close is None
    assert third_bar.total_return_close == third_bar.raw_close
    assert third_bar.limit_up == Decimal("11.220")


def test_dataset_factory_rejects_missing_master_before_trading():
    with pytest.raises(DataQualityError, match="master"):
        build_dataset_from_raw(
            [_raw(date(2024, 1, 2))],
            {},
            [
                TradingRule(
                    code="A",
                    settlement="T1",
                    effective_from=date(2020, 1, 1),
                    effective_to=date(2099, 12, 31),
                    limit_pct=Decimal("0.10"),
                )
            ],
            [],
            source_kind="TEST_FIXTURE",
        )

