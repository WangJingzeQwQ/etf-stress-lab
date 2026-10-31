from __future__ import annotations

import math

from backend.models.cvar import positive_loss_cvar, rolling_sharpe


def test_cvar_uses_positive_loss_and_strict_trailing_window():
    past = [0.01, -0.02, -0.10, -0.20, 0.50]
    estimate = positive_loss_cvar(past, alpha=0.25, window=4)
    # Only the last four observations are visible; the +50% future-like point
    # is excluded.  The worst loss is therefore +20%, not a negative return.
    assert estimate.observations == 4
    assert estimate.tail_observations == 1
    assert estimate.cvar == 0.20
    assert estimate.cvar > 0


def test_cvar_tail_mean_and_rolling_sharpe_are_deterministic():
    estimate = positive_loss_cvar(
        [-0.01, -0.03, -0.08, -0.12, 0.02, 0.04],
        alpha=0.50,
        window=6,
    )
    assert estimate.cvar == (0.12 + 0.08 + 0.03) / 3
    assert math.isclose(
        rolling_sharpe([0.01, 0.02, 0.03], window=3),
        rolling_sharpe([0.01, 0.02, 0.03], window=3),
    )
