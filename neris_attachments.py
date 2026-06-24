#!/usr/bin/env python3
"""Discover and download attachments exposed by the NERIS attachment API."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import mimetypes
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from api import fetch_local_files, local_file_download_url
from client import HumanLikeClient
from config import OUTPUT_DIR
from storage import iter_reg_law_ids, load_json, reg_file_path, save_json, utc_now_iso

ATTACHMENTS_ROOT = OUTPUT_DIR / "assets" / "neris_attachments"


def normalize_attachment(raw: dict[str, Any]) -> dict[str, Any]:
    attachment_id = str(raw.get("lawAtchId") or "")
    return {
        "attachment_id": attachment_id,
        "name": raw.get("atchName"),
        "declared_type": raw.get("atchType"),
        "declared_size": raw.get("atchSize"),
        "source_url": local_file_download_url(attachment_id) if attachment_id else None,
        "local_file": None,
        "content_type": None,
        "size_bytes": None,
        "sha256": None,
        "download_status": "pending",
        "raw": raw,
    }


def discover_attachments(client: HumanLikeClient, law_id: str) -> list[dict[str, Any]]:
    response = fetch_local_files(client, law_id)
    return [
        normalize_attachment(item)
        for item in (response.get("pageUtilList") or [])
        if isinstance(item, dict) and item.get("lawAtchId")
    ]


def _extension(item: dict[str, Any], data: bytes, content_type: str) -> str:
    if data.startswith(b"%PDF"):
        return ".pdf"
    if data.startswith(b"PK\x03\x04"):
        declared = str(item.get("declared_type") or "").lower()
        return f".{declared}" if declared in {"docx", "xlsx", "zip"} else ".zip"
    if data.startswith(b"{\\rtf"):
        return ".rtf"
    declared = str(item.get("declared_type") or "").strip().lower()
    if declared and declared.isalnum() and len(declared) <= 8:
        return f".{declared}"
    guessed = mimetypes.guess_extension(content_type) if content_type else None
    if guessed:
        return guessed
    suffix = Path(urlparse(str(item.get("source_url") or "")).path).suffix
    return suffix if suffix else ".bin"


def download_attachment(
    client: HumanLikeClient,
    law_id: str,
    item: dict[str, Any],
) -> dict[str, Any]:
    source_url = str(item.get("source_url") or "")
    if not source_url:
        raise ValueError("attachment has no source_url")
    data, content_type = client.get_binary(
        source_url,
        headers={"Accept": "*/*", "Referer": source_url},
    )
    extension = _extension(item, data, content_type)
    law_dir = ATTACHMENTS_ROOT / law_id
    law_dir.mkdir(parents=True, exist_ok=True)
    path = law_dir / f"{item['attachment_id']}{extension}"
    path.write_bytes(data)
    item.update(
        {
            "local_file": str(path.relative_to(OUTPUT_DIR)),
            "content_type": content_type,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "download_status": "ok",
            "downloaded_at": utc_now_iso(),
        }
    )
    item.pop("download_error", None)
    return item


def update_law_attachments(
    client: HumanLikeClient,
    law_id: str,
    *,
    download: bool,
    force: bool = False,
) -> list[dict[str, Any]]:
    path = reg_file_path(law_id)
    doc = load_json(path, {})
    existing_by_id = {
        str(item.get("attachment_id")): item
        for item in (doc.get("source_attachments") or [])
        if item.get("attachment_id")
    }
    discovered = discover_attachments(client, law_id)
    merged: list[dict[str, Any]] = []
    for item in discovered:
        previous = existing_by_id.get(str(item["attachment_id"])) or {}
        item = {**item, **previous, "raw": item.get("raw")}
        local_file = item.get("local_file")
        local_ok = bool(local_file and (OUTPUT_DIR / str(local_file)).exists())
        if download and (force or not local_ok):
            try:
                download_attachment(client, law_id, item)
            except Exception as exc:
                item["download_status"] = "failed"
                item["download_error"] = str(exc)
        elif local_ok:
            item["download_status"] = "ok"
        merged.append(item)
    doc["source_attachments"] = merged
    doc.setdefault("source", {})["attachments_checked_at"] = utc_now_iso()
    save_json(path, doc)
    return merged


def run(
    *,
    limit: int | None = None,
    download: bool = True,
    force: bool = False,
    delay_min: float | None = None,
    delay_max: float | None = None,
    workers: int = 1,
) -> dict[str, int]:
    law_ids = iter_reg_law_ids(limit=limit)
    counts = {
        "laws": 0,
        "attachments": 0,
        "downloaded": 0,
        "failed": 0,
        "law_failures": 0,
    }

    def process(
        law_id: str,
    ) -> tuple[str, list[dict[str, Any]], str | None]:
        client_kwargs = {"batch_size": 0}
        if delay_min is not None:
            client_kwargs["delay_min"] = delay_min
        if delay_max is not None:
            client_kwargs["delay_max"] = delay_max
        client = HumanLikeClient(**client_kwargs)
        try:
            items = update_law_attachments(
                client,
                law_id,
                download=download,
                force=force,
            )
            return law_id, items, None
        except Exception as exc:
            return law_id, [], str(exc)

    if workers <= 1:
        iterator = (process(law_id) for law_id in law_ids)
    else:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        futures = [executor.submit(process, law_id) for law_id in law_ids]
        iterator = (
            future.result()
            for future in concurrent.futures.as_completed(futures)
        )

    try:
        for index, (law_id, items, error) in enumerate(iterator, start=1):
            counts["laws"] += 1
            if error:
                counts["law_failures"] += 1
                print(f"  !! {law_id}: {error}")
            counts["attachments"] += len(items)
            counts["downloaded"] += sum(
                1 for item in items if item.get("download_status") == "ok"
            )
            counts["failed"] += sum(
                1 for item in items if item.get("download_status") == "failed"
            )
            if items or index % 100 == 0 or index == len(law_ids):
                print(
                    f"[{index}/{len(law_ids)}] {law_id}: "
                    f"attachments={len(items)}"
                )
    finally:
        if workers > 1:
            executor.shutdown(wait=True)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="发现并下载 NERIS 独立附件")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--delay-min", type=float, default=None)
    parser.add_argument("--delay-max", type=float, default=None)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    try:
        counts = run(
            limit=args.limit,
            download=not args.discover_only,
            force=args.force,
            delay_min=args.delay_min,
            delay_max=args.delay_max,
            workers=args.workers,
        )
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"失败: {exc}", file=sys.stderr)
        return 1
    print(counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
