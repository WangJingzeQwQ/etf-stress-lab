from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Iterable

from backend.data_pipeline.models import MarketDataset
from backend.engine.models import ZERO, decimal_value


VALID_STATUSES = {"ACTIVE", "SUSPENDED", "DELISTED"}
VALID_SOURCE_KINDS = {
    "SYNTHETIC_FIXTURE",
    "REAL_PROVIDER",
    "REAL_PROVIDER_PROVISIONAL",
    "PARAMETERIZED_FIXTURE",
}


class DataQualityError(ValueError):
    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class NameHistoryPoint:
    name: str
    effective_from: date
    known_at: date

    def validate(self) -> None:
        if not self.name.strip():
            raise DataQualityError("ETF name cannot be empty")
        if self.known_at > self.effective_from:
            raise DataQualityError("Name known_at cannot follow effective_from")


@dataclass(frozen=True)
class StatusHistoryPoint:
    status: str
    effective_from: date
    known_at: date

    def validate(self) -> None:
        if self.status not in VALID_STATUSES:
            raise DataQualityError(f"Unsupported ETF status: {self.status}")
        if self.known_at > self.effective_from:
            raise DataQualityError("Status known_at cannot follow effective_from")


@dataclass(frozen=True)
class MasterRecord:
    code: str
    list_date: date
    delist_date: date | None
    name_history: tuple[NameHistoryPoint, ...]
    status_history: tuple[StatusHistoryPoint, ...]
    history_complete: bool
    source: str

    def validate(self) -> None:
        if not self.code.strip():
            raise DataQualityError("ETF code cannot be empty")
        if self.list_date > date.today():
            raise DataQualityError(f"List date is in the future for {self.code}")
        if self.delist_date is not None and self.delist_date < self.list_date:
            raise DataQualityError(f"Delist date precedes list date for {self.code}")
        if not self.name_history:
            raise DataQualityError(f"Missing name history for {self.code}", code=self.code)
        if not self.status_history:
            raise DataQualityError(
                f"Missing status history for {self.code}", code=self.code
            )
        for point in self.name_history:
            point.validate()
        for point in self.status_history:
            point.validate()
        first_name = min(point.effective_from for point in self.name_history)
        first_status = min(point.effective_from for point in self.status_history)
        if first_name > self.list_date:
            raise DataQualityError(
                f"Name history begins after list date for {self.code}", code=self.code
            )
        if first_status > self.list_date:
            raise DataQualityError(
                f"Status history begins after list date for {self.code}", code=self.code
            )

    def name_at(self, data_cutoff: date) -> str:
        candidates = [
            point
            for point in self.name_history
            if point.effective_from <= data_cutoff and point.known_at <= data_cutoff
        ]
        if not candidates:
            raise DataQualityError(
                f"No point-in-time name known for {self.code} at {data_cutoff}",
                code=self.code,
            )
        return max(
            candidates, key=lambda point: (point.effective_from, point.known_at)
        ).name

    def status_at(self, trading_date: date, data_cutoff: date) -> str:
        candidates = [
            point
            for point in self.status_history
            if point.effective_from <= trading_date and point.known_at <= data_cutoff
        ]
        if not candidates:
            raise DataQualityError(
                f"No trading status known for {self.code} on {trading_date} "
                f"using cutoff {data_cutoff}",
                code=self.code,
            )
        return max(
            candidates, key=lambda point: (point.effective_from, point.known_at)
        ).status

    def former_names(self, data_cutoff: date) -> list[str]:
        visible = [
            point
            for point in sorted(
                self.name_history,
                key=lambda point: (point.effective_from, point.known_at),
            )
            if point.effective_from <= data_cutoff and point.known_at <= data_cutoff
        ]
        return list(dict.fromkeys(point.name for point in visible))

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "list_date": self.list_date.isoformat(),
            "delist_date": (
                self.delist_date.isoformat() if self.delist_date is not None else None
            ),
            "name_history": [
                {
                    "name": point.name,
                    "effective_from": point.effective_from.isoformat(),
                    "known_at": point.known_at.isoformat(),
                }
                for point in sorted(
                    self.name_history,
                    key=lambda point: (point.effective_from, point.known_at),
                )
            ],
            "status_history": [
                {
                    "status": point.status,
                    "effective_from": point.effective_from.isoformat(),
                    "known_at": point.known_at.isoformat(),
                }
                for point in sorted(
                    self.status_history,
                    key=lambda point: (point.effective_from, point.known_at),
                )
            ],
            "history_complete": self.history_complete,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MasterRecord":
        record = cls(
            code=str(payload["code"]),
            list_date=date.fromisoformat(str(payload["list_date"])),
            delist_date=(
                date.fromisoformat(str(payload["delist_date"]))
                if payload.get("delist_date")
                else None
            ),
            name_history=tuple(
                NameHistoryPoint(
                    name=str(item["name"]),
                    effective_from=date.fromisoformat(str(item["effective_from"])),
                    known_at=date.fromisoformat(str(item["known_at"])),
                )
                for item in payload["name_history"]
            ),
            status_history=tuple(
                StatusHistoryPoint(
                    status=str(item["status"]),
                    effective_from=date.fromisoformat(str(item["effective_from"])),
                    known_at=date.fromisoformat(str(item["known_at"])),
                )
                for item in payload["status_history"]
            ),
            history_complete=bool(payload["history_complete"]),
            source=str(payload["source"]),
        )
        record.validate()
        return record


