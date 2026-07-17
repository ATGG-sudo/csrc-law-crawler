"""Auditable rule identifiers and defaults for catalog processing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CatalogRule:
    rule_id: str
    description: str
    confidence: float
    evidence_fields: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "description": self.description,
            "confidence": self.confidence,
            "confidence_band": confidence_band(self.confidence),
            "evidence_fields": list(self.evidence_fields),
        }


CONFIDENCE_BANDS: tuple[tuple[str, float], ...] = (
    ("certain", 0.99),
    ("strong", 0.90),
    ("moderate", 0.75),
    ("low", 0.0),
)
SOURCE_MATCH_REVIEW_CONFIDENCE_THRESHOLD = 0.95


def confidence_band(confidence: float) -> str:
    for name, minimum in CONFIDENCE_BANDS:
        if confidence >= minimum:
            return name
    return "low"


def catalog_rule_calibration() -> dict[str, Any]:
    return {
        "confidence_bands": [
            {"name": name, "minimum_confidence": minimum} for name, minimum in CONFIDENCE_BANDS
        ],
        "source_match_review_confidence_threshold": (SOURCE_MATCH_REVIEW_CONFIDENCE_THRESHOLD),
    }


AMAC_PUBLICATION_NOTICE = CatalogRule(
    "classification.amac_publication_notice",
    "Classify AMAC records whose title is a publication notice or announcement.",
    0.95,
    ("title", "url"),
)
AMAC_REGULATORY_PRACTICE = CatalogRule(
    "classification.amac_regulatory_practice",
    "Classify AMAC registration-practice notices and database dynamic pages.",
    0.9,
    ("title", "url"),
)
AMAC_SELF_REGULATORY_RULE = CatalogRule(
    "classification.amac_self_regulatory_rule",
    "Classify AMAC records whose title contains rule-like terms.",
    0.85,
    ("title", "rule_words"),
)
AMAC_SUPPORTING_MATERIAL = CatalogRule(
    "classification.amac_supporting_material",
    "Classify AMAC records as supporting material when no stronger classifier applies.",
    0.55,
    ("title", "url"),
)
MATERIAL_MANUAL_OVERRIDE = CatalogRule(
    "material.manual_override",
    "Use an evidence-backed manual classification override.",
    1.0,
    ("canonical_id", "verified_at", "evidence_url", "note"),
)
MATERIAL_AMAC_INDUSTRY_REFERENCE = CatalogRule(
    "material.amac_industry_reference",
    "Treat AMAC industry research sections as reference material.",
    1.0,
    ("source_category", "source_section"),
)
MATERIAL_AMAC_ENFORCEMENT_REFERENCE = CatalogRule(
    "material.amac_enforcement_reference",
    "Treat AMAC discipline and institution-management sections as enforcement reference material.",
    1.0,
    ("source_category", "source_section"),
)
MATERIAL_CONSULTATION_TITLE = CatalogRule(
    "material.consultation_title",
    "Treat explicit consultation or draft titles as reference material.",
    0.99,
    ("title", "metadata.name"),
)
MATERIAL_REFERENCE_TITLE = CatalogRule(
    "material.reference_title",
    "Treat explicit interpretation, template, training, or research titles as reference material.",
    0.95,
    ("title", "metadata.name"),
)
MATERIAL_PUBLISHING_WRAPPER = CatalogRule(
    "material.publishing_wrapper",
    "Treat a publication wrapper as reference material when it publishes a distinct canonical document.",
    0.95,
    ("publishes",),
)
MATERIAL_SOURCE_RULE_LANE = CatalogRule(
    "material.source_rule_lane",
    "Use a trusted source rule lane or official rule catalog as normative material evidence.",
    0.99,
    ("material_lane", "source_system", "raw_status"),
)
MATERIAL_SOURCE_REFERENCE_LANE = CatalogRule(
    "material.source_reference_lane",
    "Use a trusted source reference lane or reference document type.",
    0.99,
    ("material_lane", "document_type"),
)
MATERIAL_AMAC_RULE_HEURISTIC = CatalogRule(
    "material.amac_rule_heuristic",
    "Treat an AMAC rule-like record as normative when no stronger reference signal applies.",
    0.85,
    ("document_type", "title"),
)
MATERIAL_INSUFFICIENT_EVIDENCE = CatalogRule(
    "material.insufficient_evidence",
    "Leave material nature unknown when no reliable rule or reference signal applies.",
    0.5,
    ("document_type", "source_system", "title"),
)
MATCH_NO_NERIS_TITLE = CatalogRule(
    "match.neris_title_absent",
    "Treat an AMAC record as new to NERIS when no normalized-title candidates exist.",
    1.0,
    ("normalized_title", "neris_candidate_count"),
)
MATCH_TITLE_FILENO = CatalogRule(
    "match.normalized_title_fileno",
    "Match AMAC and NERIS records when normalized title and document number agree.",
    1.0,
    ("normalized_title", "fileno"),
)
MATCH_TITLE_DATE = CatalogRule(
    "match.normalized_title_pub_date_window",
    "Match AMAC and NERIS records when normalized title agrees and publication dates are within three days.",
    0.99,
    ("normalized_title", "amac_pub_date", "neris_pub_date", "distance_days"),
)
MATCH_UNIQUE_TITLE = CatalogRule(
    "match.unique_normalized_title",
    "Match the only NERIS candidate with the same normalized title when stronger evidence is missing.",
    0.92,
    ("normalized_title", "neris_candidate_count"),
)
MATCH_AMAC_INTERNAL_TITLE_DATE = CatalogRule(
    "match.amac_internal_title_date",
    "Merge AMAC records with matching normalized title and compatible publication date.",
    0.95,
    ("normalized_title", "pub_date", "distance_days"),
)
MATCH_AMBIGUOUS_TITLE = CatalogRule(
    "match.ambiguous_normalized_title",
    "Mark AMAC records ambiguous when multiple NERIS records share the normalized title.",
    0.4,
    ("normalized_title", "neris_candidate_count"),
)
EFFECT_EXPLICIT_HISTORICAL = CatalogRule(
    "effectiveness.explicit_historical_status",
    "Use explicit source status or title markers when the document is historical.",
    1.0,
    ("raw_status", "title", "metadata.name"),
)
EFFECT_COMMENT_DRAFT = CatalogRule(
    "effectiveness.comment_draft_signal",
    "Treat consultation drafts and draft notices as reference material.",
    0.98,
    ("title", "metadata.name", "plain_text_sample"),
)
EFFECT_EXPLICIT_CURRENT = CatalogRule(
    "effectiveness.explicit_current_status",
    "Use explicit source status when it states the document is current.",
    1.0,
    ("raw_status",),
)
EFFECT_SUPERSEDED_BY_CATALOG = CatalogRule(
    "effectiveness.superseded_by_catalog_relation",
    "Treat a document as historical when a catalog supersedes relation targets it.",
    0.86,
    ("superseded_by",),
)
EFFECT_REFERENCE_DOCUMENT_TYPE = CatalogRule(
    "effectiveness.reference_document_type",
    "Treat reference document types as not applicable for effectiveness.",
    0.95,
    ("document_type",),
)
EFFECT_REFERENCE_TITLE = CatalogRule(
    "effectiveness.reference_title_signal",
    "Treat reference-like titles as not applicable for effectiveness.",
    0.9,
    ("title", "metadata.name"),
)
EFFECT_AMAC_OFFICIAL_DEFAULT = CatalogRule(
    "effectiveness.amac_official_rule_default",
    "Default AMAC official rules without explicit status to current.",
    0.75,
    ("source_system", "document_type", "raw_status"),
)
EFFECT_INSUFFICIENT_EVIDENCE = CatalogRule(
    "effectiveness.insufficient_evidence",
    "Mark effectiveness unknown when no stronger rule applies.",
    0.5,
    ("source_system", "document_type", "raw_status"),
)
EFFECT_MANUAL_OVERRIDE = CatalogRule(
    "effectiveness.manual_override",
    "Use an evidence-backed manual effectiveness override.",
    1.0,
    ("canonical_id", "verified_at", "evidence_url", "note"),
)
EFFECT_REFERENCE_MATERIAL = CatalogRule(
    "effectiveness.reference_material",
    "Effectiveness does not apply to reference material.",
    1.0,
    ("material_classification",),
)
EFFECT_PENDING = CatalogRule(
    "effectiveness.pending",
    "Treat a promulgated rule as pending before its effective date.",
    1.0,
    ("raw_status", "effective_date", "as_of"),
)
EFFECT_INEFFECTIVE_DATE = CatalogRule(
    "effectiveness.ineffective_date",
    "Treat a rule as historical once its ineffective date is reached.",
    1.0,
    ("ineffective_date", "as_of"),
)
EFFECT_UNVERIFIED_OFFICIAL_RULE = CatalogRule(
    "effectiveness.unverified_official_rule",
    "Keep an official rule without currentness evidence in the review queue.",
    0.75,
    ("source_system", "document_type", "raw_status"),
)
EFFECT_TRIAL_COMMENCED = CatalogRule(
    "effectiveness.trial_official_commenced",
    "Treat an officially promulgated trial rule as current after a proven commencement date.",
    0.95,
    ("title", "effective_date", "as_of", "source_system", "page_role"),
)
TRIAL_REPLACEMENT = CatalogRule(
    "relation.trial_replacement.same_title_later_formal",
    "Infer that a later formal same-title rule supersedes an earlier trial rule.",
    0.86,
    ("normalized_title", "trial_pub_date", "formal_pub_date", "pub_org"),
)
RELATION_DRAFT_FINALIZED = CatalogRule(
    "relation.draft_finalized.exact_instrument",
    "Link a formal rule to an earlier consultation draft with the same exact instrument name.",
    0.98,
    ("instrument_title", "pub_org", "draft_pub_date", "formal_pub_date"),
)
RELATION_SAME_INSTRUMENT_COPY = CatalogRule(
    "relation.same_instrument_copy.title_fileno",
    "Link source copies whose exact instrument title and document number agree.",
    1.0,
    ("instrument_title", "fileno"),
)
RELATION_EXPLICIT_SUCCESSOR = CatalogRule(
    "relation.explicit_successor.body_clause",
    "Infer supersession only from an explicit revision or repeal clause in the official text.",
    0.99,
    ("old_title", "new_title", "clause"),
)
RELATION_OFFICIAL_SUCCESSOR = CatalogRule(
    "relation.official_successor.known_revision_chain",
    "Use official revision or repeal notices to mark known successor versions.",
    1.0,
    ("title", "fileno", "successor_title", "successor_fileno", "official_url"),
)
RELATION_AMAC_PAGE_ATTACHMENT = CatalogRule(
    "relation.amac_page_attachment",
    "Infer that an AMAC page publishes the official attachment documents it links.",
    1.0,
    ("parent_source_record_id", "attachment_source_record_id"),
)
RELATION_NERIS_TITLE_QUOTED_DOCUMENT = CatalogRule(
    "relation.neris_title_quoted_document",
    "Infer publication relation from a NERIS notice title that quotes exactly one known document title.",
    0.9,
    ("notice_title", "quoted_title"),
)
REVIEW_SOURCE_MATCH_AMBIGUOUS = CatalogRule(
    "review.source_match_ambiguous",
    "Queue AMAC records whose NERIS match is ambiguous.",
    1.0,
    ("match_status", "neris_candidates"),
)
REVIEW_SOURCE_MATCH_LOW_CONFIDENCE = CatalogRule(
    "review.source_match_low_confidence",
    "Queue non-ambiguous AMAC matches below the source-match confidence review threshold.",
    1.0,
    ("match_status", "match_rule_id", "confidence"),
)
REVIEW_EFFECTIVENESS_UNKNOWN = CatalogRule(
    "review.effectiveness_unknown",
    "Queue AMAC official rules whose source does not state effectiveness.",
    1.0,
    ("document_type", "status"),
)

AMAC_CLASSIFICATION_RULES = (
    AMAC_PUBLICATION_NOTICE,
    AMAC_REGULATORY_PRACTICE,
    AMAC_SELF_REGULATORY_RULE,
    AMAC_SUPPORTING_MATERIAL,
)

MATERIAL_CLASSIFICATION_RULES = (
    MATERIAL_MANUAL_OVERRIDE,
    MATERIAL_AMAC_INDUSTRY_REFERENCE,
    MATERIAL_AMAC_ENFORCEMENT_REFERENCE,
    MATERIAL_CONSULTATION_TITLE,
    MATERIAL_REFERENCE_TITLE,
    MATERIAL_PUBLISHING_WRAPPER,
    MATERIAL_SOURCE_RULE_LANE,
    MATERIAL_SOURCE_REFERENCE_LANE,
    MATERIAL_AMAC_RULE_HEURISTIC,
    MATERIAL_INSUFFICIENT_EVIDENCE,
)

MATCHING_RULES = (
    MATCH_NO_NERIS_TITLE,
    MATCH_TITLE_FILENO,
    MATCH_TITLE_DATE,
    MATCH_UNIQUE_TITLE,
    MATCH_AMAC_INTERNAL_TITLE_DATE,
    MATCH_AMBIGUOUS_TITLE,
)

EFFECTIVENESS_RULES = (
    EFFECT_EXPLICIT_HISTORICAL,
    EFFECT_EXPLICIT_CURRENT,
    EFFECT_SUPERSEDED_BY_CATALOG,
    EFFECT_INSUFFICIENT_EVIDENCE,
    EFFECT_MANUAL_OVERRIDE,
    EFFECT_REFERENCE_MATERIAL,
    EFFECT_PENDING,
    EFFECT_INEFFECTIVE_DATE,
    EFFECT_UNVERIFIED_OFFICIAL_RULE,
    EFFECT_TRIAL_COMMENCED,
)

RELATION_RULES = (
    TRIAL_REPLACEMENT,
    RELATION_OFFICIAL_SUCCESSOR,
    RELATION_AMAC_PAGE_ATTACHMENT,
    RELATION_NERIS_TITLE_QUOTED_DOCUMENT,
    RELATION_DRAFT_FINALIZED,
    RELATION_SAME_INSTRUMENT_COPY,
    RELATION_EXPLICIT_SUCCESSOR,
)

REVIEW_RULES = (
    REVIEW_SOURCE_MATCH_AMBIGUOUS,
    REVIEW_SOURCE_MATCH_LOW_CONFIDENCE,
    REVIEW_EFFECTIVENESS_UNKNOWN,
)

ALL_CATALOG_RULES = (
    *AMAC_CLASSIFICATION_RULES,
    *MATERIAL_CLASSIFICATION_RULES,
    *MATCHING_RULES,
    *EFFECTIVENESS_RULES,
    *RELATION_RULES,
    *REVIEW_RULES,
)

RULES_BY_ID = {rule.rule_id: rule for rule in ALL_CATALOG_RULES}

RULE_WORDS = ("办法", "规则", "指引", "准则", "细则", "规定", "指南", "规范", "标准", "模板")
REFERENCE_TYPES = ("publication_notice", "regulatory_practice", "supporting_material")
OFFICIAL_RULE_TYPES = ("regulation", "self_regulatory_rule")
HISTORICAL_STATUSES = ("已失效", "失效", "已废止", "废止", "已被修改", "被修改")
COMMENT_DRAFT_PATTERNS = ("征求意见稿", "公开征求意见", "征求意见的通知", "征求意见通知", "草案")
REFERENCE_TITLE_PATTERNS = (
    "参考模板",
    "修订说明",
    "起草说明",
    "填写说明",
    "说明材料",
    "问题解答",
    "业务问答",
    "解读",
    "培训",
)


def classify_amac_document(title: str, url: str) -> tuple[str, CatalogRule]:
    if title.startswith("关于发布") or title.endswith(("公告", "通知")):
        return "publication_notice", AMAC_PUBLICATION_NOTICE
    if "登记备案动态" in title or "/dbdt/" in url:
        return "regulatory_practice", AMAC_REGULATORY_PRACTICE
    if any(word in title for word in RULE_WORDS):
        return "self_regulatory_rule", AMAC_SELF_REGULATORY_RULE
    return "supporting_material", AMAC_SUPPORTING_MATERIAL


def catalog_rules_manifest() -> list[dict[str, Any]]:
    return [rule.as_dict() for rule in ALL_CATALOG_RULES]
