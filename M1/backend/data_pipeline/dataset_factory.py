from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping

from backend.data_pipeline.contracts import (
    DataQualityError,
    MasterRecord,
    RawDailyRecord,
)
from backend.data_pipeline.models import (
    CorporateAction,
    MarketBar,
    MarketDataset,
    TradingRule,
)
from backend.engine.models import ZERO


PRICE_TICK = Decimal("0.001")


def _price(value: Decimal) -> Decimal:
    return value.quantize(PRICE_TICK, rounding=ROUND_HALF_UP)


def _reference_close(
    previous_close: Decimal,
    previous_date: date,
    current_date: date,
    actions: Iterable[CorporateAction],
    split_application_dates: dict[tuple[date, str], date],
) -> Decimal:
    reference = previous_close
    for action in actions:
        if action.action_type == "split":
            application_date = split_application_dates.get(
                (action.effective_date, action.action_type)
            )
            if application_date != current_date:
                continue
            reference /= action.split_ratio
        elif action.action_type == "cash_dividend":
            if not previous_date < action.effective_date <= current_date:
                continue
            reference -= action.cash_per_share
    return reference


def build_dataset_from_raw(
    records: Iterable[RawDailyRecord],
    master: dict[str, MasterRecord],
    trading_rules: Iterable[TradingRule],
    corporate_actions: Iterable[CorporateAction],
    *,
    source_kind: str,
    metadata: dict[str, object] | None = None,
    allow_incomplete_master: bool = False,
    reference_price_overrides: Mapping[tuple[str, date], Decimal] | None = None,
) -> MarketDataset:
    rows = list(records)
    if not rows:
        raise DataQualityError("No raw daily records supplied")
    seen: set[tuple[date, str]] = set()
    grouped: dict[str, list[RawDailyRecord]] = {}
    for record in rows:
        record.validate()
        key = (record.trading_date, record.code)
        if key in seen:
            raise DataQualityError(
                f"Duplicate raw bar for {record.code} on {record.trading_date}"
            )
        seen.add(key)
        grouped.setdefault(record.code, []).append(record)

    rules = tuple(trading_rules)
    actions_by_code: dict[str, list[CorporateAction]] = {}
    for action in corporate_actions:
        action.validate()
        actions_by_code.setdefault(action.code, []).append(action)
    for values in actions_by_code.values():
        values.sort(key=lambda item: item.effective_date)
    rules_by_code: dict[str, list[TradingRule]] = {}
    for rule in rules:
        rule.validate()
        rules_by_code.setdefault(rule.code, []).append(rule)

    bars: list[MarketBar] = []
    for code in sorted(grouped):
        record = master.get(code)
        if record is None:
            raise DataQualityError(f"Missing master record for {code}", code=code)
        if not record.history_complete and not allow_incomplete_master:
            raise DataQualityError(f"Incomplete master history for {code}", code=code)
        series = sorted(grouped[code], key=lambda item: item.trading_date)
        series_dates = [item.trading_date for item in series]
        split_application_dates: dict[tuple[date, str], date] = {}
        for action in actions_by_code.get(code, []):
            if action.action_type != "split":
                continue
            application_date = next(
                (
                    session
                    for session in series_dates
                    if session > action.effective_date
                ),
                None,
            )
            if application_date is not None:
                split_application_dates[
                    (action.effective_date, action.action_type)
                ] = application_date
        amount_history: list[Decimal] = []
        previous_close: Decimal | None = None
        previous_date: date | None = None
        for index, item in enumerate(series):
            matches = [
                rule
                for rule in rules_by_code.get(code, [])
                if rule.applies(item.trading_date)
            ]
            if len(matches) != 1:
                reason = "conflicting" if len(matches) > 1 else "missing"
                raise DataQualityError(
                    f"{reason} trading rule for {code} on "
                    f"{item.trading_date.isoformat()}",
                    code=code,
                )
            rule = matches[0]
            override = (reference_price_overrides or {}).get(
                (code, item.trading_date)
            )
            if override is not None:
                prior = Decimal(str(override))
            elif previous_close is not None and previous_date is not None:
                prior = _reference_close(
                    previous_close,
                    previous_date,
                    item.trading_date,
                    actions_by_code.get(code, []),
                    split_application_dates,
                )
            else:
                prior = item.raw_close
            adv20 = (
                sum(amount_history[-20:], ZERO) / Decimal(len(amount_history[-20:]))
                if amount_history
                else None
            )
            is_suspended = (
                rule.trading_status == "SUSPENDED"
                or item.volume == ZERO
                or item.amount == ZERO
            )
            bars.append(
                MarketBar(
                    code=code,
                    name=(
                        record.name_at(item.trading_date)
                        if record.history_complete
                        or any(
                            point.effective_from <= item.trading_date
                            and point.known_at <= item.trading_date
                            for point in record.name_history
                        )
                        else min(
                            record.name_history,
                            key=lambda point: (point.effective_from, point.known_at),
                        ).name
                    ),
                    trading_date=item.trading_date,
                    raw_open=item.raw_open,
                    raw_high=item.raw_high,
                    raw_low=item.raw_low,
                    raw_close=item.raw_close,
                    volume=item.volume,
                    amount=item.amount,
                    prev_close=prior,
                    limit_up=_price(prior * (Decimal("1") + rule.limit_pct)),
                    limit_down=_price(prior * (Decimal("1") - rule.limit_pct)),
                    adv20_amount=adv20,
                    is_suspended=is_suspended,
                    open_tradable=not is_suspended,
                    total_return_close=item.raw_close,
                    factor_effective_date=item.trading_date,
                    factor_known_at=item.trading_date,
                    provider_qfq_close=None,
                )
            )
            amount_history.append(item.amount)
            previous_close = item.raw_close
            previous_date = item.trading_date

    names = {}
    for code, series in grouped.items():
        last_date = max(item.trading_date for item in series)
        try:
            names[code] = master[code].name_at(last_date)
        except DataQualityError:
            if not allow_incomplete_master:
                raise
            names[code] = min(
                master[code].name_history,
                key=lambda point: (point.effective_from, point.known_at),
            ).name
    return MarketDataset.build(
        bars,
        names=names,
        list_dates={code: master[code].list_date for code in grouped},
        trading_rules=rules,
        corporate_actions=tuple(
            action
            for values in actions_by_code.values()
            for action in values
        ),
        source_kind=source_kind,
        metadata={
            "price_adjustment": "none",
            "provider_qfq_used_for_execution": False,
            **(metadata or {}),
        },
    )

