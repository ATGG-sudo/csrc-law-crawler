"""Catalog source matching and successor inference helpers."""

from __future__ import annotations

from collections import defaultdict
import re
from typing import Any

from catalog_rules import (
    MATCH_AMBIGUOUS_TITLE,
    MATCH_NO_NERIS_TITLE,
    MATCH_TITLE_DATE,
    MATCH_TITLE_FILENO,
    MATCH_UNIQUE_TITLE,
    OFFICIAL_RULE_TYPES,
    RELATION_OFFICIAL_SUCCESSOR,
    RELATION_DRAFT_FINALIZED,
    RELATION_EXPLICIT_SUCCESSOR,
    RELATION_SAME_INSTRUMENT_COPY,
    TRIAL_REPLACEMENT,
)
from .classification import material_classification_for

from .identity import (
    _date_distance,
    _date_sort_value,
    instrument_title_keys,
    is_draft_title,
    is_trial_title,
    normalize_fileno,
    normalize_title,
    normalize_title_without_trial,
    QUOTED_TITLE_RE,
)

EXPLICIT_RENAME_RE = re.compile(r"将《([^》]{4,120})》修订为《([^》]{4,120})》")
REVISION_TITLE_SUFFIX_RE = re.compile(r"[（(]\s*(?:\d{4}年(?:\d{1,2}月)?)?修订\s*[）)]$")
EXPLICIT_REVISION_ACTION_RE = re.compile(
    r"(?:进行(?:了)?|作(?:了|出(?:了)?)|予以|已经|已)\s*修订|"
    r"(?<!年)修订(?:了|为)"
)
EXPLICIT_REPEAL_ACTION_RE = re.compile(
    r"(?:同时|同步|一并|予以|即行|决定)\s*废止|"
    r"按法定程序\s*废止|"
    r"自[^。\n，；;]{0,40}?起\s*废止"
)
SENTENCE_SPLIT_RE = re.compile(r"[。！？!?]+")
REPEAL_CLAUSE_BOUNDARIES = "，；;"
TITLE_ALIAS_RE = re.compile(r"《([^》]{4,120})》[^。《》]{0,80}?以下简称(?:原)?《([^》]{4,60})》")

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


