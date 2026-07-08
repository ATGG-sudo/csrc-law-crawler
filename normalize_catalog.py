#!/usr/bin/env python3
"""Normalize canonical catalog entities into a source-independent text layer."""

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

from catalog_rules import (
    COMMENT_DRAFT_PATTERNS,
    EFFECT_AMAC_OFFICIAL_DEFAULT,
    EFFECT_COMMENT_DRAFT,
    EFFECT_EXPLICIT_CURRENT,
    EFFECT_EXPLICIT_HISTORICAL,
    EFFECT_INSUFFICIENT_EVIDENCE,
    EFFECT_REFERENCE_DOCUMENT_TYPE,
    EFFECT_REFERENCE_TITLE,
    EFFECT_SUPERSEDED_BY_CATALOG,
    HISTORICAL_STATUSES,
    OFFICIAL_RULE_TYPES,
    REFERENCE_TITLE_PATTERNS,
    REFERENCE_TYPES,
)
from normalize_laws import normalized_laws_dir
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
    revisions_path,
    run_with_output_lock,
    save_json,
    utc_now_iso,
)

PAGE_NUMBER_RE = re.compile(r"^\s*\d{1,3}\s*$")
CHAPTER_RE = re.compile(
    r"^第[一二三四五六七八九十百千万零〇两\d]+[章节编]\s*"
)
ARTICLE_RE = re.compile(
    r"^(第[一二三四五六七八九十百千万零〇两\d]+条)(?:\s+|(?=[^\s]))"
)
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
PUBLISH_DATE_EFFECTIVE_PATTERNS = (
    "自发布之日起施行",
    "自发布之日起实施",
    "自公布之日起施行",
    "自公布之日起实施",
    "自印发之日起施行",
    "自印发之日起实施",
)


def catalog_normalized_manifest_path() -> Path:
    return catalog_dir() / "normalized_manifest.json"


def catalog_manifest_path() -> Path:
    return catalog_dir() / "manifest.json"


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
    return f"**{marker}** {remainder}".rstrip()


def _is_unfinished_paragraph(text: str) -> bool:
    return bool(text) and not text.rstrip().endswith(PARAGRAPH_END_PUNCT)


def _matches_compact_title(text: str, compact_title: str) -> bool:
    return bool(compact_title) and re.sub(r"[\s#*]+", "", text) == compact_title


def plain_text_to_markdown(text: str, *, title: str = "") -> str:
    """Repair hard-wrapped official text into readable Markdown paragraphs."""
    value = html.unescape(str(text or ""))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\xa0", " ").replace("\u3000", " ")
    raw_lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    compact_title = re.sub(r"\s+", "", title or "")

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
                "download_status": (
                    "ok" if local_path and local_path.exists() else "source_only"
                ),
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
    for field in ("source_url", "local_file"):
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
                asset.get("local_file")
                or asset.get("source_url")
                or asset.get("asset_id")
                or ""
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
    return list(result.values())


def is_comment_draft_entity(entity: dict[str, Any]) -> bool:
    metadata = entity.get("metadata") or {}
    preferred = entity.get("preferred_content") or {}
    haystack = "\n".join(
        [
            str(entity.get("title") or ""),
            str(metadata.get("name") or ""),
            str(preferred.get("plain_text") or "")[:1200],
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
                "evidence": relation.get("evidence") or {},
            }
        )
    return result


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
        "relations_file": str(
            relative_to_output(canonical_dir() / "relations" / "graph.json")
        ),
    }


def _catalog_content_status(plain_text: str) -> str:
    return "full_text" if plain_text.strip() else "metadata_only"


