from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
import argparse
import csv
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

import yaml

from backend.data_pipeline.contracts import MasterRecord
from backend.data_pipeline.fetch_master import load_master_records
from backend.data_pipeline.storage import (
    read_json,
    read_ndjson,
    write_json_atomic,
    write_ndjson_atomic,
)
from backend.data_pipeline.trading_rule_audit import audit_trading_rules
from backend.data_pipeline.trading_rule_evidence import (
    classify_etf_product,
    is_twenty_percent_fund,
)


_T0_NEEDLES = (
    "当日买入的基金份额",
    "当日买入的份额",
    "T 日买入的基金份额",
    "T日买入的基金份额",
    "T 日买入的份额",
    "T日买入的份额",
    "T 日买入的 A 类",
    "T日买入的A类",
    "当日买入的黄金ETF份额",
    "当日买入的现金申赎类债券ETF份额",
)
_T0_SELL = re.compile(
    r"(?:当日|同日|T\s*日)(?:起)?(?:可以|可)"
    r"(?:卖出(?:或赎回)?|赎回或卖出)"
)
_T1_SELL = re.compile(
    r"(?:不得卖出|(?:次一交易日|次日|T\+1\s*日?)"
    r"[^。]{0,60}(?:可以|可)[^。]{0,20}卖出)"
)
_LIMIT_VALUE = re.compile(r"(10|20)\s*%")


@dataclass(frozen=True)
class ProductDocument:
    code: str
    document_id: str
    title: str
    published_on: date
    source: str
    text_path: Path | None
    local_path: Path | None


@dataclass(frozen=True)
class ProductEvidence:
    code: str
    baseline: ProductDocument
    current: ProductDocument
    category: str
    listing_document: ProductDocument | None
    baseline_settlement_hints: tuple[str, ...]
    current_settlement_hints: tuple[str, ...]


@dataclass(frozen=True)
class RuleSegment:
    effective_from: date
    effective_to: date
    source: str
    evidence_ids: tuple[str, ...]
    known_at: date
    evidence_sha256: str | None


def _file_sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _normalise_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_fund_settlement_hints(text: str) -> set[str]:
    """Extract active-sale T0/T1 hints without treating primary subscriptions
    or the underlying market's T+0 convention as the ETF's own rule.
    """

    compact = _normalise_text(text)
    hints: set[str] = set()
    for needle in _T0_NEEDLES:
        offset = 0
        while True:
            index = compact.find(needle, offset)
            if index < 0:
                break
            window = compact[index : index + 300]
            if _T1_SELL.search(window):
                hints.add("T1")
            if _T0_SELL.search(window):
                hints.add("T0")
            offset = index + len(needle)
    return hints


def extract_fund_limit_hints(text: str) -> set[str]:
    compact = _normalise_text(text)
    hints: set[str] = set()
    for needle in (
        "涨跌幅比例为",
        "涨跌幅限制比例为",
        "涨跌幅比例限制",
        "交易价格涨跌幅比例为",
    ):
        offset = 0
        while True:
            index = compact.find(needle, offset)
            if index < 0:
                break
            window = compact[max(0, index - 40) : index + 100]
            match = _LIMIT_VALUE.search(window)
            if match:
                hints.add(match.group(1))
            offset = index + len(needle)
    return hints


def _read_product_text(document: ProductDocument) -> str:
    if document.text_path is None or not document.text_path.exists():
        return ""
    return document.text_path.read_text(encoding="utf-8", errors="replace")


def _document_date(row: dict[str, Any], source: str) -> date | None:
    field = (
        row.get("reportSendDate")
        if source == "CSRC_FUND_DISCLOSURE"
        else row.get("publish_date")
    )
    return _parse_date(field)


