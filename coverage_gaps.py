#!/usr/bin/env python3
"""Detect likely source, attachment, download, and parsing coverage gaps."""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from typing import Any

from normalize_laws import normalized_laws_dir
from storage import (
    attachment_index_path,
    coverage_gaps_path,
    laws_dir,
    load_json,
    save_json,
    utc_now_iso,
)

QUOTED_TITLE_RE = re.compile(r"《([^》]{4,100})》")
SERIES_RE = re.compile(r"^(.*?(?:指引|规则|准则))第?(\d+)号")
ANNOUNCEMENT_WORDS = ("关于发布", "现予发布", "公告", "通知")
RULE_WORDS = ("办法", "规则", "指引", "准则", "细则", "规定")


def _finding(
    law_id: str,
    name: str,
    gap_type: str,
    reason: str,
    *,
    severity: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "law_id": law_id,
        "name": name,
        "gap_type": gap_type,
        "severity": severity,
        "reason": reason,
        "evidence": evidence or {},
        "status": "open",
    }


def detect_coverage_gaps(*, limit: int | None = None) -> dict[str, Any]:
    raw_paths = sorted(laws_dir().glob("reg_*.json"))
    if limit is not None:
        raw_paths = raw_paths[:limit]
    findings: list[dict[str, Any]] = []
    series: dict[str, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))

    for path in raw_paths:
        doc = load_json(path, {})
        metadata = doc.get("metadata") or {}
        law_id = str(metadata.get("id") or path.stem.removeprefix("reg_"))
        name = str(metadata.get("name") or law_id)
        full_text = str(doc.get("full_text") or "")
        attachment_index = load_json(attachment_index_path(law_id), {})
        if attachment_index:
            source_attachments = attachment_index.get("attachments") or []
            attachments_checked = True
        else:
            source_attachments = doc.get("source_attachments")
            attachments_checked = source_attachments is not None
        quoted_titles = [
            title.strip()
            for title in QUOTED_TITLE_RE.findall(name)
            if any(word in title for word in RULE_WORDS)
        ]

        is_announcement = any(word in name for word in ANNOUNCEMENT_WORDS)
        if is_announcement and quoted_titles and len(full_text) < 2000:
            if not attachments_checked:
                findings.append(
                    _finding(
                        law_id,
                        name,
                        "attachment_not_crawled",
                        "发布公告正文较短且尚未查询 NERIS 独立附件接口",
                        severity="high",
                        evidence={
                            "full_text_length": len(full_text),
                            "published_titles": quoted_titles,
                        },
                    )
                )
            elif not source_attachments:
                findings.append(
                    _finding(
                        law_id,
                        name,
                        "source_missing",
                        "发布公告提及制度文件，但 NERIS 正文和附件接口均无正文附件",
                        severity="high",
                        evidence={
                            "full_text_length": len(full_text),
                            "published_titles": quoted_titles,
                        },
                    )
                )

        for attachment in source_attachments or []:
            status = attachment.get("download_status")
            if status == "failed":
                findings.append(
                    _finding(
                        law_id,
                        name,
                        "download_failed",
                        "NERIS 独立附件下载失败",
                        severity="medium",
                        evidence={
                            "attachment_id": attachment.get("attachment_id"),
                            "attachment_name": attachment.get("name"),
                            "error": attachment.get("download_error"),
                        },
                    )
                )
            elif status != "ok":
                findings.append(
                    _finding(
                        law_id,
                        name,
                        "attachment_not_downloaded",
                        "已发现 NERIS 独立附件但尚未下载",
                        severity="medium",
                        evidence={
                            "attachment_id": attachment.get("attachment_id"),
                            "attachment_name": attachment.get("name"),
                        },
                    )
                )

        normalized_path = normalized_laws_dir() / path.name
        if normalized_path.exists():
            normalized = load_json(normalized_path, {})
            for asset in normalized.get("assets") or []:
                if asset.get("source_attachment_id"):
                    continue
                if asset.get("download_status") == "failed":
                    findings.append(
                        _finding(
                            law_id,
                            name,
                            "download_failed",
                            "正文内嵌资产下载失败",
                            severity="medium",
                            evidence={
                                "asset_id": asset.get("asset_id"),
                                "source_url": asset.get("source_url"),
                                "error": asset.get("download_error"),
                            },
                        )
                    )
            if full_text.strip() and not (
                normalized.get("full_text_plain") or ""
            ).strip():
                findings.append(
                    _finding(
                        law_id,
                        name,
                        "parse_failed",
                        "原始正文存在但清洗正文为空",
                        severity="high",
                    )
                )

        match = SERIES_RE.search(name)
        if match:
            series[match.group(1).strip()][int(match.group(2))].append(law_id)

    for prefix, members in sorted(series.items()):
        numbers = sorted(members)
        if len(numbers) < 2:
            continue
        missing = sorted(set(range(numbers[0], numbers[-1] + 1)) - set(numbers))
        if missing:
            findings.append(
                {
                    "law_id": None,
                    "name": prefix,
                    "gap_type": "series_gap",
                    "severity": "medium",
                    "reason": "NERIS 中同系列编号不连续",
                    "evidence": {
                        "present_numbers": numbers,
                        "missing_numbers": missing,
                        "member_law_ids": members,
                    },
                    "status": "manual_review",
                }
            )

    counts: dict[str, int] = defaultdict(int)
    for item in findings:
        counts[str(item["gap_type"])] += 1
    result = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "scanned_laws": len(raw_paths),
        "count": len(findings),
        "counts_by_type": dict(sorted(counts.items())),
        "items": findings,
    }
    save_json(coverage_gaps_path(), result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="识别法规正文和附件覆盖缺口")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    try:
        result = detect_coverage_gaps(limit=args.limit)
    except Exception as exc:
        print(f"失败: {exc}", file=sys.stderr)
        return 1
    print(
        f"完成: scanned={result['scanned_laws']} gaps={result['count']} "
        f"types={result['counts_by_type']} -> {coverage_gaps_path()}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
