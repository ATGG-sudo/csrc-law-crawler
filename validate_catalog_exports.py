#!/usr/bin/env python3
"""Validate canonical catalog normalized and Markdown coverage."""

from __future__ import annotations

from datetime import date
import json
import sys
from pathlib import Path
from typing import Any

from build_canonical_relations import canonical_graph_path
from csrc_law_crawler.processing.catalog.classification import disciplinary_penalty_subtype
from export_markdown_catalog import (
    bucket_for_document,
    catalog_library_dir,
    catalog_library_manifest_path,
    catalog_markdown_manifest_path,
    library_relative_dir_for_document,
)
from models import format_model_issues
from normalize_catalog import (
    catalog_manifest_path,
    catalog_normalized_manifest_path,
    classification_review_queue_path,
)
from runtime import log_event
from storage import (
    canonical_dir,
    catalog_laws_dir,
    catalog_relations_path,
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


def _iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError:
        return None


def validate_catalog_exports() -> tuple[list[str], dict[str, Any]]:
    issues: list[str] = []
    catalog_files = _catalog_entity_files()
    normalized_files = _catalog_normalized_files()
    markdown_files = _catalog_markdown_files()

    catalog_ids = {path.stem for path in catalog_files}
    normalized_ids: set[str] = set()
    normalized_docs: dict[str, dict[str, Any]] = {}
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
        normalized_docs[entity_id] = doc
        if entity_id != path.stem:
            issues.append(f"{path.name}: filename/id mismatch")
        if not str(doc.get("full_text_plain") or "").strip():
            empty_content += 1
        if doc.get("content_status") == "metadata_only":
            metadata_only += 1
        if not doc.get("sources"):
            issues.append(f"{path.name}: missing sources")
        material = doc.get("material_classification") or {}
        effectiveness = doc.get("effectiveness") or {}
        lane = str(material.get("lane") or "unknown")
        status = str(effectiveness.get("status") or "unknown")
        as_of = _iso_date(effectiveness.get("as_of"))
        metadata = doc.get("metadata") or {}
        enforcement = doc.get("enforcement_classification") or {}
        effective_date = _iso_date(metadata.get("effective_date"))
        ineffective_date = _iso_date(metadata.get("ineffective_date"))
        if lane == "reference" and status != "not_applicable":
            issues.append(f"{path.name}: reference material has rule effectiveness")
        if lane == "rule" and status == "not_applicable":
            issues.append(f"{path.name}: rule has not_applicable effectiveness")
        if status == "current" and effective_date and as_of and effective_date > as_of:
            issues.append(f"{path.name}: future effective rule in current")
        if status == "current" and ineffective_date and as_of and ineffective_date <= as_of:
            issues.append(f"{path.name}: expired rule in current")
        penalty_subtype = disciplinary_penalty_subtype(doc.get("title"))
        if penalty_subtype:
            if enforcement.get("category") != "penalties":
                issues.append(f"{path.name}: disciplinary material is not penalties")
            if material.get("category") != "enforcement_reference":
                issues.append(f"{path.name}: disciplinary material is not enforcement reference")
        if lane == "rule" and enforcement:
            issues.append(f"{path.name}: rule has enforcement case classification")
        if enforcement.get("category") == "penalties" and lane != "reference":
            issues.append(f"{path.name}: penalties material entered rule lane")

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
        doc = normalized_docs.get(str(item.get("id") or "")) or {}
        expected_bucket = bucket_for_document(doc)
        if item.get("bucket") != expected_bucket:
            issues.append(f"Markdown manifest {item.get('id')}: bucket mismatch")

    review_queue = load_json(classification_review_queue_path(), {})
    review_ids = {str(item.get("id") or "") for item in review_queue.get("items") or []}
    required_review_ids = {
        entity_id
        for entity_id, doc in normalized_docs.items()
        if (doc.get("material_classification") or {}).get("lane") == "unknown"
        or (
            (doc.get("material_classification") or {}).get("lane") == "rule"
            and (doc.get("effectiveness") or {}).get("status") == "unknown"
        )
    }
    if not required_review_ids <= review_ids:
        issues.append(
            "classification review queue missing unknown IDs: "
            f"{len(required_review_ids - review_ids)}"
        )
    if review_queue.get("count") != len(review_queue.get("items") or []):
        issues.append("classification review queue count mismatch")

    library_manifest = load_json(catalog_library_manifest_path(), {})
    library_items = library_manifest.get("items") or []
    library_ids = [str(item.get("id") or "") for item in library_items]
    if set(library_ids) != catalog_ids or len(library_ids) != len(set(library_ids)):
        issues.append("canonical library ID coverage or uniqueness mismatch")
    library_paths: set[Path] = set()
    for item in library_items:
        entity_id = str(item.get("id") or "")
        relative = str(item.get("file") or "")
        path = output_path(relative) if relative else Path()
        if not relative or not path.exists():
            issues.append(f"library manifest {entity_id}: missing file")
            continue
        if path in library_paths:
            issues.append(f"duplicate library path: {relative}")
        library_paths.add(path)
        doc = normalized_docs.get(entity_id) or {}
        expected_dir = catalog_library_dir() / library_relative_dir_for_document(doc)
        if path.parent != expected_dir:
            issues.append(f"library manifest {entity_id}: classification directory mismatch")
    actual_library_paths = set(catalog_library_dir().glob("**/*.md"))
    if actual_library_paths != library_paths:
        issues.append(
            "canonical library file coverage mismatch: "
            f"missing={len(library_paths - actual_library_paths)} "
            f"extra={len(actual_library_paths - library_paths)}"
        )
    if library_manifest.get("count") != len(library_items):
        issues.append("canonical library manifest count mismatch")

    allowed_library_dirs = {
        "01_现行制度",
        "02_待生效制度",
        "03_失效制度",
        "04_参考资料",
        "04_参考资料/01_发布与征求意见",
        "04_参考资料/02_解读问答",
        "04_参考资料/03_办事指南与模板",
        "04_参考资料/04_行业研究与统计",
        "04_参考资料/05_监管案例与自律措施",
        "04_参考资料/99_其他参考",
        "05_待核验",
        "05_待核验/01_制度效力待核验",
        "05_待核验/02_材料性质待核验",
    }
    actual_library_dirs = {
        str(path.relative_to(catalog_library_dir()))
        for path in catalog_library_dir().glob("**/*")
        if path.is_dir()
    }
    unexpected_dirs = actual_library_dirs - allowed_library_dirs
    if unexpected_dirs:
        issues.append(f"canonical library has unexpected directories: {sorted(unexpected_dirs)}")

    for relation in load_json(catalog_relations_path(), {}).get("items") or []:
        if relation.get("relation") != "same_instrument_copy":
            continue
        left_id = str(relation.get("from") or "")
        right_id = str(relation.get("to") or "")
        left = normalized_docs.get(left_id) or {}
        right = normalized_docs.get(right_id) or {}
        left_status = (left.get("effectiveness") or {}).get("status")
        right_status = (right.get("effectiveness") or {}).get("status")
        if left_status and right_status and left_status != right_status:
            issues.append(
                f"same instrument copy effectiveness conflict: {left_id}={left_status}, "
                f"{right_id}={right_status}"
            )

    graph_path = canonical_graph_path()
    graph = load_json(graph_path, {})
    issues.extend(format_model_issues("relation_graph", graph_path.name, graph))
    graph_nodes = {str(node.get("id")) for node in (graph.get("nodes") or []) if node.get("id")}
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
        "library_files": len(library_paths),
        "classification_review": len(review_ids),
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
                "library_count": len(library_paths),
                "metadata_only": metadata_only,
                "bucket_counts": summary["bucket_counts"],
                "relations_file": "canonical/relations/graph.json",
                "source_map_file": "canonical/indexes/source_map.json",
                "library_manifest": "canonical/library/manifest.json",
                "classification_review_queue": ("canonical/classification_review_queue.json"),
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
