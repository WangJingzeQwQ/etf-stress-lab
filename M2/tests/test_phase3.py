from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from backend.data_pipeline.contracts import content_hash
from backend.data_pipeline.storage import write_json_atomic
from backend.split.splitter import (
    HoldoutAccessError,
    SplitValidationError,
    build_expanding_walk_forward,
    build_fractional_chronological_split,
    claim_final_holdout,
    freeze_split_plan,
    split_label_records,
    stratified_training_validation_indices,
    validate_split_plan,
    verify_frozen_split_plan,
)


def _sessions(count: int) -> tuple[date, ...]:
    start = date(2018, 1, 2)
    return tuple(start + timedelta(days=index) for index in range(count))


def test_chronological_split_has_h_plus_one_purge_and_no_label_overlap():
    sessions = _sessions(300)
    plan = build_fractional_chronological_split(
        sessions,
        horizon_sessions=20,
        train_fraction=0.65,
        validation_fraction=0.15,
    )
    validate_split_plan(plan, sessions, horizon_sessions=20)
    assert plan.purge_sessions == 21
    assert plan.train_end < plan.validation_start
    assert plan.validation_end < plan.holdout_start
    records = [
        {
            "execution_date": session.isoformat(),
            "label_end_date": sessions[index + 20].isoformat(),
        }
        for index, session in enumerate(sessions[:-20])
    ]
    assigned = split_label_records(records, plan)
    assert assigned.train
    assert assigned.validation
    assert assigned.holdout
    assert max(
        date.fromisoformat(row["label_end_date"]) for row in assigned.train
    ) < plan.validation_start
    assert max(
        date.fromisoformat(row["label_end_date"])
        for row in assigned.validation
    ) < plan.holdout_start


def test_split_horizon_changes_purge_without_fixed_twenty_day_assumption():
    sessions = _sessions(240)
    short = build_fractional_chronological_split(
        sessions,
        horizon_sessions=5,
    )
    long = build_fractional_chronological_split(
        sessions,
        horizon_sessions=40,
    )
    assert short.purge_sessions == 6
    assert long.purge_sessions == 41


def test_frozen_holdout_is_immutable_and_can_only_be_claimed_once(tmp_path):
    sessions = _sessions(260)
    plan = build_fractional_chronological_split(
        sessions,
        horizon_sessions=20,
    )
    frozen_path = tmp_path / "split.json"
    assert freeze_split_plan(plan, frozen_path) == plan.to_dict()
    assert freeze_split_plan(plan, frozen_path) == plan.to_dict()
    changed_validation = replace(
        plan,
        validation_end=sessions[sessions.index(plan.validation_end) + 1],
    )
    with pytest.raises(HoldoutAccessError, match="differs"):
        verify_frozen_split_plan(changed_validation, frozen_path)
    with pytest.raises(HoldoutAccessError, match="already frozen"):
        freeze_split_plan(changed_validation, frozen_path)
    claim_path = tmp_path / "holdout_claim.json"
    claim = claim_final_holdout(
        plan,
        frozen_plan_path=frozen_path,
        claim_path=claim_path,
        evaluator_id="test-evaluator",
    )
    assert claim["status"] == "CLAIMED_ONCE"
    with pytest.raises(HoldoutAccessError, match="already been claimed"):
        claim_final_holdout(
            plan,
            frozen_plan_path=frozen_path,
            claim_path=claim_path,
            evaluator_id="second-evaluator",
        )


def test_holdout_claim_binds_preregistration_hash(tmp_path):
    sessions = _sessions(260)
    plan = build_fractional_chronological_split(
        sessions,
        horizon_sessions=20,
    )
    frozen_path = tmp_path / "split.json"
    freeze_split_plan(plan, frozen_path)
    preregistration = {
        "holdout_evaluated": False,
        "fixed_metric": "first_liquidation_probability",
    }
    preregistration_path = tmp_path / "preregistration.json"
    write_json_atomic(preregistration_path, preregistration)
    claim_path = tmp_path / "claim.json"
    claim = claim_final_holdout(
        plan,
        frozen_plan_path=frozen_path,
        claim_path=claim_path,
        evaluator_id="test-evaluator",
        preregistration_path=preregistration_path,
        preregistration_hash=content_hash(preregistration),
    )
    assert claim["preregistration_hash"] == content_hash(preregistration)


def test_random_stratified_sampling_rejects_final_holdout_rows():
    rows = [
        {"label_top20": index % 2 == 0, "split": "train"}
        for index in range(20)
    ]
    train, validation = stratified_training_validation_indices(rows, seed=7)
    assert set(train).isdisjoint(validation)
    assert set(train) | set(validation) == set(range(len(rows)))
    rows[-1]["split"] = "holdout"
    with pytest.raises(SplitValidationError, match="forbidden"):
        stratified_training_validation_indices(rows, seed=7)


def test_expanding_walk_forward_uses_gated_validation_windows():
    sessions = _sessions(260)
    folds = build_expanding_walk_forward(
        sessions,
        initial_train_sessions=120,
        validation_sessions=20,
        step_sessions=20,
        horizon_sessions=20,
        development_end=sessions[220],
    )
    assert len(folds) >= 3
    for previous, current in zip(folds, folds[1:]):
        assert current.train_start == previous.train_start
        assert current.train_end > previous.train_end
        assert current.validation_start > previous.validation_start
        assert current.validation_start > previous.validation_end
    for fold in folds:
        assert (
            sessions.index(fold.validation_start)
            - sessions.index(fold.train_end)
            - 1
            == 21
        )
        assert fold.purge_sessions == 21
