from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Iterable

from backend.engine.models import ZERO, decimal_value


@dataclass(frozen=True)
class TradingRule:
    code: str
    settlement: str
    effective_from: date
    effective_to: date
    limit_pct: Decimal
    known_at: date | None = None
    trading_status: str = "ACTIVE"
    source: str = ""
    evidence_id: str = ""
    evidence_sha256: str | None = None

    def applies(self, trading_date: date) -> bool:
        return self.effective_from <= trading_date <= self.effective_to

    def validate(self) -> None:
        if self.settlement not in {"T0", "T1"}:
            raise ValueError(f"Unsupported settlement rule {self.settlement!r}")
        if self.effective_from > self.effective_to:
            raise ValueError(f"Invalid effective interval for {self.code}")
        if self.limit_pct <= ZERO:
            raise ValueError(f"Limit percentage must be positive for {self.code}")
        if self.limit_pct > Decimal("1"):
            raise ValueError(f"Limit percentage exceeds 100% for {self.code}")
        if self.trading_status not in {"ACTIVE", "SUSPENDED"}:
            raise ValueError(
                f"Unsupported trading status rule {self.trading_status!r}"
            )
        if (self.known_at or self.effective_from) > self.effective_from:
            raise ValueError(
                f"Trading rule known_at follows effective_from for {self.code}"
            )
        if self.evidence_sha256 is not None:
            digest = self.evidence_sha256.removeprefix("sha256:").lower()
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError(
                    f"Invalid evidence SHA-256 for trading rule {self.code}"
                )


@dataclass(frozen=True)
class CorporateAction:
    code: str
    effective_date: date
    known_at: date
    action_type: str
    cash_per_share: Decimal = ZERO
    split_ratio: Decimal = Decimal("1")

    def validate(self) -> None:
        if self.known_at > self.effective_date:
            raise ValueError("Corporate action known_at cannot follow effective_date")
        if self.action_type not in {"cash_dividend", "split"}:
            raise ValueError(f"Unsupported corporate action {self.action_type!r}")
        if self.action_type == "split" and self.split_ratio <= ZERO:
            raise ValueError("Split ratio must be positive")
        if self.cash_per_share < ZERO:
            raise ValueError("Cash dividend cannot be negative")


@dataclass(frozen=True)
class MarketBar:
    code: str
    name: str
    trading_date: date
    raw_open: Decimal
    raw_high: Decimal
    raw_low: Decimal
    raw_close: Decimal
    volume: Decimal
    amount: Decimal
    prev_close: Decimal
    limit_up: Decimal
    limit_down: Decimal
    adv20_amount: Decimal | None
    is_suspended: bool = False
    open_tradable: bool = True
    total_return_close: Decimal | None = None
    factor_effective_date: date | None = None
    factor_known_at: date | None = None
    provider_qfq_close: Decimal | None = None

    def validate(self) -> None:
        if self.raw_low > min(self.raw_open, self.raw_close):
            raise ValueError(f"Invalid raw low for {self.code} on {self.trading_date}")
        if self.raw_high < max(self.raw_open, self.raw_close):
            raise ValueError(f"Invalid raw high for {self.code} on {self.trading_date}")
        if self.raw_low <= ZERO or self.raw_open <= ZERO:
            raise ValueError(f"Non-positive raw price for {self.code}")
        if self.adv20_amount is not None and self.adv20_amount < ZERO:
            raise ValueError(f"Negative ADV20 for {self.code}")
        if self.total_return_close is not None:
            if self.factor_known_at is None or self.factor_effective_date is None:
                raise ValueError(
                    f"Total-return series for {self.code} lacks factor timestamps"
                )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "trading_date": self.trading_date.isoformat(),
            "raw_open": str(self.raw_open),
            "raw_high": str(self.raw_high),
            "raw_low": str(self.raw_low),
            "raw_close": str(self.raw_close),
            "volume": str(self.volume),
            "amount": str(self.amount),
            "prev_close": str(self.prev_close),
            "limit_up": str(self.limit_up),
            "limit_down": str(self.limit_down),
            "adv20_amount": (
                str(self.adv20_amount) if self.adv20_amount is not None else None
            ),
            "is_suspended": self.is_suspended,
            "open_tradable": self.open_tradable,
            "total_return_close": (
                str(self.total_return_close)
                if self.total_return_close is not None
                else None
            ),
            "factor_effective_date": (
                self.factor_effective_date.isoformat()
                if self.factor_effective_date is not None
                else None
            ),
            "factor_known_at": (
                self.factor_known_at.isoformat()
                if self.factor_known_at is not None
                else None
            ),
            "provider_qfq_close": (
                str(self.provider_qfq_close)
                if self.provider_qfq_close is not None
                else None
            ),
        }


