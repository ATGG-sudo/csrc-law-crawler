"""Resolve audited catalog overrides and relations from stable source identities."""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any

from csrc_law_crawler.sources.evidence import source_record_id


CURATED_CATALOG_PATH = Path(__file__).with_name("curated_relations.json")
SUPPORTED_RELATIONS = {
    "narrows_application_of",
    "qualifies_application_of",
    "supersedes",
}


def _source_reference_key(reference: dict[str, Any]) -> tuple[str, str]:
    source_system = str(reference.get("source_system") or "").strip()
    upstream_id = str(reference.get("upstream_id") or "").strip()
    record_id = str(reference.get("record_id") or "").strip()
    if not source_system:
        raise ValueError("curated source reference missing source_system")
    if bool(upstream_id) == bool(record_id):
        raise ValueError(
            "curated source reference requires exactly one of upstream_id or record_id"
        )
    if upstream_id:
        record_id = source_record_id(source_system, upstream_id=upstream_id)
    return source_system, record_id


def _validate_curated_catalog(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != 1:
        raise ValueError("curated catalog data requires schema_version=1")
    if "canonical_id" in json.dumps(payload, ensure_ascii=False):
        raise ValueError("curated catalog data must resolve sources, not canonical IDs")

    documents = payload.get("documents") or []
    if not isinstance(documents, list) or not documents:
        raise ValueError("curated catalog data requires documents")
    document_keys: set[str] = set()
    source_keys: dict[tuple[str, str], str] = {}
    for index, document in enumerate(documents):
        document_key = str(document.get("document_key") or "").strip()
        if not document_key:
            raise ValueError(f"curated document[{index}] missing document_key")
        if document_key in document_keys:
            raise ValueError(f"duplicate curated document_key: {document_key}")
        document_keys.add(document_key)
        sources = document.get("sources") or []
        if not isinstance(sources, list) or not sources:
            raise ValueError(f"curated document {document_key} missing sources")
        for reference in sources:
            source_key = _source_reference_key(reference)
            previous = source_keys.get(source_key)
            if previous and previous != document_key:
                raise ValueError(
                    f"curated source identity belongs to multiple documents: {source_key}"
                )
            source_keys[source_key] = document_key
        family_id = str(document.get("version_family_id") or "").strip()
        version_label = str(document.get("version_label") or "").strip()
        if bool(family_id) != bool(version_label):
            raise ValueError(
                f"curated document {document_key} must set both version_family_id and version_label"
            )
        overrides = document.get("metadata_overrides") or {}
        evidence = document.get("metadata_evidence") or {}
        if not isinstance(overrides, dict) or not isinstance(evidence, dict):
            raise ValueError(f"curated document {document_key} has invalid metadata override")
        if overrides and not str(evidence.get("official_url") or "").startswith("https://"):
            raise ValueError(
                f"curated document {document_key} metadata override missing official_url"
            )

    relations = payload.get("relations") or []
    if not isinstance(relations, list) or not relations:
        raise ValueError("curated catalog data requires relations")
    relation_keys: set[str] = set()
    for index, item in enumerate(relations):
        relation_key = str(item.get("relation_key") or "").strip()
        relation = str(item.get("relation") or "").strip()
        from_document = str(item.get("from_document") or "").strip()
        to_document = str(item.get("to_document") or "").strip()
        evidence = item.get("evidence") or {}
        if not relation_key or relation_key in relation_keys:
            raise ValueError(f"curated relation[{index}] has missing or duplicate relation_key")
        relation_keys.add(relation_key)
        if relation not in SUPPORTED_RELATIONS:
            raise ValueError(f"curated relation {relation_key} has unsupported relation")
        if from_document not in document_keys or to_document not in document_keys:
            raise ValueError(f"curated relation {relation_key} references unknown document")
        if from_document == to_document:
            raise ValueError(f"curated relation {relation_key} is a self relation")
        if not isinstance(evidence, dict):
            raise ValueError(f"curated relation {relation_key} has invalid evidence")
        if not str(evidence.get("source") or "").strip():
            raise ValueError(f"curated relation {relation_key} missing evidence source")
        if not str(evidence.get("official_url") or "").startswith("https://"):
            raise ValueError(f"curated relation {relation_key} missing official_url")
        confidence = evidence.get("confidence", 1.0)
        if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
            raise ValueError(f"curated relation {relation_key} has invalid confidence")
        if relation == "supersedes" and evidence.get("scope") != "full_version":
            raise ValueError(f"curated relation {relation_key} must be full_version")
        if relation == "narrows_application_of":
            if evidence.get("scope") != "provision_issue":
                raise ValueError(f"curated relation {relation_key} must be provision_issue")
            if not evidence.get("target_provision"):
                raise ValueError(f"curated relation {relation_key} missing target_provision")
            if evidence.get("effect_on_target") != "remains_current":
                raise ValueError(
                    f"curated relation {relation_key} must preserve target effectiveness"
                )
        if relation == "qualifies_application_of" and evidence.get("scope") != "reference-only":
            raise ValueError(f"curated relation {relation_key} must be reference-only")


@lru_cache(maxsize=1)
def _load_default_curated_catalog() -> dict[str, Any]:
    payload = json.loads(CURATED_CATALOG_PATH.read_text(encoding="utf-8"))
    _validate_curated_catalog(payload)
    return payload


def load_curated_catalog(path: Path | None = None) -> dict[str, Any]:
    """Load and validate the audited source-key configuration."""
    if path is None:
        return _load_default_curated_catalog()
    payload = json.loads(path.read_text(encoding="utf-8"))
    _validate_curated_catalog(payload)
    return payload


def curated_documents(
    payload: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    data = payload or load_curated_catalog()
    return {str(item["document_key"]): item for item in data.get("documents") or []}


def curated_document_source_keys(document: dict[str, Any]) -> set[tuple[str, str]]:
    return {_source_reference_key(item) for item in document.get("sources") or []}


def resolve_curated_documents(
    source_to_entity: dict[tuple[str, str], str],
    payload: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Resolve logical documents to canonical IDs without storing those IDs in data."""
    resolved: dict[str, str] = {}
    for document_key, document in curated_documents(payload).items():
        entity_ids = {
            source_to_entity[source_key]
            for source_key in curated_document_source_keys(document)
            if source_key in source_to_entity
        }
        if len(entity_ids) > 1:
            raise ValueError(
                f"curated document {document_key} sources resolved to multiple entities: "
                + ", ".join(sorted(entity_ids))
            )
        if entity_ids:
            resolved[document_key] = next(iter(entity_ids))
    return resolved


def resolve_curated_catalog_relations(
    source_to_entity: dict[tuple[str, str], str],
    payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return relations whose two stable source identities exist in this catalog run."""
    data = payload or load_curated_catalog()
    resolved = resolve_curated_documents(source_to_entity, data)
    result: list[dict[str, Any]] = []
    for item in data.get("relations") or []:
        from_document = str(item["from_document"])
        to_document = str(item["to_document"])
        from_id = resolved.get(from_document)
        to_id = resolved.get(to_document)
        if not from_id or not to_id:
            continue
        evidence = {
            **(item.get("evidence") or {}),
            "curated_relation_key": item["relation_key"],
            "from_document": from_document,
            "to_document": to_document,
        }
        result.append(
            {
                "from": from_id,
                "to": to_id,
                "relation": item["relation"],
                "evidence": evidence,
            }
        )
    return result


def _entity_source_keys(entity: dict[str, Any]) -> set[tuple[str, str]]:
    result = {
        (str(item.get("system")), str(item.get("record_id")))
        for item in entity.get("sources") or []
        if item.get("system") and item.get("record_id")
    }
    preferred = entity.get("preferred_content") or {}
    if preferred.get("source_system") and preferred.get("source_record_id"):
        result.add((str(preferred["source_system"]), str(preferred["source_record_id"])))
    return result


def curated_document_for_entity(
    entity: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    entity_sources = _entity_source_keys(entity)
    matches = [
        document
        for document in curated_documents(payload).values()
        if entity_sources.intersection(curated_document_source_keys(document))
    ]
    if len(matches) > 1:
        keys = sorted(str(item["document_key"]) for item in matches)
        raise ValueError("canonical entity merged distinct curated documents: " + ", ".join(keys))
    return matches[0] if matches else None


def apply_curated_metadata_overrides(
    entity: dict[str, Any],
    metadata: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply source-keyed metadata corrections and retain field-level audit evidence."""
    document = curated_document_for_entity(entity, payload)
    overrides = (document or {}).get("metadata_overrides") or {}
    if not overrides:
        return metadata
    assert document is not None
    result = {**metadata, **overrides}
    audit_item = {
        "document_key": document["document_key"],
        "fields": sorted(overrides),
        **((document or {}).get("metadata_evidence") or {}),
    }
    audit_items = list(result.get("curated_override_evidence") or [])
    if audit_item not in audit_items:
        audit_items.append(audit_item)
    result["curated_override_evidence"] = audit_items
    return result


def curated_version_ref_for_entity(
    entity: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    document = curated_document_for_entity(entity, payload)
    if not document:
        return None
    family_id = str(document.get("version_family_id") or "").strip()
    if not family_id:
        return None
    return {
        "family_id": family_id,
        "version_label": document.get("version_label"),
        "document_key": document.get("document_key"),
        "source": "curated.catalog_relations",
    }


__all__ = [
    "CURATED_CATALOG_PATH",
    "apply_curated_metadata_overrides",
    "curated_document_for_entity",
    "curated_document_source_keys",
    "curated_documents",
    "curated_version_ref_for_entity",
    "load_curated_catalog",
    "resolve_curated_catalog_relations",
    "resolve_curated_documents",
]
