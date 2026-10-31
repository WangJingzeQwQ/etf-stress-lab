from __future__ import annotations

from datetime import date
from decimal import Decimal

from backend.data_pipeline.bundle_store import save_bundle
from backend.data_pipeline.contracts import (
    CoverageFlags,
    DataManifest,
    MasterRecord,
    NameHistoryPoint,
    Phase1Bundle,
    RawDailyRecord,
    StatusHistoryPoint,
    bundle_fingerprint,
)
from backend.data_pipeline.dataset_factory import build_dataset_from_raw
from backend.data_pipeline.formal_gate import evaluate_formal_gate
from backend.data_pipeline.models import CorporateAction, TradingRule
from backend.data_pipeline.storage import (
    canonical_file_hash,
    write_json_atomic,
    write_ndjson_atomic,
)
from backend.data_pipeline.trading_rule_audit import audit_trading_rules


def _master(*, complete: bool = True) -> MasterRecord:
    return MasterRecord(
        code="510300",
        list_date=date(2012, 5, 28),
        delist_date=None,
        name_history=(
            NameHistoryPoint(
                "沪深300ETF",
                date(2012, 5, 28),
                date(2012, 5, 28),
            ),
        ),
        status_history=(
            StatusHistoryPoint(
                "ACTIVE",
                date(2012, 5, 28),
                date(2012, 5, 28),
            ),
        ),
        history_complete=complete,
        source="AUDITED",
    )


def _rule(**overrides):
    payload = {
        "code": "510300",
        "settlement": "T1",
        "effective_from": "2012-05-28",
        "effective_to": "2099-12-31",
        "limit_pct": "0.10",
        "trading_status": "ACTIVE",
        "known_at": "2012-05-28",
        "source": "上交所基金上市交易公告书",
        "evidence_id": "SSE-OFFICIAL-001",
        "evidence_sha256": "sha256:" + "a" * 64,
    }
    payload.update(overrides)
    return payload


def _write_ready_bundle(root):
    master = {"510300": _master()}
    rule = TradingRule(
        code="510300",
        settlement="T1",
        effective_from=date(2012, 5, 28),
        effective_to=date(2099, 12, 31),
        limit_pct=Decimal("0.10"),
        known_at=date(2012, 5, 28),
        trading_status="ACTIVE",
        source="上交所基金上市交易公告书",
        evidence_id="SSE-OFFICIAL-001",
        evidence_sha256="sha256:" + "a" * 64,
    )
    action = CorporateAction(
        code="510300",
        effective_date=date(2024, 2, 1),
        known_at=date(2024, 1, 10),
        action_type="cash_dividend",
        cash_per_share=Decimal("0.01"),
    )
    raw = RawDailyRecord(
        code="510300",
        name="沪深300ETF",
        trading_date=date(2024, 1, 2),
        raw_open=Decimal("3.90"),
        raw_high=Decimal("3.95"),
        raw_low=Decimal("3.88"),
        raw_close=Decimal("3.93"),
        volume=Decimal("1000000"),
        amount=Decimal("3900000"),
        provider="AUDITED_PROVIDER",
    )
    dataset = build_dataset_from_raw(
        [raw],
        master,
        [rule],
        [action],
        source_kind="REAL_PROVIDER",
    )
    manifest = DataManifest(
        source_kind="REAL_PROVIDER",
        source_name="AUDITED_PROVIDER",
        data_version=bundle_fingerprint(dataset, master.values()),
        coverage=CoverageFlags(
            raw_ohlcv=True,
            historical_master=True,
            delist_history=True,
            corporate_actions=True,
            trading_rules=True,
            cost_schedules=True,
        ),
        notes=("test evidence",),
        real_market_evidence_allowed=True,
    )
    save_bundle(
        root / "data",
        Phase1Bundle(dataset=dataset, master=master, manifest_data=manifest),
    )
    write_json_atomic(
        root / "reports" / "m1_2" / "raw_coverage_audit.json",
        {
            "status": "COMPLETE",
            "missing_files": 0,
            "codes_with_errors": 0,
            "files_present": 1,
        },
    )
    write_ndjson_atomic(
        root / "data" / "meta" / "costs.ndjson",
        [
            {
                "scenario": scenario,
                "effective_from": "2024-01-01",
                "effective_to": "2024-12-31",
                "source_kind": "DATED_RESEARCH_ASSUMPTION",
            }
            for scenario in ("low", "base", "high")
        ],
    )
    write_json_atomic(
        root / "reports" / "m1_2" / "trading_rules_raw_validation.json",
        {
            "status": "VALIDATED",
            "rule_audit_status": "READY",
            "error_count": 0,
            "totals": {"price_limit_violations": 0},
            "rules_sha256": canonical_file_hash(
                root / "data" / "meta" / "trading_rules.ndjson"
            ),
        },
    )
    return {
        "data": {"start_date": "2024-01-01", "end_date": "2024-12-31"},
        "data_pipeline": {
            "master_file": "data/meta/etf_master.ndjson",
            "corporate_actions_file": "data/meta/corporate_actions.ndjson",
            "cost_schedules_file": "data/meta/costs.ndjson",
            "trading_rules_file": "data/meta/trading_rules.ndjson",
            "bundle_root": "data",
        },
    }


def test_trading_rule_audit_requires_full_evidence_and_continuous_coverage():
    report = audit_trading_rules(
        {"510300": _master()},
        [
            _rule(
                effective_from="2012-05-28",
                effective_to="2023-12-31",
                source="unknown",
            ),
            _rule(
                effective_from="2024-01-02",
                effective_to="2099-12-31",
            ),
        ],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
    )
    assert report.ready is False
    assert report.complete_codes == 0
    assert "missing_required_field" in report.issue_counts
    assert "coverage_gap" in report.issue_counts
    assert report.gap_rows[0]["required_effective_from"] == "2024-01-01"


def test_trading_rule_audit_rejects_overlap_and_future_known_at():
    report = audit_trading_rules(
        {"510300": _master()},
        [
            _rule(effective_to="2024-06-30"),
            _rule(
                effective_from="2024-06-30",
                effective_to="2099-12-31",
                known_at="2024-07-01",
            ),
        ],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
    )
    assert report.ready is False
    assert "conflicting_intervals" in report.issue_counts
    assert "future_known_at" in report.issue_counts


def test_formal_gate_is_ready_only_when_rules_and_bundle_hash_are_valid(tmp_path):
    config = _write_ready_bundle(tmp_path)
    report = evaluate_formal_gate(tmp_path, config)
    assert report["formal_research_ready"] is True
    assert report["blocking_reasons"] == []
    assert report["trading_rule_audit"]["complete_codes"] == 1

    rules_path = tmp_path / "data" / "meta" / "trading_rules.ndjson"
    write_ndjson_atomic(
        rules_path,
        [
            {
                "code": "510300",
                "settlement": "T1",
            }
        ],
    )
    blocked = evaluate_formal_gate(tmp_path, config)
    assert blocked["formal_research_ready"] is False
    assert "trading_rules_complete" in blocked["blocking_reasons"]
    assert "real_bundle_manifest" in blocked["blocking_reasons"]
