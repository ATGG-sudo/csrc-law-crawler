#!/usr/bin/env python3
"""Match NERIS and AMAC source records and build source-independent law entities."""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from config import OUTPUT_DIR
from storage import (
    amac_sources_dir,
    catalog_dir,
    catalog_laws_dir,
    catalog_relations_path,
    laws_dir,
    load_json,
    save_json,
    source_matches_path,
    utc_now_iso,
)

CATALOG_MANIFEST = OUTPUT_DIR / "catalog" / "manifest.json"
CATALOG_REVIEW_QUEUE = OUTPUT_DIR / "catalog" / "review_queue.json"
QUOTED_TITLE_RE = re.compile(r"《([^》]{4,120})》")
PUBLISHING_TITLE_RE = re.compile(r"^(?:关于)?(?:发布|印发|公布|修订并发布)")
SPACE_PUNCT_RE = re.compile(r"[\s\u3000·•,，。；;:：()（）\[\]【】《》“”\"'、—\-]+")
ATTACHMENT_PREFIX_RE = re.compile(r"^附件(?:\s*\d+(?:-\d+)?)?\s*[：:、.\-]?\s*")
FILE_SUFFIX_RE = re.compile(r"\.(pdf|docx?|xlsx?|zip|rar|rtf|wps)$", re.I)


def clean_title(value: Any) -> str:
    text = html.unescape(str(value or "")).strip()
    text = ATTACHMENT_PREFIX_RE.sub("", text)
    text = FILE_SUFFIX_RE.sub("", text)
    return text.replace("&mdash;", "—").strip()


def normalize_title(value: Any) -> str:
    text = clean_title(value)
    return SPACE_PUNCT_RE.sub("", text).lower()


def normalize_fileno(value: Any) -> str:
    return SPACE_PUNCT_RE.sub("", html.unescape(str(value or ""))).lower()


def canonical_id(seed: str) -> str:
    return f"law_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"


def _source_descriptor(
    system: str,
    record_id: str,
    *,
    role: str,
    local_file: str | None,
    page_url: str | None,
) -> dict[str, Any]:
    return {
        "system": system,
        "record_id": record_id,
        "role": role,
        "local_file": local_file,
        "page_url": page_url,
    }


def _neris_records() -> list[dict[str, Any]]:
    records = []
    for path in sorted(laws_dir().glob("reg_*.json")):
        doc = load_json(path, {})
        metadata = doc.get("metadata") or {}
        record_id = str(metadata.get("id") or path.stem.removeprefix("reg_"))
        records.append(
            {
                "system": "neris",
                "record_id": record_id,
                "metadata": metadata,
                "plain_text": doc.get("full_text") or "",
                "local_file": str(path.relative_to(OUTPUT_DIR)),
                "page_url": (doc.get("source") or {}).get("detail_url"),
                "assets": doc.get("source_attachments") or [],
            }
        )
    return records


def _amac_records() -> list[dict[str, Any]]:
    records = []
    for path in sorted(amac_sources_dir().glob("amac_*.json")):
        doc = load_json(path, {})
        record_id = str(doc.get("source_record_id") or path.stem)
        records.append(
            {
                "system": "amac",
                "record_id": record_id,
                "metadata": doc.get("metadata") or {},
                "plain_text": (doc.get("content") or {}).get("plain_text") or "",
                "local_file": str(path.relative_to(OUTPUT_DIR)),
                "page_url": (doc.get("source") or {}).get("page_url"),
                "assets": doc.get("assets") or [],
                "parent_record_id": None,
            }
        )
        for attachment in doc.get("attachment_documents") or []:
            attachment_id = str(attachment.get("source_record_id") or "")
            if not attachment_id:
                continue
            attachment_metadata = dict(attachment.get("metadata") or {})
            parent_metadata = doc.get("metadata") or {}
            if attachment_metadata.get("status") in {None, "", "unknown"}:
                parent_status = parent_metadata.get("status")
                if parent_status not in {None, "", "unknown"}:
                    attachment_metadata["status"] = parent_status
            for field in ("effective_date", "ineffective_date"):
                if not attachment_metadata.get(field) and parent_metadata.get(field):
                    attachment_metadata[field] = parent_metadata.get(field)
            records.append(
                {
                    "system": "amac",
                    "record_id": attachment_id,
                    "metadata": attachment_metadata,
                    "plain_text": (
                        attachment.get("content") or {}
                    ).get("plain_text") or "",
                    "local_file": next(
                        (
                            asset.get("local_file")
                            for asset in (doc.get("assets") or [])
                            if asset.get("asset_id") == attachment.get("asset_id")
                        ),
                        None,
                    ),
                    "page_url": (attachment.get("source") or {}).get("asset_url"),
                    "assets": [],
                    "parent_record_id": record_id,
                }
            )
    return records


