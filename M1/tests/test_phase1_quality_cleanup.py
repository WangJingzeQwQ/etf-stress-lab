from __future__ import annotations

from datetime import date
from decimal import Decimal
import json

from backend.data_pipeline.contracts import (
    MasterRecord,
    NameHistoryPoint,
    RawDailyRecord,
    StatusHistoryPoint,
)
from backend.data_pipeline.fetch_master import save_master_records
from backend.data_pipeline.quality_cleanup import (
    find_spurious_duplicate_tail,
    quarantine_duplicate_tail,
    reconcile_master_delist_from_raw,
)
from backend.data_pipeline.storage import save_raw_daily


def _row(day: date) -> RawDailyRecord:
    return RawDailyRecord(
        code="159927",
        name="A300ETF",
        trading_date=day,
        raw_open=Decimal("3.789"),
        raw_high=Decimal("3.867"),
        raw_low=Decimal("3.75"),
        raw_close=Decimal("3.85"),
        volume=Decimal("37975"),
        amount=Decimal("144846"),
        provider="TEST",
    )


def test_duplicate_tail_quarantine_and_master_reconciliation(tmp_path):
    raw_root = tmp_path / "raw"
    original = _row(date(2018, 2, 13))
    anomalous = RawDailyRecord(
        **{
            **original.__dict__,
            "trading_date": date(2021, 6, 27),
        }
    )
    save_raw_daily(raw_root / "159927.ndjson", [original, anomalous])
    finding = find_spurious_duplicate_tail([original, anomalous])
    assert finding is not None
    quarantine = quarantine_duplicate_tail(
        code="159927",
        raw_root=raw_root,
        quarantine_root=tmp_path / "quarantine",
        report_path=tmp_path / "quarantine.json",
    )
    assert quarantine["removed_date"] == "2021-06-27"
    assert (tmp_path / "quarantine" / "159927.original.ndjson").exists()

    master_path = tmp_path / "master.ndjson"
    save_master_records(
        master_path,
        {
            "159927": MasterRecord(
                code="159927",
                list_date=date(2018, 1, 2),
                delist_date=date(2018, 2, 12),
                name_history=(
                    NameHistoryPoint(
                        "A300ETF",
                        date(2018, 1, 2),
                        date(2018, 1, 2),
                    ),
                ),
                status_history=(
                    StatusHistoryPoint(
                        "ACTIVE",
                        date(2018, 1, 2),
                        date(2018, 1, 2),
                    ),
                    StatusHistoryPoint(
                        "DELISTED",
                        date(2018, 2, 13),
                        date(2018, 2, 12),
                    ),
                ),
                history_complete=False,
                source="TEST",
            )
        },
    )
    reconciled_path = tmp_path / "reconciled.ndjson"
    report = reconcile_master_delist_from_raw(
        master_path=master_path,
        raw_root=raw_root,
        code="159927",
        output_path=reconciled_path,
        report_path=tmp_path / "master_report.json",
    )
    assert report["new_delist_date"] == "2018-02-13"
    row = json.loads(reconciled_path.read_text(encoding="utf-8").splitlines()[0])
    assert row["delist_date"] == "2018-02-13"
    assert row["status_history"][-1]["effective_from"] == "2018-02-14"

