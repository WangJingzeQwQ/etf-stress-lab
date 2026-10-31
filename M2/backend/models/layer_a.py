from __future__ import annotations

from dataclasses import dataclass
import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import yaml

from backend.data_pipeline.storage import canonical_file_hash, write_json_atomic
from backend.engine.target_portfolio import TargetPortfolio, validate_target_portfolio
from backend.features.build_features import FEATURE_NAMES


@dataclass(frozen=True)
class ConfidenceMapper:
    thresholds: tuple[float, float, float, float]
    version: str = "g2_validation_quantiles_v1"

    @classmethod
    def fit(cls, confidence: Iterable[float]) -> "ConfidenceMapper":
        values = np.asarray(list(confidence), dtype=float)
        if len(values) < 5 or not np.isfinite(values).all():
            raise ValueError("Confidence mapper requires finite daily confidence")
        thresholds = tuple(
            float(value)
            for value in np.quantile(values, [0.2, 0.4, 0.6, 0.8])
        )
        return cls(thresholds=thresholds)

    def map(self, confidence: float) -> float:
        levels = (0.0, 0.25, 0.50, 0.75, 1.0)
        index = sum(confidence > threshold for threshold in self.thresholds)
        return levels[index]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "thresholds": list(self.thresholds),
            "levels": [0.0, 0.25, 0.5, 0.75, 1.0],
            "fit_scope": "validation_only",
        }


