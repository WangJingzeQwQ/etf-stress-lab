from __future__ import annotations

from backend.engine.survival import (
    SurvivalObservation,
    holm_adjust,
    kaplan_meier,
    time_block_bootstrap_ci,
)


def test_kaplan_meier_handles_events_and_right_censoring_once():
    observations = [
        SurvivalObservation("a", True, 10),
        SurvivalObservation("b", False, 20),
        SurvivalObservation("c", True, 20),
        SurvivalObservation("d", False, 30),
    ]
    curve = kaplan_meier(observations)
    assert curve[0]["survival"] == 1.0
    assert curve[1]["survival"] == 0.75
    assert curve[-1]["survival"] == 0.5
    assert curve[-1]["at_risk"] == 1


def test_time_block_bootstrap_is_reproducible_and_holm_is_monotone():
    first = time_block_bootstrap_ci(
        list(range(100)),
        block_size=10,
        repetitions=100,
    )
    second = time_block_bootstrap_ci(
        list(range(100)),
        block_size=10,
        repetitions=100,
    )
    assert first == second
    assert first["ci_low"] <= first["mean"] <= first["ci_high"]
    adjusted = holm_adjust([0.01, 0.04, 0.03])
    assert adjusted[0] <= adjusted[2] <= adjusted[1]
