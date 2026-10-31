from __future__ import annotations

from datetime import date
import inspect

import numpy as np
import pandas as pd
import pytest
import torch

from backend.features.build_features import FEATURE_NAMES
from backend.models.layer_a import (
    ConfidenceMapper,
    evaluate_scores,
    g2_target_from_scores,
)
from backend.models.layer_c import LstmRepresentation, build_sequence_bundle


def test_confidence_mapper_is_monotone_and_validation_calibrated():
    mapper = ConfidenceMapper.fit(np.linspace(-1, 1, 100))
    values = [
        mapper.map(confidence)
        for confidence in (-2, -0.6, -0.2, 0.2, 0.6, 2)
    ]
    assert values == sorted(values)
    assert set(values) == {0.0, 0.25, 0.5, 0.75, 1.0}
    assert mapper.to_dict()["fit_scope"] == "validation_only"


def test_g2_target_has_top_five_equal_weights_and_no_capital_fields():
    scores = {
        code: float(index)
        for index, code in enumerate(("A", "B", "C", "D", "E", "F"))
    }
    target = g2_target_from_scores(
        scores,
        signal_date=date(2024, 1, 30),
        execution_date=date(2024, 1, 31),
        data_cutoff=date(2024, 1, 30),
        mapper=ConfidenceMapper((-10.0, -5.0, -1.0, 0.0)),
        top_n=5,
    )
    assert target.group == "G2"
    assert target.total_r == 1.0
    assert set(target.weights) == {"F", "E", "D", "C", "B"}
    assert all(weight == pytest.approx(0.2) for weight in target.weights.values())
    assert not {"E0", "L", "cash", "debt"}.intersection(
        inspect.signature(g2_target_from_scores).parameters
    )


def test_g2_zero_risk_level_returns_empty_weights():
    target = g2_target_from_scores(
        {"A": 1.0, "B": 0.0},
        signal_date=date(2024, 1, 30),
        execution_date=date(2024, 1, 31),
        data_cutoff=date(2024, 1, 30),
        mapper=ConfidenceMapper((10.0, 20.0, 30.0, 40.0)),
    )
    assert target.total_r == 0.0
    assert target.weights == {}


def test_rank_metrics_are_cross_sectional_and_do_not_use_future_rows():
    dates = [f"2024-01-0{index}" for index in range(2, 7)]
    frame = pd.DataFrame(
        {
            "execution_date": [
                value for value in dates for _ in range(5)
            ],
            "code": list("ABCDE") * len(dates),
            "excess_return": [5, 4, 3, 2, 1] * len(dates),
            "label_top20": [True, False, False, False, False] * len(dates),
        }
    )
    metrics = evaluate_scores(
        frame,
        np.asarray([4, 3, 2, 1, 0] * len(dates), dtype=float),
        top_n=1,
    )
    assert metrics["dates"] == len(dates)
    assert metrics["mean_rank_ic"] == pytest.approx(1.0)
    assert metrics["mean_top5_excess_return"] == pytest.approx(5.0)
    assert metrics["top20_precision"] == pytest.approx(1.0)


def test_lstm_sequence_builder_is_prefix_safe_and_representation_is_repeatable():
    feature_rows = []
    for day in range(25):
        feature_rows.append(
            {
                "data_cutoff": f"2024-01-{day + 1:02d}",
                "code": "A",
                **{name: float(day + index) for index, name in enumerate(FEATURE_NAMES)},
            }
        )
    feature_panel = pd.DataFrame(feature_rows)
    model_rows = pd.DataFrame(
        {
            "data_cutoff": ["2024-01-25"],
            "code": ["A"],
            "excess_return": [0.01],
            "label_top20": [True],
            **{
                name: [float(24 + index)]
                for index, name in enumerate(FEATURE_NAMES)
            },
        }
    )
    bundle = build_sequence_bundle(
        feature_panel,
        model_rows,
        sequence_length=20,
    )
    assert bundle.sequences.shape == (1, 20, 14)
    assert bundle.sequences[0, 0, 0] == pytest.approx(5.0)
    assert bundle.sequences[0, -1, 0] == pytest.approx(24.0)
    model = LstmRepresentation(input_size=14, hidden_size=8)
    model.eval()
    values = torch.from_numpy(bundle.sequences)
    with torch.no_grad():
        first = model(values)
        second = model(values)
    assert torch.equal(first[0], second[0])
    assert torch.equal(first[1], second[1])