@dataclass(frozen=True)
class MarketDataset:
    bars: dict[tuple[date, str], MarketBar]
    sessions: tuple[date, ...]
    codes: tuple[str, ...]
    names: dict[str, str]
    list_dates: dict[str, date]
    trading_rules: tuple[TradingRule, ...]
    corporate_actions: tuple[CorporateAction, ...]
    source_kind: str
    metadata: dict[str, Any]

    @classmethod
    def build(
        cls,
        bars: Iterable[MarketBar],
        *,
        names: dict[str, str],
        list_dates: dict[str, date],
        trading_rules: Iterable[TradingRule],
        corporate_actions: Iterable[CorporateAction] = (),
        source_kind: str,
        metadata: dict[str, Any] | None = None,
    ) -> "MarketDataset":
        bar_map: dict[tuple[date, str], MarketBar] = {}
        sessions: set[date] = set()
        codes: set[str] = set()
        for bar in bars:
            bar.validate()
            key = (bar.trading_date, bar.code)
            if key in bar_map:
                raise ValueError(f"Duplicate bar for {bar.code} on {bar.trading_date}")
            bar_map[key] = bar
            sessions.add(bar.trading_date)
            codes.add(bar.code)
        rules = tuple(sorted(trading_rules, key=lambda item: (item.code, item.effective_from)))
        for rule in rules:
            rule.validate()
        actions = tuple(
            sorted(corporate_actions, key=lambda item: (item.effective_date, item.code))
        )
        for action in actions:
            action.validate()
        dataset = cls(
            bars=bar_map,
            sessions=tuple(sorted(sessions)),
            codes=tuple(sorted(codes)),
            names=dict(names),
            list_dates=dict(list_dates),
            trading_rules=rules,
            corporate_actions=actions,
            source_kind=source_kind,
            metadata=dict(metadata or {}),
        )
        for code in dataset.codes:
            if code not in dataset.names:
                raise ValueError(f"Missing Chinese name for {code}")
        return dataset

    def bar(self, trading_date: date, code: str) -> MarketBar | None:
        return self.bars.get((trading_date, code))

    def require_bar(self, trading_date: date, code: str) -> MarketBar:
        bar = self.bar(trading_date, code)
        if bar is None:
            raise KeyError(f"Missing bar for {code} on {trading_date}")
        return bar

    def session_bars(self, trading_date: date) -> dict[str, MarketBar]:
        return {
            code: bar
            for (session, code), bar in self.bars.items()
            if session == trading_date
        }

    def sessions_through(self, cutoff: date) -> tuple[date, ...]:
        return tuple(session for session in self.sessions if session <= cutoff)

    def rule_for(self, code: str, trading_date: date) -> TradingRule | None:
        rule, error = self.rule_lookup(code, trading_date)
        if error is not None:
            raise ValueError(
                f"{error} for {code} on {trading_date.isoformat()}"
            )
        return rule

    def rule_lookup(
        self, code: str, trading_date: date
    ) -> tuple[TradingRule | None, str | None]:
        matches = [
            rule
            for rule in self.trading_rules
            if rule.code == code and rule.applies(trading_date)
        ]
        if len(matches) > 1:
            return None, "conflicting_trading_rule"
        if not matches:
            return None, "missing_trading_rule"
        return matches[0], None

    def rule_lookup_known_by(
        self,
        code: str,
        trading_date: date,
        data_cutoff: date,
    ) -> tuple[TradingRule | None, str | None]:
        matches = [
            rule
            for rule in self.trading_rules
            if rule.code == code
            and rule.applies(trading_date)
            and (rule.known_at or rule.effective_from) <= data_cutoff
        ]
        if len(matches) > 1:
            return None, "conflicting_trading_rule"
        if not matches:
            return None, "missing_or_unknown_trading_rule"
        return matches[0], None

    def actions_for(self, trading_date: date) -> tuple[CorporateAction, ...]:
        return tuple(
            action
            for action in self.corporate_actions
            if action.effective_date == trading_date
        )

    def as_of(self, cutoff: date) -> "MarketDataset":
        selected_bars = [
            bar for (session, _), bar in self.bars.items() if session <= cutoff
        ]
        selected_sessions = tuple(session for session in self.sessions if session <= cutoff)
        names = {
            code: name
            for code, name in self.names.items()
            if any(bar.code == code for bar in selected_bars)
        }
        return MarketDataset.build(
            selected_bars,
            names=names,
            list_dates={
                code: value for code, value in self.list_dates.items() if code in names
            },
            trading_rules=self.trading_rules,
            corporate_actions=tuple(
                action
                for action in self.corporate_actions
                if action.known_at <= cutoff
            ),
            source_kind=self.source_kind,
            metadata={**self.metadata, "view_cutoff": cutoff.isoformat()},
        )

    @property
    def data_version(self) -> str:
        payload = {
            "source_kind": self.source_kind,
            "metadata": self.metadata,
            "bars": [self.bars[key].canonical_dict() for key in sorted(self.bars)],
            "rules": [
                {
                    "code": rule.code,
                    "settlement": rule.settlement,
                    "effective_from": rule.effective_from.isoformat(),
                    "effective_to": rule.effective_to.isoformat(),
                    "limit_pct": str(rule.limit_pct),
                    "known_at": (
                        rule.known_at.isoformat()
                        if rule.known_at is not None
                        else None
                    ),
                    "trading_status": rule.trading_status,
                    "source": rule.source,
                    "evidence_id": rule.evidence_id,
                    "evidence_sha256": rule.evidence_sha256,
                }
                for rule in self.trading_rules
            ],
            "actions": [
                {
                    "code": action.code,
                    "effective_date": action.effective_date.isoformat(),
                    "known_at": action.known_at.isoformat(),
                    "action_type": action.action_type,
                    "cash_per_share": str(action.cash_per_share),
                    "split_ratio": str(action.split_ratio),
                }
                for action in self.corporate_actions
            ],
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return f"sha256:{sha256(encoded).hexdigest()}"


def decimal_from_row(row: dict[str, Any], field: str) -> Decimal:
    value = row.get(field)
    if value is None or value == "":
        raise ValueError(f"Missing required field {field}")
    return decimal_value(value)