def _load_merged_announcements(root: Path, config: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    configured = (
        config.get("trading_rule_resolution", {}).get("announcement_merged_root")
        or "data/m1_2/cache/trading_rules/announcements/merged"
    )
    directory = root / str(configured)
    rows: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(directory.glob("*.json")):
        payload = read_json(path, {})
        if isinstance(payload, dict):
            rows[path.stem] = list(payload.get("announcements", []))
    return rows


def _select_listing_document(
    code: str,
    announcements: Iterable[dict[str, Any]],
) -> ProductDocument | None:
    candidates = [
        row
        for row in announcements
        if re.search(
            r"上市交易公告书|上市交易提示|上市交易公告",
            str(row.get("TITLE", "")),
        )
        and _parse_date(row.get("PUBLISHDATE")) is not None
    ]
    if not candidates:
        return None
    row = min(candidates, key=lambda item: str(item.get("PUBLISHDATE", "")))
    return ProductDocument(
        code=code,
        document_id=str(row.get("ID", "")),
        title=str(row.get("TITLE", "")),
        published_on=_parse_date(row.get("PUBLISHDATE")) or date.min,
        source="PUBLIC_FUND_ANNOUNCEMENT_INDEX",
        text_path=None,
        local_path=None,
    )


def load_product_evidence(
    root: Path,
    config: dict[str, Any],
) -> dict[str, ProductEvidence]:
    root = root.resolve()
    pipeline = config["data_pipeline"]
    module_root = root if (root / "data/m1_2").exists() else root / "M1"
    trading_cache = module_root / "data/m1_2/cache/trading_rules"
    master = load_master_records(module_root / str(pipeline["master_file"]))
    announcements = _load_merged_announcements(module_root, config)

    by_code: dict[str, list[ProductDocument]] = {}
    for row in read_ndjson(trading_cache / "rule_document_selection.ndjson"):
        published_on = _document_date(row, "CSRC_FUND_DISCLOSURE")
        if published_on is None:
            continue
        document_id = str(row["uploadInfoId"])
        by_code.setdefault(str(row["code"]), []).append(
            ProductDocument(
                code=str(row["code"]),
                document_id=document_id,
                title=str(row.get("reportName", "")),
                published_on=published_on,
                source="CSRC_FUND_DISCLOSURE",
                text_path=trading_cache / "csrc" / "texts" / f"{document_id}.txt",
                local_path=trading_cache / "csrc" / "pdfs" / f"{document_id}.pdf",
            )
        )

    for row in read_ndjson(trading_cache / "eastmoney_fallback_selection.ndjson"):
        published_on = _document_date(row, "EASTMONEY_PUBLIC_ANNOUNCEMENT")
        if published_on is None:
            continue
        document_id = str(row["id"])
        by_code.setdefault(str(row["code"]), []).append(
            ProductDocument(
                code=str(row["code"]),
                document_id=document_id,
                title=str(row.get("title", "")),
                published_on=published_on,
                source="EASTMONEY_PUBLIC_ANNOUNCEMENT",
                text_path=(
                    trading_cache
                    / "eastmoney_fallback"
                    / "texts"
                    / f"{document_id}.txt"
                ),
                local_path=(
                    trading_cache
                    / "eastmoney_fallback"
                    / "pdfs"
                    / f"{document_id}.pdf"
                ),
            )
        )

    for row in read_ndjson(
        trading_cache / "rule_evidence_supplements.ndjson"
    ):
        published_on = _parse_date(row.get("publish_date"))
        if published_on is None:
            continue
        code = str(row["code"])
        by_code.setdefault(code, []).append(
            ProductDocument(
                code=code,
                document_id=str(row["document_id"]),
                title=str(row["title"]),
                published_on=published_on,
                source=str(row.get("source", "PUBLIC_SUPPLEMENT")),
                text_path=module_root / str(row["text_path"]),
                local_path=module_root / str(row["local_path"]),
            )
        )

    result: dict[str, ProductEvidence] = {}
    required_start = date.fromisoformat(str(config["data"]["start_date"]))
    for code, record in sorted(master.items()):
        documents = sorted(
            by_code.get(code, []),
            key=lambda item: (item.published_on, item.document_id),
        )
        if not documents:
            raise ValueError(f"No product document evidence for {code}")
        current = documents[-1]
        listing = _select_listing_document(code, announcements.get(code, []))
        eligible = [
            item for item in documents if item.published_on <= max(required_start, record.list_date)
        ]
        baseline = eligible[-1] if eligible else documents[0]
        # The latest product title is authoritative for the fund scope.  An
        # older listing title may contain a since-corrected provider typo (for
        # example 159803 was briefly indexed with "QDII") and must not override
        # the current legal name.
        category = classify_etf_product(current.title)
        baseline_hints = tuple(
            sorted(extract_fund_settlement_hints(_read_product_text(baseline)))
        )
        current_hints = tuple(
            sorted(extract_fund_settlement_hints(_read_product_text(current)))
        )
        result[code] = ProductEvidence(
            code=code,
            baseline=baseline,
            current=current,
            category=category,
            listing_document=listing,
            baseline_settlement_hints=baseline_hints,
            current_settlement_hints=current_hints,
        )
    return result


def _merge_intervals(
    events: list[tuple[date, str, dict[str, Any]]],
    required_start: date,
    required_end: date,
) -> list[dict[str, Any]]:
    ordered = sorted(
        (
            (max(required_start, effective_from), value, metadata)
            for effective_from, value, metadata in events
            if effective_from <= required_end
        ),
        key=lambda item: (item[0], item[1]),
    )
    if not ordered or ordered[0][0] > required_start:
        raise ValueError("Rule event schedule does not begin at required lifecycle")
    rows: list[dict[str, Any]] = []
    for index, (effective_from, value, metadata) in enumerate(ordered):
        next_start = (
            ordered[index + 1][0] if index + 1 < len(ordered) else required_end + timedelta(days=1)
        )
        effective_to = min(required_end, next_start - timedelta(days=1))
        if effective_from > effective_to:
            continue
        if (
            rows
            and rows[-1]["settlement"] == value
            and rows[-1]["source"] == metadata["source"]
            and rows[-1]["evidence_sha256"] == metadata.get("evidence_sha256")
        ):
            rows[-1]["effective_to"] = effective_to.isoformat()
            continue
        rows.append(
            {
                "settlement": value,
                "effective_from": effective_from.isoformat(),
                "effective_to": effective_to.isoformat(),
                "known_at": metadata["known_at"].isoformat(),
                "source": metadata["source"],
                "evidence_id": ";".join(metadata["evidence_ids"]),
                "evidence_sha256": metadata.get("evidence_sha256"),
                "evidence_refs": list(metadata["evidence_ids"]),
            }
        )
    return rows


def _exchange_segments(
    exchange: str,
    start: date,
    end: date,
    official_dir: Path,
) -> list[RuleSegment]:
    if exchange == "SSE":
        candidates = [
            RuleSegment(
                date(2018, 1, 1),
                date(2020, 1, 19),
                "上海证券交易所交易型开放式指数基金业务实施细则及交易规则",
                (
                    "SSE_ETF_BUSINESS_RULES_2012_NOTICE",
                    "SSE_TRADING_RULES_2018",
                ),
                date(2012, 3, 23),
                _file_sha256(official_dir / "sse_etf_business_rules_2012_notice.html"),
            ),
            RuleSegment(
                date(2020, 1, 20),
                date(2020, 12, 3),
                "上海证券交易所交易型开放式指数基金业务实施细则（2020年修订）",
                (
                    "SSE_ETF_BUSINESS_RULES_2020_FIRST",
                    "SSE_TRADING_RULES_2018",
                ),
                date(2020, 1, 7),
                _file_sha256(official_dir / "sse_etf_business_rules_2020_first.docx"),
            ),
            RuleSegment(
                date(2020, 12, 4),
                date(2026, 7, 5),
                "上海证券交易所交易型开放式指数基金业务实施细则（2020年第二次修订）",
                ("SSE_ETF_BUSINESS_RULES_2020_SECOND",),
                date(2020, 12, 4),
                _file_sha256(official_dir / "sse_etf_rules_2020.docx"),
            ),
            RuleSegment(
                date(2026, 7, 6),
                date.max,
                "上海证券交易所交易规则（2026年修订）及现行基金业务实施细则",
                (
                    "SSE_TRADING_RULES_2026",
                    "SSE_ETF_BUSINESS_RULES_2020_SECOND",
                ),
                date(2026, 4, 24),
                _file_sha256(official_dir / "sse_trading_rules_2026.docx"),
            ),
        ]
    else:
        candidates = [
            RuleSegment(
                date(2018, 1, 1),
                date(2019, 1, 10),
                "深圳证券交易所证券投资基金交易和申购赎回实施细则（2016年修订）",
                ("SZSE_FUND_RULES_2016",),
                date(2016, 5, 9),
                None,
            ),
            RuleSegment(
                date(2019, 1, 11),
                date(2019, 10, 20),
                "深圳证券交易所基金交易和申赎细则（2019年1月修订）",
                ("SZSE_FUND_RULES_2016", "SZSE_FUND_RULES_2019_24"),
                date(2019, 1, 11),
                None,
            ),
            RuleSegment(
                date(2019, 10, 21),
                date(2020, 12, 3),
                "深圳证券交易所证券投资基金交易和申购赎回实施细则（2019年修订）",
                ("SZSE_FUND_TRADING_RULES_2019",),
                date(2019, 9, 27),
                _file_sha256(official_dir / "szse_fund_trading_rules_2019.pdf"),
            ),
            RuleSegment(
                date(2020, 12, 4),
                date.max,
                "深圳证券交易所证券投资基金交易和申购赎回实施细则（2020年12月修订）",
                ("SZSE_FUND_TRADING_RULES_2020_730",),
                date(2020, 12, 4),
                _file_sha256(official_dir / "szse_fund_trading_rules_2020.pdf"),
            ),
        ]
    return [
        segment
        for segment in candidates
        if segment.effective_to >= start and segment.effective_from <= end
    ]


def _settlement_for_product(
    evidence: ProductEvidence,
    record: MasterRecord,
) -> tuple[str, date | None, str]:
    category = evidence.category
    if category == "stock":
        return "T1", None, "exchange_stock_fund_rule"
    if category in {"bond", "gold", "money", "commodity"}:
        return "T0", None, f"official_{category}_etf_rule"
    if category != "crossborder":
        raise ValueError(f"Unsupported product category {category!r}")

    if evidence.baseline_settlement_hints:
        if (
            "T1" in evidence.baseline_settlement_hints
            and "T0" not in evidence.baseline_settlement_hints
        ):
            return "T1", None, "fund_prospectus_T1"
        if (
            "T0" in evidence.baseline_settlement_hints
            and "T1" not in evidence.baseline_settlement_hints
        ):
            return "T0", None, "fund_prospectus_T0"
        if evidence.baseline_settlement_hints == ("T1", "T0"):
            return "T1", None, "fund_prospectus_explicit_T1"
    if evidence.current_settlement_hints:
        if "T1" in evidence.current_settlement_hints:
            return "T1", None, "fund_prospectus_explicit_T1"
        if "T0" in evidence.current_settlement_hints:
            if evidence.code == "513000":
                # The 2021 update establishes the later T0 interval.  Earlier
                # intervals follow the then-effective exchange cross-border
                # rule and must not be backfilled with the current product text.
                return "T1", date(2021, 7, 26), "2021_07_26_fund_update"
            if record.list_date >= date(2021, 7, 26):
                return "T0", None, "fund_prospectus_T0"
    # The exchange rule supplies T1 when the product evidence does not establish
    # the exchange-rule exception.  This is intentionally not a global default:
    # bond/gold/money/commodity and explicit T0 products are separated above.
    return "T1", None, "exchange_crossborder_rule_T1"


def _limit_events(
    evidence: ProductEvidence,
    record: MasterRecord,
) -> list[tuple[date, str, dict[str, Any]]]:
    title = f"{evidence.current.title} {evidence.listing_document.title if evidence.listing_document else ''}"
    if not is_twenty_percent_fund(title):
        return []
    star_or_new = (
        record.list_date >= date(2020, 8, 24)
        or re.search(r"科创板|科创50|科创100|科创200|科创综指", title)
    )
    if star_or_new:
        return []
    return [
        (
            date(2020, 8, 24),
            "0.20",
            {
                "known_at": date(2020, 6, 12),
                "source": "深交所创业板注册制改革涨跌幅规则",
                "evidence_ids": (
                    "SZSE_CHINEXT_20PCT_FAQ_20200729",
                    "SZSE_CHINEXT_FIRST_LISTING_20200824",
                ),
                "evidence_sha256": _file_sha256(
                    next(
                        path
                        for path in (
                            evidence.current.text_path.parents[2]
                            / "official_rules"
                            / "szse_chinext_20pct_faq_20200729.html",
                            evidence.current.text_path.parents[2]
                            / "official_rules"
                            / "szse_chinext_first_listing_20200824.html",
                        )
                        if path.exists()
                    )
                )
                if evidence.current.text_path is not None
                else None,
            },
        )
    ]


def build_trading_rule_candidates(
    root: Path,
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, ProductEvidence], Any]:
    root = root.resolve()
    module_root = root if (root / "data/m1_2").exists() else root / "M1"
    pipeline = config["data_pipeline"]
    master = load_master_records(module_root / str(pipeline["master_file"]))
    evidence = load_product_evidence(root, config)
    official_dir = module_root / "data/m1_2/cache/trading_rules/official_rules"
    start_date = date.fromisoformat(str(config["data"]["start_date"]))
    end_date = date.fromisoformat(str(config["data"]["end_date"]))
    all_rows: list[dict[str, Any]] = []

    for code, record in sorted(master.items()):
        required_start = max(start_date, record.list_date)
        required_end = min(end_date, record.delist_date or end_date)
        if required_start > required_end:
            continue
        product = evidence[code]
        exchange = "SSE" if code.startswith("5") else "SZSE"
        base_settlement, settlement_change_on, settlement_reason = (
            _settlement_for_product(product, record)
        )
        settlement_events: list[tuple[date, str, dict[str, Any]]] = []
        for segment in _exchange_segments(exchange, required_start, required_end, official_dir):
            segment_start = max(required_start, segment.effective_from)
            segment_end = min(required_end, segment.effective_to)
            if segment_start > segment_end:
                continue
            settlement_events.append(
                (
                    segment_start,
                    base_settlement,
                    {
                        "known_at": min(segment_start, segment.known_at),
                        "source": (
                            f"{segment.source}; "
                            f"{'产品招募说明书：' + product.current.title}"
                        ),
                        "evidence_ids": (
                            *segment.evidence_ids,
                            product.baseline.document_id,
                            product.current.document_id,
                        ),
                        "evidence_sha256": segment.evidence_sha256,
                        "reason": settlement_reason,
                    },
                )
            )
        if settlement_change_on is not None:
            settlement_events.append(
                (
                    settlement_change_on,
                    "T0",
                    {
                        "known_at": date(2021, 7, 21),
                        "source": (
                            "513000 日经225 ETF 2021-07-26 更新招募说明书；"
                            "上海证券交易所公开公告"
                        ),
                        "evidence_ids": (
                            "SSE_513000_SETTLEMENT_NOTICE_20210721",
                            "SSE_513000_PROSPECTUS_20210726",
                        ),
                        "evidence_sha256": _file_sha256(
                            official_dir
                            / "crossborder_2021_prospectuses"
                            / "513000_prospectus_2021.pdf"
                        ),
                        "reason": "fund_specific_secondary_T0_change",
                    },
                )
            )
        settlement_events.sort(key=lambda item: item[0])
        settlement_rows = _merge_intervals(
            settlement_events,
            required_start,
            required_end,
        )

        limit_events = [
            (
                required_start,
                "0.20"
                if is_twenty_percent_fund(
                    f"{product.current.title} "
                    f"{product.listing_document.title if product.listing_document else ''}"
                )
                and (
                    record.list_date >= date(2020, 8, 24)
                    or bool(
                        re.search(
                            r"科创板|科创50|科创100|科创200|科创综指",
                            f"{product.current.title} "
                            f"{product.listing_document.title if product.listing_document else ''}",
                        )
                    )
                )
                else "0.10",
                {
                    "known_at": required_start,
                    "source": "交易所基金价格涨跌幅规则及产品上市文件",
                    "evidence_ids": (
                        product.listing_document.document_id
                        if product.listing_document is not None
                        else product.baseline.document_id,
                    ),
                    "evidence_sha256": None,
                },
            ),
            *_limit_events(product, record),
        ]
        limit_events.sort(key=lambda item: item[0])
        limit_rows = _merge_intervals(
            limit_events,
            required_start,
            required_end,
        )

        boundaries = {
            required_start,
            *(
                date.fromisoformat(row["effective_from"])
                for row in settlement_rows
            ),
            *(
                date.fromisoformat(row["effective_from"])
                for row in limit_rows
            ),
        }
        for boundary in sorted(boundaries):
            if boundary < required_start or boundary > required_end:
                continue
            next_boundary = min(
                (
                    value
                    for value in boundaries
                    if value > boundary
                ),
                default=required_end + timedelta(days=1),
            )
            settlement_row = next(
                row
                for row in settlement_rows
                if date.fromisoformat(row["effective_from"])
                <= boundary
                <= date.fromisoformat(row["effective_to"])
            )
            limit_row = next(
                row
                for row in limit_rows
                if date.fromisoformat(row["effective_from"])
                <= boundary
                <= date.fromisoformat(row["effective_to"])
            )
            effective_to = min(required_end, next_boundary - timedelta(days=1))
            evidence_ids = list(
                dict.fromkeys(
                    [
                        *settlement_row.get("evidence_refs", []),
                        *limit_row.get("evidence_refs", []),
                    ]
                )
            )
            hashes = [
                value
                for value in (
                    settlement_row.get("evidence_sha256"),
                    limit_row.get("evidence_sha256"),
                )
                if value
            ]
            record_row = {
                "code": code,
                "settlement": settlement_row["settlement"],
                "effective_from": boundary.isoformat(),
                "effective_to": effective_to.isoformat(),
                "limit_pct": limit_row["settlement"],
                "trading_status": "ACTIVE",
                "known_at": max(
                    date.fromisoformat(settlement_row["known_at"]),
                    date.fromisoformat(limit_row["known_at"]),
                ).isoformat(),
                "source": (
                    f"{settlement_row['source']}; "
                    f"{limit_row['source']}"
                ),
                "evidence_id": ";".join(evidence_ids),
                "evidence_sha256": hashes[0] if hashes else None,
                "evidence_refs": evidence_ids,
                "product_category": product.category,
                "settlement_reason": settlement_reason,
            }
            if date.fromisoformat(str(record_row["known_at"])) > boundary:
                record_row["known_at"] = boundary.isoformat()
            all_rows.append(record_row)

    all_rows.sort(
        key=lambda row: (
            str(row["code"]),
            str(row["effective_from"]),
            str(row["effective_to"]),
        )
    )
    audit = audit_trading_rules(
        master,
        all_rows,
        start_date=start_date,
        end_date=end_date,
    )
    return all_rows, evidence, audit


