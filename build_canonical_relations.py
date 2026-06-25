#!/usr/bin/env python3
"""Consolidate all production relation types into one canonical graph."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from config import OUTPUT_DIR
from storage import (
    canonical_dir,
    cases_path,
    catalog_laws_dir,
    catalog_relations_path,
    load_json,
    related_laws_path,
    revisions_path,
    save_json,
    source_matches_path,
    utc_now_iso,
    writ_file_path,
)

CANONICAL_GRAPH = canonical_dir() / "relations" / "graph.json"


def build_canonical_relations() -> dict[str, Any]:
    source_map_doc = load_json(source_matches_path(), {})
    source_map: dict[str, str] = source_map_doc.get("by_source") or {}
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    edge_keys: set[tuple[str, str, str, str]] = set()

    for path in sorted(catalog_laws_dir().glob("law_*.json")):
        entity = load_json(path, {})
        entity_id = str(entity.get("id") or path.stem)
        nodes[entity_id] = {
            "id": entity_id,
            "type": "law",
            "title": entity.get("title"),
            "document_type": entity.get("document_type"),
            "status": entity.get("status"),
            "local_file": str(
                (canonical_dir() / "json" / f"{entity_id}.json").relative_to(
                    OUTPUT_DIR
                )
            ),
        }

    def law_node(system: str, record_id: Any, metadata: dict[str, Any] | None = None) -> str:
        source_key = f"{system}:{record_id}"
        canonical_id = source_map.get(source_key)
        if canonical_id:
            return canonical_id
        stub_id = source_key
        if stub_id not in nodes:
            nodes[stub_id] = {
                "id": stub_id,
                "type": "law_stub",
                "source_system": system,
                "source_record_id": str(record_id),
                "title": (metadata or {}).get("name"),
                "version": (metadata or {}).get("version"),
            }
        return stub_id

    def writ_node(writ_id: Any, case: dict[str, Any] | None = None) -> str:
        node_id = f"writ:{writ_id}"
        if node_id not in nodes:
            path = writ_file_path(str(writ_id))
            writ = load_json(path, {}) if path.exists() else {}
            metadata = writ.get("metadata") or {}
            nodes[node_id] = {
                "id": node_id,
                "type": "writ",
                "title": metadata.get("name") or (case or {}).get("name"),
                "writ_type": metadata.get("writ_type"),
                "local_file": (
                    str(path.relative_to(OUTPUT_DIR)) if path.exists() else None
                ),
            }
        return node_id

    def add_edge(
        from_id: str,
        to_id: str,
        relation: str,
        *,
        source: str,
        confidence: float,
        evidence: dict[str, Any],
        qualifier: str = "",
    ) -> None:
        key = (from_id, to_id, relation, qualifier)
        if from_id == to_id or key in edge_keys:
            return
        edge_keys.add(key)
        edges.append(
            {
                "from": from_id,
                "to": to_id,
                "relation": relation,
                "source": source,
                "confidence": confidence,
                "evidence": evidence,
            }
        )

    revisions = load_json(revisions_path(), {})
    for family in (revisions.get("families") or {}).values():
        versions = {
            str(item.get("id")): item for item in (family.get("versions") or [])
        }
        for edge in family.get("edges") or []:
            from_source = str(edge.get("from"))
            to_source = str(edge.get("to"))
            add_edge(
                law_node("neris", from_source, versions.get(from_source)),
                law_node("neris", to_source, versions.get(to_source)),
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
        from_id = law_node("neris", source_id)
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
            add_edge(
                from_id,
                law_node(
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
        from_id = law_node("neris", law_id)
        for case in record.get("law_level") or []:
            writ_id = case.get("law_writ_id")
            if writ_id:
                add_edge(
                    from_id,
                    writ_node(writ_id, case),
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
                    add_edge(
                        from_id,
                        writ_node(writ_id, case),
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
        if from_id not in nodes or to_id not in nodes:
            continue
        add_edge(
            from_id,
            to_id,
            str(relation.get("relation") or "related_to"),
            source=str(relation.get("source") or "catalog"),
            confidence=float(relation.get("confidence") or 1.0),
            evidence=relation.get("evidence") or {},
        )

    relation_counts: dict[str, int] = {}
    for edge in edges:
        relation = str(edge["relation"])
        relation_counts[relation] = relation_counts.get(relation, 0) + 1
    graph = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "nodes": list(nodes.values()),
        "edges": edges,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "relations": dict(sorted(relation_counts.items())),
        },
    }
    save_json(CANONICAL_GRAPH, graph)
    return graph


def main() -> int:
    parser = argparse.ArgumentParser(description="生成唯一 canonical 关系图")
    parser.parse_args()
    try:
        graph = build_canonical_relations()
    except Exception as exc:
        print(f"失败: {exc}", file=sys.stderr)
        return 1
    print(graph["counts"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
