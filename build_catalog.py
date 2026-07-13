#!/usr/bin/env python3
"""Match NERIS and AMAC source records and build source-independent law entities."""

from __future__ import annotations

import argparse
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
from csrc_law_crawler.sources.registry import load_registry
from parser import repair_known_neris_mojibake
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
        record_id = str(metadata.get("id") or path.stem.removeprefix("reg_"))
        attachment_index = load_json(attachment_index_path(record_id), {})
        records.append(
            {
                "system": "neris",
                "record_id": record_id,
                "metadata": metadata,
                "plain_text": repair_known_neris_mojibake(doc.get("full_text") or ""),
                "local_file": relative_to_output(path),
                "page_url": (doc.get("source") or {}).get("detail_url"),
                "assets": (
                    attachment_index.get("attachments") or doc.get("source_attachments") or []
                ),
            }
        )
    return records


def _amac_records() -> list[dict[str, Any]]:
    records = []
    for path in iter_amac_source_files():
        doc = load_json(path, {})
        record_id = str(doc.get("source_record_id") or path.stem)
        records.append(
            {
                "system": "amac",
                "record_id": record_id,
                "metadata": _repair_text_fields(doc.get("metadata") or {}),
                "local_file": relative_to_output(path),
                "page_url": (doc.get("source") or {}).get("page_url"),
                "assets": doc.get("assets") or [],
                "parent_record_id": None,
            }
        )
        records[-1]["plain_text"] = _record_plain_text(
            records[-1]["metadata"],
            (doc.get("content") or {}).get("plain_text"),
            records[-1]["local_file"],
        )
        for attachment in doc.get("attachment_documents") or []:
            attachment_id = str(attachment.get("source_record_id") or "")
            if not attachment_id:
                continue
            attachment_metadata = _repair_text_fields(dict(attachment.get("metadata") or {}))
            parent_metadata = _repair_text_fields(doc.get("metadata") or {})
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
            records.append(
                {
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
            )
    return records


def _multi_source_rule_records() -> list[dict[str, Any]]:
    """Load only publishable rule records from the multi-source evidence store."""
    root = output_path("raw/sources/records")
    records: list[dict[str, Any]] = []
    if not root.exists():
        return records
    restricted_endpoints = {
        endpoint["endpoint_id"]
        for endpoint in load_registry()["endpoints"]
        if endpoint["scope_mode"] in {"catalog_filter", "query_exhaustive"}
    }
    for path in sorted(root.glob("*/*.json")):
        doc = load_json(path, {})
        if doc.get("ingest_status") != "complete" or doc.get("material_lane") != "rule":
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
        records.append(
            {
                "system": str(doc.get("source_system") or path.parent.name),
                "record_id": str(doc.get("source_record_id") or path.stem),
                "metadata": _repair_text_fields(doc.get("metadata") or {}),
                "plain_text": plain_text,
                "local_file": relative_to_output(path),
                "page_url": source.get("page_url"),
                "assets": doc.get("assets") or [],
            }
        )
    return records


def _merge_multi_source_rules(
    records: list[dict[str, Any]],
    entities: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
) -> None:
    """Merge exact official duplicates; otherwise keep a source-owned entity."""
    for record in records:
        metadata = record.get("metadata") or {}
        title_key = normalize_title(metadata.get("name"))
        fileno_key = normalize_fileno(metadata.get("fileno"))
        candidates: list[tuple[str, dict[str, Any]]] = []
        for entity_id, entity in entities.items():
            entity_metadata = entity.get("metadata") or {}
            if normalize_title(entity_metadata.get("name") or entity.get("title")) != title_key:
                continue
            entity_fileno = normalize_fileno(entity_metadata.get("fileno"))
            if fileno_key and entity_fileno and fileno_key != entity_fileno:
                continue
            left_date = metadata.get("pub_date")
            right_date = entity_metadata.get("pub_date")
            if left_date and right_date and not _dates_match_for_merge(left_date, right_date):
                continue
            candidates.append((entity_id, entity))

        if title_key and len(candidates) == 1:
            entity_id, entity = candidates[0]
            _append_record_source(entity, record, role="official_duplicate")
            _append_record_assets(entity, record)
            _prefer_longer_record_content(entity, record)
        else:
            entity_id = canonical_id(f"{record['system']}:{record['record_id']}")
            entities[entity_id] = _entity_from_record(record, entity_id)
        source_to_entity[(record["system"], record["record_id"])] = entity_id


def build_catalog(*, clean: bool = True) -> dict[str, Any]:
    source_records = CatalogSourceLoader(_neris_records, _amac_records).load()
    neris_records = source_records.neris
    amac_records = source_records.amac
    multi_source_rule_records = _multi_source_rule_records()
    if clean and catalog_laws_dir().exists():
        shutil.rmtree(catalog_laws_dir())
    catalog_laws_dir().mkdir(parents=True, exist_ok=True)
    writer = CatalogEntityWriter()

    entities, source_to_entity, title_index = _seed_neris_entities(neris_records)
    matcher = CatalogMatcher(title_index, choose_neris_match_with_rule)
    matches = _match_amac_records(amac_records, matcher, entities, source_to_entity)
    _merge_multi_source_rules(multi_source_rule_records, entities, source_to_entity)
    dedupe_result = deduplicate_catalog_entities(entities, source_to_entity, matches)
    relations = _build_catalog_relations(
        neris_records=neris_records,
        amac_records=amac_records,
        entities=entities,
        source_to_entity=source_to_entity,
    )
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
    manifest["multi_source_rule_records"] = len(multi_source_rule_records)

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
