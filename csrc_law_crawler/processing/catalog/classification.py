"""Auditable material-nature and effectiveness classification."""

from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from catalog_rules import (
    COMMENT_DRAFT_PATTERNS,
    EFFECT_EXPLICIT_CURRENT,
    EFFECT_EXPLICIT_HISTORICAL,
    EFFECT_INEFFECTIVE_DATE,
    EFFECT_INSUFFICIENT_EVIDENCE,
    EFFECT_MANUAL_OVERRIDE,
    EFFECT_PENDING,
    EFFECT_REFERENCE_MATERIAL,
    EFFECT_SUPERSEDED_BY_CATALOG,
    EFFECT_UNVERIFIED_OFFICIAL_RULE,
    EFFECT_TRIAL_COMMENCED,
    HISTORICAL_STATUSES,
    MATERIAL_AMAC_ENFORCEMENT_REFERENCE,
    MATERIAL_AMAC_INDUSTRY_REFERENCE,
    MATERIAL_AMAC_RULE_HEURISTIC,
    MATERIAL_CONSULTATION_TITLE,
    MATERIAL_INSUFFICIENT_EVIDENCE,
    MATERIAL_MANUAL_OVERRIDE,
    MATERIAL_PUBLISHING_WRAPPER,
    MATERIAL_REFERENCE_TITLE,
    MATERIAL_SOURCE_REFERENCE_LANE,
    MATERIAL_SOURCE_RULE_LANE,
    OFFICIAL_RULE_TYPES,
    REFERENCE_TYPES,
    CatalogRule,
)
from .identity import PUBLISHING_TITLE_RE, is_draft_title, is_trial_title

MATERIAL_LANES = {"rule", "reference", "unknown"}
MATERIAL_CATEGORIES = {
    "law_regulation",
    "normative_document",
    "self_regulatory_rule",
    "business_rule",
    "publication_consultation",
    "interpretation_qa",
    "template_guidance",
    "research_statistics",
    "enforcement_reference",
    "other_reference",
    "unknown",
}
RULE_CATEGORIES = {
    "law_regulation",
    "normative_document",
    "self_regulatory_rule",
    "business_rule",
}
REFERENCE_CATEGORIES = MATERIAL_CATEGORIES - RULE_CATEGORIES - {"unknown"}
EFFECTIVENESS_STATUSES = {
    "current",
    "pending",
    "historical",
    "unknown",
    "not_applicable",
}
OVERRIDES_PATH = Path(__file__).with_name("classification_overrides.json")

INDUSTRY_CATEGORIES = {
    "industry_voice",
    "industry_research_report",
    "industry_esg_research",
}
ENFORCEMENT_CATEGORIES = {
    "disciplinary_person",
    "disciplinary_institution",
    "abnormal_operation",
    "missing_institution",
    "self_regulatory_measure",
}
INTERPRETATION_PATTERNS = (
    "问题解答",
    "业务问答",
    "答记者问",
    "解读",
)
TEMPLATE_PATTERNS = (
    "参考模板",
    "修订说明",
    "起草说明",
    "填写说明",
    "说明材料",
    "会议纪要",
    "培训",
)
RESEARCH_PATTERNS = (
    "研究报告",
    "调查报告",
    "调查分析报告",
    "统计分析报告",
    "行业发展报告",
)
TITLE_HISTORICAL_PATTERNS = (
    "【已废止】",
    "〖已废止〗",
    "（已废止）",
    "(已废止)",
    "【失效】",
    "〖失效〗",
    "（失效）",
    "(失效)",
    "已失效",
)
ENFORCEMENT_CLASSIFICATION_CATEGORIES = {
    "penalties",
    "self_regulatory_measure",
    "abnormal_operation",
    "missing_institution",
    "other_enforcement",
}
ENFORCEMENT_SUBTYPES = {
    "disciplinary_decision",
    "disciplinary_prior_notice",
    "disciplinary_review_decision",
    "other",
}
DISCIPLINARY_RULE_TOKENS = (
    "办法",
    "规则",
    "细则",
    "程序",
    "规程",
    "指引",
    "准则",
    "规定",
    "实施制度",
)
WEB_CATEGORY_PROVENANCE = {
    "page_breadcrumb",
    "api_channel",
    "endpoint_profile",
    "url_inference",
}
PAGE_ROLES = {
    "normative_instrument",
    "case_document",
    "publication_wrapper",
    "supporting_material",
    "unknown",
}


