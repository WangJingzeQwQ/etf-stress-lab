from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import argparse
import csv
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from backend.data_pipeline.contracts import MasterRecord
from backend.data_pipeline.fetch_master import load_master_records
from backend.data_pipeline.storage import read_ndjson, write_json_atomic


REQUIRED_RULE_FIELDS = (
    "code",
    "settlement",
    "effective_from",
    "effective_to",
    "limit_pct",
    "trading_status",
    "known_at",
    "source",
    "evidence_id",
)
RULE_GAP_COLUMNS = (
    "code",
    "name",
    "list_date",
    "delist_date",
    "required_effective_from",
    "required_effective_to",
    "settlement",
    "effective_from",
    "effective_to",
    "limit_pct",
    "trading_status",
    "known_at",
    "source",
    "evidence_id",
    "evidence_sha256",
    "existing_intervals",
    "audit_issue_codes",
)
_PLACEHOLDER_VALUES = {
    "",
    "n/a",
    "na",
    "none",
    "null",
    "placeholder",
    "synthetic",
    "fixture",
    "tbd",
    "todo",
    "unknown",
}


@dataclass(frozen=True)
class _RuleRecord:
    code: str
    settlement: str
    effective_from: date
    effective_to: date
    limit_pct: Decimal
    trading_status: str
    known_at: date


@dataclass
class TradingRuleAuditResult:
    start_date: date
    end_date: date
    master_codes: int
    rule_rows: int
    valid_rule_rows: int
    codes_with_rules: int
    complete_codes: int
    missing_rule_codes: list[str]
    incomplete_codes: list[str]
    issue_counts: dict[str, int]
    issues: list[dict[str, Any]]
    gap_rows: list[dict[str, str]]

    @property
    def ready(self) -> bool:
        return (
            self.master_codes > 0
            and self.rule_rows > 0
            and self.complete_codes == self.master_codes
            and not self.issues
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "READY" if self.ready else "BLOCKED",
            "coverage_contract": "full_required_lifecycle_calendar_coverage",
            "required_start_policy": "max(data_start_date, master.list_date)",
            "required_end_policy": (
                "min(data_end_date, master.delist_date) when delist_date exists"
            ),
            "known_at_policy": "known_at <= effective_from",
            "required_fields": list(REQUIRED_RULE_FIELDS),
            "data_start_date": self.start_date.isoformat(),
            "data_end_date": self.end_date.isoformat(),
            "master_codes": self.master_codes,
            "rule_rows": self.rule_rows,
            "valid_rule_rows": self.valid_rule_rows,
            "codes_with_rules": self.codes_with_rules,
            "complete_codes": self.complete_codes,
            "incomplete_code_count": len(self.incomplete_codes),
            "missing_rule_code_count": len(self.missing_rule_codes),
            "missing_rule_codes": self.missing_rule_codes,
            "incomplete_codes": self.incomplete_codes,
            "issue_counts": self.issue_counts,
            "issues": self.issues,
        }


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    return text.casefold() in _PLACEHOLDER_VALUES


def _parse_date(value: Any, field: str) -> date:
    if _is_missing(value):
        raise ValueError(f"missing required field {field}")
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"invalid date in {field}: {value!r}") from exc


def _parse_decimal(value: Any) -> Decimal:
    if _is_missing(value):
        raise ValueError("missing required field limit_pct")
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"invalid limit_pct: {value!r}") from exc


