from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
import random
from typing import Any, Iterable, Mapping, Sequence, TypeVar

from backend.data_pipeline.contracts import content_hash
from backend.data_pipeline.storage import read_json, write_json_atomic


class SplitValidationError(ValueError):
    """Raised when a split could leak labels across time boundaries."""


class HoldoutAccessError(RuntimeError):
    """Raised when the final holdout is changed or accessed twice."""


T = TypeVar("T", bound=Mapping[str, Any])


@dataclass(frozen=True)
class ChronologicalSplitPlan:
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    holdout_start: date
    holdout_end: date
    horizon_sessions: int
    embargo_sessions: int
    holdout_id: str

    @property
    def purge_sessions(self) -> int:
        return self.horizon_sessions + 1 + self.embargo_sessions

    def unordered_dict(self) -> dict[str, Any]:
        return {
            "schema": "chronological_split_v1",
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "validation_start": self.validation_start.isoformat(),
            "validation_end": self.validation_end.isoformat(),
            "holdout_start": self.holdout_start.isoformat(),
            "holdout_end": self.holdout_end.isoformat(),
            "horizon_sessions": self.horizon_sessions,
            "embargo_sessions": self.embargo_sessions,
            "purge_sessions": self.purge_sessions,
            "holdout_id": self.holdout_id,
        }

    @property
    def plan_hash(self) -> str:
        return content_hash(self.unordered_dict())

    def to_dict(self) -> dict[str, Any]:
        return {**self.unordered_dict(), "plan_hash": self.plan_hash}


@dataclass(frozen=True)
class SplitAssignment:
    train: tuple[Mapping[str, Any], ...]
    validation: tuple[Mapping[str, Any], ...]
    holdout: tuple[Mapping[str, Any], ...]
    purged: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, int]:
        return {
            "train": len(self.train),
            "validation": len(self.validation),
            "holdout": len(self.holdout),
            "purged": len(self.purged),
        }


def _validate_sessions(sessions: Sequence[date]) -> tuple[date, ...]:
    ordered = tuple(sorted(set(sessions)))
    if len(ordered) != len(sessions):
        raise SplitValidationError("Split sessions must be unique")
    if len(ordered) < 3:
        raise SplitValidationError("At least three sessions are required")
    return ordered


def build_fractional_chronological_split(
    label_dates: Iterable[date],
    *,
    horizon_sessions: int,
    train_fraction: float = 0.65,
    validation_fraction: float = 0.15,
    embargo_sessions: int = 0,
    holdout_id: str | None = None,
) -> ChronologicalSplitPlan:
    dates = _validate_sessions(tuple(label_dates))
    if horizon_sessions <= 0 or embargo_sessions < 0:
        raise SplitValidationError("Horizon and embargo must be non-negative")
    if not 0 < train_fraction < 1 or not 0 < validation_fraction < 1:
        raise SplitValidationError("Split fractions must be between zero and one")
    if train_fraction + validation_fraction >= 1:
        raise SplitValidationError("Train and validation fractions must leave a holdout")
    gap = horizon_sessions + 1 + embargo_sessions
    usable = len(dates) - 2 * gap
    if usable < 3:
        raise SplitValidationError("Not enough sessions for purged splits")
    train_count = max(1, math.floor(usable * train_fraction))
    validation_count = max(1, math.floor(usable * validation_fraction))
    holdout_count = usable - train_count - validation_count
    if holdout_count < 1:
        raise SplitValidationError("Fractional split leaves no holdout sessions")
    train = dates[:train_count]
    validation = dates[train_count + gap : train_count + gap + validation_count]
    holdout = dates[
        train_count + gap + validation_count + gap : train_count
        + gap
        + validation_count
        + gap
        + holdout_count
    ]
    plan = ChronologicalSplitPlan(
        train_start=train[0],
        train_end=train[-1],
        validation_start=validation[0],
        validation_end=validation[-1],
        holdout_start=holdout[0],
        holdout_end=holdout[-1],
        horizon_sessions=horizon_sessions,
        embargo_sessions=embargo_sessions,
        holdout_id=holdout_id or "pending",
    )
    plan = ChronologicalSplitPlan(
        **{
            **plan.__dict__,
            "holdout_id": holdout_id or f"holdout_{plan.plan_hash.removeprefix('sha256:')[:12]}",
        }
    )
    validate_split_plan(
        plan,
        dates,
        horizon_sessions=horizon_sessions,
    )
    return plan