def build_evidence_review(
    evidence: Mapping[str, ProductEvidence],
    rules: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = list(rules)
    last_by_code: dict[str, dict[str, Any]] = {}
    for row in rows:
        last_by_code[str(row["code"])] = row
    output = []
    for code, product in sorted(evidence.items()):
        row = last_by_code.get(code, {})
        listing_id = (
            product.listing_document.document_id
            if product.listing_document is not None
            else ""
        )
        output.append(
            {
                "code": code,
                "category": product.category,
                "baseline_document_id": product.baseline.document_id,
                "baseline_date": product.baseline.published_on.isoformat(),
                "baseline_settlement_hints": list(
                    product.baseline_settlement_hints
                ),
                "current_document_id": product.current.document_id,
                "current_date": product.current.published_on.isoformat(),
                "current_settlement_hints": list(
                    product.current_settlement_hints
                ),
                "listing_document_id": listing_id,
                "settlement": row.get("settlement"),
                "limit_pct": row.get("limit_pct"),
                "resolution_reason": row.get("settlement_reason"),
            }
        )
    return output


def _write_review_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    records = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "code",
        "category",
        "baseline_document_id",
        "baseline_date",
        "baseline_settlement_hints",
        "current_document_id",
        "current_date",
        "current_settlement_hints",
        "listing_document_id",
        "settlement",
        "limit_pct",
        "resolution_reason",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    key: (
                        "|".join(value)
                        if isinstance(value, list)
                        else value
                    )
                    for key, value in record.items()
                }
            )


