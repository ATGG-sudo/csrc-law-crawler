#!/usr/bin/env python3
"""Crawl AMAC as a supplemental official source without mutating NERIS records."""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
import urllib3
from bs4 import BeautifulSoup, Tag

from config import AMAC_BASE_URL, AMAC_RULES_BASE_URL, OUTPUT_DIR, USER_AGENT
from storage import (
    amac_sources_dir,
    load_json,
    raw_dir,
    save_json,
    utc_now_iso,
)

POLICY_SEARCH_URL = urljoin(
    AMAC_BASE_URL,
    "portal/ESSearch/wcmPolicy/getPolicyDataByKeyword_v2",
)
SITE_SEARCH_URL = urljoin(
    AMAC_BASE_URL,
    "portal/ESSearch/doc/findDocsByKeyword",
)
AMAC_ASSETS_ROOT = raw_dir() / "assets" / "amac"
AMAC_MANIFEST = raw_dir() / "amac" / "manifest.json"

DEFAULT_SITE_KEYWORDS = [
    "私募基金登记备案动态",
    "登记备案案例",
    "备案业务问答",
    "备案须知",
    "备案关注要点",
]

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

TITLE_PREFIX_RE = re.compile(r"^附件(?:\s*\d+(?:-\d+)?)?\s*[：:、.\-]?\s*")
DATE_SUFFIX_RE = re.compile(r"\s+\d{2}-\d{2}$")
FILENO_RE = re.compile(
    r"((?:中基协|证监会|基金业协会)[发字]?\s*[〔\[]\s*\d{4}\s*[〕\]]\s*\d+\s*号)"
)


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    path = re.sub(r"/+", "/", parts.path)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def source_record_id(url: str) -> str:
    digest = hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:24]
    return f"amac_{digest}"


def _clean_text(value: str) -> str:
    value = html.unescape(value or "").replace("\xa0", " ").replace("\u3000", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _clean_attachment_title(value: str) -> str:
    value = DATE_SUFFIX_RE.sub("", _clean_text(value))
    value = TITLE_PREFIX_RE.sub("", value)
    return value.strip() or "未命名附件"


def classify_document(title: str, url: str) -> str:
    if title.startswith("关于发布") or title.endswith(("公告", "通知")):
        return "publication_notice"
    if "登记备案动态" in title or "/dbdt/" in url:
        return "regulatory_practice"
    if any(word in title for word in ("办法", "规则", "指引", "准则", "细则", "规定")):
        return "self_regulatory_rule"
    return "supporting_material"


class AmacClient:
    def __init__(self, *, delay_min: float = 0.25, delay_max: float = 0.7) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": AMAC_BASE_URL,
            }
        )
        self.delay_min = delay_min
        self.delay_max = delay_max

    def _pause(self) -> None:
        if self.delay_max > 0:
            time.sleep(random.uniform(self.delay_min, self.delay_max))

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        self._pause()
        verify = not urlsplit(url).netloc.lower().startswith("fg.amac.org.cn")
        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        response = self.session.get(url, timeout=60, verify=verify, **kwargs)
        response.raise_for_status()
        return response

    def get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        return self.get(url, params=params).json()