def _index(sessions: Sequence[date], value: date, label: str) -> int:
    try:
        return sessions.index(value)
    except ValueError as exc:
        raise SplitValidationError(f"{label} is not a dataset session: {value}") from exc


def validate_split_plan(
    plan: ChronologicalSplitPlan,
    sessions: Iterable[date],
    *,
    horizon_sessions: int | None = None,
) -> None:
    ordered = _validate_sessions(tuple(sessions))
    if horizon_sessions is not None and plan.horizon_sessions != horizon_sessions:
        raise SplitValidationError("Split plan horizon does not match labels")
    if plan.horizon_sessions <= 0 or plan.embargo_sessions < 0:
        raise SplitValidationError("Invalid split horizon or embargo")
    if not (
        plan.train_start
        <= plan.train_end
        < plan.validation_start
        <= plan.validation_end
        < plan.holdout_start
        <= plan.holdout_end
    ):
        raise SplitValidationError("Split boundaries are not strictly chronological")
    train_end_index = _index(ordered, plan.train_end, "train_end")
    validation_start_index = _index(
        ordered,
        plan.validation_start,
        "validation_start",
    )
    if validation_start_index - train_end_index - 1 < plan.purge_sessions:
        raise SplitValidationError("Train/validation purge buffer is too short")
    validation_end_index = _index(
        ordered,
        plan.validation_end,
        "validation_end",
    )
    holdout_start_index = _index(
        ordered,
        plan.holdout_start,
        "holdout_start",
    )
    if holdout_start_index - validation_end_index - 1 < plan.purge_sessions:
        raise SplitValidationError("Validation/holdout purge buffer is too short")


def split_label_records(
    records: Iterable[T],
    plan: ChronologicalSplitPlan,
    *,
    date_field: str = "execution_date",
    label_end_field: str = "label_end_date",
) -> SplitAssignment:
    train: list[Mapping[str, Any]] = []
    validation: list[Mapping[str, Any]] = []
    holdout: list[Mapping[str, Any]] = []
    purged: list[Mapping[str, Any]] = []
    for record in records:
        try:
            observation_date = date.fromisoformat(str(record[date_field]))
            label_end = date.fromisoformat(str(record[label_end_field]))
        except (KeyError, ValueError) as exc:
            raise SplitValidationError(
                f"Invalid split dates in record: {record!r}"
            ) from exc
        if label_end < observation_date:
            raise SplitValidationError("Label ends before its observation")
        if observation_date < plan.train_start:
            purged.append(record)
        elif observation_date <= plan.train_end:
            if label_end >= plan.validation_start:
                purged.append(record)
            else:
                train.append(record)
        elif observation_date < plan.validation_start:
            purged.append(record)
        elif observation_date <= plan.validation_end:
            if label_end >= plan.holdout_start:
                purged.append(record)
            else:
                validation.append(record)
        elif observation_date < plan.holdout_start:
            purged.append(record)
        elif observation_date <= plan.holdout_end:
            holdout.append(record)
        else:
            purged.append(record)
    return SplitAssignment(
        train=tuple(train),
        validation=tuple(validation),
        holdout=tuple(holdout),
        purged=tuple(purged),
    )


def freeze_split_plan(plan: ChronologicalSplitPlan, path) -> dict[str, Any]:
    payload = plan.to_dict()
    existing = read_json(path)
    if existing is not None:
        if existing != payload:
            raise HoldoutAccessError(
                "A different split plan is already frozen; holdout dates are immutable"
            )
        return existing
    write_json_atomic(path, payload)
    loaded = read_json(path)
    if loaded != payload:
        raise HoldoutAccessError("Frozen split plan failed read-back verification")
    return loaded


def verify_frozen_split_plan(plan: ChronologicalSplitPlan, path) -> None:
    frozen = read_json(path)
    if frozen is None:
        raise HoldoutAccessError("Split plan has not been frozen")
    if frozen != plan.to_dict():
        raise HoldoutAccessError("Current split plan differs from the frozen holdout")


