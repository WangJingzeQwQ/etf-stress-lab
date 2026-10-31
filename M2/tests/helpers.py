from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Iterable

from backend.data_pipeline.models import MarketBar, MarketDataset, TradingRule
from backend.engine.broker_ext import Broker, BrokerConfig
from backend.engine.cost_model import CostModel
from backend.engine.execution import ExecutionModel
from backend.engine.models import decimal_value
from backend.engine.target_portfolio import TargetPortfolio


D = decimal_value


def make_bar(
    trading_date: date,
    code: str,
    *,
    name: str | None = None,
    raw_open: str | float = "10.00",
    raw_high: str | float | None = None,
    raw_low: str | float | None = None,
    raw_close: str | float | None = None,
    prev_close: str | float | None = None,
    amount: str | float = "10000000",
    adv20_amount: str | float | None = "100000000",
    is_suspended: bool = False,
    open_tradable: bool = True,
    total_return_close: str | float | None = None,
    qfq_close: str | float | None = "0.37",
    limit_up: str | float = "100",
    limit_down: str | float = "0.01",
) -> MarketBar:
    open_value = D(raw_open)
    close_value = D(raw_close if raw_close is not None else raw_open)
    high_value = D(
        raw_high
        if raw_high is not None
        else max(open_value, close_value) * Decimal("1.01")
    )
    low_value = D(
        raw_low
        if raw_low is not None
        else min(open_value, close_value) * Decimal("0.99")
    )
    previous = D(prev_close if prev_close is not None else open_value)
    return MarketBar(
        code=code,
        name=name or f"{code}测试ETF",
        trading_date=trading_date,
        raw_open=open_value,
        raw_high=high_value,
        raw_low=low_value,
        raw_close=close_value,
        volume=(
            D(amount) / close_value if close_value > 0 else Decimal("0")
        ),
        amount=D(amount),
        prev_close=previous,
        limit_up=D(limit_up),
        limit_down=D(limit_down),
        adv20_amount=None if adv20_amount is None else D(adv20_amount),
        is_suspended=is_suspended,
        open_tradable=open_tradable,
        total_return_close=D(
            total_return_close
            if total_return_close is not None
            else close_value
        ),
        factor_effective_date=trading_date,
        factor_known_at=trading_date,
        provider_qfq_close=D(qfq_close) if qfq_close is not None else None,
    )


def make_rule(
    code: str,
    settlement: str = "T1",
    *,
    start: date = date(2020, 1, 1),
    end: date = date(2099, 12, 31),
    limit_pct: str = "0.10",
) -> TradingRule:
    return TradingRule(
        code=code,
        settlement=settlement,
        effective_from=start,
        effective_to=end,
        limit_pct=D(limit_pct),
    )


def make_dataset(
    bars: Iterable[MarketBar],
    *,
    settlement: dict[str, str] | None = None,
    source_kind: str = "TEST_FIXTURE",
) -> MarketDataset:
    rows = list(bars)
    codes = sorted({bar.code for bar in rows})
    names = {bar.code: bar.name for bar in rows}
    settlement = settlement or {code: "T1" for code in codes}
    return MarketDataset.build(
        rows,
        names=names,
        list_dates={code: date(2020, 1, 1) for code in codes},
        trading_rules=[make_rule(code, settlement[code]) for code in codes],
        source_kind=source_kind,
        metadata={"warning": "TEST_FIXTURE"},
    )


def make_cost(
    *,
    commission_rate: str = "0.0003",
    minimum_commission: str = "5",
    exchange_fee: str = "0",
    stamp_duty: str = "0",
    fixed_other: str = "0",
    buy_bps: str = "5",
    sell_bps: str = "5",
    label: str = "parameterized_etf_v1",
) -> CostModel:
    return CostModel.from_mapping(
        {
            "cost_model": label,
            "effective_from": "2020-01-01",
            "effective_to": "2099-12-31",
            "commission_rate": commission_rate,
            "minimum_commission_per_order": minimum_commission,
            "exchange_fee_rate": exchange_fee,
            "stamp_duty_rate": stamp_duty,
            "fixed_other_fee": fixed_other,
            "buy_slippage_bps": buy_bps,
            "sell_slippage_bps": sell_bps,
        }
    )


def make_broker(
    *,
    initial_cash: str = "100000",
    leverage: str = "1",
    cost: CostModel | None = None,
    participation: str = "0.10",
    interest_rate: str = "0.06",
    lot_size: int = 100,
    capital_base_mode: str = "initial_equity",
) -> Broker:
    broker_config = BrokerConfig.from_mapping(
        {
            "initial_cash": initial_cash,
            "leverage": leverage,
            "capital_base_mode": capital_base_mode,
            "financing_mode": "synthetic_stress",
            "reentry_policy": "first_liquidation_terminates",
            "annual_interest_rate": interest_rate,
            "interest_day_count": 365,
            "maintenance_fraction": "0.50",
            "maintenance_floor": "0.0025",
            "lot_size": lot_size,
            "p0_1_frozen": True,
            "liquidation_model": "synthetic_v1",
        }
    )
    execution_model = ExecutionModel(
        execution_model="adv_limit_v1",
        max_participation_rate=D(participation),
        adv_window=20,
        lot_size=lot_size,
    )
    return Broker(broker_config, cost or make_cost(), execution_model)


def make_target(
    execution_date: date,
    weights: dict[str, float] | None = None,
    *,
    total_r: float = 1.0,
    signal_date: date | None = None,
    group: str = "G1",
) -> TargetPortfolio:
    weights = {} if weights is None else weights
    signal = signal_date or date.fromordinal(execution_date.toordinal() - 1)
    return TargetPortfolio(
        total_r=total_r,
        weights=weights,
        signal_date=signal,
        execution_date=execution_date,
        data_cutoff=signal,
        group=group,
        meta={"test_fixture": True},
    )

