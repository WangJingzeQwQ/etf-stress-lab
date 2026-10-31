from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
import argparse
import json
from pathlib import Path

from backend.data_pipeline.contracts import DataQualityError
from backend.data_pipeline.models import CorporateAction, MarketDataset
from backend.data_pipeline.phase1_fixture import build_phase1_fixture
from backend.data_pipeline.storage import write_json_atomic, write_ndjson_atomic


def _event_factor(
    action: CorporateAction,
    dataset: MarketDataset,
) -> Decimal:
    if action.action_type == "split":
        if action.split_ratio <= 0:
            raise DataQualityError(f"Invalid split ratio for {action.code}")
        return Decimal("1") / action.split_ratio
    if action.action_type == "cash_dividend":
        prior_sessions = [
            session
            for session in dataset.sessions
            if session < action.effective_date
            and dataset.bar(session, action.code) is not None
        ]
        if not prior_sessions:
            raise DataQualityError(
                f"No prior close for dividend adjustment of {action.code}"
            )
        previous_close = dataset.require_bar(
            prior_sessions[-1], action.code
        ).raw_close
        factor = (previous_close - action.cash_per_share) / previous_close
        if factor <= 0:
            raise DataQualityError(
                f"Non-positive dividend adjustment factor for {action.code}"
            )
        return factor
    raise DataQualityError(f"Unsupported corporate action {action.action_type}")


def materialize_point_in_time_total_returns(
    dataset: MarketDataset,
    data_cutoff: date,
) -> MarketDataset:
    view = dataset.as_of(data_cutoff)
    visible_actions = [
        action
        for action in view.corporate_actions
        if action.known_at <= data_cutoff and action.effective_date <= data_cutoff
    ]
    factors = {
        (action.code, action.effective_date): _event_factor(action, view)
        for action in visible_actions
    }
    adjusted_bars = []
    for (session, code), bar in sorted(view.bars.items()):
        cumulative = Decimal("1")
        for action in visible_actions:
            if (
                action.code == code
                and action.effective_date > session
                and action.effective_date <= data_cutoff
            ):
                cumulative *= factors[(code, action.effective_date)]
        adjusted_bars.append(
            replace(
                bar,
                total_return_close=bar.raw_close * cumulative,
                factor_effective_date=data_cutoff,
                factor_known_at=data_cutoff,
            )
        )
    return MarketDataset.build(
        adjusted_bars,
        names=view.names,
        list_dates=view.list_dates,
        trading_rules=view.trading_rules,
        corporate_actions=view.corporate_actions,
        source_kind=view.source_kind,
        metadata={
            **view.metadata,
            "total_return_method": "point_in_time_action_factors_v1",
            "return_data_cutoff": data_cutoff.isoformat(),
            "execution_fields_remain_raw": True,
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build point-in-time total-return fields from fixture actions"
    )
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)
    bundle = build_phase1_fixture()
    cutoff = date.fromisoformat(args.date)
    dataset = materialize_point_in_time_total_returns(bundle.dataset, cutoff)
    output = (
        Path(args.output)
        if args.output
        else Path("data/derived") / f"total_return_{cutoff.strftime('%Y%m%d')}.ndjson"
    )
    write_ndjson_atomic(
        output,
        (
            dataset.bars[key].canonical_dict()
            for key in sorted(dataset.bars)
        ),
    )
    manifest_path = output.with_suffix(".json")
    write_json_atomic(
        manifest_path,
        {
            "data_cutoff": cutoff.isoformat(),
            "data_version": dataset.data_version,
            "source_kind": dataset.source_kind,
            "price_fields": {
                "execution_allowed": "raw_open/raw_high/raw_low/raw_close",
                "feature_only": "total_return_close",
            },
            "bars_file": output.name,
            "bar_count": len(dataset.bars),
            "formal_research_allowed": False,
        },
    )
    print(
        json.dumps(
            {
                "data_cutoff": cutoff.isoformat(),
                "data_version": dataset.data_version,
                "codes": list(dataset.codes),
                "output": str(output),
                "manifest": str(manifest_path),
                "note": "Corporate action factors are visible only when known_at <= cutoff.",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

