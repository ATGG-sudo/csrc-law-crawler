#!/usr/bin/env python3
"""Run the controlled SPC seeds and the judicial-interpretation directory monitor."""

from __future__ import annotations

import argparse
import json
import sys

from csrc_law_crawler.sources.court_judicial_interpretation import COURT_ENDPOINT_ID
from csrc_law_crawler.sources.court_judicial_interpretation_monitor import (
    COURT_MONITOR_ENDPOINT_ID,
)
from csrc_law_crawler.sources.court_monitor_artifacts import build_monitor_artifacts
from csrc_law_crawler.sources.digest import build_digest
from csrc_law_crawler.sources.runner import COMPLETE, SourceRunner
from runtime import log_event
from storage import run_with_output_lock


def _run() -> int:
    parser = argparse.ArgumentParser(
        description="监测最高法“权威发布—司法解释”栏目并生成复核队列"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--baseline", action="store_true", help="抓取全部详情并建立静默基线")
    mode.add_argument("--daily", action="store_true", help="完整枚举列表，仅刷新新增或列表变化详情")
    mode.add_argument("--weekly", action="store_true", help="执行 daily 并条件复核全部详情")
    parser.add_argument("--resume", metavar="RUN_ID", help="恢复相同运行模式与代码指纹的运行")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--retry-incomplete", action="store_true")
    parser.add_argument(
        "--output-root",
        help="兼容全局参数；实际路径在 settings 初始化时读取",
    )
    args = parser.parse_args()

    logical_mode = (
        "baseline" if args.baseline else "weekly" if args.weekly else "daily"
    )
    try:
        report = SourceRunner().run(
            mode="baseline" if args.baseline else "incremental",
            endpoint_ids=[COURT_ENDPOINT_ID, COURT_MONITOR_ENDPOINT_ID],
            resume_run_id=args.resume,
            workers=1,
            refresh_details=args.weekly,
            retry_failed=args.retry_failed,
            retry_incomplete=args.retry_incomplete,
        )
        artifacts = build_monitor_artifacts(run_id=report["run_id"])
        digest = build_digest(run_id=report["run_id"])
    except Exception as exc:
        log_event(
            "court_monitor_error",
            level="ERROR",
            message=f"最高法司法解释栏目监测失败: {exc}",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return 1

    log_event(
        "court_monitor_result",
        message=json.dumps(
            {
                "mode": logical_mode,
                "run_id": report["run_id"],
                "status": report["status"],
                "inventory": artifacts["inventory"]["item_count"],
                "actionable": artifacts["review_queue"]["actionable_count"],
                "requests": {
                    "list": digest["counts"].get("list_requests", 0),
                    "detail": digest["counts"].get("detail_requests", 0),
                    "not_modified": digest["counts"].get("detail_not_modified", 0),
                },
            },
            ensure_ascii=False,
        ),
    )
    return 0 if report["status"] == COMPLETE else 2


def main() -> int:
    return run_with_output_lock(_run, "court-monitor")


if __name__ == "__main__":
    sys.exit(main())
