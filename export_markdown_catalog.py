#!/usr/bin/env python3
"""Export normalized canonical catalog entities to Markdown."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

from markdown_utils import (
    assets_section,
    clean_table_value,
    filename_stem,
    relative_markdown_link,
    replace_asset_links,
    strip_leading_title,
    yaml_scalar,
)
from normalize_catalog import catalog_normalized_manifest_path
from runtime import log_event
from storage import (
    catalog_dir,
    catalog_markdown_dir,
    catalog_normalized_dir,
    listed_output_files,
    load_json,
    relative_to_output,
    run_with_output_lock,
    save_json,
    utc_now_iso,
)

def catalog_markdown_bucket_dir(bucket: str) -> Path:
    return catalog_markdown_dir() / bucket


def catalog_markdown_manifest_path() -> Path:
    return catalog_dir() / "markdown_manifest.json"


def bucket_for_document(doc: dict[str, Any]) -> str:
    effectiveness = (doc.get("effectiveness") or {}).get("status")
    return {
        "current": "current",
        "historical": "historical",
        "not_applicable": "reference",
    }.get(str(effectiveness), "unknown")


def _target_path(
    doc: dict[str, Any],
    entity_id: str,
    used_paths: set[Path],
) -> Path:
    metadata = doc.get("metadata") or {}
    target_dir = catalog_markdown_bucket_dir(bucket_for_document(doc))
    stem = filename_stem(metadata, entity_id)
    candidate = target_dir / f"{stem}.md"
    if candidate not in used_paths:
        used_paths.add(candidate)
        return candidate
    suffix = entity_id.removeprefix("law_")[:8]
    candidate = target_dir / f"{stem} - {suffix}.md"
    counter = 2
    while candidate in used_paths:
        candidate = target_dir / f"{stem} - {suffix}-{counter}.md"
        counter += 1
    used_paths.add(candidate)
    return candidate


def _front_matter(doc: dict[str, Any]) -> str:
    metadata = doc.get("metadata") or {}
    preferred = doc.get("preferred_source") or {}
    effectiveness = doc.get("effectiveness") or {}
    values = {
        "id": doc.get("id"),
        "title": doc.get("title"),
        "document_type": doc.get("document_type"),
        "status": doc.get("status"),
        "effectiveness": effectiveness.get("status"),
        "effectiveness_label": effectiveness.get("label"),
        "effectiveness_basis": effectiveness.get("basis"),
        "fileno": metadata.get("fileno"),
        "pub_org": metadata.get("pub_org"),
        "pub_date": metadata.get("pub_date"),
        "effective_date": metadata.get("effective_date"),
        "preferred_source_system": preferred.get("system"),
        "preferred_source_record_id": preferred.get("record_id"),
        "content_status": doc.get("content_status"),
        "source_file": doc.get("source_file"),
    }
    lines = ["---"]
    lines.extend(f"{key}: {yaml_scalar(value)}" for key, value in values.items())
    revision_ref = doc.get("revision_ref") or {}
    if revision_ref:
        lines.append(f"revision_ref: {yaml_scalar(revision_ref.get('family_id'))}")
    lines.append("---")
    return "\n".join(lines)


def _metadata_table(doc: dict[str, Any]) -> str:
    metadata = doc.get("metadata") or {}
    effectiveness = doc.get("effectiveness") or {}
    rows = [
        ("统一法规 ID", doc.get("id")),
        ("文件类型", doc.get("document_type")),
        ("文号", metadata.get("fileno")),
        ("发布机构", metadata.get("pub_org")),
        ("发布日期", metadata.get("pub_date")),
        ("施行日期", metadata.get("effective_date")),
        ("效力状态", doc.get("status")),
        ("归一化效力", effectiveness.get("status")),
        ("归一化效力标签", effectiveness.get("label")),
        ("归一化效力依据", effectiveness.get("basis")),
        ("首选来源", (doc.get("preferred_source") or {}).get("system")),
    ]
    lines = ["| 字段 | 值 |", "| --- | --- |"]
    lines.extend(
        f"| {clean_table_value(key)} | {clean_table_value(value)} |"
        for key, value in rows
    )
    return "\n".join(lines)


def _sources_section(doc: dict[str, Any], markdown_path: Path) -> str:
    sources = doc.get("sources") or []
    if not sources:
        return ""
    lines = [
        "## 官方来源",
        "",
        "| 来源 | 角色 | 记录 ID | 链接 |",
        "| --- | --- | --- | --- |",
    ]
    for source in sources:
        local_link = relative_markdown_link(markdown_path, source.get("local_file"))
        page_url = source.get("page_url")
        links = []
        if page_url:
            links.append(f"[官网]({page_url})")
        if local_link:
            links.append(f"[本地]({local_link})")
        lines.append(
            "| "
            + " | ".join(
                [
                    clean_table_value(source.get("system")),
                    clean_table_value(source.get("role")),
                    clean_table_value(source.get("record_id")),
                    " / ".join(links),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def build_catalog_markdown(doc: dict[str, Any], markdown_path: Path) -> str:
    title = str(doc.get("title") or doc.get("id") or markdown_path.stem)
    assets: dict[str, dict[str, Any]] = {}
    for asset in doc.get("assets") or []:
        asset_id = str(asset.get("asset_id") or "")
        if not asset_id:
            continue
        local_link = relative_markdown_link(markdown_path, asset.get("local_file"))
        assets[asset_id] = {
            **asset,
            "markdown_link": local_link or asset.get("source_url") or f"asset:{asset_id}",
        }
    body = strip_leading_title(str(doc.get("full_text_markdown") or ""), title)
    body = replace_asset_links(body, assets)
    parts = [_front_matter(doc), f"# {title}", _metadata_table(doc)]
    if body:
        parts.append(body)
    elif doc.get("content_status") == "metadata_only":
        parts.append(
            "> 正文未能从官方文件中自动抽取；请参阅下方官方来源或本地附件。"
        )
    sources = _sources_section(doc, markdown_path)
    if sources:
        parts.append(sources)
    asset_section = assets_section(assets)
    if asset_section:
        parts.append(asset_section)
    return "\n\n".join(parts).rstrip() + "\n"


def export_catalog_markdown(
    *,
    limit: int | None = None,
    force: bool = False,
    clean: bool = False,
) -> dict[str, Any]:
    normalized_files = listed_output_files(
        catalog_normalized_manifest_path(),
        field="file",
        fallback_dir=catalog_normalized_dir(),
        pattern="law_*.json",
        limit=limit,
    )
    if not normalized_files:
        raise FileNotFoundError(
            "canonical/json 不存在，请先运行 python normalize_catalog.py"
        )
    if clean and catalog_markdown_dir().exists():
        shutil.rmtree(catalog_markdown_dir())
    for bucket in ("current", "unknown", "historical", "reference"):
        catalog_markdown_bucket_dir(bucket).mkdir(parents=True, exist_ok=True)

    used_paths: set[Path] = set()
    items: list[dict[str, Any]] = []
    written = 0
    skipped = 0
    bucket_counts = {
        "current": 0,
        "unknown": 0,
        "historical": 0,
        "reference": 0,
    }
    for index, path in enumerate(normalized_files, start=1):
        doc = load_json(path, {})
        entity_id = str(doc.get("id") or path.stem)
        out_path = _target_path(doc, entity_id, used_paths)
        bucket = bucket_for_document(doc)
        bucket_counts[bucket] += 1
        if out_path.exists() and not force:
            skipped += 1
        else:
            out_path.write_text(
                build_catalog_markdown(doc, out_path),
                encoding="utf-8",
            )
            written += 1
        items.append(
            {
                "id": entity_id,
                "title": doc.get("title"),
                "status": doc.get("status"),
                "effectiveness": (doc.get("effectiveness") or {}).get("status"),
                "effectiveness_basis": (doc.get("effectiveness") or {}).get("basis"),
                "bucket": bucket,
                "source_file": relative_to_output(path),
                "file": relative_to_output(out_path),
                "text_length": len(str(doc.get("full_text_plain") or "")),
            }
        )
        if index % 100 == 0 or index == len(normalized_files):
            log_event(
                "export_progress",
                message=f"  exported catalog {index}/{len(normalized_files)}",
                index=index,
                total=len(normalized_files),
            )

    manifest = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "source_dir": relative_to_output(catalog_normalized_dir()),
        "markdown_dir": relative_to_output(catalog_markdown_dir()),
        "count": len(items),
        "bucket_counts": bucket_counts,
        "written": written,
        "skipped": skipped,
        "filename_pattern": "title - fileno - effective_date.md",
        "items": items,
    }
    save_json(catalog_markdown_manifest_path(), manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="导出统一法规目录 Markdown")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()
    try:
        manifest = export_catalog_markdown(
            limit=args.limit,
            force=args.force,
            clean=args.clean,
        )
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log_event("cli_error", level="ERROR", message=f"失败: {exc}", error_message=str(exc))
        return 1
    log_event(
        "cli_result",
        message=(
            f"完成: count={manifest['count']} written={manifest['written']} "
            f"skipped={manifest['skipped']} buckets={manifest['bucket_counts']} "
            f"-> {catalog_markdown_manifest_path()}"
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "export-markdown-catalog"))