def infer_draft_finalization_relations(
    entities: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    by_key: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for entity_id, entity in entities.items():
        for key in instrument_title_keys(entity.get("title")):
            by_key[key].append((entity_id, entity))
    relations: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for draft_id, draft in entities.items():
        if not is_draft_title(draft.get("title")):
            continue
        draft_date = _pub_date_value(draft)
        draft_org = _normalized_org(draft)
        for key in instrument_title_keys(draft.get("title")):
            candidates = []
            for formal_id, formal in by_key.get(key) or []:
                if formal_id == draft_id or is_draft_title(formal.get("title")):
                    continue
                if not _is_official_rule_entity(formal):
                    continue
                formal_org = _normalized_org(formal)
                if draft_org and formal_org and draft_org != formal_org:
                    continue
                formal_date = _pub_date_value(formal)
                if draft_date is not None and formal_date is not None and formal_date < draft_date:
                    continue
                candidates.append((formal_date or 0, formal_id, formal))
            if len(candidates) != 1:
                continue
            _, formal_id, formal = candidates[0]
            if (formal_id, draft_id) in seen:
                continue
            seen.add((formal_id, draft_id))
            relations.append(
                {
                    "from": formal_id,
                    "to": draft_id,
                    "relation": "finalizes_draft",
                    "source": "catalog.exact_instrument_draft",
                    "rule_id": RELATION_DRAFT_FINALIZED.rule_id,
                    "confidence": RELATION_DRAFT_FINALIZED.confidence,
                    "evidence": {
                        "rule_id": RELATION_DRAFT_FINALIZED.rule_id,
                        "instrument_title": key,
                        "draft_title": draft.get("title"),
                        "formal_title": formal.get("title"),
                        "draft_pub_date": (draft.get("metadata") or {}).get("pub_date"),
                        "formal_pub_date": (formal.get("metadata") or {}).get("pub_date"),
                    },
                }
            )
    return relations


def infer_same_instrument_relations(
    entities: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    by_fileno: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for entity_id, entity in entities.items():
        if any(
            source.get("page_role") == "publication_wrapper"
            for source in entity.get("sources") or []
        ):
            continue
        fileno = normalize_fileno((entity.get("metadata") or {}).get("fileno"))
        if fileno:
            by_fileno[fileno].append((entity_id, entity))
    relations: list[dict[str, Any]] = []
    for fileno, group in by_fileno.items():
        for left_index, (left_id, left) in enumerate(group):
            left_keys = instrument_title_keys(left.get("title"))
            for right_id, right in group[left_index + 1 :]:
                shared = sorted(left_keys & instrument_title_keys(right.get("title")))
                if not shared:
                    continue
                left_neris = any(
                    source.get("system") == "neris" for source in left.get("sources") or []
                )
                right_neris = any(
                    source.get("system") == "neris" for source in right.get("sources") or []
                )
                if left_neris != right_neris:
                    from_id, to_id = (left_id, right_id) if left_neris else (right_id, left_id)
                else:
                    from_id, to_id = sorted((left_id, right_id))
                relations.append(
                    {
                        "from": from_id,
                        "to": to_id,
                        "relation": "same_instrument_copy",
                        "source": "catalog.title_fileno",
                        "rule_id": RELATION_SAME_INSTRUMENT_COPY.rule_id,
                        "confidence": RELATION_SAME_INSTRUMENT_COPY.confidence,
                        "evidence": {
                            "rule_id": RELATION_SAME_INSTRUMENT_COPY.rule_id,
                            "instrument_title": shared[0],
                            "fileno": fileno,
                        },
                    }
                )
    return relations


def infer_explicit_successor_relations(
    entities: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    by_title: dict[str, list[str]] = defaultdict(list)
    by_revision_title: dict[str, list[str]] = defaultdict(list)
    for entity_id, entity in entities.items():
        for key in instrument_title_keys(entity.get("title")):
            by_title[key].append(entity_id)
        title = str(entity.get("title") or "")
        base_title = REVISION_TITLE_SUFFIX_RE.sub("", title).strip()
        if base_title:
            by_revision_title[normalize_title(base_title)].append(entity_id)
    relations: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entity_id, entity in entities.items():
        material = material_classification_for(entity)
        is_publication_wrapper = any(
            source.get("page_role") == "publication_wrapper"
            for source in entity.get("sources") or []
        )
        formal_reference_copy = material.get("category") == "other_reference" and str(
            entity.get("title") or ""
        ).endswith(("法", "条例"))
        if material.get("lane") != "rule" and not (is_publication_wrapper or formal_reference_copy):
            continue
        body = str((entity.get("preferred_content") or {}).get("plain_text") or "")
        aliases: dict[str, set[str]] = defaultdict(set)
        for full_title, alias in TITLE_ALIAS_RE.findall(body):
            aliases[normalize_title(alias)].add(full_title)
        clauses: list[tuple[str, str, str, str | None]] = []
        title = str(entity.get("title") or "")
        base_title = REVISION_TITLE_SUFFIX_RE.sub("", title).strip()
        if base_title != title:
            base_key = normalize_title(base_title)
            revision_clause = ""
            for title_match in QUOTED_TITLE_RE.finditer(body):
                if normalize_title(title_match.group(1)) != base_key:
                    continue
                tail = body[title_match.end() : title_match.end() + 80]
                action = EXPLICIT_REVISION_ACTION_RE.search(tail)
                if action:
                    revision_clause = body[title_match.start() : title_match.end() + action.end()]
                    break
            source_date = _pub_date_value(entity)
            source_org = _normalized_org(entity)
            if revision_clause and source_date is not None and source_org:
                candidates: list[tuple[int, str]] = []
                for candidate_id in by_revision_title.get(base_key) or []:
                    if candidate_id == entity_id:
                        continue
                    candidate = entities[candidate_id]
                    candidate_date = _pub_date_value(candidate)
                    if candidate_date is None or candidate_date >= source_date:
                        continue
                    if _normalized_org(candidate) != source_org:
                        continue
                    candidates.append((candidate_date, candidate_id))
                if candidates:
                    latest_date = max(candidate[0] for candidate in candidates)
                    latest_ids = [
                        candidate_id
                        for candidate_date, candidate_id in candidates
                        if candidate_date == latest_date
                    ]
                    if len(latest_ids) == 1:
                        clauses.append(
                            (
                                base_title,
                                revision_clause,
                                "exact_title_revision",
                                latest_ids[0],
                            )
                        )
        for match in EXPLICIT_RENAME_RE.finditer(body):
            old_title, new_title = match.groups()
            if normalize_title(new_title) in instrument_title_keys(entity.get("title")):
                clauses.append((old_title, match.group(0), "explicit_rename", None))
        for sentence in SENTENCE_SPLIT_RE.split(body):
            for action in EXPLICIT_REPEAL_ACTION_RE.finditer(sentence):
                prefix = sentence[: action.start()]
                clause_start = max(
                    (prefix.rfind(marker) for marker in REPEAL_CLAUSE_BOUNDARIES),
                    default=-1,
                )
                clause = sentence[clause_start + 1 : action.end()]
                prior_list_start = prefix.rfind("此前发布的")
                if prior_list_start >= 0:
                    prior_list = sentence[
                        prior_list_start + len("此前发布的") : action.end()
                    ].lstrip()
                    if prior_list.startswith("《"):
                        clause = prior_list
                clauses.extend(
                    (old_title, clause, "explicit_repeal", None)
                    for old_title in QUOTED_TITLE_RE.findall(clause)
                )
        compact_body = re.sub(r"\s+", "", body)
        supporting_repeal = "配套内容与格式指引同时废止"
        disclosure_prefix = normalize_title("私募投资基金信息披露内容与格式指引")
        if supporting_repeal in compact_body:
            for candidate_id, candidate in entities.items():
                candidate_title = str(candidate.get("title") or "")
                if candidate_id != entity_id and normalize_title(candidate_title).startswith(
                    disclosure_prefix
                ):
                    clauses.append(
                        (
                            candidate_title,
                            supporting_repeal,
                            "explicit_supporting_instruments_repeal",
                            candidate_id,
                        )
                    )
        for old_title, clause, inference, direct_target_id in clauses:
            targets = (
                [direct_target_id]
                if direct_target_id
                else by_title.get(normalize_title(old_title)) or []
            )
            resolved_title = old_title
            if not targets:
                full_titles = aliases.get(normalize_title(old_title)) or set()
                if len(full_titles) == 1:
                    resolved_title = next(iter(full_titles))
                    targets = by_title.get(normalize_title(resolved_title)) or []
            if len(targets) > 1:
                exact_targets = [
                    target_id
                    for target_id in targets
                    if normalize_title(entities[target_id].get("title"))
                    == normalize_title(resolved_title)
                ]
                if len(exact_targets) == 1:
                    targets = exact_targets
            if len(targets) != 1 or targets[0] == entity_id:
                continue
            target_id = targets[0]
            source_date = _pub_date_value(entity)
            target_date = _pub_date_value(entities[target_id])
            if source_date is not None and target_date is not None and source_date < target_date:
                continue
            if (entity_id, target_id) in seen:
                continue
            seen.add((entity_id, target_id))
            relations.append(
                {
                    "from": entity_id,
                    "to": target_id,
                    "relation": "supersedes",
                    "source": "catalog.explicit_successor_clause",
                    "rule_id": RELATION_EXPLICIT_SUCCESSOR.rule_id,
                    "confidence": RELATION_EXPLICIT_SUCCESSOR.confidence,
                    "evidence": {
                        "rule_id": RELATION_EXPLICIT_SUCCESSOR.rule_id,
                        "old_title": resolved_title,
                        "old_title_mention": old_title,
                        "new_title": entity.get("title"),
                        "inference": inference,
                        "clause": clause,
                        "source_pub_date": (entity.get("metadata") or {}).get("pub_date"),
                        "effective_date": (entity.get("metadata") or {}).get(
                            "effective_date"
                        ),
                        "target_pub_date": (entities[target_id].get("metadata") or {}).get(
                            "pub_date"
                        ),
                    },
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
    (instrument_title_keys,)
    (is_draft_title,)