def _date_distance(left: Any, right: Any) -> int | None:
    try:
        from datetime import date

        return abs(
            (
                date.fromisoformat(str(left)[:10])
                - date.fromisoformat(str(right)[:10])
            ).days
        )
    except (TypeError, ValueError):
        return None


def choose_neris_match(
    amac: dict[str, Any],
    title_index: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any] | None, str, float, list[str]]:
    metadata = amac.get("metadata") or {}
    title_key = normalize_title(metadata.get("name"))
    candidates = title_index.get(title_key) or []
    if not candidates:
        return None, "new_to_neris", 1.0, ["NERIS中无同题名记录"]

    amac_fileno = normalize_fileno(metadata.get("fileno"))
    if amac_fileno:
        fileno_matches = [
            item
            for item in candidates
            if normalize_fileno((item.get("metadata") or {}).get("fileno"))
            == amac_fileno
        ]
        if len(fileno_matches) == 1:
            return fileno_matches[0], "same_document", 1.0, ["题名和文号一致"]

    dated = []
    for item in candidates:
        distance = _date_distance(
            metadata.get("pub_date"),
            (item.get("metadata") or {}).get("pub_date"),
        )
        if distance is not None:
            dated.append((distance, item))
    if dated:
        dated.sort(key=lambda pair: pair[0])
        if dated[0][0] <= 3:
            match = dated[0][1]
            status = (
                "supplemental_copy"
                if amac.get("assets") and not match.get("assets")
                else "same_document"
            )
            return match, status, 0.99, ["题名一致且发布日期相差不超过3日"]

    if len(candidates) == 1:
        match = candidates[0]
        status = (
            "supplemental_copy"
            if amac.get("assets") and not match.get("assets")
            else "same_document"
        )
        return match, status, 0.92, ["题名唯一一致；日期或文号证据不足"]
    return None, "ambiguous", 0.4, ["NERIS存在多个同题名候选"]


def _entity_from_record(record: dict[str, Any], entity_id: str) -> dict[str, Any]:
    metadata = dict(record.get("metadata") or {})
    title = clean_title(metadata.get("name"))
    metadata["name"] = title
    text = str(record.get("plain_text") or "")
    return {
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
            )
        ],
        "updated_at": utc_now_iso(),
    }


