from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from backend.engine.account import AccountState
from backend.engine.liquidation import (
    liquidation_line,
    settle_liquidation_proceeds,
)
from backend.engine.models import AccountStatus, Position
from tests.helpers import D, make_bar, make_broker, make_dataset, make_target


def _forced_account_state(
    broker,
    *,
    position_size: int = 100,
    debt: str = "1000",
    last_price: str = "10",
) -> None:
    broker.account.cash_ledger.balance = Decimal("0")
    broker.account.debt_ledger.balance = D(debt)
    broker.account.positions["A"] = Position(
        code="A",
        name="甲ETF",
        size=position_size,
        locked_size=0,
        last_price=D(last_price),
    )
    broker.last_l_entry = D("2")


def test_synthetic_v1_liquidation_line_hand_calculation():
    assert liquidation_line(1) == D("0.5")
    assert liquidation_line(2) == D("0.25")
    assert liquidation_line(100) == D("0.005")
    assert liquidation_line(400) == D("0.0025")


def test_liquidation_fees_are_deducted_before_bad_debt():
    result = settle_liquidation_proceeds(
        debt_before=D("1000"),
        gross_proceeds=D("1000"),
        sell_fees=D("10"),
        positions_remain=False,
    )
    assert result.repay == D("990")
    assert result.bad_debt == D("10")
    assert result.cash_after == 0
    assert result.debt_after == 0
    assert result.account_status == AccountStatus.TERMINATED_INSOLVENT


def test_solvent_liquidation_returns_surplus_cash():
    result = settle_liquidation_proceeds(
        debt_before=D("500"),
        gross_proceeds=D("1000"),
        sell_fees=D("10"),
        positions_remain=False,
    )
    assert result.repay == D("500")
    assert result.cash_after == D("490")
    assert result.bad_debt == 0
    assert result.account_status == AccountStatus.TERMINATED_LIQUIDATED


def test_broker_records_bad_debt_and_permanently_terminates():
    session = date(2024, 1, 2)
    dataset = make_dataset(
        [
            make_bar(
                session,
                "A",
                raw_open="1.5",
                raw_high="1.6",
                raw_low="1.0",
                raw_close="1.2",
                prev_close="10",
                limit_down="0.5",
                limit_up="20",
                adv20_amount="100000000",
                qfq_close="100",
            )
        ]
    )
    broker = make_broker(initial_cash="1000", leverage="2", interest_rate="0")
    _forced_account_state(broker)
    broker.begin_day(dataset, session)
    result = broker.finish_day(dataset)
    liquidation = result.liquidation
    assert liquidation is not None
    assert liquidation.gross_proceeds == D("100")
    assert liquidation.sell_fees == D("5")
    assert liquidation.net_proceeds == D("95")
    assert liquidation.bad_debt == D("905")
    assert broker.account.status == AccountStatus.TERMINATED_INSOLVENT
    assert broker.account.cash == 0
    assert broker.account.debt == 0
    assert result.snapshot.equity == 0
    assert result.snapshot.cumulative_bad_debt == D("905")
    liquidation_trade = [trade for trade in result.trades if trade.is_liquidation][0]
    assert liquidation_trade.execution_price == D("1.0")
    assert liquidation_trade.gross_amount == D("100")


def test_adv_limits_liquidation_and_keeps_unfilled_position():
    session = date(2024, 1, 2)
    dataset = make_dataset(
        [
            make_bar(
                session,
                "A",
                raw_open="1.5",
                raw_high="1.6",
                raw_low="1.0",
                raw_close="1.2",
                prev_close="10",
                limit_down="0.5",
                limit_up="20",
                adv20_amount="1000",
            )
        ]
    )
    broker = make_broker(
        initial_cash="1000",
        leverage="2",
        interest_rate="0",
        participation="0.10",
    )
    _forced_account_state(broker, position_size=1000, debt="1000")
    broker.account.status = AccountStatus.FORCED_LIQUIDATION
    broker.begin_day(dataset, session)
    result = broker.finish_day(dataset)
    liquidation = result.liquidation
    assert liquidation is not None
    assert [trade.shares for trade in result.trades] == [100]
    assert liquidation.unfilled_shares == 900
    assert broker.account.positions["A"].size == 900
    assert broker.account.status == AccountStatus.FORCED_LIQUIDATION
    assert broker.account.debt == D("905")


