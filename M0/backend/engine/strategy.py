from __future__ import annotations

from datetime import date

from backend.data_pipeline.models import MarketDataset
from backend.data_pipeline.point_in_time import point_in_time_features
from backend.engine.models import decimal_value
from backend.engine.target_portfolio import TargetPortfolio


def g1_momentum_target(
    dataset: MarketDataset,
    *,
    signal_date: date,
    execution_date: date,
    data_cutoff: date,
    min_listed_sessions: int = 60,
    adv_window: int = 20,
    min_adv_amount: float = 20_000_000,
    momentum_lookback_sessions: int = 20,
    top_n: int = 5,
) -> TargetPortfolio:
    """G1 receives market data only; capital and leverage belong to Broker."""

    if data_cutoff > signal_date or signal_date >= execution_date:
        raise ValueError("G1 requires data_cutoff <= signal_date < execution_date")
    features = point_in_time_features(
        dataset,
        data_cutoff,
        min_listed_sessions=min_listed_sessions,
        adv_window=adv_window,
        min_adv_amount=decimal_value(min_adv_amount),
        momentum_lookback_sessions=momentum_lookback_sessions,
    )
    ranked = sorted(
        features.momentum.items(),
        key=lambda item: (-item[1], item[0]),
    )[:top_n]
    if not ranked:
        return TargetPortfolio(
            total_r=0.0,
            weights={},
            signal_date=signal_date,
            execution_date=execution_date,
            data_cutoff=data_cutoff,
            group="G1",
            meta={
                "fallback_reason": "no_candidates",
                "ranking": [],
                "source_kind": dataset.source_kind,
            },
        )

    weight = 1.0 / len(ranked)
    weights: dict[str, float] = {}
    cumulative = 0.0
    for index, (code, _) in enumerate(ranked):
        if index == len(ranked) - 1:
            weights[code] = 1.0 - cumulative
        else:
            weights[code] = weight
            cumulative += weight
    return TargetPortfolio(
        total_r=1.0,
        weights=weights,
        signal_date=signal_date,
        execution_date=execution_date,
        data_cutoff=data_cutoff,
        group="G1",
        meta={
            "ranking": [
                {
                    "code": code,
                    "name": features.names[code],
                    "momentum": score,
                }
                for code, score in ranked
            ],
            "lookback_sessions": momentum_lookback_sessions,
            "universe_count": len(features.universe),
            "source_kind": dataset.source_kind,
        },
    )

