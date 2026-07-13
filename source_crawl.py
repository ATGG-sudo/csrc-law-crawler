#!/usr/bin/env python3
"""CLI for multi-source baseline and incremental acquisition."""

from __future__ import annotations

import argparse
import json
import sys

from csrc_law_crawler.sources.runner import COMPLETE, SourceRunner
from runtime import log_event
from storage import run_with_output_lock


def main() -> int:
    parser = argparse.ArgumentParser(description="运行工作簿注册的公开信源")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true", help="运行全部非微信端点")
    selection.add_argument("--endpoint", action="append", default=[], help="指定 endpoint_id")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--baseline", action="store_true", help="建立基线，不报告历史为新增")
    mode.add_argument("--incremental", action="store_true", help="与已保存基线比较变化")
    parser.add_argument("--resume", metavar="RUN_ID", help="恢复同一指纹下的运行")
    args = parser.parse_args()

    runner = SourceRunner()
    try:
        report = runner.run(
            mode="baseline" if args.baseline else "incremental",
            endpoint_ids=None if args.all else args.endpoint,
            resume_run_id=args.resume,
        )
    except Exception as exc:
        log_event(
            "source_run_error",
            level="ERROR",
            message=f"多信源运行失败: {exc}",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return 1
    log_event("source_run_result", message=json.dumps(report["counts"], ensure_ascii=False))
    return 0 if report["status"] == COMPLETE else 2


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "run-sources"))
