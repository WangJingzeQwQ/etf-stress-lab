from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import argparse
import json
import math
from pathlib import Path
import statistics
from typing import Any

import yaml

from backend.data_pipeline.build_universe import (
    DailyUniverseBuilder,
    UniverseConfig,
)
from backend.data_pipeline.bundle_store import load_bundle
from backend.data_pipeline.contracts import Phase1Bundle, content_hash
from backend.data_pipeline.models import MarketDataset
from backend.data_pipeline.phase1_fixture import build_phase1_fixture
from backend.data_pipeline.quality import require_quality, validate_bundle
from backend.data_pipeline.storage import (
    canonical_file_hash,
    write_json_atomic,
    write_ndjson_atomic,
)


@dataclass(frozen=True)
class LabelRow:
    signal_date: date
    execution_date: date
    data_cutoff: date
    label_end_date: date
    code: str
    name: str
    forward_return: float
    cross_section_median: float
    excess_return: float
    label_top20: bool
    horizon_sessions: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_date": self.signal_date.isoformat(),
            "execution_date": self.execution_date.isoformat(),
            "data_cutoff": self.data_cutoff.isoformat(),
            "label_end_date": self.label_end_date.isoformat(),
            "code": self.code,
            "name": self.name,
            "forward_return": self.forward_return,
            "cross_section_median": self.cross_section_median,
            "excess_return": self.excess_return,
            "label_top20": self.label_top20,
            "horizon_sessions": self.horizon_sessions,
        }


def _factor_product(
    dataset: MarketDataset,
    *,
    code: str,
    execution_date: date,
    label_end: date,
    event_factors: dict[str, list[tuple[date, date, float]]] | None = None,
) -> float:
    product = 1.0
    if event_factors is None:
        from backend.data_pipeline.build_total_return import _event_factor

        relevant = [
            (
                action.effective_date,
                action.known_at,
                float(_event_factor(action, dataset)),
            )
            for action in dataset.corporate_actions
            if action.code == code
        ]
    else:
        relevant = event_factors.get(code, [])
    for effective_date, known_at, factor in relevant:
        if (
            known_at <= label_end
            and execution_date < effective_date <= label_end
        ):
            product *= factor
    return product


def _event_factor_index(
    dataset: MarketDataset,
) -> dict[str, list[tuple[date, date, float]]]:
    from backend.data_pipeline.build_total_return import _event_factor

    result: dict[str, list[tuple[date, date, float]]] = {}
    for action in dataset.corporate_actions:
        result.setdefault(action.code, []).append(
            (
                action.effective_date,
                action.known_at,
                float(_event_factor(action, dataset)),
            )
        )
    for values in result.values():
        values.sort(key=lambda item: (item[0], item[1]))
    return result


def forward_open_return(
    dataset: MarketDataset,
    *,
    code: str,
    execution_date: date,
    horizon_sessions: int,
) -> tuple[float, date]:
    if horizon_sessions <= 0:
        raise ValueError("horizon_sessions must be positive")
    sessions = dataset.sessions
    if execution_date not in sessions:
        raise ValueError(f"{execution_date} is not a dataset session")
    start_index = sessions.index(execution_date)
    end_index = start_index + horizon_sessions
    if end_index >= len(sessions):
        raise IndexError("label horizon exceeds available sessions")
    label_end = sessions[end_index]
    start_bar = dataset.require_bar(execution_date, code)
    end_bar = dataset.require_bar(label_end, code)
    start_open = float(start_bar.raw_open)
    end_open = float(end_bar.raw_open)
    if start_open <= 0:
        raise ValueError(f"Non-positive adjusted open for {code}")
    window_factor = _factor_product(
        dataset,
        code=code,
        execution_date=execution_date,
        label_end=label_end,
    )
    return end_open / (start_open * window_factor) - 1.0, label_end


