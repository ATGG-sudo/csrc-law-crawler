"""AMAC candidate crawling and manifest construction."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from asset_text import extract_asset_text_bytes
from config import AMAC_VERIFY_TLS
from parser import infer_effective_date, infer_pub_date
from runtime import log_event
from storage import (
    amac_sources_dir,
    load_json,
    output_path,
    raw_dir,
    relative_to_output,
    save_bytes,
    save_json,
    utc_now_iso,
)

from .client import AmacClient
from .discovery import (
    DEFAULT_INDUSTRY_RESEARCH_PAGES,
    DEFAULT_SELF_REGULATORY_MANAGEMENT_PAGES,
    DEFAULT_SELF_REGULATORY_MEASURE_PAGES,
    DEFAULT_SITE_KEYWORDS,
    DEFAULT_XWFB_PAGES,
    deduplicate_candidates,
    discover_industry_research_candidates,
    discover_policy_candidates,
    discover_self_regulatory_management_candidates,
    discover_self_regulatory_measure_candidates,
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

PDF_FILENAME_SUFFIX_RE = re.compile(r"\.pdf$", re.IGNORECASE)
WINDOWS_FILENAME_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
MAX_ASSET_FILENAME_STEM_LENGTH = 180


def amac_assets_root() -> Path:
    return raw_dir() / "assets" / "amac"


def amac_manifest_path() -> Path:
    return raw_dir() / "amac" / "manifest.json"


def extract_asset_text(data: bytes, suffix: str) -> str:
    return extract_asset_text_bytes(data, suffix)


def _asset_id(url: str) -> str:
    return f"amac_asset_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:20]}"


def _safe_filename_part(value: str, fallback: str) -> str:
    value = WINDOWS_FILENAME_UNSAFE_RE.sub(" ", clean_text(value))
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or fallback


def _pdf_filename_title(label: str) -> str:
    title = PDF_FILENAME_SUFFIX_RE.sub("", clean_attachment_title(label)).strip()
    return _safe_filename_part(title, "未命名附件")


def _asset_filename_date(published_at: Any) -> str:
    value = clean_text(str(published_at or ""))
    match = ISO_DATE_RE.search(value)
    if match:
        return match.group(0)
    return _safe_filename_part(value, "未知日期")


def _readable_pdf_filename(label: str, published_at: Any) -> str:
    date = _asset_filename_date(published_at)
    suffix = ".pdf"
    separator = f" - {date}"
    title = _pdf_filename_title(label)
    max_title_length = max(1, MAX_ASSET_FILENAME_STEM_LENGTH - len(separator))
    if len(title) > max_title_length:
        title = title[:max_title_length].rstrip(" .") or "未命名附件"
    return f"{title}{separator}{suffix}"


def _asset_filename(
    url: str,
    label: str,
    published_at: Any,
    asset_id: str,
) -> str:
    suffix = _asset_suffix(url) or ".bin"
    if suffix == ".pdf":
        return _readable_pdf_filename(label, published_at)
    return f"{asset_id}{suffix}"


def _unique_asset_path(
    asset_dir: Path,
    filename: str,
    asset_id: str,
    *,
    current_path: Path | None = None,
) -> Path:
    path = asset_dir / filename
    if current_path is not None and path == current_path:
        return path
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    short_id = asset_id.removeprefix("amac_asset_")[:8] or "asset"
    fallback = asset_dir / f"{stem} - {short_id}{suffix}"
    if current_path is not None and fallback == current_path:
        return fallback
    if not fallback.exists():
        return fallback

    counter = 2
    while True:
        candidate = asset_dir / f"{stem} - {short_id}-{counter}{suffix}"
        if current_path is not None and candidate == current_path:
            return candidate
        if not candidate.exists():
            return candidate
        counter += 1


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_file_matches_record(asset: dict[str, Any]) -> bool:
    local_file = asset.get("local_file")
    expected_sha256 = asset.get("sha256")
    if not local_file or not expected_sha256:
        return False
    path = output_path(str(local_file))
    return path.exists() and _file_sha256(path) == expected_sha256


def _asset_suffix(url: str) -> str:
    return Path(urlsplit(url).path).suffix.lower()


def _asset_matches_suffixes(url: str, asset_suffixes: set[str] | None) -> bool:
    return asset_suffixes is None or _asset_suffix(url) in asset_suffixes


def _skipped_asset(
    asset_url: str,
    label: str,
    asset_suffixes: set[str],
) -> dict[str, Any]:
    return {
        "asset_id": _asset_id(asset_url),
        "label": clean_attachment_title(label),
        "source_url": asset_url,
        "local_file": None,
        "content_type": None,
        "size_bytes": None,
        "sha256": None,
        "download_status": (
            "skipped_non_pdf"
            if asset_suffixes == {".pdf"} and _asset_suffix(asset_url) != ".pdf"
            else "skipped_asset_suffix"
        ),
    }


def _pending_asset(asset_url: str, label: str) -> dict[str, Any]:
    return {
        "asset_id": _asset_id(asset_url),
        "label": clean_attachment_title(label),
        "source_url": asset_url,
        "local_file": None,
        "content_type": None,
        "size_bytes": None,
        "sha256": None,
        "download_status": "pending",
    }


def _failed_asset(asset_url: str, label: str, exc: Exception) -> dict[str, Any]:
    return {
        "asset_id": _asset_id(asset_url),
        "label": clean_attachment_title(label),
        "source_url": asset_url,
        "local_file": None,
        "content_type": None,
        "size_bytes": None,
        "sha256": None,
        "download_status": "failed",
        "download_error": str(exc),
    }


def _asset_needs_refresh(
    asset: dict[str, Any],
    asset_suffixes: set[str] | None,
) -> bool:
    source_url = str(asset.get("source_url") or "")
    if asset_suffixes is not None and not _asset_matches_suffixes(source_url, asset_suffixes):
        return False
    status = asset.get("download_status")
    if status in {"pending", "failed"}:
        return True
    if status == "ok":
        return not _asset_file_matches_record(asset)
    return False


def _asset_stats(records: list[dict[str, Any]]) -> dict[str, int]:
    stats = {
        "pdf_assets_downloaded": 0,
        "pdf_assets_failed": 0,
        "non_pdf_assets_skipped": 0,
    }
    for record in records:
        for asset in record.get("assets") or []:
            status = asset.get("download_status")
            suffix = _asset_suffix(str(asset.get("source_url") or ""))
            if suffix == ".pdf" and status == "ok":
                stats["pdf_assets_downloaded"] += 1
            elif suffix == ".pdf" and status == "failed":
                stats["pdf_assets_failed"] += 1
            elif status == "skipped_non_pdf":
                stats["non_pdf_assets_skipped"] += 1
    return stats


def download_asset(
    client: AmacClient,
    record_id: str,
    url: str,
    label: str,
    published_at: Any = None,
) -> dict[str, Any]:
    payload = client.get_binary_payload(url)
    data = payload.data
    suffix = Path(urlsplit(url).path).suffix.lower() or ".bin"
    digest = payload.sha256
    asset_id = _asset_id(url)
    asset_dir = amac_assets_root() / record_id
    asset_dir.mkdir(parents=True, exist_ok=True)
    filename = _asset_filename(url, label, published_at, asset_id)
    path = _unique_asset_path(asset_dir, filename, asset_id)
    save_bytes(path, data)
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


def _apply_candidate_metadata(
    metadata: dict[str, Any],
    candidate: dict[str, Any],
) -> None:
    source_sections = {
        "disciplinary_institution": "受处分机构",
        "disciplinary_person": "受处分人员",
        "abnormal_operation": "异常经营",
        "missing_institution": "失联机构",
        "self_regulatory_measure": "自律措施",
        "industry_research_report": "研究报告",
        "industry_voice": "声音",
        "industry_esg_research": "ESG研究",
    }
    discovery_channel = str(candidate.get("discovery_channel") or "")
    if discovery_channel not in source_sections:
        return
    category_labels: dict[str, str] = {}
    for discovery in candidate.get("discovery") or []:
        channel = str(discovery.get("channel") or "")
        if channel in source_sections and channel not in category_labels:
            category_labels[channel] = str(
                discovery.get("keyword") or source_sections[channel]
            )
    if discovery_channel not in category_labels:
        category_labels[discovery_channel] = str(
            candidate.get("search_keyword") or source_sections[discovery_channel]
        )
    metadata["source_category"] = discovery_channel
    metadata["source_section"] = (
        candidate.get("search_keyword") or source_sections[discovery_channel]
    )
    metadata["source_categories"] = list(category_labels)
    metadata["source_sections"] = list(category_labels.values())


def crawl_candidate(
    client: AmacClient,
    candidate: dict[str, Any],
    *,
    download_assets: bool,
    asset_suffixes: set[str] | None = None,
) -> dict[str, Any]:
    url = canonical_url(str(candidate["url"]))
    record_id = source_record_id(url)
    suffix = Path(urlsplit(url).path).suffix.lower()
    assets: list[dict[str, Any]] = []
    raw_html = ""
    plain_text = ""

    if suffix in ASSET_SUFFIXES:
        title = clean_text(str(candidate.get("title") or Path(url).name))
        if asset_suffixes is not None and suffix not in asset_suffixes:
            assets.append(_skipped_asset(url, title, asset_suffixes))
        elif download_assets:
            asset = download_asset(
                client,
                record_id,
                url,
                title,
                candidate.get("published_at"),
            )
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
        metadata["pub_date"] = infer_pub_date(metadata, url)
        metadata["effective_date"] = infer_effective_date(metadata, plain_text)
        for asset_url, label in asset_links(root, url):
            if asset_suffixes is not None and not _asset_matches_suffixes(
                asset_url,
                asset_suffixes,
            ):
                assets.append(_skipped_asset(asset_url, label, asset_suffixes))
            elif download_assets:
                try:
                    assets.append(
                        download_asset(
                            client,
                            record_id,
                            asset_url,
                            label,
                            metadata.get("pub_date") or candidate.get("published_at"),
                        )
                    )
                except Exception as exc:
                    assets.append(_failed_asset(asset_url, label, exc))
            else:
                assets.append(_pending_asset(asset_url, label))

    _apply_candidate_metadata(metadata, candidate)
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


def _expected_asset_path(
    record_id: str,
    asset: dict[str, Any],
    published_at: Any,
    current_path: Path | None = None,
) -> Path:
    asset_id = str(asset.get("asset_id") or _asset_id(str(asset.get("source_url") or "")))
    filename = _asset_filename(
        str(asset.get("source_url") or ""),
        str(asset.get("label") or ""),
        published_at,
        asset_id,
    )
    asset_dir = amac_assets_root() / record_id
    return _unique_asset_path(
        asset_dir,
        filename,
        asset_id,
        current_path=current_path,
    )


def _migrate_record_asset_filenames(record: dict[str, Any]) -> int:
    record_id = str(record.get("source_record_id") or "")
    if not record_id:
        return 0

    metadata = record.get("metadata") or {}
    published_at = metadata.get("pub_date")
    renamed = 0
    for asset in record.get("assets") or []:
        if asset.get("download_status") != "ok":
            continue
        source_url = str(asset.get("source_url") or "")
        if _asset_suffix(source_url) != ".pdf":
            continue
        if not _asset_file_matches_record(asset):
            continue
        local_file = str(asset.get("local_file") or "")
        current_path = output_path(local_file)
        expected_path = _expected_asset_path(
            record_id,
            asset,
            published_at,
            current_path=current_path,
        )
        if current_path == expected_path:
            continue
        expected_path.parent.mkdir(parents=True, exist_ok=True)
        if expected_path.exists():
            if _file_sha256(expected_path) == str(asset.get("sha256") or ""):
                current_path.unlink()
            else:
                expected_path = _unique_asset_path(
                    expected_path.parent,
                    expected_path.name,
                    str(asset.get("asset_id") or ""),
                )
                current_path.rename(expected_path)
        else:
            current_path.rename(expected_path)
        asset["local_file"] = relative_to_output(expected_path)
        renamed += 1
    return renamed


def _record_asset_failures(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures = []
    for record in records:
        metadata = record.get("metadata") or {}
        for asset in record.get("assets") or []:
            if asset.get("download_status") != "failed":
                continue
            failures.append(
                {
                    "failure_type": "asset",
                    "source_record_id": record.get("source_record_id"),
                    "asset_id": asset.get("asset_id"),
                    "url": asset.get("source_url"),
                    "title": asset.get("label") or metadata.get("name"),
                    "error": asset.get("download_error") or "asset download failed",
                }
            )
    return failures


def _all_manifest_items() -> list[dict[str, Any]]:
    items = []
    for path in sorted(amac_sources_dir().glob("*.json")):
        record = load_json(path, {})
        if not isinstance(record, dict) or not record.get("source_record_id"):
            continue
        metadata = record.get("metadata") or {}
        items.append(
            {
                "source_record_id": record["source_record_id"],
                "name": metadata.get("name"),
                "document_type": metadata.get("document_type"),
                "status": metadata.get("status"),
                "file": relative_to_output(path),
                "assets": len(record.get("assets") or []),
            }
        )
    return items


def crawl_amac(
    *,
    policy_limit: int | None = None,
    site_limit: int | None = None,
    xwfb_pages: int = DEFAULT_XWFB_PAGES,
    self_regulatory_measure_pages: int = DEFAULT_SELF_REGULATORY_MEASURE_PAGES,
    self_regulatory_management_pages: int = DEFAULT_SELF_REGULATORY_MANAGEMENT_PAGES,
    industry_research_pages: int = DEFAULT_INDUSTRY_RESEARCH_PAGES,
    keywords: list[str] | None = None,
    include_self_regulatory_measures: bool = False,
    only_self_regulatory_measures: bool = False,
    include_self_regulatory_management: bool = False,
    only_self_regulatory_management: bool = False,
    include_industry_research: bool = False,
    only_industry_research: bool = False,
    download_assets: bool = True,
    asset_suffixes: set[str] | None = None,
    force: bool = False,
    delay_min: float = 0.25,
    delay_max: float = 0.7,
    verify_tls: bool = AMAC_VERIFY_TLS,
) -> dict[str, Any]:
    client = AmacClient(delay_min=delay_min, delay_max=delay_max, verify_tls=verify_tls)
    candidates = []
    only_specialized = (
        only_self_regulatory_measures
        or only_self_regulatory_management
        or only_industry_research
    )
    if not only_specialized:
        candidates.extend(discover_policy_candidates(client, limit=policy_limit))
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
    if include_self_regulatory_management or only_self_regulatory_management:
        candidates.extend(
            discover_self_regulatory_management_candidates(
                client,
                max_pages=self_regulatory_management_pages,
            )
        )
    if include_self_regulatory_measures or only_self_regulatory_measures:
        candidates.extend(
            discover_self_regulatory_measure_candidates(
                client,
                max_pages=self_regulatory_measure_pages,
            )
        )
    if include_industry_research or only_industry_research:
        candidates.extend(
            discover_industry_research_candidates(
                client,
                max_pages=industry_research_pages,
            )
        )
    candidates = deduplicate_candidates(candidates)
    amac_sources_dir().mkdir(parents=True, exist_ok=True)

    records_for_stats: list[dict[str, Any]] = []
    written = 0
    skipped = 0
    pdf_assets_renamed = 0
    failures = []
    for index, candidate in enumerate(candidates, start=1):
        record_id = source_record_id(str(candidate["url"]))
        path = amac_sources_dir() / f"{record_id}.json"
        existing_record = load_json(path, {}) if path.exists() else {}
        candidate_suffix = Path(
            urlsplit(str(candidate.get("url") or "")).path
        ).suffix.lower()
        pending_assets = any(
            _asset_needs_refresh(asset, asset_suffixes)
            for asset in (existing_record.get("assets") or [])
        )
        direct_asset_missing = (
            candidate_suffix in ASSET_SUFFIXES
            and _asset_matches_suffixes(str(candidate.get("url") or ""), asset_suffixes)
            and not (existing_record.get("assets") or [])
        )
        should_refresh = force or (
            download_assets and (pending_assets or direct_asset_missing)
        )
        if path.exists() and not should_refresh:
            record = existing_record
            renamed = _migrate_record_asset_filenames(record)
            if renamed:
                save_json(path, record)
                pdf_assets_renamed += renamed
            skipped += 1
        else:
            try:
                record = crawl_candidate(
                    client,
                    candidate,
                    download_assets=download_assets,
                    asset_suffixes=asset_suffixes,
                )
                save_json(path, record)
                written += 1
            except Exception as exc:
                failures.append(
                    {
                        "failure_type": "record",
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
        records_for_stats.append(record)
        if index % 50 == 0 or index == len(candidates):
            log_event(
                "amac_progress",
                message=f"  AMAC {index}/{len(candidates)}",
                index=index,
                total=len(candidates),
            )

    asset_stats = _asset_stats(records_for_stats)
    manifest_failures = [*failures, *_record_asset_failures(records_for_stats)]
    items = _all_manifest_items()
    manifest = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "candidate_count": len(candidates),
        "count": len(items),
        "written": written,
        "skipped": skipped,
        "failed": len(manifest_failures),
        "keywords": keywords or DEFAULT_SITE_KEYWORDS,
        "xwfb_pages": xwfb_pages,
        "include_self_regulatory_measures": include_self_regulatory_measures,
        "only_self_regulatory_measures": only_self_regulatory_measures,
        "self_regulatory_measure_pages": self_regulatory_measure_pages,
        "include_self_regulatory_management": include_self_regulatory_management,
        "only_self_regulatory_management": only_self_regulatory_management,
        "self_regulatory_management_pages": self_regulatory_management_pages,
        "include_industry_research": include_industry_research,
        "only_industry_research": only_industry_research,
        "industry_research_pages": industry_research_pages,
        "asset_suffixes": sorted(asset_suffixes) if asset_suffixes else None,
        **asset_stats,
        "pdf_assets_renamed": pdf_assets_renamed,
        "tls_policy": {
            "verify": client.verify_tls,
            "fg_ca_bundle": str(getattr(client, "fg_ca_bundle", "")) or None
            if client.verify_tls
            else None,
            "source": "default" if verify_tls == AMAC_VERIFY_TLS else "cli",
        },
        "items": items,
        "failures": manifest_failures,
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
