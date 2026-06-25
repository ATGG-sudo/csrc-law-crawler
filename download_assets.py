#!/usr/bin/env python3
"""Download law assets discovered by normalize_laws.py."""

from __future__ import annotations

import argparse
import hashlib
import mimetypes
import random
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from config import (
    BASE_URL,
    BATCH_PAUSE_MAX,
    BATCH_PAUSE_MIN,
    BATCH_SIZE,
    DELAY_MAX,
    DELAY_MIN,
    MAX_RETRIES,
    RETRY_BACKOFF_BASE,
    OUTPUT_DIR,
    USER_AGENT,
)
from normalize_laws import normalized_laws_dir
from storage import load_json, save_json, utc_now_iso
from storage import raw_dir, reports_dir, work_dir

ASSETS_ROOT = raw_dir() / "assets"
LAW_ASSETS_ROOT = ASSETS_ROOT / "embedded"
ASSETS_MANIFEST = reports_dir() / "assets_manifest.json"
ASSET_FAILURES = reports_dir() / "assets_failures.json"

CONTENT_TYPE_EXTENSIONS = {
    "application/msword": ".doc",
    "application/pdf": ".pdf",
    "application/rtf": ".rtf",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/zip": ".zip",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tif",
    "text/csv": ".csv",
    "text/plain": ".txt",
}


def _pause(request_count: int) -> None:
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
    if request_count > 0 and request_count % BATCH_SIZE == 0:
        time.sleep(random.uniform(BATCH_PAUSE_MIN, BATCH_PAUSE_MAX))


def _is_blocked(response: requests.Response) -> bool:
    text = response.text[:500] if response.content else ""
    blocked_markers = ("WAF", "请求已中断", "504 Gateway", "502 Bad Gateway")
    return response.status_code >= 500 or any(marker in text for marker in blocked_markers)