@dataclass(frozen=True)
class RawDailyRecord:
    code: str
    name: str
    trading_date: date
    raw_open: Decimal
    raw_high: Decimal
    raw_low: Decimal
    raw_close: Decimal
    volume: Decimal
    amount: Decimal
    provider: str
    adjustment: str = "none"
    volume_unit: str = "shares"
    amount_currency: str = "CNY"

    def validate(self) -> None:
        if self.adjustment != "none":
            raise DataQualityError(
                f"Adjusted price is forbidden in raw execution data: {self.code}"
            )
        if self.volume_unit != "shares":
            raise DataQualityError(
                f"Volume must be canonical shares, got {self.volume_unit}"
            )
        if self.amount_currency != "CNY":
            raise DataQualityError(
                f"Unsupported amount currency {self.amount_currency}"
            )
        if not self.code or not self.name.strip():
            raise DataQualityError("Raw daily row requires code and Chinese name")
        if min(self.raw_open, self.raw_high, self.raw_low, self.raw_close) <= ZERO:
            raise DataQualityError(
                f"Non-positive raw OHLC for {self.code} on {self.trading_date}"
            )
        if self.raw_low > min(self.raw_open, self.raw_close):
            raise DataQualityError(
                f"Invalid raw_low for {self.code} on {self.trading_date}"
            )
        if self.raw_high < max(self.raw_open, self.raw_close):
            raise DataQualityError(
                f"Invalid raw_high for {self.code} on {self.trading_date}"
            )
        if self.volume < ZERO or self.amount < ZERO:
            raise DataQualityError(
                f"Negative volume or amount for {self.code} on {self.trading_date}"
            )

    def to_dict(self) -> dict[str, Any]:
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
            "provider": self.provider,
            "adjustment": self.adjustment,
            "volume_unit": self.volume_unit,
            "amount_currency": self.amount_currency,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RawDailyRecord":
        record = cls(
            code=str(payload["code"]),
            name=str(payload["name"]),
            trading_date=date.fromisoformat(str(payload["trading_date"])),
            raw_open=decimal_value(payload["raw_open"]),
            raw_high=decimal_value(payload["raw_high"]),
            raw_low=decimal_value(payload["raw_low"]),
            raw_close=decimal_value(payload["raw_close"]),
            volume=decimal_value(payload["volume"]),
            amount=decimal_value(payload["amount"]),
            provider=str(payload["provider"]),
            adjustment=str(payload.get("adjustment", "none")),
            volume_unit=str(payload.get("volume_unit", "shares")),
            amount_currency=str(payload.get("amount_currency", "CNY")),
        )
        record.validate()
        return record


