from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from backend.engine.models import FeeBreakdown, ZERO, decimal_value, finite_float


CENT = Decimal("0.01")


def money_round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class CostModel:
    cost_model: str
    effective_from: date
    effective_to: date
    commission_rate: Decimal
    minimum_commission_per_order: Decimal
    exchange_fee_rate: Decimal
    stamp_duty_rate: Decimal
    fixed_other_fee: Decimal
    buy_slippage_bps: Decimal
    sell_slippage_bps: Decimal

    @classmethod
    def from_mapping(
        cls,
        payload: dict[str, Any],
        *,
        label_override: str | None = None,
    ) -> "CostModel":
        required = {
            "cost_model",
            "effective_from",
            "effective_to",
            "commission_rate",
            "minimum_commission_per_order",
            "exchange_fee_rate",
            "stamp_duty_rate",
            "fixed_other_fee",
            "buy_slippage_bps",
            "sell_slippage_bps",
        }
        missing = sorted(required - set(payload))
        if missing:
            raise ValueError(f"Cost configuration missing fields: {missing}")
        model = cls(
            cost_model=label_override or str(payload["cost_model"]),
            effective_from=date.fromisoformat(str(payload["effective_from"])),
            effective_to=date.fromisoformat(str(payload["effective_to"])),
            commission_rate=decimal_value(payload["commission_rate"]),
            minimum_commission_per_order=decimal_value(
                payload["minimum_commission_per_order"]
            ),
            exchange_fee_rate=decimal_value(payload["exchange_fee_rate"]),
            stamp_duty_rate=decimal_value(payload["stamp_duty_rate"]),
            fixed_other_fee=decimal_value(payload["fixed_other_fee"]),
            buy_slippage_bps=decimal_value(payload["buy_slippage_bps"]),
            sell_slippage_bps=decimal_value(payload["sell_slippage_bps"]),
        )
        model.validate()
        return model

    def validate(self) -> None:
        if self.effective_from > self.effective_to:
            raise ValueError("Cost effective_from is after effective_to")
        for name in (
            "commission_rate",
            "minimum_commission_per_order",
            "exchange_fee_rate",
            "stamp_duty_rate",
            "fixed_other_fee",
            "buy_slippage_bps",
            "sell_slippage_bps",
        ):
            if getattr(self, name) < ZERO:
                raise ValueError(f"{name} cannot be negative")

    def assert_effective(self, trading_date: date) -> None:
        if not self.effective_from <= trading_date <= self.effective_to:
            raise ValueError(
                f"No cost schedule effective on {trading_date.isoformat()}"
            )

    def fees(self, side: str, gross_amount: Decimal, shares: int) -> FeeBreakdown:
        if shares <= 0 or gross_amount <= ZERO:
            return FeeBreakdown()
        proportional_commission = gross_amount * self.commission_rate
        commission = max(proportional_commission, self.minimum_commission_per_order)
        commission_triggered = self.minimum_commission_per_order > proportional_commission
        other = (
            gross_amount * self.exchange_fee_rate
            + gross_amount * self.stamp_duty_rate
            + self.fixed_other_fee
        )
        return FeeBreakdown(
            commission=money_round(commission),
            other_fees=money_round(other),
            minimum_commission_triggered=commission_triggered,
        )

    def slippage_bps(self, side: str) -> Decimal:
        if side == "buy":
            return self.buy_slippage_bps
        if side == "sell":
            return self.sell_slippage_bps
        raise ValueError(f"Unknown side: {side}")

    def metadata(self) -> dict[str, Any]:
        return {
            "cost_model": self.cost_model,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat(),
            "commission_rate": finite_float(self.commission_rate),
            "minimum_commission_per_order": finite_float(
                self.minimum_commission_per_order
            ),
            "exchange_fee_rate": finite_float(self.exchange_fee_rate),
            "stamp_duty_rate": finite_float(self.stamp_duty_rate),
            "fixed_other_fee": finite_float(self.fixed_other_fee),
            "buy_slippage_bps": finite_float(self.buy_slippage_bps),
            "sell_slippage_bps": finite_float(self.sell_slippage_bps),
        }

