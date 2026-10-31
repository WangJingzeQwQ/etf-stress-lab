from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from backend.data_pipeline.contracts import (
    DataQualityError,
    Phase1Bundle,
    content_hash,
)
from backend.data_pipeline.bundle_store import load_bundle
from backend.data_pipeline.models import MarketBar, TradingRule
from backend.data_pipeline.phase1_fixture import build_phase1_fixture
from backend.data_pipeline.storage import write_json_atomic, write_ndjson_atomic
from backend.engine.models import ZERO


@dataclass(frozen=True)
class UniverseConfig:
    min_listed_days: int = 60
    min_history_sessions: int = 60
    adv_window: int = 20
    min_adv_amount: Decimal = Decimal("20000000")

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "UniverseConfig":
        config = cls(
            min_listed_days=int(payload["min_listed_sessions"]),
            min_history_sessions=int(payload["min_listed_sessions"]),
            adv_window=int(payload["adv_window"]),
            min_adv_amount=Decimal(str(payload["min_adv_amount"])),
        )
        if min(
            config.min_listed_days,
            config.min_history_sessions,
            config.adv_window,
        ) <= 0:
            raise ValueError("Universe windows must be positive")
        if config.min_adv_amount < ZERO:
            raise ValueError("Minimum ADV cannot be negative")
        return config


@dataclass(frozen=True)
class UniverseDecision:
    execution_date: date
    data_cutoff: date
    code: str
    name: str
    eligible: bool
    reasons: tuple[str, ...]
    list_date: date
    delist_date: date | None
    status: str | None
    bar_count: int
    listed_calendar_days: int
    adv20_amount: Decimal | None
    settlement: str | None
    limit_pct: Decimal | None
    master_history_complete: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_date": self.execution_date.isoformat(),
            "data_cutoff": self.data_cutoff.isoformat(),
            "code": self.code,
            "name": self.name,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "list_date": self.list_date.isoformat(),
            "delist_date": (
                self.delist_date.isoformat() if self.delist_date is not None else None
            ),
            "status": self.status,
            "bar_count": self.bar_count,
            "listed_calendar_days": self.listed_calendar_days,
            "adv20_amount": (
                str(self.adv20_amount) if self.adv20_amount is not None else None
            ),
            "settlement": self.settlement,
            "limit_pct": str(self.limit_pct) if self.limit_pct is not None else None,
            "master_history_complete": self.master_history_complete,
        }


class DailyUniverseBuilder:
    """Reusable point-in-time universe index for full history panels.

    The one-date helper used to rebuild a complete dataset view and rescan
    every session for every code.  Full-history labels need the same decision
    logic thousands of times, so this class indexes code histories once and
    applies only point-in-time filters on each call.
    """

    def __init__(
        self,
        bundle: Phase1Bundle,
        config: UniverseConfig,
    ) -> None:
        self.bundle = bundle
        self.config = config
        histories: dict[str, list[MarketBar]] = {
            code: [] for code in bundle.dataset.codes
        }
        for (_, code), bar in bundle.dataset.bars.items():
            histories[code].append(bar)
        for code in bundle.dataset.codes:
            histories[code].sort(key=lambda bar: bar.trading_date)
        self._histories = histories
        self._dates = {
            code: [bar.trading_date for bar in bars]
            for code, bars in histories.items()
        }
        rules_by_code: dict[str, list] = {}
        for rule in bundle.dataset.trading_rules:
            rules_by_code.setdefault(rule.code, []).append(rule)
        for rules in rules_by_code.values():
            rules.sort(key=lambda rule: rule.effective_from)
        self._rules_by_code = rules_by_code

    def _rule_lookup_known_by(
        self,
        code: str,
        execution_date: date,
        data_cutoff: date,
    ) -> tuple[TradingRule | None, str | None]:
        matches = [
            rule
            for rule in self._rules_by_code.get(code, [])
            if rule.applies(execution_date)
            and (rule.known_at or rule.effective_from) <= data_cutoff
        ]
        if len(matches) > 1:
            return None, "conflicting_trading_rule"
        if not matches:
            return None, "missing_or_unknown_trading_rule"
        return matches[0], None

    def build(
        self,
        *,
        data_cutoff: date,
        execution_date: date,
    ) -> tuple[UniverseDecision, ...]:
        if data_cutoff >= execution_date:
            raise ValueError("data_cutoff must be earlier than execution_date")
        decisions: list[UniverseDecision] = []
        config = self.config
        for code in sorted(self.bundle.master):
            record = self.bundle.master[code]
            reasons: list[str] = []
            history = self._histories.get(code, [])
            history_end = bisect_right(self._dates.get(code, []), data_cutoff)
            bar_count = history_end
            listed_days = max(0, (data_cutoff - record.list_date).days)
            try:
                name = record.name_at(data_cutoff)
            except DataQualityError:
                name = self.bundle.dataset.names.get(code, "")
                reasons.append("missing_point_in_time_name")
            try:
                status = record.status_at(execution_date, data_cutoff)
            except DataQualityError:
                status = None
                reasons.append("missing_or_unknown_status")

            if not record.history_complete:
                reasons.append("incomplete_master_history")
            if record.list_date > data_cutoff:
                reasons.append("not_yet_listed")
            if listed_days < config.min_listed_days:
                reasons.append("listed_less_than_minimum_days")
            if record.delist_date is not None and execution_date > record.delist_date:
                reasons.append("delisted_before_execution")
            if status != "ACTIVE":
                reasons.append(f"status_{status or 'unknown'}")
            if bar_count == 0:
                reasons.append("missing_price_history")
            if bar_count < config.min_history_sessions:
                reasons.append("insufficient_price_history")
            if (
                history_end == 0
                or self._dates[code][history_end - 1] != data_cutoff
            ):
                reasons.append("missing_t_prev_bar")

            rule, rule_error = self._rule_lookup_known_by(
                code,
                execution_date,
                data_cutoff,
            )
            if rule_error is not None:
                reasons.append(rule_error)

            recent_bars = history[
                max(0, history_end - config.adv_window) : history_end
            ]
            adv20 = (
                sum((bar.amount for bar in recent_bars), ZERO)
                / Decimal(len(recent_bars))
                if recent_bars
                else None
            )
            if adv20 is None or adv20 < config.min_adv_amount:
                reasons.append("adv20_below_threshold")

            decisions.append(
                UniverseDecision(
                    execution_date=execution_date,
                    data_cutoff=data_cutoff,
                    code=code,
                    name=name,
                    eligible=not reasons,
                    reasons=tuple(dict.fromkeys(reasons)),
                    list_date=record.list_date,
                    delist_date=record.delist_date,
                    status=status,
                    bar_count=bar_count,
                    listed_calendar_days=listed_days,
                    adv20_amount=adv20,
                    settlement=rule.settlement if rule is not None else None,
                    limit_pct=rule.limit_pct if rule is not None else None,
                    master_history_complete=record.history_complete,
                )
            )
        return tuple(decisions)


