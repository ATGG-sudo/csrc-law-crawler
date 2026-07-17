"""Catalog manifest and review queue helpers."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from catalog_rules import (
    REVIEW_EFFECTIVENESS_UNKNOWN,
    REVIEW_SOURCE_MATCH_AMBIGUOUS,
    REVIEW_SOURCE_MATCH_LOW_CONFIDENCE,
    SOURCE_MATCH_REVIEW_CONFIDENCE_THRESHOLD,
    catalog_rule_calibration,
    catalog_rules_manifest,
)
from storage import catalog_laws_dir, relative_to_output, utc_now_iso

def _match_counts(matches: dict[str, dict[str, Any]]) -> dict[str, int]:
    match_counts: dict[str, int] = defaultdict(int)
    for item in matches.values():
        match_counts[str(item["match_status"])] += 1
    return dict(sorted(match_counts.items()))

def _catalog_manifest_items(entities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for entity_id, entity in sorted(entities.items()):
        material = entity.get("material_classification") or {}
        items.append(
            {
                "id": entity_id,
                "title": entity.get("title"),
                "document_type": entity.get("document_type"),
                "status": entity.get("status"),
                "material_lane": material.get("lane"),
                "material_category": material.get("category"),
                "material_basis": material.get("basis"),
                "material_confidence": material.get("confidence"),
                "sources": len(entity.get("sources") or []),
                "file": relative_to_output(catalog_laws_dir() / f"{entity_id}.json"),
            }
        )
    return items

def _review_queue_items(
    amac_records: list[dict[str, Any]],
    *,
    source_to_entity: dict[tuple[str, str], str],
    matches: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    review_items = []
    for record in amac_records:
        match = matches.get(record["record_id"]) or {}
        metadata = record.get("metadata") or {}
        reasons = []
        rule_ids = []
        if match.get("match_status") == "ambiguous":
            reasons.append("source_match_ambiguous")
            rule_ids.append(REVIEW_SOURCE_MATCH_AMBIGUOUS.rule_id)
        match_confidence = float(match.get("confidence") or 0.0)
        if (
            match
            and match.get("match_status") != "ambiguous"
            and match_confidence < SOURCE_MATCH_REVIEW_CONFIDENCE_THRESHOLD
        ):
            reasons.append("source_match_low_confidence")
            rule_ids.append(REVIEW_SOURCE_MATCH_LOW_CONFIDENCE.rule_id)
        if metadata.get("document_type") == "self_regulatory_rule" and metadata.get("status") in {
            None,
            "",
            "unknown",
        }:
            reasons.append("effectiveness_unknown")
            rule_ids.append(REVIEW_EFFECTIVENESS_UNKNOWN.rule_id)
        if reasons:
            review_items.append(
                {
                    "source_record_id": record["record_id"],
                    "canonical_id": source_to_entity.get(("amac", record["record_id"])),
                    "name": metadata.get("name"),
                    "reasons": reasons,
                    "rule_ids": rule_ids,
                    "match_status": match.get("match_status"),
                    "match_rule_id": match.get("match_rule_id"),
                    "match_confidence": match.get("confidence"),
                    "source_url": record.get("page_url"),
                }
            )
    return review_items

def _catalog_manifest(
    *,
    neris_records: list[dict[str, Any]],
    amac_records: list[dict[str, Any]],
    entities: dict[str, dict[str, Any]],
    relations: list[dict[str, Any]],
    review_items: list[dict[str, Any]],
    matches: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "neris_source_records": len(neris_records),
        "amac_source_records": len(amac_records),
        "canonical_laws": len(entities),
        "relations": len(relations),
        "review_queue": len(review_items),
        "match_counts": _match_counts(matches),
        "laws_dir": relative_to_output(catalog_laws_dir()),
        "rules": catalog_rules_manifest(),
        "rule_calibration": catalog_rule_calibration(),
        "items": _catalog_manifest_items(entities),
    }


catalog_manifest = _catalog_manifest
catalog_manifest_items = _catalog_manifest_items
match_counts = _match_counts
review_queue_items = _review_queue_items
