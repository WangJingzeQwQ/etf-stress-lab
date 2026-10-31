from __future__ import annotations

from decimal import Decimal

from backend.engine.models import ZERO, decimal_value


class CashLedger:
    """Cash is a non-negative integer-cent-backed Decimal ledger."""

    def __init__(self, opening_balance: Decimal | int | str) -> None:
        self.balance = decimal_value(opening_balance)
        if self.balance < ZERO:
            raise ValueError("Opening cash cannot be negative")

    def apply(self, delta: Decimal | int | str) -> None:
        self.balance += decimal_value(delta)
        if self.balance < ZERO:
            raise AssertionError(f"Cash ledger would become negative: {self.balance}")


class DebtLedger:
    """Debt changes only through explicit financing, repayment, or capitalization."""

    def __init__(self) -> None:
        self.balance = ZERO

    def apply(self, delta: Decimal | int | str) -> None:
        self.balance += decimal_value(delta)
        if self.balance < ZERO:
            raise AssertionError(f"Debt ledger would become negative: {self.balance}")


class FeeLedger:
    def __init__(self) -> None:
        self.commission = ZERO
        self.other_fees = ZERO
        self.slippage_cost = ZERO
        self.financing_interest = ZERO

    def add(
        self,
        commission: Decimal = ZERO,
        other_fees: Decimal = ZERO,
        slippage_cost: Decimal = ZERO,
    ) -> None:
        self.commission += commission
        self.other_fees += other_fees
        self.slippage_cost += slippage_cost

