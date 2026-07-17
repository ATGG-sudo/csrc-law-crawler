"""Catalog identity and title-normalization helpers."""

from __future__ import annotations

import hashlib
import html
import re
from typing import Any

QUOTED_TITLE_RE = re.compile(r"《([^》]{4,120})》")
PUBLISHING_TITLE_RE = re.compile(
    r"^(?:(?:关于)?(?:发布|印发|公布|修订并发布)|[^《》]{2,30}发布《)"
)
SPACE_PUNCT_RE = re.compile(r"[\s\u3000·•,，。；;:：()（）\[\]【】《》“”\"'、—\-]+")
ATTACHMENT_PREFIX_RE = re.compile(r"^附件(?:\s*\d+(?:-\d+)?)?\s*[：:、.\-]?\s*")
FILE_SUFFIX_RE = re.compile(r"\.(pdf|docx?|xlsx?|zip|rar|rtf|wps)$", re.I)
TRIAL_MARKER_RE = re.compile(r"[（(]\s*试行\s*[）)]|试行")
DRAFT_MARKER_RE = re.compile(
    r"[（(]\s*(?:公开)?征求意见稿\s*[）)]|"
    r"[（(]\s*草案\s*[）)]|(?:公开)?征求意见稿|草案"
)
ORDER_PREFIX_RE = re.compile(r"^(?:关于)?(?:公开征求意见|征求意见)(?:的)?(?:通知|公告)?")
REVISION_MARKER_RE = re.compile(r"[（(]\s*(?:\d{4}年)?修订\s*[）)]")
LEADING_ITEM_MARKER_RE = re.compile(r"^\s*\d+(?:[-.、．]\d+)?[-.、．]?\s*")
ATTACHMENT_TEXT_SIGNAL_RE = re.compile(
    r"(?:详[细情]?见附件|详情请(?:查看)?附件|全文详见附件|见附件|附件下载|相关文档)"
)
SECTION_TOKEN_RE = re.compile(r"第[一二三四五六七八九十百千零〇0-9]+[条章节编款项部分]")
DEDUP_MIN_BODY_CHARS = 80


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


def is_draft_title(value: Any) -> bool:
    return bool(DRAFT_MARKER_RE.search(clean_title(value)))


def normalize_title_without_draft(value: Any) -> str:
    text = DRAFT_MARKER_RE.sub("", clean_title(value))
    text = ORDER_PREFIX_RE.sub("", text)
    return SPACE_PUNCT_RE.sub("", text).lower()


def instrument_title_keys(value: Any) -> set[str]:
    """Return exact normalized instrument names from a page or attachment title."""
    title = clean_title(value)
    values = [title, *QUOTED_TITLE_RE.findall(title)]
    return {
        normalize_title_without_draft(item)
        for item in values
        if normalize_title_without_draft(item)
    }


def normalize_fileno(value: Any) -> str:
    return SPACE_PUNCT_RE.sub("", html.unescape(str(value or ""))).lower()


def fileno_keys(value: Any) -> set[str]:
    normalized = normalize_fileno(value)
    if not normalized:
        return set()
    keys = {normalized}
    order = re.search(r"第\d+号", normalized)
    if order:
        keys.add(order.group(0))
    return keys


def canonical_id(seed: str) -> str:
    return f"law_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"


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


date_distance = _date_distance
date_sort_value = _date_sort_value
