from __future__ import annotations

from dataclasses import dataclass
import math
import random
from typing import Iterable, Sequence

import numpy as np


@dataclass(frozen=True)
class SurvivalObservation:
    path_id: str
    event_observed: bool
    time_sessions: int


def validate_survival_observations(
    observations: Iterable[SurvivalObservation],
) -> tuple[SurvivalObservation, ...]:
    values = tuple(observations)
    path_ids = [value.path_id for value in values]
    if len(path_ids) != len(set(path_ids)):
        raise ValueError("Survival paths must have unique IDs")
    if any(value.time_sessions < 0 for value in values):
        raise ValueError("Survival times cannot be negative")
    return values


def kaplan_meier(
    observations: Iterable[SurvivalObservation],
) -> list[dict[str, float | int]]:
    values = validate_survival_observations(observations)
    if not values:
        return []
    times = sorted({value.time_sessions for value in values})
    survival = 1.0
    at_risk = len(values)
    curve = [{"time_sessions": 0, "at_risk": at_risk, "survival": survival}]
    for time in times:
        events = sum(
            value.event_observed and value.time_sessions == time
            for value in values
        )
        censored = sum(
            not value.event_observed and value.time_sessions == time
            for value in values
        )
        if events and at_risk > 0:
            survival *= 1.0 - events / at_risk
        curve.append(
            {
                "time_sessions": time,
                "at_risk": at_risk,
                "events": events,
                "censored": censored,
                "survival": survival,
            }
        )
        at_risk -= events + censored
    return curve


def expected_shortfall_positive(
    values: Sequence[float],
    *,
    level: float = 0.95,
) -> float | None:
    if not 0 < level < 1:
        raise ValueError("level must be between zero and one")
    if not values:
        return None
    ordered = np.asarray(sorted((float(value) for value in values), reverse=True))
    count = max(1, int(math.ceil(len(ordered) * (1.0 - level))))
    return float(ordered[:count].mean())


def time_block_bootstrap_ci(
    values: Sequence[float],
    *,
    block_size: int = 20,
    repetitions: int = 2000,
    confidence: float = 0.95,
    seed: int = 20261031,
) -> dict[str, float | int | str]:
    if block_size <= 0 or repetitions <= 0:
        raise ValueError("bootstrap block size and repetitions must be positive")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between zero and one")
    array = np.asarray([float(value) for value in values], dtype=float)
    if array.size == 0:
        return {
            "method": "moving_time_block_bootstrap",
            "samples": 0,
            "block_size": block_size,
            "mean": float("nan"),
            "ci_low": float("nan"),
            "ci_high": float("nan"),
        }
    block_count = max(1, math.ceil(array.size / block_size))
    rng = random.Random(seed)
    samples = []
    for _ in range(repetitions):
        selected = []
        while len(selected) < array.size:
            start = rng.randrange(array.size)
            selected.extend(array[start : start + block_size].tolist())
        samples.append(float(np.mean(selected[: array.size])))
    alpha = (1.0 - confidence) / 2.0
    return {
        "method": "moving_time_block_bootstrap",
        "samples": int(array.size),
        "block_size": block_size,
        "repetitions": repetitions,
        "mean": float(array.mean()),
        "ci_low": float(np.quantile(samples, alpha)),
        "ci_high": float(np.quantile(samples, 1.0 - alpha)),
    }


def holm_adjust(p_values: Sequence[float]) -> list[float]:
    values = [float(value) for value in p_values]
    if any(not 0 <= value <= 1 for value in values):
        raise ValueError("p-values must lie in [0, 1]")
    order = sorted(range(len(values)), key=lambda index: values[index])
    adjusted = [1.0] * len(values)
    running = 1.0
    count = len(values)
    for rank, index in enumerate(order):
        candidate = min(1.0, values[index] * (count - rank))
        running = min(running, candidate)
        adjusted[index] = running
    return adjusted

