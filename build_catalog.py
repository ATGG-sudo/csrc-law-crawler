#!/usr/bin/env python3
"""Match NERIS and AMAC source records and build source-independent law entities."""

from __future__ import annotations

import argparse
import json
import hashlib
import html
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from asset_text import extract_local_asset_text
from catalog_rules import (
    MATCH_AMAC_INTERNAL_TITLE_DATE,
    MATCH_AMBIGUOUS_TITLE,
    MATCH_NO_NERIS_TITLE,
    MATCH_TITLE_DATE,
    MATCH_TITLE_FILENO,
    MATCH_UNIQUE_TITLE,
    OFFICIAL_RULE_TYPES,
    REVIEW_EFFECTIVENESS_UNKNOWN,
    REVIEW_SOURCE_MATCH_AMBIGUOUS,
    REVIEW_SOURCE_MATCH_LOW_CONFIDENCE,
    RELATION_OFFICIAL_SUCCESSOR,
    RELATION_AMAC_PAGE_ATTACHMENT,
    RELATION_NERIS_TITLE_QUOTED_DOCUMENT,
    SOURCE_MATCH_REVIEW_CONFIDENCE_THRESHOLD,
    TRIAL_REPLACEMENT,
    catalog_rule_calibration,
    catalog_rules_manifest,
)
from catalog_services import (
    CatalogEntityWriter,
    CatalogMatcher,
    CatalogRelationIngestor,
    CatalogSourceLoader,
)
from parser import repair_known_neris_mojibake
from runtime import log_event
from storage import (
    attachment_index_path,
    catalog_dir,
    catalog_laws_dir,
    catalog_relations_path,
    iter_amac_source_files,
    iter_reg_law_files,
    load_json,
    output_path,
    relative_to_output,
    run_with_output_lock,
    save_json,
    source_matches_path,
    reports_dir,
    utc_now_iso,
)

QUOTED_TITLE_RE = re.compile(r"《([^》]{4,120})》")
PUBLISHING_TITLE_RE = re.compile(r"^(?:关于)?(?:发布|印发|公布|修订并发布)")
SPACE_PUNCT_RE = re.compile(r"[\s\u3000·•,，。；;:：()（）\[\]【】《》“”\"'、—\-]+")
ATTACHMENT_PREFIX_RE = re.compile(r"^附件(?:\s*\d+(?:-\d+)?)?\s*[：:、.\-]?\s*")
FILE_SUFFIX_RE = re.compile(r"\.(pdf|docx?|xlsx?|zip|rar|rtf|wps)$", re.I)
TRIAL_MARKER_RE = re.compile(r"[（(]\s*试行\s*[）)]|试行")
REVISION_MARKER_RE = re.compile(r"[（(]\s*(?:\d{4}年)?修订\s*[）)]")
LEADING_ITEM_MARKER_RE = re.compile(r"^\s*\d+(?:[-.、．]\d+)?[-.、．]?\s*")
ATTACHMENT_TEXT_SIGNAL_RE = re.compile(
    r"(?:详[细情]?见附件|详情请(?:查看)?附件|全文详见附件|见附件|附件下载|相关文档)"
)
SECTION_TOKEN_RE = re.compile(r"第[一二三四五六七八九十百千零〇0-9]+[条章节编款项部分]")
DEDUP_MIN_BODY_CHARS = 80

KNOWN_SUCCESSOR_CHAINS: tuple[dict[str, Any], ...] = (
    {
        "source": "official.sse.main_listing_rules",
        "official_url": "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/mainipo/c/c_20260424_10816589.shtml",
        "items": (
            {"title": "上海证券交易所股票上市规则", "fileno": "上证发〔2014〕65号"},
            {
                "title": "上海证券交易所股票上市规则（2018年11月修订）",
                "fileno": "上证发〔2018〕97号",
            },
            {"title": "上海证券交易所股票上市规则（2019年修订）", "fileno": ""},
            {
                "title": "上海证券交易所股票上市规则（2020年12月修订）",
                "fileno": "上证发〔2020〕100号",
            },
            {"title": "上海证券交易所股票上市规则（2022年1月修订）", "fileno": "上证发〔2022〕1号"},
            {
                "external_id": "external:sse:main-listing-rules-2026",
                "title": "上海证券交易所股票上市规则（2026年4月修订）",
                "fileno": "上证发〔2026〕42号",
            },
        ),
    },
    {
        "source": "official.sse.star_listing_rules",
        "official_url": "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/staripo/c/c_20260424_10816592.shtml",
        "items": (
            {"title": "上海证券交易所科创板股票上市规则", "fileno": "上证发〔2019〕53号"},
            {
                "title": "上海证券交易所科创板股票上市规则（2020年12月修订）",
                "fileno": "上证发〔2020〕101号",
            },
            {
                "external_id": "external:sse:star-listing-rules-2026",
                "title": "上海证券交易所科创板股票上市规则（2026年4月修订）",
                "fileno": "上证发〔2026〕43号",
            },
        ),
    },
    {
        "source": "official.sse.relisting_rules",
        "official_url": "https://www.sse.com.cn/lawandrules/sselawsrules/repeal/rules/c/c_20210531_5478071.shtml",
        "items": (
            {"title": "上海证券交易所退市公司重新上市实施办法", "fileno": "上证发〔2015〕21号"},
            {
                "title": "上海证券交易所退市公司重新上市实施办法（2018年11月修订）",
                "fileno": "上证发〔2018〕99号",
            },
            {
                "external_id": "external:sse:relisting-rules-repealed",
                "title": "上海证券交易所退市公司重新上市实施办法（已失效）",
                "fileno": "",
            },
        ),
    },
    {
        "source": "official.dce.abnormal_trading_rules",
        "official_url": "https://www.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6297862/index.html",
        "items": (
            {
                "title": "大连商品交易所异常交易管理办法（试行）（2018年修订）",
                "fileno": "大商所发〔2018〕111号",
            },
            {
                "external_id": "external:dce:abnormal-trading-behavior-rules-2021",
                "title": "大连商品交易所异常交易行为管理办法",
                "fileno": "〔2021〕61号",
            },
        ),
    },
    {
        "source": "official.dce.abnormal_trading_standards",
        "official_url": "https://www.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/6297862/index.html",
        "items": (
            {
                "title": "《大连商品交易所异常交易管理办法（试行）》有关监管标准及处理程序",
                "fileno": "大商所发〔2018〕154号",
            },
            {
                "title": "《大连商品交易所异常交易管理办法（试行）》有关监管标准及处理程序",
                "fileno": "大商所发〔2021〕73号",
            },
            {
                "external_id": "external:dce:abnormal-trading-behavior-rules-2021",
                "title": "大连商品交易所异常交易行为管理办法",
                "fileno": "〔2021〕61号",
            },
        ),
    },
    {
        "source": "official.dce.hedging_rules",
        "official_url": "https://www.dce.com.cn/dalianshangpin/fgfz/6142914/6142922/6231266/index.html",
        "items": (
            {
                "title": "大连商品交易所套期保值管理办法（2018年4月修订）",
                "fileno": "大商所发〔2018〕154号",
            },
            {"title": "大连商品交易所套期保值管理办法", "fileno": "〔2022〕98号"},
        ),
    },
)


