"""Stable source identity and semantic fingerprints."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_QUERY_KEYS = {
    "_",
    "timestamp",
    "spm",
    "from",
    "source",
}
VOLATILE_METADATA_KEYS = {
    "crawl_time",
    "crawled_at",
    "fetched_at",
    "retrieved_at",
    "response_sha256",
    "content_sha256",
    "metadata_sha256",
    "assets_sha256",
    "discovery_evidence",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_final_url(url: str) -> str:
    parts = urlsplit(str(url).strip())
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_QUERY_KEYS:
            continue
        query.append((key, value))
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            re.sub(r"/{2,}", "/", parts.path) or "/",
            urlencode(query, doseq=True),
            "",
        )
    )


def source_record_id(
    source_system: str,
    *,
    upstream_id: Any = None,
    final_url: str | None = None,
) -> str:
    upstream_text = str(upstream_id or "").strip()
    if upstream_text:
        identity = f"{source_system}:{upstream_text}"
    elif final_url:
        identity = f"{source_system}:{canonical_final_url(final_url)}"
    else:
        raise ValueError("source_record_id requires upstream_id or final_url")
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _stable_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _without_volatile_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_volatile_metadata(item)
            for key, item in sorted(value.items())
            if key not in VOLATILE_METADATA_KEYS and not key.startswith("crawl_")
        }
    if isinstance(value, list):
        return [_without_volatile_metadata(item) for item in value]
    return value


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def record_fingerprints(
    *,
    metadata: dict[str, Any],
    plain_text: str,
    assets: list[dict[str, Any]],
    response_bytes: bytes,
) -> dict[str, str]:
    stable_assets = sorted(
        (
            {
                "source_url": canonical_final_url(str(item.get("source_url") or ""))
                if item.get("source_url")
                else "",
                "file_name": str(item.get("file_name") or item.get("label") or ""),
                "sha256": str(item.get("sha256") or ""),
            }
            for item in assets
        ),
        key=lambda item: (item["source_url"], item["file_name"], item["sha256"]),
    )
    return {
        "response_sha256": sha256_bytes(response_bytes),
        "metadata_sha256": _stable_json_sha256(_without_volatile_metadata(metadata)),
        "content_sha256": sha256_bytes(_normalized_text(plain_text).encode("utf-8")),
        "assets_sha256": _stable_json_sha256(stable_assets),
    }


__all__ = [
    "canonical_final_url",
    "record_fingerprints",
    "sha256_bytes",
    "source_record_id",
]