def test_suspended_forced_liquidation_can_fill_zero_and_stays_forced():
    session = date(2024, 1, 2)
    dataset = make_dataset(
        [
            make_bar(
                session,
                "A",
                is_suspended=True,
                raw_open="1.5",
                raw_low="1.0",
                raw_close="1.2",
            )
        ]
    )
    broker = make_broker(initial_cash="1000", leverage="2", interest_rate="0")
    _forced_account_state(broker)
    broker.begin_day(dataset, session)
    result = broker.finish_day(dataset)
    assert result.liquidation is not None
    assert result.liquidation.gross_proceeds == 0
    assert result.liquidation.unfilled_shares == 100
    assert broker.account.positions["A"].size == 100
    assert broker.account.status == AccountStatus.FORCED_LIQUIDATION


def test_forced_liquidation_skips_same_day_strategy_rebalance():
    session = date(2024, 1, 2)
    dataset = make_dataset(
        [
            make_bar(
                session,
                "A",
                raw_open="1.5",
                raw_low="1.0",
                raw_close="1.2",
                prev_close="10",
                limit_down="0.5",
                limit_up="20",
            ),
            make_bar(
                session,
                "B",
                raw_open="5",
                raw_low="4.9",
                raw_close="5",
            ),
        ]
    )
    broker = make_broker(initial_cash="1000", leverage="2", interest_rate="0")
    _forced_account_state(broker)
    context = broker.begin_day(dataset, session)
    assert not context.can_rebalance
    summary = broker.execute_target(
        dataset,
        make_target(session, {"B": 1.0}, signal_date=date(2024, 1, 1)),
        {"B"},
    )
    assert summary is None
    assert all(trade.is_liquidation for trade in broker._current_trades)
    assert broker.rejected_targets


def test_terminal_account_rejects_reentry_and_injection_creates_new_path():
    session = date(2024, 1, 2)
    dataset = make_dataset(
        [
            make_bar(
                session,
                "A",
                raw_open="1.5",
                raw_low="1.0",
                raw_close="1.2",
                prev_close="10",
                limit_down="0.5",
                limit_up="20",
            )
        ]
    )
    broker = make_broker(initial_cash="1000", leverage="2", interest_rate="0")
    _forced_account_state(broker)
    broker.begin_day(dataset, session)
    broker.finish_day(dataset)
    original = broker.account
    assert original.status == AccountStatus.TERMINATED_INSOLVENT
    broker.begin_day(dataset, session)
    broker.execute_target(
        dataset,
        make_target(session, {"A": 1.0}, signal_date=date(2024, 1, 1)),
        {"A"},
    )
    assert not [trade for trade in broker._current_trades if not trade.is_liquidation]
    with pytest.raises(PermissionError):
        original.apply_capital_injection(session, D("1000"))
    new_path = AccountState.new_path_from_capital_injection(
        original,
        session,
        "1000",
        path_id="recovery_001",
    )
    assert new_path is not original
    assert new_path.path_id == "recovery_001"
    assert original.status == AccountStatus.TERMINATED_INSOLVENT


def test_l1_without_debt_does_not_liquidate():
    session = date(2024, 1, 2)
    dataset = make_dataset([make_bar(session, "A", raw_open="10", raw_low="9.9")])
    broker = make_broker(initial_cash="100000", leverage="1", interest_rate="0")
    broker.begin_day(dataset, session)
    broker.execute_target(
        dataset,
        make_target(session, {"A": 1.0}, signal_date=date(2024, 1, 1)),
        {"A"},
    )
    result = broker.finish_day(dataset)
    assert result.liquidation is None
    assert broker.account.status == AccountStatus.ACTIVE

