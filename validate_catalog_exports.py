#!/usr/bin/env python3
"""Validate canonical catalog normalized and Markdown coverage."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from build_canonical_relations import canonical_graph_path
from export_markdown_catalog import catalog_markdown_manifest_path
from models import format_model_issues
from normalize_catalog import catalog_manifest_path, catalog_normalized_manifest_path
from runtime import log_event
from storage import (
    canonical_dir,
    catalog_laws_dir,
    catalog_markdown_dir,
    catalog_normalized_dir,
    listed_output_files,
    load_json,
    output_path,
    run_with_output_lock,
    save_json,
    utc_now_iso,
)


def _catalog_entity_files() -> list[Path]:
    return listed_output_files(
        catalog_manifest_path(),
        field="file",
        fallback_dir=catalog_laws_dir(),
        pattern="law_*.json",
    )


def _catalog_normalized_files() -> list[Path]:
    return listed_output_files(
        catalog_normalized_manifest_path(),
        field="file",
        fallback_dir=catalog_normalized_dir(),
        pattern="law_*.json",
    )


def _catalog_markdown_files() -> list[Path]:
    return listed_output_files(
        catalog_markdown_manifest_path(),
        field="file",
        fallback_dir=catalog_markdown_dir(),
        pattern="*/*.md",
    )


def validate_catalog_exports() -> tuple[list[str], dict[str, Any]]:
    issues: list[str] = []
    catalog_files = _catalog_entity_files()
    normalized_files = _catalog_normalized_files()
    markdown_files = _catalog_markdown_files()

    catalog_ids = {path.stem for path in catalog_files}
    normalized_ids: set[str] = set()
    empty_content = 0
    metadata_only = 0
    for path in normalized_files:
        doc = load_json(path, {})
        issues.extend(format_model_issues("canonical_law", path.name, doc))
        for asset in doc.get("assets") or []:
            issues.extend(
                format_model_issues(
                    "asset_record",
                    f"{path.name}:{asset.get('asset_id') or 'asset'}",
                    asset,
                )
            )
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
            "canonical/json ID coverage mismatch: "
            f"missing={len(catalog_ids - normalized_ids)} "
            f"extra={len(normalized_ids - catalog_ids)}"
        )

    normalized_manifest = load_json(catalog_normalized_manifest_path(), {})
    if normalized_manifest.get("count") != len(normalized_files):
        issues.append("catalog normalized manifest count mismatch")

    markdown_manifest = load_json(catalog_markdown_manifest_path(), {})
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
        path = output_path(str(relative))
        if path in manifest_paths:
            issues.append(f"duplicate Markdown path: {relative}")
        manifest_paths.add(path)
        if not path.exists():
            issues.append(f"missing Markdown file: {relative}")

    graph_path = canonical_graph_path()
    graph = load_json(graph_path, {})
    issues.extend(format_model_issues("relation_graph", graph_path.name, graph))
    graph_nodes = {
        str(node.get("id"))
        for node in (graph.get("nodes") or [])
        if node.get("id")
    }
    for index, edge in enumerate(graph.get("edges") or []):
        issues.extend(format_model_issues("relation_edge", f"edge[{index}]", edge))
        if str(edge.get("from")) not in graph_nodes:
            issues.append(f"canonical graph edge[{index}]: missing from node")
        if str(edge.get("to")) not in graph_nodes:
            issues.append(f"canonical graph edge[{index}]: missing to node")
    if not graph_nodes:
        issues.append("canonical relation graph missing or empty")

    summary = {
        "catalog_laws": len(catalog_files),
        "normalized_laws": len(normalized_files),
        "markdown_files": len(markdown_files),
        "normalized_empty_content": empty_content,
        "metadata_only": metadata_only,
        "bucket_counts": dict(markdown_manifest.get("bucket_counts") or {}),
        "issues": len(issues),
        "relation_nodes": len(graph_nodes),
        "relation_edges": len(graph.get("edges") or []),
    }
    if not issues:
        save_json(
            canonical_dir() / "manifest.json",
            {
                "schema_version": 1,
                "updated_at": utc_now_iso(),
                "json_count": len(normalized_files),
                "markdown_count": len(markdown_files),
                "metadata_only": metadata_only,
                "bucket_counts": summary["bucket_counts"],
                "relations_file": "canonical/relations/graph.json",
                "source_map_file": "canonical/indexes/source_map.json",
            },
        )
    return issues, summary


def main() -> int:
    issues, summary = validate_catalog_exports()
    log_event("validation_summary", message=json.dumps(summary, ensure_ascii=False, indent=2))
    if issues:
        log_event("validation_issues", level="ERROR", message="\n问题:")
        for issue in issues[:100]:
            log_event("validation_issue", level="ERROR", message=f"  - {issue}", issue=issue)
        return 1
    log_event("validation_passed", message="\n统一目录 normalized/Markdown 校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "validate-catalog-exports"))
