from __future__ import annotations

from dataclasses import replace
from datetime import date
import json

from backend.data_pipeline.build_universe import UniverseConfig, build_daily_universe
from backend.data_pipeline.build_total_return import (
    materialize_point_in_time_total_returns,
)
from backend.data_pipeline.phase1_fixture import build_phase1_fixture
from backend.features.build_features import (
    FEATURE_NAMES,
    _feature_values,
    build_feature_snapshot,
    materialize_features,
)


def test_feature_snapshot_has_fourteen_finite_point_in_time_features(tmp_path):
    bundle = build_phase1_fixture()
    cutoff = bundle.dataset.sessions[100]
    execution = bundle.dataset.sessions[101]
    decisions = build_daily_universe(
        bundle,
        data_cutoff=cutoff,
        execution_date=execution,
        config=UniverseConfig(),
    )
    rows = build_feature_snapshot(
        bundle.dataset,
        data_cutoff=cutoff,
        universe_codes={item.code for item in decisions if item.eligible},
    )
    assert 10 <= len(FEATURE_NAMES) <= 15
    assert rows
    assert set(rows[0].values) == set(FEATURE_NAMES)
    assert all(
        value == value and abs(value) != float("inf")
        for row in rows
        for value in row.values.values()
    )
    output = tmp_path / "features.ndjson"
    materialize_features(
        rows,
        output,
        source_kind="SYNTHETIC_FIXTURE",
        data_version="sha256:test-fixture",
    )
    manifest = json.loads(
        (tmp_path / "features.json").read_text(encoding="utf-8")
    )
    assert manifest["feature_count"] == 14
    assert manifest["execution_price_usage"] == "none"
    assert manifest["formal_research_allowed"] is False
    assert manifest["rows"] == len(rows)
    assert manifest["rows_sha256"].startswith("sha256:")
    assert manifest["artifact_sha256"].startswith("sha256:")


def test_features_ignore_bars_after_cutoff():
    bundle = build_phase1_fixture()
    cutoff = bundle.dataset.sessions[100]
    execution = bundle.dataset.sessions[101]
    decisions = build_daily_universe(
        bundle,
        data_cutoff=cutoff,
        execution_date=execution,
        config=UniverseConfig(),
    )
    codes = {item.code for item in decisions if item.eligible}
    baseline = build_feature_snapshot(
        bundle.dataset,
        data_cutoff=cutoff,
        universe_codes=codes,
    )
    for end in (cutoff, execution, bundle.dataset.sessions[102]):
        truncated = replace(bundle, dataset=bundle.dataset.as_of(end))
        actual = build_feature_snapshot(
            truncated.dataset,
            data_cutoff=cutoff,
            universe_codes=codes,
        )
        assert actual == baseline


def test_indexed_feature_builder_matches_materialized_point_in_time_view():
    bundle = build_phase1_fixture()
    cutoff = bundle.dataset.sessions[100]
    execution = bundle.dataset.sessions[101]
    decisions = build_daily_universe(
        bundle,
        data_cutoff=cutoff,
        execution_date=execution,
        config=UniverseConfig(),
    )
    codes = {item.code for item in decisions if item.eligible}
    optimized = {
        row.code: row.values
        for row in build_feature_snapshot(
            bundle.dataset,
            data_cutoff=cutoff,
            universe_codes=codes,
        )
    }
    view = materialize_point_in_time_total_returns(
        bundle.dataset,
        cutoff,
        codes=codes,
    )
    reference = {}
    for code in sorted(codes):
        bars = [
            view.bars[(session, code)]
            for session in view.sessions_through(cutoff)
            if (session, code) in view.bars
        ]
        values = _feature_values(bars)
        if values is not None:
            reference[code] = values
    assert optimized == reference