@dataclass(frozen=True)
class CoverageFlags:
    raw_ohlcv: bool
    historical_master: bool
    delist_history: bool
    corporate_actions: bool
    trading_rules: bool
    cost_schedules: bool

    @property
    def all_required(self) -> bool:
        return all(
            (
                self.raw_ohlcv,
                self.historical_master,
                self.delist_history,
                self.corporate_actions,
                self.trading_rules,
                self.cost_schedules,
            )
        )

    def to_dict(self) -> dict[str, bool]:
        return {
            "raw_ohlcv": self.raw_ohlcv,
            "historical_master": self.historical_master,
            "delist_history": self.delist_history,
            "corporate_actions": self.corporate_actions,
            "trading_rules": self.trading_rules,
            "cost_schedules": self.cost_schedules,
        }


@dataclass(frozen=True)
class DataManifest:
    source_kind: str
    source_name: str
    data_version: str
    coverage: CoverageFlags
    notes: tuple[str, ...] = ()
    real_market_evidence_allowed: bool = False

    def validate(self) -> None:
        if self.source_kind not in VALID_SOURCE_KINDS:
            raise DataQualityError(f"Unknown source kind: {self.source_kind}")
        if self.source_kind == "REAL_PROVIDER" and not self.coverage.all_required:
            raise DataQualityError(
                "Real provider manifest has incomplete historical coverage"
            )
        if self.source_kind != "REAL_PROVIDER" and self.real_market_evidence_allowed:
            raise DataQualityError(
                "Synthetic or parameterized data cannot allow real-market evidence"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_name": self.source_name,
            "data_version": self.data_version,
            "coverage": self.coverage.to_dict(),
            "notes": list(self.notes),
            "real_market_evidence_allowed": self.real_market_evidence_allowed,
        }


@dataclass
class Phase1Bundle:
    dataset: MarketDataset
    master: dict[str, MasterRecord]
    manifest_data: DataManifest
    issues: list[str] = field(default_factory=list)

    def validate(self) -> None:
        for code, record in self.master.items():
            if record.code != code:
                raise DataQualityError(f"Master key/code mismatch for {code}")
            record.validate()
        self.manifest_data.validate()


def canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def content_hash(payload: Any) -> str:
    return f"sha256:{sha256(canonical_bytes(payload)).hexdigest()}"


def bundle_fingerprint(
    dataset: MarketDataset,
    master: Iterable[MasterRecord],
) -> str:
    rules = [
        {
            "code": rule.code,
            "settlement": rule.settlement,
            "effective_from": rule.effective_from.isoformat(),
            "effective_to": rule.effective_to.isoformat(),
            "limit_pct": str(rule.limit_pct),
            "known_at": (
                rule.known_at.isoformat() if rule.known_at is not None else None
            ),
            "trading_status": rule.trading_status,
            "source": rule.source,
            "evidence_id": rule.evidence_id,
            "evidence_sha256": rule.evidence_sha256,
        }
        for rule in sorted(
            dataset.trading_rules,
            key=lambda item: (
                item.code,
                item.effective_from,
                item.known_at or item.effective_from,
            ),
        )
    ]
    actions = [
        {
            "code": action.code,
            "effective_date": action.effective_date.isoformat(),
            "known_at": action.known_at.isoformat(),
            "action_type": action.action_type,
            "cash_per_share": str(action.cash_per_share),
            "split_ratio": str(action.split_ratio),
        }
        for action in sorted(
            dataset.corporate_actions,
            key=lambda item: (item.effective_date, item.code, item.known_at),
        )
    ]
    bars = []
    for key in sorted(dataset.bars):
        bar = dataset.bars[key]
        bars.append(
            {
                "code": bar.code,
                "name": bar.name,
                "trading_date": bar.trading_date.isoformat(),
                "raw_open": str(bar.raw_open),
                "raw_high": str(bar.raw_high),
                "raw_low": str(bar.raw_low),
                "raw_close": str(bar.raw_close),
                "volume": str(bar.volume),
                "amount": str(bar.amount),
            }
        )
    return content_hash(
        {
            "bars": bars,
            "rules": rules,
            "actions": actions,
            "master": [record.to_dict() for record in sorted(master, key=lambda x: x.code)],
        }
    )

