from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from backend.data_pipeline.contracts import (
    DataQualityError,
    RawDailyRecord,
    canonical_bytes,
)


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_ndjson_atomic(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            handle.write("\n")
    temporary.replace(path)


def read_ndjson(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError as exc:
                raise DataQualityError(
                    f"Invalid NDJSON at {path}:{line_number}: {exc}"
                ) from exc
            if not isinstance(row, dict):
                raise DataQualityError(
                    f"NDJSON record is not an object at {path}:{line_number}"
                )
            rows.append(row)
    return rows


def load_raw_daily(path: Path) -> list[RawDailyRecord]:
    return [RawDailyRecord.from_dict(row) for row in read_ndjson(path)]


def save_raw_daily(path: Path, records: Iterable[RawDailyRecord]) -> None:
    ordered = sorted(records, key=lambda record: (record.code, record.trading_date))
    write_ndjson_atomic(path, (record.to_dict() for record in ordered))


def canonical_file_hash(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"

