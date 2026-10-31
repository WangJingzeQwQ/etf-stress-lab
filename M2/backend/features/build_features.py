from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import argparse
from bisect import bisect_right
import json
import math
from pathlib import Path
import statistics
from typing import Any

import yaml

from backend.data_pipeline.build_total_return import (
    _event_factor,
)
from backend.data_pipeline.build_universe import (
    UniverseConfig,
    build_daily_universe,
)
from backend.data_pipeline.bundle_store import load_bundle
from backend.data_pipeline.contracts import content_hash
from backend.data_pipeline.models import MarketBar, MarketDataset
from backend.data_pipeline.phase1_fixture import build_phase1_fixture
from backend.data_pipeline.quality import require_quality, validate_bundle
from backend.data_pipeline.storage import (
    canonical_file_hash,
    write_json_atomic,
    write_ndjson_atomic,
)


FEATURE_NAMES = (
    "momentum_5",
    "momentum_10",
    "momentum_20",
    "momentum_60",
    "volatility_20",
    "downside_vol_20",
    "price_to_ma20",
    "price_to_ma60",
    "rsi14",
    "amount_ratio_5_20",
    "volume_ratio_5_20",
    "amihud_20",
    "drawdown_60",
    "range_20",
)


@dataclass(frozen=True)
class FeatureRow:
    data_cutoff: date
    code: str
    name: str
    values: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_cutoff": self.data_cutoff.isoformat(),
            "code": self.code,
            "name": self.name,
            **self.values,
        }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _return_series(values: list[float]) -> list[float]:
    return [
        current / previous - 1.0
        for previous, current in zip(values, values[1:])
        if previous > 0
    ]


def _feature_values(
    bars: list[MarketBar],
    adjusted_closes: list[float] | None = None,
) -> dict[str, float] | None:
    if len(bars) < 61:
        return None
    adjusted = (
        adjusted_closes
        if adjusted_closes is not None
        else [
            float(bar.total_return_close)
            for bar in bars
            if bar.total_return_close
        ]
    )
    if len(adjusted) < 61:
        return None
    if len(adjusted) != len(bars):
        return None
    returns = _return_series(adjusted)
    if len(returns) < 60:
        return None
    latest = adjusted[-1]
    momentum = {
        f"momentum_{window}": latest / adjusted[-window - 1] - 1.0
        for window in (5, 10, 20, 60)
    }
    recent_returns = returns[-20:]
    downside_returns = [min(value, 0.0) for value in recent_returns]
    gains = [max(value, 0.0) for value in returns[-14:]]
    losses = [max(-value, 0.0) for value in returns[-14:]]
    average_gain = _mean(gains)
    average_loss = _mean(losses)
    if average_loss == 0:
        rsi = 100.0 if average_gain > 0 else 50.0
    else:
        rsi = 100.0 - 100.0 / (1.0 + average_gain / average_loss)
    amount5 = _mean([float(bar.amount) for bar in bars[-5:]])
    amount20 = _mean([float(bar.amount) for bar in bars[-20:]])
    volume5 = _mean([float(bar.volume) for bar in bars[-5:]])
    volume20 = _mean([float(bar.volume) for bar in bars[-20:]])
    amihud_samples = [
        abs(change) / float(bar.amount) * 1_000_000_000.0
        for change, bar in zip(returns[-20:], bars[-20:])
        if bar.amount > 0
    ]
    recent60 = adjusted[-60:]
    drawdown = latest / max(recent60) - 1.0
    recent_bars = bars[-20:]
    recent_adjusted_highs = []
    recent_adjusted_lows = []
    for bar, adjusted_close in zip(recent_bars, adjusted[-20:]):
        factor = adjusted_close / float(bar.raw_close)
        recent_adjusted_highs.append(float(bar.raw_high) * factor)
        recent_adjusted_lows.append(float(bar.raw_low) * factor)
    range20 = (
        max(recent_adjusted_highs)
        / min(recent_adjusted_lows)
        - 1.0
    )
    result = {
        **momentum,
        "volatility_20": statistics.pstdev(recent_returns),
        "downside_vol_20": statistics.pstdev(downside_returns),
        "price_to_ma20": latest / _mean(adjusted[-20:]) - 1.0,
        "price_to_ma60": latest / _mean(adjusted[-60:]) - 1.0,
        "rsi14": rsi,
        "amount_ratio_5_20": amount5 / amount20 - 1.0 if amount20 > 0 else 0.0,
        "volume_ratio_5_20": volume5 / volume20 - 1.0 if volume20 > 0 else 0.0,
        "amihud_20": _mean(amihud_samples),
        "drawdown_60": drawdown,
        "range_20": range20,
    }
    if any(not math.isfinite(value) for value in result.values()):
        return None
    return result


