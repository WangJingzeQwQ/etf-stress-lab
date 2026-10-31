from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence

import numpy as np


@dataclass(frozen=True)
class CvarEstimate:
    """Expected shortfall in positive-loss units."""

    cvar: float
    var: float
    tail_observations: int
    observations: int
    alpha: float

    def to_dict(self) -> dict[str, float | int]:
        return {
            "cvar_positive_loss": self.cvar,
            "var_positive_loss": self.var,
            "tail_observations": self.tail_observations,
            "observations": self.observations,
            "alpha": self.alpha,
        }


def positive_loss_cvar(
    returns: Iterable[float],
    *,
    alpha: float = 0.10,
    window: int = 120,
) -> CvarEstimate:
    """Estimate the worst-tail mean loss from a strict trailing return window.

    The observation for the current decision is supplied by the caller only
    after it has been realised.  This function never accepts a future series
    and always slices to the final ``window`` observations.  A return ``r``
    maps to loss ``-r``; the result is therefore positive for a losing tail.
    """

    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be strictly between 0 and 1")
    if window <= 0:
        raise ValueError("window must be positive")
    values = np.asarray(list(returns), dtype=float)
    if values.size == 0:
        return CvarEstimate(0.0, 0.0, 0, 0, alpha)
    if not np.isfinite(values).all():
        raise ValueError("returns must be finite")
    values = values[-window:]
    losses = np.sort(-values)[::-1]
    count = max(1, int(math.ceil(len(losses) * alpha)))
    tail = losses[:count]
    return CvarEstimate(
        cvar=float(np.mean(tail)),
        var=float(tail[-1]),
        tail_observations=int(count),
        observations=int(len(losses)),
        alpha=float(alpha),
    )


def rolling_sharpe(
    returns: Sequence[float],
    *,
    window: int = 20,
    annualization: int = 252,
    require_full_window: bool = True,
) -> float:
    if window <= 1:
        raise ValueError("window must be greater than one")
    values = np.asarray(list(returns)[-window:], dtype=float)
    if values.size == 0 or not np.isfinite(values).all():
        return 0.0
    if require_full_window and values.size < window:
        return 0.0
    volatility = float(np.std(values, ddof=1)) if values.size > 1 else 0.0
    if volatility <= 1e-12:
        return 0.0
    return float(np.mean(values) / volatility * math.sqrt(annualization))

