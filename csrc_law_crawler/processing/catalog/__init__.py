"""Catalog rule and normalization exports."""

from __future__ import annotations

from .rules import (
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
from .normalization import (
    effectiveness_for,
    normalize_catalog_entity,
    plain_text_to_markdown,
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
    "effectiveness_for",
    "normalize_catalog_entity",
    "plain_text_to_markdown",
]
