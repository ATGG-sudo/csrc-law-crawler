"""Deterministic case grouping for AMAC enforcement documents."""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
from typing import Any


def enforcement_document_role(entity: dict[str, Any]) -> str:
    title = str(entity.get("title") or "")
    classification = entity.get("enforcement_classification") or {}
    category = str(classification.get("category") or "")
    subtype = str(classification.get("subtype") or "")
    if "送达公告" in title or "公告送达" in title:
        return "service_announcement"
    if subtype in {
        "disciplinary_decision",
        "disciplinary_prior_notice",
        "disciplinary_review_decision",
    }:
        return subtype
    if category in {
        "self_regulatory_measure",
        "abnormal_operation",
        "missing_institution",
    }:
        return category
    return "other_enforcement_document"


def _case_anchor(component: list[str], entities: dict[str, dict[str, Any]]) -> str:
    asset_shas = sorted(
        {
            str(asset.get("sha256"))
            for entity_id in component
            for asset in entities[entity_id].get("assets") or []
            if asset.get("sha256")
        }
    )
    if asset_shas:
        return f"asset:{asset_shas[0]}"
    source_ids = sorted(
        {
            f"{source.get('system')}:{source.get('record_id')}"
            for entity_id in component
            for source in entities[entity_id].get("sources") or []
            if source.get("system") and source.get("record_id")
        }
    )
    return source_ids[0] if source_ids else f"entity:{component[0]}"


def _case_id(component: list[str], entities: dict[str, dict[str, Any]]) -> str:
    anchor = _case_anchor(component, entities)
    digest = hashlib.sha256(f"amac-case:{anchor}".encode("utf-8")).hexdigest()
    return f"case_{digest[:24]}"


def annotate_enforcement_cases(
    entities: dict[str, dict[str, Any]],
    relations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assign one case id to enforcement documents joined by AMAC attachment edges."""
    enforcement_ids = {
        entity_id
        for entity_id, entity in entities.items()
        if entity.get("enforcement_classification")
    }
    neighbors: dict[str, set[str]] = defaultdict(set)
    for relation in relations:
        if (
            relation.get("relation") != "publishes"
            or relation.get("source") != "amac.page_attachment"
        ):
            continue
        left = str(relation.get("from") or "")
        right = str(relation.get("to") or "")
        if left in enforcement_ids and right in enforcement_ids:
            neighbors[left].add(right)
            neighbors[right].add(left)

    visited: set[str] = set()
    role_counts: Counter[str] = Counter()
    case_count = 0
    for start in sorted(enforcement_ids):
        if start in visited:
            continue
        stack = [start]
        component: list[str] = []
        visited.add(start)
        while stack:
            entity_id = stack.pop()
            component.append(entity_id)
            for neighbor in sorted(neighbors.get(entity_id) or set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
        case_count += 1
        case_id = _case_id(sorted(component), entities)
        for entity_id in component:
            role = enforcement_document_role(entities[entity_id])
            entities[entity_id]["case_id"] = case_id
            entities[entity_id]["document_role"] = role
            role_counts[role] += 1

    return {
        "case_count": case_count,
        "document_count": len(enforcement_ids),
        "document_role_counts": dict(sorted(role_counts.items())),
    }
