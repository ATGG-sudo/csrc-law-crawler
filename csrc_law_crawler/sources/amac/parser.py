"""HTML parsing helpers for AMAC source pages."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .identity import canonical_url, classified_document_metadata, clean_text

ASSET_SUFFIXES = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".zip",
    ".rar",
    ".rtf",
    ".wps",
}

FILENO_RE = re.compile(
    r"((?:中基协|证监会|基金业协会)[发字]?\s*[〔\[]\s*\d{4}\s*[〕\]]\s*\d+\s*号)"
)


def content_root(soup: BeautifulSoup) -> Tag:
    return (
        soup.select_one(".job-infos")
        or soup.select_one(".TRS_Editor")
        or soup.select_one(".article-content")
        or soup.select_one("main")
        or soup.body
        or soup
    )


def title_from_page(soup: BeautifulSoup) -> str:
    for selector in (
        ".content-right .title h3",
        ".article-title",
        "h1",
        "h2",
    ):
        node = soup.select_one(selector)
        if node:
            title = clean_text(node.get_text(" ", strip=True))
            if title:
                return title
    if soup.title:
        return clean_text(soup.title.get_text(" ", strip=True))
    return ""


def metadata_from_page(
    soup: BeautifulSoup,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    fields: dict[str, str] = {}
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        for index in range(0, len(cells) - 1, 2):
            key = clean_text(cells[index].get_text(" ", strip=True))
            value = clean_text(cells[index + 1].get_text(" ", strip=True))
            if key:
                fields[key] = value
    title = clean_text(str(candidate.get("title") or ""))
    page_title = title_from_page(soup)
    if not title or "..." in title or "…" in title:
        title = page_title or title
    fileno_match = FILENO_RE.search(clean_text(soup.get_text("\n", strip=True)))
    status = fields.get("效力状态") or "unknown"
    return {
        "name": title,
        "fileno": fields.get("文号") or (fileno_match.group(1) if fileno_match else None),
        "pub_org": fields.get("发文单位") or "中国证券投资基金业协会",
        "pub_date": fields.get("发文日期") or candidate.get("published_at"),
        "effective_date": fields.get("实施日期") or None,
        "ineffective_date": fields.get("失效日期") or None,
        "status": status if status else "unknown",
        **classified_document_metadata(title, str(candidate.get("url") or "")),
    }


def asset_links(root: Tag, page_url: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in root.find_all("a", href=True):
        url = canonical_url(urljoin(page_url, str(anchor.get("href") or "")))
        suffix = Path(urlsplit(url).path).suffix.lower()
        if suffix not in ASSET_SUFFIXES:
            continue
        if url in seen:
            continue
        seen.add(url)
        label = clean_text(anchor.get_text(" ", strip=True)) or Path(
            urlsplit(url).path
        ).name
        result.append((url, label))
    return result


__all__ = [
    "ASSET_SUFFIXES",
    "FILENO_RE",
    "asset_links",
    "content_root",
    "metadata_from_page",
    "title_from_page",
]
