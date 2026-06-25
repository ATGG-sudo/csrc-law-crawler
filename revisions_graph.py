"""历次修订族合并与 supersedes 边生成。"""

from __future__ import annotations

import hashlib
from typing import Any

from config import OUTPUT_DIR
from storage import reg_file_path, utc_now_iso


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, node: str) -> None:
        if node not in self.parent:
            self.parent[node] = node

    def find(self, node: str) -> str:
        self.add(node)
        while self.parent[node] != node:
            self.parent[node] = self.parent[self.parent[node]]
            node = self.parent[node]
        return node

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def version_sort_key(version: str | None) -> int:
    if not version:
        return 0
    digits = "".join(ch for ch in str(version) if ch.isdigit())
    return int(digits) if digits else 0


def normalize_version_node(
    raw: dict[str, Any],
    *,
    local_meta: dict[str, Any] | None,
) -> dict[str, Any]:
    law_id = raw.get("secFutrsLawId") or raw.get("id")
    number = raw.get("secFutrsLawNbr") or (local_meta or {}).get("number")
    version = raw.get("secFutrsLawVersion") or (local_meta or {}).get("version")
    name = (
        raw.get("secFutrsLawName")
        or raw.get("wtAnttnSecFutrsLawName")
        or (local_meta or {}).get("name")
    )
    label = raw.get("evltDescrib") or name
    local_file: str | None = None
    if law_id and reg_file_path(str(law_id)).exists():
        local_file = str(reg_file_path(str(law_id)).relative_to(OUTPUT_DIR))
    return {
        "id": law_id,
        "csrc_number": number,
        "version": version,
        "label": label,
        "name": name,
        "local_file": local_file,
    }


def build_revisions_document(
    version_records: dict[str, dict[str, Any]],
    uf: UnionFind,
    evidence_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build revision families only from official revision-history evidence."""
    families_raw: dict[str, dict[str, dict[str, Any]]] = {}
    evidence_records = evidence_records or []

    for law_id, node in version_records.items():
        root = uf.find(law_id)
        families_raw.setdefault(root, {})[law_id] = node

    by_law_id: dict[str, str] = {}
    families: dict[str, Any] = {}

    for nodes_map in families_raw.values():
        nodes = list(nodes_map.values())
        member_ids = sorted(str(node["id"]) for node in nodes if node.get("id"))
        if len(member_ids) == 1:
            family_key = f"id:{member_ids[0]}"
        else:
            digest = hashlib.sha1("|".join(member_ids).encode("utf-8")).hexdigest()[:20]
            family_key = f"neris:{digest}"

        sorted_nodes = sorted(
            nodes,
            key=lambda n: version_sort_key(n.get("version")),
            reverse=True,
        )
        member_set = set(member_ids)
        family_evidence = []
        for evidence in evidence_records:
            evidence_members = {
                str(value) for value in (evidence.get("member_ids") or []) if value
            }
            if len(member_set & evidence_members) >= 2:
                family_evidence.append(evidence)

        edges = []
        if family_evidence:
            for i in range(len(sorted_nodes) - 1):
                newer_version = version_sort_key(sorted_nodes[i].get("version"))
                older_version = version_sort_key(
                    sorted_nodes[i + 1].get("version")
                )
                if (
                    newer_version <= 0
                    or older_version <= 0
                    or newer_version <= older_version
                ):
                    continue
                edges.append(
                    {
                        "from": sorted_nodes[i]["id"],
                        "to": sorted_nodes[i + 1]["id"],
                        "relation": "supersedes",
                        "source": "neris.changeLaw",
                        "evidence": [
                            {
                                "queried_law_id": item.get("queried_law_id"),
                                "member_ids": item.get("member_ids") or [],
                            }
                            for item in family_evidence
                        ],
                        "confidence": 0.95,
                        "inference": "version_order_within_official_revision_group",
                    }
                )

        families[family_key] = {
            "family_id": family_key,
            "versions": sorted_nodes,
            "edges": edges,
            "evidence": family_evidence,
        }
        for node in sorted_nodes:
            if node.get("id"):
                by_law_id[str(node["id"])] = family_key

    return {
        "schema_version": 2,
        "updated_at": utc_now_iso(),
        "generation_policy": {
            "family_membership": "neris.changeLaw evltList only",
            "edge_policy": "adjacent versions ordered within an official revision group",
            "csrc_number_used_for_grouping": False,
        },
        "families": families,
        "by_law_id": by_law_id,
    }
