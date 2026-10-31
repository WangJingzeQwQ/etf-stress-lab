from __future__ import annotations

from importlib.util import find_spec


class AkshareUnavailableError(RuntimeError):
    pass


def akshare_available() -> bool:
    return find_spec("akshare") is not None


def load_raw_etf_history(code: str, start_date: str, end_date: str):
    """Optional adapter; the core engine and tests never call the network."""

    if not akshare_available():
        raise AkshareUnavailableError(
            "AKShare is not installed. Use SYNTHETIC_FIXTURE for M0 or install "
            "a separately governed data dependency."
        )
    import akshare as ak  # type: ignore[import-not-found]

    frame = ak.fund_etf_hist_em(
        symbol=code,
        period="daily",
        start_date=start_date,
        end_date=end_date,
        adjust="",
    )
    return frame


def feasibility_probe() -> dict[str, object]:
    return {
        "provider": "AKShare",
        "installed": akshare_available(),
        "core_network_dependency": False,
        "status": "available_adapter_only" if akshare_available() else "unavailable",
        "required_fields": [
            "raw OHLCV",
            "Chinese name",
            "historical master and delist coverage",
            "corporate actions with effective_date/known_at",
            "per-code settlement and limit rules",
            "dated cost schedules",
        ],
    }

