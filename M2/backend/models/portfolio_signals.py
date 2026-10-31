from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

import numpy as np
import pandas as pd
import torch

from backend.data_pipeline.models import MarketDataset
from backend.features.build_features import FEATURE_NAMES
from backend.models.env import SignalWeights
from backend.models.layer_a import ConfidenceMapper
from backend.models.layer_c import (
    LstmRepresentation,
    SequenceBundle,
    build_sequence_bundle,
)
from backend.models.random_baseline import g5_random_target
from backend.engine.target_portfolio import TargetPortfolio


MODEL_COLUMNS = (
    "execution_date",
    "data_cutoff",
    "signal_date",
    "code",
    "name",
    "excess_return",
    "label_top20",
    *FEATURE_NAMES,
)


def read_development_panel(root: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    train = pd.read_json(root / "data/modeling/real/train.ndjson", lines=True)
    validation = pd.read_json(
        root / "data/modeling/real/validation.ndjson",
        lines=True,
    )
    if set(train["split"]) != {"train"}:
        raise ValueError("Training input contains non-training rows")
    if set(validation["split"]) != {"validation"}:
        raise ValueError("Validation input contains non-validation rows")
    return train, validation


def read_feature_prefix(
    path: Path,
    *,
    end_cutoff: date,
) -> pd.DataFrame:
    """Read only the development prefix without opening final-holdout rows."""

    rows: list[dict[str, Any]] = []
    cutoff_text = end_cutoff.isoformat()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            row_cutoff = str(payload.get("data_cutoff", ""))
            if row_cutoff > cutoff_text:
                break
            if row_cutoff < cutoff_text:
                rows.append(payload)
            else:
                rows.append(payload)
    if not rows:
        raise ValueError(f"No feature history through {end_cutoff}")
    return pd.DataFrame(rows)


def weekly_execution_dates(
    dataset: MarketDataset,
    *,
    start: date,
    end: date,
    every_sessions: int = 5,
) -> tuple[date, ...]:
    if every_sessions <= 0:
        raise ValueError("every_sessions must be positive")
    sessions = [
        session for session in dataset.sessions if start <= session <= end
    ]
    if len(sessions) < 2:
        raise ValueError("Insufficient sessions for weekly schedule")
    return tuple(sessions[::every_sessions])


def monthly_execution_dates(
    dataset: MarketDataset,
    *,
    start: date,
    end: date,
) -> tuple[date, ...]:
    sessions = [
        session for session in dataset.sessions if start <= session <= end
    ]
    selected = []
    previous_month: tuple[int, int] | None = None
    for session in sessions:
        month = (session.year, session.month)
        if previous_month is not None and month != previous_month:
            selected.append(session)
        previous_month = month
    return tuple(selected)


def _equal_weights(codes: Iterable[str]) -> dict[str, float]:
    ordered = sorted(set(codes))
    if not ordered:
        return {}
    base = 1.0 / len(ordered)
    weights = {}
    cumulative = 0.0
    for index, code in enumerate(ordered):
        if index == len(ordered) - 1:
            weights[code] = 1.0 - cumulative
        else:
            weights[code] = base
            cumulative += base
    return weights


def _report_seed(report: Mapping[str, Any], seed: int) -> Mapping[str, Any]:
    for row in report["seed_results"]:
        if int(row["seed"]) == seed:
            return row
    raise ValueError(f"Seed {seed} is absent from a frozen model report")


def _booster_predict(path: Path, features: np.ndarray) -> np.ndarray:
    import lightgbm as lgb

    if not path.exists():
        raise FileNotFoundError(path)
    # LightGBM's C API is not consistently Unicode-path safe on Windows.
    booster = lgb.Booster(model_str=path.read_text(encoding="utf-8"))
    return np.asarray(booster.predict(features), dtype=float)


def _transform_bundle(
    bundle: SequenceBundle,
    scaler: Mapping[str, Any],
) -> SequenceBundle:
    mean = np.asarray(scaler["mean"], dtype=np.float32)
    std = np.asarray(scaler["std"], dtype=np.float32)
    return SequenceBundle(
        sequences=np.clip((bundle.sequences - mean) / std, -12.0, 12.0),
        current=np.clip((bundle.current - mean) / std, -12.0, 12.0),
        targets=bundle.targets,
    )


def _encode(
    encoder: LstmRepresentation,
    bundle: SequenceBundle,
    batch_size: int = 4096,
) -> np.ndarray:
    outputs = []
    with torch.no_grad():
        values = torch.from_numpy(bundle.sequences.astype(np.float32))
        for start in range(0, len(values), batch_size):
            representation, _ = encoder(values[start : start + batch_size])
            outputs.append(representation.cpu().numpy())
    return np.vstack(outputs) if outputs else np.empty((0, 0))


def predict_scores(
    root: Path,
    *,
    group: str,
    seed: int,
    rows: pd.DataFrame,
    feature_panel: pd.DataFrame,
) -> np.ndarray:
    group = group.upper()
    if group == "G2":
        return _booster_predict(
            root / f"data/models/g2/g2_seed_{seed}.txt",
            rows[list(FEATURE_NAMES)].to_numpy(dtype=np.float32),
        )
    if group not in {"G3", "G4", "G6"}:
        raise ValueError("Score prediction is available for G2, G3, G4 or G6")
    report = json.loads(
        (root / "reports/g3_seed_results.json").read_text(encoding="utf-8")
    )
    seed_report = _report_seed(report, seed)
    c_path = Path(str(seed_report["c_model_path"]))
    a_path = Path(str(seed_report["a_model_path"]))
    payload = torch.load(c_path, map_location="cpu", weights_only=False)
    encoder = LstmRepresentation(
        input_size=len(FEATURE_NAMES),
        hidden_size=int(payload["hidden_size"]),
    )
    encoder.load_state_dict(payload["state_dict"])
    encoder.eval()
    bundle = build_sequence_bundle(
        feature_panel,
        rows,
        sequence_length=int(payload["sequence_length"]),
    )
    normalised = _transform_bundle(bundle, payload["scaler"])
    representation = _encode(encoder, normalised)
    combined = np.concatenate([normalised.current, representation], axis=1)
    return _booster_predict(a_path, combined)


def selected_panel_rows(
    panel: pd.DataFrame,
    signal_dates: Iterable[date],
) -> pd.DataFrame:
    accepted = {value.isoformat() for value in signal_dates}
    return panel[panel["data_cutoff"].astype(str).isin(accepted)].copy()


def score_rows(
    root: Path,
    *,
    group: str,
    seed: int,
    rows: pd.DataFrame,
    feature_panel: pd.DataFrame,
) -> dict[str, dict[str, float]]:
    if rows.empty:
        return {}
    scores = predict_scores(
        root,
        group=group,
        seed=seed,
        rows=rows,
        feature_panel=feature_panel,
    )
    output: dict[str, dict[str, float]] = {}
    for cutoff, group_rows in rows.assign(score=scores).groupby("data_cutoff"):
        output[str(cutoff)] = {
            str(row.code): float(row.score)
            for row in group_rows.itertuples()
        }
    return output


def market_states_by_cutoff(
    panel: pd.DataFrame,
    cutoffs: Iterable[date],
) -> dict[date, tuple[float, ...]]:
    accepted = {value.isoformat() for value in cutoffs}
    selected = panel[panel["data_cutoff"].astype(str).isin(accepted)]
    output: dict[date, tuple[float, ...]] = {}
    for cutoff, group_rows in selected.groupby("data_cutoff", sort=True):
        values = group_rows[list(FEATURE_NAMES)].mean(axis=0)
        output[date.fromisoformat(str(cutoff))] = tuple(
            float(value) for value in values
        )
    return output


def build_g2_g3_targets(
    root: Path,
    *,
    group: str,
    seed: int,
    rows: pd.DataFrame,
    feature_panel: pd.DataFrame,
    execution_dates: Iterable[date],
    dataset: MarketDataset,
    top_n: int = 5,
) -> tuple[dict[date, TargetPortfolio], dict[date, tuple[float, ...]]]:
    group = group.upper()
    report_path = root / f"reports/{group.lower()}_seed_results.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    seed_report = _report_seed(report, seed)
    mapper_payload = seed_report["confidence_mapper"]
    mapper = ConfidenceMapper(
        thresholds=tuple(float(value) for value in mapper_payload["thresholds"]),
        version=str(mapper_payload["version"]),
    )
    session_index = {value: index for index, value in enumerate(dataset.sessions)}
    schedule: dict[date, TargetPortfolio] = {}
    for execution in execution_dates:
        index = session_index.get(execution)
        if index is None or index == 0:
            continue
        signal = dataset.sessions[index - 1]
        selected = rows[rows["data_cutoff"].astype(str) == signal.isoformat()]
        if selected.empty:
            continue
        scores_map = score_rows(
            root,
            group=group,
            seed=seed,
            rows=selected,
            feature_panel=feature_panel,
        )
        scores = scores_map.get(signal.isoformat(), {})
        if not scores:
            continue
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        selected_codes = [code for code, _ in ordered[: min(top_n, len(ordered))]]
        confidence = float(
            np.mean([scores[code] for code in selected_codes])
            - np.median(list(scores.values()))
        )
        total_r = mapper.map(confidence)
        schedule[execution] = TargetPortfolio(
            total_r=total_r,
            weights=_equal_weights(selected_codes) if total_r > 0 else {},
            signal_date=signal,
            execution_date=execution,
            data_cutoff=signal,
            group=group,
            meta={
                "seed": seed,
                "confidence": confidence,
                "selected_scores": {
                    code: scores[code] for code in selected_codes
                },
                "mapper_version": mapper.version,
            },
        )
    states = market_states_by_cutoff(
        rows,
        [target.signal_date for target in schedule.values()],
    )
    return schedule, states


def build_g1_targets(
    *,
    rows: pd.DataFrame,
    dataset: MarketDataset,
    execution_dates: Iterable[date],
    top_n: int = 5,
    frequency: str = "monthly",
) -> dict[date, TargetPortfolio]:
    session_index = {value: index for index, value in enumerate(dataset.sessions)}
    schedule: dict[date, TargetPortfolio] = {}
    for execution in execution_dates:
        index = session_index.get(execution)
        if index is None or index == 0:
            continue
        signal = dataset.sessions[index - 1]
        selected = rows[rows["data_cutoff"].astype(str) == signal.isoformat()]
        if selected.empty:
            continue
        ordered = selected.sort_values(
            ["momentum_20", "code"],
            ascending=[False, True],
        ).head(top_n)
        codes = [str(code) for code in ordered["code"]]
        schedule[execution] = TargetPortfolio(
            total_r=1.0,
            weights=_equal_weights(codes),
            signal_date=signal,
            execution_date=execution,
            data_cutoff=signal,
            group="G1",
            meta={
                "frequency": frequency,
                "ranking": [
                    {
                        "code": str(row.code),
                        "name": str(row.name),
                        "momentum_20": float(row.momentum_20),
                    }
                    for row in ordered.itertuples()
                ],
            },
        )
    return schedule


def build_g5_targets(
    *,
    rows: pd.DataFrame,
    dataset: MarketDataset,
    execution_dates: Iterable[date],
    seed: int,
    top_n: int = 5,
) -> dict[date, TargetPortfolio]:
    session_index = {value: index for index, value in enumerate(dataset.sessions)}
    schedule = {}
    for execution in execution_dates:
        index = session_index.get(execution)
        if index is None or index == 0:
            continue
        signal = dataset.sessions[index - 1]
        selected = rows[rows["data_cutoff"].astype(str) == signal.isoformat()]
        if selected.empty:
            continue
        schedule[execution] = g5_random_target(
            codes=selected["code"].astype(str),
            seed=seed,
            signal_date=signal,
            execution_date=execution,
            top_n=top_n,
        )
    return schedule


def weight_schedule(
    targets: Mapping[date, TargetPortfolio],
) -> dict[date, SignalWeights]:
    return {
        execution: SignalWeights(
            signal_date=target.signal_date,
            execution_date=execution,
            weights={
                str(code): float(weight)
                for code, weight in target.weights.items()
            }
            if target.weights
            else _equal_weights(target.meta.get("selected_codes", [])),
            allowed_codes=frozenset(target.weights),
        )
        for execution, target in targets.items()
    }


def g3_weight_schedule(
    root: Path,
    *,
    seed: int,
    rows: pd.DataFrame,
    feature_panel: pd.DataFrame,
    execution_dates: Iterable[date],
    dataset: MarketDataset,
    top_n: int = 5,
) -> dict[date, SignalWeights]:
    execution_set = set(execution_dates)
    group_rows = selected_panel_rows(
        rows,
        [
            dataset.sessions[index - 1]
            for index, session in enumerate(dataset.sessions)
            if session in execution_set and index > 0
        ],
    )
    scores_by_cutoff = score_rows(
        root,
        group="G3",
        seed=seed,
        rows=group_rows,
        feature_panel=feature_panel,
    )
    session_index = {value: index for index, value in enumerate(dataset.sessions)}
    schedule: dict[date, SignalWeights] = {}
    for execution in sorted(execution_set):
        index = session_index.get(execution)
        if index is None or index == 0:
            continue
        signal = dataset.sessions[index - 1]
        scores = scores_by_cutoff.get(signal.isoformat(), {})
        if not scores:
            continue
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        codes = [code for code, _ in ordered[: min(top_n, len(ordered))]]
        schedule[execution] = SignalWeights(
            signal_date=signal,
            execution_date=execution,
            weights=_equal_weights(codes),
            allowed_codes=frozenset(scores),
        )
    return schedule

