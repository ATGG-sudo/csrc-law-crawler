"""Shared Markdown export helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from storage import output_dir, output_path

ASSET_LINK_RE = re.compile(r"\(asset:([A-Za-z0-9_-]+)\)")
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SPACE_RE = re.compile(r"\s+")
MAX_TITLE_BYTES = 120
MAX_FILENO_BYTES = 50
MAX_DATE_BYTES = 20


def yaml_scalar(value: Any) -> str:
    if value is None:
        return '""'
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def clean_table_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\n", " ").replace("|", "\\|").strip()


def relative_markdown_link(markdown_path: Path, local_file: str | None) -> str | None:
    if not local_file:
        return None
    target = output_path(local_file)
    if not target.exists():
        return None
    depth = len(markdown_path.relative_to(output_dir()).parents) - 1
    return Path(*([".."] * depth), local_file).as_posix()


def filename_part(value: Any, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    text = text.replace("\u3000", " ")
    text = INVALID_FILENAME_RE.sub(" ", text)
    text = SPACE_RE.sub(" ", text).strip(" .")
    return text or fallback


def truncate_utf8(text: str, max_bytes: int) -> str:
    text = text.strip(" .")
    while len(text.encode("utf-8")) > max_bytes:
        text = text[:-1].rstrip(" .")
    return text


def filename_stem(metadata: dict[str, Any], law_id: str) -> str:
    parts = [
        truncate_utf8(filename_part(metadata.get("name"), "无标题"), MAX_TITLE_BYTES),
        truncate_utf8(filename_part(metadata.get("fileno"), "无文号"), MAX_FILENO_BYTES),
        truncate_utf8(
            filename_part(metadata.get("effective_date"), "无施行日期"),
            MAX_DATE_BYTES,
        ),
    ]
    stem = " - ".join(parts)
    return stem or law_id


def asset_map(doc: dict[str, Any], markdown_path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for asset in doc.get("assets") or []:
        asset_id = asset.get("asset_id")
        if not asset_id:
            continue
        local_link = relative_markdown_link(markdown_path, asset.get("local_file"))
        result[asset_id] = {
            **asset,
            "markdown_link": local_link or asset.get("source_url") or f"asset:{asset_id}",
        }
    return result


def replace_asset_links(markdown: str, assets: dict[str, dict[str, Any]]) -> str:
    def replace(match: re.Match[str]) -> str:
        asset_id = match.group(1)
        asset = assets.get(asset_id)
        if not asset:
            return match.group(0)
        return f"({asset['markdown_link']})"

    return ASSET_LINK_RE.sub(replace, markdown)


def strip_leading_title(markdown: str, title: str) -> str:
    markdown = markdown.strip()
    if title and markdown.startswith(title):
        return markdown[len(title) :].lstrip()
    return markdown


def assets_section(assets: dict[str, dict[str, Any]]) -> str:
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
            target = f"[{clean_table_value(label)}]({link})"
        else:
            target = clean_table_value(label)
        lines.append(
            "| "
            + " | ".join(
                [
                    clean_table_value(asset_id),
                    clean_table_value(asset.get("kind")),
                    clean_table_value(asset.get("download_status")),
                    target,
                ]
            )
            + " |"
        )
    return "\n".join(lines)