def _content_type(response: requests.Response) -> str:
    return (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()


def _extension_from_data(data: bytes, url: str, content_type: str) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data.startswith(b"%PDF"):
        return ".pdf"
    if data.startswith(b"PK\x03\x04"):
        lower_path = urlparse(url).path.lower()
        suffix = Path(lower_path).suffix
        if suffix in {".docx", ".xlsx", ".zip"}:
            return suffix
        if "word" in content_type:
            return ".docx"
        if "spreadsheet" in content_type or "excel" in content_type:
            return ".xlsx"
        return ".zip"
    if data.startswith(b"{\\rtf"):
        return ".rtf"

    if content_type in CONTENT_TYPE_EXTENSIONS:
        return CONTENT_TYPE_EXTENSIONS[content_type]
    guessed = mimetypes.guess_extension(content_type) if content_type else None
    if guessed:
        return guessed
    suffix = Path(urlparse(url).path).suffix
    if suffix and len(suffix) <= 10:
        return suffix
    return ".bin"


def _download(session: requests.Session, url: str, request_count: int) -> tuple[bytes, str, int]:
    last_error: Exception | None = None
    headers = {
        "Accept": "*/*",
        "Referer": BASE_URL,
    }
    for attempt in range(1, MAX_RETRIES + 1):
        _pause(request_count)
        try:
            response = session.get(url, timeout=60, headers=headers)
            request_count += 1
            if _is_blocked(response):
                wait = RETRY_BACKOFF_BASE * attempt + random.uniform(2, 6)
                print(f"  [拦截/超时，{wait:.1f}s 后重试 {attempt}/{MAX_RETRIES}] {url}")
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.content, _content_type(response), request_count
        except requests.RequestException as exc:
            last_error = exc
            wait = RETRY_BACKOFF_BASE * attempt + random.uniform(1, 4)
            print(f"  [错误 {exc!r}，{wait:.1f}s 后重试 {attempt}/{MAX_RETRIES}] {url}")
            time.sleep(wait)
    raise RuntimeError(f"请求失败: {url}") from last_error


def _write_asset_file(law_id: str, asset: dict[str, Any], data: bytes, extension: str) -> str:
    law_dir = LAW_ASSETS_ROOT / law_id
    law_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{asset['asset_id']}{extension}"
    path = law_dir / filename
    path.write_bytes(data)
    return str(path.relative_to(OUTPUT_DIR))


def _existing_file(asset: dict[str, Any]) -> Path | None:
    local_file = asset.get("local_file")
    if not local_file:
        return None
    path = OUTPUT_DIR / local_file
    if path.exists() and path.is_file() and path.stat().st_size > 0:
        return path
    return None


def _asset_record(law_id: str, asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "law_id": law_id,
        "asset_id": asset.get("asset_id"),
        "kind": asset.get("kind"),
        "source_url": asset.get("source_url"),
        "local_file": asset.get("local_file"),
        "content_type": asset.get("content_type"),
        "sha256": asset.get("sha256"),
        "size_bytes": asset.get("size_bytes"),
        "download_status": asset.get("download_status"),
        "refs": asset.get("refs") or [],
    }


def download_assets(
    *,
    limit_laws: int | None = None,
    limit_assets: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    partial_scope = limit_laws is not None or limit_assets is not None
    law_files = sorted(normalized_laws_dir().glob("reg_*.json"))
    if limit_laws is not None:
        law_files = law_files[:limit_laws]

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )

    request_count = 0
    downloaded = 0
    skipped = 0
    failed = 0
    seen_assets = 0
    manifest_items: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for law_index, path in enumerate(law_files, start=1):
        doc = load_json(path, {})
        metadata = doc.get("metadata") or {}
        law_id = str(metadata.get("id") or path.stem.removeprefix("reg_"))
        assets = doc.get("assets") or []
        changed = False

        for asset in assets:
            if limit_assets is not None and seen_assets >= limit_assets:
                break
            seen_assets += 1
            source_url = asset.get("source_url")
            if not source_url:
                asset["download_status"] = "failed"
                asset["download_error"] = "missing source_url"
                failures.append(_asset_record(law_id, asset))
                failed += 1
                changed = True
                continue

            existing = _existing_file(asset)
            if existing is not None and not force:
                asset["download_status"] = "ok"
                asset.setdefault("size_bytes", existing.stat().st_size)
                if not asset.get("sha256"):
                    asset["sha256"] = hashlib.sha256(existing.read_bytes()).hexdigest()
                manifest_items.append(_asset_record(law_id, asset))
                skipped += 1
                changed = True
                continue

            try:
                data, content_type, request_count = _download(session, source_url, request_count)
                if not data:
                    raise RuntimeError("empty response body")
                digest = hashlib.sha256(data).hexdigest()
                extension = _extension_from_data(data, source_url, content_type)
                local_file = _write_asset_file(law_id, asset, data, extension)
                asset.update(
                    {
                        "local_file": local_file,
                        "content_type": content_type,
                        "sha256": digest,
                        "size_bytes": len(data),
                        "download_status": "ok",
                        "downloaded_at": utc_now_iso(),
                    }
                )
                asset.pop("download_error", None)
                manifest_items.append(_asset_record(law_id, asset))
                downloaded += 1
                changed = True
                print(f"  downloaded {asset['asset_id']} -> {local_file}")
            except Exception as exc:
                asset["download_status"] = "failed"
                asset["download_error"] = str(exc)
                asset["local_file"] = None
                asset["content_type"] = None
                asset["sha256"] = None
                asset["size_bytes"] = None
                failures.append(_asset_record(law_id, asset) | {"error": str(exc)})
                failed += 1
                changed = True
                print(f"  !! failed {asset.get('asset_id')} {source_url}: {exc}")

        if changed:
            save_json(path, doc)
            law_manifest = {
                "updated_at": utc_now_iso(),
                "law_id": law_id,
                "law_name": metadata.get("name"),
                "normalized_file": str(path.relative_to(OUTPUT_DIR)),
                "assets": [_asset_record(law_id, item) for item in assets],
            }
            save_json(LAW_ASSETS_ROOT / law_id / "asset_manifest.json", law_manifest)

        if law_index % 100 == 0 or law_index == len(law_files):
            print(f"  scanned laws {law_index}/{len(law_files)}")
        if limit_assets is not None and seen_assets >= limit_assets:
            break

    manifest = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "scope": "partial" if partial_scope else "full",
        "normalized_dir": str(normalized_laws_dir().relative_to(OUTPUT_DIR)),
        "assets_root": str(ASSETS_ROOT.relative_to(OUTPUT_DIR)),
        "seen_assets": seen_assets,
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "items": manifest_items,
    }
    failure_doc = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "scope": manifest["scope"],
        "failed": failed,
        "items": failures,
    }
    if partial_scope:
        stamp = utc_now_iso().replace(":", "").replace("+", "_")
        run_dir = work_dir() / "runs" / f"assets_{stamp}"
        save_json(run_dir / "manifest.json", manifest)
        save_json(run_dir / "failures.json", failure_doc)
        manifest["output"] = str(run_dir.relative_to(OUTPUT_DIR))
    else:
        save_json(ASSETS_MANIFEST, manifest)
        save_json(ASSET_FAILURES, failure_doc)
    return manifest


