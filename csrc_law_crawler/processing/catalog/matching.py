"""Catalog source matching and successor inference helpers."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from catalog_rules import (
    MATCH_AMBIGUOUS_TITLE,
    MATCH_NO_NERIS_TITLE,
    MATCH_TITLE_DATE,
    MATCH_TITLE_FILENO,
    MATCH_UNIQUE_TITLE,
    OFFICIAL_RULE_TYPES,
    RELATION_OFFICIAL_SUCCESSOR,
    TRIAL_REPLACEMENT,
)

from .identity import (
    _date_distance,
    _date_sort_value,
    is_trial_title,
    normalize_fileno,
    normalize_title,
    normalize_title_without_trial,
)

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

def _normalized_org(entity: dict[str, Any]) -> str:
    metadata = entity.get("metadata") or {}
    return normalize_title(metadata.get("pub_org"))

def _pub_date_value(entity: dict[str, Any]) -> int | None:
    metadata = entity.get("metadata") or {}
    return _date_sort_value(metadata.get("pub_date"))

def _is_official_rule_entity(entity: dict[str, Any]) -> bool:
    document_type = str(entity.get("document_type") or "")
    return document_type in OFFICIAL_RULE_TYPES

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
