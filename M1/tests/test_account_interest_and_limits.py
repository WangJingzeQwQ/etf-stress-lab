from __future__ import annotations

from datetime import date
from decimal import Decimal

from tests.helpers import D, make_bar, make_broker, make_dataset, make_target


def _two_day_dataset():
    day1 = date(2024, 1, 2)
    day2 = date(2024, 1, 3)
    dataset = make_dataset(
        [
            make_bar(day1, "A", raw_open="10", raw_close="10.2"),
            make_bar(
                day2,
                "A",
                raw_open="12",
                raw_close="12.3",
                prev_close="10.2",
            ),
        ]
    )
    return dataset, day1, day2


def test_price_change_does_not_change_debt_with_zero_interest():
    dataset, day1, day2 = _two_day_dataset()
    broker = make_broker(initial_cash="100000", leverage="2", interest_rate="0")
    broker.begin_day(dataset, day1)
    broker.execute_target(
        dataset,
        make_target(day1, {"A": 1.0}, signal_date=date(2024, 1, 1)),
        {"A"},
    )
    first = broker.finish_day(dataset)
    debt_after_trade = first.snapshot.debt
    broker.begin_day(dataset, day2)
    second = broker.finish_day(dataset)
    assert debt_after_trade > 0
    assert second.snapshot.debt == debt_after_trade
    assert second.snapshot.equity == (
        second.snapshot.cash
        + second.snapshot.position_value
        - second.snapshot.debt
    )


def test_interest_uses_previous_day_debt_as_base():
    dataset, day1, day2 = _two_day_dataset()
    broker = make_broker(initial_cash="100000", leverage="2")
    broker.begin_day(dataset, day1)
    broker.execute_target(
        dataset,
        make_target(day1, {"A": 1.0}, signal_date=date(2024, 1, 1)),
        {"A"},
    )
    first = broker.finish_day(dataset)
    prior_debt = first.snapshot.debt
    broker.begin_day(dataset, day2)
    interest = broker._current_context.daily_interest
    assert interest.base_debt == prior_debt
    assert interest.accrued == prior_debt * D("0.06") / D(365)
    second = broker.finish_day(dataset)
    assert second.snapshot.cumulative_interest == interest.accrued


def test_financing_limit_reduces_order_to_whole_lots():
    session = date(2024, 1, 2)
    dataset = make_dataset(
        [
            make_bar(
                session,
                "A",
                raw_open="10",
                raw_high="10.1",
                adv20_amount="1000000000",
            )
        ]
    )
    broker = make_broker(initial_cash="100000", leverage="2", interest_rate="0")
    broker.begin_day(dataset, session)
    summary = broker.execute_target(
        dataset,
        make_target(session, {"A": 1.0}, signal_date=date(2024, 1, 1)),
        {"A"},
    )
    trade = broker._current_trades[0]
    assert broker.account.cash >= 0
    assert broker.account.debt <= summary.credit_limit
    assert trade.shares < summary.lines[0].target_shares
    assert trade.shares % 100 == 0


def test_daily_equity_identity_holds_after_interest_and_trade():
    dataset, day1, day2 = _two_day_dataset()
    broker = make_broker(initial_cash="100000", leverage="5")
    broker.begin_day(dataset, day1)
    broker.execute_target(
        dataset,
        make_target(day1, {"A": 1.0}, signal_date=date(2024, 1, 1)),
        {"A"},
    )
    first = broker.finish_day(dataset)
    broker.begin_day(dataset, day2)
    second = broker.finish_day(dataset)
    for snapshot in (first.snapshot, second.snapshot):
        assert snapshot.equity == (
            snapshot.cash + snapshot.position_value - snapshot.debt
        )
        assert snapshot.cash >= 0
        assert snapshot.debt >= 0
        assert all(position["size"] >= 0 for position in snapshot.positions)


def test_ledger_explains_cash_debt_position_and_fee_events():
    session = date(2024, 1, 2)
    dataset = make_dataset([make_bar(session, "A", raw_open="10")])
    broker = make_broker(initial_cash="100000", leverage="3")
    broker.begin_day(dataset, session)
    broker.execute_target(
        dataset,
        make_target(session, {"A": 1.0}, signal_date=date(2024, 1, 1)),
        {"A"},
    )
    broker.finish_day(dataset)
    events = broker.account.audit_events
    trade_event = [event for event in events if event.event_type == "active_buy"][0]
    assert trade_event.cash_delta < 0
    assert trade_event.debt_delta > 0
    assert trade_event.position_delta > 0
    assert trade_event.commission > 0
    assert trade_event.cash_after == broker.account.cash
    assert trade_event.debt_after == broker.account.debt
    cash_delta_total = sum((event.cash_delta for event in events), Decimal("0"))
    debt_delta_total = sum((event.debt_delta for event in events), Decimal("0"))
    assert broker.account.cash == broker.account.initial_cash + cash_delta_total
    assert broker.account.debt == debt_delta_total


def test_current_equity_capital_base_uses_previous_close_equity():
    day1 = date(2024, 1, 2)
    day2 = date(2024, 1, 3)
    dataset = make_dataset(
        [
            make_bar(day1, "A", raw_open="10", raw_close="10"),
            make_bar(day2, "A", raw_open="9", raw_close="9", prev_close="10"),
        ]
    )
    broker = make_broker(
        initial_cash="100000",
        leverage="1",
        interest_rate="0",
        capital_base_mode="current_equity",
    )
    broker.begin_day(dataset, day1)
    broker.execute_target(
        dataset,
        make_target(
            day1, {"A": 1.0}, signal_date=date(2024, 1, 1), group="G5"
        ),
        {"A"},
    )
    first = broker.finish_day(dataset)
    broker.begin_day(dataset, day2)
    summary = broker.execute_target(
        dataset,
        make_target(day2, {"A": 1.0}, signal_date=day1, group="G5"),
        {"A"},
    )
    assert summary is not None
    assert summary.capital_base == first.snapshot.equity
    assert summary.capital_base < D("100000")


def test_debt_above_credit_limit_blocks_new_buys_even_with_cash():
    session = date(2024, 1, 2)
    dataset = make_dataset([make_bar(session, "A", raw_open="10")])
    broker = make_broker(initial_cash="100000", leverage="2", interest_rate="0")
    broker.account.cash_ledger.balance = D("50000")
    broker.account.debt_ledger.balance = D("150000")
    broker.begin_day(dataset, session)
    summary = broker.execute_target(
        dataset,
        make_target(session, {"A": 1.0}, signal_date=date(2024, 1, 1)),
        {"A"},
    )
    assert summary is not None
    assert broker.account.debt == D("150000")
    assert broker.account.cash == D("50000")
    assert "A" not in broker.account.positions
    assert broker._current_trades[0].reason == "cash_or_credit_limit"

