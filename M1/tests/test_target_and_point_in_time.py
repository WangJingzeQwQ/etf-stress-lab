from __future__ import annotations

from datetime import date, timedelta
import inspect

import pytest

from backend.data_pipeline.fixture import build_synthetic_fixture
from backend.data_pipeline.models import MarketDataset
from backend.data_pipeline.point_in_time import point_in_time_features
from backend.engine.strategy import g1_momentum_target
from backend.engine.target_portfolio import (
    PortfolioValidationError,
    TargetPortfolio,
    validate_target_portfolio,
)
from tests.helpers import (
    D,
    make_bar,
    make_broker,
    make_dataset,
    make_rule,
    make_target,
)


def test_target_weights_are_strictly_normalized():
    target = make_target(date(2024, 1, 2), {"A": 0.6, "B": 0.4})
    validate_target_portfolio(target, {"A", "B"}, date(2024, 1, 2))

    with pytest.raises(PortfolioValidationError, match="sum to 1"):
        validate_target_portfolio(
            make_target(date(2024, 1, 2), {"A": 0.6, "B": 0.3}),
            {"A", "B"},
        )


def test_zero_total_r_requires_empty_weights():
    validate_target_portfolio(
        make_target(date(2024, 1, 2), {}, total_r=0.0),
        {"A"},
    )
    with pytest.raises(PortfolioValidationError, match="weights must be empty"):
        validate_target_portfolio(
            make_target(date(2024, 1, 2), {"A": 1.0}, total_r=0.0),
            {"A"},
        )


def test_invalid_pool_code_rejects_entire_target():
    with pytest.raises(PortfolioValidationError, match="not in the execution pool: B"):
        validate_target_portfolio(
            make_target(date(2024, 1, 2), {"A": 0.5, "B": 0.5}),
            {"A"},
        )


def test_target_meta_cannot_contain_capital_or_shares():
    target = make_target(date(2024, 1, 2), {"A": 1.0})
    target.meta["nested"] = {"PV_target": 123}
    with pytest.raises(PortfolioValidationError, match="forbidden"):
        validate_target_portfolio(target, {"A"})


def test_g1_top_five_equal_weights_and_no_capital_parameters():
    dataset = build_synthetic_fixture(date(2023, 10, 2), date(2024, 4, 30), seed=7)
    cutoff = dataset.sessions[100]
    execution = dataset.sessions[101]
    target = g1_momentum_target(
        dataset.as_of(cutoff),
        signal_date=cutoff,
        execution_date=execution,
        data_cutoff=cutoff,
    )
    assert isinstance(target, TargetPortfolio)
    assert target.total_r == 1.0
    assert len(target.weights) == 5
    assert all(weight == pytest.approx(0.2) for weight in target.weights.values())
    assert abs(sum(target.weights.values()) - 1.0) <= 1e-8
    parameters = inspect.signature(g1_momentum_target).parameters
    assert not {"E0", "L", "cash", "debt"}.intersection(parameters)


def test_g1_no_candidates_emits_zero_r_and_empty_weights():
    dataset = build_synthetic_fixture(date(2024, 1, 2), date(2024, 1, 10), seed=7)
    cutoff = dataset.sessions[2]
    execution = dataset.sessions[3]
    target = g1_momentum_target(
        dataset,
        signal_date=cutoff,
        execution_date=execution,
        data_cutoff=cutoff,
        min_listed_sessions=60,
    )
    assert target.total_r == 0.0
    assert target.weights == {}
    assert target.meta["fallback_reason"] == "no_candidates"


def test_g1_fewer_than_five_candidates_equal_weights_all_available():
    source = build_synthetic_fixture(date(2023, 10, 2), date(2024, 4, 30), seed=9)
    selected_codes = set(source.codes[:3])
    selected_bars = [
        bar for (_, code), bar in source.bars.items() if code in selected_codes
    ]
    dataset = MarketDataset.build(
        selected_bars,
        names={code: source.names[code] for code in selected_codes},
        list_dates={code: source.list_dates[code] for code in selected_codes},
        trading_rules=[
            rule for rule in source.trading_rules if rule.code in selected_codes
        ],
        source_kind="SYNTHETIC_FIXTURE",
        metadata={"warning": "TEST_FIXTURE"},
    )
    cutoff = dataset.sessions[100]
    target = g1_momentum_target(
        dataset,
        signal_date=cutoff,
        execution_date=dataset.sessions[101],
        data_cutoff=cutoff,
    )
    assert set(target.weights) == selected_codes
    assert len(target.weights) == 3
    assert all(weight == pytest.approx(1.0 / 3.0) for weight in target.weights.values())
    assert abs(sum(target.weights.values()) - 1.0) <= 1e-8