def build_cross_sectional_labels(
    bundle: Phase1Bundle,
    *,
    start_execution_date: date,
    end_execution_date: date,
    horizon_sessions: int,
    top_quantile: float = 0.2,
    universe_config: UniverseConfig | None = None,
) -> list[LabelRow]:
    if not 0 < top_quantile < 1:
        raise ValueError("top_quantile must be between 0 and 1")
    sessions = list(bundle.dataset.sessions)
    start_index = next(
        index for index, value in enumerate(sessions) if value >= start_execution_date
    )
    end_index = next(
        index for index, value in enumerate(sessions) if value >= end_execution_date
    )
    output: list[LabelRow] = []
    universe_config = universe_config or UniverseConfig()
    universe_builder = DailyUniverseBuilder(bundle, universe_config)
    event_factors = _event_factor_index(bundle.dataset)
    for index in range(
        max(1, start_index),
        min(end_index + 1, len(sessions) - horizon_sessions),
    ):
        execution_date = sessions[index]
        signal_date = sessions[index - 1]
        label_end = sessions[index + horizon_sessions]
        decisions = universe_builder.build(
            data_cutoff=signal_date,
            execution_date=execution_date,
        )
        candidates = {decision.code for decision in decisions if decision.eligible}
        returns: dict[str, tuple[float, date]] = {}
        for code in sorted(candidates):
            try:
                start_bar = bundle.dataset.require_bar(execution_date, code)
                end_bar = bundle.dataset.require_bar(label_end, code)
                start_open = float(start_bar.raw_open)
                end_open = float(end_bar.raw_open)
                window_factor = _factor_product(
                    bundle.dataset,
                    code=code,
                    execution_date=execution_date,
                    label_end=label_end,
                    event_factors=event_factors,
                )
                returns[code] = (
                    end_open / (start_open * window_factor) - 1.0,
                    label_end,
                )
            except (KeyError, IndexError, ValueError):
                continue
        if not returns:
            continue
        median_return = statistics.median(
            value for value, _ in returns.values()
        )
        excess = {
            code: value - median_return for code, (value, _) in returns.items()
        }
        top_count = max(1, math.ceil(len(excess) * top_quantile))
        top_codes = {
            code
            for code, _ in sorted(
                excess.items(), key=lambda item: (-item[1], item[0])
            )[:top_count]
        }
        for code, (value, label_end) in sorted(returns.items()):
            output.append(
                LabelRow(
                    signal_date=signal_date,
                    execution_date=execution_date,
                    data_cutoff=signal_date,
                    label_end_date=label_end,
                    code=code,
                    name=bundle.master[code].name_at(signal_date),
                    forward_return=value,
                    cross_section_median=median_return,
                    excess_return=excess[code],
                    label_top20=code in top_codes,
                    horizon_sessions=horizon_sessions,
                )
            )
    return output


def materialize_labels(
    labels: list[LabelRow],
    output_path: Path,
    *,
    source_kind: str,
    data_version: str,
    horizon_sessions: int,
    formal_research_allowed: bool = False,
) -> None:
    payloads = [row.to_dict() for row in labels]
    write_ndjson_atomic(output_path, payloads)
    write_json_atomic(
        output_path.with_suffix(".json"),
        {
            "source_kind": source_kind,
            "data_version": data_version,
            "rows": len(labels),
            "rows_sha256": content_hash(payloads),
            "artifact_sha256": canonical_file_hash(output_path),
            "row_key_fields": ["execution_date", "code"],
            "horizon_sessions": horizon_sessions,
            "date_from": (
                min(row.execution_date for row in labels).isoformat()
                if labels
                else None
            ),
            "date_to": (
                max(row.execution_date for row in labels).isoformat()
                if labels
                else None
            ),
            "start_definition": "execution_date raw_open adjusted by known actions",
            "end_definition": "execution_date + horizon raw_open adjusted by known actions",
            "cross_section": "return minus same-date median",
            "classification": "top 20 percent by excess return",
            "formal_research_allowed": formal_research_allowed,
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Phase 2 forward labels")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    data = config["data"]
    if config.get("data_pipeline", {}).get("source") == "stored_bundle":
        bundle = load_bundle(
            Path(str(config["data_pipeline"].get("bundle_root", "data")))
        )
    else:
        bundle = build_phase1_fixture(
            date.fromisoformat(str(data["start_date"])),
            date.fromisoformat(str(data["end_date"])),
            seed=int(data["fixture_seed"]),
        )
    formal_requested = bool(
        config.get("data_pipeline", {}).get("formal_research", False)
    )
    quality = validate_bundle(bundle, formal_research=formal_requested)
    require_quality(quality)
    formal_allowed = (
        formal_requested
        and bundle.manifest_data.source_kind == "REAL_PROVIDER"
        and quality.formal_research_allowed
    )
    horizon = int(config["labels"]["horizon_sessions"])
    labels = build_cross_sectional_labels(
        bundle,
        start_execution_date=date.fromisoformat(args.start),
        end_execution_date=date.fromisoformat(args.end),
        horizon_sessions=horizon,
        top_quantile=float(config["labels"]["top_quantile"]),
        universe_config=UniverseConfig.from_mapping(config["universe"]),
    )
    output = (
        Path(args.output)
        if args.output
        else Path("data/labels") / f"labels_H{horizon}.ndjson"
    )
    materialize_labels(
        labels,
        output,
        source_kind=bundle.manifest_data.source_kind,
        data_version=bundle.manifest_data.data_version,
        horizon_sessions=horizon,
        formal_research_allowed=formal_allowed,
    )
    print(
        json.dumps(
            {
                "rows": len(labels),
                "horizon_sessions": horizon,
                "output": str(output),
                "source_kind": bundle.manifest_data.source_kind,
                "data_version": bundle.manifest_data.data_version,
                "formal_research_allowed": formal_allowed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

