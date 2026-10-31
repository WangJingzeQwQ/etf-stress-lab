from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_FLOOR
from typing import Any

from backend.data_pipeline.models import MarketDataset


@dataclass(frozen=True)
class FeatureSnapshot:
    data_cutoff: date
    universe: tuple[str, ...]
    momentum: dict[str, float]
    visible_sessions: tuple[date, ...]
    adv20: dict[str, Decimal]
    names: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "data_cutoff": self.data_cutoff.isoformat(),
            "universe": list(self.universe),
            "momentum": dict(sorted(self.momentum.items())),
            "visible_sessions": [item.isoformat() for item in self.visible_sessions],
            "adv20": {code: str(value) for code, value in sorted(self.adv20.items())},
        }


def eligible_universe(
    dataset: MarketDataset,
    data_cutoff: date,
    *,
    min_listed_sessions: int,
    adv_window: int,
    min_adv_amount: Decimal,
) -> tuple[str, ...]:
    sessions = dataset.sessions_through(data_cutoff)
    eligible: list[str] = []
    for code in dataset.codes:
        visible_bars = [
            dataset.bars[(session, code)]
            for session in sessions
            if (session, code) in dataset.bars
        ]
        if len(visible_bars) < min_listed_sessions:
            continue
        try:
            rule = dataset.rule_for(code, data_cutoff)
        except ValueError:
            rule = None
        if rule is None:
            continue
        amounts = [bar.amount for bar in visible_bars[-adv_window:]]
        if (
            not amounts
            or sum(amounts, Decimal("0")) / Decimal(len(amounts))
            < min_adv_amount
        ):
            continue
        eligible.append(code)
    return tuple(sorted(eligible))


def point_in_time_features(
    dataset: MarketDataset,
    data_cutoff: date,
    *,
    min_listed_sessions: int,
    adv_window: int,
    min_adv_amount: Decimal,
    momentum_lookback_sessions: int,
) -> FeatureSnapshot:
    sessions = dataset.sessions_through(data_cutoff)
    universe = eligible_universe(
        dataset,
        data_cutoff,
        min_listed_sessions=min_listed_sessions,
        adv_window=adv_window,
        min_adv_amount=min_adv_amount,
    )
    momentum: dict[str, float] = {}
    adv20: dict[str, Decimal] = {}
    required = momentum_lookback_sessions + 1
    for code in universe:
        visible = []
        for session in sessions:
            bar = dataset.bar(session, code)
            if bar is None or bar.total_return_close is None:
                continue
            if (
                bar.factor_known_at is not None
                and bar.factor_known_at <= data_cutoff
                and bar.factor_effective_date is not None
                and bar.factor_effective_date <= data_cutoff
            ):
                visible.append(bar)
        if len(visible) < required:
            continue
        selected = visible[-required:]
        start = selected[0].total_return_close
        end = selected[-1].total_return_close
        if start is None or start <= 0 or end is None:
            continue
        momentum[code] = float(end / start - Decimal("1"))
        recent = [bar.amount for bar in visible[-adv_window:]]
        adv20[code] = sum(recent, Decimal("0")) / Decimal(len(recent))

    final_universe = tuple(code for code in universe if code in momentum)
    return FeatureSnapshot(
        data_cutoff=data_cutoff,
        universe=final_universe,
        momentum=momentum,
        visible_sessions=sessions,
        adv20=adv20,
        names={code: dataset.names[code] for code in final_universe},
    )

