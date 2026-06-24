#!/usr/bin/env python3
"""Export normalized law JSON files to Markdown documents."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from config import OUTPUT_DIR
from normalize_laws import normalized_laws_dir
from storage import load_json, save_json, utc_now_iso

MARKDOWN_ROOT = OUTPUT_DIR / "markdown"
MARKDOWN_LAWS_DIR = MARKDOWN_ROOT / "laws"
MARKDOWN_CURRENT_DIR = MARKDOWN_LAWS_DIR / "current"
MARKDOWN_OTHER_DIR = MARKDOWN_LAWS_DIR / "other"
MARKDOWN_MANIFEST = MARKDOWN_ROOT / "manifest.json"

ASSET_LINK_RE = re.compile(r"\(asset:([A-Za-z0-9_-]+)\)")
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SPACE_RE = re.compile(r"\s+")
MAX_TITLE_BYTES = 120
MAX_FILENO_BYTES = 50
MAX_DATE_BYTES = 20


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return '""'
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _clean_table_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\n", " ").replace("|", "\\|").strip()


def _relative_markdown_link(markdown_path: Path, local_file: str | None) -> str | None:
    if not local_file:
        return None
    target = OUTPUT_DIR / local_file
    if not target.exists():
        return None
    return Path(*([".."] * (len(markdown_path.relative_to(OUTPUT_DIR).parents) - 1)), local_file).as_posix()


def _filename_part(value: Any, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    text = text.replace("\u3000", " ")
    text = INVALID_FILENAME_RE.sub(" ", text)
    text = SPACE_RE.sub(" ", text).strip(" .")
    return text or fallback


def _truncate_utf8(text: str, max_bytes: int) -> str:
    text = text.strip(" .")
    while len(text.encode("utf-8")) > max_bytes:
        text = text[:-1].rstrip(" .")
    return text


def _filename_stem(metadata: dict[str, Any], law_id: str) -> str:
    parts = [
        _truncate_utf8(_filename_part(metadata.get("name"), "无标题"), MAX_TITLE_BYTES),
        _truncate_utf8(_filename_part(metadata.get("fileno"), "无文号"), MAX_FILENO_BYTES),
        _truncate_utf8(
            _filename_part(metadata.get("effective_date"), "无施行日期"),
            MAX_DATE_BYTES,
        ),
    ]
    stem = " - ".join(parts)
    return stem or law_id


def _target_dir(metadata: dict[str, Any]) -> Path:
    if metadata.get("status") == "现行有效":
        return MARKDOWN_CURRENT_DIR
    return MARKDOWN_OTHER_DIR


def _target_path(
    metadata: dict[str, Any],
    law_id: str,
    used_paths: set[Path],
    *,
    allow_existing: bool,
) -> Path:
    target_dir = _target_dir(metadata)
    stem = _filename_stem(metadata, law_id)
    candidate = target_dir / f"{stem}.md"
    if candidate not in used_paths and (allow_existing or not candidate.exists()):
        used_paths.add(candidate)
        return candidate
    suffix = law_id[:8]
    candidate = target_dir / f"{stem} - {suffix}.md"
    counter = 2
    while candidate in used_paths or candidate.exists():
        candidate = target_dir / f"{stem} - {suffix}-{counter}.md"
        counter += 1
    used_paths.add(candidate)
    return candidate


def _asset_map(doc: dict[str, Any], markdown_path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for asset in doc.get("assets") or []:
        asset_id = asset.get("asset_id")
        if not asset_id:
            continue
        local_link = _relative_markdown_link(markdown_path, asset.get("local_file"))
        result[asset_id] = {
            **asset,
            "markdown_link": local_link or asset.get("source_url") or f"asset:{asset_id}",
        }
    return result


def _replace_asset_links(markdown: str, assets: dict[str, dict[str, Any]]) -> str:
    def replace(match: re.Match[str]) -> str:
        asset_id = match.group(1)
        asset = assets.get(asset_id)
        if not asset:
            return match.group(0)
        return f"({asset['markdown_link']})"

    return ASSET_LINK_RE.sub(replace, markdown)


def _strip_leading_title(markdown: str, title: str) -> str:
    markdown = markdown.strip()
    if title and markdown.startswith(title):
        return markdown[len(title) :].lstrip()
    return markdown


def _front_matter(metadata: dict[str, Any], doc: dict[str, Any]) -> str:
    keys = {
        "id": metadata.get("id"),
        "title": metadata.get("name"),
        "number": metadata.get("number"),
        "fileno": metadata.get("fileno"),
        "pub_org": metadata.get("pub_org"),
        "pub_date": metadata.get("pub_date"),
        "effective_date": metadata.get("effective_date"),
        "status": metadata.get("status"),
        "version": metadata.get("version"),
        "source_file": doc.get("source_file"),
    }
    lines = ["---"]
    for key, value in keys.items():
        lines.append(f"{key}: {_yaml_scalar(value)}")
    revision_ref = doc.get("revision_ref") or {}
    if revision_ref:
        lines.append(f"revision_ref: {_yaml_scalar(revision_ref.get('family_id'))}")
    lines.append("---")
    return "\n".join(lines)


def _metadata_table(metadata: dict[str, Any]) -> str:
    rows = [
        ("法规 ID", metadata.get("id")),
        ("规则编号", metadata.get("number")),
        ("文号", metadata.get("fileno")),
        ("发布机构", metadata.get("pub_org")),
        ("发布日期", metadata.get("pub_date")),
        ("施行日期", metadata.get("effective_date")),
        ("效力状态", metadata.get("status")),
        ("版本", metadata.get("version")),
    ]
    lines = ["| 字段 | 值 |", "| --- | --- |"]
    for key, value in rows:
        lines.append(f"| {key} | {_clean_table_value(value)} |")
    return "\n".join(lines)


def _assets_section(assets: dict[str, dict[str, Any]]) -> str:
    if not assets:
        return ""
    lines = [
        "## 资产",
        "",
        "| ID | 类型 | 状态 | 本地/来源 |",
        "| --- | --- | --- | --- |",
    ]
    for asset_id in sorted(assets):
        asset = assets[asset_id]
        label = asset.get("label") or asset_id
        link = asset.get("markdown_link") or asset.get("source_url") or ""
        if link:
            target = f"[{_clean_table_value(label)}]({link})"
        else:
            target = _clean_table_value(label)
        lines.append(
            "| "
            + " | ".join(
                [
                    _clean_table_value(asset_id),
                    _clean_table_value(asset.get("kind")),
                    _clean_table_value(asset.get("download_status")),
                    target,
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def build_markdown(doc: dict[str, Any], markdown_path: Path) -> str:
    metadata = doc.get("metadata") or {}
    title = metadata.get("name") or metadata.get("id") or markdown_path.stem
    assets = _asset_map(doc, markdown_path)
    body = _strip_leading_title(doc.get("full_text_markdown") or "", title)
    body = _replace_asset_links(body, assets)

    parts = [
        _front_matter(metadata, doc),
        f"# {title}",
        _metadata_table(metadata),
    ]
    if body:
        parts.append(body)
    assets_section = _assets_section(assets)
    if assets_section:
        parts.append(assets_section)
    return "\n\n".join(parts).rstrip() + "\n"


def export_markdown(
    *,
    limit: int | None = None,
    force: bool = False,
    clean: bool = False,
) -> dict[str, Any]:
    if not normalized_laws_dir().exists():
        raise FileNotFoundError("normalized/laws 不存在，请先运行 python normalize_laws.py")

    if clean and MARKDOWN_LAWS_DIR.exists():
        shutil.rmtree(MARKDOWN_LAWS_DIR)
    MARKDOWN_CURRENT_DIR.mkdir(parents=True, exist_ok=True)
    MARKDOWN_OTHER_DIR.mkdir(parents=True, exist_ok=True)
    normalized_files = sorted(normalized_laws_dir().glob("reg_*.json"))
    if limit is not None:
        normalized_files = normalized_files[:limit]

    manifest_items: list[dict[str, Any]] = []
    used_paths: set[Path] = set()
    written = 0
    skipped = 0
    current_count = 0
    other_count = 0

    for index, path in enumerate(normalized_files, start=1):
        doc = load_json(path, {})
        metadata = doc.get("metadata") or {}
        law_id = str(metadata.get("id") or path.stem.removeprefix("reg_"))
        out_path = _target_path(metadata, law_id, used_paths, allow_existing=force)
        bucket = "current" if out_path.parent == MARKDOWN_CURRENT_DIR else "other"
        if bucket == "current":
            current_count += 1
        else:
            other_count += 1
        if out_path.exists() and not force:
            skipped += 1
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(build_markdown(doc, out_path), encoding="utf-8")
            written += 1
        manifest_items.append(
            {
                "id": law_id,
                "name": metadata.get("name"),
                "fileno": metadata.get("fileno"),
                "effective_date": metadata.get("effective_date"),
                "status": metadata.get("status"),
                "bucket": bucket,
                "source_file": str(path.relative_to(OUTPUT_DIR)),
                "file": str(out_path.relative_to(OUTPUT_DIR)),
                "tables": len(doc.get("tables") or []),
                "assets": len(doc.get("assets") or []),
            }
        )
        if index % 100 == 0 or index == len(normalized_files):
            print(f"  exported {index}/{len(normalized_files)}")

    manifest = {
        "updated_at": utc_now_iso(),
        "source_dir": str(normalized_laws_dir().relative_to(OUTPUT_DIR)),
        "markdown_dir": str(MARKDOWN_LAWS_DIR.relative_to(OUTPUT_DIR)),
        "count": len(manifest_items),
        "current_count": current_count,
        "other_count": other_count,
        "written": written,
        "skipped": skipped,
        "filename_pattern": "title - fileno - effective_date.md",
        "items": manifest_items,
    }
    save_json(MARKDOWN_MANIFEST, manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="将 normalized/laws 导出为 Markdown")
    parser.add_argument("--limit", type=int, default=None, help="仅导出前 N 个法规")
    parser.add_argument("--force", action="store_true", help="覆盖已有 Markdown 文件")
    parser.add_argument("--clean", action="store_true", help="导出前清空 markdown/laws")
    args = parser.parse_args()

    try:
        manifest = export_markdown(limit=args.limit, force=args.force, clean=args.clean)
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"失败: {exc}", file=sys.stderr)
        return 1

    print(
        "完成: "
        f"count={manifest['count']} written={manifest['written']} "
        f"skipped={manifest['skipped']} current={manifest['current_count']} "
        f"other={manifest['other_count']} -> {MARKDOWN_MANIFEST}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
