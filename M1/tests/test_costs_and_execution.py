from __future__ import annotations

from datetime import date
from decimal import Decimal

from backend.engine.models import AccountStatus
from tests.helpers import D, make_bar, make_broker, make_cost, make_dataset, make_target


def test_minimum_commission_only_when_trade_fills():
    cost = make_cost(commission_rate="0.0003", minimum_commission="5")
    fee = cost.fees("buy", D("100"), 10)
    assert fee.commission == D("5.00")
    assert fee.minimum_commission_triggered
    zero = cost.fees("buy", D("0"), 0)
    assert zero.commission == Decimal("0")
    assert not zero.minimum_commission_triggered


def test_buy_cash_out_equals_gross_plus_fees_and_slippage_is_separate():
    session = date(2024, 1, 2)
    cost = make_cost(
        commission_rate="0",
        minimum_commission="0",
        exchange_fee="0.0001",
        fixed_other="1",
        buy_bps="5",
    )
    dataset = make_dataset([make_bar(session, "A", raw_open="10", raw_high="10.1")])
    broker = make_broker(initial_cash="100000", leverage="1", cost=cost)
    broker.begin_day(dataset, session)
    broker.execute_target(
        dataset,
        make_target(session, {"A": 1.0}, signal_date=date(2024, 1, 1)),
        {"A"},
    )
    trade = broker._current_trades[0]
    gross = trade.execution_price * trade.shares
    other_expected = (gross * D("0.0001") + D("1")).quantize(D("0.01"))
    assert trade.gross_amount == gross
    assert trade.other_fees == other_expected
    assert trade.commission == Decimal("0")
    assert trade.net_cash_flow == -(trade.gross_amount + trade.other_fees)
    assert trade.slippage_cost == D("0.005") * trade.shares
    assert broker.account.cash + broker.account.debt == (
        D("100000") - trade.gross_amount - trade.other_fees
    )


def test_sell_net_proceeds_deduct_fees_then_repay_debt():
    first = date(2024, 1, 2)
    second = date(2024, 1, 3)
    dataset = make_dataset(
        [
            make_bar(first, "A", raw_open="10", raw_close="10"),
            make_bar(
                second,
                "A",
                raw_open="11",
                raw_close="11",
                prev_close="10",
                raw_low="10.8",
            ),
        ]
    )
    broker = make_broker(initial_cash="100000", leverage="2", interest_rate="0")
    broker.begin_day(dataset, first)
    broker.execute_target(
        dataset,
        make_target(first, {"A": 1.0}, signal_date=date(2024, 1, 1)),
        {"A"},
    )
    day_one = broker.finish_day(dataset)
    debt_before_sale = day_one.snapshot.debt
    assert debt_before_sale > 0

    broker.begin_day(dataset, second)
    broker.execute_target(
        dataset,
        make_target(
            second,
            {},
            total_r=0.0,
            signal_date=first,
        ),
        {"A"},
    )
    sell = [
        trade
        for trade in broker._current_trades
        if trade.side == "sell" and trade.shares > 0
    ][0]
    expected_net = sell.gross_amount - sell.commission - sell.other_fees
    assert sell.net_cash_flow == expected_net
    assert sell.debt_repaid == min(expected_net, debt_before_sale)
    day_two = broker.finish_day(dataset)
    expected_debt = debt_before_sale - min(expected_net, debt_before_sale)
    assert day_two.snapshot.debt == expected_debt


def test_zero_fill_due_suspension_has_no_fee():
    session = date(2024, 1, 2)
    dataset = make_dataset(
        [make_bar(session, "A", is_suspended=True, amount="100000000")]
    )
    broker = make_broker()
    broker.begin_day(dataset, session)
    broker.execute_target(
        dataset,
        make_target(session, {"A": 1.0}, signal_date=date(2024, 1, 1)),
        {"A"},
    )
    trade = broker._current_trades[0]
    assert trade.shares == 0
    assert trade.commission == 0
    assert trade.other_fees == 0
    assert trade.unfilled_shares == 10000
    assert trade.reason == "suspended_or_open_unavailable"


