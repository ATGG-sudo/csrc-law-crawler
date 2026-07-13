#!/usr/bin/env python3
"""Discover and download attachments exposed by the NERIS attachment API."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import mimetypes
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from api import fetch_local_files, local_file_download_url
from client import HumanLikeClient
from config import DELAY_MAX, DELAY_MIN
from runtime import log_event
from storage import (
    attachment_index_path,
    iter_reg_law_ids,
    load_json,
    output_path,
    raw_dir,
    reg_file_path,
    relative_to_output,
    run_with_output_lock,
    save_bytes,
    save_json,
    utc_now_iso,
)

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


def attachments_root() -> Path:
    return raw_dir() / "assets" / "neris_attachments"


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
    payload = client.get_binary_payload(
        source_url,
        headers={"Accept": "*/*", "Referer": source_url},
    )
    data = payload.data
    content_type = payload.content_type
    extension = _extension(item, data, content_type)
    law_dir = attachments_root() / law_id
    law_dir.mkdir(parents=True, exist_ok=True)
    path = law_dir / f"{item['attachment_id']}{extension}"
    save_bytes(path, data)
    item.update(
        {
            "local_file": relative_to_output(path),
            "content_type": content_type,
            "size_bytes": payload.size_bytes,
            "sha256": payload.sha256,
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
    index_path = attachment_index_path(law_id)
    index_doc = load_json(index_path, {})
    existing_by_id = {
        str(item.get("attachment_id")): item
        for item in (
            index_doc.get("attachments")
            or doc.get("source_attachments")
            or []
        )
        if item.get("attachment_id")
    }
    discovered = discover_attachments(client, law_id)
    merged: list[dict[str, Any]] = []
    for item in discovered:
        previous = existing_by_id.get(str(item["attachment_id"])) or {}
        item = {**item, **previous, "raw": item.get("raw")}
        local_file = item.get("local_file")
        local_path = output_path(str(local_file)) if local_file else None
        local_ok = False
        if local_path is not None and local_path.is_file():
            local_ok = True
            if item.get("size_bytes") is not None:
                local_ok = local_path.stat().st_size == int(item["size_bytes"])
            if local_ok and item.get("sha256"):
                local_ok = (
                    hashlib.sha256(local_path.read_bytes()).hexdigest()
                    == item["sha256"]
                )
        if download and (force or not local_ok):
            try:
                download_attachment(client, law_id, item)
            except Exception as exc:
                item["download_status"] = "failed"
                item["download_error"] = str(exc)
        elif local_ok:
            item["download_status"] = "ok"
        merged.append(item)
    save_json(
        index_path,
        {
            "schema_version": 1,
            "law_id": law_id,
            "checked_at": utc_now_iso(),
            "attachments": merged,
        },
    )
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
        client = HumanLikeClient(
            batch_size=0,
            delay_min=delay_min if delay_min is not None else DELAY_MIN,
            delay_max=delay_max if delay_max is not None else DELAY_MAX,
        )
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
                log_event(
                    "neris_attachment_law_failed",
                    level="ERROR",
                    message=f"  !! {law_id}: {error}",
                    law_id=law_id,
                    error_message=error,
                )
            counts["attachments"] += len(items)
            counts["downloaded"] += sum(
                1 for item in items if item.get("download_status") == "ok"
            )
            counts["failed"] += sum(
                1 for item in items if item.get("download_status") == "failed"
            )
            if items or index % 100 == 0 or index == len(law_ids):
                log_event(
                    "neris_attachment_progress",
                    message=(
                        f"[{index}/{len(law_ids)}] {law_id}: "
                        f"attachments={len(items)}"
                    ),
                    index=index,
                    total=len(law_ids),
                    law_id=law_id,
                    attachments=len(items),
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
        log_event("cli_error", level="ERROR", message=f"失败: {exc}", error_message=str(exc))
        return 1
    log_event("cli_result", message=json.dumps(counts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "neris-attachments"))
