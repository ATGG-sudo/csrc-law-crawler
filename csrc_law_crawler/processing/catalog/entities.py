"""Canonical catalog entity construction and dedupe helpers."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from functools import lru_cache
import hashlib
import re
from typing import Any

from asset_text import extract_local_asset_text
from catalog_rules import (
    MATCH_AMAC_INTERNAL_TITLE_DATE,
    MATCH_AMAC_PARENT_ATTACHMENT_SAME_DOCUMENT,
)
from catalog_services import CatalogMatcher
from csrc_law_crawler.sources.evidence import canonical_final_url
from parser import repair_known_neris_mojibake
from storage import output_path, utc_now_iso

from .classification import disciplinary_penalty_subtype

from .identity import (
    ATTACHMENT_TEXT_SIGNAL_RE,
    DEDUP_MIN_BODY_CHARS,
    LEADING_ITEM_MARKER_RE,
    QUOTED_TITLE_RE,
    REVISION_MARKER_RE,
    SECTION_TOKEN_RE,
    SPACE_PUNCT_RE,
    TRIAL_MARKER_RE,
    _date_distance,
    canonical_id,
    clean_title,
    fileno_keys,
    normalize_fileno,
    normalize_title,
)


def _source_descriptor(
    system: str,
    record_id: str,
    *,
    role: str,
    local_file: str | None,
    page_url: str | None,
    material_lane: str | None = None,
    web_classification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "system": system,
        "record_id": record_id,
        "role": role,
        "local_file": local_file,
        "page_url": page_url,
    }
    if material_lane:
        result["material_lane"] = material_lane
    for key in (
        "web_category_leaf",
        "web_category_path",
        "web_category_provenance",
        "page_role",
    ):
        value = (web_classification or {}).get(key)
        result[key] = value
    return result


def _record_assets(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(asset) for asset in record.get("assets") or []]


def _asset_text_fallback(metadata: dict[str, Any], local_file: Any) -> str:
    local_file_text = str(local_file or "")
    if not local_file_text:
        return ""
    return extract_local_asset_text(output_path(local_file_text))


def _low_signal_plain_text(metadata: dict[str, Any], text: str) -> bool:
    compact = SPACE_PUNCT_RE.sub("", text)
    if not compact:
        return True
    title = SPACE_PUNCT_RE.sub("", str(metadata.get("name") or ""))
    remainder = compact.replace(title, "") if title else compact
    remainder = ATTACHMENT_TEXT_SIGNAL_RE.sub("", remainder)
    remainder = SECTION_TOKEN_RE.sub("", remainder)
    if title and not remainder:
        return True
    if ATTACHMENT_TEXT_SIGNAL_RE.search(compact) and len(remainder) <= 30:
        return True
    return bool(title and title in compact and len(remainder) <= 8)


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


def _entity_from_record(record: dict[str, Any], entity_id: str) -> dict[str, Any]:
    metadata = dict(record.get("metadata") or {})
    title = clean_title(metadata.get("name"))
    metadata["name"] = title
    text = repair_known_neris_mojibake(str(record.get("plain_text") or ""))
    entity = {
        "schema_version": 1,
        "id": entity_id,
        "title": title,
        "document_type": metadata.get("document_type") or "regulation",
        "status": metadata.get("status") or "unknown",
        "metadata": metadata,
        "preferred_content": {
            "source_system": record["system"],
            "source_record_id": record["record_id"],
            "plain_text": text,
        },
        "sources": [
            _source_descriptor(
                record["system"],
                record["record_id"],
                role="official_text",
                local_file=record.get("local_file"),
                page_url=record.get("page_url"),
                material_lane=record.get("material_lane"),
                web_classification=record,
            )
        ],
        "assets": _record_assets(record),
        "updated_at": utc_now_iso(),
    }
    if record.get("material_lane"):
        entity["material_lane"] = record["material_lane"]
    return entity


def _seed_neris_entities(
    neris_records: list[dict[str, Any]],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[tuple[str, str], str],
    dict[str, list[dict[str, Any]]],
]:
    entities: dict[str, dict[str, Any]] = {}
    source_to_entity: dict[tuple[str, str], str] = {}
    title_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    neris_entity_index: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)

    for record in neris_records:
        title_key = normalize_title((record.get("metadata") or {}).get("name"))
        existing_entity_id = _find_existing_neris_entity(record, neris_entity_index)
        if existing_entity_id:
            entity_id = existing_entity_id
            entity = entities[entity_id]
            _append_record_source(entity, record, role="official_duplicate")
            _append_record_assets(entity, record)
            _prefer_longer_record_content(entity, record)
            _merge_record_metadata(entity, record)
            _append_neris_merge_metadata(entity, record)
        else:
            entity_id = canonical_id(f"neris:{record['record_id']}")
            entity = _entity_from_record(record, entity_id)
            entities[entity_id] = entity
            neris_key = _neris_merge_index_key(record)
            if neris_key:
                neris_entity_index[neris_key].append((entity_id, record))
            title_index[title_key].append(record)
        source_to_entity[("neris", record["record_id"])] = entity_id
    return entities, source_to_entity, title_index


def _neris_merge_index_key(record: dict[str, Any]) -> str | None:
    metadata = record.get("metadata") or {}
    title_key = normalize_title(metadata.get("name"))
    fileno_key = normalize_fileno(metadata.get("fileno"))
    if not title_key or not fileno_key:
        return None
    return f"{title_key}\0{fileno_key}"


def _dates_match_for_merge(left: Any, right: Any) -> bool:
    distance = _date_distance(left, right)
    if distance is not None:
        return distance <= 3
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    return bool(left_text) and left_text == right_text


def _find_existing_neris_entity(
    record: dict[str, Any],
    neris_entity_index: dict[str, list[tuple[str, dict[str, Any]]]],
) -> str | None:
    key = _neris_merge_index_key(record)
    if not key:
        return None
    metadata = record.get("metadata") or {}
    for entity_id, candidate in neris_entity_index.get(key) or []:
        candidate_metadata = candidate.get("metadata") or {}
        if _dates_match_for_merge(
            metadata.get("pub_date"),
            candidate_metadata.get("pub_date"),
        ):
            return entity_id
    return None


def _neris_merge_source_metadata(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata") or {}
    return {
        "record_id": record.get("record_id"),
        "number": metadata.get("number"),
        "status": metadata.get("status"),
        "pub_org": metadata.get("pub_org"),
        "pub_date": metadata.get("pub_date"),
        "version": metadata.get("version"),
    }


def _append_neris_merge_metadata(
    entity: dict[str, Any],
    record: dict[str, Any],
) -> None:
    metadata = entity.setdefault("metadata", {})
    merged_sources = metadata.setdefault("merged_neris_sources", [])
    if not merged_sources:
        merged_sources.append(
            {
                "record_id": metadata.get("id"),
                "number": metadata.get("number"),
                "status": metadata.get("status"),
                "pub_org": metadata.get("pub_org"),
                "pub_date": metadata.get("pub_date"),
                "version": metadata.get("version"),
            }
        )
    source_metadata = _neris_merge_source_metadata(record)
    if source_metadata not in merged_sources:
        merged_sources.append(source_metadata)


def _append_record_source(
    entity: dict[str, Any],
    record: dict[str, Any],
    *,
    role: str,
) -> None:
    entity["sources"].append(
        _source_descriptor(
            str(record["system"]),
            str(record["record_id"]),
            role=role,
            local_file=record.get("local_file"),
            page_url=record.get("page_url"),
            material_lane=record.get("material_lane"),
            web_classification=record,
        )
    )


def _append_record_assets(entity: dict[str, Any], record: dict[str, Any]) -> None:
    _append_unique_assets(entity, {"assets": _record_assets(record)})


def _valid_date(value: Any) -> bool:
    try:
        date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return False
    return True


def _merge_record_metadata(
    entity: dict[str, Any],
    incoming: dict[str, Any],
) -> None:
    """Fill missing canonical metadata without replacing stronger evidence."""
    target = entity.setdefault("metadata", {})
    source = incoming.get("metadata") or {}
    title = str(target.get("name") or entity.get("title") or "")
    revision_year_match = re.search(r"[（(](\d{4})年修订[）)]", title)
    revision_year = revision_year_match.group(1) if revision_year_match else None
    for field in ("pub_date", "effective_date", "ineffective_date"):
        if not _valid_date(target.get(field)) and _valid_date(source.get(field)):
            target[field] = source[field]
        elif (
            field in {"pub_date", "effective_date"}
            and revision_year
            and str(source.get(field) or "").startswith(revision_year)
            and not str(target.get(field) or "").startswith(revision_year)
        ):
            target[field] = source[field]
    for field in (
        "fileno",
        "pub_org",
        "publisher",
        "document_type",
        "version",
    ):
        if target.get(field) in (None, "", "unknown") and source.get(field) not in (
            None,
            "",
            "unknown",
        ):
            target[field] = source[field]
    source_status = source.get("status")
    if target.get("status") in (None, "", "unknown") and source_status not in (
        None,
        "",
        "unknown",
    ):
        target["status"] = source_status
        entity["status"] = source_status
    if entity.get("document_type") in (None, "", "unknown") and target.get("document_type"):
        entity["document_type"] = target["document_type"]


def _prefer_longer_record_content(
    entity: dict[str, Any],
    record: dict[str, Any],
) -> None:
    current_text = (entity.get("preferred_content") or {}).get("plain_text") or ""
    new_text = str(record.get("plain_text") or "")
    if len(new_text) > len(current_text):
        entity["preferred_content"] = {
            "source_system": record["system"],
            "source_record_id": record["record_id"],
            "plain_text": new_text,
        }


def _compact_body_text(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _body_hash(entity: dict[str, Any]) -> str:
    text = _compact_body_text((entity.get("preferred_content") or {}).get("plain_text"))
    if len(text) < DEDUP_MIN_BODY_CHARS:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dedupe_title_key(value: Any) -> str:
    text = clean_title(value)
    text = LEADING_ITEM_MARKER_RE.sub("", text)
    text = TRIAL_MARKER_RE.sub("", text)
    text = REVISION_MARKER_RE.sub("", text)
    return normalize_title(text)


def _entity_dedupe_title_key(entity: dict[str, Any]) -> str:
    metadata = entity.get("metadata") or {}
    return _dedupe_title_key(metadata.get("name") or entity.get("title"))


def _source_file_sha256(local_file: Any) -> str:
    local_file_text = str(local_file or "")
    if not local_file_text:
        return ""
    path = output_path(local_file_text)
    if not path.exists() or not path.is_file() or path.suffix.lower() == ".json":
        return ""
    return _path_sha256(path.as_posix())


@lru_cache(maxsize=None)
def _path_sha256(path_text: str) -> str:
    digest = hashlib.sha256()
    with open(path_text, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entity_asset_shas(entity: dict[str, Any]) -> set[str]:
    result = {
        str(asset.get("sha256") or "")
        for asset in entity.get("assets") or []
        if asset.get("sha256")
    }
    for source in entity.get("sources") or []:
        digest = _source_file_sha256(source.get("local_file"))
        if digest:
            result.add(digest)
    return result


def _entity_sources_key(source: dict[str, Any]) -> tuple[Any, ...]:
    return (
        source.get("system"),
        source.get("record_id"),
        source.get("role"),
        source.get("local_file"),
        source.get("page_url"),
    )


def _append_unique_sources(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    sources = target.setdefault("sources", [])
    seen = {_entity_sources_key(source) for source in sources}
    for source in incoming.get("sources") or []:
        key = _entity_sources_key(source)
        if key not in seen:
            sources.append(source)
            seen.add(key)


def _append_unique_assets(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    assets = target.setdefault("assets", [])
    seen = {
        asset.get("sha256")
        or asset.get("local_file")
        or asset.get("source_url")
        or asset.get("asset_id")
        for asset in assets
    }
    for asset in incoming.get("assets") or []:
        key = (
            asset.get("sha256")
            or asset.get("local_file")
            or asset.get("source_url")
            or asset.get("asset_id")
        )
        if key and key not in seen:
            assets.append(asset)
            seen.add(key)


def _entity_completeness(entity: dict[str, Any]) -> int:
    metadata = entity.get("metadata") or {}
    score = 0
    for field in ("fileno", "pub_date", "effective_date"):
        if metadata.get(field):
            score += 1
    status = str(metadata.get("status") or entity.get("status") or "")
    if status and status != "unknown":
        score += 1
    return score


def _has_neris_source(entity: dict[str, Any]) -> bool:
    return any(source.get("system") == "neris" for source in entity.get("sources") or [])


def _choose_kept_entity_id(
    entity_ids: list[str],
    entities: dict[str, dict[str, Any]],
) -> str:
    return sorted(
        entity_ids,
        key=lambda entity_id: (
            not _has_neris_source(entities[entity_id]),
            -len((entities[entity_id].get("preferred_content") or {}).get("plain_text") or ""),
            -_entity_completeness(entities[entity_id]),
            entity_id,
        ),
    )[0]


def _source_records_for_entity(
    source_to_entity: dict[tuple[str, str], str],
    entity_id: str,
) -> list[str]:
    return [
        f"{system}:{record_id}"
        for (system, record_id), mapped_id in sorted(source_to_entity.items())
        if mapped_id == entity_id
    ]


def _merge_catalog_entity(
    *,
    entities: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
    matches: dict[str, dict[str, Any]],
    kept_id: str,
    removed_id: str,
    reason: str,
) -> dict[str, Any]:
    kept = entities[kept_id]
    removed = entities[removed_id]
    _append_unique_sources(kept, removed)
    _append_unique_assets(kept, removed)
    _merge_record_metadata(kept, removed)
    kept_text = str((kept.get("preferred_content") or {}).get("plain_text") or "")
    removed_text = str((removed.get("preferred_content") or {}).get("plain_text") or "")
    if len(removed_text) > len(kept_text):
        kept["preferred_content"] = dict(removed.get("preferred_content") or {})

    metadata = kept.setdefault("metadata", {})
    if reason == "same_enforcement_attachment_sha":
        title_aliases = {
            str(title)
            for title in [
                kept.get("title"),
                removed.get("title"),
                *((kept.get("metadata") or {}).get("title_aliases") or []),
                *((removed.get("metadata") or {}).get("title_aliases") or []),
            ]
            if title
        }
        metadata["title_aliases"] = sorted(title_aliases)
    merged = metadata.setdefault("merged_catalog_entities", [])
    merged.append(
        {
            "canonical_id": removed_id,
            "title": removed.get("title"),
            "reason": reason,
        }
    )
    for item in (removed.get("metadata") or {}).get("merged_catalog_entities") or []:
        if item not in merged:
            merged.append(item)

    source_records = _source_records_for_entity(source_to_entity, removed_id)
    for source_key, mapped_id in list(source_to_entity.items()):
        if mapped_id == removed_id:
            source_to_entity[source_key] = kept_id
    for match in matches.values():
        if match.get("canonical_id") == removed_id:
            match["canonical_id"] = kept_id
    del entities[removed_id]
    return {
        "removed_id": removed_id,
        "kept_id": kept_id,
        "reason": reason,
        "source_records": source_records,
    }


def _merge_equivalent_groups(
    entities: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
    matches: dict[str, dict[str, Any]],
    groups: dict[str, list[str]],
    reason: str,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for entity_ids in groups.values():
        by_title: dict[str, list[str]] = defaultdict(list)
        for entity_id in entity_ids:
            if entity_id in entities:
                key = _entity_dedupe_title_key(entities[entity_id])
                if key:
                    by_title[key].append(entity_id)
        for matching_ids in by_title.values():
            if len(matching_ids) < 2:
                continue
            kept_id = _choose_kept_entity_id(matching_ids, entities)
            for removed_id in sorted(
                entity_id for entity_id in matching_ids if entity_id != kept_id
            ):
                if removed_id in entities:
                    merged.append(
                        _merge_catalog_entity(
                            entities=entities,
                            source_to_entity=source_to_entity,
                            matches=matches,
                            kept_id=kept_id,
                            removed_id=removed_id,
                            reason=reason,
                        )
                    )
    return merged


def _body_duplicate_groups(entities: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for entity_id, entity in entities.items():
        digest = _body_hash(entity)
        if digest:
            groups[digest].append(entity_id)
    return {digest: ids for digest, ids in groups.items() if len(ids) > 1}


def _asset_duplicate_groups(entities: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for entity_id, entity in entities.items():
        for sha256 in _entity_asset_shas(entity):
            groups[sha256].append(entity_id)
    return {sha256: ids for sha256, ids in groups.items() if len(ids) > 1}


def _is_amac_enforcement_attachment(entity: dict[str, Any]) -> bool:
    sources = entity.get("sources") or []
    amac_sources = [source for source in sources if source.get("system") == "amac"]
    if not amac_sources or len(amac_sources) != len(sources):
        return False
    if not all(
        str(source.get("record_id") or "").startswith("amac_asset_") for source in amac_sources
    ):
        return False
    title = str(entity.get("title") or "")
    if disciplinary_penalty_subtype(title):
        return True
    return any(
        source.get("web_category_leaf") in {"disciplinary_person", "disciplinary_institution"}
        and source.get("page_role") == "case_document"
        for source in amac_sources
    )


def _merge_enforcement_attachment_groups(
    entities: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
    matches: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for entity_ids in _asset_duplicate_groups(entities).values():
        partitions: dict[tuple[str, str], list[str]] = defaultdict(list)
        for entity_id in entity_ids:
            entity = entities.get(entity_id)
            if not entity or not _is_amac_enforcement_attachment(entity):
                continue
            subtype = disciplinary_penalty_subtype(entity.get("title")) or "other"
            pub_date = str((entity.get("metadata") or {}).get("pub_date") or "")[:10]
            partitions[(subtype, pub_date)].append(entity_id)
        for matching_ids in partitions.values():
            if len(matching_ids) < 2:
                continue
            kept_id = _choose_kept_entity_id(matching_ids, entities)
            for removed_id in sorted(
                entity_id for entity_id in matching_ids if entity_id != kept_id
            ):
                if removed_id in entities:
                    merged.append(
                        _merge_catalog_entity(
                            entities=entities,
                            source_to_entity=source_to_entity,
                            matches=matches,
                            kept_id=kept_id,
                            removed_id=removed_id,
                            reason="same_enforcement_attachment_sha",
                        )
                    )
    return merged


def _source_url_duplicate_groups(
    entities: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for entity_id, entity in entities.items():
        urls = {
            canonical_final_url(str(source.get("page_url") or ""))
            for source in entity.get("sources") or []
            if source.get("page_url")
        }
        for url in urls:
            if url:
                groups[url].append(entity_id)
    return {url: ids for url, ids in groups.items() if len(ids) > 1}


def _fileno_date_duplicate_groups(
    entities: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for entity_id, entity in entities.items():
        metadata = entity.get("metadata") or {}
        pub_date = str(metadata.get("pub_date") or "")[:10]
        if not _valid_date(pub_date):
            continue
        for fileno in fileno_keys(metadata.get("fileno")):
            groups[f"{fileno}:{pub_date}"].append(entity_id)
    return {key: ids for key, ids in groups.items() if len(ids) > 1}


def _announcement_mentions_multiple_titles(text: str, entities: list[dict[str, Any]]) -> bool:
    normalized_text = normalize_title(text)
    mentioned = 0
    for entity in entities:
        title = clean_title(entity.get("title") or (entity.get("metadata") or {}).get("name"))
        title_base = re.split(r"[（(]", title, maxsplit=1)[0]
        if title_base and normalize_title(title_base) in normalized_text:
            mentioned += 1
    return mentioned >= 2


def _is_multi_document_announcement_body(
    text: str,
    entities: list[dict[str, Any]],
) -> bool:
    if len({entity.get("title") for entity in entities}) < 2:
        return False
    if "现公布" not in text and "发布" not in text and "印发" not in text:
        return False
    if len(QUOTED_TITLE_RE.findall(text)) < 2:
        return False
    return _announcement_mentions_multiple_titles(text, entities)


def _repair_multi_document_announcement_content(
    entities: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    repaired: list[dict[str, Any]] = []
    for digest, entity_ids in _body_duplicate_groups(entities).items():
        current_entities = [
            entities[entity_id] for entity_id in entity_ids if entity_id in entities
        ]
        if len(current_entities) < 2:
            continue
        text = str((current_entities[0].get("preferred_content") or {}).get("plain_text") or "")
        if not _is_multi_document_announcement_body(text, current_entities):
            continue
        for entity in current_entities:
            preferred = entity.setdefault("preferred_content", {})
            original_length = len(str(preferred.get("plain_text") or ""))
            preferred["plain_text"] = ""
            metadata = entity.setdefault("metadata", {})
            metadata["content_repair"] = {
                "reason": "multi_document_announcement_body",
                "body_hash": digest,
                "original_text_length": original_length,
            }
            repaired.append(
                {
                    "canonical_id": entity.get("id"),
                    "title": entity.get("title"),
                    "reason": "multi_document_announcement_body",
                    "body_hash": digest,
                    "original_text_length": original_length,
                }
            )
    return repaired


def deduplicate_catalog_entities(
    entities: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
    matches: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    merged_entities: list[dict[str, Any]] = []
    merged_entities.extend(
        _merge_equivalent_groups(
            entities,
            source_to_entity,
            matches,
            _fileno_date_duplicate_groups(entities),
            "same_fileno_date_equivalent_title",
        )
    )
    enforcement_attachment_merges = _merge_enforcement_attachment_groups(
        entities,
        source_to_entity,
        matches,
    )
    merged_entities.extend(enforcement_attachment_merges)
    merged_entities.extend(
        _merge_equivalent_groups(
            entities,
            source_to_entity,
            matches,
            _source_url_duplicate_groups(entities),
            "same_official_url_equivalent_title",
        )
    )
    merged_entities.extend(
        _merge_equivalent_groups(
            entities,
            source_to_entity,
            matches,
            _body_duplicate_groups(entities),
            "same_body_equivalent_title",
        )
    )
    merged_entities.extend(
        _merge_equivalent_groups(
            entities,
            source_to_entity,
            matches,
            _asset_duplicate_groups(entities),
            "same_asset_equivalent_title",
        )
    )
    content_repairs = _repair_multi_document_announcement_content(entities)
    return {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "merged": len(merged_entities),
        "enforcement_attachment_merges": len(enforcement_attachment_merges),
        "content_repairs_count": len(content_repairs),
        "merged_entities": merged_entities,
        "content_repairs": content_repairs,
    }


def _find_existing_amac_entity(
    record: dict[str, Any],
    amac_entity_index: dict[str, list[tuple[str, dict[str, Any]]]],
) -> str | None:
    title_key = normalize_title((record.get("metadata") or {}).get("name"))
    amac_candidates = amac_entity_index.get(title_key) or []
    if len(amac_candidates) != 1:
        return None
    candidate_entity_id, candidate_record = amac_candidates[0]
    distance = _date_distance(
        (record.get("metadata") or {}).get("pub_date"),
        (candidate_record.get("metadata") or {}).get("pub_date"),
    )
    if distance is None or distance <= 3:
        return candidate_entity_id
    metadata = record.get("metadata") or {}
    candidate_metadata = candidate_record.get("metadata") or {}
    title = str(metadata.get("name") or "")
    if REVISION_MARKER_RE.search(title):
        left_org = normalize_title(metadata.get("pub_org"))
        right_org = normalize_title(candidate_metadata.get("pub_org"))
        if not left_org or not right_org or left_org == right_org:
            return candidate_entity_id
    return None


def _carrier_title_key(value: Any) -> str:
    title = re.sub(
        r"[（(][^（）()]*中基协字[^（）()]*[）)]",
        "",
        str(value or ""),
    )
    return normalize_title(title)


def _is_self_regulatory_measure_record(record: dict[str, Any]) -> bool:
    leaf = str(record.get("web_category_leaf") or "")
    page_url = str(record.get("page_url") or "")
    return leaf in {"self_regulatory_measure", "自律措施"} or "/zlgl/zlcs/" in page_url


def _same_document_carrier_parent(
    record: dict[str, Any],
    *,
    records_by_id: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
) -> tuple[str, dict[str, Any]] | None:
    parent_record_id = str(record.get("parent_record_id") or "")
    if not parent_record_id:
        return None
    parent = records_by_id.get(parent_record_id)
    parent_entity_id = source_to_entity.get(("amac", parent_record_id))
    if not parent or not parent_entity_id:
        return None
    parent_title = str((parent.get("metadata") or {}).get("name") or "")
    child_title = str((record.get("metadata") or {}).get("name") or "")
    if "送达公告" in parent_title or "公告送达" in parent_title:
        return None
    parent_assets = parent.get("assets") or []
    if len(parent_assets) != 1 or str(parent_assets[0].get("asset_id") or "") != str(
        record.get("record_id") or ""
    ):
        return None
    parent_date = str((parent.get("metadata") or {}).get("pub_date") or "")[:10]
    child_date = str((record.get("metadata") or {}).get("pub_date") or "")[:10]
    if not parent_date or parent_date != child_date:
        return None
    parent_subtype = disciplinary_penalty_subtype(parent_title)
    child_subtype = disciplinary_penalty_subtype(child_title)
    same_disciplinary_stage = bool(
        parent_subtype
        and parent_subtype == child_subtype
        and parent.get("page_role") == "case_document"
        and record.get("page_role") == "case_document"
    )
    same_self_regulatory_title = (
        _is_self_regulatory_measure_record(parent)
        and _is_self_regulatory_measure_record(record)
        and _carrier_title_key(parent_title) == _carrier_title_key(child_title)
    )
    if not same_disciplinary_stage and not same_self_regulatory_title:
        return None
    return parent_entity_id, parent


def _merge_same_document_carrier(
    record: dict[str, Any],
    *,
    parent_entity_id: str,
    parent_record: dict[str, Any],
    entities: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
) -> dict[str, Any]:
    entity = entities[parent_entity_id]
    _append_record_source(entity, record, role="official_attachment_copy")
    _append_record_assets(entity, record)
    _prefer_longer_record_content(entity, record)
    _merge_record_metadata(entity, record)
    metadata = entity.setdefault("metadata", {})
    metadata["title_aliases"] = sorted(
        {
            str(title)
            for title in [
                entity.get("title"),
                (record.get("metadata") or {}).get("name"),
                *(metadata.get("title_aliases") or []),
            ]
            if title
        }
    )
    source_to_entity[("amac", record["record_id"])] = parent_entity_id
    subtype = disciplinary_penalty_subtype(entity.get("title")) or "self_regulatory_measure"
    return {
        "match_status": "same_document_carrier",
        "neris_id": None,
        "canonical_id": parent_entity_id,
        "match_method": "amac_parent_attachment_same_document",
        "match_rule_id": MATCH_AMAC_PARENT_ATTACHMENT_SAME_DOCUMENT.rule_id,
        "confidence": MATCH_AMAC_PARENT_ATTACHMENT_SAME_DOCUMENT.confidence,
        "evidence": [
            f"AMAC单附件与父页面为同一{subtype}",
            f"parent_source_record_id={parent_record['record_id']}",
        ],
    }


def _merge_amac_record(
    record: dict[str, Any],
    *,
    matcher: CatalogMatcher,
    entities: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
    amac_entity_index: dict[str, list[tuple[str, dict[str, Any]]]],
) -> dict[str, Any]:
    match, status, confidence, evidence, match_rule_id = matcher.choose_neris_match(record)
    if match is not None:
        entity_id = source_to_entity[("neris", match["record_id"])]
        entity = entities[entity_id]
        _append_record_source(
            entity,
            record,
            role=(
                "supplemental_official_copy" if status == "supplemental_copy" else "official_copy"
            ),
        )
        _append_record_assets(entity, record)
        _prefer_longer_record_content(entity, record)
        _merge_record_metadata(entity, record)
    else:
        title_key = normalize_title((record.get("metadata") or {}).get("name"))
        existing_amac_entity = _find_existing_amac_entity(record, amac_entity_index)
        if existing_amac_entity:
            entity_id = existing_amac_entity
            entity = entities[entity_id]
            _append_record_source(entity, record, role="official_copy")
            _append_record_assets(entity, record)
            _prefer_longer_record_content(entity, record)
            _merge_record_metadata(entity, record)
            status = "same_document"
            confidence = MATCH_AMAC_INTERNAL_TITLE_DATE.confidence
            evidence = ["AMAC多个官方页面或附件题名、发布日期一致"]
            match_rule_id = MATCH_AMAC_INTERNAL_TITLE_DATE.rule_id
        else:
            entity_id = canonical_id(f"amac:{record['record_id']}")
            entities[entity_id] = _entity_from_record(record, entity_id)
            amac_entity_index[title_key].append((entity_id, record))

    source_to_entity[("amac", record["record_id"])] = entity_id
    return {
        "match_status": status,
        "neris_id": match.get("record_id") if match else None,
        "canonical_id": entity_id,
        "match_method": "normalized_title_fileno_date",
        "match_rule_id": match_rule_id,
        "confidence": confidence,
        "evidence": evidence,
    }


def _match_amac_records(
    amac_records: list[dict[str, Any]],
    matcher: CatalogMatcher,
    entities: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
) -> dict[str, dict[str, Any]]:
    amac_entity_index: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    matches: dict[str, dict[str, Any]] = {}
    records_by_id = {str(record.get("record_id") or ""): record for record in amac_records}

    for record in amac_records:
        carrier_parent = _same_document_carrier_parent(
            record,
            records_by_id=records_by_id,
            source_to_entity=source_to_entity,
        )
        if carrier_parent:
            parent_entity_id, parent_record = carrier_parent
            matches[record["record_id"]] = _merge_same_document_carrier(
                record,
                parent_entity_id=parent_entity_id,
                parent_record=parent_record,
                entities=entities,
                source_to_entity=source_to_entity,
            )
            continue
        matches[record["record_id"]] = _merge_amac_record(
            record,
            matcher=matcher,
            entities=entities,
            source_to_entity=source_to_entity,
            amac_entity_index=amac_entity_index,
        )
    return matches


entity_from_record = _entity_from_record
match_amac_records = _match_amac_records
record_plain_text = _record_plain_text
seed_neris_entities = _seed_neris_entities
