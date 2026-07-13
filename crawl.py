#!/usr/bin/env python3
"""证监会法规库全量抓取（温和限速，可断点续传）。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from client import HumanLikeClient
from config import (
    LAW_TYPE_REGULATION,
    LAW_TYPE_WRIT,
    PAGE_SIZE,
)
from parser import build_law_document
from neris_attachments import update_law_attachments
from pipeline import (
    STEP_COMPLETE,
    STEP_INCOMPLETE,
    PipelineHalted,
    PipelineRunner,
    PipelineStep,
    StepResult,
)
from runtime import log_event
from storage import (
    checkpoint_path,
    laws_dir,
    load_json,
    manifest_path,
    output_dir,
    relative_to_output,
    reports_dir,
    run_with_output_lock,
    save_json,
    utc_now_iso,
    writs_dir,
)
from writ_crawl import fetch_and_save_writ, writ_has_body

StepResultRun = Callable[[], StepResult]


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


def _write_step_results(results: list[StepResult]) -> None:
    save_json(
        reports_dir() / "crawl_step_results.json",
        {
            "schema_version": 1,
            "updated_at": utc_now_iso(),
            "status": (
                STEP_COMPLETE
                if all(item.status == STEP_COMPLETE for item in results)
                else STEP_INCOMPLETE
            ),
            "items": [item.as_dict() for item in results],
        },
    )


def _write_crawl_failures(stage: str, failures: list[dict[str, Any]]) -> str | None:
    path = reports_dir() / f"{stage}_failures.json"
    if not failures:
        path.unlink(missing_ok=True)
        return None
    save_json(
        path,
        {
            "schema_version": 1,
            "updated_at": utc_now_iso(),
            "status": STEP_INCOMPLETE,
            "stage": stage,
            "failures": failures,
        },
    )
    return relative_to_output(path)


def _existing_document(path: Path, law_id: str, law_type: int) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        document = load_json(path, None)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    if law_type == LAW_TYPE_WRIT:
        return document if writ_has_body(document) else None
    metadata = document.get("metadata")
    if not isinstance(metadata, dict) or str(metadata.get("id") or "") != law_id:
        return None
    return document if isinstance(document.get("full_text"), str) else None


def _manifest_item(
    law_id: str,
    type_key: str,
    name: str,
    summary: dict[str, Any],
    out_path: Path,
    document: dict[str, Any],
) -> dict[str, Any]:
    source = document.get("source") or {}
    return {
        "id": law_id,
        "type": type_key,
        "name": name,
        "fileno": summary.get("fileno"),
        "file": relative_to_output(out_path),
        "crawled_at": source.get("crawled_at") or utc_now_iso(),
    }


def _save_manifest(manifest: dict[str, Any], manifest_index: dict[str, Any]) -> None:
    manifest["items"] = sorted(
        manifest_index.values(), key=lambda item: item.get("name") or ""
    )
    manifest["updated_at"] = utc_now_iso()
    save_json(manifest_path(), manifest)


def _clear_checkpoint_failure(
    checkpoint: dict[str, Any], law_id: str, type_key: str
) -> None:
    checkpoint["failures"] = [
        failure
        for failure in checkpoint.get("failures", [])
        if failure.get("id") != law_id or failure.get("type") != type_key
    ]


def crawl_type(
    client: HumanLikeClient,
    law_type: int,
    checkpoint: dict[str, Any],
    limit: int | None = None,
    fetch_attachments: bool = True,
) -> dict[str, Any]:
    type_key = "regulations" if law_type == LAW_TYPE_REGULATION else "writs"
    done_ids: set[str] = set(checkpoint.get("completed_ids", {}).get(type_key, []))

    label = "法规" if law_type == LAW_TYPE_REGULATION else "执法文书"
    stage = f"crawl_{type_key}"
    checkpoint.setdefault("crawl_status", {})[type_key] = {
        "status": "in_progress",
        "started_at": utc_now_iso(),
    }
    save_json(checkpoint_path(), checkpoint)
    log_event("crawl_type_started", message=f"\n=== 开始抓取 {label} ===", type=type_key)

    first = fetch_list_page(client, 1, law_type)
    page_util = first["pageUtil"]
    row_count = page_util["rowCount"]
    total_pages = (row_count + PAGE_SIZE - 1) // PAGE_SIZE
    log_event(
        "crawl_type_discovered",
        message=f"  共 {row_count} 条，{total_pages} 页（已跳过 {len(done_ids)} 条）",
        row_count=row_count,
        total_pages=total_pages,
        skipped_existing=len(done_ids),
    )

    manifest = load_json(manifest_path(), {"updated_at": None, "items": []})
    manifest_index = {
        str(item["id"]): item
        for item in manifest.get("items", [])
        if isinstance(item, dict) and item.get("id")
    }

    seen = 0
    written = 0
    skipped = 0
    run_failures: list[dict[str, Any]] = []

    def finish_counts() -> dict[str, Any]:
        status = STEP_INCOMPLETE if run_failures else STEP_COMPLETE
        crawl_status = checkpoint.setdefault("crawl_status", {}).setdefault(type_key, {})
        crawl_status["status"] = status
        crawl_status["seen"] = seen
        crawl_status["written"] = written
        crawl_status["skipped"] = skipped
        crawl_status["failed"] = len(run_failures)
        if run_failures:
            crawl_status.pop("finished_at", None)
        else:
            crawl_status["finished_at"] = utc_now_iso()
        checkpoint["updated_at"] = utc_now_iso()
        save_json(checkpoint_path(), checkpoint)
        failure_file = _write_crawl_failures(stage, run_failures)
        return {
            "schema_version": 1,
            "status": status,
            "seen": seen,
            "written": written,
            "skipped": skipped,
            "failed": len(run_failures),
            "source_total": row_count,
            "source_pages": total_pages,
            "output": relative_to_output(manifest_path()),
            "failure_file": failure_file,
        }

    for page in range(1, total_pages + 1):
        if page == 1:
            resp = first
        else:
            log_event(
                "crawl_page_started",
                message=f"  列表第 {page}/{total_pages} 页",
                page=page,
                total_pages=total_pages,
            )
            resp = fetch_list_page(client, page, law_type)

        for summary in resp["pageUtil"].get("pageList") or []:
            if limit is not None and seen >= limit:
                log_event(
                    "crawl_limit_reached",
                    message=f"\n已达 --limit {limit}，停止。",
                    limit=limit,
                )
                return finish_counts()

            law_id = summary.get("secFutrsLawId") or summary.get("lawWritId")
            if not law_id:
                continue
            law_id = str(law_id)
            out_path = law_file_path(law_id, law_type)
            name = summary.get("secFutrsLawName") or summary.get("name") or law_id
            existing_document = _existing_document(out_path, law_id, law_type)
            if law_id in done_ids and existing_document is not None:
                if law_id not in manifest_index:
                    manifest_index[law_id] = _manifest_item(
                        law_id,
                        type_key,
                        str(name),
                        summary,
                        out_path,
                        existing_document,
                    )
                    _save_manifest(manifest, manifest_index)
                skipped += 1
                continue

            seen += 1
            idx = seen
            total_hint = limit if limit is not None else "?"
            log_event(
                "crawl_record_started",
                message=f"[{idx}/{total_hint}] {name}",
                index=idx,
                total_hint=total_hint,
                record_name=name,
            )

            try:
                if law_type == LAW_TYPE_REGULATION:
                    document = existing_document
                    if document is None:
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
                    if fetch_attachments:
                        update_law_attachments(
                            client,
                            law_id,
                            download=False,
                        )
                else:
                    document = fetch_and_save_writ(client, law_id, list_row=summary)

                manifest_index[law_id] = _manifest_item(
                    law_id,
                    type_key,
                    str(name),
                    summary,
                    out_path,
                    document,
                )
                _save_manifest(manifest, manifest_index)

                done_ids.add(law_id)
                checkpoint.setdefault("completed_ids", {}).setdefault(type_key, [])
                if law_id not in checkpoint["completed_ids"][type_key]:
                    checkpoint["completed_ids"][type_key].append(law_id)
                _clear_checkpoint_failure(checkpoint, law_id, type_key)
                checkpoint["updated_at"] = utc_now_iso()
                save_json(checkpoint_path(), checkpoint)
                written += 1

            except Exception as exc:
                log_event(
                    "crawl_record_failed",
                    level="ERROR",
                    message=f"  !! 失败: {exc}",
                    record_id=law_id,
                    record_type=type_key,
                    error_message=str(exc),
                )
                failure = {
                    "id": law_id,
                    "type": type_key,
                    "error": str(exc),
                    "at": utc_now_iso(),
                }
                run_failures.append(failure)
                _clear_checkpoint_failure(checkpoint, law_id, type_key)
                checkpoint.setdefault("failures", []).append(failure)
                save_json(checkpoint_path(), checkpoint)

    log_event(
        "crawl_type_finished",
        message=f"  本轮写入 {written} 条，失败 {len(run_failures)} 条，跳过 {skipped} 条",
        seen=seen,
        written=written,
        skipped=skipped,
        failed=len(run_failures),
    )
    return finish_counts()


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
    parser.add_argument(
        "--skip-attachments",
        action="store_true",
        help="法规抓取时不查询 NERIS 独立附件列表",
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="允许某类抓取 incomplete/failed 后继续执行后续类型，并记录非 complete 状态",
    )
    args = parser.parse_args()

    output_dir().mkdir(parents=True, exist_ok=True)
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
    log_event("cli_message", message=f"输出目录: {output_dir()}")
    log_event("cli_message", message=f"开始时间: {checkpoint['started_at']}")

    steps: list[PipelineStep] = []

    def add_step(name: str, run: StepResultRun) -> None:
        steps.append(PipelineStep(name=name, run=run))

    def crawl_step(
        stage_name: str,
        law_type: int,
        *,
        fetch_attachments: bool = True,
    ) -> StepResult:
        counts = crawl_type(
            client,
            law_type,
            checkpoint,
            args.limit,
            fetch_attachments=fetch_attachments,
        )
        output_file = counts.get("output")
        failure_file = counts.get("failure_file")
        return StepResult.from_counts(
            stage_name,
            counts,
            seen_key="seen",
            written_key="written",
            output_files=[str(output_file)] if output_file else [],
            failure_file=str(failure_file) if failure_file else None,
        )

    if args.types in ("regulation", "all"):
        add_step(
            "crawl.regulations",
            lambda: crawl_step(
                "crawl.regulations",
                LAW_TYPE_REGULATION,
                fetch_attachments=not args.skip_attachments,
            ),
        )
    if args.types in ("writ", "all"):
        add_step(
            "crawl.writs",
            lambda: crawl_step("crawl.writs", LAW_TYPE_WRIT),
        )

    try:
        run = PipelineRunner(
            allow_incomplete=args.allow_incomplete,
            on_update=_write_step_results,
        ).run(steps)
        log_event("pipeline_result", status=run.status, stages=len(run.items))
    except KeyboardInterrupt:
        return 130
    except PipelineHalted as exc:
        log_event(
            "cli_error",
            level="ERROR",
            message=f"抓取失败: {exc}",
            error_message=str(exc),
            failed_stage=exc.result.stage,
        )
        return 1

    checkpoint["finished_at"] = utc_now_iso()
    save_json(checkpoint_path(), checkpoint)
    log_event("cli_result", message=f"\n完成。清单: {manifest_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "crawl"))
