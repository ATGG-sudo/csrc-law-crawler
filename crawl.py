#!/usr/bin/env python3
"""证监会法规库全量抓取（温和限速，可断点续传）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from client import HumanLikeClient
from config import (
    LAW_TYPE_REGULATION,
    LAW_TYPE_WRIT,
    OUTPUT_DIR,
    PAGE_SIZE,
)
from parser import build_law_document
from storage import (
    checkpoint_path,
    laws_dir,
    load_json,
    manifest_path,
    save_json,
    utc_now_iso,
    writs_dir,
)
from writ_crawl import fetch_and_save_writ


def fetch_list_page(
    client: HumanLikeClient, page_no: int, law_type: int
) -> dict[str, Any]:
    return client.post_json(
        "rdqsHeader/informationController",
        {"pageNo": page_no, "lawType": law_type},
    )


def fetch_law_detail(client: HumanLikeClient, law_id: str) -> dict[str, Any]:
    return client.post_json(
        "rdqsHeader/lawlist",
        {"secFutrsLawId": law_id, "navbarId": "1"},
    )


def law_file_path(law_id: str, law_type: int) -> Path:
    prefix = "reg" if law_type == LAW_TYPE_REGULATION else "writ"
    return (laws_dir() if law_type == LAW_TYPE_REGULATION else writs_dir()) / (
        f"{prefix}_{law_id}.json"
    )


def crawl_type(
    client: HumanLikeClient,
    law_type: int,
    checkpoint: dict[str, Any],
    limit: int | None = None,
) -> None:
    type_key = "regulations" if law_type == LAW_TYPE_REGULATION else "writs"
    done_ids: set[str] = set(checkpoint.get("completed_ids", {}).get(type_key, []))

    label = "法规" if law_type == LAW_TYPE_REGULATION else "执法文书"
    print(f"\n=== 开始抓取 {label} ===")

    first = fetch_list_page(client, 1, law_type)
    page_util = first["pageUtil"]
    row_count = page_util["rowCount"]
    total_pages = (row_count + PAGE_SIZE - 1) // PAGE_SIZE
    print(f"  共 {row_count} 条，{total_pages} 页（已跳过 {len(done_ids)} 条）")

    manifest = load_json(manifest_path(), {"updated_at": None, "items": []})
    manifest_index = {item["id"]: item for item in manifest.get("items", [])}

    fetched = 0
    skipped = 0

    for page in range(1, total_pages + 1):
        if page == 1:
            resp = first
        else:
            print(f"  列表第 {page}/{total_pages} 页")
            resp = fetch_list_page(client, page, law_type)

        for summary in resp["pageUtil"].get("pageList") or []:
            if limit is not None and fetched >= limit:
                print(f"\n已达 --limit {limit}，停止。")
                return

            law_id = summary.get("secFutrsLawId") or summary.get("lawWritId")
            if not law_id:
                continue
            if law_id in done_ids and law_file_path(law_id, law_type).exists():
                skipped += 1
                continue

            fetched += 1
            idx = fetched
            total_hint = limit if limit is not None else "?"
            name = summary.get("secFutrsLawName") or summary.get("name") or law_id
            print(f"[{idx}/{total_hint}] {name}")

            out_path = law_file_path(law_id, law_type)
            try:
                if law_type == LAW_TYPE_REGULATION:
                    detail_resp = fetch_law_detail(client, law_id)
                    document = build_law_document(detail_resp.get("lawlist") or {})
                    metadata = document.setdefault("metadata", {})
                    if not metadata.get("pub_org"):
                        metadata["pub_org"] = summary.get("lawPubOrgName")
                    document["source"] = {
                        "list_summary": {
                            "fileno": summary.get("fileno"),
                            "pub_org": summary.get("lawPubOrgName"),
                            "pub_date_ms": summary.get("pubDate"),
                        },
                        "crawled_at": utc_now_iso(),
                        "detail_url": (
                            f"https://neris.csrc.gov.cn/falvfagui/"
                            f"rdqsHeader/mainbody?navbarId=1&secFutrsLawId={law_id}"
                        ),
                    }
                    save_json(out_path, document)
                else:
                    fetch_and_save_writ(client, law_id, list_row=summary)

                manifest_index[law_id] = {
                    "id": law_id,
                    "type": type_key,
                    "name": name,
                    "fileno": summary.get("fileno"),
                    "file": str(out_path.relative_to(OUTPUT_DIR)),
                    "crawled_at": utc_now_iso(),
                }

                done_ids.add(law_id)
                checkpoint.setdefault("completed_ids", {}).setdefault(type_key, [])
                if law_id not in checkpoint["completed_ids"][type_key]:
                    checkpoint["completed_ids"][type_key].append(law_id)
                checkpoint["updated_at"] = utc_now_iso()
                save_json(checkpoint_path(), checkpoint)

                manifest["items"] = sorted(
                    manifest_index.values(), key=lambda x: x.get("name") or ""
                )
                manifest["updated_at"] = utc_now_iso()
                save_json(manifest_path(), manifest)

            except Exception as exc:
                print(f"  !! 失败: {exc}")
                checkpoint.setdefault("failures", []).append(
                    {
                        "id": law_id,
                        "type": type_key,
                        "error": str(exc),
                        "at": utc_now_iso(),
                    }
                )
                save_json(checkpoint_path(), checkpoint)

    print(f"  本轮新抓取 {fetched} 条，跳过 {skipped} 条")


def main() -> int:
    parser = argparse.ArgumentParser(description="证监会法规库全量抓取")
    parser.add_argument(
        "--types",
        default="regulation",
        choices=["regulation", "writ", "all"],
        help="抓取范围：法规 / 执法文书 / 全部",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="仅抓取前 N 条（调试用）",
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    laws_dir().mkdir(parents=True, exist_ok=True)
    writs_dir().mkdir(parents=True, exist_ok=True)

    checkpoint = load_json(
        checkpoint_path(),
        {"started_at": utc_now_iso(), "completed_ids": {"regulations": [], "writs": []}},
    )
    if "started_at" not in checkpoint:
        checkpoint["started_at"] = utc_now_iso()
    save_json(checkpoint_path(), checkpoint)

    client = HumanLikeClient()
    print(f"输出目录: {OUTPUT_DIR}")
    print(f"开始时间: {checkpoint['started_at']}")

    if args.types in ("regulation", "all"):
        crawl_type(client, LAW_TYPE_REGULATION, checkpoint, args.limit)
    if args.types in ("writ", "all"):
        crawl_type(client, LAW_TYPE_WRIT, checkpoint, args.limit)

    checkpoint["finished_at"] = utc_now_iso()
    save_json(checkpoint_path(), checkpoint)
    print(f"\n完成。清单: {manifest_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
