from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import math
import random

from backend.data_pipeline.models import MarketBar, MarketDataset, TradingRule


FIXTURE_KIND = "SYNTHETIC_FIXTURE"
PRICE_TICK = Decimal("0.001")


FIXTURE_ETFS = (
    ("510300", "沪深300ETF", "3.80", "0.00030", "0.0050", 0.3),
    ("510500", "中证500ETF", "5.20", "0.00055", "0.0080", 1.2),
    ("510050", "上证50ETF", "2.60", "0.00018", "0.0040", 2.1),
    ("159915", "创业板ETF", "2.00", "0.00075", "0.0110", 2.9),
    ("512100", "中证1000ETF", "2.60", "0.00062", "0.0100", 4.1),
    ("511010", "国债ETF", "100.00", "0.00003", "0.0010", 5.2),
)


def business_days(start: date, end: date) -> tuple[date, ...]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return tuple(days)


def _tick(value: Decimal) -> Decimal:
    return value.quantize(PRICE_TICK, rounding=ROUND_HALF_UP)


def build_synthetic_fixture(
    start: date,
    end: date,
    *,
    seed: int = 20261029,
) -> MarketDataset:
    """Build a deterministic fixture. It is not a real-market calendar or result."""

    sessions = business_days(start, end)
    if len(sessions) < 2:
        raise ValueError("Fixture requires at least two business days")
    bars: list[MarketBar] = []
    names: dict[str, str] = {}
    list_dates: dict[str, date] = {}
    rules: list[TradingRule] = []

    for index, (code, name, base_text, drift_text, amplitude_text, phase) in enumerate(
        FIXTURE_ETFS
    ):
        names[code] = name
        list_dates[code] = date(2020, 1, 2)
        rules.append(
            TradingRule(
                code=code,
                settlement="T0" if code == "511010" else "T1",
                effective_from=date(2020, 1, 2),
                effective_to=date(2099, 12, 31),
                limit_pct=Decimal("0.10"),
            )
        )
        rng = random.Random(seed + index * 104_729)
        base = Decimal(base_text)
        drift = Decimal(drift_text)
        amplitude = Decimal(amplitude_text)
        previous_close = base
        amount_history: list[Decimal] = []
        for session_index, trading_date in enumerate(sessions):
            cycle = Decimal(
                str(
                    amplitude
                    * Decimal(
                        str(
                            math.sin(
                                (session_index + phase * 7.0) / 13.0
                            )
                        )
                    )
                )
            )
            noise = Decimal(str(rng.uniform(-0.0012, 0.0012)))
            daily_return = drift + cycle + noise
            raw_close = _tick(previous_close * (Decimal("1") + daily_return))
            gap = Decimal(
                str(math.sin((session_index + phase) / 5.0) * 0.0015)
            )
            raw_open = _tick(previous_close * (Decimal("1") + gap))
            raw_high = _tick(max(raw_open, raw_close) * Decimal("1.004"))
            raw_low = _tick(min(raw_open, raw_close) * Decimal("0.996"))
            amount = Decimal("65000000") + Decimal(index * 5_000_000) + Decimal(
                str(round((math.sin(session_index / 9.0 + phase) + 1) * 4_000_000, 2))
            )
            volume = (amount / raw_close).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            window = amount_history[-20:]
            adv20 = (
                sum(window, Decimal("0")) / Decimal(len(window))
                if window
                else None
            )
            limit_up = _tick(previous_close * Decimal("1.10"))
            limit_down = _tick(previous_close * Decimal("0.90"))
            # Purposefully different provider qfq: execution must never use it.
            provider_qfq = _tick(raw_close * Decimal("0.37"))
            bars.append(
                MarketBar(
                    code=code,
                    name=name,
                    trading_date=trading_date,
                    raw_open=raw_open,
                    raw_high=raw_high,
                    raw_low=raw_low,
                    raw_close=raw_close,
                    volume=volume,
                    amount=amount,
                    prev_close=previous_close,
                    limit_up=limit_up,
                    limit_down=limit_down,
                    adv20_amount=adv20,
                    total_return_close=raw_close,
                    factor_effective_date=trading_date,
                    factor_known_at=trading_date,
                    provider_qfq_close=provider_qfq,
                )
            )
            amount_history.append(amount)
            previous_close = raw_close

    return MarketDataset.build(
        bars,
        names=names,
        list_dates=list_dates,
        trading_rules=rules,
        source_kind=FIXTURE_KIND,
        metadata={
            "fixture_id": "m0_deterministic_v1",
            "fixture_seed": seed,
            "calendar": "weekday_only_synthetic",
            "warning": (
                "SYNTHETIC_FIXTURE: prices, calendar, NAV, names and liquidity "
                "are engineering fixtures, not real market conclusions."
            ),
        },
    )

