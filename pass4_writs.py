"""Pass 4：执法文书（列表索引 + 详情页正文）。"""

from __future__ import annotations

import json

from api import fetch_writ_list_page
from client import HumanLikeClient
from config import PAGE_SIZE
from storage import (
    cases_path,
    load_checkpoint,
    load_json,
    save_checkpoint,
    utc_now_iso,
    writ_file_path,
    writs_dir,
)
from writ_crawl import fetch_and_save_writ, writ_has_body


def _collect_needed_writ_ids(all_writs: bool) -> set[str]:
    if all_writs:
        return set()
    cases_doc = load_json(cases_path(), {})
    return set(cases_doc.get("writ_ids") or [])


def _should_skip(writ_id: str, done: set[str], force: bool) -> bool:
    if force:
        return False
    if writ_id not in done:
        return False
    path = writ_file_path(writ_id)
    if not path.exists():
        return False
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return writ_has_body(doc)


def run_pass4(
    client: HumanLikeClient,
    *,
    all_writs: bool = False,
    limit_pages: int | None = None,
    force: bool = False,
) -> None:
    checkpoint = load_checkpoint()
    pass_state = checkpoint.setdefault("pass4", {"completed_writ_ids": [], "failures": []})
    done: set[str] = set(pass_state.get("completed_writ_ids") or [])

    needed = _collect_needed_writ_ids(all_writs)
    writs_dir().mkdir(parents=True, exist_ok=True)

    if all_writs:
        print("\n=== Pass 4：全量执法文书（列表 + 详情页正文）===")
    elif needed:
        print(f"\n=== Pass 4：案例引用文书 {len(needed)} 个（直拉详情页）===")
    else:
        print("\n=== Pass 4：无目标 writ_id（先跑 pass3 或 --all-writs）===")
        return

    saved = 0

    if not all_writs:
        for writ_id in sorted(needed):
            if _should_skip(writ_id, done, force):
                continue
            try:
                doc = fetch_and_save_writ(client, writ_id, force=force)
                saved += 1
                body_len = len(doc.get("body") or "")
                print(f"  writ_{writ_id[:8]}… 正文 {body_len} 字")
                done.add(writ_id)
                if writ_id not in pass_state["completed_writ_ids"]:
                    pass_state["completed_writ_ids"].append(writ_id)
                save_checkpoint(checkpoint)
            except Exception as exc:
                print(f"  !! 失败 {writ_id}: {exc}")
                pass_state.setdefault("failures", []).append(
                    {"id": writ_id, "error": str(exc), "at": utc_now_iso()}
                )
                save_checkpoint(checkpoint)
    else:
        first = fetch_writ_list_page(client, 1)
        page_util = first["pageUtil"]
        row_count = page_util["rowCount"]
        total_pages = (row_count + PAGE_SIZE - 1) // PAGE_SIZE
        if limit_pages is not None:
            total_pages = min(total_pages, limit_pages)
        print(f"  文书列表共 {row_count} 条，扫描 {total_pages} 页")

        for page in range(1, total_pages + 1):
            resp = first if page == 1 else fetch_writ_list_page(client, page)
            if page > 1:
                print(f"  列表第 {page}/{total_pages} 页")

            for row in resp["pageUtil"].get("pageList") or []:
                writ_id = row.get("lawWritId")
                if not writ_id:
                    continue
                writ_id = str(writ_id)
                if _should_skip(writ_id, done, force):
                    continue
                try:
                    doc = fetch_and_save_writ(client, writ_id, list_row=row, force=force)
                    saved += 1
                    body_len = len(doc.get("body") or "")
                    name = (doc.get("metadata") or {}).get("name") or row.get("name", "")
                    print(f"  writ_{writ_id[:8]}… 正文 {body_len} 字 | {name[:36]}")
                    done.add(writ_id)
                    if writ_id not in pass_state["completed_writ_ids"]:
                        pass_state["completed_writ_ids"].append(writ_id)
                    save_checkpoint(checkpoint)
                except Exception as exc:
                    print(f"  !! 失败 {writ_id}: {exc}")
                    pass_state.setdefault("failures", []).append(
                        {"id": writ_id, "error": str(exc), "at": utc_now_iso()}
                    )
                    save_checkpoint(checkpoint)

    pass_state["finished_at"] = utc_now_iso()
    save_checkpoint(checkpoint)
    print(f"  本轮保存/更新 {saved} 份文书 → {writs_dir()}")
