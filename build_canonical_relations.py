#!/usr/bin/env python3
"""Consolidate all production relation types into one canonical graph."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from relation_services import CanonicalRelationGraphBuilder
from runtime import log_event
from storage import (
    canonical_dir,
    cases_path,
    catalog_dir,
    catalog_laws_dir,
    catalog_relations_path,
    listed_output_files,
    load_json,
    related_laws_path,
    relative_to_output,
    revisions_path,
    run_with_output_lock,
    save_json,
    source_matches_path,
    utc_now_iso,
    writ_file_path,
)

def canonical_graph_path() -> Path:
    return canonical_dir() / "relations" / "graph.json"


def catalog_manifest_path() -> Path:
    return catalog_dir() / "manifest.json"


def build_canonical_relations() -> dict[str, Any]:
    source_map_doc = load_json(source_matches_path(), {})
    source_map: dict[str, str] = source_map_doc.get("by_source") or {}

    def load_writ_node(writ_id: str) -> tuple[dict[str, Any], str | None]:
        path = writ_file_path(writ_id)
        writ = load_json(path, {}) if path.exists() else {}
        return writ, relative_to_output(path) if path.exists() else None

    builder = CanonicalRelationGraphBuilder(
        source_map=source_map,
        load_writ=load_writ_node,
    )

    catalog_files = listed_output_files(
        catalog_manifest_path(),
        field="file",
        fallback_dir=catalog_laws_dir(),
        pattern="law_*.json",
    )
    for path in catalog_files:
        entity = load_json(path, {})
        entity_id = str(entity.get("id") or path.stem)
        normalized_path = canonical_dir() / "json" / f"{entity_id}.json"
        normalized = load_json(normalized_path, {}) if normalized_path.exists() else {}
        builder.add_catalog_entity(
            normalized or entity,
            local_file=relative_to_output(normalized_path),
        )

    revisions = load_json(revisions_path(), {})
    for family in (revisions.get("families") or {}).values():
        versions = {
            str(item.get("id")): item for item in (family.get("versions") or [])
        }
        for edge in family.get("edges") or []:
            from_source = str(edge.get("from"))
            to_source = str(edge.get("to"))
            builder.add_edge(
                builder.law_node("neris", from_source, versions.get(from_source)),
                builder.law_node("neris", to_source, versions.get(to_source)),
                "supersedes",
                source=str(edge.get("source") or "neris.changeLaw"),
                confidence=float(edge.get("confidence") or 0.95),
                evidence={
                    "family_id": family.get("family_id"),
                    "source_ids": [from_source, to_source],
                    "details": edge.get("evidence") or [],
                },
            )

    related = load_json(related_laws_path(), {}).get("items") or {}
    for source_id, items in related.items():
        from_id = builder.law_node("neris", source_id)
        for item in items or []:
            raw = item.get("raw") or {}
            target_id = (
                item.get("to_law_id")
                or raw.get("putAndLawId")
                or raw.get("secFutrsLawId")
            )
            if str(target_id) == str(source_id) and raw.get("putAndLawId"):
                target_id = raw.get("putAndLawId")
            if not target_id:
                continue
            builder.add_edge(
                from_id,
                builder.law_node(
                    "neris",
                    target_id,
                    {"name": item.get("name") or raw.get("putAndLawName")},
                ),
                "related_to",
                source="neris.findRelativeFile",
                confidence=1.0,
                evidence={
                    "relation_type": item.get("relation_type"),
                    "raw_relation_id": raw.get("secFutrsLawPutAndLawId"),
                },
            )

    cases = load_json(cases_path(), {}).get("by_law") or {}
    for law_id, record in cases.items():
        from_id = builder.law_node("neris", law_id)
        for case in record.get("law_level") or []:
            writ_id = case.get("law_writ_id")
            if writ_id:
                builder.add_edge(
                    from_id,
                    builder.writ_node(writ_id, case),
                    "cited_by_case",
                    source="neris.relativeExample",
                    confidence=1.0,
                    evidence={"scope": "law"},
                    qualifier=f"law:{writ_id}",
                )
        for entry_id, entry_cases in (record.get("by_entry") or {}).items():
            for case in entry_cases or []:
                writ_id = case.get("law_writ_id")
                if writ_id:
                    builder.add_edge(
                        from_id,
                        builder.writ_node(writ_id, case),
                        "cited_by_case",
                        source="neris.relativeExample",
                        confidence=1.0,
                        evidence={"scope": "entry", "entry_id": entry_id},
                        qualifier=f"entry:{entry_id}:{writ_id}",
                    )

    catalog_relations = load_json(catalog_relations_path(), {}).get("items") or []
    for relation in catalog_relations:
        from_id = str(relation.get("from") or "")
        to_id = str(relation.get("to") or "")
        if from_id not in builder.nodes or to_id not in builder.nodes:
            continue
        builder.add_edge(
            from_id,
            to_id,
            str(relation.get("relation") or "related_to"),
            source=str(relation.get("source") or "catalog"),
            confidence=float(relation.get("confidence") or 1.0),
            evidence=relation.get("evidence") or {},
        )

    graph = builder.as_graph(updated_at=utc_now_iso())
    save_json(canonical_graph_path(), graph)
    return graph


def main() -> int:
    parser = argparse.ArgumentParser(description="生成唯一 canonical 关系图")
    parser.parse_args()
    try:
        graph = build_canonical_relations()
    except Exception as exc:
        log_event("cli_error", level="ERROR", message=f"失败: {exc}", error_message=str(exc))
        return 1
    log_event("cli_result", message=str(graph["counts"]))
    return 0


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "build-canonical-relations"))
