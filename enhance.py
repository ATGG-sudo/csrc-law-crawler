#!/usr/bin/env python3
"""Pass 2/3/4 增强抓取：修订、案例、执法文书。"""

from __future__ import annotations

import argparse
import sys

from client import HumanLikeClient
from config import OUTPUT_DIR
from pass2_relations import run_pass2
from pass3_cases import run_pass3
from pass4_writs import run_pass4
from storage import load_checkpoint, relations_dir, save_checkpoint, utc_now_iso


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
        "--no-patch-revision-ref",
        action="store_true",
        help="pass2 不回写 reg_*.json 的 revision_ref",
    )
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
    args = parser.parse_args()

    selected = args.passes or ["all"]
    if "all" in selected:
        run_list = ["2", "3", "4"]
    else:
        run_list = selected

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    relations_dir().mkdir(parents=True, exist_ok=True)

    checkpoint = load_checkpoint()
    checkpoint.setdefault("enhance_started_at", utc_now_iso())
    save_checkpoint(checkpoint)

    client_kwargs = {}
    if args.delay_min is not None:
        client_kwargs["delay_min"] = args.delay_min
    if args.delay_max is not None:
        client_kwargs["delay_max"] = args.delay_max
    client = HumanLikeClient(**client_kwargs)
    print(f"输出目录: {OUTPUT_DIR}")

    if "2" in run_list:
        run_pass2(
            client,
            limit=args.limit,
            patch_revision_ref=not args.no_patch_revision_ref,
            rebuild=args.rebuild_relations,
            fetch_related=not args.skip_related_laws,
            refresh_revision_cache=args.refresh_revision_cache,
        )
    if "3" in run_list:
        run_pass3(
            client,
            limit=args.limit,
            skip_law_level=args.skip_law_level_cases,
        )
    if "4" in run_list:
        run_pass4(
            client,
            all_writs=args.all_writs,
            limit_pages=args.writ_pages,
            force=args.force,
        )

    checkpoint = load_checkpoint()
    checkpoint["enhance_finished_at"] = utc_now_iso()
    save_checkpoint(checkpoint)
    print("\n增强抓取完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