class PointInTimeFeatureBuilder:
    """Reusable full-history feature builder without per-date dataset copies."""

    def __init__(self, dataset: MarketDataset) -> None:
        histories: dict[str, list[MarketBar]] = {
            code: [] for code in dataset.codes
        }
        for (_, code), bar in dataset.bars.items():
            histories[code].append(bar)
        for bars in histories.values():
            bars.sort(key=lambda bar: bar.trading_date)
        self._histories = histories
        self._dates = {
            code: [bar.trading_date for bar in bars]
            for code, bars in histories.items()
        }
        factors: dict[str, list[tuple[date, date, Decimal]]] = {}
        for action in dataset.corporate_actions:
            factors.setdefault(action.code, []).append(
                (
                    action.effective_date,
                    action.known_at,
                    _event_factor(action, dataset),
                )
            )
        for values in factors.values():
            values.sort(key=lambda item: item[0])
        self._factors = factors

    def _adjusted_closes(
        self,
        code: str,
        bars: list[MarketBar],
        data_cutoff: date,
    ) -> list[float]:
        factors = [
            (effective_date, factor)
            for effective_date, known_at, factor in self._factors.get(code, [])
            if known_at <= data_cutoff and effective_date <= data_cutoff
        ]
        factor_index = len(factors) - 1
        cumulative = 1.0
        output = [0.0] * len(bars)
        cumulative_decimal = Decimal("1")
        for index in range(len(bars) - 1, -1, -1):
            while (
                factor_index >= 0
                and factors[factor_index][0] > bars[index].trading_date
            ):
                cumulative_decimal *= factors[factor_index][1]
                factor_index -= 1
            output[index] = float(
                bars[index].raw_close * cumulative_decimal
            )
        return output

    def build(
        self,
        *,
        data_cutoff: date,
        universe_codes: set[str],
        names: dict[str, str],
    ) -> list[FeatureRow]:
        rows: list[FeatureRow] = []
        for code in sorted(universe_codes):
            history = self._histories.get(code, [])
            end = bisect_right(self._dates.get(code, []), data_cutoff)
            if end < 61:
                continue
            bars = history[end - 61 : end]
            adjusted = self._adjusted_closes(code, bars, data_cutoff)
            values = _feature_values(bars, adjusted)
            if values is None:
                continue
            rows.append(
                FeatureRow(
                    data_cutoff=data_cutoff,
                    code=code,
                    name=names[code],
                    values=values,
                )
            )
        return rows


def build_feature_snapshot(
    dataset: MarketDataset,
    *,
    data_cutoff: date,
    universe_codes: set[str],
    names: dict[str, str] | None = None,
) -> list[FeatureRow]:
    return PointInTimeFeatureBuilder(dataset).build(
        data_cutoff=data_cutoff,
        universe_codes=universe_codes,
        names=names or dataset.names,
    )


