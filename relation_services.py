"""Service helpers for assembling canonical relation graphs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

JsonRecord = dict[str, Any]
WritLoader = Callable[[str], tuple[JsonRecord, str | None]]


@dataclass
class CanonicalRelationGraphBuilder:
    source_map: dict[str, str]
    load_writ: WritLoader
    nodes: dict[str, JsonRecord] = field(default_factory=dict)
    edges: list[JsonRecord] = field(default_factory=list)
    edge_keys: set[tuple[str, str, str, str]] = field(default_factory=set)

    def add_catalog_entity(self, entity: JsonRecord, *, local_file: str) -> str:
        entity_id = str(entity.get("id") or "")
        if not entity_id:
            return ""
        self.nodes[entity_id] = {
            "id": entity_id,
            "type": "law",
            "title": entity.get("title"),
            "document_type": entity.get("document_type"),
            "status": entity.get("status"),
            "local_file": local_file,
        }
        return entity_id

    def law_node(
        self,
        system: str,
        record_id: Any,
        metadata: JsonRecord | None = None,
    ) -> str:
        source_key = f"{system}:{record_id}"
        canonical_id = self.source_map.get(source_key)
        if canonical_id:
            return canonical_id
        stub_id = source_key
        if stub_id not in self.nodes:
            self.nodes[stub_id] = {
                "id": stub_id,
                "type": "law_stub",
                "source_system": system,
                "source_record_id": str(record_id),
                "title": (metadata or {}).get("name"),
                "version": (metadata or {}).get("version"),
            }
        return stub_id

    def writ_node(self, writ_id: Any, case: JsonRecord | None = None) -> str:
        node_id = f"writ:{writ_id}"
        if node_id in self.nodes:
            return node_id
        writ, local_file = self.load_writ(str(writ_id))
        metadata = writ.get("metadata") or {}
        self.nodes[node_id] = {
            "id": node_id,
            "type": "writ",
            "title": metadata.get("name") or (case or {}).get("name"),
            "writ_type": metadata.get("writ_type"),
            "local_file": local_file,
        }
        return node_id

    def add_edge(
        self,
        from_id: str,
        to_id: str,
        relation: str,
        *,
        source: str,
        confidence: float,
        evidence: JsonRecord,
        qualifier: str = "",
    ) -> None:
        key = (from_id, to_id, relation, qualifier)
        if from_id == to_id or key in self.edge_keys:
            return
        self.edge_keys.add(key)
        edge = {
            "from": from_id,
            "to": to_id,
            "relation": relation,
            "source": source,
            "confidence": confidence,
            "evidence": evidence,
        }
        rule_id = evidence.get("rule_id")
        if rule_id:
            edge["rule_id"] = rule_id
        self.edges.append(edge)

    def as_graph(self, *, updated_at: str) -> JsonRecord:
        relation_counts: dict[str, int] = {}
        for edge in self.edges:
            relation = str(edge["relation"])
            relation_counts[relation] = relation_counts.get(relation, 0) + 1
        return {
            "schema_version": 1,
            "updated_at": updated_at,
            "nodes": list(self.nodes.values()),
            "edges": self.edges,
            "counts": {
                "nodes": len(self.nodes),
                "edges": len(self.edges),
                "relations": dict(sorted(relation_counts.items())),
            },
        }
