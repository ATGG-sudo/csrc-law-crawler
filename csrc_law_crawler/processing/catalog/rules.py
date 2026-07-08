"""Catalog rule compatibility exports."""

from __future__ import annotations

from catalog_rules import (
    ALL_CATALOG_RULES,
    CONFIDENCE_BANDS,
    RULES_BY_ID,
    SOURCE_MATCH_REVIEW_CONFIDENCE_THRESHOLD,
    CatalogRule,
    catalog_rule_calibration,
    catalog_rules_manifest,
    classify_amac_document,
    confidence_band,
)

__all__ = [
    "ALL_CATALOG_RULES",
    "CONFIDENCE_BANDS",
    "RULES_BY_ID",
    "SOURCE_MATCH_REVIEW_CONFIDENCE_THRESHOLD",
    "CatalogRule",
    "catalog_rule_calibration",
    "catalog_rules_manifest",
    "classify_amac_document",
    "confidence_band",
]
