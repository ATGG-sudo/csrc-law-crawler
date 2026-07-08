#!/usr/bin/env python3
"""Export normalized law JSON files to Markdown documents."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

from markdown_utils import (
    asset_map as _asset_map,
    assets_section as _assets_section,
    clean_table_value as _clean_table_value,
    filename_stem as _filename_stem,
    replace_asset_links as _replace_asset_links,
    strip_leading_title as _strip_leading_title,
    yaml_scalar as _yaml_scalar,
)
from normalize_laws import normalized_laws_dir, normalized_manifest_path
from runtime import log_event
from storage import (
    listed_output_files,
    load_json,
    relative_to_output,
    run_with_output_lock,
    save_json,
    utc_now_iso,
    work_dir,
)

def markdown_root() -> Path:
    return work_dir() / "markdown_neris"


def markdown_laws_dir() -> Path:
    return markdown_root() / "laws"


def markdown_current_dir() -> Path:
    return markdown_laws_dir() / "current"


def markdown_other_dir() -> Path:
    return markdown_laws_dir() / "other"


def markdown_manifest_path() -> Path:
    return markdown_root() / "manifest.json"


def _target_dir(metadata: dict[str, Any]) -> Path:
    if metadata.get("status") == "现行有效":
        return markdown_current_dir()
    return markdown_other_dir()


def _target_path(
    metadata: dict[str, Any],
    law_id: str,
    used_paths: set[Path],
    *,
    allow_existing: bool,
) -> Path:
    target_dir = _target_dir(metadata)
    stem = _filename_stem(metadata, law_id)
    candidate = target_dir / f"{stem}.md"
    if candidate not in used_paths and (allow_existing or not candidate.exists()):
        used_paths.add(candidate)
        return candidate
    suffix = law_id[:8]
    candidate = target_dir / f"{stem} - {suffix}.md"
    counter = 2
    while candidate in used_paths or candidate.exists():
        candidate = target_dir / f"{stem} - {suffix}-{counter}.md"
        counter += 1
    used_paths.add(candidate)
    return candidate


def _front_matter(metadata: dict[str, Any], doc: dict[str, Any]) -> str:
    keys = {
        "id": metadata.get("id"),
        "title": metadata.get("name"),
        "number": metadata.get("number"),
        "fileno": metadata.get("fileno"),
        "pub_org": metadata.get("pub_org"),
        "pub_date": metadata.get("pub_date"),
        "effective_date": metadata.get("effective_date"),
        "status": metadata.get("status"),
        "version": metadata.get("version"),
        "source_file": doc.get("source_file"),
    }
    lines = ["---"]
    for key, value in keys.items():
        lines.append(f"{key}: {_yaml_scalar(value)}")
    revision_ref = doc.get("revision_ref") or {}
    if revision_ref:
        lines.append(f"revision_ref: {_yaml_scalar(revision_ref.get('family_id'))}")
    lines.append("---")
    return "\n".join(lines)


def _metadata_table(metadata: dict[str, Any]) -> str:
    rows = [
        ("法规 ID", metadata.get("id")),
        ("规则编号", metadata.get("number")),
        ("文号", metadata.get("fileno")),
        ("发布机构", metadata.get("pub_org")),
        ("发布日期", metadata.get("pub_date")),
        ("施行日期", metadata.get("effective_date")),
        ("效力状态", metadata.get("status")),
        ("版本", metadata.get("version")),
    ]
    lines = ["| 字段 | 值 |", "| --- | --- |"]
    for key, value in rows:
        lines.append(f"| {key} | {_clean_table_value(value)} |")
    return "\n".join(lines)


def build_markdown(doc: dict[str, Any], markdown_path: Path) -> str:
    metadata = doc.get("metadata") or {}
    title = metadata.get("name") or metadata.get("id") or markdown_path.stem
    assets = _asset_map(doc, markdown_path)
    body = _strip_leading_title(doc.get("full_text_markdown") or "", title)
    body = _replace_asset_links(body, assets)

    parts = [
        _front_matter(metadata, doc),
        f"# {title}",
        _metadata_table(metadata),
    ]
    if body:
        parts.append(body)
    assets_section = _assets_section(assets)
    if assets_section:
        parts.append(assets_section)
    return "\n\n".join(parts).rstrip() + "\n"


def export_markdown(
    *,
    limit: int | None = None,
    force: bool = False,
    clean: bool = False,
) -> dict[str, Any]:
    if not normalized_laws_dir().exists():
        raise FileNotFoundError(
            "work/normalized_neris/laws 不存在，请先运行 python normalize_laws.py"
        )

    if clean and markdown_laws_dir().exists():
        shutil.rmtree(markdown_laws_dir())
    markdown_current_dir().mkdir(parents=True, exist_ok=True)
    markdown_other_dir().mkdir(parents=True, exist_ok=True)
    normalized_files = listed_output_files(
        normalized_manifest_path(),
        field="file",
        fallback_dir=normalized_laws_dir(),
        pattern="reg_*.json",
        limit=limit,
    )

    manifest_items: list[dict[str, Any]] = []
    used_paths: set[Path] = set()
    written = 0
    skipped = 0
    current_count = 0
    other_count = 0

    for index, path in enumerate(normalized_files, start=1):
        doc = load_json(path, {})
        metadata = doc.get("metadata") or {}
        law_id = str(metadata.get("id") or path.stem.removeprefix("reg_"))
        out_path = _target_path(metadata, law_id, used_paths, allow_existing=force)
        bucket = "current" if out_path.parent == markdown_current_dir() else "other"
        if bucket == "current":
            current_count += 1
        else:
            other_count += 1
        if out_path.exists() and not force:
            skipped += 1
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(build_markdown(doc, out_path), encoding="utf-8")
            written += 1
        manifest_items.append(
            {
                "id": law_id,
                "name": metadata.get("name"),
                "fileno": metadata.get("fileno"),
                "effective_date": metadata.get("effective_date"),
                "status": metadata.get("status"),
                "bucket": bucket,
                "source_file": relative_to_output(path),
                "file": relative_to_output(out_path),
                "tables": len(doc.get("tables") or []),
                "assets": len(doc.get("assets") or []),
            }
        )
        if index % 100 == 0 or index == len(normalized_files):
            log_event(
                "export_progress",
                message=f"  exported {index}/{len(normalized_files)}",
                index=index,
                total=len(normalized_files),
            )

    manifest = {
        "updated_at": utc_now_iso(),
        "source_dir": relative_to_output(normalized_laws_dir()),
        "markdown_dir": relative_to_output(markdown_laws_dir()),
        "count": len(manifest_items),
        "current_count": current_count,
        "other_count": other_count,
        "written": written,
        "skipped": skipped,
        "filename_pattern": "title - fileno - effective_date.md",
        "items": manifest_items,
    }
    save_json(markdown_manifest_path(), manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将 work/normalized_neris/laws 导出为中间 Markdown"
    )
    parser.add_argument("--limit", type=int, default=None, help="仅导出前 N 个法规")
    parser.add_argument("--force", action="store_true", help="覆盖已有 Markdown 文件")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="导出前清空 work/markdown_neris/laws",
    )
    args = parser.parse_args()

    try:
        manifest = export_markdown(limit=args.limit, force=args.force, clean=args.clean)
    except KeyboardInterrupt:
        log_event("cli_interrupted", level="ERROR", message="已中断")
        return 130
    except Exception as exc:
        log_event("cli_error", level="ERROR", message=f"失败: {exc}", error_message=str(exc))
        return 1

    log_event(
        "cli_result",
        message=(
            "完成: "
            f"count={manifest['count']} written={manifest['written']} "
            f"skipped={manifest['skipped']} current={manifest['current_count']} "
            f"other={manifest['other_count']} -> {markdown_manifest_path()}"
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "export-markdown-laws"))
