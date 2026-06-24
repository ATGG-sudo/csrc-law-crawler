"""法规 JSON 解析与正文拼接。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def ms_to_date(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def law_status_label(code: str | None) -> str | None:
    mapping = {
        "0": "已颁布未施行",
        "1": "现行有效",
        "2": "已被修改",
        "3": "已被废止",
    }
    if code is None:
        return None
    return mapping.get(str(code), str(code))


def parse_entry_items(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not items:
        return []
    result = []
    for item in items:
        text = (item.get("cntnt") or "").strip()
        if not text and not item.get("title"):
            continue
        result.append(
            {
                "entry_id": item.get("secFutrsLawEntryId"),
                "code": item.get("secFutrsLawEntryCde"),
                "title": item.get("title") or "",
                "text": text,
            }
        )
    return result


def parse_entries(entries: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not entries:
        return []
    result = []
    for entry in entries:
        title = (entry.get("title") or "").strip()
        cntnt = (entry.get("cntnt") or "").strip()
        items = parse_entry_items(entry.get("itemList"))
        if not title and not cntnt and not items:
            continue
        node: dict[str, Any] = {
            "entry_id": entry.get("secFutrsLawEntryId"),
            "code": entry.get("secFutrsLawEntryCde"),
            "class_code": entry.get("secFutrsLawEntryClsfCde"),
            "title": title,
            "text": cntnt,
        }
        if items:
            node["items"] = items
        result.append(node)
    return result


def build_full_text(metadata: dict[str, Any], entries: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    name = metadata.get("name") or ""
    if name:
        parts.append(name)

    body_ago = (metadata.get("body_ago") or "").strip()
    if body_ago:
        parts.append(body_ago)

    for entry in entries:
        title = entry.get("title") or ""
        text = entry.get("text") or ""
        if title:
            parts.append(title)
        if text:
            parts.append(text)
        for item in entry.get("items") or []:
            item_title = item.get("title") or ""
            item_text = item.get("text") or ""
            if item_title:
                parts.append(item_title)
            if item_text:
                parts.append(item_text)

    body_aft = (metadata.get("body_aft") or "").strip()
    if body_aft:
        parts.append(body_aft)

    return "\n\n".join(p for p in parts if p)


def extract_metadata(law: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": law.get("secFutrsLawId"),
        "number": law.get("secFutrsLawNbr"),
        "name": law.get("secFutrsLawName"),
        "fileno": law.get("fileno"),
        "pub_org": law.get("lawPubOrgName"),
        "pub_date": ms_to_date(law.get("pubDate")),
        "effective_date": ms_to_date(law.get("efctvDate")),
        "ineffective_date": ms_to_date(law.get("inefctvDate")),
        "status_code": law.get("lawAthrtyStsCde"),
        "status": law_status_label(law.get("lawAthrtyStsCde")),
        "version": law.get("secFutrsLawVersion"),
        "body_ago": law.get("bodyAgoCntnt"),
        "body_aft": law.get("bodyAftCntnt"),
    }


def build_law_document(lawlist: dict[str, Any]) -> dict[str, Any]:
    law = lawlist.get("law") or {}
    entries = parse_entries(lawlist.get("lawEntryVOs"))
    metadata = extract_metadata(law)
    return {
        "metadata": metadata,
        "entries": entries,
        "full_text": build_full_text(metadata, entries),
        "entry_class_code": lawlist.get("lawEntryClsfCde"),
    }