def catalog_manifest_path() -> Path:
    return catalog_dir() / "manifest.json"


def catalog_review_queue_path() -> Path:
    return reports_dir() / "review_queue.json"


def _repair_text_fields(value: Any) -> Any:
    if isinstance(value, str):
        return repair_known_neris_mojibake(value)
    if isinstance(value, dict):
        return {key: _repair_text_fields(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_repair_text_fields(item) for item in value]
    return value


def clean_title(value: Any) -> str:
    text = html.unescape(str(value or "")).strip()
    text = ATTACHMENT_PREFIX_RE.sub("", text)
    text = FILE_SUFFIX_RE.sub("", text)
    return text.replace("&mdash;", "—").strip()


def normalize_title(value: Any) -> str:
    text = clean_title(value)
    return SPACE_PUNCT_RE.sub("", text).lower()


def is_trial_title(value: Any) -> bool:
    return bool(TRIAL_MARKER_RE.search(clean_title(value)))


def normalize_title_without_trial(value: Any) -> str:
    text = TRIAL_MARKER_RE.sub("", clean_title(value))
    return SPACE_PUNCT_RE.sub("", text).lower()


def normalize_fileno(value: Any) -> str:
    return SPACE_PUNCT_RE.sub("", html.unescape(str(value or ""))).lower()


def canonical_id(seed: str) -> str:
    return f"law_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"


def _source_descriptor(
    system: str,
    record_id: str,
    *,
    role: str,
    local_file: str | None,
    page_url: str | None,
) -> dict[str, Any]:
    return {
        "system": system,
        "record_id": record_id,
        "role": role,
        "local_file": local_file,
        "page_url": page_url,
    }


def _record_assets(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(asset) for asset in record.get("assets") or []]


def _neris_records() -> list[dict[str, Any]]:
    records = []
    for path in iter_reg_law_files():
        doc = load_json(path, {})
        metadata = _repair_text_fields(doc.get("metadata") or {})
        record_id = str(metadata.get("id") or path.stem.removeprefix("reg_"))
        attachment_index = load_json(attachment_index_path(record_id), {})
        records.append(
            {
                "system": "neris",
                "record_id": record_id,
                "metadata": metadata,
                "plain_text": repair_known_neris_mojibake(doc.get("full_text") or ""),
                "local_file": relative_to_output(path),
                "page_url": (doc.get("source") or {}).get("detail_url"),
                "assets": (
                    attachment_index.get("attachments") or doc.get("source_attachments") or []
                ),
            }
        )
    return records


def _amac_records() -> list[dict[str, Any]]:
    records = []
    for path in iter_amac_source_files():
        doc = load_json(path, {})
        record_id = str(doc.get("source_record_id") or path.stem)
        records.append(
            {
                "system": "amac",
                "record_id": record_id,
                "metadata": _repair_text_fields(doc.get("metadata") or {}),
                "local_file": relative_to_output(path),
                "page_url": (doc.get("source") or {}).get("page_url"),
                "assets": doc.get("assets") or [],
                "parent_record_id": None,
            }
        )
        records[-1]["plain_text"] = _record_plain_text(
            records[-1]["metadata"],
            (doc.get("content") or {}).get("plain_text"),
            records[-1]["local_file"],
        )
        for attachment in doc.get("attachment_documents") or []:
            attachment_id = str(attachment.get("source_record_id") or "")
            if not attachment_id:
                continue
            attachment_metadata = _repair_text_fields(dict(attachment.get("metadata") or {}))
            parent_metadata = _repair_text_fields(doc.get("metadata") or {})
            if attachment_metadata.get("status") in {None, "", "unknown"}:
                parent_status = parent_metadata.get("status")
                if parent_status not in {None, "", "unknown"}:
                    attachment_metadata["status"] = parent_status
            for field in ("effective_date", "ineffective_date"):
                if not attachment_metadata.get(field) and parent_metadata.get(field):
                    attachment_metadata[field] = parent_metadata.get(field)
            local_file = next(
                (
                    asset.get("local_file")
                    for asset in (doc.get("assets") or [])
                    if asset.get("asset_id") == attachment.get("asset_id")
                ),
                None,
            )
            records.append(
                {
                    "system": "amac",
                    "record_id": attachment_id,
                    "metadata": attachment_metadata,
                    "plain_text": _record_plain_text(
                        attachment_metadata,
                        (attachment.get("content") or {}).get("plain_text"),
                        local_file,
                    ),
                    "local_file": local_file,
                    "page_url": (attachment.get("source") or {}).get("asset_url"),
                    "assets": [],
                    "parent_record_id": record_id,
                }
            )
    return records


def _date_distance(left: Any, right: Any) -> int | None:
    try:
        from datetime import date

        return abs((date.fromisoformat(str(left)[:10]) - date.fromisoformat(str(right)[:10])).days)
    except (TypeError, ValueError):
        return None


def _date_sort_value(value: Any) -> int | None:
    try:
        from datetime import date

        parsed = date.fromisoformat(str(value)[:10])
        return parsed.toordinal()
    except (TypeError, ValueError):
        return None


def _normalized_org(entity: dict[str, Any]) -> str:
    metadata = entity.get("metadata") or {}
    return normalize_title(metadata.get("pub_org"))


def _pub_date_value(entity: dict[str, Any]) -> int | None:
    metadata = entity.get("metadata") or {}
    return _date_sort_value(metadata.get("pub_date"))


def _is_official_rule_entity(entity: dict[str, Any]) -> bool:
    document_type = str(entity.get("document_type") or "")
    return document_type in OFFICIAL_RULE_TYPES


def _asset_text_fallback(metadata: dict[str, Any], local_file: Any) -> str:
    local_file_text = str(local_file or "")
    if not local_file_text:
        return ""
    return extract_local_asset_text(output_path(local_file_text))


def _low_signal_plain_text(metadata: dict[str, Any], text: str) -> bool:
    compact = SPACE_PUNCT_RE.sub("", text)
    if not compact:
        return True
    title = SPACE_PUNCT_RE.sub("", str(metadata.get("name") or ""))
    remainder = compact.replace(title, "") if title else compact
    remainder = ATTACHMENT_TEXT_SIGNAL_RE.sub("", remainder)
    remainder = SECTION_TOKEN_RE.sub("", remainder)
    if title and not remainder:
        return True
    if ATTACHMENT_TEXT_SIGNAL_RE.search(compact) and len(remainder) <= 30:
        return True
    return bool(title and title in compact and len(remainder) <= 8)


def _record_plain_text(
    metadata: dict[str, Any],
    plain_text: Any,
    local_file: Any = None,
) -> str:
    text = repair_known_neris_mojibake(str(plain_text or ""))
    if text.strip() and not _low_signal_plain_text(metadata, text):
        return text
    fallback = _asset_text_fallback(metadata, local_file)
    return fallback if fallback else text


def infer_trial_replacement_relations(
    entities: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Infer later formal rules replacing same-title trial rules."""
    by_trial_key: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for entity_id, entity in entities.items():
        title = entity.get("title")
        key = normalize_title_without_trial(title)
        if key and _is_official_rule_entity(entity):
            by_trial_key[key].append((entity_id, entity))

    relations: list[dict[str, Any]] = []
    for trial_id, trial_entity in entities.items():
        trial_title = str(trial_entity.get("title") or "")
        if not is_trial_title(trial_title) or not _is_official_rule_entity(trial_entity):
            continue
        trial_date = _pub_date_value(trial_entity)
        if trial_date is None:
            continue
        trial_org = _normalized_org(trial_entity)
        candidates: list[tuple[int, str, dict[str, Any]]] = []
        trial_key = normalize_title_without_trial(trial_title)
        for formal_id, formal_entity in by_trial_key.get(trial_key) or []:
            if formal_id == trial_id or is_trial_title(formal_entity.get("title")):
                continue
            formal_date = _pub_date_value(formal_entity)
            if formal_date is None or formal_date <= trial_date:
                continue
            formal_org = _normalized_org(formal_entity)
            if trial_org and formal_org and trial_org != formal_org:
                continue
            candidates.append((formal_date, formal_id, formal_entity))
        if not candidates:
            continue
        formal_date, formal_id, formal_entity = sorted(
            candidates,
            key=lambda item: item[0],
        )[0]
        relations.append(
            {
                "from": formal_id,
                "to": trial_id,
                "relation": "supersedes",
                "source": "catalog.trial_replacement",
                "rule_id": TRIAL_REPLACEMENT.rule_id,
                "evidence": {
                    "rule_id": TRIAL_REPLACEMENT.rule_id,
                    "inference": "later_same_title_formal_rule_replaces_trial_rule",
                    "normalized_title": trial_key,
                    "trial_title": trial_title,
                    "trial_pub_date": (trial_entity.get("metadata") or {}).get("pub_date"),
                    "formal_title": formal_entity.get("title"),
                    "formal_pub_date": (formal_entity.get("metadata") or {}).get("pub_date"),
                },
                "confidence": TRIAL_REPLACEMENT.confidence,
            }
        )
    return relations


def _known_successor_key(item: dict[str, Any]) -> tuple[str, str]:
    return (
        normalize_title(item.get("title") or item.get("name")),
        normalize_fileno(item.get("fileno")),
    )


def infer_known_successor_relations(
    entities: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Infer documented successor chains that are too specific for heuristics."""
    by_key = {}
    for entity_id, entity in entities.items():
        metadata = dict(entity.get("metadata") or {})
        metadata.setdefault("title", entity.get("title"))
        metadata.setdefault("name", entity.get("title"))
        by_key[_known_successor_key(metadata)] = (entity_id, entity)
    relations: list[dict[str, Any]] = []
    for chain in KNOWN_SUCCESSOR_CHAINS:
        items = list(chain["items"])
        for old, new in zip(items, items[1:]):
            old_match = by_key.get(_known_successor_key(old))
            if not old_match:
                continue
            old_id, old_entity = old_match
            new_match = by_key.get(_known_successor_key(new))
            new_id = str(new.get("external_id") or (new_match or ("",))[0])
            if not new_id:
                continue
            relations.append(
                {
                    "from": new_id,
                    "to": old_id,
                    "relation": "supersedes",
                    "source": chain["source"],
                    "rule_id": RELATION_OFFICIAL_SUCCESSOR.rule_id,
                    "evidence": {
                        "rule_id": RELATION_OFFICIAL_SUCCESSOR.rule_id,
                        "official_url": chain["official_url"],
                        "old_title": old_entity.get("title"),
                        "old_fileno": (old_entity.get("metadata") or {}).get("fileno"),
                        "successor_title": new.get("title"),
                        "successor_fileno": new.get("fileno"),
                    },
                    "confidence": RELATION_OFFICIAL_SUCCESSOR.confidence,
                }
            )
    return relations


def choose_neris_match(
    amac: dict[str, Any],
    title_index: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any] | None, str, float, list[str]]:
    match, status, confidence, evidence, _rule_id = choose_neris_match_with_rule(
        amac,
        title_index,
    )
    return match, status, confidence, evidence


def choose_neris_match_with_rule(
    amac: dict[str, Any],
    title_index: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any] | None, str, float, list[str], str]:
    metadata = amac.get("metadata") or {}
    title_key = normalize_title(metadata.get("name"))
    candidates = title_index.get(title_key) or []
    if not candidates:
        rule = MATCH_NO_NERIS_TITLE
        return None, "new_to_neris", rule.confidence, ["NERIS中无同题名记录"], rule.rule_id

    amac_fileno = normalize_fileno(metadata.get("fileno"))
    if amac_fileno:
        fileno_matches = [
            item
            for item in candidates
            if normalize_fileno((item.get("metadata") or {}).get("fileno")) == amac_fileno
        ]
        if len(fileno_matches) == 1:
            rule = MATCH_TITLE_FILENO
            return (
                fileno_matches[0],
                "same_document",
                rule.confidence,
                ["题名和文号一致"],
                rule.rule_id,
            )

    dated = []
    for item in candidates:
        distance = _date_distance(
            metadata.get("pub_date"),
            (item.get("metadata") or {}).get("pub_date"),
        )
        if distance is not None:
            dated.append((distance, item))
    if dated:
        dated.sort(key=lambda pair: pair[0])
        if dated[0][0] <= 3:
            match = dated[0][1]
            status = (
                "supplemental_copy"
                if amac.get("assets") and not match.get("assets")
                else "same_document"
            )
            rule = MATCH_TITLE_DATE
            return match, status, rule.confidence, ["题名一致且发布日期相差不超过3日"], rule.rule_id

    if len(candidates) == 1:
        match = candidates[0]
        status = (
            "supplemental_copy"
            if amac.get("assets") and not match.get("assets")
            else "same_document"
        )
        rule = MATCH_UNIQUE_TITLE
        return match, status, rule.confidence, ["题名唯一一致；日期或文号证据不足"], rule.rule_id
    rule = MATCH_AMBIGUOUS_TITLE
    return None, "ambiguous", rule.confidence, ["NERIS存在多个同题名候选"], rule.rule_id


