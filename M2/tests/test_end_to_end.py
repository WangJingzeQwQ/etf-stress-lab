from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from backend.data_pipeline.contracts import DataQualityError
from backend.data_pipeline.bundle_store import save_bundle
from backend.data_pipeline.phase1_fixture import build_phase1_fixture
from backend.engine.run_backtest import (
    load_config,
    main,
    run_backtest,
    write_result,
)


ROOT = Path(__file__).resolve().parents[1]


def test_full_fixture_replay_is_deterministic_and_complete(tmp_path):
    config = load_config(ROOT / "config.yaml")
    first = run_backtest(config)
    second = run_backtest(deepcopy(config))
    assert first.result_hash == second.result_hash
    assert first.dataset.source_kind == "SYNTHETIC_FIXTURE"
    assert len(first.snapshots) == len(first.dataset.sessions)
    assert first.targets
    assert first.trades
    assert first.audit_events
    assert first.metrics["total_commission"] > 0
    assert first.metrics["minimum_fee_order_count"] > 0
    assert first.metrics["slippage_cost"] > 0
    assert first.metrics["fee_drag"] is not None
    assert first.metadata["engine_version"] == "etf-stress-m1/0.2.0"
    assert first.metadata["phase1_data"]["source_kind"] == "SYNTHETIC_FIXTURE"
    assert all("phase1_universe_version" in target.meta for target in first.targets)

    out1 = tmp_path / "run1"
    out2 = tmp_path / "run2"
    write_result(first, out1)
    write_result(second, out2)
    for name in (
        "trades.ndjson",
        "daily_snapshot.ndjson",
        "target_portfolio.ndjson",
        "target_execution.ndjson",
        "audit_events.ndjson",
    ):
        assert (out1 / name).read_bytes() == (out2 / name).read_bytes()


def test_cli_outputs_required_fields_and_no_qfq_execution(tmp_path):
    exit_code = main(
        ["--config", str(ROOT / "config.yaml"), "--output", str(tmp_path)]
    )
    assert exit_code == 0
    trade_lines = [
        json.loads(line)
        for line in (tmp_path / "trades.ndjson").read_text(encoding="utf-8").splitlines()
    ]
    required = {
        "signal_date",
        "execution_date",
        "code",
        "name",
        "reference_price",
        "execution_price",
        "shares",
        "gross_amount",
        "commission",
        "other_fees",
        "slippage_cost",
        "net_cash_flow",
        "unfilled_shares",
        "account_status",
    }
    assert trade_lines
    assert required.issubset(trade_lines[0])
    assert all(line["name"] for line in trade_lines)
    assert all(int(line["shares"]) % 100 == 0 for line in trade_lines if not line["is_liquidation"])
    snapshots = [
        json.loads(line)
        for line in (tmp_path / "daily_snapshot.ndjson")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(snapshots) == 195
    assert all("name" in position for row in snapshots for position in row["positions"])
    assert all("unfilled_orders" in row for row in snapshots)
    execution_lines = [
        json.loads(line)
        for line in (tmp_path / "target_execution.ndjson")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert execution_lines
    assert all("pv_target" in line for row in execution_lines for line in row["lines"])
    manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
    metadata = json.loads(
        (tmp_path / "config_metadata.json").read_text(encoding="utf-8")
    )
    assert manifest["result_hash"].startswith("sha256:")
    assert metadata["financing_mode"] == "synthetic_stress"
    assert metadata["liquidation_model"] == "synthetic_v1"
    assert metadata["cost_model"] == "parameterized_etf_v1"
    assert metadata["p0_1_frozen"] is True
    assert metadata["data_source_kind"] == "SYNTHETIC_FIXTURE"


def test_formal_research_mode_blocks_synthetic_phase1_data():
    config = load_config(ROOT / "config.yaml")
    config["data_pipeline"]["formal_research"] = True
    with pytest.raises(DataQualityError, match="Formal research"):
        run_backtest(config)


def test_stored_phase1_bundle_replays_same_result_hash(tmp_path):
    config = load_config(ROOT / "config.yaml")
    in_memory = run_backtest(config)
    save_bundle(tmp_path / "bundle", build_phase1_fixture())
    stored_config = deepcopy(config)
    stored_config["data_pipeline"]["source"] = "stored_bundle"
    stored_config["data_pipeline"]["bundle_root"] = str(tmp_path / "bundle")
    stored = run_backtest(stored_config)
    assert stored.metadata["data_version"] == in_memory.metadata["data_version"]
    assert stored.metrics == in_memory.metrics
    assert stored.trades == in_memory.trades
    assert stored.snapshots == in_memory.snapshots
    assert stored.targets == in_memory.targets

