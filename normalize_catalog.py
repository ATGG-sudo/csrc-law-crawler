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

from config import OUTPUT_DIR
from normalize_laws import normalized_laws_dir
from storage import (
    canonical_dir,
    catalog_dir,
    catalog_laws_dir,
    catalog_normalized_dir,
    load_json,
    revisions_path,
    save_json,
    utc_now_iso,
)

CATALOG_NORMALIZED_MANIFEST = catalog_dir() / "normalized_manifest.json"
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


def plain_text_to_markdown(text: str, *, title: str = "") -> str:
    """Repair hard-wrapped official text into readable Markdown paragraphs."""
    value = html.unescape(str(text or ""))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\xa0", " ").replace("\u3000", " ")
    raw_lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]

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
        if ARTICLE_RE.match(line) or ITEM_RE.match(line):
            flush()
            paragraph = line
        else:
            paragraph = _join_fragment(paragraph, line)
        if line.endswith(("。", "！", "？", "；")):
            flush()
    flush()

    compact_title = re.sub(r"\s+", "", title or "")
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
        local_path = OUTPUT_DIR / local_file if local_file else None
        assets.append(
            {
                "asset_id": f"catalog_source_{digest}",
                "kind": "source_document",
                "label": Path(local_file).name or entity.get("title") or "来源文件",
                "source_url": page_url or None,
                "local_file": local_file or None,
                "download_status": (
                    "ok" if local_path and local_path.exists() else "source_only"
                ),
                "source_system": source.get("system"),
                "source_record_id": source.get("record_id"),
                "source_role": source.get("role"),
            }
        )
    return assets


def _merge_assets(
    inherited: list[dict[str, Any]],
    source_assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for asset in [*inherited, *source_assets]:
        key = str(
            asset.get("local_file")
            or asset.get("source_url")
            or asset.get("asset_id")
            or ""
        )
        if key and key not in result:
            result[key] = asset
    return list(result.values())


HISTORICAL_STATUSES = {
    "已失效",
    "失效",
    "已废止",
    "废止",
    "已被修改",
    "被修改",
}
REFERENCE_TYPES = {
    "publication_notice",
    "regulatory_practice",
    "supporting_material",
}


def effectiveness_for(entity: dict[str, Any]) -> dict[str, Any]:
    raw_status = str(entity.get("status") or "unknown")
    document_type = str(entity.get("document_type") or "")
    if raw_status in HISTORICAL_STATUSES:
        status = "historical"
        confidence = 1.0
    elif raw_status == "现行有效":
        status = "current"
        confidence = 1.0
    elif document_type in REFERENCE_TYPES:
        status = "not_applicable"
        confidence = 0.95
    else:
        status = "unknown"
        confidence = 0.5
    return {
        "status": status,
        "raw_status": raw_status,
        "source": (entity.get("preferred_content") or {}).get("source_system"),
        "confidence": confidence,
        "as_of": utc_now_iso()[:10],
    }


def normalize_catalog_entity(
    path: Path,
    *,
    revision_by_law_id: dict[str, str] | None = None,
) -> dict[str, Any]:
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
    preferred = entity.get("preferred_content") or {}
    preferred_system = str(preferred.get("source_system") or "")
    preferred_record_id = str(preferred.get("source_record_id") or "")
    plain_text = str(preferred.get("plain_text") or "")
    markdown = ""
    tables: list[dict[str, Any]] = []
    inherited_assets: list[dict[str, Any]] = []
    revision_ref = None
    normalization_method = "plain_text_reflow"

    if preferred_system == "neris" and preferred_record_id:
        neris_path = normalized_laws_dir() / f"reg_{preferred_record_id}.json"
        if neris_path.exists():
            neris = load_json(neris_path, {})
            plain_text = str(neris.get("full_text_plain") or plain_text)
            markdown = str(neris.get("full_text_markdown") or "")
            tables = neris.get("tables") or []
            inherited_assets = neris.get("assets") or []
            normalization_method = "neris_normalized_reuse"

    if not markdown:
        markdown = plain_text_to_markdown(plain_text, title=title)
    content_status = "full_text" if plain_text.strip() else "metadata_only"
    revision_by_law_id = revision_by_law_id or {}
    neris_source_id = next(
        (
            str(source.get("record_id"))
            for source in (entity.get("sources") or [])
            if source.get("system") == "neris" and source.get("record_id")
        ),
        None,
    )
    if neris_source_id and neris_source_id in revision_by_law_id:
        revision_ref = {
            "family_id": revision_by_law_id[neris_source_id],
            "relations_file": str(
                (canonical_dir() / "relations" / "graph.json").relative_to(OUTPUT_DIR)
            ),
        }
    effectiveness = effectiveness_for(entity)

    return {
        "schema_version": 1,
        "id": entity_id,
        "source_file": str(path.relative_to(OUTPUT_DIR)),
        "normalized_at": utc_now_iso(),
        "normalization_method": normalization_method,
        "content_status": content_status,
        "title": title,
        "document_type": metadata.get("document_type"),
        "status": metadata.get("status"),
        "effectiveness": effectiveness,
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
    source_files = sorted(catalog_laws_dir().glob("law_*.json"))
    if limit is not None:
        source_files = source_files[:limit]
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
    for index, path in enumerate(source_files, start=1):
        out_path = out_dir / path.name
        if out_path.exists() and not force:
            doc = load_json(out_path, {})
            skipped += 1
        else:
            doc = normalize_catalog_entity(
                path,
                revision_by_law_id=revision_by_law_id,
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
                "source_system": (doc.get("preferred_source") or {}).get("system"),
                "source_file": str(path.relative_to(OUTPUT_DIR)),
                "file": str(out_path.relative_to(OUTPUT_DIR)),
                "text_length": len(str(doc.get("full_text_plain") or "")),
                "content_status": doc.get("content_status"),
                "assets": len(doc.get("assets") or []),
            }
        )
        if index % 100 == 0 or index == len(source_files):
            print(f"  normalized catalog {index}/{len(source_files)}")

    manifest = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "source_dir": str(catalog_laws_dir().relative_to(OUTPUT_DIR)),
        "normalized_dir": str(out_dir.relative_to(OUTPUT_DIR)),
        "count": len(items),
        "written": written,
        "skipped": skipped,
        "empty_content": empty_content,
        "normalization_methods": dict(sorted(method_counts.items())),
        "items": items,
    }
    save_json(CATALOG_NORMALIZED_MANIFEST, manifest)
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
        print(f"失败: {exc}", file=sys.stderr)
        return 1
    print(
        f"完成: count={manifest['count']} written={manifest['written']} "
        f"skipped={manifest['skipped']} empty={manifest['empty_content']} "
        f"-> {CATALOG_NORMALIZED_MANIFEST}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
