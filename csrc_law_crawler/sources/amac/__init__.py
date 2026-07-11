"""AMAC source adapter exports."""

from __future__ import annotations

from .client import AmacClient
from .discovery import (
    DEFAULT_PRACTICE_SITE_KEYWORDS,
    DEFAULT_RULE_NOTICE_KEYWORDS,
    DEFAULT_SITE_KEYWORDS,
    DEFAULT_XWFB_PAGES,
    DEFAULT_XWFB_SECTIONS,
    deduplicate_candidates,
    discover_policy_candidates,
    discover_site_candidates,
    discover_xwfb_rule_notice_candidates,
    is_xwfb_rule_notice_title,
)
from .identity import canonical_url, classify_document, source_record_id
from .pipeline import amac_manifest_path, crawl_amac, crawl_candidate

__all__ = [
    "AmacClient",
    "DEFAULT_PRACTICE_SITE_KEYWORDS",
    "DEFAULT_RULE_NOTICE_KEYWORDS",
    "DEFAULT_SITE_KEYWORDS",
    "DEFAULT_XWFB_PAGES",
    "DEFAULT_XWFB_SECTIONS",
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
    "source_record_id",
]
