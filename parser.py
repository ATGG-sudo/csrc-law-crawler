"""法规 JSON 解析与正文拼接。"""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any
from zoneinfo import ZoneInfo


KNOWN_NERIS_MOJIBAKE = {
    "\ufffd0\ufffd2": "",
    "\ufffd6\ufffd1": "·",
}

EXPLICIT_EFFECTIVE_DATE_RE = re.compile(
    r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*起\s*(?:施行|实施)"
)
PUBLISH_DATE_EFFECTIVE_PATTERNS = (
    "自发布之日起施行",
    "自发布之日起实施",
    "自公布之日起施行",
    "自公布之日起实施",
    "自印发之日起施行",
    "自印发之日起实施",
)


def repair_known_neris_mojibake(text: str) -> str:
    """Repair known NERIS replacement-character artifacts in derived text."""
    value = text
    for broken, repaired in KNOWN_NERIS_MOJIBAKE.items():
        value = value.replace(broken, repaired)
    return value


def _clean_source_text(value: Any) -> str:
    return repair_known_neris_mojibake(str(value or ""))


def _clean_source_field(value: Any) -> Any:
    if isinstance(value, str):
        return repair_known_neris_mojibake(value)
    return value


def ms_to_date(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=ZoneInfo("Asia/Shanghai")).strftime(
        "%Y-%m-%d"
    )


def infer_effective_date(metadata: dict[str, Any], text: str) -> str | None:
    """Prefer an explicit commencement clause over lossy source metadata."""
    compact = re.sub(r"\s+", "", text)
    existing_text = str(metadata.get("effective_date") or "").strip()[:10]
    try:
        existing = date.fromisoformat(existing_text)
    except ValueError:
        existing = None
    for match in reversed(list(EXPLICIT_EFFECTIVE_DATE_RE.finditer(compact))):
        try:
            inferred = date(*(int(value) for value in match.groups()))
        except ValueError:
            continue
        if existing and abs((inferred - existing).days) > 3:
            return existing.isoformat()
        return inferred.isoformat()
    if any(pattern in compact for pattern in PUBLISH_DATE_EFFECTIVE_PATTERNS):
        version = str(metadata.get("version") or "")
        if len(version) == 8 and version.isdigit():
            try:
                inferred = date(int(version[:4]), int(version[4:6]), int(version[6:]))
                if existing and abs((inferred - existing).days) > 3:
                    return existing.isoformat()
                return inferred.isoformat()
            except ValueError:
                pass
        published = str(metadata.get("pub_date") or "")[:10]
        try:
            inferred = date.fromisoformat(published)
            if existing and abs((inferred - existing).days) > 3:
                return existing.isoformat()
            return inferred.isoformat()
        except ValueError:
            pass
    value = str(metadata.get("effective_date") or "").strip()
    return value or None


def infer_pub_date(metadata: dict[str, Any], page_url: str | None = None) -> str | None:
    """Normalize a full publication date, falling back to dated official URLs."""
    value = str(metadata.get("pub_date") or "").strip()
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        pass
    match = re.search(r"(?:^|/)t(\d{4})(\d{2})(\d{2})_", str(page_url or ""))
    if match:
        try:
            return date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            pass
    return value or None


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
        text = _clean_source_text(item.get("cntnt")).strip()
        if not text and not item.get("title"):
            continue
        result.append(
            {
                "entry_id": item.get("secFutrsLawEntryId"),
                "code": item.get("secFutrsLawEntryCde"),
                "title": _clean_source_text(item.get("title")).strip(),
                "text": text,
            }
        )
    return result


def parse_entries(entries: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not entries:
        return []
    result = []
    for entry in entries:
        title = _clean_source_text(entry.get("title")).strip()
        cntnt = _clean_source_text(entry.get("cntnt")).strip()
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
        "name": _clean_source_field(law.get("secFutrsLawName")),
        "fileno": _clean_source_field(law.get("fileno")),
        "pub_org": _clean_source_field(law.get("lawPubOrgName")),
        "pub_date": ms_to_date(law.get("pubDate")),
        "effective_date": ms_to_date(law.get("efctvDate")),
        "ineffective_date": ms_to_date(law.get("inefctvDate")),
        "status_code": law.get("lawAthrtyStsCde"),
        "status": law_status_label(law.get("lawAthrtyStsCde")),
        "version": law.get("secFutrsLawVersion"),
        "body_ago": _clean_source_field(law.get("bodyAgoCntnt")),
        "body_aft": _clean_source_field(law.get("bodyAftCntnt")),
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