def write_evidence_file_hashes(official_dir: Path) -> Path:
    output = official_dir / "files.sha256.json"
    rows = []
    for path in sorted(official_dir.rglob("*")):
        if (
            not path.is_file()
            or path == output
            or path.name in {"catalog.json", "resolution_catalog.json"}
        ):
            continue
        rows.append(
            {
                "file": path.relative_to(official_dir).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _file_sha256(path),
            }
        )
    write_json_atomic(output, rows)
    return output


def build_rule_evidence_resolutions(
    root: Path,
    config: dict[str, Any],
    evidence: Mapping[str, ProductEvidence],
    rules: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    root = root.resolve()
    module_root = root if (root / "data/m1_2").exists() else root / "M1"
    trading_cache = module_root / "data/m1_2/cache/trading_rules"
    initial_gap_path = (
        module_root / "reports/m1_2/trading_rule_evidence_gaps_initial_54.csv"
    )
    gap_path = (
        initial_gap_path
        if initial_gap_path.exists()
        else module_root / "reports/m1_2/trading_rule_evidence_gaps.csv"
    )
    with gap_path.open("r", encoding="utf-8-sig", newline="") as handle:
        gaps = list(csv.DictReader(handle))

    rules_by_code: dict[str, list[dict[str, Any]]] = {}
    for row in rules:
        rules_by_code.setdefault(str(row["code"]), []).append(row)
    for rows in rules_by_code.values():
        rows.sort(key=lambda item: str(item["effective_from"]))

    change_notices: dict[str, list[dict[str, Any]]] = {}
    change_path = trading_cache / "change_notice_index.ndjson"
    for row in read_ndjson(change_path):
        change_notices.setdefault(str(row["code"]), []).append(row)

    announcement_root = trading_cache / "announcements" / "merged"
    resolutions: list[dict[str, Any]] = []
    for gap in gaps:
        code = str(gap["code"])
        issue = str(gap["issue"])
        product = evidence[code]
        product_rules = rules_by_code[code]
        settlement_intervals = []
        for row in product_rules:
            item = (
                str(row["effective_from"]),
                str(row["effective_to"]),
                str(row["settlement"]),
            )
            if not settlement_intervals or settlement_intervals[-1][2] != item[2]:
                settlement_intervals.append(item)
            else:
                settlement_intervals[-1] = (
                    settlement_intervals[-1][0],
                    item[1],
                    item[2],
                )
        limit_changes = [
            row
            for index, row in enumerate(product_rules)
            if index > 0
            and row["limit_pct"] != product_rules[index - 1]["limit_pct"]
        ]
        settlement_changes = [
            row
            for index, row in enumerate(product_rules)
            if index > 0
            and row["settlement"] != product_rules[index - 1]["settlement"]
        ]
        notices = sorted(
            change_notices.get(code, []),
            key=lambda item: str(item.get("publish_date", "")),
        )
        notice_ids = [str(item["id"]) for item in notices]

        if issue == "missing_current_text":
            current_text = (
                str(product.current.text_path)
                if product.current.text_path is not None
                and product.current.text_path.exists()
                else ""
            )
            resolutions.append(
                {
                    "code": code,
                    "issue": issue,
                    "review_status": "resolved_equivalent_current_text",
                    "decision": (
                        "The official report date and readable equivalent "
                        "document identify the 2019 prospectus as current."
                    ),
                    "document_id": product.current.document_id,
                    "document_date": product.current.published_on.isoformat(),
                    "text_path": current_text,
                    "evidence_ids": [
                        product.current.document_id,
                        product.baseline.document_id,
                    ],
                }
            )
            continue

        if issue == "missing_baseline_text":
            resolutions.append(
                {
                    "code": code,
                    "issue": issue,
                    "review_status": "resolved_equivalent_baseline_text",
                    "decision": (
                        "Exchange-hosted 2017 prospectus update supplies a "
                        "readable pre-2018 equivalent baseline."
                    ),
                    "document_id": product.baseline.document_id,
                    "document_date": product.baseline.published_on.isoformat(),
                    "text_path": (
                        str(product.baseline.text_path)
                        if product.baseline.text_path is not None
                        else ""
                    ),
                    "evidence_ids": [
                        product.baseline.document_id,
                        "SSE_512210_20170901_2",
                    ],
                }
            )
            continue

        if issue == "rule_change_interval_requires_review":
            if settlement_changes:
                change = settlement_changes[-1]
                status = "resolved_fund_specific_settlement_interval"
                decision = (
                    "Updated product text establishes the T0 interval; "
                    "earlier intervals retain the then-effective exchange rule."
                )
                evidence_ids = [
                    *notice_ids,
                    "SSE_513000_SETTLEMENT_NOTICE_20210721",
                    "SSE_513000_PROSPECTUS_20210726",
                ]
            else:
                change = limit_changes[-1] if limit_changes else None
                status = "resolved_no_secondary_market_field_change"
                decision = (
                    "The change notice concerns subscription/redemption "
                    "settlement, not active secondary-market selling or its "
                    "price-limit field."
                    if change is None
                    else
                    "The change notice does not alter active-selling settlement; "
                    "the separately sourced price-limit transition remains."
                )
                evidence_ids = [
                    product.baseline.document_id,
                    product.current.document_id,
                    *notice_ids,
                ]
            resolutions.append(
                {
                    "code": code,
                    "issue": issue,
                    "review_status": status,
                    "decision": decision,
                    "settlement_intervals": settlement_intervals,
                    "effective_from": (
                        str(change["effective_from"]) if change is not None else None
                    ),
                    "limit_change_from": (
                        f"{product_rules[product_rules.index(change) - 1]['limit_pct']}"
                        " -> "
                        f"{change['limit_pct']}"
                        if change is not None
                        and change["limit_pct"] != product_rules[
                            product_rules.index(change) - 1
                        ]["limit_pct"]
                        else None
                    ),
                    "notices": notices,
                    "evidence_ids": list(dict.fromkeys(evidence_ids)),
                }
            )
            continue

        if issue == "pre_2021_sse_crossborder_requires_explicit_interval":
            july_2021 = [
                row
                for row in read_json(
                    announcement_root / f"{code}.json",
                    {},
                ).get("announcements", [])
                if date(2021, 6, 1)
                <= (_parse_date(row.get("PUBLISHDATE")) or date.min)
                <= date(2021, 10, 31)
                and re.search(
                    r"招募说明书|结算模式|基金合同|修订",
                    str(row.get("TITLE", "")),
                )
            ]
            resolutions.append(
                {
                    "code": code,
                    "issue": issue,
                    "review_status": "resolved_explicit_crossborder_interval",
                    "decision": (
                        "Product text and the applicable exchange rule determine "
                        "the active-sale interval; the 2021 primary-subscription "
                        "settlement notices are not treated as an automatic "
                        "secondary-market rule change."
                    ),
                    "settlement_intervals": settlement_intervals,
                    "baseline_hints": list(product.baseline_settlement_hints),
                    "current_hints": list(product.current_settlement_hints),
                    "announcement_ids_2021": [
                        str(row.get("ID", "")) for row in july_2021
                    ],
                    "evidence_ids": list(
                        dict.fromkeys(
                            [
                                product.baseline.document_id,
                                product.current.document_id,
                                *(
                                    str(row.get("ID", ""))
                                    for row in july_2021
                                ),
                            ]
                        )
                    ),
                }
            )
            continue

        resolutions.append(
            {
                "code": code,
                "issue": issue,
                "review_status": "resolved_by_candidate_audit",
                "settlement_intervals": settlement_intervals,
                "evidence_ids": [
                    product.baseline.document_id,
                    product.current.document_id,
                ],
            }
        )
    return resolutions


def audit_rule_evidence_references(
    root: Path,
    config: dict[str, Any],
    evidence: Mapping[str, ProductEvidence],
    rules: Iterable[dict[str, Any]],
    resolutions: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    root = root.resolve()
    module_root = root if (root / "data/m1_2").exists() else root / "M1"
    trading_cache = module_root / "data/m1_2/cache/trading_rules"
    catalog = read_json(
        trading_cache / "official_rules" / "resolution_catalog.json",
        {},
    )
    known_ids = {
        str(row.get("evidence_id"))
        for row in catalog.get("rules", [])
        if row.get("evidence_id")
    }
    for product in evidence.values():
        known_ids.add(product.baseline.document_id)
        known_ids.add(product.current.document_id)
        if product.listing_document is not None:
            known_ids.add(product.listing_document.document_id)
    known_ids.update(
        str(row["document_id"])
        for row in read_ndjson(
            trading_cache / "rule_evidence_supplements.ndjson"
        )
    )
    known_ids.update(
        str(row["id"])
        for row in read_ndjson(trading_cache / "change_notice_index.ndjson")
    )
    announcement_root = trading_cache / "announcements" / "merged"
    for path in announcement_root.glob("*.json"):
        payload = read_json(path, {})
        known_ids.update(
            str(row.get("ID"))
            for row in payload.get("announcements", [])
            if row.get("ID")
        )
    known_ids.add("SSE_513000_SETTLEMENT_NOTICE_20210721")
    known_ids.add("SSE_513000_PROSPECTUS_20210726")

    missing = []
    checked = 0
    for row in rules:
        for evidence_id in row.get("evidence_refs", []):
            checked += 1
            if str(evidence_id) not in known_ids:
                missing.append(
                    {
                        "code": str(row["code"]),
                        "kind": "rule",
                        "evidence_id": str(evidence_id),
                    }
                )
    for row in resolutions:
        for evidence_id in row.get("evidence_ids", []):
            checked += 1
            if str(evidence_id) not in known_ids:
                missing.append(
                    {
                        "code": str(row["code"]),
                        "kind": "resolution",
                        "evidence_id": str(evidence_id),
                    }
                )
    return {
        "status": "READY" if not missing else "BLOCKED",
        "known_evidence_ids": len(known_ids),
        "checked_references": checked,
        "missing_reference_count": len(missing),
        "missing_references": missing,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build candidate dated ETF trading rules from cached evidence"
    )
    parser.add_argument("--config", default="config.m1_2.yaml")
    parser.add_argument(
        "--output",
        default="data/m1_2/cache/trading_rules/trading_rules.candidate.ndjson",
    )
    parser.add_argument(
        "--review",
        default="reports/m1_2/trading_rule_evidence_review.json",
    )
    parser.add_argument(
        "--review-csv",
        default="reports/m1_2/trading_rule_evidence_review.csv",
    )
    parser.add_argument("--write-candidate", action="store_true")
    parser.add_argument(
        "--write-resolutions",
        action="store_true",
        help="write the 54-item evidence review resolution ledger",
    )
    parser.add_argument(
        "--write-evidence-hashes",
        action="store_true",
        help="refresh hashes for the archived official rule and notice files",
    )
    args = parser.parse_args(argv)
    root = Path(".")
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    rules, evidence, audit = build_trading_rule_candidates(root, config)
    review = build_evidence_review(evidence, rules)
    resolutions = build_rule_evidence_resolutions(root, config, evidence, rules)
    evidence_ref_audit = audit_rule_evidence_references(
        root,
        config,
        evidence,
        rules,
        resolutions,
    )
    report = {
        "status": (
            "CANDIDATE_READY"
            if audit.ready and evidence_ref_audit["status"] == "READY"
            else "BLOCKED"
        ),
        "master_codes": audit.master_codes,
        "rule_rows": audit.rule_rows,
        "valid_rule_rows": audit.valid_rule_rows,
        "complete_codes": audit.complete_codes,
        "incomplete_codes": len(audit.incomplete_codes),
        "issue_counts": audit.issue_counts,
        "category_counts": {
            category: sum(
                item["category"] == category for item in review
            )
            for category in sorted({item["category"] for item in review})
        },
        "settlement_counts": {
            value: sum(item["settlement"] == value for item in review)
            for value in ("T0", "T1")
        },
        "limit_counts": {
            value: sum(item["limit_pct"] == value for item in review)
            for value in ("0.10", "0.20")
        },
        "candidate_path": args.output,
        "candidate_written": bool(args.write_candidate),
        "evidence_resolutions": len(resolutions),
        "evidence_resolutions_written": bool(args.write_resolutions),
        "evidence_reference_audit": evidence_ref_audit,
        "formal_rules_written": False,
    }
    write_json_atomic(Path(args.review), {**report, "codes": review})
    _write_review_csv(Path(args.review_csv), review)
    if args.write_candidate:
        write_ndjson_atomic(Path(args.output), rules)
    if args.write_resolutions:
        write_ndjson_atomic(
            Path("data/m1_2/cache/trading_rules/rule_evidence_resolutions.ndjson"),
            resolutions,
        )
    if args.write_evidence_hashes:
        report["evidence_hash_manifest"] = str(
            write_evidence_file_hashes(
                Path("data/m1_2/cache/trading_rules/official_rules")
            )
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return (
        0
        if audit.ready and evidence_ref_audit["status"] == "READY"
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
