#!/usr/bin/env python3
"""Validate normalized laws and downloaded asset manifests."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from download_assets import assets_manifest_path
from models import format_model_issues
from normalize_laws import normalized_laws_dir, normalized_manifest_path
from runtime import log_event
from storage import iter_reg_law_files, listed_output_files, load_json, output_path, run_with_context

HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9:_-]*(?:\s[^<>]*)?>")


def _has_html_tag(text: str) -> bool:
    return bool(HTML_TAG_RE.search(text or ""))


def _normalized_law_files():
    return listed_output_files(
        normalized_manifest_path(),
        field="file",
        fallback_dir=normalized_laws_dir(),
        pattern="reg_*.json",
    )


def validate_normalized(*, sample: int = 5) -> tuple[list[str], dict[str, Any]]:
    issues: list[str] = []
    raw_files = iter_reg_law_files()
    normalized_files = _normalized_law_files()

    if len(raw_files) != len(normalized_files):
        issues.append(f"normalized 文件数 {len(normalized_files)} != raw laws {len(raw_files)}")

    for path in raw_files:
        issues.extend(format_model_issues("law_document", path.name, load_json(path, {})))

    total_tables = 0
    total_assets = 0
    html_in_plain: list[str] = []
    html_in_markdown: list[str] = []
    missing_source: list[str] = []
    missing_full_text: list[str] = []
    pending_assets = 0
    ok_assets = 0
    failed_assets = 0
    embedded_failed_assets = 0
    source_attachment_failed_assets = 0
    missing_local_files: list[tuple[str, str]] = []

    for path in normalized_files:
        doc = load_json(path, {})
        if not doc.get("source_file"):
            missing_source.append(path.name)
        if not (doc.get("full_text_plain") or "").strip():
            missing_full_text.append(path.name)
        if _has_html_tag(doc.get("full_text_plain") or ""):
            html_in_plain.append(path.name)
        if _has_html_tag(doc.get("full_text_markdown") or ""):
            html_in_markdown.append(path.name)

        total_tables += len(doc.get("tables") or [])
        total_assets += len(doc.get("assets") or [])
        for asset in doc.get("assets") or []:
            issues.extend(
                format_model_issues(
                    "asset_record",
                    f"{path.name}:{asset.get('asset_id') or 'asset'}",
                    asset,
                )
            )
            status = asset.get("download_status")
            if status == "ok":
                ok_assets += 1
                local_file = asset.get("local_file")
                if not local_file or not output_path(local_file).exists():
                    missing_local_files.append((path.name, asset.get("asset_id") or ""))
            elif status == "failed":
                failed_assets += 1
                if asset.get("source_attachment_id"):
                    source_attachment_failed_assets += 1
                else:
                    embedded_failed_assets += 1
            else:
                pending_assets += 1

    if html_in_plain:
        issues.append(f"full_text_plain 仍有 HTML-like 标签 {len(html_in_plain)} 个")
    if missing_source:
        issues.append(f"缺 source_file {len(missing_source)} 个")
    if missing_full_text:
        issues.append(f"full_text_plain 为空 {len(missing_full_text)} 个")
    if missing_local_files:
        issues.append(f"download_status=ok 但本地资产缺失 {len(missing_local_files)} 个")

    manifest = load_json(normalized_manifest_path(), {})
    if manifest and manifest.get("count") != len(normalized_files):
        issues.append(
            f"normalized manifest count={manifest.get('count')} != files={len(normalized_files)}"
        )

    assets_manifest = load_json(assets_manifest_path(), {})
    if (
        assets_manifest
        and assets_manifest.get("failed", 0) != embedded_failed_assets
    ):
        issues.append(
            "assets_manifest failed="
            f"{assets_manifest.get('failed')} != normalized embedded failed="
            f"{embedded_failed_assets}"
        )

    summary = {
        "raw_laws": len(raw_files),
        "normalized_laws": len(normalized_files),
        "tables": total_tables,
        "assets": total_assets,
        "assets_ok": ok_assets,
        "assets_pending": pending_assets,
        "assets_failed": failed_assets,
        "embedded_assets_failed": embedded_failed_assets,
        "source_attachments_failed": source_attachment_failed_assets,
        "html_in_plain": len(html_in_plain),
        "html_in_markdown": len(html_in_markdown),
        "missing_source": len(missing_source),
        "missing_full_text": len(missing_full_text),
        "missing_local_files": len(missing_local_files),
        "samples": {
            "html_in_plain": html_in_plain[:sample],
            "html_in_markdown": html_in_markdown[:sample],
            "missing_local_files": missing_local_files[:sample],
        },
    }
    return issues, summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="校验 work/normalized_neris/laws 和资产结果"
    )
    parser.add_argument("--sample", type=int, default=5, help="每类问题最多展示 N 个样本")
    args = parser.parse_args()

    issues, summary = validate_normalized(sample=args.sample)
    log_event("validation_summary", message=json.dumps(summary, ensure_ascii=False, indent=2))
    if issues:
        log_event("validation_issues", level="ERROR", message="\n问题:")
        for issue in issues:
            log_event("validation_issue", level="ERROR", message=f"  - {issue}", issue=issue)
        return 1
    log_event("validation_passed", message="\nnormalized 校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(run_with_context(main, "validate-normalized"))