def test_limit_up_buy_returns_zero_fill():
    session = date(2024, 1, 2)
    dataset = make_dataset(
        [
            make_bar(
                session,
                "A",
                raw_open="10",
                raw_high="10",
                raw_low="9.9",
                limit_up="10",
            )
        ]
    )
    broker = make_broker()
    broker.begin_day(dataset, session)
    broker.execute_target(
        dataset,
        make_target(session, {"A": 1.0}, signal_date=date(2024, 1, 1)),
        {"A"},
    )
    assert broker._current_trades[0].shares == 0
    assert broker._current_trades[0].reason == "limit_up"


def test_slippage_price_crossing_limit_is_not_clamped_and_filled():
    session = date(2024, 1, 2)
    dataset = make_dataset(
        [
            make_bar(
                session,
                "A",
                raw_open="9.999",
                raw_high="10.1",
                raw_low="9.9",
                limit_up="10",
            )
        ]
    )
    broker = make_broker()
    broker.begin_day(dataset, session)
    broker.execute_target(
        dataset,
        make_target(session, {"A": 1.0}, signal_date=date(2024, 1, 1)),
        {"A"},
    )
    trade = broker._current_trades[0]
    assert trade.shares == 0
    assert trade.reason == "limit_up"
    assert trade.commission == 0


def test_adv_caps_active_fill_and_saves_unfilled_shares():
    session = date(2024, 1, 2)
    dataset = make_dataset(
        [
            make_bar(
                session,
                "A",
                raw_open="10",
                raw_high="10.1",
                adv20_amount="1000000",
            )
        ]
    )
    broker = make_broker(initial_cash="1000000", leverage="10")
    broker.begin_day(dataset, session)
    summary = broker.execute_target(
        dataset,
        make_target(session, {"A": 1.0}, signal_date=date(2024, 1, 1)),
        {"A"},
    )
    trade = broker._current_trades[0]
    # 10% of 1m = 100k notional; at 10.005 that is 9,900 shares.
    assert trade.shares == 9900
    assert trade.unfilled_shares == summary.lines[0].target_shares - 9900
    assert broker.account.positions["A"].size == 9900


def test_t1_blocks_same_day_active_sell():
    session = date(2024, 1, 2)
    dataset = make_dataset([make_bar(session, "A", raw_open="10")], settlement={"A": "T1"})
    broker = make_broker()
    broker.begin_day(dataset, session)
    signal = date(2024, 1, 1)
    broker.execute_target(dataset, make_target(session, {"A": 1.0}, signal_date=signal), {"A"})
    broker.execute_target(
        dataset,
        make_target(session, {}, total_r=0.0, signal_date=signal),
        {"A"},
    )
    blocked = [trade for trade in broker._current_trades if trade.side == "sell"]
    assert blocked
    assert blocked[0].shares == 0
    assert blocked[0].reason == "t1_locked"
    assert broker.account.positions["A"].size > 0


def test_t0_allows_same_day_active_sell():
    session = date(2024, 1, 2)
    dataset = make_dataset([make_bar(session, "A", raw_open="10")], settlement={"A": "T0"})
    broker = make_broker()
    broker.begin_day(dataset, session)
    signal = date(2024, 1, 1)
    broker.execute_target(dataset, make_target(session, {"A": 1.0}, signal_date=signal), {"A"})
    broker.execute_target(
        dataset,
        make_target(session, {}, total_r=0.0, signal_date=signal),
        {"A"},
    )
    sells = [
        trade
        for trade in broker._current_trades
        if trade.side == "sell" and trade.shares > 0
    ]
    assert sells
    assert "A" not in broker.account.positions
    assert broker.account.status == AccountStatus.ACTIVE

