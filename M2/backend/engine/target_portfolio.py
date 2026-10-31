from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Iterable
import math


DEFAULT_R_LEVELS = (0.0, 0.25, 0.5, 0.75, 1.0)
BANNED_META_KEYS = {"shares", "cash", "debt", "pv_target", "e0", "leverage"}


class PortfolioValidationError(ValueError):
    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


@dataclass
class TargetPortfolio:
    total_r: float
    weights: dict[str, float]
    signal_date: date
    execution_date: date
    data_cutoff: date
    group: str
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["signal_date"] = self.signal_date.isoformat()
        payload["execution_date"] = self.execution_date.isoformat()
        payload["data_cutoff"] = self.data_cutoff.isoformat()
        return payload


def _contains_banned_meta(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).strip().lower() in BANNED_META_KEYS:
                return True
            if _contains_banned_meta(child):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_banned_meta(item) for item in value)
    return False


def validate_target_portfolio(
    target: TargetPortfolio,
    allowed_codes: set[str],
    expected_execution_date: date | None = None,
) -> None:
    errors: list[str] = []
    group_text = str(target.group).upper()
    allowed_r = {0.0, 1.0} if group_text == "G1" else set(DEFAULT_R_LEVELS)
    if isinstance(target.total_r, bool) or not isinstance(target.total_r, (int, float)):
        errors.append("total_r must be numeric")
    elif not math.isfinite(float(target.total_r)) or not any(
        math.isclose(float(target.total_r), value, rel_tol=0.0, abs_tol=1e-12)
        for value in allowed_r
    ):
        errors.append(f"total_r={target.total_r!r} is not an allowed discrete level")

    date_values = (target.signal_date, target.execution_date, target.data_cutoff)
    if not all(isinstance(value, date) for value in date_values):
        errors.append("signal_date, execution_date and data_cutoff must be date values")
    else:
        if target.signal_date >= target.execution_date:
            errors.append("signal_date must be earlier than execution_date")
        if target.data_cutoff >= target.execution_date:
            errors.append("data_cutoff must be earlier than execution_date")
        if target.data_cutoff > target.signal_date:
            errors.append("data_cutoff cannot be later than signal_date")
        if (
            expected_execution_date is not None
            and target.execution_date != expected_execution_date
        ):
            errors.append(
                f"execution_date {target.execution_date} does not match "
                f"{expected_execution_date}"
            )
    if group_text not in {"G1", "G2", "G3", "G4", "G5", "G6"}:
        errors.append("group must be a G1-G6 strategy identifier")

    if not isinstance(target.meta, dict):
        errors.append("meta must be a mapping")
    elif _contains_banned_meta(target.meta):
        errors.append("meta contains forbidden capital or position fields")

    weights = target.weights
    if not isinstance(weights, dict):
        errors.append("weights must be a mapping")
        weights = {}
    if target.total_r == 0.0:
        if weights:
            errors.append("weights must be empty when total_r=0")
    else:
        if not weights:
            errors.append("weights cannot be empty when total_r>0")
        else:
            numeric: dict[str, float] = {}
            for code, weight in weights.items():
                if not isinstance(code, str) or not code:
                    errors.append("weight keys must be non-empty code strings")
                    continue
                if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                    errors.append(f"weight for {code} is not numeric")
                    continue
                value = float(weight)
                if not math.isfinite(value) or value < 0:
                    errors.append(f"weight for {code} must be finite and non-negative")
                numeric[code] = value
            if numeric:
                total = math.fsum(numeric.values())
                if abs(total - 1.0) > 1e-8:
                    errors.append(f"weights must sum to 1 within 1e-8; got {total:.17g}")
            missing = sorted(set(weights) - set(allowed_codes))
            if missing:
                errors.append(f"codes not in the execution pool: {','.join(missing)}")

    if errors:
        raise PortfolioValidationError(errors)

