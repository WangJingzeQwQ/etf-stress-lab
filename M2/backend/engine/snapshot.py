from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from backend.engine.account import AccountState
from backend.engine.models import AccountStatus, ZERO


@dataclass(frozen=True)
class Snapshot:
    trading_date: date
    account_status: AccountStatus
    cash: Decimal
    debt: Decimal
    position_value: Decimal
    equity: Decimal
    margin_ratio: Decimal | None
    l_eff_actual: Decimal | None
    positions: list[dict[str, Any]]
    financing_mode: str
    cost_model: str
    cumulative_interest: Decimal
    cumulative_commission: Decimal
    cumulative_other_fees: Decimal
    cumulative_slippage_cost: Decimal
    cumulative_bad_debt: Decimal
    unfilled_order_count: int
    unfilled_orders: list[dict[str, Any]]
    daily_commission: Decimal
    daily_other_fees: Decimal
    daily_slippage_cost: Decimal
    daily_interest: Decimal = ZERO
    pv_target: Decimal | None = None
    pv_actual: Decimal | None = None
    pv_target_deviation: Decimal | None = None


def build_snapshot(
    *,
    trading_date: date,
    account: AccountState,
    prices: dict[str, Decimal],
    financing_mode: str,
    cost_model: str,
    trades: list[Any] | None = None,
    daily_interest: Decimal = ZERO,
    pv_target: Decimal | None = None,
    pv_actual: Decimal | None = None,
) -> Snapshot:
    market_value = account.all_positions_value(prices)
    positions = []
    for code in sorted(account.positions):
        position = account.positions[code]
        price = prices.get(code, position.last_price)
        positions.append(
            {
                "code": code,
                "name": position.name,
                "size": position.size,
                "locked_size": position.locked_size,
                "price": price,
                "value": position.market_value(price),
                "average_cost": position.average_cost,
            }
        )
    total_commission = account.total_commission
    total_other = account.total_other_fees
    total_slippage = account.total_slippage_cost
    trades = trades or []
    daily_commission = sum((trade.commission for trade in trades), ZERO)
    daily_other = sum((trade.other_fees for trade in trades), ZERO)
    daily_slippage = sum((trade.slippage_cost for trade in trades), ZERO)
    unfilled_orders = [
        {
            "code": trade.code,
            "name": trade.name,
            "side": trade.side,
            "unfilled_shares": trade.unfilled_shares,
            "reason": trade.reason,
            "is_liquidation": trade.is_liquidation,
        }
        for trade in trades
        if trade.unfilled_shares > 0
    ]
    return Snapshot(
        trading_date=trading_date,
        account_status=account.status,
        cash=account.cash,
        debt=account.debt,
        position_value=market_value,
        equity=account.equity(prices),
        margin_ratio=(
            account.equity(prices) / market_value if market_value > ZERO else None
        ),
        l_eff_actual=(
            market_value / account.equity(prices)
            if market_value > ZERO and account.equity(prices) > ZERO
            else None
        ),
        positions=positions,
        financing_mode=financing_mode,
        cost_model=cost_model,
        cumulative_interest=account.total_interest_accrued,
        cumulative_commission=total_commission,
        cumulative_other_fees=total_other,
        cumulative_slippage_cost=total_slippage,
        cumulative_bad_debt=account.total_bad_debt,
        unfilled_order_count=account.unfilled_order_count,
        unfilled_orders=unfilled_orders,
        daily_commission=daily_commission,
        daily_other_fees=daily_other,
        daily_slippage_cost=daily_slippage,
        daily_interest=daily_interest,
        pv_target=pv_target,
        pv_actual=pv_actual,
        pv_target_deviation=(
            pv_target - pv_actual
            if pv_target is not None and pv_actual is not None
            else None
        ),
    )

