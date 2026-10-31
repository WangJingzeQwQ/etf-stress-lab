from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from backend.engine.ledger import CashLedger, DebtLedger
from backend.engine.models import (
    AccountStatus,
    AuditEvent,
    Position,
    ZERO,
    decimal_value,
    position_value,
)


@dataclass(frozen=True)
class InterestCharge:
    base_debt: Decimal
    accrued: Decimal
    paid_from_cash: Decimal
    capitalized: Decimal


@dataclass
class AccountState:
    initial_cash: Decimal
    cash_ledger: CashLedger
    debt_ledger: DebtLedger = field(default_factory=DebtLedger)
    positions: dict[str, Position] = field(default_factory=dict)
    status: AccountStatus = AccountStatus.ACTIVE
    audit_events: list[AuditEvent] = field(default_factory=list)
    first_liquidation_date: date | None = None
    first_liquidation_day_index: int | None = None
    path_id: str = "primary"
    total_bad_debt: Decimal = ZERO
    total_capital_injection: Decimal = ZERO
    total_interest_accrued: Decimal = ZERO
    total_interest_capitalized: Decimal = ZERO
    minimum_fee_order_count: int = 0
    unfilled_order_count: int = 0
    total_commission: Decimal = ZERO
    total_other_fees: Decimal = ZERO
    total_slippage_cost: Decimal = ZERO

    @classmethod
    def open(cls, initial_cash: Decimal | int | str) -> "AccountState":
        opening = decimal_value(initial_cash)
        return cls(initial_cash=opening, cash_ledger=CashLedger(opening))

    @property
    def cash(self) -> Decimal:
        return self.cash_ledger.balance

    @property
    def debt(self) -> Decimal:
        return self.debt_ledger.balance

    def unlock_at_start_of(self, current_date: date) -> None:
        for code in sorted(self.positions):
            position = self.positions[code]
            locked_before = position.locked_size
            unlock_until = position.locked_until
            position.unlock_at_start_of(current_date)
            if locked_before != position.locked_size:
                self.record_event(
                    event_date=current_date,
                    event_type="t1_unlock",
                    cash_delta=ZERO,
                    debt_delta=ZERO,
                    position_delta=0,
                    code=code,
                    details={
                        "locked_before": locked_before,
                        "locked_after": position.locked_size,
                        "locked_until_before": (
                            unlock_until.isoformat() if unlock_until else None
                        ),
                    },
                )

    def record_event(
        self,
        *,
        event_date: date,
        event_type: str,
        cash_delta: Decimal,
        debt_delta: Decimal,
        position_delta: int,
        code: str | None = None,
        side: str | None = None,
        commission: Decimal = ZERO,
        other_fees: Decimal = ZERO,
        slippage_cost: Decimal = ZERO,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            sequence=len(self.audit_events) + 1,
            event_date=event_date,
            event_type=event_type,
            cash_delta=cash_delta,
            debt_delta=debt_delta,
            position_delta=position_delta,
            cash_after=self.cash,
            debt_after=self.debt,
            code=code,
            side=side,
            commission=commission,
            other_fees=other_fees,
            slippage_cost=slippage_cost,
            details=dict(details or {}),
        )
        self.audit_events.append(event)
        self.total_commission += commission
        self.total_other_fees += other_fees
        self.total_slippage_cost += slippage_cost
        return event

    def apply_interest(self, current_date: date, rate: Decimal, day_count: int) -> InterestCharge:
        base_debt = self.debt
        accrued = base_debt * rate / decimal_value(day_count)
        if accrued <= ZERO:
            self.record_event(
                event_date=current_date,
                event_type="financing_interest",
                cash_delta=ZERO,
                debt_delta=ZERO,
                position_delta=0,
                details={
                    "base_debt": str(base_debt),
                    "accrued": "0",
                    "capitalized": "0",
                },
            )
            return InterestCharge(base_debt, ZERO, ZERO, ZERO)
        paid = min(self.cash, accrued)
        capitalized = accrued - paid
        self.cash_ledger.apply(-paid)
        self.debt_ledger.apply(capitalized)
        self.total_interest_accrued += accrued
        self.total_interest_capitalized += capitalized
        self.record_event(
            event_date=current_date,
            event_type="financing_interest",
            cash_delta=-paid,
            debt_delta=capitalized,
            position_delta=0,
            details={
                "base_debt": str(base_debt),
                "annual_rate": str(rate),
                "day_count": day_count,
                "accrued": str(accrued),
                "paid_from_cash": str(paid),
                "capitalized": str(capitalized),
            },
        )
        return InterestCharge(base_debt, accrued, paid, capitalized)

    def apply_position_delta(
        self,
        *,
        code: str,
        name: str,
        delta: int,
        price: Decimal,
        locked_until: date | None = None,
        average_cost_hint: Decimal | None = None,
    ) -> Position:
        position = self.positions.get(code)
        if position is None:
            if delta < 0:
                raise AssertionError(f"Cannot sell missing position {code}")
            position = Position(code=code, name=name)
            self.positions[code] = position
        old_size = position.size
        new_size = old_size + delta
        if new_size < 0:
            raise AssertionError(f"Position {code} would become negative")
        if delta > 0:
            prior_cost = position.average_cost * decimal_value(old_size)
            added_cost = (
                average_cost_hint
                if average_cost_hint is not None
                else price * decimal_value(delta)
            )
            position.average_cost = (
                (prior_cost + added_cost) / decimal_value(new_size)
                if new_size
                else ZERO
            )
            if locked_until is not None:
                position.locked_size += delta
                position.locked_until = locked_until
        elif delta < 0:
            position.locked_size = min(position.locked_size, new_size)
        position.size = new_size
        position.last_price = price
        position.name = name
        if position.size == 0:
            self.positions.pop(code, None)
        else:
            position.validate()
        return position

    def mark_price(self, code: str, price: Decimal) -> None:
        if code in self.positions:
            self.positions[code].last_price = price

    def all_positions_value(self, prices: dict[str, Decimal] | None = None) -> Decimal:
        return position_value(self.positions, prices or {})

    def equity(self, prices: dict[str, Decimal] | None = None) -> Decimal:
        return self.cash + self.all_positions_value(prices) - self.debt

    def margin_ratio(self, prices: dict[str, Decimal] | None = None) -> Decimal | None:
        value = self.all_positions_value(prices)
        if value <= ZERO:
            return None
        return self.equity(prices) / value

    def effective_leverage(
        self, prices: dict[str, Decimal] | None = None
    ) -> Decimal | None:
        value = self.all_positions_value(prices)
        equity = self.equity(prices)
        if value <= ZERO or equity <= ZERO:
            return None
        return value / equity

    @classmethod
    def new_path_from_capital_injection(
        cls,
        terminal_account: "AccountState",
        current_date: date,
        amount: Decimal | int | str,
        *,
        path_id: str,
    ) -> "AccountState":
        if not terminal_account.status.is_terminal:
            raise PermissionError("Capital injection requires a terminal source path")
        if not path_id or path_id == terminal_account.path_id:
            raise ValueError("Capital injection requires a distinct path_id")
        injection = decimal_value(amount)
        if injection <= ZERO:
            raise ValueError("Capital injection must be positive")
        new_account = cls.open(injection)
        new_account.path_id = path_id
        new_account.total_capital_injection = injection
        new_account.record_event(
            event_date=current_date,
            event_type="capital_injection",
            cash_delta=injection,
            debt_delta=ZERO,
            position_delta=0,
            details={"new_path": True, "source_path_id": terminal_account.path_id},
        )
        return new_account

    def apply_capital_injection(self, current_date: date, amount: Decimal) -> None:
        raise PermissionError(
            "External capital injection must create a separate AccountState path"
        )

    def assert_consistent(self, prices: dict[str, Decimal] | None = None) -> None:
        if self.cash < ZERO:
            raise AssertionError("Cash ledger is negative")
        if self.debt < ZERO:
            raise AssertionError("Debt ledger is negative")
        for position in self.positions.values():
            position.validate()
        prices = prices or {}
        computed = self.cash + self.all_positions_value(prices) - self.debt
        if computed != self.equity(prices):
            raise AssertionError("equity != cash + position_value - debt")

