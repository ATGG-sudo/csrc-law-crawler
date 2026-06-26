#!/usr/bin/env python3
"""Orchestrate the P0-P2 multi-source repair pipeline."""

from __future__ import annotations

import argparse
import sys

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
from validate_catalog_exports import validate_catalog_exports


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

    try:
        if "p0" in phases:
            if not args.skip_revision_rebuild:
                evidence_result = prefetch(
                    limit=args.law_limit,
                    workers=args.workers,
                    delay_min=args.delay_min,
                    delay_max=args.delay_max,
                )
                if evidence_result["failed"]:
                    raise RuntimeError(
                        f"修订证据预取失败 {evidence_result['failed']} 条"
                    )
                client = HumanLikeClient(
                    delay_min=args.delay_min,
                    delay_max=args.delay_max,
                )
                run_pass2(
                    client,
                    limit=args.law_limit,
                    rebuild=True,
                    fetch_related=False,
                    refresh_revision_cache=False,
                )
            if not args.skip_neris_attachments:
                run_neris_attachments(
                    limit=args.law_limit,
                    download=not args.discover_only,
                    delay_min=args.delay_min,
                    delay_max=args.delay_max,
                    workers=args.workers,
                )
            normalize_laws(limit=args.law_limit, force=True)
            rebuild_asset_manifests()
            detect_coverage_gaps(limit=args.law_limit)

        if "p1" in phases:
            crawl_amac(
                policy_limit=args.policy_limit,
                site_limit=args.site_limit,
                xwfb_pages=args.xwfb_pages,
                download_assets=not args.discover_only,
                force=False,
                delay_min=args.delay_min,
                delay_max=args.delay_max,
            )

        if "p2" in phases:
            build_catalog(clean=True)
            normalize_catalog(
                force=True,
                clean=True,
            )
            export_catalog_markdown(
                force=True,
                clean=True,
            )
            build_canonical_relations()
            issues, _summary = validate_catalog_exports()
            if issues:
                raise RuntimeError(
                    f"统一目录 normalized/Markdown 校验失败 {len(issues)} 项"
                )
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"修复失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