def _entity_from_record(record: dict[str, Any], entity_id: str) -> dict[str, Any]:
    metadata = dict(record.get("metadata") or {})
    title = clean_title(metadata.get("name"))
    metadata["name"] = title
    text = repair_known_neris_mojibake(str(record.get("plain_text") or ""))
    return {
        "schema_version": 1,
        "id": entity_id,
        "title": title,
        "document_type": metadata.get("document_type") or "regulation",
        "status": metadata.get("status") or "unknown",
        "metadata": metadata,
        "preferred_content": {
            "source_system": record["system"],
            "source_record_id": record["record_id"],
            "plain_text": text,
        },
        "sources": [
            _source_descriptor(
                record["system"],
                record["record_id"],
                role="official_text",
                local_file=record.get("local_file"),
                page_url=record.get("page_url"),
            )
        ],
        "assets": _record_assets(record),
        "updated_at": utc_now_iso(),
    }


def _seed_neris_entities(
    neris_records: list[dict[str, Any]],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[tuple[str, str], str],
    dict[str, list[dict[str, Any]]],
]:
    entities: dict[str, dict[str, Any]] = {}
    source_to_entity: dict[tuple[str, str], str] = {}
    title_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    neris_entity_index: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)

    for record in neris_records:
        title_key = normalize_title((record.get("metadata") or {}).get("name"))
        existing_entity_id = _find_existing_neris_entity(record, neris_entity_index)
        if existing_entity_id:
            entity_id = existing_entity_id
            entity = entities[entity_id]
            _append_record_source(entity, record, role="official_duplicate")
            _append_record_assets(entity, record)
            _prefer_longer_record_content(entity, record)
            _append_neris_merge_metadata(entity, record)
        else:
            entity_id = canonical_id(f"neris:{record['record_id']}")
            entity = _entity_from_record(record, entity_id)
            entities[entity_id] = entity
            neris_key = _neris_merge_index_key(record)
            if neris_key:
                neris_entity_index[neris_key].append((entity_id, record))
            title_index[title_key].append(record)
        source_to_entity[("neris", record["record_id"])] = entity_id
    return entities, source_to_entity, title_index


