from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from importlib.util import find_spec
import time
from typing import Any, Callable, Protocol

from backend.data_pipeline.contracts import (
    DataQualityError,
    RawDailyRecord,
)
from backend.engine.models import decimal_value


class ETFDataProvider(Protocol):
    provider_name: str

    def fetch_daily(
        self, code: str, name: str, start_date: date, end_date: date
    ) -> list[RawDailyRecord]: ...


class ProviderUnavailableError(RuntimeError):
    pass


class HistoricalMasterUnavailableError(RuntimeError):
    pass


class RequestPacer:
    def __init__(
        self,
        interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if interval_seconds < 3.0:
            raise ValueError("Provider request interval must be at least 3 seconds")
        self.interval_seconds = interval_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._last_request_at: float | None = None
        self.sleep_calls: list[float] = []

    def before_request(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            delay = max(
                0.0,
                self.interval_seconds - (now - self._last_request_at),
            )
            if delay > 0:
                self.sleep_calls.append(delay)
                self._sleeper(delay)
        self._last_request_at = self._clock()


COLUMN_ALIASES = {
    "日期": "trading_date",
    "date": "trading_date",
    "开盘": "raw_open",
    "open": "raw_open",
    "最高": "raw_high",
    "high": "raw_high",
    "最低": "raw_low",
    "low": "raw_low",
    "收盘": "raw_close",
    "close": "raw_close",
    "成交量": "volume",
    "volume": "volume",
    "成交额": "amount",
    "amount": "amount",
}


def normalize_provider_frame(
    frame: Any,
    *,
    code: str,
    name: str,
    provider: str,
) -> list[RawDailyRecord]:
    if frame is None or not hasattr(frame, "to_dict"):
        raise DataQualityError("Provider response must be a DataFrame-like object")
    if name is None or not str(name).strip():
        raise DataQualityError(f"Provider response for {code} lacks Chinese name")
    frame_columns = list(getattr(frame, "columns", []))
    mapped = {
        column: COLUMN_ALIASES[str(column)]
        for column in frame_columns
        if str(column) in COLUMN_ALIASES
    }
    required = {"trading_date", "raw_open", "raw_high", "raw_low", "raw_close", "volume", "amount"}
    if not required.issubset(set(mapped.values())):
        missing = sorted(required - set(mapped.values()))
        raise DataQualityError(f"Provider frame missing fields: {missing}")
    records: list[RawDailyRecord] = []
    for raw_row in frame.to_dict("records"):
        row = {
            mapped[column]: value
            for column, value in raw_row.items()
            if column in mapped
        }
        date_value = row["trading_date"]
        if hasattr(date_value, "date"):
            trading_date = date_value.date()
        elif isinstance(date_value, date):
            trading_date = date_value
        else:
            trading_date = date.fromisoformat(str(date_value)[:10])
        volume = decimal_value(row["volume"])
        if provider == "akshare_raw":
            # Eastmoney ETF volume is in lots (手); retain canonical shares.
            volume *= Decimal("100")
        record = RawDailyRecord(
            code=code,
            name=str(name),
            trading_date=trading_date,
            raw_open=decimal_value(row["raw_open"]),
            raw_high=decimal_value(row["raw_high"]),
            raw_low=decimal_value(row["raw_low"]),
            raw_close=decimal_value(row["raw_close"]),
            volume=volume,
            amount=decimal_value(row["amount"]),
            provider=provider,
            adjustment="none",
            volume_unit="shares",
            amount_currency="CNY",
        )
        record.validate()
        records.append(record)
    if len({record.trading_date for record in records}) != len(records):
        raise DataQualityError(f"Duplicate trading dates returned for {code}")
    if not records:
        raise DataQualityError(f"Provider returned no rows for {code}")
    return sorted(records, key=lambda record: record.trading_date)


class AkshareETFProvider:
    provider_name = "akshare_raw"

    def __init__(
        self,
        *,
        request_interval_seconds: float = 3.0,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        limiter: RequestPacer | None = None,
        fetch_func: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries must be positive")
        self.limiter = limiter or RequestPacer(request_interval_seconds)
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._fetch_func = fetch_func
        self._sleeper = sleeper

    def _load_fetch_func(self) -> Callable[..., Any]:
        if self._fetch_func is not None:
            return self._fetch_func
        if find_spec("akshare") is None:
            raise ProviderUnavailableError(
                "AKShare is not installed; use PHASE1_FIXTURE or install the "
                "explicit provider dependency."
            )
        import akshare as ak  # type: ignore[import-not-found]

        return ak.fund_etf_hist_em

    def fetch_daily(
        self, code: str, name: str, start_date: date, end_date: date
    ) -> list[RawDailyRecord]:
        fetch_func = self._load_fetch_func()
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self.limiter.before_request()
            try:
                frame = fetch_func(
                    symbol=code,
                    period="daily",
                    start_date=start_date.strftime("%Y%m%d"),
                    end_date=end_date.strftime("%Y%m%d"),
                    adjust="",
                )
                return normalize_provider_frame(
                    frame,
                    code=code,
                    name=name,
                    provider=self.provider_name,
                )
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.max_retries:
                    self._sleeper(self.backoff_seconds * (2**attempt))
        raise RuntimeError(
            f"Provider failed after {self.max_retries} attempts for {code}: {last_error}"
        ) from last_error

    def fetch_historical_master(self) -> None:
        raise HistoricalMasterUnavailableError(
            "AKShare spot ETF endpoints are not a documented historical master "
            "with delisting and rename intervals. Import a separately validated "
            "historical master; current spot data must not be presented as it."
        )


class AkshareSinaETFProvider:
    provider_name = "akshare_sina_raw"

    def __init__(
        self,
        *,
        request_interval_seconds: float = 3.0,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        limiter: RequestPacer | None = None,
        fetch_func: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries must be positive")
        self.limiter = limiter or RequestPacer(request_interval_seconds)
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._fetch_func = fetch_func
        self._sleeper = sleeper

    def _load_fetch_func(self) -> Callable[..., Any]:
        if self._fetch_func is not None:
            return self._fetch_func
        if find_spec("akshare") is None:
            raise ProviderUnavailableError(
                "AKShare is not installed; use PHASE1_FIXTURE or install the "
                "explicit provider dependency."
            )
        import akshare as ak  # type: ignore[import-not-found]

        return ak.fund_etf_hist_sina

    @staticmethod
    def _sina_symbol(code: str) -> str:
        if code.startswith("5"):
            return f"sh{code}"
        if code.startswith("1"):
            return f"sz{code}"
        raise ValueError(f"Cannot infer Sina exchange prefix for ETF code {code}")

    def fetch_daily(
        self, code: str, name: str, start_date: date, end_date: date
    ) -> list[RawDailyRecord]:
        fetch_func = self._load_fetch_func()
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self.limiter.before_request()
            try:
                frame = fetch_func(symbol=self._sina_symbol(code))
                records = normalize_provider_frame(
                    frame,
                    code=code,
                    name=name,
                    provider=self.provider_name,
                )
                selected = [
                    record
                    for record in records
                    if start_date <= record.trading_date <= end_date
                ]
                if not selected:
                    raise DataQualityError(
                        f"Sina returned no records in requested range for {code}"
                    )
                return selected
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.max_retries:
                    self._sleeper(self.backoff_seconds * (2**attempt))
        raise RuntimeError(
            f"Sina provider failed after {self.max_retries} attempts for {code}: "
            f"{last_error}"
        ) from last_error


class BaoStockETFProvider:
    provider_name = "baostock_raw"

    def __init__(
        self,
        *,
        request_interval_seconds: float = 3.0,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        limiter: RequestPacer | None = None,
        bs_module: Any | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.limiter = limiter or RequestPacer(request_interval_seconds)
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._module = bs_module
        self._sleeper = sleeper

    def _load_module(self) -> Any:
        if self._module is not None:
            return self._module
        if find_spec("baostock") is None:
            raise ProviderUnavailableError("BaoStock is not installed")
        import baostock as bs  # type: ignore[import-not-found]

        return bs

    @staticmethod
    def _symbol(code: str) -> str:
        if code.startswith("5"):
            return f"sh.{code}"
        if code.startswith("1"):
            return f"sz.{code}"
        raise ValueError(f"Cannot infer BaoStock exchange prefix for {code}")

    def fetch_daily(
        self, code: str, name: str, start_date: date, end_date: date
    ) -> list[RawDailyRecord]:
        module = self._load_module()
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self.limiter.before_request()
            login = module.login()
            if getattr(login, "error_code", "1") != "0":
                last_error = RuntimeError(getattr(login, "error_msg", "login failed"))
                self._sleeper(self.backoff_seconds * (2**attempt))
                continue
            try:
                result = module.query_history_k_data_plus(
                    self._symbol(code),
                    "date,open,high,low,close,volume,amount,adjustflag",
                    start_date=start_date.isoformat(),
                    end_date=end_date.isoformat(),
                    frequency="d",
                    adjustflag="3",
                )
                if getattr(result, "error_code", "1") != "0":
                    raise RuntimeError(result.error_msg)
                rows = []
                while result.next():
                    rows.append(result.get_row_data())
                records = []
                for row in rows:
                    data = dict(zip(result.fields, row))
                    trading_date = date.fromisoformat(str(data["date"]))
                    if not start_date <= trading_date <= end_date:
                        continue
                    if str(data["adjustflag"]) != "3":
                        raise DataQualityError("BaoStock returned adjusted prices")
                    record = RawDailyRecord(
                        code=code,
                        name=name,
                        trading_date=trading_date,
                        raw_open=decimal_value(data["open"]),
                        raw_high=decimal_value(data["high"]),
                        raw_low=decimal_value(data["low"]),
                        raw_close=decimal_value(data["close"]),
                        volume=decimal_value(data["volume"]),
                        amount=decimal_value(data["amount"]),
                        provider=self.provider_name,
                        adjustment="none",
                        volume_unit="shares",
                        amount_currency="CNY",
                    )
                    record.validate()
                    records.append(record)
                if not records:
                    raise DataQualityError(f"BaoStock returned no rows for {code}")
                return sorted(records, key=lambda item: item.trading_date)
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.max_retries:
                    self._sleeper(self.backoff_seconds * (2**attempt))
            finally:
                module.logout()
        raise RuntimeError(
            f"BaoStock failed after {self.max_retries} attempts for {code}: "
            f"{last_error}"
        ) from last_error