def is_disciplinary_rule_title(value: Any) -> bool:
    title = str(value or "")
    return "纪律处分" in title and any(token in title for token in DISCIPLINARY_RULE_TOKENS)


def disciplinary_penalty_subtype(value: Any) -> str | None:
    title = str(value or "")
    if "纪律处分" not in title or is_disciplinary_rule_title(title):
        return None
    if "复核决定" in title:
        return "disciplinary_review_decision"
    if "事先告知" in title:
        return "disciplinary_prior_notice"
    if "决定" in title:
        return "disciplinary_decision"
    return "other"


def source_web_classification(
    metadata: dict[str, Any],
    *,
    page_url: str | None,
    material_lane: str | None,
    endpoint_profiles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Normalize native page taxonomy while retaining the evidence tier."""
    existing_path = metadata.get("web_category_path")
    if isinstance(existing_path, str):
        existing_path = [part for part in existing_path.split("/") if part]
    path = list(existing_path or [])
    leaf = str(metadata.get("web_category_leaf") or "").strip()
    provenance = str(metadata.get("web_category_provenance") or "").strip()
    source_section = str(metadata.get("source_section") or "").strip()
    source_category = str(metadata.get("source_category") or "").strip()
    channel = str(metadata.get("channel_name") or metadata.get("channel") or "").strip()
    url = str(page_url or "")

    if not leaf and source_section and not source_section.startswith(("http://", "https://")):
        leaf, path, provenance = source_section, [source_section], "page_breadcrumb"
    if not leaf and channel:
        leaf, path, provenance = channel, [channel], "api_channel"
    category_provenance = str(metadata.get("source_category_provenance") or "")
    if not leaf and source_category and category_provenance != "endpoint_profile":
        leaf, path, provenance = source_category, [source_category], "api_channel"
    if not leaf:
        profile_label = next(
            (
                str(profile.get("channel_name") or profile.get("material_nature") or "")
                for profile in endpoint_profiles or []
                if profile.get("channel_name") or profile.get("material_nature")
            ),
            "",
        )
        if profile_label:
            leaf, path, provenance = profile_label, [profile_label], "endpoint_profile"
    if not leaf:
        url_labels = (
            ("/zlgl/jlcf/scfry/", "disciplinary_person"),
            ("/zlgl/jlcf/scfjg/", "disciplinary_institution"),
            ("/zlgl/zlcs/", "self_regulatory_measure"),
            ("/hyyj/", "industry_research"),
        )
        leaf = next((label for token, label in url_labels if token in url), "")
        if leaf:
            path, provenance = [leaf], "url_inference"

    title = str(metadata.get("name") or metadata.get("title") or "")
    penalty_subtype = disciplinary_penalty_subtype(title)
    if material_lane == "case" or penalty_subtype or leaf in ENFORCEMENT_CATEGORIES:
        page_role = "case_document"
    elif PUBLISHING_TITLE_RE.search(title):
        page_role = "publication_wrapper"
    elif material_lane == "rule":
        page_role = "normative_instrument"
    elif material_lane == "reference":
        page_role = "supporting_material"
    else:
        page_role = "unknown"
    return {
        "web_category_leaf": leaf or None,
        "web_category_path": path,
        "web_category_provenance": provenance or None,
        "page_role": page_role,
    }


def enforcement_classification_for(
    entity: dict[str, Any],
    *,
    material_classification: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    material = material_classification or {}
    if material.get("lane") == "rule":
        return None
    metadata = entity.get("metadata") or {}
    title = _title_text(entity)
    subtype = disciplinary_penalty_subtype(title)
    sources = entity.get("sources") or []
    categories = {
        str(value)
        for value in [
            metadata.get("source_category"),
            *(metadata.get("source_categories") or []),
            *(source.get("web_category_leaf") for source in sources),
        ]
        if value
    }
    urls = _source_urls(entity)
    url_penalty = any(token in url for url in urls for token in ("/jlcf/scfry/", "/jlcf/scfjg/"))
    disciplinary_section = bool(
        categories & {"disciplinary_person", "disciplinary_institution"} or url_penalty
    )
    generic_prior_notice = "事先告知书" in title and disciplinary_section
    if subtype or generic_prior_notice:
        category = "penalties"
        subtype = subtype or "other"
        basis = "disciplinary_document"
        confidence = 1.0 if disciplinary_penalty_subtype(title) else 0.95
    elif "abnormal_operation" in categories:
        category, subtype, basis, confidence = "abnormal_operation", "other", "web_category", 1.0
    elif "missing_institution" in categories:
        category, subtype, basis, confidence = "missing_institution", "other", "web_category", 1.0
    elif "self_regulatory_measure" in categories:
        category, subtype, basis, confidence = (
            "self_regulatory_measure",
            "other",
            "web_category",
            1.0,
        )
    elif material.get("category") == "enforcement_reference" or disciplinary_section:
        category, subtype, basis, confidence = (
            "other_enforcement",
            "other",
            "enforcement_reference",
            0.8,
        )
    else:
        return None
    return {
        "category": category,
        "subtype": subtype,
        "basis": basis,
        "confidence": confidence,
        "evidence": {
            "title": str(entity.get("title") or ""),
            "source_categories": sorted(categories),
            "source_urls": sorted(urls),
        },
    }


def reference_lifecycle_for(
    entity: dict[str, Any],
    *,
    material_classification: dict[str, Any],
    finalized_by: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if material_classification.get("lane") != "reference" or not is_draft_title(
        _title_text(entity)
    ):
        return {
            "status": "not_applicable",
            "basis": "not_consultation_draft",
            "confidence": 1.0,
            "evidence": {},
        }
    title = _title_text(entity)
    if any(token in title for token in ("撤回", "终止征求意见")):
        return {
            "status": "withdrawn",
            "basis": "explicit_withdrawal",
            "confidence": 1.0,
            "evidence": {"title": title},
        }
    finalized_by = finalized_by or []
    if finalized_by:
        return {
            "status": "finalized",
            "basis": "finalizes_draft_relation",
            "confidence": max(float(item.get("confidence") or 0) for item in finalized_by),
            "evidence": {"finalized_by": finalized_by},
        }
    return {
        "status": "unfinalized",
        "basis": "no_strict_formal_relation",
        "confidence": 0.9,
        "evidence": {"title": title},
    }


def china_as_of() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def load_classification_overrides(path: Path | None = None) -> dict[str, dict[str, Any]]:
    source = path or OVERRIDES_PATH
    if not source.exists():
        return {}
    payload = json.loads(source.read_text(encoding="utf-8"))
    result: dict[str, dict[str, Any]] = {}
    required = {
        "canonical_id",
        "material_lane",
        "material_category",
        "effectiveness",
        "verified_at",
        "evidence_url",
        "note",
    }
    for index, item in enumerate(payload.get("items") or []):
        missing = sorted(required - set(item))
        if missing:
            raise ValueError(f"classification override[{index}] missing: {', '.join(missing)}")
        canonical_id = str(item["canonical_id"])
        lane = str(item["material_lane"])
        category = str(item["material_category"])
        effectiveness = str(item["effectiveness"])
        if lane not in MATERIAL_LANES:
            raise ValueError(f"classification override {canonical_id}: invalid material_lane")
        if category not in MATERIAL_CATEGORIES:
            raise ValueError(f"classification override {canonical_id}: invalid material_category")
        if effectiveness not in EFFECTIVENESS_STATUSES:
            raise ValueError(f"classification override {canonical_id}: invalid effectiveness")
        if _parse_date(item["verified_at"]) is None:
            raise ValueError(f"classification override {canonical_id}: invalid verified_at")
        if not str(item["evidence_url"]).startswith(("https://", "http://")):
            raise ValueError(f"classification override {canonical_id}: invalid evidence_url")
        if not str(item["note"]).strip():
            raise ValueError(f"classification override {canonical_id}: missing note")
        if not canonical_id.startswith("law_"):
            raise ValueError(f"classification override {canonical_id}: invalid canonical_id")
        if lane == "rule" and category not in RULE_CATEGORIES:
            raise ValueError(f"classification override {canonical_id}: invalid rule category")
        if lane == "reference" and category not in REFERENCE_CATEGORIES:
            raise ValueError(f"classification override {canonical_id}: invalid reference category")
        if lane == "unknown" and category != "unknown":
            raise ValueError(f"classification override {canonical_id}: unknown category required")
        if lane == "reference" and effectiveness != "not_applicable":
            raise ValueError(
                f"classification override {canonical_id}: reference must be not_applicable"
            )
        if lane == "unknown" and effectiveness != "unknown":
            raise ValueError(
                f"classification override {canonical_id}: unknown lane must have unknown effectiveness"
            )
        if lane == "rule" and effectiveness == "not_applicable":
            raise ValueError(
                f"classification override {canonical_id}: rule cannot be not_applicable"
            )
        if canonical_id in result:
            raise ValueError(f"duplicate classification override: {canonical_id}")
        result[canonical_id] = dict(item)
    return result


def _title_text(entity: dict[str, Any]) -> str:
    metadata = entity.get("metadata") or {}
    return "\n".join((str(entity.get("title") or ""), str(metadata.get("name") or "")))


def _source_systems(entity: dict[str, Any]) -> set[str]:
    systems = {
        str(source.get("system")) for source in entity.get("sources") or [] if source.get("system")
    }
    preferred = (entity.get("preferred_content") or {}).get("source_system")
    if preferred:
        systems.add(str(preferred))
    return systems


def _source_lanes(entity: dict[str, Any]) -> set[str]:
    lanes = {
        str(source.get("material_lane"))
        for source in entity.get("sources") or []
        if source.get("material_lane") in {"rule", "reference"}
    }
    if entity.get("material_lane") in {"rule", "reference"}:
        lanes.add(str(entity["material_lane"]))
    return lanes


def _source_urls(entity: dict[str, Any]) -> set[str]:
    return {
        str(source.get("page_url"))
        for source in entity.get("sources") or []
        if source.get("page_url")
    }


def _source_web_evidence(entity: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            key: source.get(key)
            for key in (
                "system",
                "record_id",
                "web_category_leaf",
                "web_category_path",
                "web_category_provenance",
                "page_role",
            )
            if source.get(key) not in (None, "", [])
        }
        for source in entity.get("sources") or []
        if any(
            source.get(key)
            for key in (
                "web_category_leaf",
                "web_category_path",
                "web_category_provenance",
                "page_role",
            )
        )
    ]


def _reference_category(document_type: str, title: str) -> str:
    if any(pattern in title for pattern in COMMENT_DRAFT_PATTERNS):
        return "publication_consultation"
    if any(pattern in title for pattern in INTERPRETATION_PATTERNS):
        return "interpretation_qa"
    if any(pattern in title for pattern in TEMPLATE_PATTERNS):
        return "template_guidance"
    if title.startswith("《声音》") or any(pattern in title for pattern in RESEARCH_PATTERNS):
        return "research_statistics"
    if "解读" in document_type or "问答" in document_type:
        return "interpretation_qa"
    if any(token in document_type for token in ("模板", "办事", "许可")):
        return "template_guidance"
    if any(token in document_type for token in ("统计", "研究")):
        return "research_statistics"
    if any(token in document_type for token in ("征求意见", "通知公告", "政府公报")):
        return "publication_consultation"
    if document_type == "publication_notice":
        return "publication_consultation"
    return "other_reference"


def _rule_category(document_type: str, systems: set[str]) -> str:
    if document_type == "self_regulatory_rule" or "自律" in document_type:
        return "self_regulatory_rule"
    if any(token in document_type for token in ("交易所", "登记结算", "业务规则")) or systems & {
        "sse_com_cn",
        "szse_cn",
        "chinaclear_cn",
    }:
        return "business_rule"
    if document_type == "regulation":
        return "law_regulation"
    return "normative_document"


def _classification(
    *,
    lane: str,
    category: str,
    basis: str,
    rule: CatalogRule,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "lane": lane,
        "category": category,
        "basis": basis,
        "rule_id": rule.rule_id,
        "confidence": rule.confidence,
        "evidence": evidence,
    }


def material_classification_for(
    entity: dict[str, Any],
    *,
    publishes: list[str] | None = None,
    override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = entity.get("metadata") or {}
    title = _title_text(entity)
    document_type = str(entity.get("document_type") or metadata.get("document_type") or "")
    raw_status = str(entity.get("status") or metadata.get("status") or "unknown")
    systems = _source_systems(entity)
    source_urls = _source_urls(entity)
    lanes = _source_lanes(entity)
    source_categories = {
        str(value)
        for value in [
            metadata.get("source_category"),
            *(metadata.get("source_categories") or []),
            *(source.get("web_category_leaf") for source in entity.get("sources") or []),
        ]
        if value
    }
    publishes = sorted(
        {str(value) for value in publishes or [] if value and str(value) != entity.get("id")}
    )
    evidence = {
        "document_type": document_type,
        "raw_status": raw_status,
        "source_systems": sorted(systems),
        "source_urls": sorted(source_urls),
        "source_lanes": sorted(lanes),
        "source_categories": sorted(source_categories),
        "source_section": metadata.get("source_section"),
        "publishes": publishes,
        "web_categories": _source_web_evidence(entity),
    }

    if override:
        return _classification(
            lane=str(override["material_lane"]),
            category=str(override["material_category"]),
            basis="manual_override",
            rule=MATERIAL_MANUAL_OVERRIDE,
            evidence={
                **evidence,
                "verified_at": override["verified_at"],
                "evidence_url": override["evidence_url"],
                "note": override["note"],
            },
        )
    if source_categories & INDUSTRY_CATEGORIES or (
        "amac" in systems and any("/hyyj/" in url for url in source_urls)
    ):
        return _classification(
            lane="reference",
            category="research_statistics",
            basis="amac_industry_reference",
            rule=MATERIAL_AMAC_INDUSTRY_REFERENCE,
            evidence=evidence,
        )
    penalty_subtype = disciplinary_penalty_subtype(title)
    enforcement_url = any(
        token in url for url in source_urls for token in ("/jlcf/scfry/", "/jlcf/scfjg/")
    )
    if (
        penalty_subtype or enforcement_url or source_categories & ENFORCEMENT_CATEGORIES
    ) and not is_disciplinary_rule_title(title):
        return _classification(
            lane="reference",
            category="enforcement_reference",
            basis="amac_enforcement_reference",
            rule=MATERIAL_AMAC_ENFORCEMENT_REFERENCE,
            evidence={**evidence, "disciplinary_subtype": penalty_subtype},
        )
    if any(pattern in title for pattern in COMMENT_DRAFT_PATTERNS):
        return _classification(
            lane="reference",
            category="publication_consultation",
            basis="consultation_title",
            rule=MATERIAL_CONSULTATION_TITLE,
            evidence=evidence,
        )
    # Only title/section metadata is a strong reference signal here.  A broad
    # source type such as ``publication_notice`` can still contain the actual
    # reusable rule and must not win before source lane or official status.
    reference_category = _reference_category("", title)
    if reference_category != "other_reference":
        return _classification(
            lane="reference",
            category=reference_category,
            basis="reference_title",
            rule=MATERIAL_REFERENCE_TITLE,
            evidence=evidence,
        )
    if publishes:
        return _classification(
            lane="reference",
            category="publication_consultation",
            basis="publishing_wrapper",
            rule=MATERIAL_PUBLISHING_WRAPPER,
            evidence=evidence,
        )
    if "rule" in lanes:
        return _classification(
            lane="rule",
            category=_rule_category(document_type, systems),
            basis="source_rule_lane",
            rule=MATERIAL_SOURCE_RULE_LANE,
            evidence={**evidence, "source_lane_conflict": "reference" in lanes},
        )
    if lanes == {"reference"}:
        return _classification(
            lane="reference",
            category=_reference_category(document_type, title),
            basis="source_reference_lane",
            rule=MATERIAL_SOURCE_REFERENCE_LANE,
            evidence=evidence,
        )
    if raw_status in {*HISTORICAL_STATUSES, "现行有效", "已颁布未施行"} or "neris" in systems:
        return _classification(
            lane="rule",
            category=_rule_category(document_type, systems),
            basis="official_rule_catalog",
            rule=MATERIAL_SOURCE_RULE_LANE,
            evidence=evidence,
        )
    if document_type in OFFICIAL_RULE_TYPES:
        return _classification(
            lane="rule",
            category=_rule_category(document_type, systems),
            basis="amac_rule_heuristic",
            rule=MATERIAL_AMAC_RULE_HEURISTIC,
            evidence=evidence,
        )
    if document_type in REFERENCE_TYPES:
        return _classification(
            lane="reference",
            category=_reference_category(document_type, title),
            basis="reference_document_type",
            rule=MATERIAL_SOURCE_REFERENCE_LANE,
            evidence=evidence,
        )
    return _classification(
        lane="unknown",
        category="unknown",
        basis="insufficient_evidence",
        rule=MATERIAL_INSUFFICIENT_EVIDENCE,
        evidence=evidence,
    )


def _effectiveness_result(
    *,
    status: str,
    raw_status: str,
    label: str,
    basis: str,
    rule: CatalogRule,
    source: str | None,
    as_of: str,
    confidence: float | None = None,
    superseded_by: list[dict[str, Any]] | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "status": status,
        "raw_status": raw_status,
        "label": label,
        "basis": basis,
        "rule_id": rule.rule_id,
        "source": source,
        "confidence": rule.confidence if confidence is None else confidence,
        "as_of": as_of,
    }
    if superseded_by:
        result["superseded_by"] = superseded_by
    if evidence:
        result["evidence"] = evidence
    return result


def effectiveness_for(
    entity: dict[str, Any],
    *,
    material_classification: dict[str, Any] | None = None,
    superseded_by: list[dict[str, Any]] | None = None,
    as_of: str | None = None,
    override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = entity.get("metadata") or {}
    raw_status = str(entity.get("status") or metadata.get("status") or "unknown")
    document_type = str(entity.get("document_type") or metadata.get("document_type") or "")
    source = (entity.get("preferred_content") or {}).get("source_system")
    if not source:
        source = next(
            (item.get("system") for item in entity.get("sources") or [] if item.get("system")),
            None,
        )
    superseded_by = superseded_by or []
    as_of = as_of or china_as_of()
    as_of_date = _parse_date(as_of)
    if as_of_date is None:
        raise ValueError(f"invalid classification as_of date: {as_of}")
    active_superseded_by = [
        item
        for item in superseded_by
        if _parse_date(item.get("effective_date")) is None
        or _parse_date(item.get("effective_date")) <= as_of_date
    ]
    material = material_classification or material_classification_for(entity, override=override)

    if override:
        return _effectiveness_result(
            status=str(override["effectiveness"]),
            raw_status=raw_status,
            label="人工核验",
            basis="manual_override",
            rule=EFFECT_MANUAL_OVERRIDE,
            source=source,
            as_of=as_of,
            evidence={
                "verified_at": override["verified_at"],
                "evidence_url": override["evidence_url"],
                "note": override["note"],
            },
        )
    if material.get("lane") == "reference":
        return _effectiveness_result(
            status="not_applicable",
            raw_status=raw_status,
            label="参考材料，不适用制度效力",
            basis="reference_material",
            rule=EFFECT_REFERENCE_MATERIAL,
            source=source,
            as_of=as_of,
        )
    if material.get("lane") != "rule":
        return _effectiveness_result(
            status="unknown",
            raw_status=raw_status,
            label="材料性质待核验",
            basis="insufficient_evidence",
            rule=EFFECT_INSUFFICIENT_EVIDENCE,
            source=source,
            as_of=as_of,
        )

    ineffective_date = _parse_date(metadata.get("ineffective_date"))
    effective_date = _parse_date(metadata.get("effective_date"))
    title = _title_text(entity)
    if ineffective_date and ineffective_date <= as_of_date:
        return _effectiveness_result(
            status="historical",
            raw_status=raw_status,
            label="已到失效日期",
            basis="ineffective_date",
            rule=EFFECT_INEFFECTIVE_DATE,
            source=source,
            as_of=as_of,
        )
    if raw_status in HISTORICAL_STATUSES or any(
        pattern in title for pattern in TITLE_HISTORICAL_PATTERNS
    ):
        return _effectiveness_result(
            status="historical",
            raw_status=raw_status,
            label=raw_status if raw_status in HISTORICAL_STATUSES else "已废止/失效（标题标注）",
            basis="explicit_historical_status",
            rule=EFFECT_EXPLICIT_HISTORICAL,
            source=source,
            as_of=as_of,
        )
    if active_superseded_by:
        confidence = max(
            float(item.get("confidence") or 0.0) for item in active_superseded_by
        )
        return _effectiveness_result(
            status="historical",
            raw_status=raw_status,
            label="已被替代",
            basis="superseded_by_catalog_relation",
            rule=EFFECT_SUPERSEDED_BY_CATALOG,
            source=source,
            as_of=as_of,
            confidence=confidence,
            superseded_by=active_superseded_by,
        )
    if raw_status == "已颁布未施行" or (effective_date and effective_date > as_of_date):
        return _effectiveness_result(
            status="pending",
            raw_status=raw_status,
            label="已颁布未施行",
            basis="pending",
            rule=EFFECT_PENDING,
            source=source,
            as_of=as_of,
        )
    if raw_status == "现行有效" and (not is_trial_title(title) or effective_date is not None):
        return _effectiveness_result(
            status="current",
            raw_status=raw_status,
            label=raw_status,
            basis="explicit_current_status",
            rule=EFFECT_EXPLICIT_CURRENT,
            source=source,
            as_of=as_of,
        )
    if is_trial_title(title) and effective_date and effective_date <= as_of_date:
        sources = entity.get("sources") or []
        official_formal = any(
            source.get("material_lane") == "rule"
            or source.get("page_role") == "normative_instrument"
            for source in sources
        ) or source in {"neris", "amac"}
        if official_formal:
            return _effectiveness_result(
                status="current",
                raw_status=raw_status,
                label="试行制度已到施行日",
                basis="trial_official_commenced",
                rule=EFFECT_TRIAL_COMMENCED,
                source=source,
                as_of=as_of,
                evidence={"effective_date": effective_date.isoformat()},
            )
    if source == "amac" or document_type in OFFICIAL_RULE_TYPES:
        return _effectiveness_result(
            status="unknown",
            raw_status=raw_status,
            label="未发现失效证据，效力待核验",
            basis="unverified_official_rule",
            rule=EFFECT_UNVERIFIED_OFFICIAL_RULE,
            source=source,
            as_of=as_of,
        )
    return _effectiveness_result(
        status="unknown",
        raw_status=raw_status,
        label="效力待核验",
        basis="insufficient_evidence",
        rule=EFFECT_INSUFFICIENT_EVIDENCE,
        source=source,
        as_of=as_of,
    )


__all__ = [
    "ENFORCEMENT_CLASSIFICATION_CATEGORIES",
    "ENFORCEMENT_SUBTYPES",
    "EFFECTIVENESS_STATUSES",
    "MATERIAL_CATEGORIES",
    "MATERIAL_LANES",
    "china_as_of",
    "effectiveness_for",
    "disciplinary_penalty_subtype",
    "enforcement_classification_for",
    "is_disciplinary_rule_title",
    "load_classification_overrides",
    "material_classification_for",
    "reference_lifecycle_for",
    "source_web_classification",
]
