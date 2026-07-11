"""AMAC candidate crawling and manifest construction."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from asset_text import extract_asset_text_bytes
from config import AMAC_VERIFY_TLS
from runtime import log_event
from storage import (
    amac_sources_dir,
    load_json,
    raw_dir,
    relative_to_output,
    save_json,
    utc_now_iso,
)

from .client import AmacClient
from .discovery import (
    DEFAULT_SITE_KEYWORDS,
    DEFAULT_XWFB_PAGES,
    deduplicate_candidates,
    discover_policy_candidates,
    discover_site_candidates,
    discover_xwfb_rule_notice_candidates,
)
from .identity import (
    canonical_url,
    classified_document_metadata,
    clean_attachment_title,
    clean_text,
    source_record_id,
)
from .parser import ASSET_SUFFIXES, asset_links, content_root, metadata_from_page


def amac_assets_root() -> Path:
    return raw_dir() / "assets" / "amac"


def amac_manifest_path() -> Path:
    return raw_dir() / "amac" / "manifest.json"


def extract_asset_text(data: bytes, suffix: str) -> str:
    return extract_asset_text_bytes(data, suffix)


def download_asset(
    client: AmacClient,
    record_id: str,
    url: str,
    label: str,
) -> dict[str, Any]:
    payload = client.get_binary_payload(url)
    data = payload.data
    suffix = Path(urlsplit(url).path).suffix.lower() or ".bin"
    digest = payload.sha256
    asset_id = f"amac_asset_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:20]}"
    asset_dir = amac_assets_root() / record_id
    asset_dir.mkdir(parents=True, exist_ok=True)
    path = asset_dir / f"{asset_id}{suffix}"
    path.write_bytes(data)
    extracted_text = clean_text(extract_asset_text(data, suffix))
    return {
        "asset_id": asset_id,
        "label": clean_attachment_title(label),
        "source_url": canonical_url(url),
        "local_file": relative_to_output(path),
        "content_type": payload.content_type,
        "size_bytes": payload.size_bytes,
        "sha256": digest,
        "download_status": "ok",
        "extracted_text": extracted_text,
    }


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
        title = clean_text(str(candidate.get("title") or Path(url).name))
        if download_assets:
            asset = download_asset(client, record_id, url, title)
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
            **classified_document_metadata(title, url),
        }
    else:
        response = client.get(url)
        response.encoding = response.apparent_encoding or "utf-8"
        raw_html = response.text
        soup = BeautifulSoup(raw_html, "html.parser")
        root = content_root(soup)
        plain_text = clean_text(root.get_text("\n", strip=True))
        metadata = metadata_from_page(soup, candidate)
        for asset_url, label in asset_links(root, url):
            if download_assets:
                try:
                    assets.append(
                        download_asset(client, record_id, asset_url, label)
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
                            "label": clean_attachment_title(label),
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
                        "label": clean_attachment_title(label),
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
                    **classified_document_metadata(
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
    xwfb_pages: int = DEFAULT_XWFB_PAGES,
    keywords: list[str] | None = None,
    download_assets: bool = True,
    force: bool = False,
    delay_min: float = 0.25,
    delay_max: float = 0.7,
    verify_tls: bool = AMAC_VERIFY_TLS,
) -> dict[str, Any]:
    client = AmacClient(delay_min=delay_min, delay_max=delay_max, verify_tls=verify_tls)
    candidates = discover_policy_candidates(client, limit=policy_limit)
    candidates.extend(
        discover_xwfb_rule_notice_candidates(
            client,
            max_pages=xwfb_pages,
        )
    )
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
                log_event(
                    "amac_record_failed",
                    level="ERROR",
                    message=f"  !! AMAC失败: {candidate.get('title')} | {exc}",
                    title=candidate.get("title"),
                    url=candidate.get("url"),
                    error_message=str(exc),
                )
                continue
        metadata = record.get("metadata") or {}
        items.append(
            {
                "source_record_id": record_id,
                "name": metadata.get("name"),
                "document_type": metadata.get("document_type"),
                "status": metadata.get("status"),
                "file": relative_to_output(path),
                "assets": len(record.get("assets") or []),
            }
        )
        if index % 50 == 0 or index == len(candidates):
            log_event(
                "amac_progress",
                message=f"  AMAC {index}/{len(candidates)}",
                index=index,
                total=len(candidates),
            )

    manifest = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "candidate_count": len(candidates),
        "count": len(items),
        "written": written,
        "skipped": skipped,
        "failed": len(failures),
        "keywords": keywords or DEFAULT_SITE_KEYWORDS,
        "xwfb_pages": xwfb_pages,
        "tls_policy": {
            "verify": client.verify_tls,
            "source": "default" if verify_tls == AMAC_VERIFY_TLS else "cli",
        },
        "items": items,
        "failures": failures,
    }
    save_json(amac_manifest_path(), manifest)
    return manifest


__all__ = [
    "amac_assets_root",
    "amac_manifest_path",
    "crawl_amac",
    "crawl_candidate",
    "download_asset",
    "extract_asset_text",
]
