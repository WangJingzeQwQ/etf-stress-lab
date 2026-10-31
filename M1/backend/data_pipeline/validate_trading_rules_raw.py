from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import argparse
import csv
import json
from pathlib import Path
from typing import Any, Iterable

import yaml

from backend.data_pipeline.storage import read_ndjson, write_json_atomic
from backend.data_pipeline.trading_rule_audit import audit_trading_rules
from backend.data_pipeline.fetch_master import load_master_records


PRICE_TICK = Decimal("0.001")


@dataclass(frozen=True)
class _Rule:
    settlement: str
    effective_from: date
    effective_to: date
    limit_pct: Decimal
    trading_status: str
    known_at: date


def _price(value: Decimal) -> Decimal:
    return value.quantize(PRICE_TICK, rounding=ROUND_HALF_UP)


def _read_rule_map(rows: Iterable[dict[str, Any]]) -> dict[str, list[_Rule]]:
    result: dict[str, list[_Rule]] = {}
    for row in rows:
        result.setdefault(str(row["code"]), []).append(
            _Rule(
                settlement=str(row["settlement"]),
                effective_from=date.fromisoformat(str(row["effective_from"])),
                effective_to=date.fromisoformat(str(row["effective_to"])),
                limit_pct=Decimal(str(row["limit_pct"])),
                trading_status=str(row["trading_status"]),
                known_at=date.fromisoformat(str(row["known_at"])),
            )
        )
    for values in result.values():
        values.sort(key=lambda item: item.effective_from)
    return result


