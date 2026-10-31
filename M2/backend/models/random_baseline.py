from __future__ import annotations

from datetime import date
import random
from typing import Iterable

from backend.models.env import ACTION_TOTAL_R
from backend.engine.target_portfolio import TargetPortfolio


G5_SEEDS = (
    11,
    19,
    31,
    47,
    59,
    67,
    79,
    89,
    97,
    107,
    127,
    139,
    149,
    163,
    181,
    193,
    211,
    227,
    241,
    257,
)


def validate_g5_seeds(seeds: Iterable[int]) -> tuple[int, ...]:
    values = tuple(int(seed) for seed in seeds)
    if len(values) < 20:
        raise ValueError("G5 requires at least 20 seeds")
    if len(set(values)) != len(values):
        raise ValueError("G5 seeds must be unique")
    return values


def g5_random_target(
    *,
    codes: Iterable[str],
    seed: int,
    signal_date: date,
    execution_date: date,
    top_n: int = 5,
) -> TargetPortfolio:
    ordered = sorted(set(codes))
    if not ordered:
        return TargetPortfolio(
            total_r=0.0,
            weights={},
            signal_date=signal_date,
            execution_date=execution_date,
            data_cutoff=signal_date,
            group="G5",
            meta={"seed": seed, "fallback_reason": "no_candidates"},
        )
    rng = random.Random(seed)
    selected = rng.sample(ordered, min(top_n, len(ordered)))
    raw_weights = [(code, rng.expovariate(1.0)) for code in selected]
    total_weight = sum(value for _, value in raw_weights)
    weights: dict[str, float] = {}
    cumulative = 0.0
    for index, (code, value) in enumerate(raw_weights):
        if index == len(raw_weights) - 1:
            weights[code] = 1.0 - cumulative
        else:
            weight = value / total_weight
            weights[code] = weight
            cumulative += weight
    total_r = rng.choice(ACTION_TOTAL_R)
    return TargetPortfolio(
        total_r=total_r,
        weights=weights if total_r > 0 else {},
        signal_date=signal_date,
        execution_date=execution_date,
        data_cutoff=signal_date,
        group="G5",
        meta={
            "seed": seed,
            "sampling": "uniform_total_r_exponential_weights_then_normalise",
        },
    )

