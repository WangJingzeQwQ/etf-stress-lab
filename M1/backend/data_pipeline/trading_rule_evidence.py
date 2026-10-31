from __future__ import annotations

from datetime import date
from hashlib import sha256
import argparse
import csv
import json
from pathlib import Path
import re
from typing import Any, Iterable

import yaml

from backend.data_pipeline.fetch_master import load_master_records
from backend.data_pipeline.storage import (
    read_json,
    write_json_atomic,
    write_ndjson_atomic,
)


_FULL_PROSPECTUS = re.compile(r"招募说明书")
_EXCERPT_SUMMARY = re.compile(r"摘要|内容摘要")
_LISTING_TITLE = re.compile(r"上市交易公告书|上市交易提示")
_RULE_CHANGE_TITLE = re.compile(
    r"结算模式|交易方式|回转交易|T\+0|T＋0|涨跌幅|交易机制|交易规则"
)
_T0_PATTERNS = (
    re.compile(
        r"当日买入(?:的)?(?:基金)?份额[^。；]{0,30}"
        r"(?:同日|当日)\s*(?:可以|可)\s*(?:赎回或)?卖出"
    ),
    re.compile(
        r"T\s*日买入[^。；]{0,100}(?:同日|当日)(?:可以|可)(?:赎回或)?卖出"
    ),
    re.compile(r"当日买入的份额[，,]?\s*当日可卖出"),
)
_T1_PATTERNS = (
    re.compile(
        r"当日买入(?:的)?(?:基金)?份额[^。；]{0,40}"
        r"(?:同日|当日)\s*(?:可以)?\s*赎回\s*[，,]?\s*(?:但)?不得卖出"
    ),
    re.compile(r"T\s*日买入[^。；]{0,120}不得卖出"),
    re.compile(r"当日买入[^。；]{0,100}次日(?:可以|可)卖出"),
)
_LIMIT_PATTERNS = (
    re.compile(
        r"基金实行价格涨跌幅限制[，,]\s*涨跌幅比例为\s*"
        r"(\d+(?:\.\d+)?)\s*%"
    ),
    re.compile(
        r"本基金竞价交易(?:也)?实行\s*"
        r"(\d+(?:\.\d+)?)\s*%\s*涨跌幅(?:比例)?(?:限制)?"
    ),
    re.compile(
        r"本基金(?:的)?(?:竞价交易)?涨跌幅限制为\s*"
        r"(\d+(?:\.\d+)?)\s*%"
    ),
)
_EFFECTIVE_DATE = re.compile(
    r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*起"
)


def announcement_cache_root(root: Path, config: dict[str, Any]) -> Path:
    configured = config.get("trading_rule_evidence", {}).get("cache_root")
    return root / str(
        configured or "data/m1_2/cache/trading_rules/announcements"
    )