def claim_final_holdout(
    plan: ChronologicalSplitPlan,
    *,
    frozen_plan_path,
    claim_path,
    evaluator_id: str,
    preregistration_path=None,
    preregistration_hash: str | None = None,
) -> dict[str, Any]:
    verify_frozen_split_plan(plan, frozen_plan_path)
    if claim_path.exists():
        raise HoldoutAccessError("Final holdout has already been claimed")
    if (preregistration_path is None) != (preregistration_hash is None):
        raise HoldoutAccessError(
            "Preregistration path and hash must be supplied together"
        )
    payload = {
        "holdout_id": plan.holdout_id,
        "plan_hash": plan.plan_hash,
        "evaluator_id": evaluator_id,
        "status": "CLAIMED_ONCE",
    }
    if preregistration_path is not None and preregistration_hash is not None:
        preregistered = read_json(preregistration_path)
        if preregistered is None:
            raise HoldoutAccessError("Preregistration file is missing")
        actual_hash = content_hash(preregistered)
        if actual_hash != preregistration_hash:
            raise HoldoutAccessError(
                "Preregistration hash differs from the frozen hash"
            )
        if preregistered.get("holdout_evaluated") is not False:
            raise HoldoutAccessError(
                "Preregistration was not frozen before Holdout evaluation"
            )
        payload["preregistration_path"] = str(preregistration_path)
        payload["preregistration_hash"] = preregistration_hash
    write_json_atomic(claim_path, payload)
    return read_json(claim_path)


def stratified_training_validation_indices(
    records: Sequence[Mapping[str, Any]],
    *,
    label_field: str = "label_top20",
    split_field: str = "split",
    train_fraction: float = 0.80,
    seed: int = 0,
) -> tuple[list[int], list[int]]:
    if not 0 < train_fraction < 1:
        raise SplitValidationError("train_fraction must be between zero and one")
    if any(
        str(record.get(split_field, "")).casefold() in {"holdout", "test"}
        for record in records
    ):
        raise SplitValidationError(
            "Random stratified sampling is forbidden on validation-final holdout data"
        )
    by_class: dict[Any, list[int]] = {}
    for index, record in enumerate(records):
        if label_field not in record:
            raise SplitValidationError(f"Missing class label {label_field}")
        by_class.setdefault(record[label_field], []).append(index)
    rng = random.Random(seed)
    train_indices: list[int] = []
    validation_indices: list[int] = []
    for label in sorted(by_class, key=str):
        indices = list(by_class[label])
        rng.shuffle(indices)
        cut = max(1, min(len(indices) - 1, round(len(indices) * train_fraction)))
        train_indices.extend(indices[:cut])
        validation_indices.extend(indices[cut:])
    return sorted(train_indices), sorted(validation_indices)


@dataclass(frozen=True)
class WalkForwardFold:
    fold: int
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    purge_sessions: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold": self.fold,
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "validation_start": self.validation_start.isoformat(),
            "validation_end": self.validation_end.isoformat(),
            "purge_sessions": self.purge_sessions,
        }


def build_expanding_walk_forward(
    sessions: Iterable[date],
    *,
    initial_train_sessions: int,
    validation_sessions: int,
    step_sessions: int,
    horizon_sessions: int,
    embargo_sessions: int = 0,
    development_end: date | None = None,
) -> list[WalkForwardFold]:
    dates = tuple(sorted(set(sessions)))
    if min(initial_train_sessions, validation_sessions, step_sessions) <= 0:
        raise SplitValidationError("Walk-forward window sizes must be positive")
    if development_end is not None:
        dates = tuple(value for value in dates if value <= development_end)
    gap = horizon_sessions + 1 + embargo_sessions
    first_validation = initial_train_sessions + gap
    output: list[WalkForwardFold] = []
    fold_index = 0
    while first_validation + validation_sessions <= len(dates):
        validation_start_index = first_validation
        validation_end_index = validation_start_index + validation_sessions - 1
        output.append(
            WalkForwardFold(
                fold=fold_index,
                train_start=dates[0],
                train_end=dates[validation_start_index - gap - 1],
                validation_start=dates[validation_start_index],
                validation_end=dates[validation_end_index],
                purge_sessions=gap,
            )
        )
        fold_index += 1
        first_validation += step_sessions
    return output