def _neris_merge_index_key(record: dict[str, Any]) -> str | None:
    metadata = record.get("metadata") or {}
    title_key = normalize_title(metadata.get("name"))
    fileno_key = normalize_fileno(metadata.get("fileno"))
    if not title_key or not fileno_key:
        return None
    return f"{title_key}\0{fileno_key}"


def _dates_match_for_merge(left: Any, right: Any) -> bool:
    distance = _date_distance(left, right)
    if distance is not None:
        return distance <= 3
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    return bool(left_text) and left_text == right_text


def _find_existing_neris_entity(
    record: dict[str, Any],
    neris_entity_index: dict[str, list[tuple[str, dict[str, Any]]]],
) -> str | None:
    key = _neris_merge_index_key(record)
    if not key:
        return None
    metadata = record.get("metadata") or {}
    for entity_id, candidate in neris_entity_index.get(key) or []:
        candidate_metadata = candidate.get("metadata") or {}
        if _dates_match_for_merge(
            metadata.get("pub_date"),
            candidate_metadata.get("pub_date"),
        ):
            return entity_id
    return None


def _neris_merge_source_metadata(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata") or {}
    return {
        "record_id": record.get("record_id"),
        "number": metadata.get("number"),
        "status": metadata.get("status"),
        "pub_org": metadata.get("pub_org"),
        "pub_date": metadata.get("pub_date"),
        "version": metadata.get("version"),
    }


def _append_neris_merge_metadata(
    entity: dict[str, Any],
    record: dict[str, Any],
) -> None:
    metadata = entity.setdefault("metadata", {})
    merged_sources = metadata.setdefault("merged_neris_sources", [])
    if not merged_sources:
        merged_sources.append(
            {
                "record_id": metadata.get("id"),
                "number": metadata.get("number"),
                "status": metadata.get("status"),
                "pub_org": metadata.get("pub_org"),
                "pub_date": metadata.get("pub_date"),
                "version": metadata.get("version"),
            }
        )
    source_metadata = _neris_merge_source_metadata(record)
    if source_metadata not in merged_sources:
        merged_sources.append(source_metadata)


def _append_record_source(
    entity: dict[str, Any],
    record: dict[str, Any],
    *,
    role: str,
) -> None:
    entity["sources"].append(
        _source_descriptor(
            str(record["system"]),
            str(record["record_id"]),
            role=role,
            local_file=record.get("local_file"),
            page_url=record.get("page_url"),
        )
    )


def _append_record_assets(entity: dict[str, Any], record: dict[str, Any]) -> None:
    _append_unique_assets(entity, {"assets": _record_assets(record)})


def _prefer_longer_record_content(
    entity: dict[str, Any],
    record: dict[str, Any],
) -> None:
    current_text = (entity.get("preferred_content") or {}).get("plain_text") or ""
    new_text = str(record.get("plain_text") or "")
    if len(new_text) > len(current_text):
        entity["preferred_content"] = {
            "source_system": record["system"],
            "source_record_id": record["record_id"],
            "plain_text": new_text,
        }


def _compact_body_text(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _body_hash(entity: dict[str, Any]) -> str:
    text = _compact_body_text((entity.get("preferred_content") or {}).get("plain_text"))
    if len(text) < DEDUP_MIN_BODY_CHARS:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dedupe_title_key(value: Any) -> str:
    text = clean_title(value)
    text = LEADING_ITEM_MARKER_RE.sub("", text)
    text = TRIAL_MARKER_RE.sub("", text)
    text = REVISION_MARKER_RE.sub("", text)
    return normalize_title(text)


def _entity_dedupe_title_key(entity: dict[str, Any]) -> str:
    metadata = entity.get("metadata") or {}
    return _dedupe_title_key(metadata.get("name") or entity.get("title"))


def _source_file_sha256(local_file: Any) -> str:
    local_file_text = str(local_file or "")
    if not local_file_text:
        return ""
    path = output_path(local_file_text)
    if not path.exists() or not path.is_file() or path.suffix.lower() == ".json":
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entity_asset_shas(entity: dict[str, Any]) -> set[str]:
    result = {
        str(asset.get("sha256") or "")
        for asset in entity.get("assets") or []
        if asset.get("sha256")
    }
    for source in entity.get("sources") or []:
        digest = _source_file_sha256(source.get("local_file"))
        if digest:
            result.add(digest)
    return result


def _entity_sources_key(source: dict[str, Any]) -> tuple[Any, ...]:
    return (
        source.get("system"),
        source.get("record_id"),
        source.get("role"),
        source.get("local_file"),
        source.get("page_url"),
    )


def _append_unique_sources(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    sources = target.setdefault("sources", [])
    seen = {_entity_sources_key(source) for source in sources}
    for source in incoming.get("sources") or []:
        key = _entity_sources_key(source)
        if key not in seen:
            sources.append(source)
            seen.add(key)


def _append_unique_assets(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    assets = target.setdefault("assets", [])
    seen = {
        asset.get("sha256")
        or asset.get("local_file")
        or asset.get("source_url")
        or asset.get("asset_id")
        for asset in assets
    }
    for asset in incoming.get("assets") or []:
        key = (
            asset.get("sha256")
            or asset.get("local_file")
            or asset.get("source_url")
            or asset.get("asset_id")
        )
        if key and key not in seen:
            assets.append(asset)
            seen.add(key)


def _entity_completeness(entity: dict[str, Any]) -> int:
    metadata = entity.get("metadata") or {}
    score = 0
    for field in ("fileno", "pub_date", "effective_date"):
        if metadata.get(field):
            score += 1
    status = str(metadata.get("status") or entity.get("status") or "")
    if status and status != "unknown":
        score += 1
    return score


def _has_neris_source(entity: dict[str, Any]) -> bool:
    return any(source.get("system") == "neris" for source in entity.get("sources") or [])


def _choose_kept_entity_id(
    entity_ids: list[str],
    entities: dict[str, dict[str, Any]],
) -> str:
    return sorted(
        entity_ids,
        key=lambda entity_id: (
            not _has_neris_source(entities[entity_id]),
            -len((entities[entity_id].get("preferred_content") or {}).get("plain_text") or ""),
            -_entity_completeness(entities[entity_id]),
            entity_id,
        ),
    )[0]


def _source_records_for_entity(
    source_to_entity: dict[tuple[str, str], str],
    entity_id: str,
) -> list[str]:
    return [
        f"{system}:{record_id}"
        for (system, record_id), mapped_id in sorted(source_to_entity.items())
        if mapped_id == entity_id
    ]


def _merge_catalog_entity(
    *,
    entities: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
    matches: dict[str, dict[str, Any]],
    kept_id: str,
    removed_id: str,
    reason: str,
) -> dict[str, Any]:
    kept = entities[kept_id]
    removed = entities[removed_id]
    _append_unique_sources(kept, removed)
    _append_unique_assets(kept, removed)
    kept_text = str((kept.get("preferred_content") or {}).get("plain_text") or "")
    removed_text = str((removed.get("preferred_content") or {}).get("plain_text") or "")
    if len(removed_text) > len(kept_text):
        kept["preferred_content"] = dict(removed.get("preferred_content") or {})

    metadata = kept.setdefault("metadata", {})
    merged = metadata.setdefault("merged_catalog_entities", [])
    merged.append(
        {
            "canonical_id": removed_id,
            "title": removed.get("title"),
            "reason": reason,
        }
    )
    for item in (removed.get("metadata") or {}).get("merged_catalog_entities") or []:
        if item not in merged:
            merged.append(item)

    source_records = _source_records_for_entity(source_to_entity, removed_id)
    for source_key, mapped_id in list(source_to_entity.items()):
        if mapped_id == removed_id:
            source_to_entity[source_key] = kept_id
    for match in matches.values():
        if match.get("canonical_id") == removed_id:
            match["canonical_id"] = kept_id
    del entities[removed_id]
    return {
        "removed_id": removed_id,
        "kept_id": kept_id,
        "reason": reason,
        "source_records": source_records,
    }


def _merge_equivalent_groups(
    entities: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
    matches: dict[str, dict[str, Any]],
    groups: dict[str, list[str]],
    reason: str,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for entity_ids in groups.values():
        by_title: dict[str, list[str]] = defaultdict(list)
        for entity_id in entity_ids:
            if entity_id in entities:
                key = _entity_dedupe_title_key(entities[entity_id])
                if key:
                    by_title[key].append(entity_id)
        for matching_ids in by_title.values():
            if len(matching_ids) < 2:
                continue
            kept_id = _choose_kept_entity_id(matching_ids, entities)
            for removed_id in sorted(
                entity_id for entity_id in matching_ids if entity_id != kept_id
            ):
                if removed_id in entities:
                    merged.append(
                        _merge_catalog_entity(
                            entities=entities,
                            source_to_entity=source_to_entity,
                            matches=matches,
                            kept_id=kept_id,
                            removed_id=removed_id,
                            reason=reason,
                        )
                    )
    return merged


def _body_duplicate_groups(entities: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for entity_id, entity in entities.items():
        digest = _body_hash(entity)
        if digest:
            groups[digest].append(entity_id)
    return {digest: ids for digest, ids in groups.items() if len(ids) > 1}


def _asset_duplicate_groups(entities: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for entity_id, entity in entities.items():
        for sha256 in _entity_asset_shas(entity):
            groups[sha256].append(entity_id)
    return {sha256: ids for sha256, ids in groups.items() if len(ids) > 1}


def _announcement_mentions_multiple_titles(text: str, entities: list[dict[str, Any]]) -> bool:
    normalized_text = normalize_title(text)
    mentioned = 0
    for entity in entities:
        title = clean_title(entity.get("title") or (entity.get("metadata") or {}).get("name"))
        title_base = re.split(r"[（(]", title, maxsplit=1)[0]
        if title_base and normalize_title(title_base) in normalized_text:
            mentioned += 1
    return mentioned >= 2


def _is_multi_document_announcement_body(
    text: str,
    entities: list[dict[str, Any]],
) -> bool:
    if len({entity.get("title") for entity in entities}) < 2:
        return False
    if "现公布" not in text and "发布" not in text and "印发" not in text:
        return False
    if len(QUOTED_TITLE_RE.findall(text)) < 2:
        return False
    return _announcement_mentions_multiple_titles(text, entities)


def _repair_multi_document_announcement_content(
    entities: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    repaired: list[dict[str, Any]] = []
    for digest, entity_ids in _body_duplicate_groups(entities).items():
        current_entities = [
            entities[entity_id] for entity_id in entity_ids if entity_id in entities
        ]
        if len(current_entities) < 2:
            continue
        text = str((current_entities[0].get("preferred_content") or {}).get("plain_text") or "")
        if not _is_multi_document_announcement_body(text, current_entities):
            continue
        for entity in current_entities:
            preferred = entity.setdefault("preferred_content", {})
            original_length = len(str(preferred.get("plain_text") or ""))
            preferred["plain_text"] = ""
            metadata = entity.setdefault("metadata", {})
            metadata["content_repair"] = {
                "reason": "multi_document_announcement_body",
                "body_hash": digest,
                "original_text_length": original_length,
            }
            repaired.append(
                {
                    "canonical_id": entity.get("id"),
                    "title": entity.get("title"),
                    "reason": "multi_document_announcement_body",
                    "body_hash": digest,
                    "original_text_length": original_length,
                }
            )
    return repaired


def deduplicate_catalog_entities(
    entities: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
    matches: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    merged_entities: list[dict[str, Any]] = []
    merged_entities.extend(
        _merge_equivalent_groups(
            entities,
            source_to_entity,
            matches,
            _body_duplicate_groups(entities),
            "same_body_equivalent_title",
        )
    )
    merged_entities.extend(
        _merge_equivalent_groups(
            entities,
            source_to_entity,
            matches,
            _asset_duplicate_groups(entities),
            "same_asset_equivalent_title",
        )
    )
    content_repairs = _repair_multi_document_announcement_content(entities)
    return {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "merged": len(merged_entities),
        "content_repairs_count": len(content_repairs),
        "merged_entities": merged_entities,
        "content_repairs": content_repairs,
    }


def _find_existing_amac_entity(
    record: dict[str, Any],
    amac_entity_index: dict[str, list[tuple[str, dict[str, Any]]]],
) -> str | None:
    title_key = normalize_title((record.get("metadata") or {}).get("name"))
    amac_candidates = amac_entity_index.get(title_key) or []
    if len(amac_candidates) != 1:
        return None
    candidate_entity_id, candidate_record = amac_candidates[0]
    distance = _date_distance(
        (record.get("metadata") or {}).get("pub_date"),
        (candidate_record.get("metadata") or {}).get("pub_date"),
    )
    if distance is None or distance <= 3:
        return candidate_entity_id
    return None


def _merge_amac_record(
    record: dict[str, Any],
    *,
    matcher: CatalogMatcher,
    entities: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
    amac_entity_index: dict[str, list[tuple[str, dict[str, Any]]]],
) -> dict[str, Any]:
    match, status, confidence, evidence, match_rule_id = matcher.choose_neris_match(record)
    if match is not None:
        entity_id = source_to_entity[("neris", match["record_id"])]
        entity = entities[entity_id]
        _append_record_source(
            entity,
            record,
            role=(
                "supplemental_official_copy" if status == "supplemental_copy" else "official_copy"
            ),
        )
        _append_record_assets(entity, record)
        _prefer_longer_record_content(entity, record)
    else:
        title_key = normalize_title((record.get("metadata") or {}).get("name"))
        existing_amac_entity = _find_existing_amac_entity(record, amac_entity_index)
        if existing_amac_entity:
            entity_id = existing_amac_entity
            entity = entities[entity_id]
            _append_record_source(entity, record, role="official_copy")
            _append_record_assets(entity, record)
            _prefer_longer_record_content(entity, record)
            status = "same_document"
            confidence = MATCH_AMAC_INTERNAL_TITLE_DATE.confidence
            evidence = ["AMAC多个官方页面或附件题名、发布日期一致"]
            match_rule_id = MATCH_AMAC_INTERNAL_TITLE_DATE.rule_id
        else:
            entity_id = canonical_id(f"amac:{record['record_id']}")
            entities[entity_id] = _entity_from_record(record, entity_id)
            amac_entity_index[title_key].append((entity_id, record))

    source_to_entity[("amac", record["record_id"])] = entity_id
    return {
        "match_status": status,
        "neris_id": match.get("record_id") if match else None,
        "canonical_id": entity_id,
        "match_method": "normalized_title_fileno_date",
        "match_rule_id": match_rule_id,
        "confidence": confidence,
        "evidence": evidence,
    }


def _match_amac_records(
    amac_records: list[dict[str, Any]],
    matcher: CatalogMatcher,
    entities: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
) -> dict[str, dict[str, Any]]:
    amac_entity_index: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    matches: dict[str, dict[str, Any]] = {}

    for record in amac_records:
        matches[record["record_id"]] = _merge_amac_record(
            record,
            matcher=matcher,
            entities=entities,
            source_to_entity=source_to_entity,
            amac_entity_index=amac_entity_index,
        )
    return matches


def _add_amac_page_attachment_relations(
    amac_records: list[dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
    relation_ingestor: CatalogRelationIngestor,
) -> None:
    for record in amac_records:
        parent_id = record.get("parent_record_id")
        if not parent_id:
            continue
        parent_entity = source_to_entity.get(("amac", str(parent_id)))
        child_entity = source_to_entity.get(("amac", record["record_id"]))
        if parent_entity and child_entity:
            relation_ingestor.add(
                parent_entity,
                child_entity,
                "publishes",
                {
                    "source": "amac.page_attachment",
                    "rule_id": RELATION_AMAC_PAGE_ATTACHMENT.rule_id,
                    "parent_source_record_id": parent_id,
                    "attachment_source_record_id": record["record_id"],
                    "confidence": RELATION_AMAC_PAGE_ATTACHMENT.confidence,
                },
            )


def _add_neris_title_relations(
    neris_records: list[dict[str, Any]],
    entities: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
    relation_ingestor: CatalogRelationIngestor,
) -> None:
    entity_title_index: dict[str, list[str]] = defaultdict(list)
    for entity_id, entity in entities.items():
        entity_title_index[normalize_title(entity.get("title"))].append(entity_id)
    for record in neris_records:
        parent_entity = source_to_entity[("neris", record["record_id"])]
        title = str((record.get("metadata") or {}).get("name") or "")
        if not PUBLISHING_TITLE_RE.search(title):
            continue
        for quoted in QUOTED_TITLE_RE.findall(title):
            candidates = entity_title_index.get(normalize_title(quoted)) or []
            if len(candidates) == 1:
                relation_ingestor.add(
                    parent_entity,
                    candidates[0],
                    "publishes",
                    {
                        "source": "neris.title",
                        "rule_id": RELATION_NERIS_TITLE_QUOTED_DOCUMENT.rule_id,
                        "quoted_title": quoted,
                        "confidence": RELATION_NERIS_TITLE_QUOTED_DOCUMENT.confidence,
                    },
                )


def _add_trial_replacement_relations(
    entities: dict[str, dict[str, Any]],
    relation_ingestor: CatalogRelationIngestor,
) -> None:
    for relation in infer_trial_replacement_relations(entities):
        relation_ingestor.add(
            str(relation["from"]),
            str(relation["to"]),
            str(relation["relation"]),
            {
                "source": relation.get("source"),
                "confidence": relation.get("confidence"),
                **(relation.get("evidence") or {}),
            },
        )


def _add_known_successor_relations(
    entities: dict[str, dict[str, Any]],
    relation_ingestor: CatalogRelationIngestor,
) -> None:
    for relation in infer_known_successor_relations(entities):
        relation_ingestor.add(
            str(relation["from"]),
            str(relation["to"]),
            str(relation["relation"]),
            {
                "source": relation.get("source"),
                "confidence": relation.get("confidence"),
                **(relation.get("evidence") or {}),
            },
        )


def _build_catalog_relations(
    *,
    neris_records: list[dict[str, Any]],
    amac_records: list[dict[str, Any]],
    entities: dict[str, dict[str, Any]],
    source_to_entity: dict[tuple[str, str], str],
) -> list[dict[str, Any]]:
    relation_ingestor = CatalogRelationIngestor()
    _add_amac_page_attachment_relations(amac_records, source_to_entity, relation_ingestor)
    _add_neris_title_relations(
        neris_records,
        entities,
        source_to_entity,
        relation_ingestor,
    )
    _add_trial_replacement_relations(entities, relation_ingestor)
    _add_known_successor_relations(entities, relation_ingestor)
    return relation_ingestor.items


def _match_counts(matches: dict[str, dict[str, Any]]) -> dict[str, int]:
    match_counts: dict[str, int] = defaultdict(int)
    for item in matches.values():
        match_counts[str(item["match_status"])] += 1
    return dict(sorted(match_counts.items()))


def _catalog_manifest_items(entities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for entity_id, entity in sorted(entities.items()):
        items.append(
            {
                "id": entity_id,
                "title": entity.get("title"),
                "document_type": entity.get("document_type"),
                "status": entity.get("status"),
                "sources": len(entity.get("sources") or []),
                "file": relative_to_output(catalog_laws_dir() / f"{entity_id}.json"),
            }
        )
    return items


def _review_queue_items(
    amac_records: list[dict[str, Any]],
    *,
    source_to_entity: dict[tuple[str, str], str],
    matches: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    review_items = []
    for record in amac_records:
        match = matches.get(record["record_id"]) or {}
        metadata = record.get("metadata") or {}
        reasons = []
        rule_ids = []
        if match.get("match_status") == "ambiguous":
            reasons.append("source_match_ambiguous")
            rule_ids.append(REVIEW_SOURCE_MATCH_AMBIGUOUS.rule_id)
        match_confidence = float(match.get("confidence") or 0.0)
        if (
            match
            and match.get("match_status") != "ambiguous"
            and match_confidence < SOURCE_MATCH_REVIEW_CONFIDENCE_THRESHOLD
        ):
            reasons.append("source_match_low_confidence")
            rule_ids.append(REVIEW_SOURCE_MATCH_LOW_CONFIDENCE.rule_id)
        if metadata.get("document_type") == "self_regulatory_rule" and metadata.get("status") in {
            None,
            "",
            "unknown",
        }:
            reasons.append("effectiveness_unknown")
            rule_ids.append(REVIEW_EFFECTIVENESS_UNKNOWN.rule_id)
        if reasons:
            review_items.append(
                {
                    "source_record_id": record["record_id"],
                    "canonical_id": source_to_entity.get(("amac", record["record_id"])),
                    "name": metadata.get("name"),
                    "reasons": reasons,
                    "rule_ids": rule_ids,
                    "match_status": match.get("match_status"),
                    "match_rule_id": match.get("match_rule_id"),
                    "match_confidence": match.get("confidence"),
                    "source_url": record.get("page_url"),
                }
            )
    return review_items


def _catalog_manifest(
    *,
    neris_records: list[dict[str, Any]],
    amac_records: list[dict[str, Any]],
    entities: dict[str, dict[str, Any]],
    relations: list[dict[str, Any]],
    review_items: list[dict[str, Any]],
    matches: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "neris_source_records": len(neris_records),
        "amac_source_records": len(amac_records),
        "canonical_laws": len(entities),
        "relations": len(relations),
        "review_queue": len(review_items),
        "match_counts": _match_counts(matches),
        "laws_dir": relative_to_output(catalog_laws_dir()),
        "rules": catalog_rules_manifest(),
        "rule_calibration": catalog_rule_calibration(),
        "items": _catalog_manifest_items(entities),
    }


def build_catalog(*, clean: bool = True) -> dict[str, Any]:
    source_records = CatalogSourceLoader(_neris_records, _amac_records).load()
    neris_records = source_records.neris
    amac_records = source_records.amac
    if clean and catalog_laws_dir().exists():
        shutil.rmtree(catalog_laws_dir())
    catalog_laws_dir().mkdir(parents=True, exist_ok=True)
    writer = CatalogEntityWriter()

    entities, source_to_entity, title_index = _seed_neris_entities(neris_records)
    matcher = CatalogMatcher(title_index, choose_neris_match_with_rule)
    matches = _match_amac_records(amac_records, matcher, entities, source_to_entity)
    dedupe_result = deduplicate_catalog_entities(entities, source_to_entity, matches)
    relations = _build_catalog_relations(
        neris_records=neris_records,
        amac_records=amac_records,
        entities=entities,
        source_to_entity=source_to_entity,
    )
    review_items = _review_queue_items(
        amac_records,
        source_to_entity=source_to_entity,
        matches=matches,
    )
    manifest = _catalog_manifest(
        neris_records=neris_records,
        amac_records=amac_records,
        entities=entities,
        relations=relations,
        review_items=review_items,
        matches=matches,
    )

    writer.write_entities(catalog_laws_dir(), entities)
    writer.write_source_matches(source_matches_path(), source_to_entity, matches)
    writer.write_relations(catalog_relations_path(), relations)
    save_json(reports_dir() / "canonical_dedupe_map.json", dedupe_result)
    writer.write_review_queue(
        catalog_review_queue_path(),
        rules=catalog_rules_manifest(),
        rule_calibration=catalog_rule_calibration(),
        items=review_items,
    )
    catalog_dir().mkdir(parents=True, exist_ok=True)
    writer.write_manifest(catalog_manifest_path(), manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="生成NERIS+AMAC统一法规实体目录")
    parser.add_argument("--no-clean", action="store_true")
    args = parser.parse_args()
    try:
        manifest = build_catalog(clean=not args.no_clean)
    except Exception as exc:
        log_event("cli_error", level="ERROR", message=f"失败: {exc}", error_message=str(exc))
        return 1
    log_event("cli_result", message=json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "build-catalog"))
