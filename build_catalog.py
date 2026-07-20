#!/usr/bin/env python3
"""Match NERIS and AMAC source records and build source-independent law entities."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from asset_text import extract_local_asset_text
from catalog_rules import catalog_rule_calibration, catalog_rules_manifest
from catalog_services import CatalogEntityWriter, CatalogMatcher, CatalogSourceLoader
from csrc_law_crawler.processing.catalog import entities as _catalog_entities
from csrc_law_crawler.processing.catalog import identity as _catalog_identity
from csrc_law_crawler.processing.catalog import manifest as _catalog_manifest_helpers
from csrc_law_crawler.processing.catalog import matching as _catalog_matching
from csrc_law_crawler.processing.catalog import relations as _catalog_relations
from csrc_law_crawler.processing.catalog.cases import annotate_enforcement_cases
from csrc_law_crawler.processing.catalog.classification import (
    enforcement_classification_for,
    load_classification_overrides,
    material_classification_for,
    reference_lifecycle_for,
    source_web_classification,
)
from csrc_law_crawler.sources.registry import load_registry
from parser import (
    infer_effective_date,
    infer_pub_date,
    ms_to_date,
    repair_known_neris_mojibake,
)
from runtime import log_event
from storage import (
    attachment_index_path,
    catalog_dir,
    catalog_laws_dir,
    catalog_relations_path,
    iter_amac_source_files,
    iter_reg_law_files,
    load_json,
    output_path,
    relative_to_output,
    reports_dir,
    run_with_output_lock,
    save_json,
    source_matches_path,
)

# Legacy re-exports from _catalog_entities.
_announcement_mentions_multiple_titles = _catalog_entities._announcement_mentions_multiple_titles
_append_neris_merge_metadata = _catalog_entities._append_neris_merge_metadata
_append_record_assets = _catalog_entities._append_record_assets
_append_record_source = _catalog_entities._append_record_source
_append_unique_assets = _catalog_entities._append_unique_assets
_append_unique_sources = _catalog_entities._append_unique_sources
_asset_duplicate_groups = _catalog_entities._asset_duplicate_groups
_body_duplicate_groups = _catalog_entities._body_duplicate_groups
_body_hash = _catalog_entities._body_hash
_choose_kept_entity_id = _catalog_entities._choose_kept_entity_id
_compact_body_text = _catalog_entities._compact_body_text
_dates_match_for_merge = _catalog_entities._dates_match_for_merge
_dedupe_title_key = _catalog_entities._dedupe_title_key
_entity_asset_shas = _catalog_entities._entity_asset_shas
_entity_completeness = _catalog_entities._entity_completeness
_entity_dedupe_title_key = _catalog_entities._entity_dedupe_title_key
_entity_from_record = _catalog_entities._entity_from_record
_entity_sources_key = _catalog_entities._entity_sources_key
_find_existing_amac_entity = _catalog_entities._find_existing_amac_entity
_find_existing_neris_entity = _catalog_entities._find_existing_neris_entity
_has_neris_source = _catalog_entities._has_neris_source
_is_multi_document_announcement_body = _catalog_entities._is_multi_document_announcement_body
_low_signal_plain_text = _catalog_entities._low_signal_plain_text
_merge_amac_record = _catalog_entities._merge_amac_record
_merge_catalog_entity = _catalog_entities._merge_catalog_entity
_merge_record_metadata = _catalog_entities._merge_record_metadata
_merge_equivalent_groups = _catalog_entities._merge_equivalent_groups
_match_amac_records = _catalog_entities._match_amac_records
_neris_merge_index_key = _catalog_entities._neris_merge_index_key
_neris_merge_source_metadata = _catalog_entities._neris_merge_source_metadata
_prefer_longer_record_content = _catalog_entities._prefer_longer_record_content
_record_assets = _catalog_entities._record_assets
_repair_multi_document_announcement_content = (
    _catalog_entities._repair_multi_document_announcement_content
)
_seed_neris_entities = _catalog_entities._seed_neris_entities
_source_descriptor = _catalog_entities._source_descriptor
_source_file_sha256 = _catalog_entities._source_file_sha256
_source_records_for_entity = _catalog_entities._source_records_for_entity
deduplicate_catalog_entities = _catalog_entities.deduplicate_catalog_entities

# Legacy re-exports from _catalog_identity.
ATTACHMENT_PREFIX_RE = _catalog_identity.ATTACHMENT_PREFIX_RE
ATTACHMENT_TEXT_SIGNAL_RE = _catalog_identity.ATTACHMENT_TEXT_SIGNAL_RE
DEDUP_MIN_BODY_CHARS = _catalog_identity.DEDUP_MIN_BODY_CHARS
FILE_SUFFIX_RE = _catalog_identity.FILE_SUFFIX_RE
LEADING_ITEM_MARKER_RE = _catalog_identity.LEADING_ITEM_MARKER_RE
PUBLISHING_TITLE_RE = _catalog_identity.PUBLISHING_TITLE_RE
QUOTED_TITLE_RE = _catalog_identity.QUOTED_TITLE_RE
REVISION_MARKER_RE = _catalog_identity.REVISION_MARKER_RE
SECTION_TOKEN_RE = _catalog_identity.SECTION_TOKEN_RE
SPACE_PUNCT_RE = _catalog_identity.SPACE_PUNCT_RE
TRIAL_MARKER_RE = _catalog_identity.TRIAL_MARKER_RE
_date_distance = _catalog_identity._date_distance
_date_sort_value = _catalog_identity._date_sort_value
canonical_id = _catalog_identity.canonical_id
clean_title = _catalog_identity.clean_title
is_trial_title = _catalog_identity.is_trial_title
normalize_fileno = _catalog_identity.normalize_fileno
normalize_title = _catalog_identity.normalize_title
normalize_title_without_trial = _catalog_identity.normalize_title_without_trial

# Legacy re-exports from _catalog_manifest_helpers.
_catalog_manifest = _catalog_manifest_helpers._catalog_manifest
_catalog_manifest_items = _catalog_manifest_helpers._catalog_manifest_items
_match_counts = _catalog_manifest_helpers._match_counts
_review_queue_items = _catalog_manifest_helpers._review_queue_items

# Legacy re-exports from _catalog_matching.
KNOWN_SUCCESSOR_CHAINS = _catalog_matching.KNOWN_SUCCESSOR_CHAINS
_is_official_rule_entity = _catalog_matching._is_official_rule_entity
_known_successor_key = _catalog_matching._known_successor_key
_normalized_org = _catalog_matching._normalized_org
_pub_date_value = _catalog_matching._pub_date_value
choose_neris_match = _catalog_matching.choose_neris_match
choose_neris_match_with_rule = _catalog_matching.choose_neris_match_with_rule
infer_known_successor_relations = _catalog_matching.infer_known_successor_relations
infer_trial_replacement_relations = _catalog_matching.infer_trial_replacement_relations
infer_draft_finalization_relations = _catalog_matching.infer_draft_finalization_relations
infer_same_instrument_relations = _catalog_matching.infer_same_instrument_relations
infer_explicit_successor_relations = _catalog_matching.infer_explicit_successor_relations

# Legacy re-exports from _catalog_relations.
_add_amac_page_attachment_relations = _catalog_relations._add_amac_page_attachment_relations
_add_known_successor_relations = _catalog_relations._add_known_successor_relations
_add_neris_title_relations = _catalog_relations._add_neris_title_relations
_add_trial_replacement_relations = _catalog_relations._add_trial_replacement_relations
_build_catalog_relations = _catalog_relations._build_catalog_relations


def catalog_manifest_path() -> Path:
    return catalog_dir() / "manifest.json"


def catalog_review_queue_path() -> Path:
    return reports_dir() / "review_queue.json"


def _repair_text_fields(value: Any) -> Any:
    if isinstance(value, str):
        return repair_known_neris_mojibake(value)
    if isinstance(value, dict):
        return {key: _repair_text_fields(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_repair_text_fields(item) for item in value]
    return value


def _asset_text_fallback(metadata: dict[str, Any], local_file: Any) -> str:
    local_file_text = str(local_file or "")
    if not local_file_text:
        return ""
    return extract_local_asset_text(output_path(local_file_text))


def _record_plain_text(
    metadata: dict[str, Any],
    plain_text: Any,
    local_file: Any = None,
) -> str:
    text = repair_known_neris_mojibake(str(plain_text or ""))
    if text.strip() and not _low_signal_plain_text(metadata, text):
        return text
    fallback = _asset_text_fallback(metadata, local_file)
    return fallback if fallback else text


def _neris_records() -> list[dict[str, Any]]:
    records = []
    for path in iter_reg_law_files():
        doc = load_json(path, {})
        metadata = _repair_text_fields(doc.get("metadata") or {})
        summary_date_ms = ((doc.get("source") or {}).get("list_summary") or {}).get(
            "pub_date_ms"
        )
        if summary_date_ms is not None:
            metadata["pub_date"] = ms_to_date(summary_date_ms)
        plain_text = repair_known_neris_mojibake(doc.get("full_text") or "")
        metadata["effective_date"] = infer_effective_date(metadata, plain_text)
        record_id = str(metadata.get("id") or path.stem.removeprefix("reg_"))
        attachment_index = load_json(attachment_index_path(record_id), {})
        record = {
            "system": "neris",
            "record_id": record_id,
            "metadata": metadata,
            "plain_text": plain_text,
            "local_file": relative_to_output(path),
            "page_url": (doc.get("source") or {}).get("detail_url"),
            "assets": (attachment_index.get("attachments") or doc.get("source_attachments") or []),
        }
        record.update(
            source_web_classification(
                metadata,
                page_url=record.get("page_url"),
                material_lane="rule",
            )
        )
        records.append(record)
    return records


def _amac_records() -> list[dict[str, Any]]:
    records = []
    for path in iter_amac_source_files():
        doc = load_json(path, {})
        record_id = str(doc.get("source_record_id") or path.stem)
        record = {
            "system": "amac",
            "record_id": record_id,
            "metadata": _repair_text_fields(doc.get("metadata") or {}),
            "local_file": relative_to_output(path),
            "page_url": (doc.get("source") or {}).get("page_url"),
            "assets": doc.get("assets") or [],
            "parent_record_id": None,
        }
        record["metadata"]["pub_date"] = infer_pub_date(
            record["metadata"], record.get("page_url")
        )
        record.update(
            source_web_classification(
                record["metadata"],
                page_url=record.get("page_url"),
                material_lane=str(doc.get("material_lane") or "") or None,
            )
        )
        records.append(record)
        records[-1]["plain_text"] = _record_plain_text(
            records[-1]["metadata"],
            (doc.get("content") or {}).get("plain_text"),
            records[-1]["local_file"],
        )
        records[-1]["metadata"]["effective_date"] = infer_effective_date(
            records[-1]["metadata"], records[-1]["plain_text"]
        )
        for attachment in doc.get("attachment_documents") or []:
            attachment_id = str(attachment.get("source_record_id") or "")
            if not attachment_id:
                continue
            attachment_metadata = _repair_text_fields(dict(attachment.get("metadata") or {}))
            parent_metadata = dict(records[-1]["metadata"])
            if attachment_metadata.get("status") in {None, "", "unknown"}:
                parent_status = parent_metadata.get("status")
                if parent_status not in {None, "", "unknown"}:
                    attachment_metadata["status"] = parent_status
            for field in ("effective_date", "ineffective_date"):
                if not attachment_metadata.get(field) and parent_metadata.get(field):
                    attachment_metadata[field] = parent_metadata.get(field)
            local_file = next(
                (
                    asset.get("local_file")
                    for asset in (doc.get("assets") or [])
                    if asset.get("asset_id") == attachment.get("asset_id")
                ),
                None,
            )
            attachment_record = {
                "system": "amac",
                "record_id": attachment_id,
                "metadata": attachment_metadata,
                "plain_text": _record_plain_text(
                    attachment_metadata,
                    (attachment.get("content") or {}).get("plain_text"),
                    local_file,
                ),
                "local_file": local_file,
                "page_url": (attachment.get("source") or {}).get("asset_url"),
                "assets": [],
                "parent_record_id": record_id,
            }
            attachment_record.update(
                source_web_classification(
                    attachment_metadata,
                    page_url=attachment_record.get("page_url"),
                    material_lane=str(doc.get("material_lane") or "") or None,
                )
            )
            records.append(attachment_record)
    return records


def _multi_source_catalog_records() -> list[dict[str, Any]]:
    """Load publishable rule and reference records from the evidence store."""
    root = output_path("raw/sources/records")
    records: list[dict[str, Any]] = []
    if not root.exists():
        return records
    registry_endpoints = {
        endpoint["endpoint_id"]: endpoint for endpoint in load_registry()["endpoints"]
    }
    restricted_endpoints = {
        endpoint["endpoint_id"]
        for endpoint in registry_endpoints.values()
        if endpoint["scope_mode"] in {"catalog_filter", "query_exhaustive"}
    }
    for path in sorted(root.glob("*/*.json")):
        doc = load_json(path, {})
        material_lane = str(doc.get("material_lane") or "")
        if doc.get("ingest_status") != "complete" or material_lane not in {
            "rule",
            "reference",
        }:
            continue
        content = doc.get("content") or {}
        plain_text = repair_known_neris_mojibake(str(content.get("plain_text") or ""))
        if not plain_text.strip():
            continue
        source = doc.get("source") or {}
        if (
            source.get("endpoint_id") in restricted_endpoints
            and source.get("scope_status") != "matched"
        ):
            continue
        endpoint = registry_endpoints.get(str(source.get("endpoint_id") or "")) or {}
        profiles = endpoint.get("profiles") or []
        metadata = _repair_text_fields(doc.get("metadata") or {})
        metadata["pub_date"] = infer_pub_date(metadata, source.get("page_url"))
        metadata["effective_date"] = infer_effective_date(metadata, plain_text)
        native_source_category = metadata.get("source_category")
        metadata.setdefault("material_lane", material_lane)
        metadata.setdefault("source_endpoint_id", source.get("endpoint_id"))
        metadata.setdefault(
            "source_category",
            next(
                (
                    profile.get("material_nature")
                    for profile in profiles
                    if profile.get("material_nature")
                ),
                metadata.get("document_type"),
            ),
        )
        if not native_source_category and metadata.get("source_category"):
            metadata.setdefault("source_category_provenance", "endpoint_profile")
        source_sections = sorted(
            {
                str(item.get("list_url"))
                for item in source.get("discovery_evidence") or []
                if item.get("list_url")
            }
        )
        if source_sections:
            metadata.setdefault("source_section", source_sections[0])
            metadata.setdefault("source_sections", source_sections)
        source_types = sorted(
            {
                str(profile.get("effect") or profile.get("material_nature"))
                for profile in profiles
                if profile.get("effect") or profile.get("material_nature")
            }
        )
        if source_types:
            metadata.setdefault("source_types", source_types)
        record = {
            "system": str(doc.get("source_system") or path.parent.name),
            "record_id": str(doc.get("source_record_id") or path.stem),
            "metadata": metadata,
            "plain_text": plain_text,
            "local_file": relative_to_output(path),
            "page_url": source.get("page_url"),
            "assets": doc.get("assets") or [],
            "material_lane": material_lane,
        }
        record.update(
            source_web_classification(
                metadata,
                page_url=record.get("page_url"),
                material_lane=material_lane,
                endpoint_profiles=profiles,
            )
        )
        records.append(record)
    return records


def _multi_source_rule_records() -> list[dict[str, Any]]:
    """Compatibility helper retained for callers that only consume rules."""
    return [
        record
        for record in _multi_source_catalog_records()
        if record.get("material_lane") == "rule"
    ]


def _merge_multi_source_rules(
    records: list[dict[str, Any]],
    entities: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
) -> None:
    """Merge exact official duplicates; otherwise keep a source-owned entity."""
    for record in records:
        metadata = record.get("metadata") or {}
        title_keys = _catalog_identity.instrument_title_keys(metadata.get("name"))
        fileno_keys = _catalog_identity.fileno_keys(metadata.get("fileno"))
        candidates: list[tuple[str, dict[str, Any]]] = []
        for entity_id, entity in entities.items():
            entity_metadata = entity.get("metadata") or {}
            entity_title_keys = _catalog_identity.instrument_title_keys(
                entity_metadata.get("name") or entity.get("title")
            )
            if not title_keys.intersection(entity_title_keys):
                continue
            entity_fileno_keys = _catalog_identity.fileno_keys(entity_metadata.get("fileno"))
            if fileno_keys and entity_fileno_keys and not fileno_keys.intersection(
                entity_fileno_keys
            ):
                continue
            left_date = metadata.get("pub_date")
            right_date = entity_metadata.get("pub_date")
            if left_date and right_date and not _dates_match_for_merge(left_date, right_date):
                continue
            candidates.append((entity_id, entity))

        if fileno_keys:
            numbered_candidates = [
                (entity_id, entity)
                for entity_id, entity in candidates
                if fileno_keys.intersection(
                    _catalog_identity.fileno_keys(
                        (entity.get("metadata") or {}).get("fileno")
                    )
                )
            ]
            if numbered_candidates:
                candidates = numbered_candidates

        if title_keys and len(candidates) == 1:
            entity_id, entity = candidates[0]
            _append_record_source(entity, record, role="official_duplicate")
            _append_record_assets(entity, record)
            _prefer_longer_record_content(entity, record)
            _merge_record_metadata(entity, record)
        else:
            entity_id = canonical_id(f"{record['system']}:{record['record_id']}")
            entities[entity_id] = _entity_from_record(record, entity_id)
        source_to_entity[(record["system"], record["record_id"])] = entity_id


def build_catalog(*, clean: bool = True) -> dict[str, Any]:
    source_records = CatalogSourceLoader(_neris_records, _amac_records).load()
    neris_records = source_records.neris
    amac_records = source_records.amac
    multi_source_records = _multi_source_catalog_records()
    if clean and catalog_laws_dir().exists():
        shutil.rmtree(catalog_laws_dir())
    catalog_laws_dir().mkdir(parents=True, exist_ok=True)
    writer = CatalogEntityWriter()

    entities, source_to_entity, title_index = _seed_neris_entities(neris_records)
    matcher = CatalogMatcher(title_index, choose_neris_match_with_rule)
    matches = _match_amac_records(amac_records, matcher, entities, source_to_entity)
    _merge_multi_source_rules(multi_source_records, entities, source_to_entity)
    dedupe_result = deduplicate_catalog_entities(entities, source_to_entity, matches)
    relations = _build_catalog_relations(
        neris_records=neris_records,
        amac_records=amac_records,
        entities=entities,
        source_to_entity=source_to_entity,
    )
    publishes_by_entity: dict[str, list[str]] = defaultdict(list)
    finalized_by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for relation in relations:
        if relation.get("relation") == "publishes":
            publishes_by_entity[str(relation.get("from"))].append(str(relation.get("to")))
        if relation.get("relation") == "finalizes_draft":
            finalized_by_entity[str(relation.get("to"))].append(
                {
                    "canonical_id": relation.get("from"),
                    "source": relation.get("source"),
                    "confidence": relation.get("confidence"),
                    "evidence": relation.get("evidence") or {},
                }
            )
    overrides = load_classification_overrides()
    missing_override_ids = sorted(set(overrides) - set(entities))
    if missing_override_ids:
        raise ValueError(
            "classification overrides reference missing canonical IDs: "
            + ", ".join(missing_override_ids)
        )
    for entity_id, entity in entities.items():
        material = material_classification_for(
            entity,
            publishes=publishes_by_entity.get(entity_id),
            override=overrides.get(entity_id),
        )
        entity["material_classification"] = material
        entity["enforcement_classification"] = enforcement_classification_for(
            entity, material_classification=material
        )
        entity["reference_lifecycle"] = reference_lifecycle_for(
            entity,
            material_classification=material,
            finalized_by=finalized_by_entity.get(entity_id),
        )
    enforcement_case_summary = annotate_enforcement_cases(entities, relations)
    review_items = _review_queue_items(
        amac_records,
        source_to_entity=source_to_entity,
        matches=matches,
    )
    manifest = _catalog_manifest(
        neris_records=neris_records,
        amac_records=amac_records,
        entities=entities,
        relations=relations,
        review_items=review_items,
        matches=matches,
    )
    multi_source_counts = Counter(
        str(record.get("material_lane")) for record in multi_source_records
    )
    material_counts = Counter(
        str((entity.get("material_classification") or {}).get("lane"))
        for entity in entities.values()
    )
    manifest["multi_source_catalog_records"] = len(multi_source_records)
    manifest["multi_source_rule_records"] = multi_source_counts.get("rule", 0)
    manifest["multi_source_reference_records"] = multi_source_counts.get("reference", 0)
    manifest["material_lane_counts"] = dict(sorted(material_counts.items()))
    manifest["enforcement_cases"] = enforcement_case_summary

    writer.write_entities(catalog_laws_dir(), entities)
    writer.write_source_matches(source_matches_path(), source_to_entity, matches)
    writer.write_relations(catalog_relations_path(), relations)
    save_json(reports_dir() / "canonical_dedupe_map.json", dedupe_result)
    writer.write_review_queue(
        catalog_review_queue_path(),
        rules=catalog_rules_manifest(),
        rule_calibration=catalog_rule_calibration(),
        items=review_items,
    )
    catalog_dir().mkdir(parents=True, exist_ok=True)
    writer.write_manifest(catalog_manifest_path(), manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="生成NERIS+AMAC统一法规实体目录")
    parser.add_argument("--no-clean", action="store_true")
    args = parser.parse_args()
    try:
        manifest = build_catalog(clean=not args.no_clean)
    except Exception as exc:
        log_event("cli_error", level="ERROR", message=f"失败: {exc}", error_message=str(exc))
        return 1
    log_event("cli_result", message=json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "build-catalog"))
