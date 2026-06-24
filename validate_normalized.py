#!/usr/bin/env python3
"""Validate normalized laws and downloaded asset manifests."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from config import OUTPUT_DIR
from download_assets import ASSETS_MANIFEST
from normalize_laws import normalized_laws_dir, normalized_manifest_path
from storage import laws_dir, load_json

HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9:_-]*(?:\s[^<>]*)?>")


def _has_html_tag(text: str) -> bool:
    return bool(HTML_TAG_RE.search(text or ""))


def validate_normalized(*, sample: int = 5) -> tuple[list[str], dict[str, Any]]:
    issues: list[str] = []
    raw_files = sorted(laws_dir().glob("reg_*.json"))
    normalized_files = sorted(normalized_laws_dir().glob("reg_*.json"))

    if len(raw_files) != len(normalized_files):
        issues.append(f"normalized 文件数 {len(normalized_files)} != raw laws {len(raw_files)}")

    total_tables = 0
    total_assets = 0
    html_in_plain: list[str] = []
    html_in_markdown: list[str] = []
    missing_source: list[str] = []
    missing_full_text: list[str] = []
    pending_assets = 0
    ok_assets = 0
    failed_assets = 0
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
            status = asset.get("download_status")
            if status == "ok":
                ok_assets += 1
                local_file = asset.get("local_file")
                if not local_file or not (OUTPUT_DIR / local_file).exists():
                    missing_local_files.append((path.name, asset.get("asset_id") or ""))
            elif status == "failed":
                failed_assets += 1
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

    assets_manifest = load_json(ASSETS_MANIFEST, {})
    if assets_manifest and assets_manifest.get("failed", 0) != failed_assets:
        issues.append(
            f"assets_manifest failed={assets_manifest.get('failed')} != normalized failed={failed_assets}"
        )

    summary = {
        "raw_laws": len(raw_files),
        "normalized_laws": len(normalized_files),
        "tables": total_tables,
        "assets": total_assets,
        "assets_ok": ok_assets,
        "assets_pending": pending_assets,
        "assets_failed": failed_assets,
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
    parser = argparse.ArgumentParser(description="校验 normalized/laws 和 assets 结果")
    parser.add_argument("--sample", type=int, default=5, help="每类问题最多展示 N 个样本")
    args = parser.parse_args()

    issues, summary = validate_normalized(sample=args.sample)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if issues:
        print("\n问题:")
        for issue in issues:
            print(f"  - {issue}")
        return 1
    print("\nnormalized 校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
