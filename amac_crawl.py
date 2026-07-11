#!/usr/bin/env python3
"""Crawl AMAC as a supplemental official source without mutating NERIS records."""

from __future__ import annotations

import argparse
import sys

from csrc_law_crawler.sources.amac.client import AmacClient, random, time
from csrc_law_crawler.sources.amac.discovery import (
    DEFAULT_PRACTICE_SITE_KEYWORDS,
    DEFAULT_RULE_NOTICE_KEYWORDS,
    DEFAULT_SITE_KEYWORDS,
    DEFAULT_XWFB_PAGES,
    DEFAULT_XWFB_SECTIONS,
    NON_RULE_NOTICE_WORDS,
    POLICY_SEARCH_URL,
    RULE_NOTICE_ACTION_WORDS,
    SITE_SEARCH_URL,
    XWFB_ARTICLE_DATE_RE,
    XWFB_PAGE_COUNT_RE,
    date_from_xwfb_url as _date_from_xwfb_url,
    deduplicate_candidates,
    discover_policy_candidates,
    discover_site_candidates,
    discover_xwfb_rule_notice_candidates,
    is_xwfb_rule_notice_title,
    xwfb_list_url as _xwfb_list_url,
)
from csrc_law_crawler.sources.amac.identity import (
    DATE_SUFFIX_RE,
    TITLE_PREFIX_RE,
    canonical_url,
    classified_document_metadata as _classified_document_metadata,
    classify_document,
    clean_attachment_title as _clean_attachment_title,
    clean_text as _clean_text,
    source_record_id,
)
from csrc_law_crawler.sources.amac.parser import (
    ASSET_SUFFIXES,
    FILENO_RE,
    asset_links as _asset_links,
    content_root as _content_root,
    metadata_from_page as _metadata_from_page,
    title_from_page as _title_from_page,
)
from csrc_law_crawler.sources.amac.pipeline import (
    amac_assets_root,
    amac_manifest_path,
    crawl_amac,
    crawl_candidate,
    download_asset as _download_asset,
    extract_asset_text as _extract_asset_text,
)
from runtime import log_event
from storage import run_with_output_lock

__all__ = [
    "ASSET_SUFFIXES",
    "AmacClient",
    "DATE_SUFFIX_RE",
    "DEFAULT_PRACTICE_SITE_KEYWORDS",
    "DEFAULT_RULE_NOTICE_KEYWORDS",
    "DEFAULT_SITE_KEYWORDS",
    "DEFAULT_XWFB_PAGES",
    "DEFAULT_XWFB_SECTIONS",
    "FILENO_RE",
    "NON_RULE_NOTICE_WORDS",
    "POLICY_SEARCH_URL",
    "RULE_NOTICE_ACTION_WORDS",
    "SITE_SEARCH_URL",
    "TITLE_PREFIX_RE",
    "XWFB_ARTICLE_DATE_RE",
    "XWFB_PAGE_COUNT_RE",
    "_asset_links",
    "_classified_document_metadata",
    "_clean_attachment_title",
    "_clean_text",
    "_content_root",
    "_date_from_xwfb_url",
    "_download_asset",
    "_extract_asset_text",
    "_metadata_from_page",
    "_title_from_page",
    "_xwfb_list_url",
    "amac_assets_root",
    "amac_manifest_path",
    "canonical_url",
    "classify_document",
    "crawl_amac",
    "crawl_candidate",
    "deduplicate_candidates",
    "discover_policy_candidates",
    "discover_site_candidates",
    "discover_xwfb_rule_notice_candidates",
    "is_xwfb_rule_notice_title",
    "random",
    "source_record_id",
    "time",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 AMAC 补充制度和实践材料")
    parser.add_argument("--policy-limit", type=int, default=None)
    parser.add_argument("--site-limit", type=int, default=None)
    parser.add_argument(
        "--xwfb-pages",
        type=int,
        default=DEFAULT_XWFB_PAGES,
        help="每个 xwfb 栏目扫描页数；0 表示跳过",
    )
    parser.add_argument("--keyword", action="append", dest="keywords")
    parser.add_argument("--no-download-assets", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--delay-min", type=float, default=0.25)
    parser.add_argument("--delay-max", type=float, default=0.7)
    parser.add_argument(
        "--amac-insecure-tls",
        action="store_true",
        help="临时关闭 AMAC HTTPS 证书校验，并在 manifest 中记录",
    )
    args = parser.parse_args()
    try:
        manifest = crawl_amac(
            policy_limit=args.policy_limit,
            site_limit=args.site_limit,
            xwfb_pages=args.xwfb_pages,
            keywords=args.keywords,
            download_assets=not args.no_download_assets,
            force=args.force,
            delay_min=args.delay_min,
            delay_max=args.delay_max,
            verify_tls=not args.amac_insecure_tls,
        )
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log_event("cli_error", level="ERROR", message=f"失败: {exc}", error_message=str(exc))
        return 1
    log_event(
        "cli_result",
        message=(
            f"完成: candidates={manifest['candidate_count']} count={manifest['count']} "
            f"written={manifest['written']} failed={manifest['failed']} -> {amac_manifest_path()}"
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "amac-crawl"))
