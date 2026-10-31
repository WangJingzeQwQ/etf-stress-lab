from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from backend.data_pipeline.build_universe import (
    UniverseConfig,
    build_daily_universe,
    materialize_universe,
)
from backend.data_pipeline.phase1_fixture import build_phase1_fixture
from backend.data_pipeline.models import MarketDataset, TradingRule


def _execution_after(dataset, target: date) -> tuple[date, date]:
    execution = next(session for session in dataset.sessions if session > target)
    index = dataset.sessions.index(execution)
    return dataset.sessions[index - 1], execution


def test_delisted_etf_is_eligible_before_and_rejected_after_delist():
    bundle = build_phase1_fixture()
    config = UniverseConfig()
    cutoff, execution = _execution_after(bundle.dataset, date(2024, 2, 8))
    before = {item.code: item for item in build_daily_universe(
        bundle, data_cutoff=cutoff, execution_date=execution, config=config
    )}
    assert before["159999"].eligible

    cutoff_after, execution_after = _execution_after(
        bundle.dataset, date(2024, 2, 15)
    )
    after = {item.code: item for item in build_daily_universe(
        bundle,
        data_cutoff=cutoff_after,
        execution_date=execution_after,
        config=config,
    )}
    assert not after["159999"].eligible
    assert "delisted_before_execution" in after["159999"].reasons


def test_historical_rename_is_visible_only_after_known_and_effective_date():
    bundle = build_phase1_fixture()
    record = bundle.master["510500"]
    assert record.name_at(date(2023, 11, 14)) == "中证500ETF旧称"
    assert record.name_at(date(2023, 11, 15)) == "中证500ETF旧称"
    assert record.name_at(date(2023, 12, 1)) == "中证500ETF"


def test_universe_decisions_are_invariant_to_future_bar_truncation():
    bundle = build_phase1_fixture()
    config = UniverseConfig()
    cutoff = bundle.dataset.sessions[80]
    execution = bundle.dataset.sessions[81]
    baseline = build_daily_universe(
        bundle,
        data_cutoff=cutoff,
        execution_date=execution,
        config=config,
    )
    for end in (cutoff, execution, bundle.dataset.sessions[82]):
        truncated = replace(bundle, dataset=bundle.dataset.as_of(end))
        actual = build_daily_universe(
            truncated,
            data_cutoff=cutoff,
            execution_date=execution,
            config=config,
        )
        assert actual == baseline


def test_missing_rule_is_audited_and_excluded():
    bundle = build_phase1_fixture()
    config = UniverseConfig()
    cutoff = bundle.dataset.sessions[100]
    execution = bundle.dataset.sessions[101]
    broken_dataset = MarketDataset.build(
        bundle.dataset.bars.values(),
        names=bundle.dataset.names,
        list_dates=bundle.dataset.list_dates,
        trading_rules=[
            rule for rule in bundle.dataset.trading_rules if rule.code != "510300"
        ],
        corporate_actions=bundle.dataset.corporate_actions,
        source_kind=bundle.dataset.source_kind,
        metadata=bundle.dataset.metadata,
    )
    decisions = build_daily_universe(
        replace(bundle, dataset=broken_dataset),
        data_cutoff=cutoff,
        execution_date=execution,
        config=config,
    )
    target = next(item for item in decisions if item.code == "510300")
    assert not target.eligible
    assert "missing_or_unknown_trading_rule" in target.reasons


def test_rule_effective_on_execution_but_unknown_at_cutoff_is_excluded():
    bundle = build_phase1_fixture()
    config = UniverseConfig()
    cutoff, execution = _execution_after(bundle.dataset, date(2024, 2, 8))
    rules = [
        rule for rule in bundle.dataset.trading_rules if rule.code != "510300"
    ] + [
        TradingRule(
            code="510300",
            settlement="T1",
            effective_from=execution,
            effective_to=date(2099, 12, 31),
            limit_pct=Decimal("0.10"),
            known_at=execution,
        )
    ]
    dataset = MarketDataset.build(
        bundle.dataset.bars.values(),
        names=bundle.dataset.names,
        list_dates=bundle.dataset.list_dates,
        trading_rules=rules,
        corporate_actions=bundle.dataset.corporate_actions,
        source_kind=bundle.dataset.source_kind,
        metadata=bundle.dataset.metadata,
    )
    decisions = build_daily_universe(
        replace(bundle, dataset=dataset),
        data_cutoff=cutoff,
        execution_date=execution,
        config=config,
    )
    target = next(item for item in decisions if item.code == "510300")
    assert not target.eligible
    assert "missing_or_unknown_trading_rule" in target.reasons


def test_universe_audit_files_are_materialized(tmp_path):
    bundle = build_phase1_fixture()
    cutoff = bundle.dataset.sessions[-2]
    execution = bundle.dataset.sessions[-1]
    decisions = build_daily_universe(
        bundle,
        data_cutoff=cutoff,
        execution_date=execution,
        config=UniverseConfig(),
    )
    paths = materialize_universe(bundle, decisions, tmp_path)
    assert paths["universe"].exists()
    assert paths["manifest"].exists()
    assert "source_data_version" in paths["manifest"].read_text(encoding="utf-8")