def build_feature_panel(
    bundle,
    *,
    start_cutoff: date,
    end_cutoff: date,
    universe_config,
) -> list[FeatureRow]:
    from backend.data_pipeline.build_universe import DailyUniverseBuilder

    sessions = bundle.dataset.sessions
    if start_cutoff > end_cutoff:
        raise ValueError("Feature panel start date follows end date")
    first = bisect_right(sessions, start_cutoff)
    if first > 0 and sessions[first - 1] == start_cutoff:
        first -= 1
    last = min(bisect_right(sessions, end_cutoff), len(sessions) - 1)
    universe_builder = DailyUniverseBuilder(bundle, universe_config)
    feature_builder = PointInTimeFeatureBuilder(bundle.dataset)
    rows: list[FeatureRow] = []
    for index in range(max(1, first), last):
        cutoff = sessions[index]
        execution = sessions[index + 1]
        decisions = universe_builder.build(
            data_cutoff=cutoff,
            execution_date=execution,
        )
        eligible = {item.code: item for item in decisions if item.eligible}
        rows.extend(
            feature_builder.build(
                data_cutoff=cutoff,
                universe_codes=set(eligible),
                names={
                    code: item.name
                    for code, item in eligible.items()
                },
            )
        )
    return rows


def materialize_features(
    rows: list[FeatureRow],
    output_path: Path,
    *,
    source_kind: str,
    data_version: str,
    formal_research_allowed: bool = False,
) -> Path:
    payloads = [row.to_dict() for row in rows]
    write_ndjson_atomic(output_path, payloads)
    write_json_atomic(
        output_path.with_suffix(".json"),
        {
            "data_cutoff": rows[0].data_cutoff.isoformat() if rows else None,
            "date_from": (
                min(row.data_cutoff for row in rows).isoformat()
                if rows
                else None
            ),
            "date_to": (
                max(row.data_cutoff for row in rows).isoformat()
                if rows
                else None
            ),
            "dates": len({row.data_cutoff for row in rows}),
            "source_kind": source_kind,
            "data_version": data_version,
            "rows": len(rows),
            "rows_sha256": content_hash(payloads),
            "artifact_sha256": canonical_file_hash(output_path),
            "row_key_fields": ["data_cutoff", "code"],
            "feature_names": list(FEATURE_NAMES),
            "feature_count": len(FEATURE_NAMES),
            "execution_price_usage": "none",
            "label_visibility": "features use data <= data_cutoff only",
            "formal_research_allowed": formal_research_allowed,
        },
    )
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Phase 2 feature snapshot")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--date", default=None)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
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
    universe_config = UniverseConfig.from_mapping(config["universe"])
    if args.date and (args.start or args.end):
        parser.error("--date cannot be combined with --start/--end")
    if args.date:
        cutoff = date.fromisoformat(args.date)
        execution = next(
            session for session in bundle.dataset.sessions if session > cutoff
        )
        decisions = build_daily_universe(
            bundle,
            data_cutoff=cutoff,
            execution_date=execution,
            config=universe_config,
        )
        rows = build_feature_snapshot(
            bundle.dataset,
            data_cutoff=cutoff,
            universe_codes={
                decision.code for decision in decisions if decision.eligible
            },
            names={
                decision.code: decision.name
                for decision in decisions
                if decision.eligible
            },
        )
    elif args.start and args.end:
        cutoff = date.fromisoformat(args.start)
        execution = None
        rows = build_feature_panel(
            bundle,
            start_cutoff=cutoff,
            end_cutoff=date.fromisoformat(args.end),
            universe_config=universe_config,
        )
    else:
        parser.error("provide --date or both --start and --end")
    output = (
        Path(args.output)
        if args.output
        else (
            Path("data/features") / f"features_{cutoff.strftime('%Y%m%d')}.ndjson"
            if args.date
            else Path("data/features/real")
            / f"features_panel_{cutoff.strftime('%Y%m%d')}.ndjson"
        )
    )
    materialize_features(
        rows,
        output,
        source_kind=bundle.manifest_data.source_kind,
        data_version=bundle.manifest_data.data_version,
        formal_research_allowed=formal_allowed,
    )
    print(
        json.dumps(
            {
                "data_cutoff": cutoff.isoformat(),
                "execution_date": execution.isoformat() if execution else None,
                "rows": len(rows),
                "dates": len({row.data_cutoff for row in rows}),
                "features": len(FEATURE_NAMES),
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

