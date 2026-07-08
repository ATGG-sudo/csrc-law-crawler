#!/usr/bin/env python3
"""Prefetch NERIS changeLaw responses into a resumable evidence cache."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
from typing import Any

from api import fetch_change_law
from client import HumanLikeClient
from runtime import log_event
from storage import (
    iter_reg_law_ids,
    revision_evidence_cache_path,
    run_with_output_lock,
    save_json,
)


def _fetch(
    law_id: str,
    *,
    delay_min: float,
    delay_max: float,
) -> tuple[str, dict[str, Any]]:
    client = HumanLikeClient(
        delay_min=delay_min,
        delay_max=delay_max,
        batch_size=0,
    )
    return law_id, fetch_change_law(client, law_id)


def prefetch(
    *,
    limit: int | None = None,
    workers: int = 2,
    force: bool = False,
    delay_min: float = 0.05,
    delay_max: float = 0.15,
) -> dict[str, int]:
    law_ids = iter_reg_law_ids(limit=limit)
    pending = [
        law_id
        for law_id in law_ids
        if force or not revision_evidence_cache_path(law_id).exists()
    ]
    counts = {
        "total": len(law_ids),
        "cached": len(law_ids) - len(pending),
        "fetched": 0,
        "failed": 0,
    }
    if not pending:
        return counts

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = {
            executor.submit(
                _fetch,
                law_id,
                delay_min=delay_min,
                delay_max=delay_max,
            ): law_id
            for law_id in pending
        }
        for index, future in enumerate(
            concurrent.futures.as_completed(futures),
            start=1,
        ):
            law_id = futures[future]
            try:
                fetched_id, payload = future.result()
                save_json(revision_evidence_cache_path(fetched_id), payload)
                counts["fetched"] += 1
            except Exception as exc:
                counts["failed"] += 1
                log_event(
                    "revision_evidence_failed",
                    level="ERROR",
                    message=f"  !! {law_id}: {exc}",
                    law_id=law_id,
                    error_message=str(exc),
                )
            if index % 100 == 0 or index == len(pending):
                log_event(
                    "revision_evidence_progress",
                    message=(
                        f"  evidence {index}/{len(pending)} "
                        f"failed={counts['failed']}"
                    ),
                    index=index,
                    total=len(pending),
                    failed=counts["failed"],
                )
    except BaseException:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="并发预取 changeLaw 修订证据")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--delay-min", type=float, default=0.05)
    parser.add_argument("--delay-max", type=float, default=0.15)
    args = parser.parse_args()
    try:
        result = prefetch(
            limit=args.limit,
            workers=args.workers,
            force=args.force,
            delay_min=args.delay_min,
            delay_max=args.delay_max,
        )
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log_event("cli_error", level="ERROR", message=f"失败: {exc}", error_message=str(exc))
        return 1
    log_event("cli_result", message=json.dumps(result, ensure_ascii=False))
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "prefetch-revision-evidence"))
