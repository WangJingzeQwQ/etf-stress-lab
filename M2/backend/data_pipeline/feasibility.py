from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Any

from backend.data_pipeline.akshare_adapter import feasibility_probe


def run_feasibility_spike(project_root: Path) -> dict[str, Any]:
    data_root = project_root / "data"
    observed_files = sorted(
        str(path.relative_to(project_root))
        for path in data_root.rglob("*")
        if path.is_file()
    )
    observed_suffixes = sorted({Path(item).suffix for item in observed_files})
    market_data_suffixes = {
        ".csv",
        ".parquet",
        ".ndjson",
        ".json",
        ".xlsx",
        ".db",
        ".sqlite",
    }
    actual_market_data_available = any(
        Path(item).suffix.lower() in market_data_suffixes
        for item in observed_files
    )
    return {
        "probe_date": date.today().isoformat(),
        "representative_codes_requested": ["510300", "510500", "159915"],
        "data_root": "data",
        "observed_files": observed_files,
        "observed_suffixes": observed_suffixes,
        "provider_probe": feasibility_probe(),
        "actual_market_data_available": actual_market_data_available,
        "missing": [
            "validated raw OHLCV history",
            "historical ETF membership including delisted/paused funds",
            "code-level settlement and limit-rule effective intervals",
            "corporate actions with effective_date and known_at",
            "dated broker/exchange fee documents",
        ],
        "m0_decision": (
            "Continue engine validation with SYNTHETIC_FIXTURE and "
            "parameterized_etf_v1. Do not publish real-market conclusions."
        ),
    }


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    print(
        json.dumps(
            run_feasibility_spike(project_root),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