def _parse_rule(
    payload: Mapping[str, Any],
) -> tuple[_RuleRecord | None, list[dict[str, Any]]]:
    code = str(payload.get("code", "")).strip()
    issues: list[dict[str, Any]] = []

    def add_issue(kind: str, message: str) -> None:
        issues.append(
            {
                "code": code or "*",
                "kind": kind,
                "message": message,
            }
        )

    for field in REQUIRED_RULE_FIELDS:
        if _is_missing(payload.get(field)):
            add_issue("missing_required_field", f"missing or placeholder {field}")

    try:
        effective_from = _parse_date(payload.get("effective_from"), "effective_from")
    except ValueError as exc:
        add_issue("invalid_effective_from", str(exc))
        effective_from = None
    try:
        effective_to = _parse_date(payload.get("effective_to"), "effective_to")
    except ValueError as exc:
        add_issue("invalid_effective_to", str(exc))
        effective_to = None
    try:
        known_at = _parse_date(payload.get("known_at"), "known_at")
    except ValueError as exc:
        add_issue("invalid_known_at", str(exc))
        known_at = None
    try:
        limit_pct = _parse_decimal(payload.get("limit_pct"))
    except ValueError as exc:
        add_issue("invalid_limit_pct", str(exc))
        limit_pct = None

    settlement = str(payload.get("settlement", "")).strip().upper()
    if settlement and settlement not in {"T0", "T1"}:
        add_issue("invalid_settlement", f"unsupported settlement {settlement!r}")
    trading_status = str(payload.get("trading_status", "")).strip().upper()
    if trading_status and trading_status not in {"ACTIVE", "SUSPENDED"}:
        add_issue(
            "invalid_trading_status",
            f"unsupported trading_status {trading_status!r}",
        )

    if limit_pct is not None and not (Decimal("0") < limit_pct <= Decimal("1")):
        add_issue("invalid_limit_pct", "limit_pct must be in (0, 1]")
    if effective_from is not None and effective_to is not None:
        if effective_from > effective_to:
            add_issue("invalid_interval", "effective_from follows effective_to")
    if known_at is not None and effective_from is not None and known_at > effective_from:
        add_issue("future_known_at", "known_at follows effective_from")

    evidence_sha256 = payload.get("evidence_sha256")
    if evidence_sha256 is not None and not _is_missing(evidence_sha256):
        digest = str(evidence_sha256).removeprefix("sha256:").lower()
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            add_issue("invalid_evidence_sha256", "invalid evidence SHA-256")

    if (
        not code
        or effective_from is None
        or effective_to is None
        or known_at is None
        or limit_pct is None
    ):
        return None, issues
    return (
        _RuleRecord(
            code=code,
            settlement=settlement,
            effective_from=effective_from,
            effective_to=effective_to,
            limit_pct=limit_pct,
            trading_status=trading_status,
            known_at=known_at,
        ),
        issues,
    )


