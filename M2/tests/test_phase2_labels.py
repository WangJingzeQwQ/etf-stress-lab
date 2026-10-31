from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import json
import math

import pytest

from backend.data_pipeline.build_universe import UniverseConfig
from backend.data_pipeline.models import CorporateAction, MarketDataset
from backend.data_pipeline.phase1_fixture import build_phase1_fixture
from backend.features.label_builder import (
    build_cross_sectional_labels,
    forward_open_return,
    materialize_labels,
)
from tests.helpers import make_bar, make_dataset, make_rule


def test_forward_open_return_uses_exact_horizon_sessions():
    start = date(2024, 1, 2)
    days = [start + timedelta(days=index) for index in range(22)]
    bars = [
        make_bar(
            day,
            "A",
            raw_open=str(10 + index),
            raw_close=str(10 + index),
        )
        for index, day in enumerate(days)
    ]
    dataset = make_dataset(bars)
    value, label_end = forward_open_return(
        dataset,
        code="A",
        execution_date=days[0],
        horizon_sessions=20,
    )
    assert label_end == days[20]
    assert value == pytest.approx(30 / 10 - 1)
    with pytest.raises(IndexError):
        forward_open_return(
            dataset,
            code="A",
            execution_date=days[2],
            horizon_sessions=20,
        )


def test_cross_sectional_labels_use_median_and_top_twenty_percent():
    bundle = build_phase1_fixture()
    execution = bundle.dataset.sessions[100]
    labels = build_cross_sectional_labels(
        bundle,
        start_execution_date=execution,
        end_execution_date=execution,
        horizon_sessions=20,
        top_quantile=0.2,
        universe_config=UniverseConfig(),
    )
    assert labels
    expected_end = bundle.dataset.sessions[
        bundle.dataset.sessions.index(execution) + 20
    ]
    assert all(row.label_end_date == expected_end for row in labels)
    assert all(row.data_cutoff < row.execution_date for row in labels)
    assert all(
        abs(
            row.excess_return
            - (row.forward_return - row.cross_section_median)
        )
        < 1e-12
        for row in labels
    )
    expected_top = max(1, math.ceil(len(labels) * 0.2))
    assert sum(row.label_top20 for row in labels) == expected_top


def test_label_builder_omits_dates_without_full_horizon():
    bundle = build_phase1_fixture()
    last = bundle.dataset.sessions[-1]
    labels = build_cross_sectional_labels(
        bundle,
        start_execution_date=last,
        end_execution_date=last,
        horizon_sessions=20,
        universe_config=UniverseConfig(),
    )
    assert labels == []


def test_label_return_applies_split_without_treating_raw_drop_as_loss():
    start = date(2024, 1, 2)
    days = [start + timedelta(days=index) for index in range(21)]
    bars = [
        make_bar(
            day,
            "A",
            raw_open="10" if index < 5 else "5",
            raw_close="10" if index < 5 else "5",
        )
        for index, day in enumerate(days)
    ]
    dataset = MarketDataset.build(
        bars,
        names={"A": "拆分测试ETF"},
        list_dates={"A": date(2020, 1, 1)},
        trading_rules=[make_rule("A")],
        corporate_actions=[
            CorporateAction(
                code="A",
                effective_date=days[5],
                known_at=days[2],
                action_type="split",
                split_ratio=Decimal("2"),
            )
        ],
        source_kind="TEST_FIXTURE",
    )
    value, label_end = forward_open_return(
        dataset,
        code="A",
        execution_date=days[4],
        horizon_sessions=1,
    )
    assert label_end == days[5]
    assert value == pytest.approx(0.0)


def test_label_manifest_contains_artifact_and_semantic_hashes(tmp_path):
    label = build_cross_sectional_labels(
        build_phase1_fixture(),
        start_execution_date=date(2024, 2, 1),
        end_execution_date=date(2024, 2, 1),
        horizon_sessions=20,
        universe_config=UniverseConfig(),
    )
    output = tmp_path / "labels.ndjson"
    materialize_labels(
        label,
        output,
        source_kind="SYNTHETIC_FIXTURE",
        data_version="sha256:test",
        horizon_sessions=20,
    )
    manifest = json.loads(
        (tmp_path / "labels.json").read_text(encoding="utf-8")
    )
    assert manifest["rows"] == len(label)
    assert manifest["rows_sha256"].startswith("sha256:")
    assert manifest["artifact_sha256"].startswith("sha256:")
    assert manifest["date_from"] == "2024-02-01"
    assert manifest["date_to"] == "2024-02-01"

