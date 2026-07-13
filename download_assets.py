#!/usr/bin/env python3
"""Download law assets discovered by normalize_laws.py."""

from __future__ import annotations

import argparse
import hashlib
import mimetypes
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from client import HumanLikeClient
from config import BASE_URL
from normalize_laws import normalized_laws_dir, normalized_manifest_path
from runtime import log_event
from storage import (
    listed_output_files,
    load_json,
    output_path,
    relative_to_output,
    run_with_output_lock,
    save_bytes,
    save_json,
    utc_now_iso,
)
from storage import raw_dir, reports_dir, work_dir


def assets_root() -> Path:
    return raw_dir() / "assets"


def law_assets_root() -> Path:
    return assets_root() / "embedded"


def assets_manifest_path() -> Path:
    return reports_dir() / "assets_manifest.json"


def asset_failures_path() -> Path:
    return reports_dir() / "assets_failures.json"


def _normalized_law_files(limit: int | None = None) -> list[Path]:
    return listed_output_files(
        normalized_manifest_path(),
        field="file",
        fallback_dir=normalized_laws_dir(),
        pattern="reg_*.json",
        limit=limit,
    )


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


def _write_asset_file(law_id: str, asset: dict[str, Any], data: bytes, extension: str) -> str:
    law_dir = law_assets_root() / law_id
    law_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{asset['asset_id']}{extension}"
    path = law_dir / filename
    save_bytes(path, data)
    return relative_to_output(path)


def _existing_file(asset: dict[str, Any]) -> Path | None:
    local_file = asset.get("local_file")
    if not local_file:
        return None
    path = output_path(local_file)
    if not path.exists() or not path.is_file() or path.stat().st_size <= 0:
        return None
    expected_size = asset.get("size_bytes")
    if expected_size is not None and path.stat().st_size != int(expected_size):
        return None
    expected_sha256 = asset.get("sha256")
    if expected_sha256:
        actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_sha256 != expected_sha256:
            return None
    return path


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
    law_files = _normalized_law_files(limit_laws)

    client = HumanLikeClient()
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
                payload = client.get_binary_payload(
                    source_url,
                    headers={"Accept": "*/*", "Referer": BASE_URL},
                )
                data = payload.data
                content_type = payload.content_type
                digest = payload.sha256
                extension = _extension_from_data(data, source_url, content_type)
                local_file = _write_asset_file(law_id, asset, data, extension)
                asset.update(
                    {
                        "local_file": local_file,
                        "content_type": content_type,
                        "sha256": digest,
                        "size_bytes": payload.size_bytes,
                        "download_status": "ok",
                        "downloaded_at": utc_now_iso(),
                    }
                )
                asset.pop("download_error", None)
                manifest_items.append(_asset_record(law_id, asset))
                downloaded += 1
                changed = True
                log_event(
                    "asset_downloaded",
                    message=f"  downloaded {asset['asset_id']} -> {local_file}",
                    asset_id=asset.get("asset_id"),
                    local_file=local_file,
                )
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
                log_event(
                    "asset_download_failed",
                    level="ERROR",
                    message=f"  !! failed {asset.get('asset_id')} {source_url}: {exc}",
                    asset_id=asset.get("asset_id"),
                    source_url=source_url,
                    error_message=str(exc),
                )

        if changed:
            save_json(path, doc)
            law_manifest = {
                "updated_at": utc_now_iso(),
                "law_id": law_id,
                "law_name": metadata.get("name"),
                "normalized_file": relative_to_output(path),
                "assets": [_asset_record(law_id, item) for item in assets],
            }
            save_json(law_assets_root() / law_id / "asset_manifest.json", law_manifest)

        if law_index % 100 == 0 or law_index == len(law_files):
            log_event(
                "asset_scan_progress",
                message=f"  scanned laws {law_index}/{len(law_files)}",
                index=law_index,
                total=len(law_files),
            )
        if limit_assets is not None and seen_assets >= limit_assets:
            break

    manifest = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "scope": "partial" if partial_scope else "full",
        "normalized_dir": relative_to_output(normalized_laws_dir()),
        "assets_root": relative_to_output(assets_root()),
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
        manifest["output"] = relative_to_output(run_dir)
    else:
        save_json(assets_manifest_path(), manifest)
        save_json(asset_failures_path(), failure_doc)
    return manifest


def rebuild_asset_manifests() -> dict[str, Any]:
    """Re-index current embedded-asset state without making network requests."""
    manifest_items: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    seen_assets = 0
    ok_assets = 0
    failed_assets = 0
    pending_assets = 0

    for path in _normalized_law_files():
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
            "normalized_file": relative_to_output(path),
            "assets": [
                _asset_record(law_id, asset) for asset in embedded_assets
            ],
        }
        save_json(law_assets_root() / law_id / "asset_manifest.json", law_manifest)

    manifest = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "scope": "full",
        "normalized_dir": relative_to_output(normalized_laws_dir()),
        "assets_root": relative_to_output(assets_root()),
        "seen_assets": seen_assets,
        "downloaded": 0,
        "skipped": ok_assets,
        "failed": failed_assets,
        "pending": pending_assets,
        "items": manifest_items,
    }
    save_json(assets_manifest_path(), manifest)
    save_json(
        asset_failures_path(),
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
        log_event(
            "cli_error",
            level="ERROR",
            message="normalized/laws 不存在，请先运行 python normalize_laws.py",
        )
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
        log_event("cli_interrupted", level="ERROR", message="已中断")
        return 130

    log_event(
        "cli_result",
        message=(
            "完成: "
            f"seen={manifest['seen_assets']} downloaded={manifest['downloaded']} "
            f"skipped={manifest['skipped']} failed={manifest['failed']} -> "
            f"{manifest.get('output') or assets_manifest_path()}"
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "download-assets"))