def build_daily_universe(
    bundle: Phase1Bundle,
    *,
    data_cutoff: date,
    execution_date: date,
    config: UniverseConfig,
) -> tuple[UniverseDecision, ...]:
    return DailyUniverseBuilder(bundle, config).build(
        data_cutoff=data_cutoff,
        execution_date=execution_date,
    )


def materialize_universe(
    bundle: Phase1Bundle,
    decisions: tuple[UniverseDecision, ...],
    output_dir: Path,
) -> dict[str, Path]:
    if not decisions:
        raise ValueError("No universe decisions to write")
    execution_date = decisions[0].execution_date
    output_dir.mkdir(parents=True, exist_ok=True)
    universe_path = output_dir / f"universe_{execution_date.strftime('%Y%m%d')}.ndjson"
    rows = [decision.to_dict() for decision in decisions]
    write_ndjson_atomic(universe_path, rows)
    manifest_path = output_dir / f"universe_{execution_date.strftime('%Y%m%d')}.json"
    eligible_codes = [decision.code for decision in decisions if decision.eligible]
    write_json_atomic(
        manifest_path,
        {
            "execution_date": execution_date.isoformat(),
            "data_cutoff": decisions[0].data_cutoff.isoformat(),
            "source_kind": bundle.manifest_data.source_kind,
            "source_data_version": bundle.manifest_data.data_version,
            "universe_hash": content_hash(rows),
            "eligible_count": len(eligible_codes),
            "eligible_codes": eligible_codes,
            "rejected_count": len(decisions) - len(eligible_codes),
            "audit_file": universe_path.name,
            "real_market_evidence_allowed": (
                bundle.manifest_data.real_market_evidence_allowed
            ),
        },
    )
    return {"universe": universe_path, "manifest": manifest_path}


def _load_config(
    path: Path,
    bundle_root: Path,
) -> tuple[Phase1Bundle, UniverseConfig]:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    data = config["data"]
    if (bundle_root / "meta" / "data_manifest.json").exists():
        bundle = load_bundle(bundle_root)
    else:
        bundle = build_phase1_fixture(
            date.fromisoformat(str(data["start_date"])),
            date.fromisoformat(str(data["end_date"])),
            seed=int(data["fixture_seed"]),
        )
    return bundle, UniverseConfig.from_mapping(config["universe"])


def _previous_session(bundle: Phase1Bundle, execution_date: date) -> date:
    candidates = [session for session in bundle.dataset.sessions if session < execution_date]
    if not candidates:
        raise ValueError("No prior session available for data cutoff")
    return candidates[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build audited Phase 1 daily universe")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--execution-date", default=None)
    parser.add_argument("--output", default="reports/phase1")
    parser.add_argument("--bundle-root", default="data")
    args = parser.parse_args(argv)
    bundle, universe_config = _load_config(Path(args.config), Path(args.bundle_root))
    execution_date = (
        date.fromisoformat(args.execution_date)
        if args.execution_date
        else bundle.dataset.sessions[-1]
    )
    data_cutoff = _previous_session(bundle, execution_date)
    decisions = build_daily_universe(
        bundle,
        data_cutoff=data_cutoff,
        execution_date=execution_date,
        config=universe_config,
    )
    paths = materialize_universe(bundle, decisions, Path(args.output))
    print(
        json.dumps(
            {
                "execution_date": execution_date.isoformat(),
                "data_cutoff": data_cutoff.isoformat(),
                "eligible": [item.code for item in decisions if item.eligible],
                "files": {key: str(value) for key, value in paths.items()},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

