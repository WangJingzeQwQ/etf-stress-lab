from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from backend.data_pipeline.build_total_return import (
    materialize_point_in_time_total_returns,
)
from backend.data_pipeline.contracts import DataQualityError
from backend.data_pipeline.phase1_fixture import build_phase1_fixture
from backend.data_pipeline.quality import validate_bundle


def test_total_return_is_invariant_to_prices_after_cutoff():
    bundle = build_phase1_fixture()
    cutoff = date(2024, 1, 31)
    full = materialize_point_in_time_total_returns(bundle.dataset, cutoff)
    truncated = materialize_point_in_time_total_returns(
        bundle.dataset.as_of(cutoff),
        cutoff,
    )
    assert full.data_version == truncated.data_version


def test_known_future_dividend_does_not_change_series_before_effective_date():
    bundle = build_phase1_fixture()
    cutoff = date(2024, 1, 31)
    dataset = materialize_point_in_time_total_returns(bundle.dataset, cutoff)
    for session in dataset.sessions:
        bar = dataset.bar(session, "510300")
        assert bar is not None
        assert bar.total_return_close == bar.raw_close


def test_effective_dividend_adjusts_only_history_before_ex_date():
    bundle = build_phase1_fixture()
    cutoff = date(2024, 2, 2)
    dataset = materialize_point_in_time_total_returns(bundle.dataset, cutoff)
    prior_sessions = [session for session in dataset.sessions if session < date(2024, 2, 1)]
    prior = dataset.require_bar(prior_sessions[-1], "510300")
    expected_factor = (prior.raw_close - Decimal("0.03")) / prior.raw_close
    assert prior.total_return_close == prior.raw_close * expected_factor
    ex_date_bar = dataset.require_bar(date(2024, 2, 1), "510300")
    assert ex_date_bar.total_return_close == ex_date_bar.raw_close


def test_effective_split_adjusts_history_before_split_date():
    bundle = build_phase1_fixture()
    cutoff = date(2024, 3, 4)
    dataset = materialize_point_in_time_total_returns(bundle.dataset, cutoff)
    prior_sessions = [session for session in dataset.sessions if session < date(2024, 3, 1)]
    prior = dataset.require_bar(prior_sessions[-1], "159915")
    assert prior.total_return_close == prior.raw_close * Decimal("0.5")
    split_day = dataset.require_bar(date(2024, 3, 1), "159915")
    assert split_day.total_return_close == split_day.raw_close
    assert split_day.raw_close == bundle.dataset.require_bar(date(2024, 3, 1), "159915").raw_close


def test_formal_research_gate_blocks_synthetic_bundle():
    bundle = build_phase1_fixture()
    report = validate_bundle(bundle, formal_research=True)
    assert not report.formal_research_allowed
    assert report.errors
    assert any("Formal research" in issue.message for issue in report.errors)


def test_fixture_bundle_is_allowed_for_engineering_validation():
    bundle = build_phase1_fixture()
    report = validate_bundle(bundle)
    assert report.backtest_allowed
    assert not report.formal_research_allowed
    assert report.warnings

