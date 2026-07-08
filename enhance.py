#!/usr/bin/env python3
"""Pass 2/3/4 增强抓取：修订、案例、执法文书。"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

from client import HumanLikeClient
from config import DELAY_MAX, DELAY_MIN
from pass2_relations import run_pass2
from pass3_cases import run_pass3
from pass4_writs import run_pass4
from pipeline import (
    STEP_COMPLETE,
    PipelineHalted,
    PipelineRunner,
    PipelineStep,
    StepResult,
)
from runtime import log_event
from storage import (
    load_checkpoint,
    output_dir,
    reports_dir,
    relations_dir,
    run_with_output_lock,
    save_checkpoint,
    save_json,
    utc_now_iso,
)

StepResultRun = Callable[[], StepResult]


def _write_step_results(results: list[StepResult]) -> None:
    save_json(
        reports_dir() / "enhance_step_results.json",
        {
            "schema_version": 1,
            "updated_at": utc_now_iso(),
            "status": (
                STEP_COMPLETE
                if all(item.status == STEP_COMPLETE for item in results)
                else "incomplete"
            ),
            "items": [item.as_dict() for item in results],
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="CSRC 法规库增强抓取 (Pass 2/3/4)")
    parser.add_argument(
        "--pass",
        dest="passes",
        action="append",
        choices=["2", "3", "4", "all"],
        help="执行 pass（可重复指定，默认 all）",
    )
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 条法规 (pass2/3)")
    parser.add_argument(
        "--rebuild-relations",
        action="store_true",
        help="pass2 丢弃旧修订关系并从官网重新拉取",
    )
    parser.add_argument(
        "--skip-related-laws",
        action="store_true",
        help="pass2 仅重建修订关系，不重新拉取关联法规",
    )
    parser.add_argument(
        "--refresh-revision-cache",
        action="store_true",
        help="忽略本地 changeLaw 证据缓存并重新请求",
    )
    parser.add_argument("--delay-min", type=float, default=None)
    parser.add_argument("--delay-max", type=float, default=None)
    parser.add_argument(
        "--skip-law-level-cases",
        action="store_true",
        help="pass3 跳过法规级案例拉取",
    )
    parser.add_argument(
        "--all-writs",
        action="store_true",
        help="pass4 全量执法文书（默认仅 cases 引用的 writ_id）",
    )
    parser.add_argument(
        "--writ-pages",
        type=int,
        default=None,
        help="pass4 最多扫描列表页数（调试）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="pass4 强制重抓（含已有标题无正文的 writ）",
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="允许 pass incomplete/failed 后继续执行后续 pass，并记录非 complete 状态",
    )
    args = parser.parse_args()

    selected = args.passes or ["all"]
    if "all" in selected:
        run_list = ["2", "3", "4"]
    else:
        run_list = selected

    output_dir().mkdir(parents=True, exist_ok=True)
    relations_dir().mkdir(parents=True, exist_ok=True)

    checkpoint = load_checkpoint()
    checkpoint.setdefault("enhance_started_at", utc_now_iso())
    save_checkpoint(checkpoint)

    delay_min = args.delay_min if args.delay_min is not None else DELAY_MIN
    delay_max = args.delay_max if args.delay_max is not None else DELAY_MAX
    client = HumanLikeClient(delay_min=delay_min, delay_max=delay_max)
    log_event("cli_message", message=f"输出目录: {output_dir()}")

    steps: list[PipelineStep] = []

    def add_step(name: str, run: StepResultRun) -> None:
        steps.append(PipelineStep(name=name, run=run))

    if "2" in run_list:
        add_step(
            "enhance.pass2_relations",
            lambda: StepResult.from_counts(
                "enhance.pass2_relations",
                run_pass2(
                    client,
                    limit=args.limit,
                    rebuild=args.rebuild_relations,
                    fetch_related=not args.skip_related_laws,
                    refresh_revision_cache=args.refresh_revision_cache,
                ),
                seen_key="laws",
                written_key="families",
            ),
        )
    if "3" in run_list:
        add_step(
            "enhance.pass3_cases",
            lambda: StepResult.from_counts(
                "enhance.pass3_cases",
                run_pass3(
                    client,
                    limit=args.limit,
                    skip_law_level=args.skip_law_level_cases,
                ),
                seen_key="laws",
                written_key="processed",
            ),
        )
    if "4" in run_list:
        add_step(
            "enhance.pass4_writs",
            lambda: StepResult.from_counts(
                "enhance.pass4_writs",
                run_pass4(
                    client,
                    all_writs=args.all_writs,
                    limit_pages=args.writ_pages,
                    force=args.force,
                ),
                seen_key="targets",
                written_key="saved",
            ),
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
            message=f"增强抓取失败: {exc}",
            error_message=str(exc),
            failed_stage=exc.result.stage,
        )
        return 1

    checkpoint = load_checkpoint()
    checkpoint["enhance_finished_at"] = utc_now_iso()
    save_checkpoint(checkpoint)
    log_event("cli_result", message="\n增强抓取完成。")
    return 0


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "enhance"))