def discover_policy_candidates(
    client: AmacClient,
    *,
    page_size: int = 100,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    page = 1
    total: int | None = None
    while total is None or len(candidates) < total:
        payload = client.get_json(
            POLICY_SEARCH_URL,
            {
                "keyword": "",
                "sortFlag": 2,
                "program": "",
                "lawPromNum": "",
                "parId": "",
                "childId": "",
                "pageSize": page_size,
                "pageNo": page,
                "searchType": 1,
            },
        )
        data = ((payload.get("data") or {}).get("data") or {})
        result = data.get("searchListVos") or {}
        total = int(result.get("total") or 0)
        rows = result.get("dataList") or []
        for row in rows:
            relative_url = str(row.get("docPubUrl") or "")
            if not relative_url:
                continue
            candidates.append(
                {
                    "title": row.get("docTitle"),
                    "url": urljoin(AMAC_RULES_BASE_URL, relative_url),
                    "published_at": row.get("lawPromTime"),
                    "search_content": row.get("docContent"),
                    "discovery_channel": "policy_search",
                    "search_raw": row,
                }
            )
            if limit is not None and len(candidates) >= limit:
                return candidates
        if not rows:
            break
        page += 1
    return candidates


def discover_site_candidates(
    client: AmacClient,
    keywords: Iterable[str],
    *,
    page_size: int = 100,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for keyword in keywords:
        payload = client.get_json(
            SITE_SEARCH_URL,
            {
                "keyword": keyword,
                "flag": 1,
                "pageNo": 1,
                "pageSize": page_size,
                "sortFlag": 2,
                "searchType": 0,
            },
        )
        data = ((payload.get("data") or {}).get("data") or {})
        result = data.get("wcmDocuments") or {}
        for row in result.get("dataList") or []:
            relative_url = str(row.get("docPubUrl") or "")
            if not relative_url:
                continue
            candidates.append(
                {
                    "title": row.get("docTitle"),
                    "url": urljoin(AMAC_BASE_URL, relative_url),
                    "published_at": row.get("docRelTime"),
                    "search_content": row.get("docContent"),
                    "discovery_channel": "site_search",
                    "search_keyword": keyword,
                    "search_raw": row,
                }
            )
            if limit is not None and len(candidates) >= limit:
                return candidates
    return candidates


def deduplicate_candidates(
    candidates: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_url: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        url = canonical_url(str(candidate.get("url") or ""))
        if not url:
            continue
        if url not in by_url:
            by_url[url] = {**candidate, "url": url, "discovery": []}
        by_url[url]["discovery"].append(
            {
                "channel": candidate.get("discovery_channel"),
                "keyword": candidate.get("search_keyword"),
            }
        )
    return list(by_url.values())


def _content_root(soup: BeautifulSoup) -> Tag:
    return (
        soup.select_one(".job-infos")
        or soup.select_one(".TRS_Editor")
        or soup.select_one(".article-content")
        or soup.select_one("main")
        or soup.body
        or soup
    )


def _metadata_from_page(
    soup: BeautifulSoup,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    fields: dict[str, str] = {}
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        for index in range(0, len(cells) - 1, 2):
            key = _clean_text(cells[index].get_text(" ", strip=True))
            value = _clean_text(cells[index + 1].get_text(" ", strip=True))
            if key:
                fields[key] = value
    title = _clean_text(str(candidate.get("title") or ""))
    if not title and soup.title:
        title = _clean_text(soup.title.get_text(" ", strip=True))
    fileno_match = FILENO_RE.search(_clean_text(soup.get_text("\n", strip=True)))
    status = fields.get("效力状态") or "unknown"
    return {
        "name": title,
        "fileno": fields.get("文号") or (fileno_match.group(1) if fileno_match else None),
        "pub_org": fields.get("发文单位") or "中国证券投资基金业协会",
        "pub_date": fields.get("发文日期") or candidate.get("published_at"),
        "effective_date": fields.get("实施日期") or None,
        "ineffective_date": fields.get("失效日期") or None,
        "status": status if status else "unknown",
        "document_type": classify_document(title, str(candidate.get("url") or "")),
    }


def _extract_asset_text(data: bytes, suffix: str) -> str:
    suffix = suffix.lower()
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            return "\n\n".join((page.extract_text() or "") for page in reader.pages)
        if suffix == ".docx":
            from docx import Document

            document = Document(io.BytesIO(data))
            return "\n".join(paragraph.text for paragraph in document.paragraphs)
    except Exception:
        return ""
    return ""


def _download_asset(
    client: AmacClient,
    record_id: str,
    url: str,
    label: str,
) -> dict[str, Any]:
    response = client.get(url, headers={"Accept": "*/*", "Referer": url})
    data = response.content
    suffix = Path(urlsplit(url).path).suffix.lower() or ".bin"
    digest = hashlib.sha256(data).hexdigest()
    asset_id = f"amac_asset_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:20]}"
    asset_dir = AMAC_ASSETS_ROOT / record_id
    asset_dir.mkdir(parents=True, exist_ok=True)
    path = asset_dir / f"{asset_id}{suffix}"
    path.write_bytes(data)
    extracted_text = _clean_text(_extract_asset_text(data, suffix))
    return {
        "asset_id": asset_id,
        "label": _clean_attachment_title(label),
        "source_url": canonical_url(url),
        "local_file": str(path.relative_to(OUTPUT_DIR)),
        "content_type": (
            response.headers.get("Content-Type") or ""
        ).split(";")[0].strip().lower(),
        "size_bytes": len(data),
        "sha256": digest,
        "download_status": "ok",
        "extracted_text": extracted_text,
    }


def _asset_links(root: Tag, page_url: str) -> list[tuple[str, str]]:
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
        label = _clean_text(anchor.get_text(" ", strip=True)) or Path(
            urlsplit(url).path
        ).name
        result.append((url, label))
    return result


def crawl_candidate(
    client: AmacClient,
    candidate: dict[str, Any],
    *,
    download_assets: bool,
) -> dict[str, Any]:
    url = canonical_url(str(candidate["url"]))
    record_id = source_record_id(url)
    suffix = Path(urlsplit(url).path).suffix.lower()
    assets: list[dict[str, Any]] = []
    raw_html = ""
    plain_text = ""

    if suffix in ASSET_SUFFIXES:
        title = _clean_text(str(candidate.get("title") or Path(url).name))
        if download_assets:
            asset = _download_asset(client, record_id, url, title)
            assets.append(asset)
            plain_text = asset.get("extracted_text") or ""
        metadata = {
            "name": title,
            "fileno": None,
            "pub_org": "中国证券投资基金业协会",
            "pub_date": candidate.get("published_at"),
            "effective_date": None,
            "ineffective_date": None,
            "status": "unknown",
            "document_type": classify_document(title, url),
        }
    else:
        response = client.get(url)
        response.encoding = response.apparent_encoding or "utf-8"
        raw_html = response.text
        soup = BeautifulSoup(raw_html, "html.parser")
        root = _content_root(soup)
        plain_text = _clean_text(root.get_text("\n", strip=True))
        metadata = _metadata_from_page(soup, candidate)
        for asset_url, label in _asset_links(root, url):
            if download_assets:
                try:
                    assets.append(
                        _download_asset(client, record_id, asset_url, label)
                    )
                except Exception as exc:
                    assets.append(
                        {
                            "asset_id": (
                                "amac_asset_"
                                + hashlib.sha1(
                                    asset_url.encode("utf-8")
                                ).hexdigest()[:20]
                            ),
                            "label": _clean_attachment_title(label),
                            "source_url": asset_url,
                            "local_file": None,
                            "download_status": "failed",
                            "download_error": str(exc),
                        }
                    )
            else:
                assets.append(
                    {
                        "asset_id": (
                            "amac_asset_"
                            + hashlib.sha1(asset_url.encode("utf-8")).hexdigest()[:20]
                        ),
                        "label": _clean_attachment_title(label),
                        "source_url": asset_url,
                        "local_file": None,
                        "download_status": "pending",
                    }
                )

    attachment_documents = []
    for asset in assets:
        attachment_documents.append(
            {
                "source_record_id": asset["asset_id"],
                "metadata": {
                    "name": asset.get("label"),
                    "fileno": None,
                    "pub_org": metadata.get("pub_org"),
                    "pub_date": metadata.get("pub_date"),
                    "effective_date": metadata.get("effective_date"),
                    "ineffective_date": metadata.get("ineffective_date"),
                    "status": metadata.get("status") or "unknown",
                    "document_type": classify_document(
                        str(asset.get("label") or ""),
                        str(asset.get("source_url") or ""),
                    ),
                },
                "content": {
                    "plain_text": asset.get("extracted_text") or "",
                },
                "asset_id": asset["asset_id"],
                "source": {
                    "page_url": url,
                    "asset_url": asset.get("source_url"),
                    "role": "published_attachment",
                },
            }
        )

    content_hash = hashlib.sha256(plain_text.encode("utf-8")).hexdigest()
    return {
        "schema_version": 1,
        "source_record_id": record_id,
        "source_system": "amac",
        "metadata": metadata,
        "content": {
            "raw_html": raw_html,
            "plain_text": plain_text,
            "content_sha256": content_hash,
        },
        "assets": assets,
        "attachment_documents": attachment_documents,
        "source": {
            "page_url": url,
            "discovery": candidate.get("discovery") or [],
            "search_content": candidate.get("search_content"),
            "search_raw": candidate.get("search_raw"),
            "crawled_at": utc_now_iso(),
        },
    }


def crawl_amac(
    *,
    policy_limit: int | None = None,
    site_limit: int | None = None,
    keywords: list[str] | None = None,
    download_assets: bool = True,
    force: bool = False,
    delay_min: float = 0.25,
    delay_max: float = 0.7,
) -> dict[str, Any]:
    client = AmacClient(delay_min=delay_min, delay_max=delay_max)
    candidates = discover_policy_candidates(client, limit=policy_limit)
    candidates.extend(
        discover_site_candidates(
            client,
            keywords or DEFAULT_SITE_KEYWORDS,
            limit=site_limit,
        )
    )
    candidates = deduplicate_candidates(candidates)
    amac_sources_dir().mkdir(parents=True, exist_ok=True)

    items = []
    written = 0
    skipped = 0
    failures = []
    for index, candidate in enumerate(candidates, start=1):
        record_id = source_record_id(str(candidate["url"]))
        path = amac_sources_dir() / f"{record_id}.json"
        existing_record = load_json(path, {}) if path.exists() else {}
        candidate_suffix = Path(
            urlsplit(str(candidate.get("url") or "")).path
        ).suffix.lower()
        pending_assets = any(
            asset.get("download_status") != "ok"
            for asset in (existing_record.get("assets") or [])
        )
        direct_asset_missing = (
            candidate_suffix in ASSET_SUFFIXES
            and not (existing_record.get("assets") or [])
        )
        should_refresh = force or (
            download_assets and (pending_assets or direct_asset_missing)
        )
        if path.exists() and not should_refresh:
            record = existing_record
            skipped += 1
        else:
            try:
                record = crawl_candidate(
                    client,
                    candidate,
                    download_assets=download_assets,
                )
                save_json(path, record)
                written += 1
            except Exception as exc:
                failures.append(
                    {
                        "url": candidate.get("url"),
                        "title": candidate.get("title"),
                        "error": str(exc),
                    }
                )
                print(f"  !! AMAC失败: {candidate.get('title')} | {exc}")
                continue
        metadata = record.get("metadata") or {}
        items.append(
            {
                "source_record_id": record_id,
                "name": metadata.get("name"),
                "document_type": metadata.get("document_type"),
                "status": metadata.get("status"),
                "file": str(path.relative_to(OUTPUT_DIR)),
                "assets": len(record.get("assets") or []),
            }
        )
        if index % 50 == 0 or index == len(candidates):
            print(f"  AMAC {index}/{len(candidates)}")

    manifest = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "candidate_count": len(candidates),
        "count": len(items),
        "written": written,
        "skipped": skipped,
        "failed": len(failures),
        "keywords": keywords or DEFAULT_SITE_KEYWORDS,
        "items": items,
        "failures": failures,
    }
    save_json(AMAC_MANIFEST, manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 AMAC 补充制度和实践材料")
    parser.add_argument("--policy-limit", type=int, default=None)
    parser.add_argument("--site-limit", type=int, default=None)
    parser.add_argument("--keyword", action="append", dest="keywords")
    parser.add_argument("--no-download-assets", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--delay-min", type=float, default=0.25)
    parser.add_argument("--delay-max", type=float, default=0.7)
    args = parser.parse_args()
    try:
        manifest = crawl_amac(
            policy_limit=args.policy_limit,
            site_limit=args.site_limit,
            keywords=args.keywords,
            download_assets=not args.no_download_assets,
            force=args.force,
            delay_min=args.delay_min,
            delay_max=args.delay_max,
        )
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"失败: {exc}", file=sys.stderr)
        return 1
    print(
        f"完成: candidates={manifest['candidate_count']} count={manifest['count']} "
        f"written={manifest['written']} failed={manifest['failed']} -> {AMAC_MANIFEST}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
