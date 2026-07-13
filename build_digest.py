#!/usr/bin/env python3
"""Build the source baseline/change digest."""

from __future__ import annotations

import argparse
import json

from csrc_law_crawler.sources.digest import build_digest
from runtime import log_event
from storage import run_with_output_lock


def main() -> int:
    parser = argparse.ArgumentParser(description="生成信源基线与变化摘要")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    try:
        report = build_digest(run_id=args.run_id)
    except Exception as exc:
        log_event("cli_error", level="ERROR", message=f"失败: {exc}")
        return 1
    log_event("cli_result", message=json.dumps(report["counts"], ensure_ascii=False))
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(run_with_output_lock(main, "build-digest"))
