#!/usr/bin/env python3
"""CLI for manually exported wechat-article-exporter bundles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from csrc_law_crawler.sources.wechat import import_wechat_bundle
from runtime import log_event
from storage import run_with_output_lock


def main() -> int:
    parser = argparse.ArgumentParser(description="导入基小律 JSON + HTML 导出包")
    parser.add_argument("--input", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = import_wechat_bundle(args.input)
    except Exception as exc:
        log_event(
            "wechat_import_error",
            level="ERROR",
            message=f"微信导入失败: {exc}",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return 1
    log_event("wechat_import_result", message=json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "import-wechat"))
