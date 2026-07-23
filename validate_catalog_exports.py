#!/usr/bin/env python3
"""Validate canonical catalog normalized and Markdown coverage."""

from __future__ import annotations

import csv
from datetime import date
import json
import sys
from pathlib import Path
from typing import Any

from build_canonical_relations import canonical_graph_path
from csrc_law_crawler.processing.catalog.classification import disciplinary_penalty_subtype
from csrc_law_crawler.processing.catalog.curated_relations import (
    load_curated_catalog,
    resolve_curated_documents,
)
from export_markdown_catalog import (
    bucket_for_document,
    catalog_library_dir,
    catalog_library_manifest_path,
    catalog_markdown_manifest_path,
    directional_relation_summary,
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
    source_matches_path,
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


def _relation_ref_key(
    *,
    owner_id: str,
    direction: str,
    relation: dict[str, Any],
) -> tuple[str, str, str, str, str, str, str, str]:
    counterpart_id = str(relation.get("canonical_id") or "")
    return (
        owner_id,
        direction,
        counterpart_id,
        str(relation.get("relation") or ""),
        str(relation.get("source") or ""),
        str(relation.get("confidence", 1.0)),
        str(relation.get("rule_id") or ""),
        json.dumps(relation.get("evidence") or {}, ensure_ascii=False, sort_keys=True),
    )


def _document_relation_summary(doc: dict[str, Any]) -> tuple[list[str], list[str]]:
    relations = doc.get("relations") or {}
    items = [*(relations.get("outgoing") or []), *(relations.get("incoming") or [])]
    relation_types = sorted(
        {str(item.get("relation")) for item in items if item.get("relation")}
    )
    related_ids = sorted(
        {str(item.get("canonical_id")) for item in items if item.get("canonical_id")}
    )
    return relation_types, related_ids


def _has_omnibus_parent_page_prefix(text: str) -> bool:
    normalized_lead = "".join(str(text or "")[:500].split())
    return normalized_lead.startswith(
        "最高人民法院关于修改《最高人民法院关于破产企业国有划拨土地使用权"
    )


def _expected_canonical_relation_refs(
    catalog_relations: list[dict[str, Any]],
    normalized_ids: set[str],
) -> set[tuple[str, str, str, str, str, str, str, str]]:
    expected: set[tuple[str, str, str, str, str, str, str, str]] = set()
    for relation in catalog_relations:
        from_id = str(relation.get("from") or "")
        to_id = str(relation.get("to") or "")
        common = {
            "relation": relation.get("relation"),
            "source": relation.get("source"),
            "confidence": relation.get("confidence", 1.0),
            "evidence": relation.get("evidence") or {},
        }
        if relation.get("rule_id"):
            common["rule_id"] = relation["rule_id"]
        if from_id in normalized_ids:
            expected.add(
                _relation_ref_key(
                    owner_id=from_id,
                    direction="outgoing",
                    relation={"canonical_id": to_id, **common},
                )
            )
        if to_id in normalized_ids:
            expected.add(
                _relation_ref_key(
                    owner_id=to_id,
                    direction="incoming",
                    relation={"canonical_id": from_id, **common},
                )
            )
    return expected


def _catalog_graph_edge_keys(
    catalog_relations: list[dict[str, Any]],
    graph_nodes: set[str],
) -> set[tuple[str, str, str]]:
    return {
        (str(item.get("from")), str(item.get("to")), str(item.get("relation")))
        for item in catalog_relations
        if str(item.get("from")) in graph_nodes
        and str(item.get("to")) in graph_nodes
    }


def _validate_curated_semantics(
    normalized_docs: dict[str, dict[str, Any]],
    catalog_relations: list[dict[str, Any]],
) -> tuple[list[str], dict[str, int]]:
    issues: list[str] = []
    payload = load_curated_catalog()
    source_map = load_json(source_matches_path(), {}).get("by_source") or {}
    source_to_entity = {
        tuple(str(source_key).split(":", 1)): str(entity_id)
        for source_key, entity_id in source_map.items()
        if ":" in str(source_key)
    }
    try:
        resolved = resolve_curated_documents(source_to_entity, payload)
    except ValueError as exc:
        issues.append(f"curated source resolution conflict: {exc}")
        resolved = {}
    expected_document_keys = {
        str(item.get("document_key")) for item in payload.get("documents") or []
    }
    missing_document_keys = expected_document_keys - set(resolved)
    if missing_document_keys:
        issues.append(
            "curated source resolution incomplete: "
            + ", ".join(sorted(missing_document_keys))
        )

    expected_relations = payload.get("relations") or []
    resolved_relation_count = 0
    for spec in expected_relations:
        from_id = resolved.get(str(spec.get("from_document")))
        to_id = resolved.get(str(spec.get("to_document")))
        if not from_id or not to_id:
            continue
        matches = [
            item
            for item in catalog_relations
            if str(item.get("from")) == from_id
            and str(item.get("to")) == to_id
            and str(item.get("relation")) == str(spec.get("relation"))
            and (item.get("evidence") or {}).get("curated_relation_key")
            == spec.get("relation_key")
        ]
        if len(matches) != 1:
            issues.append(
                f"curated relation {spec.get('relation_key')}: expected 1 edge, got "
                f"{len(matches)}"
            )
        else:
            resolved_relation_count += 1

    keys = {
        "old": "spc_company_law_interpretation_3_2014",
        "new": "spc_company_law_interpretation_3_2020",
        "rule7": "spc_company_law_temporal_effect_2024_7",
        "reply15": "spc_company_law_reply_2024_15",
        "guidance": "spc_company_law_transition_guidance_438551",
    }
    docs = {
        name: normalized_docs.get(resolved.get(document_key, ""), {})
        for name, document_key in keys.items()
    }
    if docs["rule7"]:
        if (docs["rule7"].get("effectiveness") or {}).get("status") != "current":
            issues.append("法释〔2024〕7号 must remain current")
        if docs["rule7"].get("superseded_by"):
            issues.append("法释〔2024〕7号 must not have superseded_by")
    if docs["reply15"]:
        reply_metadata = docs["reply15"].get("metadata") or {}
        reply_text = str(docs["reply15"].get("full_text_plain") or "")
        if reply_metadata.get("fileno") != "法释〔2024〕15号":
            issues.append("法释〔2024〕15号 fileno mismatch")
        if "2024年7月1日之后发生的未届出资期限的股权转让行为" not in reply_text:
            issues.append("法释〔2024〕15号 missing official temporal boundary text")
    if docs["old"] and docs["new"]:
        old_status = (docs["old"].get("effectiveness") or {}).get("status")
        new_status = (docs["new"].get("effectiveness") or {}).get("status")
        if old_status != "historical" or new_status != "current":
            issues.append(
                "公司法解释（三）version effectiveness mismatch: "
                f"2014={old_status}, 2020={new_status}"
            )
        old_family = (docs["old"].get("revision_ref") or {}).get("family_id")
        new_family = (docs["new"].get("revision_ref") or {}).get("family_id")
        if not old_family or old_family != new_family:
            issues.append("公司法解释（三）versions do not share one revision family")
        new_id = str(docs["new"].get("id") or "")
        if new_id not in {
            str(item.get("canonical_id")) for item in docs["old"].get("superseded_by") or []
        }:
            issues.append("2014公司法解释（三）missing 2020 superseding version")
        old_metadata = docs["old"].get("metadata") or {}
        new_metadata = docs["new"].get("metadata") or {}
        if old_metadata.get("effective_date") != "2014-03-01":
            issues.append("2014公司法解释（三）effective_date must be 2014-03-01")
        if old_metadata.get("amending_fileno") != "法释〔2014〕2号":
            issues.append("2014公司法解释（三）amending_fileno mismatch")
        if new_metadata.get("amending_fileno") != "法释〔2020〕18号":
            issues.append("2020公司法解释（三）amending_fileno mismatch")
        if new_metadata.get("applicability_mode") != "conditional":
            issues.append("2020公司法解释（三）missing conditional applicability")
        new_text = str(docs["new"].get("full_text_plain") or "")
        new_markdown = str(docs["new"].get("full_text_markdown") or "")
        if "民法典第三百一十一条" not in new_text or "物权法第一百零六条" in new_text:
            issues.append("2020公司法解释（三）contains wrong property-law reference")
        if "合同法第五十二条" in new_text:
            issues.append("2020公司法解释（三）contains obsolete contract-law reference")
        if "## 第二十八条" not in new_markdown or "## 第二十九条" in new_markdown:
            issues.append("2020公司法解释（三）article boundary mismatch")
        if _has_omnibus_parent_page_prefix(new_text):
            issues.append("2020公司法解释（三）contains omnibus parent page text")
    if docs["guidance"]:
        material_lane = (docs["guidance"].get("material_classification") or {}).get("lane")
        if material_lane != "reference":
            issues.append("最高法公司法衔接答记者问 must remain reference material")

    reply_id = resolved.get(keys["reply15"])
    rule7_id = resolved.get(keys["rule7"])
    if reply_id and rule7_id and any(
        str(item.get("from")) == reply_id
        and str(item.get("to")) == rule7_id
        and str(item.get("relation")) == "supersedes"
        for item in catalog_relations
    ):
        issues.append("法释〔2024〕15号 must not supersede whole 法释〔2024〕7号")

    return issues, {
        "documents_resolved": len(resolved),
        "relations_resolved": resolved_relation_count,
    }


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

    catalog_relation_items = load_json(catalog_relations_path(), {}).get("items") or []
    curated_issues, curated_summary = _validate_curated_semantics(
        normalized_docs,
        catalog_relation_items,
    )
    issues.extend(curated_issues)
    expected_relation_refs = _expected_canonical_relation_refs(
        catalog_relation_items,
        normalized_ids,
    )
    actual_relation_refs: set[tuple[str, str, str, str, str, str, str, str]] = set()
    for entity_id, doc in normalized_docs.items():
        relations = doc.get("relations") or {}
        for direction in ("outgoing", "incoming"):
            for relation in relations.get(direction) or []:
                actual_relation_refs.add(
                    _relation_ref_key(
                        owner_id=entity_id,
                        direction=direction,
                        relation=relation,
                    )
                )
    if actual_relation_refs != expected_relation_refs:
        issues.append(
            "canonical JSON relation mirror mismatch: "
            f"missing={len(expected_relation_refs - actual_relation_refs)} "
            f"extra={len(actual_relation_refs - expected_relation_refs)}"
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
        relation_types, _ = _document_relation_summary(doc)
        if relation_types and path.exists():
            markdown = path.read_text(encoding="utf-8")
            if "## 版本与适用关系" not in markdown:
                issues.append(f"Markdown manifest {item.get('id')}: missing relations section")
            for relation_type in relation_types:
                if relation_type not in markdown:
                    issues.append(
                        f"Markdown manifest {item.get('id')}: missing relation {relation_type}"
                    )

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
        relation_types, related_ids = _document_relation_summary(doc)
        outgoing_relations, incoming_relations = directional_relation_summary(doc)
        if sorted(item.get("relation_types") or []) != relation_types:
            issues.append(f"library manifest {entity_id}: relation types mismatch")
        if sorted(item.get("related_ids") or []) != related_ids:
            issues.append(f"library manifest {entity_id}: related IDs mismatch")
        if sorted(item.get("outgoing_relations") or []) != outgoing_relations:
            issues.append(f"library manifest {entity_id}: outgoing relations mismatch")
        if sorted(item.get("incoming_relations") or []) != incoming_relations:
            issues.append(f"library manifest {entity_id}: incoming relations mismatch")
    actual_library_paths = set(catalog_library_dir().glob("**/*.md"))
    if actual_library_paths != library_paths:
        issues.append(
            "canonical library file coverage mismatch: "
            f"missing={len(library_paths - actual_library_paths)} "
            f"extra={len(actual_library_paths - library_paths)}"
        )
    if library_manifest.get("count") != len(library_items):
        issues.append("canonical library manifest count mismatch")
    index_path = catalog_library_dir() / "index.csv"
    if not index_path.is_file():
        issues.append("canonical library index.csv missing")
    else:
        with index_path.open("r", encoding="utf-8-sig", newline="") as handle:
            index_rows = list(csv.DictReader(handle))
        index_ids = [str(item.get("id") or "") for item in index_rows]
        if set(index_ids) != catalog_ids or len(index_ids) != len(set(index_ids)):
            issues.append("canonical library index.csv ID coverage or uniqueness mismatch")
        for row in index_rows:
            doc = normalized_docs.get(str(row.get("id") or "")) or {}
            relation_types, related_ids = _document_relation_summary(doc)
            outgoing_relations, incoming_relations = directional_relation_summary(doc)
            if sorted(filter(None, str(row.get("relation_types") or "").split(";"))) != relation_types:
                issues.append(f"canonical library index {row.get('id')}: relation types mismatch")
            if sorted(filter(None, str(row.get("related_ids") or "").split(";"))) != related_ids:
                issues.append(f"canonical library index {row.get('id')}: related IDs mismatch")
            if sorted(filter(None, str(row.get("outgoing_relations") or "").split(";"))) != outgoing_relations:
                issues.append(f"canonical library index {row.get('id')}: outgoing relations mismatch")
            if sorted(filter(None, str(row.get("incoming_relations") or "").split(";"))) != incoming_relations:
                issues.append(f"canonical library index {row.get('id')}: incoming relations mismatch")

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
    catalog_edge_keys = _catalog_graph_edge_keys(catalog_relation_items, graph_nodes)
    graph_edge_keys = {
        (str(item.get("from")), str(item.get("to")), str(item.get("relation")))
        for item in graph.get("edges") or []
    }
    if not catalog_edge_keys <= graph_edge_keys:
        issues.append(
            "canonical graph missing catalog relations: "
            f"{len(catalog_edge_keys - graph_edge_keys)}"
        )
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
        "curated_documents_resolved": curated_summary["documents_resolved"],
        "curated_relations_resolved": curated_summary["relations_resolved"],
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
                "relation_nodes": len(graph_nodes),
                "relation_edges": len(graph.get("edges") or []),
                "relation_counts": (graph.get("counts") or {}).get("relations") or {},
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
