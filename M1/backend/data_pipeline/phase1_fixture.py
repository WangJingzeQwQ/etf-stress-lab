from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from backend.data_pipeline.contracts import (
    CoverageFlags,
    DataManifest,
    MasterRecord,
    NameHistoryPoint,
    Phase1Bundle,
    StatusHistoryPoint,
    bundle_fingerprint,
)
from backend.data_pipeline.fixture import build_synthetic_fixture
from backend.data_pipeline.models import (
    CorporateAction,
    MarketDataset,
    TradingRule,
)


PHASE1_FIXTURE_SOURCE = "PHASE1_SYNTHETIC_FIXTURE"


def _master_record(
    code: str,
    name: str,
    *,
    list_date: date = date(2020, 1, 2),
    delist_date: date | None = None,
    names: tuple[NameHistoryPoint, ...] | None = None,
    statuses: tuple[StatusHistoryPoint, ...] | None = None,
) -> MasterRecord:
    record = MasterRecord(
        code=code,
        list_date=list_date,
        delist_date=delist_date,
        name_history=names
        or (NameHistoryPoint(name=name, effective_from=list_date, known_at=list_date),),
        status_history=statuses
        or (
            StatusHistoryPoint(
                status="ACTIVE",
                effective_from=list_date,
                known_at=list_date,
            ),
        ),
        history_complete=True,
        source=PHASE1_FIXTURE_SOURCE,
    )
    record.validate()
    return record


def build_phase1_fixture(
    start: date = date(2023, 10, 2),
    end: date = date(2024, 6, 28),
    *,
    seed: int = 20261029,
) -> Phase1Bundle:
    base = build_synthetic_fixture(start, end, seed=seed)
    delisted_code = "159999"
    delist_date = date(2024, 2, 15)
    delisted_bars = [
        replace(
            bar,
            code=delisted_code,
            name="清盘测试ETF",
            provider_qfq_close=(bar.raw_close * Decimal("0.21")),
        )
        for (session, code), bar in sorted(base.bars.items())
        if code == "511010" and session <= delist_date
    ]
    synthetic_actions = (
        CorporateAction(
            code="510300",
            effective_date=date(2024, 2, 1),
            known_at=date(2024, 1, 15),
            action_type="cash_dividend",
            cash_per_share=Decimal("0.03"),
        ),
        CorporateAction(
            code="159915",
            effective_date=date(2024, 3, 1),
            known_at=date(2024, 2, 15),
            action_type="split",
            split_ratio=Decimal("2"),
        ),
    )
    rules = tuple(base.trading_rules) + (
        TradingRule(
            code=delisted_code,
            settlement="T1",
            effective_from=date(2020, 1, 2),
            effective_to=delist_date,
            limit_pct=Decimal("0.10"),
            known_at=date(2020, 1, 2),
        ),
    )
    names = dict(base.names)
    names[delisted_code] = "清盘测试ETF"
    list_dates = dict(base.list_dates)
    list_dates[delisted_code] = date(2020, 1, 2)
    dataset = MarketDataset.build(
        [*base.bars.values(), *delisted_bars],
        names=names,
        list_dates=list_dates,
        trading_rules=rules,
        corporate_actions=synthetic_actions,
        source_kind="SYNTHETIC_FIXTURE",
        metadata={
            **base.metadata,
            "phase": "Phase 1",
            "fixture_scope": "historical master, delist, rename, corporate action",
            "real_market_data": False,
        },
    )

    master: dict[str, MasterRecord] = {}
    for code in dataset.codes:
        if code == delisted_code:
            master[code] = _master_record(
                code,
                "清盘测试ETF",
                delist_date=delist_date,
                statuses=(
                    StatusHistoryPoint(
                        status="ACTIVE",
                        effective_from=date(2020, 1, 2),
                        known_at=date(2020, 1, 2),
                    ),
                    StatusHistoryPoint(
                        status="DELISTED",
                        effective_from=delist_date,
                        known_at=date(2024, 1, 31),
                    ),
                ),
            )
        elif code == "510500":
            master[code] = _master_record(
                code,
                "中证500ETF",
                names=(
                    NameHistoryPoint(
                        name="中证500ETF旧称",
                        effective_from=date(2020, 1, 2),
                        known_at=date(2020, 1, 2),
                    ),
                    NameHistoryPoint(
                        name="中证500ETF",
                        effective_from=date(2023, 12, 1),
                        known_at=date(2023, 11, 15),
                    ),
                ),
            )
        else:
            master[code] = _master_record(code, dataset.names[code])

    normalized_bars = [
        replace(bar, name=master[bar.code].name_at(bar.trading_date))
        for bar in dataset.bars.values()
    ]
    dataset = MarketDataset.build(
        normalized_bars,
        names=dataset.names,
        list_dates=dataset.list_dates,
        trading_rules=dataset.trading_rules,
        corporate_actions=dataset.corporate_actions,
        source_kind=dataset.source_kind,
        metadata=dataset.metadata,
    )

    provisional = Phase1Bundle(
        dataset=dataset,
        master=master,
        manifest_data=DataManifest(
            source_kind="SYNTHETIC_FIXTURE",
            source_name="M1 Phase 1 deterministic fixture",
            data_version="sha256:pending",
            coverage=CoverageFlags(
                raw_ohlcv=True,
                historical_master=True,
                delist_history=True,
                corporate_actions=True,
                trading_rules=True,
                cost_schedules=True,
            ),
            notes=(
                "Synthetic calendar, prices, liquidity, corporate actions and "
                "master history. No real-market conclusion is permitted.",
            ),
            real_market_evidence_allowed=False,
        ),
    )
    data_version = bundle_fingerprint(dataset, master.values())
    bundle = Phase1Bundle(
        dataset=dataset,
        master=master,
        manifest_data=replace(provisional.manifest_data, data_version=data_version),
        issues=list(provisional.issues),
    )
    bundle.validate()
    return bundle

