from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from backend.engine.models import AccountStatus, ZERO


def liquidation_line(
    l_entry: Decimal | float | int | str,
    *,
    maintenance_fraction: Decimal | float | int | str = Decimal("0.50"),
    maintenance_floor: Decimal | float | int | str = Decimal("0.0025"),
) -> Decimal:
    from backend.engine.models import decimal_value

    leverage = decimal_value(l_entry)
    floor = decimal_value(maintenance_floor)
    fraction = decimal_value(maintenance_fraction)
    if leverage <= ZERO:
        raise ValueError("L_entry must be positive")
    if floor < ZERO or fraction < ZERO:
        raise ValueError("Liquidation parameters cannot be negative")
    return max(floor, fraction / leverage)


@dataclass(frozen=True)
class LiquidationSettlement:
    repay: Decimal
    debt_after: Decimal
    cash_after: Decimal
    bad_debt: Decimal
    account_status: AccountStatus


def settle_liquidation_proceeds(
    *,
    debt_before: Decimal,
    gross_proceeds: Decimal,
    sell_fees: Decimal,
    positions_remain: bool,
) -> LiquidationSettlement:
    if debt_before < ZERO or gross_proceeds < ZERO or sell_fees < ZERO:
        raise ValueError("Liquidation amounts cannot be negative")
    net_proceeds = gross_proceeds - sell_fees
    if net_proceeds < ZERO:
        net_proceeds = ZERO
    repay = min(net_proceeds, debt_before)
    debt_after = debt_before - repay
    cash_after = net_proceeds - repay
    bad_debt = ZERO
    if not positions_remain and debt_after > ZERO:
        bad_debt = debt_after
        return LiquidationSettlement(
            repay=repay,
            debt_after=ZERO,
            cash_after=ZERO,
            bad_debt=bad_debt,
            account_status=AccountStatus.TERMINATED_INSOLVENT,
        )
    if not positions_remain:
        return LiquidationSettlement(
            repay=repay,
            debt_after=ZERO,
            cash_after=cash_after,
            bad_debt=ZERO,
            account_status=AccountStatus.TERMINATED_LIQUIDATED,
        )
    return LiquidationSettlement(
        repay=repay,
        debt_after=debt_after,
        cash_after=cash_after,
        bad_debt=ZERO,
        account_status=AccountStatus.FORCED_LIQUIDATION,
    )

