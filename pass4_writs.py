"""Pass 4：执法文书（列表索引 + 详情页正文）。"""

from __future__ import annotations

import json
from typing import Any

from api import fetch_writ_list_page
from client import HumanLikeClient
from config import PAGE_SIZE
from pipeline import STEP_COMPLETE, STEP_INCOMPLETE
from runtime import log_event
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
) -> dict[str, Any]:
    checkpoint = load_checkpoint()
    pass_state = checkpoint.setdefault("pass4", {"completed_writ_ids": [], "failures": []})
    pass_state["status"] = "in_progress"
    pass_state["started_at"] = utc_now_iso()
    save_checkpoint(checkpoint)
    done: set[str] = set(pass_state.get("completed_writ_ids") or [])

    needed = _collect_needed_writ_ids(all_writs)
    writs_dir().mkdir(parents=True, exist_ok=True)

    target_count = 0
    skipped = 0
    run_failures: list[dict[str, Any]] = []

    if all_writs:
        log_event("pass4_started", message="\n=== Pass 4：全量执法文书（列表 + 详情页正文）===", mode="all")
    elif needed:
        target_count = len(needed)
        log_event(
            "pass4_started",
            message=f"\n=== Pass 4：案例引用文书 {len(needed)} 个（直拉详情页）===",
            mode="needed",
            needed=len(needed),
        )
    else:
        log_event("pass4_no_targets", message="\n=== Pass 4：无目标 writ_id（先跑 pass3 或 --all-writs）===")
        pass_state["status"] = STEP_COMPLETE
        pass_state["finished_at"] = utc_now_iso()
        save_checkpoint(checkpoint)
        return {
            "schema_version": 1,
            "status": STEP_COMPLETE,
            "targets": 0,
            "saved": 0,
            "skipped": 0,
            "failed": 0,
            "output_dir": str(writs_dir()),
        }

    saved = 0

    if not all_writs:
        for writ_id in sorted(needed):
            if _should_skip(writ_id, done, force):
                skipped += 1
                continue
            try:
                doc = fetch_and_save_writ(client, writ_id, force=force)
                saved += 1
                body_len = len(doc.get("body") or "")
                log_event(
                    "pass4_writ_saved",
                    message=f"  writ_{writ_id[:8]}… 正文 {body_len} 字",
                    writ_id=writ_id,
                    body_length=body_len,
                )
                done.add(writ_id)
                if writ_id not in pass_state["completed_writ_ids"]:
                    pass_state["completed_writ_ids"].append(writ_id)
                save_checkpoint(checkpoint)
            except Exception as exc:
                log_event(
                    "pass4_writ_failed",
                    level="ERROR",
                    message=f"  !! 失败 {writ_id}: {exc}",
                    writ_id=writ_id,
                    error_message=str(exc),
                )
                failure = {"id": writ_id, "error": str(exc), "at": utc_now_iso()}
                run_failures.append(failure)
                pass_state.setdefault("failures", []).append(
                    failure
                )
                save_checkpoint(checkpoint)
    else:
        first = fetch_writ_list_page(client, 1)
        page_util = first["pageUtil"]
        row_count = page_util["rowCount"]
        total_pages = (row_count + PAGE_SIZE - 1) // PAGE_SIZE
        if limit_pages is not None:
            total_pages = min(total_pages, limit_pages)
        log_event(
            "pass4_writ_list_discovered",
            message=f"  文书列表共 {row_count} 条，扫描 {total_pages} 页",
            row_count=row_count,
            total_pages=total_pages,
        )

        for page in range(1, total_pages + 1):
            resp = first if page == 1 else fetch_writ_list_page(client, page)
            if page > 1:
                log_event(
                    "pass4_page_started",
                    message=f"  列表第 {page}/{total_pages} 页",
                    page=page,
                    total_pages=total_pages,
                )

            for row in resp["pageUtil"].get("pageList") or []:
                writ_id = row.get("lawWritId")
                if not writ_id:
                    continue
                writ_id = str(writ_id)
                target_count += 1
                if _should_skip(writ_id, done, force):
                    skipped += 1
                    continue
                try:
                    doc = fetch_and_save_writ(client, writ_id, list_row=row, force=force)
                    saved += 1
                    body_len = len(doc.get("body") or "")
                    name = (doc.get("metadata") or {}).get("name") or row.get("name", "")
                    log_event(
                        "pass4_writ_saved",
                        message=f"  writ_{writ_id[:8]}… 正文 {body_len} 字 | {name[:36]}",
                        writ_id=writ_id,
                        body_length=body_len,
                        name=name,
                    )
                    done.add(writ_id)
                    if writ_id not in pass_state["completed_writ_ids"]:
                        pass_state["completed_writ_ids"].append(writ_id)
                    save_checkpoint(checkpoint)
                except Exception as exc:
                    log_event(
                        "pass4_writ_failed",
                        level="ERROR",
                        message=f"  !! 失败 {writ_id}: {exc}",
                        writ_id=writ_id,
                        error_message=str(exc),
                    )
                    failure = {"id": writ_id, "error": str(exc), "at": utc_now_iso()}
                    run_failures.append(failure)
                    pass_state.setdefault("failures", []).append(
                        failure
                    )
                    save_checkpoint(checkpoint)

    status = STEP_INCOMPLETE if run_failures else STEP_COMPLETE
    pass_state["status"] = status
    if run_failures:
        pass_state.pop("finished_at", None)
    else:
        pass_state["finished_at"] = utc_now_iso()
    save_checkpoint(checkpoint)
    log_event(
        "pass4_finished",
        message=f"  本轮保存/更新 {saved} 份文书 → {writs_dir()}",
        saved=saved,
        output_dir=str(writs_dir()),
    )
    return {
        "schema_version": 1,
        "status": status,
        "targets": target_count,
        "saved": saved,
        "skipped": skipped,
        "failed": len(run_failures),
        "output_dir": str(writs_dir()),
    }
