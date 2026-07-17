"""Catalog rule and normalization exports."""

from __future__ import annotations

from importlib import import_module

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
from .identity import (
    canonical_id,
    clean_title,
    is_trial_title,
    normalize_fileno,
    normalize_title,
    normalize_title_without_trial,
)
from .matching import (
    choose_neris_match,
    choose_neris_match_with_rule,
    infer_known_successor_relations,
    infer_trial_replacement_relations,
)
from .entities import (
    deduplicate_catalog_entities,
    match_amac_records,
    record_plain_text,
    seed_neris_entities,
)
from .relations import build_catalog_relations
from .manifest import (
    catalog_manifest,
    catalog_manifest_items,
    match_counts,
    review_queue_items,
)

__all__ = [
    "ALL_CATALOG_RULES",
    "CONFIDENCE_BANDS",
    "RULES_BY_ID",
    "SOURCE_MATCH_REVIEW_CONFIDENCE_THRESHOLD",
    "CatalogRule",
    "catalog_rule_calibration",
    "catalog_rules_manifest",
    "canonical_id",
    "catalog_manifest",
    "catalog_manifest_items",
    "choose_neris_match",
    "choose_neris_match_with_rule",
    "classify_amac_document",
    "clean_title",
    "confidence_band",
    "deduplicate_catalog_entities",
    "effectiveness_for",
    "build_catalog_relations",
    "infer_known_successor_relations",
    "infer_trial_replacement_relations",
    "is_trial_title",
    "match_amac_records",
    "match_counts",
    "normalize_fileno",
    "normalize_catalog_entity",
    "normalize_title",
    "normalize_title_without_trial",
    "plain_text_to_markdown",
    "record_plain_text",
    "review_queue_items",
    "seed_neris_entities",
]


_LAZY_NORMALIZATION_EXPORTS = {
    "effectiveness_for",
    "normalize_catalog_entity",
    "plain_text_to_markdown",
}


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    if name in _LAZY_NORMALIZATION_EXPORTS:
        module = import_module(".normalization", __name__)
        return getattr(module, name)
    raise AttributeError(name)
