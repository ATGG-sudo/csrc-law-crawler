#!/usr/bin/env python3
"""Normalize raw law JSON into search-friendly derived documents.

The raw files under raw/neris/laws/ are immutable source material. This
script writes intermediate JSON to work/normalized_neris/laws/.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

from config import BASE_URL, OUTPUT_DIR
from storage import (
    attachment_index_path,
    laws_dir,
    load_json,
    save_json,
    utc_now_iso,
    work_dir,
)

NORMALIZED_SUBDIR = "normalized"
ASSETS_SUBDIR = "assets"

BLOCK_TAGS = {
    "address",
    "article",
    "blockquote",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "li",
    "ol",
    "p",
    "pre",
    "section",
    "tr",
    "ul",
}

ASSET_EXTENSIONS = {
    ".bmp",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".rar",
    ".rtf",
    ".tif",
    ".tiff",
    ".txt",
    ".xls",
    ".xlsx",
    ".zip",
}

HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9:_-]*(?:\s[^<>]*)?>")
ANGLE_RE = re.compile(r"<([^<>]+)>")


def normalized_laws_dir() -> Path:
    return work_dir() / "normalized_neris" / "laws"


def normalized_manifest_path() -> Path:
    return work_dir() / "normalized_neris" / "manifest.json"


def _protect_non_html_angles(raw: str) -> str:
    """Escape non-HTML angle-bracket text before handing it to BeautifulSoup."""

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if HTML_TAG_RE.fullmatch(token):
            return token
        return token.replace("<", "&lt;").replace(">", "&gt;")

    return ANGLE_RE.sub(replace, raw)


def _soup_fragment(raw: str) -> BeautifulSoup:
    return BeautifulSoup(_protect_non_html_angles(raw or ""), "html.parser")


def _clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _clean_markdown(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t\f\v]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _cell_text(cell: Tag) -> str:
    return _clean_text(cell.get_text(" ", strip=True))


def _table_matrix(table: Tag) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if not cells:
            cells = row.find_all(["th", "td"])
        values = [_cell_text(cell) for cell in cells]
        if any(values):
            rows.append(values)
    return rows


def _matrix_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    header = padded[0]
    body = padded[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _looks_like_asset_url(url: str) -> bool:
    parsed = urlparse(url)
    lower_path = parsed.path.lower()
    suffix = Path(lower_path).suffix
    return "rdqsheader/file" in lower_path or suffix in ASSET_EXTENSIONS


def _resolve_url(ref: str, detail_url: str) -> str:
    ref = (ref or "").strip()
    if not ref:
        return ""
    # Some NERIS fragments contain malformed attributes such as
    # src="../rdqsHeader/file/abc style='height:0px'".  Keep the URL token and
    # drop the leaked style/class attributes.
    ref = re.split(r"\s+(?:style|class|width|height|alt|title)=", ref, maxsplit=1)[0]
    ref = ref.strip("\"'")
    for scheme in ("https://", "http://"):
        nested_at = ref.find(scheme, 1)
        if nested_at > 0:
            ref = ref[nested_at:]
            break
    if ref.startswith("//"):
        return f"https:{ref}"
    base = detail_url or BASE_URL
    return urljoin(base, ref)


def _asset_id(kind: str, source_url: str) -> str:
    digest = hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:16]
    return f"{kind}_{digest}"


class LawNormalizer:
    def __init__(self, law_id: str, source_file: str, detail_url: str) -> None:
        self.law_id = law_id
        self.source_file = source_file
        self.detail_url = detail_url
        self.assets_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        self.tables: list[dict[str, Any]] = []

    def register_asset(
        self,
        *,
        kind: str,
        ref: str,
        label: str,
        section: str,
        entry_id: str | None = None,
        item_id: str | None = None,
        context: str = "",
    ) -> dict[str, Any]:
        source_url = _resolve_url(ref, self.detail_url)
        key = (kind, source_url)
        asset = self.assets_by_key.get(key)
        if asset is None:
            asset = {
                "asset_id": _asset_id(kind, source_url),
                "kind": kind,
                "source_url": source_url,
                "original_ref": ref,
                "label": label,
                "local_file": None,
                "content_type": None,
                "sha256": None,
                "size_bytes": None,
                "download_status": "pending",
                "refs": [],
            }
            self.assets_by_key[key] = asset
        ref_record = {
            "section": section,
            "entry_id": entry_id,
            "item_id": item_id,
            "context": context,
        }
        if ref_record not in asset["refs"]:
            asset["refs"].append(ref_record)
        return asset

    def register_table(
        self,
        table: Tag,
        *,
        section: str,
        entry_id: str | None = None,
        item_id: str | None = None,
        context: str = "",
    ) -> dict[str, Any]:
        rows = _table_matrix(table)
        markdown = _matrix_to_markdown(rows)
        table_id = f"table_{len(self.tables) + 1:04d}"
        record = {
            "table_id": table_id,
            "section": section,
            "entry_id": entry_id,
            "item_id": item_id,
            "context": context,
            "rows": rows,
            "markdown": markdown,
            "raw_html": str(table),
        }
        self.tables.append(record)
        return record

    def normalize_fragment(
        self,
        raw: str,
        *,
        section: str,
        entry_id: str | None = None,
        item_id: str | None = None,
        context: str = "",
    ) -> dict[str, Any]:
        soup = _soup_fragment(raw or "")
        fragment_tables: list[str] = []
        fragment_assets: list[str] = []

        def plain_node(node: Tag | NavigableString) -> str:
            if isinstance(node, NavigableString):
                return str(node)
            if not isinstance(node, Tag):
                return ""
            name = node.name.lower() if node.name else ""
            if name == "br":
                return "\n"
            if name == "img":
                label = node.get("alt") or node.get("title") or "图片"
                asset = self.register_asset(
                    kind="image",
                    ref=node.get("src") or "",
                    label=label,
                    section=section,
                    entry_id=entry_id,
                    item_id=item_id,
                    context=context,
                )
                if asset["asset_id"] not in fragment_assets:
                    fragment_assets.append(asset["asset_id"])
                return f"[图片:{label}]"
            if name == "table":
                table = self.register_table(
                    node,
                    section=section,
                    entry_id=entry_id,
                    item_id=item_id,
                    context=context,
                )
                fragment_tables.append(table["table_id"])
                return "\n" + table["markdown"] + "\n"

            parts = [plain_node(child) for child in node.children]
            text = "".join(parts)
            if name in BLOCK_TAGS:
                return f"\n{text}\n"
            return text

        def markdown_node(node: Tag | NavigableString) -> str:
            if isinstance(node, NavigableString):
                return str(node)
            if not isinstance(node, Tag):
                return ""
            name = node.name.lower() if node.name else ""
            if name == "br":
                return "\n"
            if name == "img":
                label = node.get("alt") or node.get("title") or "图片"
                asset = self.register_asset(
                    kind="image",
                    ref=node.get("src") or "",
                    label=label,
                    section=section,
                    entry_id=entry_id,
                    item_id=item_id,
                    context=context,
                )
                if asset["asset_id"] not in fragment_assets:
                    fragment_assets.append(asset["asset_id"])
                return f"![{label}](asset:{asset['asset_id']})"
            if name == "a":
                href = node.get("href") or ""
                label = _clean_text("".join(markdown_node(child) for child in node.children)) or href
                if href and _looks_like_asset_url(_resolve_url(href, self.detail_url)):
                    asset = self.register_asset(
                        kind="attachment",
                        ref=href,
                        label=label,
                        section=section,
                        entry_id=entry_id,
                        item_id=item_id,
                        context=context,
                    )
                    if asset["asset_id"] not in fragment_assets:
                        fragment_assets.append(asset["asset_id"])
                    return f"[{label}](asset:{asset['asset_id']})"
                if href:
                    return f"[{label}]({_resolve_url(href, self.detail_url)})"
                return label
            if name == "table":
                table = self.register_table(
                    node,
                    section=section,
                    entry_id=entry_id,
                    item_id=item_id,
                    context=context,
                )
                if table["table_id"] not in fragment_tables:
                    fragment_tables.append(table["table_id"])
                return "\n\n" + table["markdown"] + "\n\n"

            text = "".join(markdown_node(child) for child in node.children)
            if name in BLOCK_TAGS:
                return f"\n{text}\n"
            return text

        plain = _clean_text("".join(plain_node(child) for child in soup.contents))
        markdown = _clean_markdown("".join(markdown_node(child) for child in soup.contents))
        return {
            "raw_html": raw or "",
            "plain": plain,
            "markdown": markdown,
            "tables": fragment_tables,
            "assets": fragment_assets,
        }

    def assets(self) -> list[dict[str, Any]]:
        return sorted(self.assets_by_key.values(), key=lambda item: item["asset_id"])


def _entry_context(entry: dict[str, Any]) -> str:
    return entry.get("title") or entry.get("code") or entry.get("entry_id") or ""


def build_normalized_law(path: Path) -> dict[str, Any]:
    source = load_json(path, {})
    metadata = copy.deepcopy(source.get("metadata") or {})
    law_id = str(metadata.get("id") or path.stem.removeprefix("reg_"))
    source_file = str(path.relative_to(OUTPUT_DIR))
    detail_url = ((source.get("source") or {}).get("detail_url") or "")
    normalizer = LawNormalizer(law_id, source_file, detail_url)
    attachment_index = load_json(attachment_index_path(law_id), {})
    source_attachments = (
        attachment_index.get("attachments")
        or source.get("source_attachments")
        or []
    )

    body_ago = normalizer.normalize_fragment(
        metadata.get("body_ago") or "",
        section="body_ago",
        context=metadata.get("name") or law_id,
    )
    body_aft = normalizer.normalize_fragment(
        metadata.get("body_aft") or "",
        section="body_aft",
        context=metadata.get("name") or law_id,
    )

    source_attachment_state: dict[str, dict[str, Any]] = {}
    for attachment in source_attachments:
        source_url = attachment.get("source_url")
        if not source_url:
            continue
        asset = normalizer.register_asset(
            kind="attachment",
            ref=str(source_url),
            label=attachment.get("name") or "附件",
            section="source_attachment",
            context=metadata.get("name") or law_id,
        )
        source_attachment_state[asset["asset_id"]] = attachment

    normalized_entries: list[dict[str, Any]] = []
    for entry in source.get("entries") or []:
        entry_id = entry.get("entry_id")
        entry_context = _entry_context(entry)
        normalized_text = normalizer.normalize_fragment(
            entry.get("text") or "",
            section="entry",
            entry_id=entry_id,
            context=entry_context,
        )
        normalized_items: list[dict[str, Any]] = []
        for item in entry.get("items") or []:
            item_id = item.get("entry_id")
            item_text = normalizer.normalize_fragment(
                item.get("text") or "",
                section="item",
                entry_id=entry_id,
                item_id=item_id,
                context=item.get("title") or item.get("code") or entry_context,
            )
            normalized_items.append(
                {
                    "entry_id": item_id,
                    "code": item.get("code"),
                    "title": item.get("title") or "",
                    "text_raw_html": item.get("text") or "",
                    "text_plain": item_text["plain"],
                    "text_markdown": item_text["markdown"],
                    "tables": item_text["tables"],
                    "assets": item_text["assets"],
                }
            )

        normalized_entries.append(
            {
                "entry_id": entry_id,
                "code": entry.get("code"),
                "class_code": entry.get("class_code"),
                "title": entry.get("title") or "",
                "text_raw_html": entry.get("text") or "",
                "text_plain": normalized_text["plain"],
                "text_markdown": normalized_text["markdown"],
                "tables": normalized_text["tables"],
                "assets": normalized_text["assets"],
                "items": normalized_items,
            }
        )

    plain_parts = [metadata.get("name") or "", body_ago["plain"]]
    markdown_parts = [metadata.get("name") or "", body_ago["markdown"]]
    for entry in normalized_entries:
        title = entry.get("title") or ""
        if title:
            plain_parts.append(title)
            markdown_parts.append(f"## {title}")
        if entry.get("text_plain"):
            plain_parts.append(entry["text_plain"])
        if entry.get("text_markdown"):
            markdown_parts.append(entry["text_markdown"])
        for item in entry.get("items") or []:
            if item.get("title"):
                plain_parts.append(item["title"])
                markdown_parts.append(f"### {item['title']}")
            if item.get("text_plain"):
                plain_parts.append(item["text_plain"])
            if item.get("text_markdown"):
                markdown_parts.append(item["text_markdown"])
    if body_aft["plain"]:
        plain_parts.append(body_aft["plain"])
    if body_aft["markdown"]:
        markdown_parts.append(body_aft["markdown"])

    assets = normalizer.assets()
    prior_normalized = load_json(normalized_laws_dir() / path.name, {})
    prior_asset_manifest = load_json(
        OUTPUT_DIR
        / "raw"
        / ASSETS_SUBDIR
        / "embedded"
        / law_id
        / "asset_manifest.json",
        {},
    )
    prior_assets = {
        str(item.get("asset_id")): item
        for item in (prior_asset_manifest.get("assets") or [])
        if item.get("asset_id")
    }
    prior_assets.update(
        {
            str(item.get("asset_id")): item
            for item in (prior_normalized.get("assets") or [])
            if item.get("asset_id")
        }
    )
    for asset in assets:
        prior = prior_assets.get(str(asset.get("asset_id")))
        if prior:
            asset.update(
                {
                    key: prior.get(key)
                    for key in (
                        "local_file",
                        "content_type",
                        "sha256",
                        "size_bytes",
                        "download_status",
                        "download_error",
                    )
                    if prior.get(key) is not None
                }
            )
        attachment = source_attachment_state.get(str(asset.get("asset_id")))
        if attachment:
            asset.update(
                {
                    "local_file": attachment.get("local_file"),
                    "content_type": attachment.get("content_type"),
                    "sha256": attachment.get("sha256"),
                    "size_bytes": attachment.get("size_bytes"),
                    "download_status": attachment.get("download_status") or "pending",
                    "download_error": attachment.get("download_error"),
                    "source_attachment_id": attachment.get("attachment_id"),
                }
            )

    return {
        "source_file": source_file,
        "normalized_at": utc_now_iso(),
        "metadata": metadata,
        "entry_class_code": source.get("entry_class_code"),
        "revision_ref": source.get("revision_ref"),
        "source": source.get("source"),
        "body_ago": body_ago,
        "body_aft": body_aft,
        "entries": normalized_entries,
        "full_text_plain": "\n\n".join(part for part in plain_parts if part),
        "full_text_markdown": "\n\n".join(part for part in markdown_parts if part),
        "tables": normalizer.tables,
        "assets": assets,
    }


def normalize_laws(*, limit: int | None = None, force: bool = False) -> dict[str, Any]:
    out_dir = normalized_laws_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    law_files = sorted(laws_dir().glob("reg_*.json"))
    if limit is not None:
        law_files = law_files[:limit]

    manifest_items: list[dict[str, Any]] = []
    written = 0
    skipped = 0
    total_tables = 0
    total_assets = 0

    for index, path in enumerate(law_files, start=1):
        law_id = path.stem.removeprefix("reg_")
        out_path = out_dir / path.name
        if out_path.exists() and not force:
            doc = load_json(out_path, {})
            skipped += 1
        else:
            doc = build_normalized_law(path)
            save_json(out_path, doc)
            written += 1

        total_tables += len(doc.get("tables") or [])
        total_assets += len(doc.get("assets") or [])
        metadata = doc.get("metadata") or {}
        manifest_items.append(
            {
                "id": law_id,
                "name": metadata.get("name"),
                "source_file": str(path.relative_to(OUTPUT_DIR)),
                "file": str(out_path.relative_to(OUTPUT_DIR)),
                "tables": len(doc.get("tables") or []),
                "assets": len(doc.get("assets") or []),
            }
        )
        if index % 100 == 0 or index == len(law_files):
            print(f"  normalized {index}/{len(law_files)}")

    manifest = {
        "updated_at": utc_now_iso(),
        "source_dir": "laws",
        "normalized_dir": str(out_dir.relative_to(OUTPUT_DIR)),
        "count": len(manifest_items),
        "written": written,
        "skipped": skipped,
        "tables": total_tables,
        "assets": total_assets,
        "items": manifest_items,
    }
    save_json(normalized_manifest_path(), manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="生成法规清洗派生文件")
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 个法规文件")
    parser.add_argument("--force", action="store_true", help="覆盖已有 normalized/laws 文件")
    args = parser.parse_args()

    try:
        manifest = normalize_laws(limit=args.limit, force=args.force)
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130

    print(
        "完成: "
        f"count={manifest['count']} written={manifest['written']} "
        f"skipped={manifest['skipped']} tables={manifest['tables']} "
        f"assets={manifest['assets']} -> {normalized_manifest_path()}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
