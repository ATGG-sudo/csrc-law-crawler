#!/usr/bin/env python3
"""Normalize canonical catalog entities into a source-independent text layer."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from catalog_rules import COMMENT_DRAFT_PATTERNS, REFERENCE_TITLE_PATTERNS
from csrc_law_crawler.processing.catalog.classification import (
    _parse_date,
    china_as_of,
    disciplinary_penalty_subtype,
    enforcement_classification_for,
    effectiveness_for as classify_effectiveness,
    load_classification_overrides,
    material_classification_for,
    reference_lifecycle_for,
)
from csrc_law_crawler.processing.catalog.curated_relations import (
    apply_curated_metadata_overrides,
    curated_version_ref_for_entity,
)
from csrc_law_crawler.processing.catalog.identity import is_trial_title
from normalize_laws import normalized_laws_dir
from parser import infer_effective_date
from runtime import log_event
from storage import (
    canonical_dir,
    catalog_dir,
    catalog_laws_dir,
    catalog_relations_path,
    catalog_normalized_dir,
    listed_output_files,
    load_json,
    output_path,
    relative_to_output,
    reports_dir,
    revisions_path,
    run_with_output_lock,
    save_json,
    utc_now_iso,
)

PAGE_NUMBER_RE = re.compile(r"^\s*\d{1,3}\s*$")
CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十百千万零〇两\d]+[章节编]\s*")
ARTICLE_RE = re.compile(r"^(第[一二三四五六七八九十百千万零〇两\d]+条)(?:\s+|(?=[^\s]))")
ITEM_RE = re.compile(r"^[（(][一二三四五六七八九十百零〇两\d]+[）)]")
ASCII_EDGE_RE = re.compile(r"[A-Za-z0-9]$")
ASCII_START_RE = re.compile(r"^[A-Za-z0-9]")
PARAGRAPH_END_PUNCT = ("。", "！", "？", "；", "：")
TITLE_HISTORICAL_PATTERNS = (
    "【已废止】",
    "〖已废止〗",
    "（已废止）",
    "(已废止)",
    "【失效】",
    "〖失效〗",
    "（失效）",
    "(失效)",
    "已失效",
)


def catalog_normalized_manifest_path() -> Path:
    return catalog_dir() / "normalized_manifest.json"


def catalog_manifest_path() -> Path:
    return catalog_dir() / "manifest.json"


def classification_review_queue_path() -> Path:
    return canonical_dir() / "classification_review_queue.json"


def classification_review_queue_csv_path() -> Path:
    return canonical_dir() / "classification_review_queue.csv"


def _join_fragment(left: str, right: str) -> str:
    if not left:
        return right
    separator = " " if ASCII_EDGE_RE.search(left) and ASCII_START_RE.search(right) else ""
    return left + separator + right


def _format_paragraph(text: str) -> str:
    match = ARTICLE_RE.match(text)
    if not match:
        return text
    marker = match.group(1)
    remainder = text[match.end() :].lstrip()
    if not remainder:
        return f"## {marker}"
    return f"## {marker}\n\n{remainder}".rstrip()


def _is_unfinished_paragraph(text: str) -> bool:
    return bool(text) and not text.rstrip().endswith(PARAGRAPH_END_PUNCT)


def _matches_compact_title(text: str, compact_title: str) -> bool:
    return bool(compact_title) and re.sub(r"[\s#*]+", "", text) == compact_title


def _remove_split_title_lines(lines: list[str], compact_title: str) -> list[str]:
    """Remove one early, multi-line hard-wrapped duplicate of the title."""
    if not compact_title:
        return lines
    for start, line in enumerate(lines[:20]):
        if not line:
            continue
        candidate = ""
        for end in range(start, len(lines)):
            part = lines[end]
            if not part:
                break
            candidate += re.sub(r"\s+", "", part)
            if candidate == compact_title and end > start:
                return [*lines[:start], "", *lines[end + 1 :]]
            if not compact_title.startswith(candidate):
                break
    return lines


def plain_text_to_markdown(text: str, *, title: str = "") -> str:
    """Repair hard-wrapped official text into readable Markdown paragraphs."""
    value = html.unescape(str(text or ""))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\xa0", " ").replace("\u3000", " ")
    raw_lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    compact_title = re.sub(r"\s+", "", title or "")
    raw_lines = _remove_split_title_lines(raw_lines, compact_title)

    blocks: list[str] = []
    paragraph = ""

    def flush() -> None:
        nonlocal paragraph
        if paragraph:
            blocks.append(_format_paragraph(paragraph))
            paragraph = ""

    for line in raw_lines:
        if not line:
            flush()
            continue
        if PAGE_NUMBER_RE.fullmatch(line):
            continue
        if CHAPTER_RE.match(line):
            flush()
            blocks.append(f"## {line}")
            continue
        if (
            ARTICLE_RE.match(line)
            and ARTICLE_RE.match(paragraph)
            and _is_unfinished_paragraph(paragraph)
            and not _matches_compact_title(paragraph, compact_title)
        ):
            paragraph = _join_fragment(paragraph, line)
        elif ARTICLE_RE.match(line) or ITEM_RE.match(line):
            flush()
            paragraph = line
        else:
            paragraph = _join_fragment(paragraph, line)
        if line.endswith(("。", "！", "？", "；")):
            flush()
    flush()

    if blocks and compact_title:
        compact_first_block = re.sub(r"[\s#*]+", "", blocks[0])
        if compact_first_block == compact_title:
            blocks.pop(0)
    return "\n\n".join(block for block in blocks if block).strip()


def _source_assets(entity: dict[str, Any]) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in entity.get("sources") or []:
        local_file = str(source.get("local_file") or "")
        page_url = str(source.get("page_url") or "")
        suffix = Path(local_file).suffix.lower()
        if suffix in {"", ".json"}:
            continue
        key = local_file or page_url
        if not key or key in seen:
            continue
        seen.add(key)
        digest = hashlib.sha1(
            f"{source.get('system')}:{source.get('record_id')}:{key}".encode("utf-8")
        ).hexdigest()[:20]
        local_path = output_path(local_file) if local_file else None
        sha256 = _sha256_file(local_path) if local_path and local_path.exists() else None
        assets.append(
            {
                "asset_id": f"catalog_source_{digest}",
                "kind": "source_document",
                "label": Path(local_file).name or entity.get("title") or "来源文件",
                "source_url": page_url or None,
                "local_file": local_file or None,
                "sha256": sha256,
                "download_status": ("ok" if local_path and local_path.exists() else "source_only"),
                "source_system": source.get("system"),
                "source_record_id": source.get("record_id"),
                "source_role": source.get("role"),
            }
        )
    return assets


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_source_record(asset: dict[str, Any]) -> dict[str, Any] | None:
    record = {
        "source_system": asset.get("source_system"),
        "source_record_id": asset.get("source_record_id"),
        "source_role": asset.get("source_role"),
        "source_url": asset.get("source_url"),
        "local_file": asset.get("local_file"),
    }
    return record if any(record.values()) else None


def _append_unique(target: list[Any], value: Any) -> None:
    if value is None or value == "" or value == [] or value == {}:
        return
    if value not in target:
        target.append(value)


def _merge_asset_into(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    for field in ("asset_id", "source_url", "local_file"):
        if not existing.get(field) and incoming.get(field):
            existing[field] = incoming[field]
    for field in ("source_urls", "local_files", "source_records"):
        existing.setdefault(field, [])

    for value in incoming.get("source_urls") or []:
        _append_unique(existing["source_urls"], value)
    _append_unique(existing["source_urls"], incoming.get("source_url"))

    for value in incoming.get("local_files") or []:
        _append_unique(existing["local_files"], value)
    _append_unique(existing["local_files"], incoming.get("local_file"))

    for record in incoming.get("source_records") or []:
        _append_unique(existing["source_records"], record)
    _append_unique(existing["source_records"], _asset_source_record(incoming))


def _merge_assets(
    inherited: list[dict[str, Any]],
    source_assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for asset in [*inherited, *source_assets]:
        sha256 = str(asset.get("sha256") or "")
        key = (
            f"sha256:{sha256}"
            if sha256
            else str(
                asset.get("local_file") or asset.get("source_url") or asset.get("asset_id") or ""
            )
        )
        if not key:
            continue
        if key not in result:
            merged = dict(asset)
            merged["source_urls"] = list(asset.get("source_urls") or [])
            _append_unique(merged["source_urls"], asset.get("source_url"))
            merged["local_files"] = list(asset.get("local_files") or [])
            _append_unique(merged["local_files"], asset.get("local_file"))
            merged["source_records"] = list(asset.get("source_records") or [])
            _append_unique(merged["source_records"], _asset_source_record(asset))
            result[key] = merged
        else:
            _merge_asset_into(result[key], asset)
    for key, asset in result.items():
        if not asset.get("asset_id"):
            digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
            asset["asset_id"] = f"catalog_asset_{digest}"
    return list(result.values())


def is_comment_draft_entity(entity: dict[str, Any]) -> bool:
    metadata = entity.get("metadata") or {}
    haystack = "\n".join(
        [
            str(entity.get("title") or ""),
            str(metadata.get("name") or ""),
        ]
    )
    return any(pattern in haystack for pattern in COMMENT_DRAFT_PATTERNS)


def is_historical_title_entity(entity: dict[str, Any]) -> bool:
    metadata = entity.get("metadata") or {}
    haystack = "\n".join(
        [
            str(entity.get("title") or ""),
            str(metadata.get("name") or ""),
        ]
    )
    return any(pattern in haystack for pattern in TITLE_HISTORICAL_PATTERNS)


def is_reference_title_entity(entity: dict[str, Any]) -> bool:
    metadata = entity.get("metadata") or {}
    haystack = "\n".join(
        [
            str(entity.get("title") or ""),
            str(metadata.get("name") or ""),
        ]
    )
    return any(pattern in haystack for pattern in REFERENCE_TITLE_PATTERNS)


def catalog_superseded_by() -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for relation in load_json(catalog_relations_path(), {}).get("items") or []:
        if relation.get("relation") != "supersedes":
            continue
        superseded_id = str(relation.get("to") or "")
        superseding_id = str(relation.get("from") or "")
        if not superseded_id or not superseding_id:
            continue
        result[superseded_id].append(
            {
                "canonical_id": superseding_id,
                "source": relation.get("source"),
                "rule_id": relation.get("rule_id"),
                "confidence": relation.get("confidence"),
                "effective_date": (relation.get("evidence") or {}).get("effective_date"),
                "evidence": relation.get("evidence") or {},
            }
        )
    return result


def catalog_relation_refs() -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Index compact incoming/outgoing catalog edges for each canonical law."""
    result: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"outgoing": [], "incoming": []}
    )
    for relation in load_json(catalog_relations_path(), {}).get("items") or []:
        from_id = str(relation.get("from") or "")
        to_id = str(relation.get("to") or "")
        relation_type = str(relation.get("relation") or "")
        if not from_id or not to_id or not relation_type:
            continue
        common = {
            "relation": relation_type,
            "source": relation.get("source"),
            "confidence": relation.get("confidence", 1.0),
            "evidence": relation.get("evidence") or {},
        }
        if relation.get("rule_id"):
            common["rule_id"] = relation["rule_id"]
        result[from_id]["outgoing"].append({"canonical_id": to_id, **common})
        result[to_id]["incoming"].append({"canonical_id": from_id, **common})
    for references in result.values():
        for direction in ("outgoing", "incoming"):
            references[direction].sort(
                key=lambda item: (
                    str(item.get("relation") or ""),
                    str(item.get("canonical_id") or ""),
                    str(item.get("source") or ""),
                )
            )
    return dict(result)


