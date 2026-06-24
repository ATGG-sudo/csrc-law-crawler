"""历次修订族合并与 supersedes 边生成。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
        local_file = str(Path("laws") / f"reg_{law_id}.json")
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
) -> dict[str, Any]:
    """按 union-find 根合并版本节点，生成 families / edges / by_law_id。"""
    families_raw: dict[str, dict[str, dict[str, Any]]] = {}

    for law_id, node in version_records.items():
        root = uf.find(law_id)
        families_raw.setdefault(root, {})[law_id] = node

    family_buckets: dict[str, dict[str, dict[str, Any]]] = {}
    by_law_id: dict[str, str] = {}

    for _root, nodes_map in families_raw.items():
        nodes = list(nodes_map.values())
        numbers = {str(n["csrc_number"]) for n in nodes if n.get("csrc_number")}
        if len(numbers) == 1:
            family_key = next(iter(numbers))
        elif numbers:
            family_key = sorted(numbers)[0]
        else:
            family_key = f"id:{nodes[0]['id']}"

        # Multiple union-find roots can share the same CSRC number.  The CSRC
        # number is our public family key, so merge those roots instead of
        # overwriting the earlier family.
        bucket = family_buckets.setdefault(family_key, {})
        for law_id, node in nodes_map.items():
            bucket[law_id] = {**bucket.get(law_id, {}), **node}

    families: dict[str, Any] = {}
    for family_key, nodes_map in family_buckets.items():
        nodes = list(nodes_map.values())
        sorted_nodes = sorted(
            nodes,
            key=lambda n: version_sort_key(n.get("version")),
            reverse=True,
        )
        edges = []
        for i in range(len(sorted_nodes) - 1):
            edges.append(
                {
                    "from": sorted_nodes[i]["id"],
                    "to": sorted_nodes[i + 1]["id"],
                    "relation": "supersedes",
                }
            )

        families[family_key] = {
            "family_key": family_key,
            "versions": sorted_nodes,
            "edges": edges,
        }
        for node in sorted_nodes:
            if node.get("id"):
                by_law_id[str(node["id"])] = family_key

    return {
        "updated_at": utc_now_iso(),
        "families": families,
        "by_law_id": by_law_id,
    }
