"""Catalog relation assembly helpers."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from catalog_rules import (
    RELATION_AMAC_PAGE_ATTACHMENT,
    RELATION_NERIS_TITLE_QUOTED_DOCUMENT,
)
from catalog_services import CatalogRelationIngestor

from .curated_relations import resolve_curated_catalog_relations
from .identity import PUBLISHING_TITLE_RE, QUOTED_TITLE_RE, normalize_title
from .matching import (
    infer_draft_finalization_relations,
    infer_explicit_successor_relations,
    infer_known_successor_relations,
    infer_same_instrument_relations,
    infer_trial_replacement_relations,
)


def _add_amac_page_attachment_relations(
    amac_records: list[dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
    relation_ingestor: CatalogRelationIngestor,
) -> None:
    for record in amac_records:
        parent_id = record.get("parent_record_id")
        if not parent_id:
            continue
        parent_entity = source_to_entity.get(("amac", str(parent_id)))
        child_entity = source_to_entity.get(("amac", record["record_id"]))
        if parent_entity and child_entity:
            relation_ingestor.add(
                parent_entity,
                child_entity,
                "publishes",
                {
                    "source": "amac.page_attachment",
                    "rule_id": RELATION_AMAC_PAGE_ATTACHMENT.rule_id,
                    "parent_source_record_id": parent_id,
                    "attachment_source_record_id": record["record_id"],
                    "confidence": RELATION_AMAC_PAGE_ATTACHMENT.confidence,
                },
            )


def _add_neris_title_relations(
    neris_records: list[dict[str, Any]],
    entities: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
    relation_ingestor: CatalogRelationIngestor,
) -> None:
    entity_title_index: dict[str, list[str]] = defaultdict(list)
    for entity_id, entity in entities.items():
        entity_title_index[normalize_title(entity.get("title"))].append(entity_id)
    for record in neris_records:
        parent_entity = source_to_entity[("neris", record["record_id"])]
        title = str((record.get("metadata") or {}).get("name") or "")
        if not PUBLISHING_TITLE_RE.search(title):
            continue
        for quoted in QUOTED_TITLE_RE.findall(title):
            candidates = entity_title_index.get(normalize_title(quoted)) or []
            if len(candidates) == 1:
                relation_ingestor.add(
                    parent_entity,
                    candidates[0],
                    "publishes",
                    {
                        "source": "neris.title",
                        "rule_id": RELATION_NERIS_TITLE_QUOTED_DOCUMENT.rule_id,
                        "quoted_title": quoted,
                        "confidence": RELATION_NERIS_TITLE_QUOTED_DOCUMENT.confidence,
                    },
                )


def _add_trial_replacement_relations(
    entities: dict[str, dict[str, Any]],
    relation_ingestor: CatalogRelationIngestor,
) -> None:
    for relation in infer_trial_replacement_relations(entities):
        relation_ingestor.add(
            str(relation["from"]),
            str(relation["to"]),
            str(relation["relation"]),
            {
                "source": relation.get("source"),
                "confidence": relation.get("confidence"),
                **(relation.get("evidence") or {}),
            },
        )


def _add_known_successor_relations(
    entities: dict[str, dict[str, Any]],
    relation_ingestor: CatalogRelationIngestor,
) -> None:
    for relation in infer_known_successor_relations(entities):
        relation_ingestor.add(
            str(relation["from"]),
            str(relation["to"]),
            str(relation["relation"]),
            {
                "source": relation.get("source"),
                "confidence": relation.get("confidence"),
                **(relation.get("evidence") or {}),
            },
        )


def _add_inferred_relations(
    entities: dict[str, dict[str, Any]],
    relation_ingestor: CatalogRelationIngestor,
) -> None:
    for infer in (
        infer_draft_finalization_relations,
        infer_same_instrument_relations,
        infer_explicit_successor_relations,
    ):
        for relation in infer(entities):
            from_id = str(relation["from"])
            to_id = str(relation["to"])
            relation_type = str(relation["relation"])
            if relation_type == "same_instrument_copy" and (
                (from_id, to_id, "supersedes") in relation_ingestor.keys
                or (to_id, from_id, "supersedes") in relation_ingestor.keys
            ):
                continue
            relation_ingestor.add(
                from_id,
                to_id,
                relation_type,
                {
                    "source": relation.get("source"),
                    "rule_id": relation.get("rule_id"),
                    "confidence": relation.get("confidence"),
                    **(relation.get("evidence") or {}),
                },
            )


def _add_curated_relations(
    source_to_entity: dict[tuple[str, str], str],
    relation_ingestor: CatalogRelationIngestor,
) -> None:
    for relation in resolve_curated_catalog_relations(source_to_entity):
        relation_ingestor.add(
            str(relation["from"]),
            str(relation["to"]),
            str(relation["relation"]),
            relation["evidence"],
        )


def _build_catalog_relations(
    *,
    neris_records: list[dict[str, Any]],
    amac_records: list[dict[str, Any]],
    entities: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
) -> list[dict[str, Any]]:
    relation_ingestor = CatalogRelationIngestor()
    # Curated edges go first so audited evidence wins the ingestor's edge-level
    # dedupe if a later heuristic happens to infer the same endpoints.
    _add_curated_relations(source_to_entity, relation_ingestor)
    _add_amac_page_attachment_relations(amac_records, source_to_entity, relation_ingestor)
    _add_neris_title_relations(
        neris_records,
        entities,
        source_to_entity,
        relation_ingestor,
    )
    _add_trial_replacement_relations(entities, relation_ingestor)
    _add_known_successor_relations(entities, relation_ingestor)
    _add_inferred_relations(entities, relation_ingestor)
    return relation_ingestor.items


add_amac_page_attachment_relations = _add_amac_page_attachment_relations
add_curated_relations = _add_curated_relations
add_known_successor_relations = _add_known_successor_relations
add_neris_title_relations = _add_neris_title_relations
add_trial_replacement_relations = _add_trial_replacement_relations
build_catalog_relations = _build_catalog_relations
