#!/usr/bin/env python3
"""Orchestrate the P0-P2 multi-source repair pipeline."""

from __future__ import annotations

import argparse
import sys
from typing import Callable

from amac_crawl import DEFAULT_XWFB_PAGES, crawl_amac
from build_catalog import build_catalog
from build_canonical_relations import build_canonical_relations
from client import HumanLikeClient
from config import DELAY_MAX, DELAY_MIN
from coverage_gaps import detect_coverage_gaps
from download_assets import rebuild_asset_manifests
from export_markdown_catalog import export_catalog_markdown
from neris_attachments import run as run_neris_attachments
from normalize_catalog import normalize_catalog
from normalize_laws import normalize_laws
from pass2_relations import run_pass2
from prefetch_revision_evidence import prefetch
from pipeline import (
    STEP_COMPLETE,
    STEP_FAILED,
    PipelineHalted,
    PipelineRunner,
    PipelineStep,
    StepResult,
)
from runtime import log_event
from storage import reports_dir, run_with_output_lock, save_json, utc_now_iso
from validate_catalog_exports import validate_catalog_exports

StepResultRun = Callable[[], StepResult]


def _write_step_results(results: list[StepResult]) -> None:
    save_json(
        reports_dir() / "repair_step_results.json",
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


def _build_canonical_relations_step() -> StepResult:
    graph = build_canonical_relations()
    return StepResult(
        stage="p2.build_canonical_relations",
        status=STEP_COMPLETE,
        seen=int((graph.get("counts") or {}).get("nodes") or 0),
        written=int((graph.get("counts") or {}).get("edges") or 0),
    )


def _validate_catalog_exports_step() -> StepResult:
    issues, _summary = validate_catalog_exports()
    return StepResult(
        stage="p2.validate_catalog_exports",
        status=STEP_COMPLETE if not issues else STEP_FAILED,
        seen=len(issues),
        failed=len(issues),
        message=(
            None
            if not issues
            else f"统一目录 normalized/Markdown 校验失败 {len(issues)} 项"
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="执行 P0-P2 多源法规修复")
    parser.add_argument(
        "--phase",
        action="append",
        choices=["p0", "p1", "p2", "all"],
        help="可重复指定；默认 all",
    )
    parser.add_argument("--law-limit", type=int, default=None)
    parser.add_argument("--policy-limit", type=int, default=None)
    parser.add_argument("--site-limit", type=int, default=None)
    parser.add_argument("--xwfb-pages", type=int, default=DEFAULT_XWFB_PAGES)
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--skip-neris-attachments", action="store_true")
    parser.add_argument("--skip-revision-rebuild", action="store_true")
    parser.add_argument("--delay-min", type=float, default=DELAY_MIN)
    parser.add_argument("--delay-max", type=float, default=DELAY_MAX)
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="NERIS并发数；默认1，提速需显式指定",
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="允许阶段 incomplete/failed 后继续执行后续阶段，并记录非 complete 状态",
    )
    args = parser.parse_args()

    phases = args.phase or ["all"]
    if "all" in phases:
        phases = ["p0", "p1", "p2"]
    if (
        "p0" in phases
        and args.law_limit is not None
        and not args.skip_revision_rebuild
    ):
        parser.error(
            "--law-limit 不能用于正式修订关系重建；"
            "请同时指定 --skip-revision-rebuild，或使用独立测试输出目录"
        )

    steps: list[PipelineStep] = []

    def add_step(name: str, run: StepResultRun) -> None:
        steps.append(PipelineStep(name=name, run=run))

    if "p0" in phases:
        if not args.skip_revision_rebuild:
            add_step(
                "p0.prefetch_revision_evidence",
                lambda: StepResult.from_counts(
                    "p0.prefetch_revision_evidence",
                    prefetch(
                        limit=args.law_limit,
                        workers=args.workers,
                        delay_min=args.delay_min,
                        delay_max=args.delay_max,
                    ),
                    seen_key="total",
                    written_key="fetched",
                    skipped_key="cached",
                ),
            )
            add_step(
                "p0.pass2_relations",
                lambda: StepResult.from_counts(
                    "p0.pass2_relations",
                    run_pass2(
                        HumanLikeClient(
                            delay_min=args.delay_min,
                            delay_max=args.delay_max,
                        ),
                        limit=args.law_limit,
                        rebuild=True,
                        fetch_related=False,
                        refresh_revision_cache=False,
                    ),
                    seen_key="laws",
                    written_key="families",
                ),
            )
        if not args.skip_neris_attachments:
            add_step(
                "p0.neris_attachments",
                lambda: StepResult.from_counts(
                    "p0.neris_attachments",
                    run_neris_attachments(
                        limit=args.law_limit,
                        download=not args.discover_only,
                        delay_min=args.delay_min,
                        delay_max=args.delay_max,
                        workers=args.workers,
                    ),
                    seen_key="laws",
                    written_key="downloaded",
                    skipped_key="attachments",
                    failed_keys=("failed", "law_failures"),
                ),
            )
        add_step(
            "p0.normalize_laws",
            lambda: StepResult.from_counts(
                "p0.normalize_laws",
                normalize_laws(limit=args.law_limit, force=True),
            ),
        )
        add_step(
            "p0.rebuild_asset_manifests",
            lambda: StepResult.from_counts(
                "p0.rebuild_asset_manifests",
                rebuild_asset_manifests(),
                seen_key="seen_assets",
            ),
        )
        add_step(
            "p0.coverage_gaps",
            lambda: StepResult.from_counts(
                "p0.coverage_gaps",
                detect_coverage_gaps(limit=args.law_limit),
                seen_key="scanned_laws",
                written_key="count",
                failed_keys=(),
            ),
        )

    if "p1" in phases:
        add_step(
            "p1.amac_crawl",
            lambda: StepResult.from_counts(
                "p1.amac_crawl",
                crawl_amac(
                    policy_limit=args.policy_limit,
                    site_limit=args.site_limit,
                    xwfb_pages=args.xwfb_pages,
                    download_assets=not args.discover_only,
                    force=False,
                    delay_min=args.delay_min,
                    delay_max=args.delay_max,
                ),
                seen_key="candidate_count",
            ),
        )

    if "p2" in phases:
        add_step(
            "p2.build_catalog",
            lambda: StepResult.from_counts(
                "p2.build_catalog",
                build_catalog(clean=True),
            ),
        )
        add_step(
            "p2.normalize_catalog",
            lambda: StepResult.from_counts(
                "p2.normalize_catalog",
                normalize_catalog(
                    force=True,
                    clean=True,
                ),
            ),
        )
        add_step(
            "p2.export_catalog_markdown",
            lambda: StepResult.from_counts(
                "p2.export_catalog_markdown",
                export_catalog_markdown(
                    force=True,
                    clean=True,
                ),
            ),
        )
        add_step(
            "p2.build_canonical_relations",
            _build_canonical_relations_step,
        )
        add_step(
            "p2.validate_catalog_exports",
            _validate_catalog_exports_step,
        )

    try:
        run = PipelineRunner(
            allow_incomplete=args.allow_incomplete,
            on_update=_write_step_results,
        ).run(steps)
        log_event(
            "pipeline_result",
            status=run.status,
            stages=len(run.items),
        )
    except KeyboardInterrupt:
        return 130
    except PipelineHalted as exc:
        log_event(
            "cli_error",
            level="ERROR",
            message=f"修复失败: {exc}",
            error_message=str(exc),
            failed_stage=exc.result.stage,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "repair"))