def merge_announcement_pages(
    root: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    master = load_master_records(root / str(config["data_pipeline"]["master_file"]))
    cache_root = announcement_cache_root(root, config)
    merged_root = cache_root / "merged"
    merged_root.mkdir(parents=True, exist_ok=True)
    page_directories = sorted(
        path
        for path in cache_root.glob("page*")
        if path.is_dir()
    )
    report_rows = []
    failures = []
    for code in sorted(master):
        rows_by_id: dict[str, dict[str, Any]] = {}
        pages = []
        announced_total = 0
        for page_dir in page_directories:
            payload = read_json(page_dir / f"{code}.json")
            if not isinstance(payload, dict):
                continue
            if payload.get("ErrCode") != 0:
                failures.append(
                    {
                        "code": code,
                        "page": page_dir.name,
                        "error": payload.get("ErrMsg"),
                    }
                )
                continue
            pages.append(page_dir.name)
            announced_total = max(
                announced_total, int(payload.get("TotalCount", 0))
            )
            for row in payload.get("Data", []):
                announcement_id = str(row.get("ID", "")).strip()
                if not announcement_id:
                    continue
                rows_by_id[announcement_id] = {
                    **row,
                    "source_page": page_dir.name,
                }
        rows = sorted(
            rows_by_id.values(),
            key=lambda item: (
                str(item.get("PUBLISHDATE", "")),
                str(item.get("ID", "")),
            ),
            reverse=True,
        )
        write_json_atomic(
            merged_root / f"{code}.json",
            {
                "code": code,
                "total_announced": announced_total,
                "unique_rows_retrieved": len(rows),
                "pages": pages,
                "announcements": rows,
            },
        )
        report_rows.append(
            {
                "code": code,
                "total_announced": announced_total,
                "unique_rows_retrieved": len(rows),
                "complete": len(rows) >= announced_total,
                "pages": pages,
            }
        )
    report = {
        "status": (
            "COMPLETE"
            if report_rows
            and all(row["complete"] for row in report_rows)
            and not failures
            else "PARTIAL"
        ),
        "codes": len(master),
        "codes_with_announcements": sum(
            row["unique_rows_retrieved"] > 0 for row in report_rows
        ),
        "codes_complete": sum(row["complete"] for row in report_rows),
        "announcements_retrieved": sum(
            row["unique_rows_retrieved"] for row in report_rows
        ),
        "announcements_announced": sum(
            row["total_announced"] for row in report_rows
        ),
        "failures": failures,
        "incomplete_codes": [
            row["code"] for row in report_rows if not row["complete"]
        ],
    }
    report_path = cache_root.parent / "announcement_merge_report.json"
    write_json_atomic(report_path, report)
    return {**report, "report": str(report_path)}


def _announcement_date(row: dict[str, Any]) -> date:
    return date.fromisoformat(str(row["PUBLISHDATE"])[:10])


def select_rule_documents(
    announcements: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    rows = list(announcements)
    selected: dict[str, dict[str, Any]] = {}
    reasons: dict[str, set[str]] = {}

    def add(row: dict[str, Any], reason: str) -> None:
        announcement_id = str(row.get("ID", "")).strip()
        if not announcement_id:
            return
        selected[announcement_id] = row
        reasons.setdefault(announcement_id, set()).add(reason)

    full_prospectuses = [
        row
        for row in rows
        if _FULL_PROSPECTUS.search(str(row.get("TITLE", "")))
        and not _EXCERPT_SUMMARY.search(str(row.get("TITLE", "")))
    ]
    full_prospectuses.sort(key=_announcement_date)
    if full_prospectuses:
        add(full_prospectuses[0], "earliest_full_prospectus")
        add(full_prospectuses[-1], "latest_full_prospectus")

    for row in rows:
        title = str(row.get("TITLE", ""))
        if _LISTING_TITLE.search(title):
            add(row, "listing_trading_document")
        if _RULE_CHANGE_TITLE.search(title):
            add(row, "rule_change_title")

    output = []
    for announcement_id, row in selected.items():
        output.append(
            {
                "id": announcement_id,
                "code": str(row.get("FUNDCODE", "")),
                "title": str(row.get("TITLE", "")),
                "publish_date": str(row.get("PUBLISHDATE", ""))[:10],
                "selection_reasons": sorted(reasons.get(announcement_id, set())),
            }
        )
    output.sort(key=lambda item: (item["publish_date"], item["id"]))
    return output, sorted(
        reason
        for values in reasons.values()
        for reason in values
    )


def prepare_pdf_downloads(
    root: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    root = root.resolve()
    master = load_master_records(root / str(config["data_pipeline"]["master_file"]))
    cache_root = announcement_cache_root(root, config)
    merged_root = cache_root / "merged"
    trading_cache = cache_root.parent
    pdf_dir = trading_cache / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    missing_indexes = []
    for code in sorted(master):
        payload = read_json(merged_root / f"{code}.json")
        if not isinstance(payload, dict):
            missing_indexes.append(code)
            continue
        selected, _ = select_rule_documents(payload.get("announcements", []))
        for item in selected:
            if item["code"] and item["code"] != code:
                raise ValueError(
                    f"Announcement code mismatch for {item['id']}: "
                    f"{item['code']} != {code}"
                )
            rows.append({**item, "code": code})

    config_lines: list[str] = []
    for index, row in enumerate(rows):
        announcement_id = row["id"]
        output_path = (pdf_dir / f"{announcement_id}.pdf").as_posix()
        config_lines.extend(
            (
                f'url = "https://pdf.dfcfw.com/pdf/H2_{announcement_id}_1.pdf"',
                f'output = "{output_path}"',
                'header = "User-Agent: Mozilla/5.0"',
            )
        )
        if index + 1 < len(rows):
            config_lines.append("next")
    curl_config = trading_cache / "rule_documents.curl.conf"
    curl_config.write_text("\n".join(config_lines) + "\n", encoding="utf-8")
    selection_path = trading_cache / "rule_document_selection.ndjson"
    write_ndjson_atomic(selection_path, rows)
    report = {
        "status": "READY_FOR_DOWNLOAD" if not missing_indexes else "PARTIAL",
        "codes": len(master),
        "selected_documents": len(rows),
        "codes_with_documents": len({row["code"] for row in rows}),
        "missing_announcement_indexes": missing_indexes,
        "selection": str(selection_path),
        "curl_config": str(curl_config),
        "pdf_directory": str(pdf_dir),
    }
    report_path = trading_cache / "rule_document_selection_report.json"
    write_json_atomic(report_path, report)
    return {**report, "report": str(report_path)}


def _normalise_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()


def _excerpt(text: str, start: int, end: int, radius: int = 320) -> str:
    return text[max(0, start - radius) : min(len(text), end + radius)]


def _effective_dates_near(text: str, start: int, end: int) -> list[str]:
    window = _excerpt(text, start, end, radius=500)
    values = []
    for match in _EFFECTIVE_DATE.finditer(window):
        try:
            values.append(
                date(
                    int(match.group(1)),
                    int(match.group(2)),
                    int(match.group(3)),
                ).isoformat()
            )
        except ValueError:
            continue
    return sorted(set(values))


def extract_pdf_evidence(pdf_path: Path) -> dict[str, Any]:
    from pypdf import PdfReader

    raw = pdf_path.read_bytes()
    reader = PdfReader(pdf_path)
    pages = [(page.extract_text() or "") for page in reader.pages]
    result = extract_text_evidence("\n".join(pages))
    return {
        "document_id": pdf_path.stem,
        "pdf_sha256": f"sha256:{sha256(raw).hexdigest()}",
        "page_count": len(pages),
        **result,
    }


def extract_text_evidence(text: str) -> dict[str, Any]:
    text = _normalise_text(text)
    settlement_hints = []
    for kind, patterns in (("T0", _T0_PATTERNS), ("T1", _T1_PATTERNS)):
        for pattern in patterns:
            for match in pattern.finditer(text):
                settlement_hints.append(
                    {
                        "settlement": kind,
                        "excerpt": _excerpt(text, match.start(), match.end()),
                        "effective_dates_near_match": _effective_dates_near(
                            text, match.start(), match.end()
                        ),
                    }
                )
    limit_hints = []
    for pattern in _LIMIT_PATTERNS:
        for match in pattern.finditer(text):
            limit_hints.append(
                {
                    "limit_pct": f"{float(match.group(1)) / 100:.4f}",
                    "quoted_percent": match.group(1),
                    "excerpt": _excerpt(text, match.start(), match.end()),
                    "effective_dates_near_match": _effective_dates_near(
                        text, match.start(), match.end()
                    ),
                }
            )
    unique_settlement = {
        (item["settlement"], item["excerpt"]): item
        for item in settlement_hints
    }
    unique_limits = {
        (item["limit_pct"], item["excerpt"]): item for item in limit_hints
    }
    return {
        "content_sha256": f"sha256:{sha256(text.encode('utf-8')).hexdigest()}",
        "text_characters": len(text),
        "settlement_hints": list(unique_settlement.values()),
        "limit_hints": list(unique_limits.values()),
        "extraction_status": "TEXT_EXTRACTED" if text else "EMPTY_TEXT",
    }


def extract_selected_documents(
    root: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    trading_cache = announcement_cache_root(root, config).parent
    selection_path = trading_cache / "rule_document_selection.ndjson"
    from backend.data_pipeline.storage import read_ndjson

    selection = list(
        {
            str(row["id"]): row
            for row in read_ndjson(selection_path)
        }.values()
    )
    extracted_root = trading_cache / "extracted"
    extracted_root.mkdir(parents=True, exist_ok=True)
    results = []
    failures = []
    for row in selection:
        document_id = str(row["id"])
        pdf_path = trading_cache / "pdfs" / f"{document_id}.pdf"
        if not pdf_path.exists():
            failures.append(
                {"id": document_id, "code": row["code"], "error": "missing_pdf"}
            )
            continue
        try:
            result = extract_pdf_evidence(pdf_path)
        except Exception as exc:
            failures.append(
                {
                    "id": document_id,
                    "code": row["code"],
                    "error": str(exc),
                }
            )
            continue
        result.update(
            {
                "code": row["code"],
                "title": row["title"],
                "publish_date": row["publish_date"],
                "selection_reasons": row["selection_reasons"],
            }
        )
        write_json_atomic(extracted_root / f"{document_id}.json", result)
        results.append(result)
    report = {
        "status": "EXTRACTED" if not failures else "PARTIAL",
        "selected_documents": len(selection),
        "extracted_documents": len(results),
        "documents_with_settlement_hint": sum(
            bool(item["settlement_hints"]) for item in results
        ),
        "documents_with_limit_hint": sum(
            bool(item["limit_hints"]) for item in results
        ),
        "failure_count": len(failures),
        "failures": failures,
    }
    report_path = trading_cache / "rule_document_extraction_report.json"
    write_json_atomic(report_path, report)
    return {**report, "report": str(report_path)}


def prepare_content_downloads(
    root: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    root = root.resolve()
    trading_cache = announcement_cache_root(root, config).parent
    selection_path = trading_cache / "rule_document_selection.ndjson"
    from backend.data_pipeline.storage import read_ndjson

    selection = _stage_one_content_selection(read_ndjson(selection_path))
    content_dir = trading_cache / "contents"
    content_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for index, row in enumerate(selection):
        announcement_id = str(row["id"])
        output_path = (content_dir / f"{announcement_id}.json").as_posix()
        lines.extend(
            (
                'url = "https://np-cnotice-fund.eastmoney.com/api/content/ann'
                f'?client_source=web_fund&show_all=1&art_code={announcement_id}"',
                f'output = "{output_path}"',
                'header = "User-Agent: Mozilla/5.0"',
                'header = "Referer: https://fund.eastmoney.com/gonggao/'
                f'{row["code"]},{announcement_id}.html"',
            )
        )
        if index + 1 < len(selection):
            lines.append("next")
    config_path = trading_cache / "announcement_contents.curl.conf"
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = {
        "status": "READY_FOR_DOWNLOAD",
        "selected_documents": len(selection),
        "curl_config": str(config_path),
        "content_directory": str(content_dir),
    }
    report_path = trading_cache / "announcement_content_selection.json"
    write_json_atomic(report_path, report)
    return {**report, "report": str(report_path)}


def _stage_one_content_selection(
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    selected = {}
    for row in rows:
        reasons = set(row.get("selection_reasons", []))
        if (
            "latest_full_prospectus" in reasons
            or "rule_change_title" in reasons
            or (
                "earliest_full_prospectus" in reasons
                and str(row.get("publish_date", "")) < "2018-01-01"
            )
        ):
            selected[str(row["id"])] = row
    return list(selected.values())


def extract_downloaded_contents(
    root: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    root = root.resolve()
    trading_cache = announcement_cache_root(root, config).parent
    selection_path = trading_cache / "rule_document_selection.ndjson"
    content_dir = trading_cache / "contents"
    extracted_root = trading_cache / "extracted"
    extracted_root.mkdir(parents=True, exist_ok=True)
    from backend.data_pipeline.storage import read_ndjson

    selection = _stage_one_content_selection(read_ndjson(selection_path))
    results = []
    failures = []
    for row in selection:
        announcement_id = str(row["id"])
        content_path = content_dir / f"{announcement_id}.json"
        payload = read_json(content_path)
        if not isinstance(payload, dict) or payload.get("success") != 1:
            failures.append(
                {
                    "id": announcement_id,
                    "code": row["code"],
                    "error": "missing_or_invalid_detail_response",
                }
            )
            continue
        data = payload.get("data") or {}
        text = str(data.get("notice_content") or "")
        if not text.strip():
            failures.append(
                {
                    "id": announcement_id,
                    "code": row["code"],
                    "error": "empty_notice_content",
                }
            )
            continue
        evidence = extract_text_evidence(text)
        evidence.update(
            {
                "document_id": announcement_id,
                "code": row["code"],
                "title": row["title"],
                "publish_date": row["publish_date"],
                "selection_reasons": row["selection_reasons"],
                "notice_title": data.get("notice_title"),
                "notice_date": data.get("notice_date"),
                "attach_url": data.get("attach_url_web") or data.get("attach_url"),
                "security": data.get("security", []),
            }
        )
        write_json_atomic(extracted_root / f"{announcement_id}.json", evidence)
        results.append(evidence)
    report = {
        "status": "EXTRACTED" if not failures else "PARTIAL",
        "selected_documents": len(selection),
        "extracted_documents": len(results),
        "documents_with_settlement_hint": sum(
            bool(item["settlement_hints"]) for item in results
        ),
        "documents_with_limit_hint": sum(
            bool(item["limit_hints"]) for item in results
        ),
        "failure_count": len(failures),
        "failures": failures,
    }
    report_path = trading_cache / "announcement_content_extraction_report.json"
    write_json_atomic(report_path, report)
    return {**report, "report": str(report_path)}


def prepare_official_documents(
    root: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    root = root.resolve()
    master = load_master_records(root / str(config["data_pipeline"]["master_file"]))
    trading_cache = announcement_cache_root(root, config).parent
    index_root = trading_cache / "csrc" / "by_code"
    pdf_dir = trading_cache / "csrc" / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    rows_by_code: dict[str, dict[int, dict[str, Any]]] = {}
    for code in master:
        by_id: dict[int, dict[str, Any]] = {}
        for path in sorted(index_root.glob(f"{code}.page*.json")):
            payload = read_json(path)
            if not isinstance(payload, dict):
                continue
            for row in payload.get("aaData", []):
                if str(row.get("fundCode", "")).strip() != code:
                    continue
                if str(row.get("reportCode", "")) not in {
                    "FA010010",
                    "FA010030",
                }:
                    continue
                report_name = str(row.get("reportName", ""))
                short_name = str(row.get("fundShortName", ""))
                if "交易型开放式" not in report_name and "ETF" not in (
                    report_name + short_name
                ):
                    continue
                upload_info_id = int(row["uploadInfoId"])
                by_id[upload_info_id] = row
        rows_by_code[code] = by_id

    selected: list[dict[str, Any]] = []
    missing_codes = []
    for code in sorted(master):
        rows = sorted(
            rows_by_code.get(code, {}).values(),
            key=lambda item: (
                str(item.get("uploadDate", "")),
                int(item.get("uploadInfoId", 0)),
            ),
            reverse=True,
        )
        if not rows:
            missing_codes.append(code)
            continue
        latest = rows[0]
        baseline_candidates = [
            row
            for row in rows
            if str(row.get("uploadDate", "")) <= "2018-01-02"
        ]
        baseline = baseline_candidates[0] if baseline_candidates else rows[-1]
        for reason, row in (
            ("latest_official_prospectus", latest),
            ("baseline_official_prospectus", baseline),
        ):
            selected.append(
                {
                    **row,
                    "code": code,
                    "selection_reason": reason,
                    "official_pdf_url": (
                        "http://eid.csrc.gov.cn/fund/disclose/"
                        "instance_show_pdf_id.do"
                        f"?instanceid={row['uploadInfoId']}"
                    ),
                }
            )
    selected_by_code_and_id = {
        (str(row["code"]), int(row["uploadInfoId"])): row
        for row in selected
    }
    selected = sorted(
        selected_by_code_and_id.values(),
        key=lambda row: (str(row["code"]), str(row["uploadDate"])),
    )
    selection_path = trading_cache / "rule_document_selection.ndjson"
    write_ndjson_atomic(selection_path, selected)
    download_rows = {
        int(row["uploadInfoId"]): row
        for row in selected
    }
    lines = []
    ordered_downloads = list(download_rows.values())
    for index, row in enumerate(ordered_downloads):
        output_path = (
            pdf_dir / f"{int(row['uploadInfoId'])}.pdf"
        ).as_posix()
        lines.extend(
            (
                f'url = "{row["official_pdf_url"]}"',
                f'output = "{output_path}"',
                'header = "User-Agent: Mozilla/5.0"',
                'header = "Referer: http://eid.csrc.gov.cn/fund/disclose/"',
            )
        )
        if index + 1 < len(ordered_downloads):
            lines.append("next")
    config_path = trading_cache / "csrc_official_pdfs.curl.conf"
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = {
        "status": "READY_FOR_DOWNLOAD" if not missing_codes else "PARTIAL",
        "master_codes": len(master),
        "selected_documents": len(selected),
        "unique_pdf_documents": len(download_rows),
        "codes_with_selected_documents": len(
            {str(row["code"]) for row in selected}
        ),
        "missing_codes": missing_codes,
        "selection": str(selection_path),
        "curl_config": str(config_path),
        "pdf_directory": str(pdf_dir),
    }
    report_path = trading_cache / "csrc_official_document_report.json"
    write_json_atomic(report_path, report)
    return {**report, "report": str(report_path)}


def extract_official_texts(
    root: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    root = root.resolve()
    trading_cache = announcement_cache_root(root, config).parent
    selection_path = trading_cache / "rule_document_selection.ndjson"
    text_root = trading_cache / "csrc" / "texts"
    extracted_root = trading_cache / "csrc" / "extracted"
    extracted_root.mkdir(parents=True, exist_ok=True)
    from backend.data_pipeline.storage import read_ndjson

    selection = read_ndjson(selection_path)
    results = []
    failures = []
    for row in selection:
        upload_info_id = int(row["uploadInfoId"])
        text_path = text_root / f"{upload_info_id}.txt"
        if not text_path.exists():
            failures.append(
                {
                    "document_id": str(upload_info_id),
                    "code": row["code"],
                    "error": "missing_extracted_text",
                }
            )
            continue
        text = text_path.read_text(encoding="utf-8", errors="replace")
        evidence = extract_text_evidence(text)
        evidence.update(
            {
                "document_id": str(upload_info_id),
                "code": row["code"],
                "title": row["reportName"],
                "publish_date": row["uploadDate"],
                "selection_reason": row["selection_reason"],
                "source": "CSRC_FUND_DISCLOSURE",
                "source_url": row["official_pdf_url"],
                "fund_name": row.get("fundShortName"),
            }
        )
        write_json_atomic(
            extracted_root / f"{upload_info_id}.json",
            evidence,
        )
        results.append(evidence)
    report = {
        "status": "EXTRACTED" if not failures else "PARTIAL",
        "selected_documents": len(selection),
        "extracted_documents": len(results),
        "documents_with_settlement_hint": sum(
            bool(item["settlement_hints"]) for item in results
        ),
        "documents_with_limit_hint": sum(
            bool(item["limit_hints"]) for item in results
        ),
        "failure_count": len(failures),
        "failures": failures,
    }
    report_path = trading_cache / "csrc_official_evidence_report.json"
    write_json_atomic(report_path, report)
    return {**report, "report": str(report_path)}


def prepare_change_notices(
    root: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    root = root.resolve()
    trading_cache = announcement_cache_root(root, config).parent
    change_index_path = trading_cache / "change_notice_index.ndjson"
    from backend.data_pipeline.storage import read_ndjson

    change_index = read_ndjson(change_index_path)
    selected = []
    misses = []
    for index, change in enumerate(change_index):
        index_path = trading_cache / "csrc" / "change_notices" / f"{index:03d}.json"
        payload = read_json(index_path)
        if not isinstance(payload, dict):
            misses.append(change)
            continue
        target_date = date.fromisoformat(str(change["publish_date"]))
        candidates = []
        for row in payload.get("aaData", []):
            if str(row.get("fundCode", "")) != str(change["code"]):
                continue
            row_date = date.fromisoformat(str(row["uploadDate"]))
            difference = abs((row_date - target_date).days)
            title = str(row.get("reportName", ""))
            if difference <= 12 and any(
                token in title
                for token in ("结算", "修改招募说明书", "修订基金合同")
            ):
                candidates.append((difference, title, row))
        if not candidates:
            misses.append(change)
            continue
        difference, title, row = min(candidates, key=lambda item: item[:2])
        selected.append(
            {
                **row,
                "source_code": str(change["code"]),
                "source_eastmoney_id": str(change["id"]),
                "source_date": str(change["publish_date"]),
                "source_title": str(change["title"]),
                "match_days": difference,
            }
        )
    selection_path = trading_cache / "csrc_changes_selected.ndjson"
    write_ndjson_atomic(selection_path, selected)
    pdf_dir = trading_cache / "csrc" / "change_pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for index, row in enumerate(selected):
        upload_info_id = int(row["uploadInfoId"])
        output_path = (pdf_dir / f"{upload_info_id}.pdf").as_posix()
        lines.extend(
            (
                "url = \"http://eid.csrc.gov.cn/fund/disclose/"
                f"instance_show_pdf_id.do?instanceid={upload_info_id}\"",
                f'output = "{output_path}"',
                'header = "User-Agent: Mozilla/5.0"',
            )
        )
        if index + 1 < len(selected):
            lines.append("next")
    config_path = trading_cache / "csrc_change_pdfs.curl.conf"
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = {
        "status": "READY_FOR_DOWNLOAD" if not misses else "PARTIAL",
        "change_notices": len(change_index),
        "official_matches": len(selected),
        "unmatched": misses,
        "selection": str(selection_path),
        "curl_config": str(config_path),
        "pdf_directory": str(pdf_dir),
    }
    report_path = trading_cache / "csrc_change_notice_report.json"
    write_json_atomic(report_path, report)
    return {**report, "report": str(report_path)}


def classify_etf_product(title: str, text: str = "") -> str:
    # Issuer names can contain words such as "全球" without describing the
    # fund's investment scope.  Do not let that issuer-name fragment turn a
    # domestic equity ETF into a cross-border product.
    normalised_title = title.replace("兴证全球", "")
    if re.search(
        r"债券|国债|公司债|信用债|城投债|地方政府债|地方债|可转债|"
        r"可交换债|转债ETF|债ETF|债交易型",
        normalised_title,
    ):
        return "bond"
    if re.search(r"货币市场|交易型货币|保证金交易", normalised_title):
        return "money"
    if (
        re.search(r"黄金交易型开放式|黄金ETF|黄金基金|上海金", normalised_title)
        and not re.search(r"黄金股|黄金产业|黄金股票", normalised_title)
    ):
        return "gold"
    if re.search(
        r"商品期货|期货交易型开放式|期货ETF|豆粕|能源化工|有色(?:金属)?期货",
        normalised_title,
    ):
        return "commodity"
    if any(
        token in normalised_title
        for token in (
            "QDII",
            "跨境",
            "港股",
            "恒生",
            "纳斯达克",
            "日经",
            "东证",
            "日本",
            "美国",
            "香港",
            "海外",
            "全球",
            "亚太",
            "德国",
            "法国",
            "DAX",
            "CAC",
            "沙特",
            "东南亚",
            "中概",
            "H股",
            "HK",
            "巴西",
            "印度",
            "越南",
            "TOPIX",
            "日经",
            "纳指",
        )
    ) or (
        "标普" in normalised_title
        and "中国A股" not in normalised_title
        and "沪深" not in normalised_title
    ):
        return "crossborder"
    return "stock"


def is_twenty_percent_fund(title: str) -> bool:
    return any(
        token in title
        for token in (
            "科创板",
            "科创50",
            "科创100",
            "科创200",
            "科创综指",
            "科创创业",
            "双创",
            "创业板",
            "创50",
        )
    )


def summarize_official_rule_evidence(
    root: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    root = root.resolve()
    master = load_master_records(root / str(config["data_pipeline"]["master_file"]))
    trading_cache = announcement_cache_root(root, config).parent
    from backend.data_pipeline.storage import read_ndjson

    source_rows = []
    official_path = trading_cache / "rule_document_selection.ndjson"
    if official_path.exists():
        for row in read_ndjson(official_path):
            source_rows.append(
                {
                    "code": str(row["code"]),
                    "document_id": str(row["uploadInfoId"]),
                    "title": str(row["reportName"]),
                    # reportSendDate is the public report date. uploadDate is
                    # merely when the CSRC disclosure site received the scan;
                    # conflating the two makes an old 2017 prospectus look
                    # newer than the 2019 replacement for 511230.
                    "publish_date": str(
                        row.get("reportSendDate") or row["uploadDate"]
                    ),
                    "source": "CSRC_FUND_DISCLOSURE",
                    "source_url": str(row["official_pdf_url"]),
                    "text_path": trading_cache
                    / "csrc"
                    / "texts"
                    / f"{row['uploadInfoId']}.txt",
                }
            )
    fallback_path = trading_cache / "eastmoney_fallback_selection.ndjson"
    if fallback_path.exists():
        for row in read_ndjson(fallback_path):
            source_rows.append(
                {
                    "code": str(row["code"]),
                    "document_id": str(row["id"]),
                    "title": str(row["title"]),
                    "publish_date": str(row["publish_date"]),
                    "source": "EASTMONEY_PUBLIC_ANNOUNCEMENT",
                    "source_url": (
                        "https://pdf.dfcfw.com/pdf/"
                        f"H2_{row['id']}_1.pdf"
                    ),
                    "text_path": trading_cache
                    / "eastmoney_fallback"
                    / "texts"
                    / f"{row['id']}.txt",
                }
            )
    supplements_path = trading_cache / "rule_evidence_supplements.ndjson"
    if supplements_path.exists():
        for row in read_ndjson(supplements_path):
            source_rows.append(
                {
                    "code": str(row["code"]),
                    "document_id": str(row["document_id"]),
                    "title": str(row["title"]),
                    "publish_date": str(row["publish_date"]),
                    "source": str(row["source"]),
                    "source_url": str(row.get("source_url", "")),
                    "text_path": root / str(row["text_path"]),
                }
            )
    by_code: dict[str, list[dict[str, Any]]] = {}
    for row in source_rows:
        by_code.setdefault(row["code"], []).append(row)

    change_codes = {
        str(row["code"])
        for row in (
            read_ndjson(trading_cache / "change_notice_index.ndjson")
            if (trading_cache / "change_notice_index.ndjson").exists()
            else []
        )
    }
    categories: dict[str, int] = {}
    current_documents = 0
    baseline_documents = 0
    current_texts = 0
    baseline_texts = 0
    twenty_percent_candidates = []
    unresolved = []

    def has_text(row: dict[str, Any]) -> bool:
        path = Path(row["text_path"])
        return path.exists() and path.stat().st_size > 0

    for code in sorted(master):
        rows = sorted(
            by_code.get(code, []),
            key=lambda item: (item["publish_date"], item["document_id"]),
        )
        if not rows:
            unresolved.append({"code": code, "issue": "no_prospectus_index"})
            continue
        latest = max(
            rows,
            key=lambda item: (
                item["publish_date"],
                has_text(item),
                item["document_id"],
            ),
        )
        current_documents += 1
        latest_text_path = latest["text_path"]
        latest_text = (
            latest_text_path.read_text(encoding="utf-8", errors="replace")
            if has_text(latest)
            else ""
        )
        if latest_text:
            current_texts += 1
        else:
            unresolved.append({"code": code, "issue": "missing_current_text"})
        baseline_rows = [
            row for row in rows if row["publish_date"] <= "2018-01-02"
        ]
        readable_baselines = [row for row in baseline_rows if has_text(row)]
        baseline = (
            max(
                readable_baselines,
                key=lambda item: (item["publish_date"], item["document_id"]),
            )
            if readable_baselines
            else max(
                baseline_rows,
                key=lambda item: (item["publish_date"], item["document_id"]),
            )
            if baseline_rows
            else min(
                rows,
                key=lambda item: (
                    item["publish_date"],
                    not has_text(item),
                    item["document_id"],
                ),
            )
        )
        baseline_documents += 1
        if has_text(baseline):
            baseline_texts += 1
        else:
            unresolved.append({"code": code, "issue": "missing_baseline_text"})
        category = classify_etf_product(latest["title"], latest_text)
        categories[category] = categories.get(category, 0) + 1
        if is_twenty_percent_fund(latest["title"]):
            twenty_percent_candidates.append(code)
        if code in change_codes:
            unresolved.append(
                {
                    "code": code,
                    "issue": "rule_change_interval_requires_review",
                }
            )
        elif (
            category == "crossborder"
            and code.startswith("5")
            and master[code].list_date < date(2021, 7, 26)
        ):
            unresolved.append(
                {
                    "code": code,
                    "issue": "pre_2021_sse_crossborder_requires_explicit_interval",
                }
            )
    resolutions_path = trading_cache / "rule_evidence_resolutions.ndjson"
    resolutions = {
        (str(row["code"]), str(row["issue"])): row
        for row in (
            read_ndjson(resolutions_path) if resolutions_path.exists() else []
        )
    }
    unresolved = [
        item
        for item in unresolved
        if (str(item["code"]), str(item["issue"])) not in resolutions
    ]
    report = {
        "status": "EVIDENCE_INDEXED" if not unresolved else "PARTIAL",
        "formal_research_ready": False,
        "master_codes": len(master),
        "codes_with_current_document": current_documents,
        "codes_with_baseline_document": baseline_documents,
        "codes_with_current_extracted_text": current_texts,
        "codes_with_baseline_extracted_text": baseline_texts,
        "category_counts": dict(sorted(categories.items())),
        "twenty_percent_candidate_count": len(twenty_percent_candidates),
        "twenty_percent_candidates": twenty_percent_candidates,
        "unresolved_issue_count": len(unresolved),
        "unresolved": unresolved,
        "reviewed_issue_count": len(resolutions),
        "reviewed_resolutions": sum(
            str(row.get("review_status", "")).startswith("resolved")
            for row in resolutions.values()
        ),
        "notes": [
            "Exchange rule documents are indexed separately from code product evidence.",
            "Category classification is not treated as a formal rule without exchange rule history.",
            "This report does not create or approve trading_rules.ndjson.",
        ],
    }
    report_path = trading_cache / "rule_evidence_coverage.json"
    write_json_atomic(report_path, report)
    gaps_path = root / "reports/m1_2/trading_rule_evidence_gaps.csv"
    gaps_path.parent.mkdir(parents=True, exist_ok=True)
    with gaps_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "code",
                "name",
                "list_date",
                "issue",
                "review_status",
                "evidence_ids",
            ),
        )
        writer.writeheader()
        for item in unresolved:
            record = master[str(item["code"])]
            resolution = resolutions.get(
                (str(item["code"]), str(item["issue"])),
                {},
            )
            writer.writerow(
                {
                    "code": record.code,
                    "name": record.name_history[0].name,
                    "list_date": record.list_date.isoformat(),
                    "issue": item["issue"],
                    "review_status": resolution.get("review_status", ""),
                    "evidence_ids": ";".join(
                        str(value)
                        for value in resolution.get("evidence_ids", [])
                    ),
                }
            )
    return {**report, "report": str(report_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge announcement indexes and extract trading-rule evidence"
    )
    parser.add_argument(
        "stage",
        choices=(
            "merge-indexes",
            "prepare-pdfs",
            "extract-pdfs",
            "prepare-contents",
            "extract-contents",
            "prepare-official",
            "extract-official",
            "prepare-changes",
            "summarize-evidence",
        ),
    )
    parser.add_argument("--config", default="config.m1_2.yaml")
    args = parser.parse_args(argv)
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    if args.stage == "merge-indexes":
        result = merge_announcement_pages(Path("."), config)
    elif args.stage == "prepare-pdfs":
        result = prepare_pdf_downloads(Path("."), config)
    elif args.stage == "extract-pdfs":
        result = extract_selected_documents(Path("."), config)
    elif args.stage == "prepare-contents":
        result = prepare_content_downloads(Path("."), config)
    elif args.stage == "extract-contents":
        result = extract_downloaded_contents(Path("."), config)
    elif args.stage == "prepare-official":
        result = prepare_official_documents(Path("."), config)
    elif args.stage == "extract-official":
        result = extract_official_texts(Path("."), config)
    elif args.stage == "prepare-changes":
        result = prepare_change_notices(Path("."), config)
    else:
        result = summarize_official_rule_evidence(Path("."), config)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] not in {"PARTIAL"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