def load_development_panels(
    root: Path,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    train_path = root / "data/modeling/real/train.ndjson"
    validation_path = root / "data/modeling/real/validation.ndjson"
    train = pd.read_json(train_path, lines=True)
    validation = pd.read_json(validation_path, lines=True)
    if set(train["split"]) != {"train"}:
        raise ValueError("Training panel contains non-training rows")
    if set(validation["split"]) != {"validation"}:
        raise ValueError("Validation panel contains non-validation rows")
    return train, validation


def _score_frame(
    frame: pd.DataFrame,
    scores: np.ndarray,
    *,
    top_n: int,
) -> tuple[pd.DataFrame, list[float]]:
    scored = frame[["execution_date", "code", "excess_return", "label_top20"]].copy()
    scored["score"] = scores
    confidences: list[float] = []
    for _, group in scored.groupby("execution_date", sort=True):
        ordered = group.sort_values(["score", "code"], ascending=[False, True])
        selected = ordered.head(min(top_n, len(ordered)))
        median_score = float(group["score"].median())
        confidences.append(float(selected["score"].mean() - median_score))
        rank_correlation = group["score"].rank().corr(
            group["excess_return"].rank()
        )
        daily_ic = (
            float(rank_correlation)
            if rank_correlation is not None and math.isfinite(rank_correlation)
            else 0.0
        )
        scored.loc[group.index, "daily_ic"] = daily_ic
        scored.loc[group.index, "top_mean_excess"] = float(
            selected["excess_return"].mean()
        )
        scored.loc[group.index, "top20_precision"] = float(
            selected["label_top20"].mean()
        )
        scored.loc[group.index, "confidence"] = confidences[-1]
    return scored, confidences


def evaluate_scores(
    frame: pd.DataFrame,
    scores: np.ndarray,
    *,
    top_n: int,
) -> dict[str, Any]:
    scored, confidences = _score_frame(frame, scores, top_n=top_n)
    daily = scored.groupby("execution_date", sort=True).agg(
        daily_ic=("daily_ic", "first"),
        top_mean_excess=("top_mean_excess", "first"),
        top20_precision=("top20_precision", "first"),
        confidence=("confidence", "first"),
    )
    mean_ic = float(daily["daily_ic"].mean())
    std_ic = float(daily["daily_ic"].std(ddof=1))
    return {
        "rows": len(frame),
        "dates": len(daily),
        "mean_rank_ic": mean_ic,
        "rank_ic_std": std_ic,
        "rank_ic_ir": mean_ic / std_ic if std_ic > 0 else 0.0,
        "positive_ic_date_rate": float((daily["daily_ic"] > 0).mean()),
        "mean_top5_excess_return": float(daily["top_mean_excess"].mean()),
        "top20_precision": float(daily["top20_precision"].mean()),
        "mapper": ConfidenceMapper.fit(confidences).to_dict(),
        "daily_confidence": daily["confidence"].astype(float).tolist(),
    }


def _new_model(params: dict[str, Any], seed: int):
    try:
        from lightgbm import LGBMRegressor
    except ImportError as exc:
        raise RuntimeError(
            "LightGBM is required for G2/G3; install the project dependency"
        ) from exc
    return LGBMRegressor(
        objective="regression",
        n_estimators=int(params.get("n_estimators", 300)),
        learning_rate=float(params.get("learning_rate", 0.03)),
        num_leaves=int(params.get("num_leaves", 15)),
        min_child_samples=int(params.get("min_child_samples", 200)),
        subsample=float(params.get("subsample", 0.9)),
        colsample_bytree=float(params.get("colsample_bytree", 0.9)),
        reg_lambda=float(params.get("reg_lambda", 1.0)),
        random_state=seed,
        n_jobs=int(params.get("n_jobs", 4)),
        deterministic=True,
        force_col_wise=True,
        verbosity=-1,
    )


def _fit_predict(
    train: pd.DataFrame,
    target: pd.DataFrame,
    *,
    params: dict[str, Any],
    seed: int,
) -> tuple[np.ndarray, Any]:
    model = _new_model(params, seed)
    model.fit(
        train[list(FEATURE_NAMES)],
        train["excess_return"],
    )
    predictions = np.asarray(
        model.predict(target[list(FEATURE_NAMES)]),
        dtype=float,
    )
    return predictions, model


def train_g2(
    root: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    train, validation = load_development_panels(root)
    model_config = config["models"]
    a_config = model_config["a_layer"]
    seeds = [int(value) for value in model_config["paired_seeds"]]
    if len(seeds) < 5 or len(set(seeds)) != len(seeds):
        raise ValueError("G2 requires at least five unique paired seeds")
    params = dict(a_config.get("params", {}))
    top_n = int(config.get("strategy", {}).get("top_n", 5))
    split_manifest = json.loads(
        (root / str(config["split"]["report_path"])).read_text(encoding="utf-8")
    )
    folds = split_manifest["walk_forward"]["folds"]
    configured_fold_ids = [
        int(value)
        for value in a_config.get("walk_forward_fold_ids", [len(folds) - 1])
    ]
    selected_folds = [folds[index] for index in configured_fold_ids]
    artifact_root = root / "data/models/g2"
    artifact_root.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for seed in seeds:
        walk_forward = []
        for fold in selected_folds:
            train_end = pd.Timestamp(fold["train_end"])
            validation_start = pd.Timestamp(fold["validation_start"])
            validation_end = pd.Timestamp(fold["validation_end"])
            fold_train = train[
                pd.to_datetime(train["execution_date"]) <= train_end
            ]
            fold_validation = validation[
                (pd.to_datetime(validation["execution_date"]) >= validation_start)
                & (
                    pd.to_datetime(validation["execution_date"])
                    <= validation_end
                )
            ]
            predictions, _ = _fit_predict(
                fold_train,
                fold_validation,
                params=params,
                seed=seed,
            )
            metrics = evaluate_scores(
                fold_validation,
                predictions,
                top_n=top_n,
            )
            metrics.pop("daily_confidence", None)
            walk_forward.append(
                {
                    "fold": int(fold["fold"]),
                    "train_rows": len(fold_train),
                    "validation_rows": len(fold_validation),
                    **metrics,
                }
            )
        final_predictions, final_model = _fit_predict(
            train,
            validation,
            params=params,
            seed=seed,
        )
        final_metrics = evaluate_scores(
            validation,
            final_predictions,
            top_n=top_n,
        )
        mapper = ConfidenceMapper.fit(final_metrics.pop("daily_confidence"))
        model_path = artifact_root / f"g2_seed_{seed}.txt"
        temporary_model = model_path.with_suffix(model_path.suffix + ".tmp")
        temporary_model.write_text(
            final_model.booster_.model_to_string(),
            encoding="utf-8",
        )
        temporary_model.replace(model_path)
        results.append(
            {
                "seed": seed,
                "walk_forward": walk_forward,
                "validation": final_metrics,
                "confidence_mapper": mapper.to_dict(),
                "model_path": str(model_path),
                "model_sha256": canonical_file_hash(model_path),
            }
        )
    keys = (
        "mean_rank_ic",
        "rank_ic_ir",
        "mean_top5_excess_return",
        "top20_precision",
    )
    aggregate = {}
    for key in keys:
        values = [float(result["validation"][key]) for result in results]
        standard_error = float(np.std(values, ddof=1) / math.sqrt(len(values)))
        t_critical = 2.776 if len(values) == 5 else 1.96
        aggregate[key] = {
            "mean": float(np.mean(values)),
            "std": float(np.std(values, ddof=1)),
            "min": float(np.min(values)),
            "max": float(np.max(values)),
            "ci95_low": float(np.mean(values) - t_critical * standard_error),
            "ci95_high": float(np.mean(values) + t_critical * standard_error),
        }
    modeling_manifest = json.loads(
        (
            root
            / str(
                config.get("modeling", {}).get(
                    "dataset_report",
                    "reports/modeling_dataset_manifest.json",
                )
            )
        ).read_text(encoding="utf-8")
    )
    report = {
        "status": "COMPLETE",
        "group": "G2",
        "source_kind": "REAL_PROVIDER",
        "data_version": modeling_manifest["data_version"],
        "holdout_evaluated": False,
        "seeds": seeds,
        "feature_names": list(FEATURE_NAMES),
        "train_rows": len(train),
        "validation_rows": len(validation),
        "aggregate": aggregate,
        "seed_results": results,
    }
    report_path = root / str(
        a_config.get("report_path", "reports/g2_seed_results.json")
    )
    write_json_atomic(report_path, report)
    return report


def g2_target_from_scores(
    scores: dict[str, float],
    *,
    signal_date,
    execution_date,
    data_cutoff,
    mapper: ConfidenceMapper,
    top_n: int = 5,
) -> TargetPortfolio:
    if not scores:
        return TargetPortfolio(
            total_r=0.0,
            weights={},
            signal_date=signal_date,
            execution_date=execution_date,
            data_cutoff=data_cutoff,
            group="G2",
            meta={"fallback_reason": "no_candidates"},
        )
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    selected = ordered[: min(top_n, len(ordered))]
    selected_codes = {code for code, _ in selected}
    confidence = float(
        np.mean([score for _, score in selected])
        - np.median(list(scores.values()))
    )
    weights = {code: 1.0 / len(selected) for code in selected_codes}
    total_r = mapper.map(confidence)
    target = TargetPortfolio(
        total_r=total_r,
        weights=weights if total_r > 0 else {},
        signal_date=signal_date,
        execution_date=execution_date,
        data_cutoff=data_cutoff,
        group="G2",
        meta={
            "confidence": confidence,
            "mapper_version": mapper.version,
            "scores": {code: float(score) for code, score in selected},
        },
    )
    validate_target_portfolio(target, set(scores))
    return target


def g3_target_from_scores(
    scores: dict[str, float],
    *,
    signal_date,
    execution_date,
    data_cutoff,
    mapper: ConfidenceMapper,
    top_n: int = 5,
) -> TargetPortfolio:
    target = g2_target_from_scores(
        scores,
        signal_date=signal_date,
        execution_date=execution_date,
        data_cutoff=data_cutoff,
        mapper=mapper,
        top_n=top_n,
    )
    target.group = "G3"
    target.meta["mapper_version"] = "g3_validation_quantiles_v1"
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train G2 LightGBM layer")
    parser.add_argument("--config", default="config.m2_real.yaml")
    args = parser.parse_args(argv)
    root = Path(".").resolve()
    config = yaml.safe_load((root / args.config).read_text(encoding="utf-8"))
    report = train_g2(root, config)
    print(
        json.dumps(
            {
                "status": report["status"],
                "seeds": report["seeds"],
                "train_rows": report["train_rows"],
                "validation_rows": report["validation_rows"],
                "aggregate": report["aggregate"],
                "holdout_evaluated": report["holdout_evaluated"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