def _version_date(value: Any) -> str | None:
    text = str(value or "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return None


def _infer_effective_date(metadata: dict[str, Any], plain_text: str) -> str | None:
    if metadata.get("effective_date"):
        return str(metadata["effective_date"])
    if any(pattern in plain_text for pattern in PUBLISH_DATE_EFFECTIVE_PATTERNS):
        return _version_date(metadata.get("version")) or metadata.get("pub_date")
    return None


def effectiveness_for(
    entity: dict[str, Any],
    *,
    superseded_by: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    raw_status = str(entity.get("status") or "unknown")
    document_type = str(entity.get("document_type") or "")
    source_system = (entity.get("preferred_content") or {}).get("source_system")
    superseded_by = superseded_by or []
    if raw_status in HISTORICAL_STATUSES:
        rule = EFFECT_EXPLICIT_HISTORICAL
        status = "historical"
        confidence = rule.confidence
        basis = "explicit_historical_status"
        label = raw_status
    elif is_historical_title_entity(entity):
        rule = EFFECT_EXPLICIT_HISTORICAL
        status = "historical"
        confidence = rule.confidence
        basis = "explicit_historical_status"
        label = "已废止/失效（标题标注）"
    elif is_comment_draft_entity(entity):
        rule = EFFECT_COMMENT_DRAFT
        status = "not_applicable"
        confidence = rule.confidence
        basis = "comment_draft_signal"
        label = "征求意见/仅供参考"
    elif superseded_by:
        rule = EFFECT_SUPERSEDED_BY_CATALOG
        status = "historical"
        confidence = max(
            float(item.get("confidence") or 0.0) for item in superseded_by
        )
        basis = "superseded_by_catalog_relation"
        label = "已被替代"
    elif raw_status == "现行有效":
        rule = EFFECT_EXPLICIT_CURRENT
        status = "current"
        confidence = rule.confidence
        basis = "explicit_current_status"
        label = raw_status
    elif document_type in REFERENCE_TYPES:
        rule = EFFECT_REFERENCE_DOCUMENT_TYPE
        status = "not_applicable"
        confidence = rule.confidence
        basis = "reference_document_type"
        label = "仅供参考"
    elif is_reference_title_entity(entity):
        rule = EFFECT_REFERENCE_TITLE
        status = "not_applicable"
        confidence = rule.confidence
        basis = "reference_title_signal"
        label = "仅供参考"
    elif (
        source_system == "amac"
        and document_type in OFFICIAL_RULE_TYPES
        and raw_status in {"unknown", "", "None"}
    ):
        rule = EFFECT_AMAC_OFFICIAL_DEFAULT
        status = "current"
        confidence = rule.confidence
        basis = "amac_official_rule_default"
        label = "有效（AMAC未显式标注）"
    else:
        rule = EFFECT_INSUFFICIENT_EVIDENCE
        status = "unknown"
        confidence = rule.confidence
        basis = "insufficient_evidence"
        label = "待核验"
    result = {
        "status": status,
        "raw_status": raw_status,
        "label": label,
        "basis": basis,
        "rule_id": rule.rule_id,
        "source": source_system,
        "confidence": confidence,
        "as_of": utc_now_iso()[:10],
    }
    if superseded_by:
        result["superseded_by"] = superseded_by
    return result


def normalize_catalog_entity(
    path: Path,
    *,
    revision_by_law_id: dict[str, str] | None = None,
    superseded_by_catalog: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    entity, entity_id, title, metadata = _catalog_entity_context(path)
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
    effectiveness = effectiveness_for(entity, superseded_by=superseded_by)

    return {
        "schema_version": 1,
        "id": entity_id,
        "source_file": relative_to_output(path),
        "normalized_at": utc_now_iso(),
        "normalization_method": normalization_method,
        "content_status": content_status,
        "title": title,
        "document_type": metadata.get("document_type"),
        "status": metadata.get("status"),
        "effectiveness": effectiveness,
        "superseded_by": superseded_by,
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
        "assets": _merge_assets(inherited_assets, _source_assets(entity)),
    }


def normalize_catalog(
    *,
    limit: int | None = None,
    force: bool = False,
    clean: bool = False,
) -> dict[str, Any]:
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
    revision_by_law_id = (
        load_json(revisions_path(), {}).get("by_law_id") or {}
    )
    superseded_by_catalog = catalog_superseded_by()
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
                "effectiveness": (doc.get("effectiveness") or {}).get("status"),
                "effectiveness_basis": (doc.get("effectiveness") or {}).get("basis"),
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
        "source_dir": relative_to_output(catalog_laws_dir()),
        "normalized_dir": relative_to_output(out_dir),
        "count": len(items),
        "written": written,
        "skipped": skipped,
        "empty_content": empty_content,
        "normalization_methods": dict(sorted(method_counts.items())),
        "items": items,
    }
    save_json(catalog_normalized_manifest_path(), manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="生成统一法规目录的 normalized 派生层")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()
    try:
        manifest = normalize_catalog(
            limit=args.limit,
            force=args.force,
            clean=args.clean,
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