def _rule_for(
    rules: dict[str, list[_Rule]],
    code: str,
    trading_date: date,
) -> tuple[_Rule | None, str]:
    matches = [
        item
        for item in rules.get(code, [])
        if item.effective_from <= trading_date <= item.effective_to
    ]
    if not matches:
        return None, "missing_rule"
    if len(matches) > 1:
        return None, "conflicting_rule"
    if matches[0].known_at > trading_date:
        return None, "future_known_at"
    return matches[0], ""


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _load_actions(
    path: Path,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for row in read_ndjson(path):
        result.setdefault(str(row["code"]), []).append(row)
    for rows in result.values():
        rows.sort(key=lambda item: str(item["effective_date"]))
    return result


def _reference_price(
    previous_close: Decimal,
    previous_date: date,
    current_date: date,
    actions: Iterable[dict[str, Any]],
    split_application_dates: dict[tuple[str, str, str], date],
) -> tuple[Decimal, list[dict[str, Any]]]:
    reference = previous_close
    applied = []
    for action in actions:
        effective = date.fromisoformat(str(action["effective_date"]))
        if action["action_type"] == "split":
            # 拆分折算日 is an after-close conversion date.  The first raw
            # bar reflecting the new unit is the next available session.
            key = (
                str(action.get("announcement_report_id", "")),
                effective.isoformat(),
                str(action.get("action_type")),
            )
            application_date = split_application_dates.get(key)
            if application_date is None:
                continue
            if application_date != current_date:
                continue
            reference /= Decimal(str(action["split_ratio"]))
        elif action["action_type"] == "cash_dividend":
            if not previous_date < effective <= current_date:
                continue
            reference -= Decimal(str(action["cash_per_share"]))
        else:
            raise ValueError(f"Unsupported action {action['action_type']!r}")
        applied.append(action)
    return reference, applied


def _raw_validation(
    root: Path,
    config: dict[str, Any],
    rule_path: Path,
) -> dict[str, Any]:
    root = root.resolve()
    module_root = root if (root / "data/m1_2").exists() else root / "M1"
    pipeline = config["data_pipeline"]
    data = config["data"]
    master = load_master_records(module_root / str(pipeline["master_file"]))
    rules_payloads = read_ndjson(module_root / rule_path)
    rules = _read_rule_map(rules_payloads)
    actions = _load_actions(
        module_root / str(pipeline["corporate_actions_file"])
    )
    reference_overrides = {
        (str(row["code"]), date.fromisoformat(str(row["trading_date"]))): (
            Decimal(str(row["preclose"])),
            row,
        )
        for row in read_ndjson(
            module_root
            / "data/m1_2/cache/trading_rules"
            / "corporate_action_price_references.ndjson"
        )
    }
    raw_root = module_root / str(pipeline["raw_directory"])
    start_date = date.fromisoformat(str(data["start_date"]))
    end_date = date.fromisoformat(str(data["end_date"]))
    audit = audit_trading_rules(
        master,
        rules_payloads,
        start_date=start_date,
        end_date=end_date,
    )

    all_sessions: set[date] = set()
    raw_files = sorted(raw_root.glob("*.ndjson"))
    for path in raw_files:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                all_sessions.add(date.fromisoformat(str(row["trading_date"])))
    ordered_sessions = sorted(all_sessions)

    issue_rows: list[dict[str, Any]] = []
    totals = Counter()
    price_violation_samples: list[dict[str, Any]] = []
    code_reports = []
    for path in raw_files:
        code = path.stem
        bars = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    bars.append(json.loads(line))
        bars.sort(key=lambda item: str(item["trading_date"]))
        if not bars:
            issue_rows.append(
                {
                    "code": code,
                    "issue": "empty_raw_file",
                    "date_from": "",
                    "date_to": "",
                    "count": 0,
                    "detail": "",
                }
            )
            totals["empty_raw_file"] += 1
            continue

        zero_rows = [
            row
            for row in bars
            if Decimal(str(row["volume"])) == 0
            or Decimal(str(row["amount"])) == 0
        ]
        if zero_rows:
            totals["zero_volume_rows"] += len(zero_rows)
            totals["codes_with_zero_volume"] += 1
            issue_rows.append(
                {
                    "code": code,
                    "issue": "zero_volume_or_amount",
                    "date_from": str(zero_rows[0]["trading_date"]),
                    "date_to": str(zero_rows[-1]["trading_date"]),
                    "count": len(zero_rows),
                    "detail": "Broker must treat these bars as non-tradable.",
                }
            )

        long_gaps = []
        previous_date = None
        for row in bars:
            current = date.fromisoformat(str(row["trading_date"]))
            if previous_date is not None:
                days = (current - previous_date).days
                if days > 20:
                    missing_sessions = sum(
                        previous_date < session < current
                        for session in ordered_sessions
                    )
                    long_gaps.append(
                        (
                            previous_date,
                            current,
                            days,
                            missing_sessions,
                        )
                    )
            previous_date = current
        if long_gaps:
            totals["long_gap_count"] += len(long_gaps)
            totals["codes_with_long_gaps"] += 1
            for gap_from, gap_to, calendar_days, missing_sessions in long_gaps:
                issue_rows.append(
                    {
                        "code": code,
                        "issue": "long_quote_gap",
                        "date_from": gap_from.isoformat(),
                        "date_to": gap_to.isoformat(),
                        "count": missing_sessions,
                        "detail": (
                            f"{calendar_days} calendar days; "
                            f"{missing_sessions} market sessions have no "
                            "raw row for this code. Absence is treated as "
                            "no executable bar, not as proof of suspension."
                        ),
                    }
                )

        first_date = date.fromisoformat(str(bars[0]["trading_date"]))
        last_date = date.fromisoformat(str(bars[-1]["trading_date"]))
        expected_sessions = [
            session
            for session in ordered_sessions
            if first_date <= session <= last_date
        ]
        present_sessions = {
            date.fromisoformat(str(row["trading_date"])) for row in bars
        }
        missing_sessions = [
            session for session in expected_sessions if session not in present_sessions
        ]
        if missing_sessions:
            totals["missing_rule_calendar_bars"] += len(missing_sessions)
            totals["codes_with_missing_sessions"] += 1

        rule_issues = Counter()
        violations = 0
        session_dates = sorted(
            date.fromisoformat(str(row["trading_date"])) for row in bars
        )
        split_application_dates = {}
        for action in actions.get(code, []):
            if action["action_type"] != "split":
                continue
            effective = date.fromisoformat(str(action["effective_date"]))
            application_date = next(
                (
                    session
                    for session in session_dates
                    if session > effective
                ),
                None,
            )
            if application_date is not None:
                split_application_dates[
                    (
                        str(action.get("announcement_report_id", "")),
                        effective.isoformat(),
                        str(action.get("action_type")),
                    )
                ] = application_date
        for row in bars:
            trading_date = date.fromisoformat(str(row["trading_date"]))
            rule, reason = _rule_for(rules, code, trading_date)
            if rule is None:
                rule_issues[reason] += 1
                issue_rows.append(
                    {
                        "code": code,
                        "issue": reason,
                        "date_from": trading_date.isoformat(),
                        "date_to": trading_date.isoformat(),
                        "count": 1,
                        "detail": "",
                    }
                )

        for previous, current in zip(bars, bars[1:]):
            current_date = date.fromisoformat(str(current["trading_date"]))
            previous_date = date.fromisoformat(str(previous["trading_date"]))
            rule, _ = _rule_for(rules, code, current_date)
            if rule is None:
                continue
            override = reference_overrides.get((code, current_date))
            if override is not None:
                reference, override_row = override
                applied = [override_row]
                totals["reference_price_overrides_used"] += 1
            else:
                reference, applied = _reference_price(
                    Decimal(str(previous["raw_close"])),
                    previous_date,
                    current_date,
                    actions.get(code, []),
                    split_application_dates,
                )
            if reference <= 0:
                totals["non_positive_adjusted_reference"] += 1
                continue
            upper = _price(reference * (Decimal("1") + rule.limit_pct))
            lower = _price(reference * (Decimal("1") - rule.limit_pct))
            high = Decimal(str(current["raw_high"]))
            low = Decimal(str(current["raw_low"]))
            if high > upper + PRICE_TICK or low < lower - PRICE_TICK:
                violations += 1
                totals["price_limit_violations"] += 1
                if len(price_violation_samples) < 200:
                    price_violation_samples.append(
                        {
                            "code": code,
                            "trading_date": current_date.isoformat(),
                            "previous_date": previous_date.isoformat(),
                            "limited_reference": str(reference),
                            "limit_pct": str(rule.limit_pct),
                            "allowed_low": str(lower),
                            "allowed_high": str(upper),
                            "raw_low": str(low),
                            "raw_high": str(high),
                            "actions_applied": [
                                str(item.get("action_type"))
                                for item in applied
                            ],
                        }
                    )
        if violations:
            issue_rows.append(
                {
                    "code": code,
                    "issue": "price_limit_violation",
                    "date_from": str(bars[0]["trading_date"]),
                    "date_to": str(bars[-1]["trading_date"]),
                    "count": violations,
                    "detail": "OHLC exceeded action-adjusted allowed band.",
                }
            )

        code_reports.append(
            {
                "code": code,
                "rows": len(bars),
                "first_date": first_date.isoformat(),
                "last_date": last_date.isoformat(),
                "zero_volume_rows": len(zero_rows),
                "missing_market_sessions": len(missing_sessions),
                "long_gap_count": len(long_gaps),
                "rule_issues": dict(rule_issues),
                "price_limit_violations": violations,
            }
        )

    error_kinds = {
        "empty_raw_file",
        "missing_rule",
        "conflicting_rule",
        "future_known_at",
        "non_positive_adjusted_reference",
        "price_limit_violation",
    }
    errors = [
        row for row in issue_rows if str(row["issue"]) in error_kinds
    ]
    return {
        "status": "BLOCKED" if errors else "VALIDATED",
        "rule_audit_status": "READY" if audit.ready else "BLOCKED",
        "rules_path": str(rule_path),
        "rules_sha256": _file_sha256(module_root / rule_path),
        "raw_files": len(raw_files),
        "raw_rows": sum(item["rows"] for item in code_reports),
        "market_sessions": len(ordered_sessions),
        "totals": dict(sorted(totals.items())),
        "error_count": len(errors),
        "review_count": len(issue_rows) - len(errors),
        "price_limit_violation_samples": price_violation_samples,
        "issue_rows": issue_rows,
        "codes": code_reports,
        "notes": [
            "A missing raw row is not treated as an executable bar.",
            "Zero-volume bars are retained only so the Broker can reject execution.",
            "Long no-quote intervals do not by themselves prove suspension.",
            "Price-limit checks use declared corporate actions as the ex-reference adjustment.",
        ],
    }


def _write_issue_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    fields = ["code", "issue", "date_from", "date_to", "count", "detail"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate candidate trading rules against raw ETF quotes"
    )
    parser.add_argument("--config", default="config.m1_2.yaml")
    parser.add_argument(
        "--rules",
        default="data/m1_2/cache/trading_rules/trading_rules.candidate.ndjson",
    )
    parser.add_argument(
        "--output",
        default="reports/m1_2/trading_rules_raw_validation.json",
    )
    parser.add_argument(
        "--issues",
        default="reports/m1_2/trading_rules_raw_validation_issues.csv",
    )
    args = parser.parse_args(argv)
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    report = _raw_validation(Path("."), config, Path(args.rules))
    write_json_atomic(Path(args.output), report)
    _write_issue_csv(Path(args.issues), report["issue_rows"])
    summary = {
        key: report[key]
        for key in (
            "status",
            "rule_audit_status",
            "raw_files",
            "raw_rows",
            "market_sessions",
            "totals",
            "error_count",
            "review_count",
        )
    }
    summary["output"] = args.output
    summary["issues"] = args.issues
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "VALIDATED" and report["rule_audit_status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
