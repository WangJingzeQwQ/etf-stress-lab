from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any
import math


ZERO = Decimal("0")
ONE = Decimal("1")


def decimal_value(value: Decimal | int | float | str | None) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("Boolean values are not valid monetary amounts")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid decimal value: {value!r}") from exc


def finite_float(value: float | int) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Value must be finite")
    return result


class AccountStatus(str, Enum):
    ACTIVE = "ACTIVE"
    FORCED_LIQUIDATION = "FORCED_LIQUIDATION"
    TERMINATED_LIQUIDATED = "TERMINATED_LIQUIDATED"
    TERMINATED_INSOLVENT = "TERMINATED_INSOLVENT"

    @property
    def is_terminal(self) -> bool:
        return self in {
            AccountStatus.TERMINATED_LIQUIDATED,
            AccountStatus.TERMINATED_INSOLVENT,
        }


@dataclass
class Position:
    code: str
    name: str
    size: int = 0
    locked_size: int = 0
    locked_until: date | None = None
    average_cost: Decimal = ZERO
    last_price: Decimal = ZERO

    def unlock_at_start_of(self, current_date: date) -> None:
        if self.locked_until is not None and self.locked_until <= current_date:
            self.locked_size = 0
            self.locked_until = None

    @property
    def available_size(self) -> int:
        return max(0, self.size - self.locked_size)

    def market_value(self, price: Decimal | None = None) -> Decimal:
        mark = self.last_price if price is None else price
        return decimal_value(self.size) * mark

    def validate(self) -> None:
        if self.size < 0:
            raise AssertionError(f"Negative position size for {self.code}")
        if self.locked_size < 0 or self.locked_size > self.size:
            raise AssertionError(
                f"Invalid locked size for {self.code}: "
                f"locked={self.locked_size}, size={self.size}"
            )


@dataclass(frozen=True)
class FeeBreakdown:
    commission: Decimal = ZERO
    other_fees: Decimal = ZERO
    minimum_commission_triggered: bool = False

    @property
    def total(self) -> Decimal:
        return self.commission + self.other_fees


@dataclass(frozen=True)
class AuditEvent:
    sequence: int
    event_date: date
    event_type: str
    cash_delta: Decimal
    debt_delta: Decimal
    position_delta: int
    cash_after: Decimal
    debt_after: Decimal
    code: str | None = None
    side: str | None = None
    commission: Decimal = ZERO
    other_fees: Decimal = ZERO
    slippage_cost: Decimal = ZERO
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TradeExecution:
    signal_date: date
    execution_date: date
    code: str
    name: str
    side: str
    reference_price: Decimal
    execution_price: Decimal
    shares: int
    gross_amount: Decimal
    commission: Decimal
    other_fees: Decimal
    slippage_cost: Decimal
    net_cash_flow: Decimal
    unfilled_shares: int
    reason: str
    is_liquidation: bool = False
    account_status: AccountStatus = AccountStatus.ACTIVE
    borrowed_amount: Decimal = ZERO
    debt_repaid: Decimal = ZERO


@dataclass(frozen=True)
class LiquidationSummary:
    trigger_date: date
    liquidation_line: Decimal
    margin_ratio_open: Decimal
    gross_proceeds: Decimal
    sell_fees: Decimal
    net_proceeds: Decimal
    repay: Decimal
    bad_debt: Decimal
    unfilled_shares: int
    positions: list[dict[str, Any]]
    account_status: AccountStatus


def position_value(positions: dict[str, Position], prices: dict[str, Decimal]) -> Decimal:
    total = ZERO
    for code, position in positions.items():
        total += position.market_value(prices.get(code))
    return total