def test_point_in_time_features_are_truncation_invariant():
    dataset = build_synthetic_fixture(date(2023, 10, 2), date(2024, 5, 31), seed=11)
    cutoff_index = 100
    cutoff = dataset.sessions[cutoff_index]
    settings = {
        "min_listed_sessions": 60,
        "adv_window": 20,
        "min_adv_amount": D("20000000"),
        "momentum_lookback_sessions": 20,
    }
    # The signal is fixed at a date available in every truncation. Bars after
    # that cutoff must not change its universe or features.
    signal_cutoff = dataset.sessions[cutoff_index - 5]
    full_view_at_cutoff = point_in_time_features(
        dataset, signal_cutoff, **settings
    ).as_dict()
    for truncation_index in (cutoff_index - 1, cutoff_index, cutoff_index + 1):
        truncated = dataset.as_of(dataset.sessions[truncation_index])
        actual = point_in_time_features(
            truncated, signal_cutoff, **settings
        ).as_dict()
        assert actual == full_view_at_cutoff


def test_raw_price_and_lot_size_ignore_provider_qfq():
    session = date(2024, 1, 2)
    signal = date(2024, 1, 1)
    bar = make_bar(
        session,
        "A",
        raw_open="10",
        raw_close="10.1",
        raw_low="9.9",
        raw_high="10.2",
        qfq_close="0.01",
        adv20_amount="100000000",
    )
    dataset = make_dataset([bar])
    broker = make_broker(initial_cash="100000", leverage="1")
    broker.begin_day(dataset, session)
    summary = broker.execute_target(
        dataset,
        make_target(session, {"A": 1.0}, signal_date=signal),
        {"A"},
    )
    assert summary is not None
    line = summary.lines[0]
    assert line.pv_target == D("100000")
    assert line.target_shares == 10000
    assert line.target_shares % 100 == 0
    fills = [trade for trade in broker._current_trades if trade.shares > 0]
    assert fills
    assert fills[0].reference_price == D("10")
    assert fills[0].execution_price == D("10.005")
    assert fills[0].gross_amount == D("10.005") * fills[0].shares


def test_multi_asset_target_is_split_by_weight_before_execution_constraints():
    session = date(2024, 1, 2)
    dataset = make_dataset(
        [
            make_bar(session, "A", name="甲ETF", raw_open="10"),
            make_bar(session, "B", name="乙ETF", raw_open="20"),
        ]
    )
    broker = make_broker(initial_cash="1000000", leverage="2")
    broker.begin_day(dataset, session)
    summary = broker.execute_target(
        dataset,
        make_target(
            session,
            {"A": 0.6, "B": 0.4},
            signal_date=date(2024, 1, 1),
        ),
        {"A", "B"},
    )
    assert summary is not None
    lines = {line.code: line for line in summary.lines}
    assert lines["A"].pv_target == D("1200000")
    assert lines["B"].pv_target == D("800000")
    assert lines["A"].target_shares == 120000
    assert lines["B"].target_shares == 40000
    assert lines["A"].pv_actual <= lines["A"].pv_target
    assert lines["B"].pv_actual <= lines["B"].pv_target


def test_target_execution_date_must_match_day():
    session = date(2024, 1, 2)
    dataset = make_dataset([make_bar(session, "A")])
    broker = make_broker()
    broker.begin_day(dataset, session)
    summary = broker.execute_target(
        dataset,
        make_target(session + timedelta(days=1), {"A": 1.0}),
        {"A"},
    )
    assert summary is None
    assert broker.rejected_targets
    assert "does not match" in broker.rejected_targets[0]["errors"][0]


def test_broker_rejects_whole_signal_if_any_code_is_outside_pool():
    session = date(2024, 1, 2)
    dataset = make_dataset(
        [make_bar(session, "A"), make_bar(session, "B")]
    )
    broker = make_broker()
    broker.begin_day(dataset, session)
    summary = broker.execute_target(
        dataset,
        make_target(
            session,
            {"A": 0.5, "B": 0.5},
            signal_date=date(2024, 1, 1),
        ),
        {"A"},
    )
    assert summary is None
    assert not broker._current_trades
    assert broker.account.audit_events[-1].event_type == "invalid_target_portfolio"


def test_conflicting_trading_rules_reject_whole_signal():
    session = date(2024, 1, 2)
    bar = make_bar(session, "A")
    dataset = MarketDataset.build(
        [bar],
        names={"A": bar.name},
        list_dates={"A": date(2020, 1, 1)},
        trading_rules=[
            make_rule("A", "T1"),
            make_rule("A", "T0"),
        ],
        source_kind="TEST_FIXTURE",
    )
    broker = make_broker()
    broker.begin_day(dataset, session)
    summary = broker.execute_target(
        dataset,
        make_target(
            session,
            {"A": 1.0},
            signal_date=date(2024, 1, 1),
        ),
        {"A"},
    )
    assert summary is None
    assert not broker._current_trades
    assert "conflicting_trading_rule" in "; ".join(
        broker.rejected_targets[0]["errors"]
    )

