from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import random
from typing import Any

import numpy as np
import pandas as pd
import torch
from torch import nn
import yaml

from backend.data_pipeline.storage import canonical_file_hash, write_json_atomic
from backend.features.build_features import FEATURE_NAMES
from backend.models.layer_a import (
    ConfidenceMapper,
    _new_model,
    evaluate_scores,
)


class LstmRepresentation(nn.Module):
    def __init__(self, *, input_size: int, hidden_size: int) -> None:
        super().__init__()
        self.encoder = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=1,
            batch_first=True,
        )
        self.head = nn.Linear(hidden_size, 1)

    def forward(self, values: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        _, (hidden, _) = self.encoder(values)
        representation = hidden[-1]
        prediction = self.head(representation).squeeze(-1)
        return representation, prediction


@dataclass
class SequenceBundle:
    sequences: np.ndarray
    current: np.ndarray
    targets: np.ndarray


def _set_determinism(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.set_num_threads(min(4, torch.get_num_threads()))
    torch.use_deterministic_algorithms(True)


def _load_panel(path: Path) -> pd.DataFrame:
    frame = pd.read_json(path, lines=True)
    required = {
        "execution_date",
        "data_cutoff",
        "code",
        "excess_return",
        "label_top20",
        *FEATURE_NAMES,
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Modeling panel misses columns: {sorted(missing)}")
    return frame


def build_sequence_bundle(
    feature_panel: pd.DataFrame,
    rows: pd.DataFrame,
    *,
    sequence_length: int,
) -> SequenceBundle:
    ordered = feature_panel.sort_values(["code", "data_cutoff"]).reset_index(
        drop=True
    )
    features = ordered[list(FEATURE_NAMES)].to_numpy(dtype=np.float32)
    key_index = pd.MultiIndex.from_arrays(
        [ordered["data_cutoff"].astype(str), ordered["code"].astype(str)]
    )
    requested = pd.MultiIndex.from_arrays(
        [rows["data_cutoff"].astype(str), rows["code"].astype(str)]
    )
    positions = key_index.get_indexer(requested)
    if (positions < 0).any():
        missing = int((positions < 0).sum())
        raise ValueError(f"{missing} modeling rows lack a feature-history row")
    code_values = ordered["code"].to_numpy(dtype=object)
    local_position = np.empty(len(ordered), dtype=np.int32)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and code_values[end] == code_values[start]:
            end += 1
        local_position[start:end] = np.arange(end - start, dtype=np.int32)
        start = end

    sequences = np.zeros(
        (len(rows), sequence_length, len(FEATURE_NAMES)),
        dtype=np.float32,
    )
    for row_index, feature_position in enumerate(positions):
        position = int(feature_position)
        history_length = min(
            sequence_length,
            int(local_position[position]) + 1,
        )
        history_start = position - history_length + 1
        sequence = features[history_start : position + 1]
        if history_length < sequence_length:
            sequence = np.vstack(
                [
                    np.zeros(
                        (
                            sequence_length - history_length,
                            len(FEATURE_NAMES),
                        ),
                        dtype=np.float32,
                    ),
                    sequence,
                ]
            )
        sequences[row_index] = sequence
    return SequenceBundle(
        sequences=sequences,
        current=rows[list(FEATURE_NAMES)].to_numpy(dtype=np.float32),
        targets=rows["excess_return"].to_numpy(dtype=np.float32),
    )


def normalise_sequences(
    train: SequenceBundle,
    validation: SequenceBundle,
) -> tuple[SequenceBundle, SequenceBundle, dict[str, list[float]]]:
    mean = train.current.mean(axis=0, dtype=np.float64).astype(np.float32)
    std = train.current.std(axis=0, dtype=np.float64).astype(np.float32)
    std[std < 1e-8] = 1.0

    def transform(bundle: SequenceBundle) -> SequenceBundle:
        sequences = np.clip(
            (bundle.sequences - mean) / std,
            -12.0,
            12.0,
        ).astype(np.float32)
        current = np.clip(
            (bundle.current - mean) / std,
            -12.0,
            12.0,
        ).astype(np.float32)
        return SequenceBundle(
            sequences=sequences,
            current=current,
            targets=bundle.targets,
        )

    metadata = {
        "feature_names": list(FEATURE_NAMES),
        "mean": mean.tolist(),
        "std": std.tolist(),
        "fit_scope": "train_only",
    }
    return transform(train), transform(validation), metadata


def _train_encoder(
    train: SequenceBundle,
    validation: SequenceBundle,
    *,
    seed: int,
    hidden_size: int,
    epochs: int,
    batch_size: int,
    learning_rate: float,
) -> tuple[LstmRepresentation, dict[str, Any]]:
    _set_determinism(seed)
    model = LstmRepresentation(
        input_size=len(FEATURE_NAMES),
        hidden_size=hidden_size,
    )
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    loss_function = nn.MSELoss()
    train_x = torch.from_numpy(train.sequences)
    train_y = torch.from_numpy(train.targets)
    validation_x = torch.from_numpy(validation.sequences)
    validation_y = torch.from_numpy(validation.targets)
    best_loss = float("inf")
    best_state = {
        key: value.detach().clone()
        for key, value in model.state_dict().items()
    }
    history = []
    generator = torch.Generator().manual_seed(seed)
    for epoch in range(epochs):
        model.train()
        order = torch.randperm(len(train_x), generator=generator)
        running = 0.0
        for start in range(0, len(order), batch_size):
            batch_indices = order[start : start + batch_size]
            optimizer.zero_grad(set_to_none=True)
            _, predictions = model(train_x[batch_indices])
            loss = loss_function(predictions, train_y[batch_indices])
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
            running += float(loss.item()) * len(batch_indices)
        model.eval()
        with torch.no_grad():
            validation_losses = []
            for start in range(0, len(validation_x), batch_size):
                _, predictions = model(
                    validation_x[start : start + batch_size]
                )
                validation_losses.append(
                    float(
                        loss_function(
                            predictions,
                            validation_y[start : start + batch_size],
                        ).item()
                    )
                )
            validation_loss = float(np.mean(validation_losses))
        history.append(
            {
                "epoch": epoch + 1,
                "train_mse": running / len(train_x),
                "validation_mse": validation_loss,
            }
        )
        if validation_loss < best_loss:
            best_loss = validation_loss
            best_state = {
                key: value.detach().clone()
                for key, value in model.state_dict().items()
            }
    model.load_state_dict(best_state)
    model.eval()
    return model, {
        "best_validation_mse": best_loss,
        "epochs_completed": epochs,
        "selected_epoch": min(
            history,
            key=lambda item: item["validation_mse"],
        )["epoch"],
        "history": history,
    }


def _encode(
    model: LstmRepresentation,
    bundle: SequenceBundle,
    *,
    batch_size: int,
) -> np.ndarray:
    output = []
    with torch.no_grad():
        values = torch.from_numpy(bundle.sequences)
        for start in range(0, len(values), batch_size):
            encoded, _ = model(values[start : start + batch_size])
            output.append(encoded.cpu().numpy())
    return np.vstack(output)


def train_g3(
    root: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    train = _load_panel(root / "data/modeling/real/train.ndjson")
    validation = _load_panel(root / "data/modeling/real/validation.ndjson")
    if set(train["split"]) != {"train"} or set(validation["split"]) != {
        "validation"
    }:
        raise ValueError("G3 received rows outside train/validation partitions")
    feature_panel_path = root / "data/features/real/features_panel.ndjson"
    feature_panel = pd.read_json(feature_panel_path, lines=True)
    development_cutoff = str(validation["data_cutoff"].max())
    feature_panel = feature_panel[
        feature_panel["data_cutoff"].astype(str) <= development_cutoff
    ].copy()
    model_config = config["models"]
    c_config = model_config["c_layer"]
    seeds = [int(value) for value in model_config["paired_seeds"]]
    sequence_length = int(c_config.get("sequence_length", 20))
    hidden_size = int(c_config.get("hidden_size", 16))
    epochs = int(c_config.get("epochs", 6))
    batch_size = int(c_config.get("batch_size", 4096))
    train_bundle = build_sequence_bundle(
        feature_panel,
        train,
        sequence_length=sequence_length,
    )
    validation_bundle = build_sequence_bundle(
        feature_panel,
        validation,
        sequence_length=sequence_length,
    )
    train_bundle, validation_bundle, scaler = normalise_sequences(
        train_bundle,
        validation_bundle,
    )
    a_params = dict(model_config["a_layer"].get("params", {}))
    top_n = int(config.get("strategy", {}).get("top_n", 5))
    artifact_root = root / "data/models/g3"
    artifact_root.mkdir(parents=True, exist_ok=True)
    modeling_manifest = json.loads(
        (
            root / "reports/modeling_dataset_manifest.json"
        ).read_text(encoding="utf-8")
    )
    results = []
    for seed in seeds:
        encoder, encoder_metadata = _train_encoder(
            train_bundle,
            validation_bundle,
            seed=seed,
            hidden_size=hidden_size,
            epochs=epochs,
            batch_size=batch_size,
            learning_rate=float(c_config.get("learning_rate", 0.001)),
        )
        train_encoded = _encode(encoder, train_bundle, batch_size=batch_size)
        validation_encoded = _encode(
            encoder,
            validation_bundle,
            batch_size=batch_size,
        )
        a_train = np.concatenate([train_bundle.current, train_encoded], axis=1)
        a_validation = np.concatenate(
            [validation_bundle.current, validation_encoded],
            axis=1,
        )
        model = _new_model(a_params, seed)
        model.fit(a_train, train_bundle.targets)
        predictions = np.asarray(
            model.predict(a_validation),
            dtype=float,
        )
        metrics = evaluate_scores(validation, predictions, top_n=top_n)
        mapper = ConfidenceMapper.fit(metrics.pop("daily_confidence"))
        model_path = artifact_root / f"g3_a_seed_{seed}.txt"
        temporary_model = model_path.with_suffix(model_path.suffix + ".tmp")
        temporary_model.write_text(
            model.booster_.model_to_string(),
            encoding="utf-8",
        )
        temporary_model.replace(model_path)
        encoder_path = artifact_root / f"g3_c_seed_{seed}.pt"
        with encoder_path.open("wb") as handle:
            torch.save(
                {
                    "state_dict": encoder.state_dict(),
                    "hidden_size": hidden_size,
                    "sequence_length": sequence_length,
                    "seed": seed,
                    "scaler": scaler,
                },
                handle,
            )
        results.append(
            {
                "seed": seed,
                "encoder": encoder_metadata,
                "validation": metrics,
                "confidence_mapper": mapper.to_dict(),
                "a_model_path": str(model_path),
                "a_model_sha256": canonical_file_hash(model_path),
                "c_model_path": str(encoder_path),
                "c_model_sha256": canonical_file_hash(encoder_path),
            }
        )
    aggregate = {}
    for key in (
        "mean_rank_ic",
        "rank_ic_ir",
        "mean_top5_excess_return",
        "top20_precision",
    ):
        values = [float(row["validation"][key]) for row in results]
        standard_error = float(np.std(values, ddof=1) / np.sqrt(len(values)))
        t_critical = 2.776 if len(values) == 5 else 1.96
        aggregate[key] = {
            "mean": float(np.mean(values)),
            "std": float(np.std(values, ddof=1)),
            "min": float(np.min(values)),
            "max": float(np.max(values)),
            "ci95_low": float(np.mean(values) - t_critical * standard_error),
            "ci95_high": float(np.mean(values) + t_critical * standard_error),
        }
    report = {
        "status": "COMPLETE",
        "group": "G3",
        "source_kind": "REAL_PROVIDER",
        "data_version": modeling_manifest["data_version"],
        "holdout_evaluated": False,
        "seeds": seeds,
        "sequence_length": sequence_length,
        "hidden_size": hidden_size,
        "development_feature_cutoff": development_cutoff,
        "train_rows": len(train),
        "validation_rows": len(validation),
        "scaler": scaler,
        "aggregate": aggregate,
        "seed_results": results,
        "fairness": {
            "a_layer_features": "14 current features + C representation",
            "c_only_difference": "LSTM representation added to the same current features",
            "a_layer_params": a_params,
            "paired_seeds": seeds,
        },
    }
    report_path = root / "reports/g3_seed_results.json"
    write_json_atomic(report_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train G3 LSTM plus LightGBM")
    parser.add_argument("--config", default="config.m2_real.yaml")
    args = parser.parse_args(argv)
    root = Path(".").resolve()
    config = yaml.safe_load((root / args.config).read_text(encoding="utf-8"))
    report = train_g3(root, config)
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