def catalog_publishes() -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    for relation in load_json(catalog_relations_path(), {}).get("items") or []:
        if relation.get("relation") != "publishes":
            continue
        parent_id = str(relation.get("from") or "")
        child_id = str(relation.get("to") or "")
        if parent_id and child_id and child_id not in result[parent_id]:
            result[parent_id].append(child_id)
    return result


def catalog_finalized_by() -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for relation in load_json(catalog_relations_path(), {}).get("items") or []:
        if relation.get("relation") != "finalizes_draft":
            continue
        draft_id = str(relation.get("to") or "")
        formal_id = str(relation.get("from") or "")
        if draft_id and formal_id:
            result[draft_id].append(
                {
                    "canonical_id": formal_id,
                    "source": relation.get("source"),
                    "confidence": relation.get("confidence"),
                    "evidence": relation.get("evidence") or {},
                }
            )
    return result


def catalog_same_instrument_groups() -> list[set[str]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for relation in load_json(catalog_relations_path(), {}).get("items") or []:
        if relation.get("relation") != "same_instrument_copy":
            continue
        left = str(relation.get("from") or "")
        right = str(relation.get("to") or "")
        if left and right:
            adjacency[left].add(right)
            adjacency[right].add(left)
    groups: list[set[str]] = []
    remaining = set(adjacency)
    while remaining:
        seed = remaining.pop()
        group = {seed}
        stack = [seed]
        while stack:
            current = stack.pop()
            for neighbour in adjacency[current] - group:
                group.add(neighbour)
                remaining.discard(neighbour)
                stack.append(neighbour)
        groups.append(group)
    return groups


def _catalog_entity_context(path: Path) -> tuple[dict[str, Any], str, str, dict[str, Any]]:
    entity = load_json(path, {})
    entity_id = str(entity.get("id") or path.stem)
    title = str(entity.get("title") or entity_id)
    source_metadata = dict(entity.get("metadata") or {})
    original_source_id = source_metadata.get("id")
    metadata = {
        **source_metadata,
        "id": entity_id,
        "canonical_id": entity_id,
        "source_id": original_source_id,
        "name": title,
        "document_type": entity.get("document_type") or source_metadata.get("document_type"),
        "status": entity.get("status") or source_metadata.get("status") or "unknown",
    }
    return entity, entity_id, title, metadata


def _preferred_content_state(
    entity: dict[str, Any],
    title: str,
) -> tuple[
    str,
    str,
    str,
    str,
    list[dict[str, Any]],
    list[dict[str, Any]],
    str,
]:
    preferred = entity.get("preferred_content") or {}
    preferred_system = str(preferred.get("source_system") or "")
    preferred_record_id = str(preferred.get("source_record_id") or "")
    plain_text = str(preferred.get("plain_text") or "")
    markdown = ""
    tables: list[dict[str, Any]] = []
    inherited_assets: list[dict[str, Any]] = []

    reused_neris = False
    if preferred_system == "neris" and preferred_record_id:
        neris_path = normalized_laws_dir() / f"reg_{preferred_record_id}.json"
        if neris_path.exists():
            reused_neris = True
            neris = load_json(neris_path, {})
            plain_text = str(neris.get("full_text_plain") or plain_text)
            markdown = str(neris.get("full_text_markdown") or "")
            tables = neris.get("tables") or []
            inherited_assets = neris.get("assets") or []

    if not markdown:
        markdown = plain_text_to_markdown(plain_text, title=title)
    normalization_method = "neris_normalized_reuse" if reused_neris else "plain_text_reflow"
    return (
        preferred_system,
        preferred_record_id,
        plain_text,
        markdown,
        tables,
        inherited_assets,
        normalization_method,
    )


def _catalog_revision_ref(
    entity: dict[str, Any],
    revision_by_law_id: dict[str, str],
) -> dict[str, Any] | None:
    curated_ref = curated_version_ref_for_entity(entity)
    if curated_ref:
        return {
            **curated_ref,
            "relations_file": str(relative_to_output(canonical_dir() / "relations" / "graph.json")),
        }
    neris_source_id = next(
        (
            str(source.get("record_id"))
            for source in (entity.get("sources") or [])
            if source.get("system") == "neris" and source.get("record_id")
        ),
        None,
    )
    if not neris_source_id or neris_source_id not in revision_by_law_id:
        return None
    return {
        "family_id": revision_by_law_id[neris_source_id],
        "relations_file": str(relative_to_output(canonical_dir() / "relations" / "graph.json")),
    }


def _catalog_content_status(plain_text: str) -> str:
    return "full_text" if plain_text.strip() else "metadata_only"


def _infer_effective_date(metadata: dict[str, Any], plain_text: str) -> str | None:
    return infer_effective_date(metadata, plain_text)


def _same_copy_effectiveness_evidence(
    source_files: list[Path],
) -> dict[str, dict[str, Any]]:
    entities = {path.stem: load_json(path, {}) for path in source_files}
    shared: dict[str, dict[str, Any]] = {}
    for group in catalog_same_instrument_groups():
        values: dict[str, set[str]] = defaultdict(set)
        rule_peer_ids: list[str] = []
        for entity_id in group:
            entity = entities.get(entity_id) or {}
            if (entity.get("material_classification") or {}).get("lane") == "rule":
                rule_peer_ids.append(entity_id)
            metadata = entity.get("metadata") or {}
            plain_text = str((entity.get("preferred_content") or {}).get("plain_text") or "")
            effective_date = _infer_effective_date(metadata, plain_text)
            for field, value in (
                ("effective_date", effective_date),
                ("ineffective_date", metadata.get("ineffective_date")),
                ("status", entity.get("status") or metadata.get("status")),
            ):
                text = str(value or "").strip()
                if text and text != "unknown":
                    values[field].add(text)
        inherited: dict[str, Any] = {
            field: next(iter(field_values))
            for field, field_values in values.items()
            if len(field_values) == 1
        }
        if rule_peer_ids:
            inherited["material_lane"] = "rule"
            inherited["material_inherited_from"] = sorted(rule_peer_ids)
        if not inherited:
            continue
        for entity_id in group:
            shared[entity_id] = {
                **inherited,
                "effectiveness_inherited_from": sorted(group - {entity_id}),
            }
    return shared


def effectiveness_for(
    entity: dict[str, Any],
    *,
    superseded_by: list[dict[str, Any]] | None = None,
    material_classification: dict[str, Any] | None = None,
    as_of: str | None = None,
    override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return classify_effectiveness(
        entity,
        material_classification=material_classification,
        superseded_by=superseded_by,
        as_of=as_of,
        override=override,
    )


def normalize_catalog_entity(
    path: Path,
    *,
    revision_by_law_id: dict[str, str] | None = None,
    superseded_by_catalog: dict[str, list[dict[str, Any]]] | None = None,
    publishes_by_catalog: dict[str, list[str]] | None = None,
    finalized_by_catalog: dict[str, list[dict[str, Any]]] | None = None,
    relations_by_catalog: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
    same_copy_evidence: dict[str, dict[str, Any]] | None = None,
    overrides: dict[str, dict[str, Any]] | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    entity, entity_id, title, metadata = _catalog_entity_context(path)
    metadata = apply_curated_metadata_overrides(entity, metadata)
    (
        preferred_system,
        preferred_record_id,
        plain_text,
        markdown,
        tables,
        inherited_assets,
        normalization_method,
    ) = _preferred_content_state(entity, title)
    content_status = _catalog_content_status(plain_text)
    revision_by_law_id = revision_by_law_id or {}
    revision_ref = _catalog_revision_ref(entity, revision_by_law_id)
    superseded_by = (superseded_by_catalog or {}).get(entity_id) or []
    inferred_effective_date = _infer_effective_date(metadata, plain_text)
    if inferred_effective_date:
        metadata["effective_date"] = inferred_effective_date
    superseding_effective_dates = sorted(
        {
            str(item.get("effective_date"))[:10]
            for item in superseded_by
            if _parse_date(item.get("effective_date")) is not None
        }
    )
    if superseding_effective_dates and not _parse_date(metadata.get("ineffective_date")):
        metadata["ineffective_date"] = superseding_effective_dates[0]
    inherited = (same_copy_evidence or {}).get(entity_id) or {}
    for field in ("effective_date", "ineffective_date"):
        if not metadata.get(field) and inherited.get(field):
            metadata[field] = inherited[field]
    if str(metadata.get("status") or "unknown") == "unknown" and inherited.get("status"):
        metadata["status"] = inherited["status"]
    if inherited.get("effectiveness_inherited_from"):
        metadata["effectiveness_inherited_from"] = inherited["effectiveness_inherited_from"]
    classification_entity = {**entity, "metadata": metadata}
    curated_override_fields = {
        str(field)
        for item in metadata.get("curated_override_evidence") or []
        for field in item.get("fields") or []
    }
    if "status" in curated_override_fields and metadata.get("status"):
        classification_entity["status"] = metadata["status"]
    if inherited.get("material_lane") == "rule":
        classification_entity["material_lane"] = "rule"
        metadata["material_inherited_from"] = inherited.get("material_inherited_from") or []
    if str(classification_entity.get("status") or "unknown") == "unknown":
        classification_entity["status"] = metadata.get("status") or "unknown"
    override = (overrides or {}).get(entity_id)
    material_classification = material_classification_for(
        classification_entity,
        publishes=(publishes_by_catalog or {}).get(entity_id),
        override=override,
    )
    effectiveness = effectiveness_for(
        classification_entity,
        superseded_by=superseded_by,
        material_classification=material_classification,
        as_of=as_of,
        override=override,
    )
    enforcement_classification = enforcement_classification_for(
        classification_entity,
        material_classification=material_classification,
    )
    reference_lifecycle = reference_lifecycle_for(
        classification_entity,
        material_classification=material_classification,
        finalized_by=(finalized_by_catalog or {}).get(entity_id),
    )

    return {
        "schema_version": 1,
        "id": entity_id,
        "source_file": relative_to_output(path),
        "normalized_at": utc_now_iso(),
        "normalization_method": normalization_method,
        "content_status": content_status,
        "title": title,
        "document_type": metadata.get("document_type"),
        "status": classification_entity.get("status") or metadata.get("status"),
        "material_lane": entity.get("material_lane"),
        "material_classification": material_classification,
        "effectiveness": effectiveness,
        "reference_lifecycle": reference_lifecycle,
        "enforcement_classification": enforcement_classification,
        "case_id": entity.get("case_id"),
        "document_role": entity.get("document_role"),
        "superseded_by": superseded_by,
        "relations": (relations_by_catalog or {}).get(entity_id)
        or {"outgoing": [], "incoming": []},
        "metadata": metadata,
        "preferred_source": {
            "system": preferred_system,
            "record_id": preferred_record_id,
        },
        "sources": entity.get("sources") or [],
        "revision_ref": revision_ref,
        "full_text_plain": plain_text,
        "full_text_markdown": markdown,
        "tables": tables,
        "assets": _merge_assets(
            entity.get("assets") or [],
            _merge_assets(inherited_assets, _source_assets(entity)),
        ),
    }


def _classification_review_items(
    manifest_items: list[dict[str, Any]],
    *,
    out_dir: Path,
    as_of: str,
    same_copy_conflicts: dict[str, dict[str, list[str]]] | None = None,
) -> list[dict[str, Any]]:
    review_items: list[dict[str, Any]] = []
    for manifest_item in manifest_items:
        doc = load_json(out_dir / Path(str(manifest_item["file"])).name, {})
        material = doc.get("material_classification") or {}
        effectiveness = doc.get("effectiveness") or {}
        reference_lifecycle = doc.get("reference_lifecycle") or {}
        enforcement = doc.get("enforcement_classification") or {}
        metadata = doc.get("metadata") or {}
        reasons: list[str] = []
        lane = str(material.get("lane") or "unknown")
        status = str(effectiveness.get("status") or "unknown")
        if lane == "unknown":
            reasons.append("material_nature_unknown")
        if lane == "rule" and status == "unknown":
            reasons.append("effectiveness_unknown")
        if (material.get("evidence") or {}).get("source_lane_conflict"):
            reasons.append("source_lane_conflict")
        material_confidence = float(material.get("confidence") or 0.0)
        effect_confidence = float(effectiveness.get("confidence") or 0.0)
        if min(material_confidence, effect_confidence) < 0.8:
            reasons.append("low_confidence")
        effective_date = str(metadata.get("effective_date") or "")[:10]
        ineffective_date = str(metadata.get("ineffective_date") or "")[:10]
        if status == "current" and effective_date and effective_date > as_of:
            reasons.append("future_effective_date_marked_current")
        if status == "current" and ineffective_date and ineffective_date <= as_of:
            reasons.append("expired_date_marked_current")
        if lane == "reference" and status != "not_applicable":
            reasons.append("reference_effectiveness_conflict")
        penalty_subtype = disciplinary_penalty_subtype(doc.get("title"))
        if penalty_subtype and enforcement.get("category") != "penalties":
            reasons.append("disciplinary_material_not_penalties")
        if lane == "rule" and enforcement.get("category") == "penalties":
            reasons.append("penalty_rule_conflict")
        if lane == "rule" and is_trial_title(doc.get("title")) and not effective_date:
            reasons.append("trial_missing_commencement_evidence")
        if reference_lifecycle.get("status") == "unfinalized":
            reasons.append("draft_formal_relation_unknown")
        provenances = {
            str(source.get("web_category_provenance"))
            for source in doc.get("sources") or []
            if source.get("web_category_provenance")
        }
        if (
            provenances
            and provenances <= {"endpoint_profile", "url_inference"}
            and material.get("basis") != "manual_override"
        ):
            reasons.append("web_taxonomy_low_provenance")
        copy_conflict = (same_copy_conflicts or {}).get(str(doc.get("id") or ""))
        if copy_conflict:
            reasons.append("same_instrument_effectiveness_conflict")
        if not reasons:
            continue
        source_urls = sorted(
            {
                str(source.get("page_url"))
                for source in doc.get("sources") or []
                if source.get("page_url")
            }
        )
        review_items.append(
            {
                "id": doc.get("id"),
                "title": doc.get("title"),
                "reasons": reasons,
                "material_lane": lane,
                "material_category": material.get("category"),
                "material_confidence": material_confidence,
                "material_basis": material.get("basis"),
                "effectiveness": status,
                "effectiveness_confidence": effect_confidence,
                "effectiveness_basis": effectiveness.get("basis"),
                "reference_lifecycle": reference_lifecycle.get("status"),
                "enforcement_category": enforcement.get("category"),
                "enforcement_subtype": enforcement.get("subtype"),
                "pub_org": metadata.get("pub_org"),
                "fileno": metadata.get("fileno"),
                "pub_date": metadata.get("pub_date"),
                "effective_date": metadata.get("effective_date"),
                "ineffective_date": metadata.get("ineffective_date"),
                "official_sources": source_urls,
                "same_copy_conflict": copy_conflict,
                "canonical_file": manifest_item.get("file"),
            }
        )
    return review_items


def _write_classification_review_queue(
    items: list[dict[str, Any]],
    *,
    as_of: str,
) -> None:
    payload = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "classification_as_of": as_of,
        "count": len(items),
        "items": items,
    }
    save_json(classification_review_queue_path(), payload)
    csv_path = classification_review_queue_csv_path()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "id",
        "title",
        "reasons",
        "material_lane",
        "material_category",
        "material_confidence",
        "material_basis",
        "effectiveness",
        "effectiveness_confidence",
        "effectiveness_basis",
        "reference_lifecycle",
        "enforcement_category",
        "enforcement_subtype",
        "pub_org",
        "fileno",
        "pub_date",
        "effective_date",
        "ineffective_date",
        "official_sources",
        "same_copy_conflict",
        "canonical_file",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in items:
            row = dict(item)
            row["reasons"] = ";".join(item.get("reasons") or [])
            row["official_sources"] = ";".join(item.get("official_sources") or [])
            row["same_copy_conflict"] = json.dumps(
                item.get("same_copy_conflict") or {}, ensure_ascii=False
            )
            writer.writerow(row)
    reason_counts: dict[str, int] = defaultdict(int)
    for item in items:
        for reason in item.get("reasons") or []:
            reason_counts[str(reason)] += 1
    save_json(
        reports_dir() / "classification_quality.json",
        {
            "schema_version": 1,
            "updated_at": utc_now_iso(),
            "classification_as_of": as_of,
            "review_count": len(items),
            "reason_counts": dict(sorted(reason_counts.items())),
            "queue_json": relative_to_output(classification_review_queue_path()),
            "queue_csv": relative_to_output(classification_review_queue_csv_path()),
        },
    )


def normalize_catalog(
    *,
    limit: int | None = None,
    force: bool = False,
    clean: bool = False,
    as_of: str | None = None,
) -> dict[str, Any]:
    as_of = as_of or china_as_of()
    source_files = listed_output_files(
        catalog_manifest_path(),
        field="file",
        fallback_dir=catalog_laws_dir(),
        pattern="law_*.json",
        limit=limit,
    )
    out_dir = catalog_normalized_dir()
    if clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    skipped = 0
    empty_content = 0
    method_counts: dict[str, int] = defaultdict(int)
    items: list[dict[str, Any]] = []
    revision_by_law_id = load_json(revisions_path(), {}).get("by_law_id") or {}
    superseded_by_catalog = catalog_superseded_by()
    relations_by_catalog = catalog_relation_refs()
    publishes_by_catalog = catalog_publishes()
    finalized_by_catalog = catalog_finalized_by()
    same_copy_evidence = _same_copy_effectiveness_evidence(source_files)
    overrides = load_classification_overrides()
    source_ids = {path.stem for path in source_files}
    missing_override_ids = sorted(set(overrides) - source_ids)
    if missing_override_ids:
        raise ValueError(
            "classification overrides reference missing canonical IDs: "
            + ", ".join(missing_override_ids)
        )
    for index, path in enumerate(source_files, start=1):
        out_path = out_dir / path.name
        if out_path.exists() and not force:
            doc = load_json(out_path, {})
            skipped += 1
        else:
            doc = normalize_catalog_entity(
                path,
                revision_by_law_id=revision_by_law_id,
                superseded_by_catalog=superseded_by_catalog,
                publishes_by_catalog=publishes_by_catalog,
                finalized_by_catalog=finalized_by_catalog,
                relations_by_catalog=relations_by_catalog,
                same_copy_evidence=same_copy_evidence,
                overrides=overrides,
                as_of=as_of,
            )
            save_json(out_path, doc)
            written += 1
        if not str(doc.get("full_text_plain") or "").strip():
            empty_content += 1
        method_counts[str(doc.get("normalization_method") or "unknown")] += 1
        items.append(
            {
                "id": doc.get("id"),
                "title": doc.get("title"),
                "status": doc.get("status"),
                "material_lane": (doc.get("material_classification") or {}).get("lane"),
                "material_category": (doc.get("material_classification") or {}).get("category"),
                "effectiveness": (doc.get("effectiveness") or {}).get("status"),
                "effectiveness_basis": (doc.get("effectiveness") or {}).get("basis"),
                "reference_lifecycle": (doc.get("reference_lifecycle") or {}).get("status"),
                "enforcement_category": (doc.get("enforcement_classification") or {}).get(
                    "category"
                ),
                "enforcement_subtype": (doc.get("enforcement_classification") or {}).get("subtype"),
                "case_id": doc.get("case_id"),
                "document_role": doc.get("document_role"),
                "source_system": (doc.get("preferred_source") or {}).get("system"),
                "source_file": relative_to_output(path),
                "file": relative_to_output(out_path),
                "text_length": len(str(doc.get("full_text_plain") or "")),
                "content_status": doc.get("content_status"),
                "assets": len(doc.get("assets") or []),
            }
        )
        if index % 100 == 0 or index == len(source_files):
            log_event(
                "normalize_progress",
                message=f"  normalized catalog {index}/{len(source_files)}",
                index=index,
                total=len(source_files),
            )

    manifest = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "classification_as_of": as_of,
        "source_dir": relative_to_output(catalog_laws_dir()),
        "normalized_dir": relative_to_output(out_dir),
        "count": len(items),
        "written": written,
        "skipped": skipped,
        "empty_content": empty_content,
        "normalization_methods": dict(sorted(method_counts.items())),
        "items": items,
    }
    same_copy_conflicts: dict[str, dict[str, list[str]]] = {}
    for group in catalog_same_instrument_groups():
        group_docs = [load_json(out_dir / f"{entity_id}.json", {}) for entity_id in group]
        fields = {
            field: sorted(
                {
                    str((doc.get("metadata") or {}).get(field) or "")
                    for doc in group_docs
                    if (doc.get("metadata") or {}).get(field)
                }
            )
            for field in ("effective_date", "ineffective_date")
        }
        fields["effectiveness"] = sorted(
            {
                str((doc.get("effectiveness") or {}).get("status") or "")
                for doc in group_docs
                if (doc.get("effectiveness") or {}).get("status")
            }
        )
        conflict = {field: values for field, values in fields.items() if len(values) > 1}
        if conflict:
            for entity_id in group:
                same_copy_conflicts[entity_id] = conflict
    review_items = _classification_review_items(
        items,
        out_dir=out_dir,
        as_of=as_of,
        same_copy_conflicts=same_copy_conflicts,
    )
    _write_classification_review_queue(review_items, as_of=as_of)
    manifest["classification_review_count"] = len(review_items)
    manifest["classification_review_queue"] = relative_to_output(classification_review_queue_path())
    save_json(catalog_normalized_manifest_path(), manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="生成统一法规目录的 normalized 派生层")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument(
        "--as-of",
        default=None,
        help="分类基准日（YYYY-MM-DD，默认中国时区当天）",
    )
    args = parser.parse_args()
    try:
        manifest = normalize_catalog(
            limit=args.limit,
            force=args.force,
            clean=args.clean,
            as_of=args.as_of,
        )
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log_event("cli_error", level="ERROR", message=f"失败: {exc}", error_message=str(exc))
        return 1
    log_event(
        "cli_result",
        message=(
            f"完成: count={manifest['count']} written={manifest['written']} "
            f"skipped={manifest['skipped']} empty={manifest['empty_content']} "
            f"-> {catalog_normalized_manifest_path()}"
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "normalize-catalog"))
