from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_FLOOR

from backend.data_pipeline.models import MarketBar, TradingRule
from backend.engine.cost_model import CostModel
from backend.engine.models import ZERO


@dataclass(frozen=True)
class ExecutionQuote:
    tradable: bool
    reference_price: Decimal
    execution_price: Decimal
    adv_cap_shares: int
    reason: str


@dataclass(frozen=True)
class ExecutionModel:
    execution_model: str
    max_participation_rate: Decimal
    adv_window: int
    lot_size: int

    def quote(
        self,
        bar: MarketBar,
        side: str,
        cost_model: CostModel,
        rule: TradingRule | None,
        *,
        is_liquidation: bool = False,
    ) -> ExecutionQuote:
        reference = bar.raw_low if is_liquidation else bar.raw_open
        if rule is None:
            return ExecutionQuote(False, reference, ZERO, 0, "missing_trading_rule")
        if bar.is_suspended or not bar.open_tradable:
            return ExecutionQuote(False, reference, ZERO, 0, "suspended_or_open_unavailable")
        if reference <= ZERO:
            return ExecutionQuote(False, reference, ZERO, 0, "invalid_reference_price")
        if bar.adv20_amount is None:
            return ExecutionQuote(False, reference, ZERO, 0, "missing_t_adv20")

        if is_liquidation:
            execution_price = reference
            if side == "sell" and bar.raw_low <= bar.limit_down:
                return ExecutionQuote(False, reference, ZERO, 0, "limit_down")
        else:
            adjustment = cost_model.slippage_bps(side) / Decimal("10000")
            execution_price = (
                reference * (Decimal("1") + adjustment)
                if side == "buy"
                else reference * (Decimal("1") - adjustment)
            )
            if side == "buy":
                if bar.raw_open >= bar.limit_up or execution_price > bar.limit_up:
                    return ExecutionQuote(False, reference, ZERO, 0, "limit_up")
                if execution_price > bar.raw_high:
                    return ExecutionQuote(False, reference, ZERO, 0, "buy_price_outside_high")
            elif side == "sell":
                if bar.raw_open <= bar.limit_down or execution_price < bar.limit_down:
                    return ExecutionQuote(False, reference, ZERO, 0, "limit_down")
                if execution_price < bar.raw_low:
                    return ExecutionQuote(False, reference, ZERO, 0, "sell_price_outside_low")
            else:
                raise ValueError(f"Unknown side: {side}")

        max_notional = bar.adv20_amount * self.max_participation_rate
        raw_cap = (max_notional / execution_price).to_integral_value(
            rounding=ROUND_FLOOR
        )
        lot = Decimal(self.lot_size)
        cap_shares = int((raw_cap // lot) * lot)
        if cap_shares <= 0:
            return ExecutionQuote(False, reference, execution_price, 0, "adv_cap_zero")
        return ExecutionQuote(True, reference, execution_price, cap_shares, "tradable")

    def metadata(self) -> dict[str, object]:
        return {
            "execution_model": self.execution_model,
            "max_participation_rate": float(self.max_participation_rate),
            "adv_window": self.adv_window,
            "lot_size": self.lot_size,
            "liquidity_barrier_uses": "T-1",
        }