def build_catalog(*, clean: bool = True) -> dict[str, Any]:
    neris_records = _neris_records()
    amac_records = _amac_records()
    if clean and catalog_laws_dir().exists():
        shutil.rmtree(catalog_laws_dir())
    catalog_laws_dir().mkdir(parents=True, exist_ok=True)

    entities: dict[str, dict[str, Any]] = {}
    source_to_entity: dict[tuple[str, str], str] = {}
    title_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    amac_entity_index: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    matches: dict[str, dict[str, Any]] = {}

    for record in neris_records:
        entity_id = canonical_id(f"neris:{record['record_id']}")
        entity = _entity_from_record(record, entity_id)
        entities[entity_id] = entity
        source_to_entity[("neris", record["record_id"])] = entity_id
        title_index[normalize_title((record.get("metadata") or {}).get("name"))].append(
            record
        )

    for record in amac_records:
        match, status, confidence, evidence = choose_neris_match(record, title_index)
        if match is not None:
            entity_id = source_to_entity[("neris", match["record_id"])]
            entity = entities[entity_id]
            entity["sources"].append(
                _source_descriptor(
                    "amac",
                    record["record_id"],
                    role=(
                        "supplemental_official_copy"
                        if status == "supplemental_copy"
                        else "official_copy"
                    ),
                    local_file=record.get("local_file"),
                    page_url=record.get("page_url"),
                )
            )
            current_text = (
                (entity.get("preferred_content") or {}).get("plain_text") or ""
            )
            new_text = str(record.get("plain_text") or "")
            if len(new_text) > len(current_text):
                entity["preferred_content"] = {
                    "source_system": "amac",
                    "source_record_id": record["record_id"],
                    "plain_text": new_text,
                }
        else:
            title_key = normalize_title((record.get("metadata") or {}).get("name"))
            amac_candidates = amac_entity_index.get(title_key) or []
            existing_amac_entity: str | None = None
            if len(amac_candidates) == 1:
                candidate_entity_id, candidate_record = amac_candidates[0]
                distance = _date_distance(
                    (record.get("metadata") or {}).get("pub_date"),
                    (candidate_record.get("metadata") or {}).get("pub_date"),
                )
                if distance is None or distance <= 3:
                    existing_amac_entity = candidate_entity_id
            if existing_amac_entity:
                entity_id = existing_amac_entity
                entity = entities[entity_id]
                entity["sources"].append(
                    _source_descriptor(
                        "amac",
                        record["record_id"],
                        role="official_copy",
                        local_file=record.get("local_file"),
                        page_url=record.get("page_url"),
                    )
                )
                current_text = (
                    (entity.get("preferred_content") or {}).get("plain_text") or ""
                )
                new_text = str(record.get("plain_text") or "")
                if len(new_text) > len(current_text):
                    entity["preferred_content"] = {
                        "source_system": "amac",
                        "source_record_id": record["record_id"],
                        "plain_text": new_text,
                    }
                status = "same_document"
                confidence = 0.95
                evidence = ["AMAC多个官方页面或附件题名、发布日期一致"]
            else:
                entity_id = canonical_id(f"amac:{record['record_id']}")
                entities[entity_id] = _entity_from_record(record, entity_id)
                amac_entity_index[title_key].append((entity_id, record))
        source_to_entity[("amac", record["record_id"])] = entity_id
        matches[record["record_id"]] = {
            "match_status": status,
            "neris_id": match.get("record_id") if match else None,
            "canonical_id": entity_id,
            "match_method": "normalized_title_fileno_date",
            "confidence": confidence,
            "evidence": evidence,
        }

    relations: list[dict[str, Any]] = []
    relation_keys: set[tuple[str, str, str]] = set()

    def add_relation(
        from_id: str,
        to_id: str,
        relation: str,
        evidence: dict[str, Any],
    ) -> None:
        key = (from_id, to_id, relation)
        if from_id == to_id or key in relation_keys:
            return
        relation_keys.add(key)
        relations.append(
            {
                "from": from_id,
                "to": to_id,
                "relation": relation,
                "source": evidence.get("source"),
                "evidence": evidence,
                "confidence": evidence.get("confidence", 1.0),
            }
        )

    for record in amac_records:
        parent_id = record.get("parent_record_id")
        if not parent_id:
            continue
        parent_entity = source_to_entity.get(("amac", str(parent_id)))
        child_entity = source_to_entity.get(("amac", record["record_id"]))
        if parent_entity and child_entity:
            add_relation(
                parent_entity,
                child_entity,
                "publishes",
                {
                    "source": "amac.page_attachment",
                    "parent_source_record_id": parent_id,
                    "attachment_source_record_id": record["record_id"],
                    "confidence": 1.0,
                },
            )

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
                add_relation(
                    parent_entity,
                    candidates[0],
                    "publishes",
                    {
                        "source": "neris.title",
                        "quoted_title": quoted,
                        "confidence": 0.9,
                    },
                )

    for entity_id, entity in entities.items():
        save_json(catalog_laws_dir() / f"{entity_id}.json", entity)

    matches_doc = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "items": matches,
    }
    save_json(source_matches_path(), matches_doc)
    save_json(
        catalog_relations_path(),
        {
            "schema_version": 1,
            "updated_at": utc_now_iso(),
            "items": relations,
        },
    )

    match_counts: dict[str, int] = defaultdict(int)
    for item in matches.values():
        match_counts[str(item["match_status"])] += 1
    review_items = []
    for record in amac_records:
        match = matches.get(record["record_id"]) or {}
        metadata = record.get("metadata") or {}
        reasons = []
        if match.get("match_status") == "ambiguous":
            reasons.append("source_match_ambiguous")
        if (
            metadata.get("document_type") == "self_regulatory_rule"
            and metadata.get("status") in {None, "", "unknown"}
        ):
            reasons.append("effectiveness_unknown")
        if reasons:
            review_items.append(
                {
                    "source_record_id": record["record_id"],
                    "canonical_id": source_to_entity.get(
                        ("amac", record["record_id"])
                    ),
                    "name": metadata.get("name"),
                    "reasons": reasons,
                    "source_url": record.get("page_url"),
                }
            )
    save_json(
        CATALOG_REVIEW_QUEUE,
        {
            "schema_version": 1,
            "updated_at": utc_now_iso(),
            "count": len(review_items),
            "items": review_items,
        },
    )
    manifest = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "neris_source_records": len(neris_records),
        "amac_source_records": len(amac_records),
        "canonical_laws": len(entities),
        "relations": len(relations),
        "review_queue": len(review_items),
        "match_counts": dict(sorted(match_counts.items())),
        "laws_dir": str(catalog_laws_dir().relative_to(OUTPUT_DIR)),
    }
    catalog_dir().mkdir(parents=True, exist_ok=True)
    save_json(CATALOG_MANIFEST, manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="生成NERIS+AMAC统一法规实体目录")
    parser.add_argument("--no-clean", action="store_true")
    args = parser.parse_args()
    try:
        manifest = build_catalog(clean=not args.no_clean)
    except Exception as exc:
        print(f"失败: {exc}", file=sys.stderr)
        return 1
    print(manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