def rebuild_asset_manifests() -> dict[str, Any]:
    """Re-index current embedded-asset state without making network requests."""
    manifest_items: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    seen_assets = 0
    ok_assets = 0
    failed_assets = 0
    pending_assets = 0

    for path in sorted(normalized_laws_dir().glob("reg_*.json")):
        doc = load_json(path, {})
        metadata = doc.get("metadata") or {}
        law_id = str(metadata.get("id") or path.stem.removeprefix("reg_"))
        embedded_assets = [
            asset
            for asset in (doc.get("assets") or [])
            if not asset.get("source_attachment_id")
        ]
        if not embedded_assets:
            continue
        seen_assets += len(embedded_assets)
        for asset in embedded_assets:
            record = _asset_record(law_id, asset)
            status = asset.get("download_status")
            if status == "ok":
                ok_assets += 1
                manifest_items.append(record)
            elif status == "failed":
                failed_assets += 1
                failures.append(
                    record | {"error": asset.get("download_error")}
                )
            else:
                pending_assets += 1

        law_manifest = {
            "updated_at": utc_now_iso(),
            "law_id": law_id,
            "law_name": metadata.get("name"),
            "normalized_file": str(path.relative_to(OUTPUT_DIR)),
            "assets": [
                _asset_record(law_id, asset) for asset in embedded_assets
            ],
        }
        save_json(LAW_ASSETS_ROOT / law_id / "asset_manifest.json", law_manifest)

    manifest = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "scope": "full",
        "normalized_dir": str(normalized_laws_dir().relative_to(OUTPUT_DIR)),
        "assets_root": str(ASSETS_ROOT.relative_to(OUTPUT_DIR)),
        "seen_assets": seen_assets,
        "downloaded": 0,
        "skipped": ok_assets,
        "failed": failed_assets,
        "pending": pending_assets,
        "items": manifest_items,
    }
    save_json(ASSETS_MANIFEST, manifest)
    save_json(
        ASSET_FAILURES,
        {
            "updated_at": utc_now_iso(),
            "failed": failed_assets,
            "items": failures,
        },
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="下载法规清洗阶段发现的图片和附件")
    parser.add_argument("--limit-laws", type=int, default=None, help="仅扫描前 N 个 normalized 法规")
    parser.add_argument("--limit-assets", type=int, default=None, help="最多下载/检查 N 个资产")
    parser.add_argument("--force", action="store_true", help="即使已有本地文件也重新下载")
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="仅从 normalized/laws 重建资产清单，不发起下载",
    )
    args = parser.parse_args()

    if not normalized_laws_dir().exists():
        print("normalized/laws 不存在，请先运行 python normalize_laws.py", file=sys.stderr)
        return 2

    try:
        if args.manifest_only:
            manifest = rebuild_asset_manifests()
        else:
            manifest = download_assets(
                limit_laws=args.limit_laws,
                limit_assets=args.limit_assets,
                force=args.force,
            )
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130

    print(
        "完成: "
        f"seen={manifest['seen_assets']} downloaded={manifest['downloaded']} "
        f"skipped={manifest['skipped']} failed={manifest['failed']} -> "
        f"{manifest.get('output') or ASSETS_MANIFEST}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
