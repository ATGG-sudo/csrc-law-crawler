"""Identity and classification helpers for AMAC source records."""

from __future__ import annotations

import hashlib
import html
import re
from urllib.parse import urlsplit, urlunsplit

from catalog_rules import classify_amac_document

TITLE_PREFIX_RE = re.compile(r"^附件(?:\s*\d+(?:-\d+)?)?\s*[：:、.\-]?\s*")
DATE_SUFFIX_RE = re.compile(r"\s+\d{2}-\d{2}$")


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    path = re.sub(r"/+", "/", parts.path)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def source_record_id(url: str) -> str:
    digest = hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:24]
    return f"amac_{digest}"


def clean_text(value: str) -> str:
    value = html.unescape(value or "").replace("\xa0", " ").replace("\u3000", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def clean_attachment_title(value: str) -> str:
    value = DATE_SUFFIX_RE.sub("", clean_text(value))
    value = TITLE_PREFIX_RE.sub("", value)
    return value.strip() or "未命名附件"


def classify_document(title: str, url: str) -> str:
    return classify_amac_document(title, url)[0]


def classified_document_metadata(title: str, url: str) -> dict[str, str]:
    document_type, rule = classify_amac_document(title, url)
    return {
        "document_type": document_type,
        "document_type_rule_id": rule.rule_id,
    }


__all__ = [
    "DATE_SUFFIX_RE",
    "TITLE_PREFIX_RE",
    "canonical_url",
    "classified_document_metadata",
    "classify_document",
    "clean_attachment_title",
    "clean_text",
    "source_record_id",
]
