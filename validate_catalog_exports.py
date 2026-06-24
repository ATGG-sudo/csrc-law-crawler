#!/usr/bin/env python3
"""Validate canonical catalog normalized and Markdown coverage."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from config import OUTPUT_DIR
from export_markdown_catalog import CATALOG_MARKDOWN_MANIFEST
from normalize_catalog import CATALOG_NORMALIZED_MANIFEST
from storage import (
    catalog_laws_dir,
    catalog_markdown_dir,
    catalog_normalized_dir,
    load_json,
)


def validate_catalog_exports() -> tuple[list[str], dict[str, Any]]:
    issues: list[str] = []
    catalog_files = sorted(catalog_laws_dir().glob("law_*.json"))
    normalized_files = sorted(catalog_normalized_dir().glob("law_*.json"))
    markdown_files = sorted(catalog_markdown_dir().glob("*/*.md"))

    catalog_ids = {path.stem for path in catalog_files}
    normalized_ids: set[str] = set()
    empty_content = 0
    metadata_only = 0
    for path in normalized_files:
        doc = load_json(path, {})
        entity_id = str(doc.get("id") or "")
        normalized_ids.add(entity_id)
        if entity_id != path.stem:
            issues.append(f"{path.name}: filename/id mismatch")
        if not str(doc.get("full_text_plain") or "").strip():
            empty_content += 1
        if doc.get("content_status") == "metadata_only":
            metadata_only += 1
        if not doc.get("sources"):
            issues.append(f"{path.name}: missing sources")

    if catalog_ids != normalized_ids:
        issues.append(
            "catalog/normalized ID coverage mismatch: "
            f"missing={len(catalog_ids - normalized_ids)} "
            f"extra={len(normalized_ids - catalog_ids)}"
        )

    normalized_manifest = load_json(CATALOG_NORMALIZED_MANIFEST, {})
    if normalized_manifest.get("count") != len(normalized_files):
        issues.append("catalog normalized manifest count mismatch")

    markdown_manifest = load_json(CATALOG_MARKDOWN_MANIFEST, {})
    markdown_items = markdown_manifest.get("items") or []
    markdown_ids = {str(item.get("id") or "") for item in markdown_items}
    if markdown_ids != catalog_ids:
        issues.append(
            "catalog Markdown ID coverage mismatch: "
            f"missing={len(catalog_ids - markdown_ids)} "
            f"extra={len(markdown_ids - catalog_ids)}"
        )
    if markdown_manifest.get("count") != len(markdown_files):
        issues.append("catalog Markdown manifest/file count mismatch")

    manifest_paths: set[Path] = set()
    for item in markdown_items:
        relative = item.get("file")
        if not relative:
            issues.append(f"Markdown manifest {item.get('id')}: missing file")
            continue
        path = OUTPUT_DIR / str(relative)
        if path in manifest_paths:
            issues.append(f"duplicate Markdown path: {relative}")
        manifest_paths.add(path)
        if not path.exists():
            issues.append(f"missing Markdown file: {relative}")

    summary = {
        "catalog_laws": len(catalog_files),
        "normalized_laws": len(normalized_files),
        "markdown_files": len(markdown_files),
        "normalized_empty_content": empty_content,
        "metadata_only": metadata_only,
        "current_markdown": len(
            list((catalog_markdown_dir() / "current").glob("*.md"))
        ),
        "other_markdown": len(
            list((catalog_markdown_dir() / "other").glob("*.md"))
        ),
        "issues": len(issues),
    }
    return issues, summary


def main() -> int:
    issues, summary = validate_catalog_exports()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if issues:
        print("\n问题:")
        for issue in issues[:100]:
            print(f"  - {issue}")
        return 1
    print("\n统一目录 normalized/Markdown 校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
