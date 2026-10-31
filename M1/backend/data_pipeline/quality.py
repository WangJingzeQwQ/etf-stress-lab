from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.data_pipeline.contracts import (
    DataQualityError,
    Phase1Bundle,
)


@dataclass(frozen=True)
class QualityIssue:
    severity: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }


@dataclass
class QualityReport:
    issues: list[QualityIssue] = field(default_factory=list)
    formal_research_allowed: bool = False

    @property
    def errors(self) -> list[QualityIssue]:
        return [issue for issue in self.issues if issue.severity == "ERROR"]

    @property
    def warnings(self) -> list[QualityIssue]:
        return [issue for issue in self.issues if issue.severity == "WARNING"]

    @property
    def backtest_allowed(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "backtest_allowed": self.backtest_allowed,
            "formal_research_allowed": self.formal_research_allowed,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [issue.to_dict() for issue in self.issues],
        }


def validate_bundle(
    bundle: Phase1Bundle,
    *,
    formal_research: bool = False,
) -> QualityReport:
    bundle.validate()
    report = QualityReport()

    if bundle.manifest_data.source_kind != "REAL_PROVIDER":
        report.issues.append(
            QualityIssue(
                "WARNING",
                "*",
                "Source is synthetic or parameterized; not real-market evidence.",
            )
        )
    if not bundle.manifest_data.coverage.all_required:
        report.issues.append(
            QualityIssue(
                (
                    "ERROR"
                    if formal_research
                    or bundle.manifest_data.source_kind == "REAL_PROVIDER"
                    else "WARNING"
                ),
                "*",
                "One or more required historical coverage flags are false.",
            )
        )

    for code in sorted(bundle.dataset.codes):
        record = bundle.master.get(code)
        if record is None:
            report.issues.append(
                QualityIssue("ERROR", code, "Bar series lacks a master record.")
            )
            continue
        if not record.history_complete:
            report.issues.append(
                QualityIssue(
                    "ERROR" if formal_research or bundle.manifest_data.source_kind == "REAL_PROVIDER" else "WARNING",
                    code,
                    "Master history is explicitly marked incomplete.",
                )
            )
        if record.delist_date is not None:
            latest_bar = max(
                session for (session, bar_code) in bundle.dataset.bars if bar_code == code
            )
            if latest_bar > record.delist_date:
                report.issues.append(
                    QualityIssue(
                        "ERROR",
                        code,
                        "Bar data extends beyond the recorded delist date.",
                    )
                )

    for code in sorted(set(bundle.master) - set(bundle.dataset.codes)):
        report.issues.append(
            QualityIssue(
                "WARNING",
                code,
                "Master record has no bar data in this bundle.",
            )
        )

    for action in bundle.dataset.corporate_actions:
        if action.code not in bundle.master:
            report.issues.append(
                QualityIssue(
                    "ERROR",
                    action.code,
                    "Corporate action lacks a master record.",
                )
            )

    for rule in bundle.dataset.trading_rules:
        if rule.code not in bundle.master:
            report.issues.append(
                QualityIssue(
                    "ERROR",
                    rule.code,
                    "Trading rule lacks a master record.",
                )
            )

    report.formal_research_allowed = (
        bundle.manifest_data.source_kind == "REAL_PROVIDER"
        and bundle.manifest_data.coverage.all_required
        and bundle.manifest_data.real_market_evidence_allowed
        and not any(not record.history_complete for record in bundle.master.values())
        and not report.errors
    )
    if formal_research and not report.formal_research_allowed:
        report.issues.append(
            QualityIssue(
                "ERROR",
                "*",
                "Formal research requested but source/coverage gates are not satisfied.",
            )
        )
    return report


def require_quality(report: QualityReport) -> None:
    if report.errors:
        messages = "; ".join(
            f"{issue.code}: {issue.message}" for issue in report.errors
        )
        raise DataQualityError(messages)