def audit_trading_rules(
    master: Mapping[str, MasterRecord],
    rule_rows: Iterable[Mapping[str, Any]],
    *,
    start_date: date,
    end_date: date,
) -> TradingRuleAuditResult:
    if start_date > end_date:
        raise ValueError("start_date must not follow end_date")
    rows = list(rule_rows)
    issues: list[dict[str, Any]] = []
    issue_codes_by_code: dict[str, set[str]] = {}
    parsed_by_code: dict[str, list[_RuleRecord]] = {}
    structural_by_code: dict[str, list[_RuleRecord]] = {}
    raw_by_code: dict[str, list[Mapping[str, Any]]] = {}
    valid_rule_rows = 0

    def add_issue(
        code: str,
        kind: str,
        message: str,
        **details: Any,
    ) -> None:
        issue = {"code": code, "kind": kind, "message": message, **details}
        issues.append(issue)
        issue_codes_by_code.setdefault(code, set()).add(kind)

    for raw in rows:
        code = str(raw.get("code", "")).strip()
        if not code:
            add_issue("*", "missing_code", "rule row has no code")
            continue
        raw_by_code.setdefault(code, []).append(raw)
        if code not in master:
            add_issue(code, "unknown_code", "rule code is absent from master")
        parsed, row_issues = _parse_rule(raw)
        for issue in row_issues:
            add_issue(
                code or "*",
                str(issue["kind"]),
                str(issue["message"]),
            )
        if parsed is not None:
            structural_by_code.setdefault(code, []).append(parsed)
        if row_issues or parsed is None:
            continue
        parsed_by_code.setdefault(code, []).append(parsed)
        valid_rule_rows += 1

    required_ranges: dict[str, tuple[date, date] | None] = {}
    for code, record in master.items():
        required_start = max(start_date, record.list_date)
        required_end = (
            min(end_date, record.delist_date)
            if record.delist_date is not None
            else end_date
        )
        required_ranges[code] = (
            (required_start, required_end)
            if required_start <= required_end
            else None
        )

    missing_rule_codes: list[str] = []
    for code in sorted(master):
        required = required_ranges[code]
        if required is None:
            continue
        required_start, required_end = required
        records = sorted(
            parsed_by_code.get(code, []),
            key=lambda item: (item.effective_from, item.effective_to),
        )
        if not records:
            missing_rule_codes.append(code)
            add_issue(code, "missing_rule", "no valid rule row for required lifecycle")
            continue

        structural_records = sorted(
            structural_by_code.get(code, []),
            key=lambda item: (item.effective_from, item.effective_to),
        )
        for previous, current in zip(structural_records, structural_records[1:]):
            if current.effective_from <= previous.effective_to:
                add_issue(
                    code,
                    "conflicting_intervals",
                    "overlapping effective intervals",
                    first_from=previous.effective_from.isoformat(),
                    first_to=previous.effective_to.isoformat(),
                    second_from=current.effective_from.isoformat(),
                    second_to=current.effective_to.isoformat(),
                )

        cursor = required_start
        for record in records:
            if record.effective_to < required_start:
                continue
            if record.effective_from > required_end:
                break
            overlap_start = max(record.effective_from, required_start)
            overlap_end = min(record.effective_to, required_end)
            if overlap_start > cursor:
                gap_end = min(overlap_start - timedelta(days=1), required_end)
                add_issue(
                    code,
                    "coverage_gap",
                    "required lifecycle is not continuously covered",
                    gap_from=cursor.isoformat(),
                    gap_to=gap_end.isoformat(),
                )
            cursor = max(cursor, overlap_end + timedelta(days=1))
            if cursor > required_end:
                break
        if cursor <= required_end:
            add_issue(
                code,
                "coverage_gap",
                "required lifecycle is not continuously covered",
                gap_from=cursor.isoformat(),
                gap_to=required_end.isoformat(),
            )

    incomplete_codes = sorted(
        code for code in master if code in issue_codes_by_code
    )
    issue_counts: dict[str, int] = {}
    for issue in issues:
        kind = str(issue["kind"])
        issue_counts[kind] = issue_counts.get(kind, 0) + 1

    gap_rows: list[dict[str, str]] = []
    incomplete = set(incomplete_codes)
    for code in sorted(incomplete):
        record = master[code]
        required = required_ranges[code]
        existing = ";".join(
            f"{row.effective_from.isoformat()}..{row.effective_to.isoformat()}"
            for row in sorted(
                parsed_by_code.get(code, []),
                key=lambda item: item.effective_from,
            )
        )
        gap_rows.append(
            {
                "code": code,
                "name": record.name_history[0].name,
                "list_date": record.list_date.isoformat(),
                "delist_date": (
                    record.delist_date.isoformat()
                    if record.delist_date is not None
                    else ""
                ),
                "required_effective_from": (
                    required[0].isoformat() if required is not None else ""
                ),
                "required_effective_to": (
                    required[1].isoformat() if required is not None else ""
                ),
                "settlement": "",
                "effective_from": "",
                "effective_to": "",
                "limit_pct": "",
                "trading_status": "",
                "known_at": "",
                "source": "",
                "evidence_id": "",
                "evidence_sha256": "",
                "existing_intervals": existing,
                "audit_issue_codes": ";".join(
                    sorted(issue_codes_by_code.get(code, set()))
                ),
            }
        )

    return TradingRuleAuditResult(
        start_date=start_date,
        end_date=end_date,
        master_codes=len(master),
        rule_rows=len(rows),
        valid_rule_rows=valid_rule_rows,
        codes_with_rules=len(raw_by_code),
        complete_codes=len(master) - len(incomplete),
        missing_rule_codes=missing_rule_codes,
        incomplete_codes=incomplete_codes,
        issue_counts=dict(sorted(issue_counts.items())),
        issues=issues,
        gap_rows=gap_rows,
    )


def write_rule_gap_csv(rows: list[dict[str, str]], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RULE_GAP_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit the per-code trading-rule evidence contract"
    )
    parser.add_argument("--config", default="config.m1_2.yaml")
    parser.add_argument(
        "--report",
        default="reports/m1_2/trading_rule_audit.json",
    )
    parser.add_argument(
        "--gap",
        default="reports/m1_2/trading_rule_gap.csv",
    )
    args = parser.parse_args(argv)
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    data = config["data"]
    pipeline = config["data_pipeline"]
    master = load_master_records(Path(str(pipeline["master_file"])))
    rules_path = Path(str(pipeline["trading_rules_file"]))
    rules = read_ndjson(rules_path) if rules_path.exists() else []
    result = audit_trading_rules(
        master,
        rules,
        start_date=date.fromisoformat(str(data["start_date"])),
        end_date=date.fromisoformat(str(data["end_date"])),
    )
    report = result.to_dict()
    report["master_file"] = str(pipeline["master_file"])
    report["trading_rules_file"] = str(rules_path)
    report["gap_file"] = str(args.gap)
    write_json_atomic(Path(args.report), report)
    write_rule_gap_csv(result.gap_rows, Path(args.gap))
    print(
        json.dumps(
            {
                "status": report["status"],
                "master_codes": report["master_codes"],
                "rule_rows": report["rule_rows"],
                "complete_codes": report["complete_codes"],
                "missing_rule_code_count": report["missing_rule_code_count"],
                "issue_counts": report["issue_counts"],
                "report": args.report,
                "gap": args.gap,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
