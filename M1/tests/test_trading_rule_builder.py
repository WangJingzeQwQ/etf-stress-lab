from datetime import date
from decimal import Decimal

from backend.data_pipeline.build_trading_rules import (
    extract_fund_settlement_hints,
)
from backend.data_pipeline.contracts import (
    MasterRecord,
    NameHistoryPoint,
    RawDailyRecord,
    StatusHistoryPoint,
)
from backend.data_pipeline.dataset_factory import build_dataset_from_raw
from backend.data_pipeline.models import CorporateAction, TradingRule


def _master() -> MasterRecord:
    return MasterRecord(
        code="159220",
        list_date=date(2024, 1, 1),
        delist_date=None,
        name_history=(
            NameHistoryPoint("红利ETF", date(2024, 1, 1), date(2024, 1, 1)),
        ),
        status_history=(
            StatusHistoryPoint("ACTIVE", date(2024, 1, 1), date(2024, 1, 1)),
        ),
        history_complete=True,
        source="AUDITED",
    )


def _bar(day: date, open_price: str, high: str, low: str, close: str):
    return RawDailyRecord(
        code="159220",
        name="红利ETF",
        trading_date=day,
        raw_open=Decimal(open_price),
        raw_high=Decimal(high),
        raw_low=Decimal(low),
        raw_close=Decimal(close),
        volume=Decimal("100000"),
        amount=Decimal("100000"),
        provider="AUDITED",
    )


def test_fund_settlement_parser_keeps_etf_sale_separate_from_primary_subscription():
    assert extract_fund_settlement_hints(
        "T日买入的A类、B类基金份额T日起可以卖出或赎回。"
    ) == {"T0"}
    assert extract_fund_settlement_hints(
        "当日买入的基金份额，同日可以赎回，但不得卖出。"
    ) == {"T1"}
    assert not extract_fund_settlement_hints(
        "港股市场实行T+0回转交易，当日买入的股票当日可以卖出。"
    )


def test_split_ex_reference_uses_first_session_after_conversion_date():
    rule = TradingRule(
        code="159220",
        settlement="T1",
        effective_from=date(2024, 1, 1),
        effective_to=date(2026, 12, 31),
        limit_pct=Decimal("0.10"),
        known_at=date(2024, 1, 1),
        trading_status="ACTIVE",
        source="exchange",
        evidence_id="test-rule",
    )
    split = CorporateAction(
        code="159220",
        effective_date=date(2025, 11, 7),
        known_at=date(2025, 11, 4),
        action_type="split",
        split_ratio=Decimal("2"),
    )
    dataset = build_dataset_from_raw(
        [
            _bar(date(2025, 11, 6), "1.22", "1.233", "1.216", "1.232"),
            _bar(date(2025, 11, 7), "1.233", "1.242", "1.233", "1.24"),
            _bar(date(2025, 11, 10), "0.616", "0.628", "0.614", "0.627"),
        ],
        {"159220": _master()},
        [rule],
        [split],
        source_kind="REAL_PROVIDER",
    )
    converted = dataset.require_bar(date(2025, 11, 10), "159220")
    assert converted.prev_close == Decimal("0.6200")
    assert converted.limit_down == Decimal("0.558")
    assert converted.limit_up == Decimal("0.682")
