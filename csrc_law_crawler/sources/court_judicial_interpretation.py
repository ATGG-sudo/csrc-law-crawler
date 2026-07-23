"""Controlled acquisition for selected Supreme People's Court documents.

The configured pages are official but are not uniform document pages: some are
news wrappers and two contain several amended judicial interpretations.  This
adapter therefore discovers only checked-in document specifications and refuses
to materialize a record when a configured boundary or structural assertion is
not satisfied exactly once.
"""

from __future__ import annotations

from html import escape
import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from .adapters import HttpHtmlAdapter


COURT_ENDPOINT_ID = "court_judicial_interpretation_company_law"
COURT_SOURCE_SYSTEM = "court_judicial_interpretation"
_ARTICLE_HEADING_RE = re.compile(
    r"(?m)^[ \t\u3000]*第([一二三四五六七八九十百]+)条"
)


class ControlledDocumentError(ValueError):
    """Raised when an official page no longer matches its controlled spec."""


def _compact(value: str) -> str:
    return "".join(char for char in str(value) if not char.isspace())


def _chinese_number(value: int) -> str:
    if value <= 0 or value >= 100:
        raise ValueError(f"unsupported article number: {value}")
    digits = "零一二三四五六七八九"
    if value < 10:
        return digits[value]
    tens, ones = divmod(value, 10)
    prefix = "十" if tens == 1 else f"{digits[tens]}十"
    return prefix if not ones else f"{prefix}{digits[ones]}"


def _unique_compact_offset(text: str, marker: str, label: str) -> tuple[int, int]:
    compact_text = _compact(text)
    compact_marker = _compact(marker)
    if not compact_marker:
        raise ControlledDocumentError(f"{label} marker is empty")
    matches = [match.start() for match in re.finditer(re.escape(compact_marker), compact_text)]
    if len(matches) != 1:
        raise ControlledDocumentError(
            f"{label} marker must occur exactly once, got {len(matches)}"
        )
    return matches[0], len(compact_marker)


def _slice_controlled(text: str, segment: dict[str, Any]) -> str:
    compact_positions = [index for index, char in enumerate(text) if not char.isspace()]
    start_at, _ = _unique_compact_offset(text, str(segment.get("start") or ""), "start")
    end_at, end_length = _unique_compact_offset(
        text, str(segment.get("end") or ""), "end"
    )
    if end_at <= start_at:
        raise ControlledDocumentError("end marker must follow start marker")
    start_original = compact_positions[start_at]
    if segment.get("include_end"):
        end_original = compact_positions[end_at + end_length - 1] + 1
    else:
        end_original = compact_positions[end_at]
    return text[start_original:end_original]


def _normalized_lines(text: str) -> str:
    lines = [
        re.sub(r"[ \t\u3000]+", " ", line).strip()
        for line in str(text).splitlines()
    ]
    return "\n".join(line for line in lines if line)


def _body_node(soup: BeautifulSoup) -> Tag:
    candidates: list[Tag] = []
    seen: set[int] = set()
    for selector in ("#zoom", ".txt_txt"):
        for node in soup.select(selector):
            if isinstance(node, Tag) and id(node) not in seen:
                candidates.append(node)
                seen.add(id(node))
    if len(candidates) != 1:
        raise ControlledDocumentError(
            f"official body selector must resolve to one node, got {len(candidates)}"
        )
    return candidates[0]


def _assert_document(text: str, spec: dict[str, Any]) -> None:
    compact_text = _compact(text)
    for marker in spec.get("required_markers") or []:
        if _compact(str(marker)) not in compact_text:
            raise ControlledDocumentError(f"required marker missing: {marker}")
    for marker in spec.get("forbidden_markers") or []:
        if _compact(str(marker)) in compact_text:
            raise ControlledDocumentError(f"forbidden marker present: {marker}")

    expected_count = spec.get("expected_article_count")
    if expected_count is None:
        return
    expected_count = int(expected_count)
    actual = [f"第{match.group(1)}条" for match in _ARTICLE_HEADING_RE.finditer(text)]
    expected = [f"第{_chinese_number(number)}条" for number in range(1, expected_count + 1)]
    if actual != expected:
        raise ControlledDocumentError(
            f"article headings must be 1-{expected_count} exactly, got {actual}"
        )


class CourtJudicialInterpretationAdapter(HttpHtmlAdapter):
    """Adapter for a fixed, audited set of official SPC document pages."""

    def discover(
        self,
        endpoint: dict[str, Any],
        registry: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> dict[str, Any]:
        del registry, checkpoint
        documents = endpoint.get("documents")
        if not isinstance(documents, list) or not documents:
            raise ControlledDocumentError("court endpoint requires document specs")

        items: list[dict[str, Any]] = []
        upstream_ids: set[str] = set()
        for spec in documents:
            upstream_id = str(spec.get("upstream_id") or "").strip()
            url = str(spec.get("url") or "").strip()
            title = str(spec.get("name") or "").strip()
            if not upstream_id or upstream_id in upstream_ids:
                raise ControlledDocumentError(
                    f"duplicate or empty court upstream_id: {upstream_id!r}"
                )
            if not url or not title:
                raise ControlledDocumentError(f"incomplete court document spec: {upstream_id}")
            upstream_ids.add(upstream_id)
            items.append(
                {
                    "url": url,
                    "title": title,
                    "upstream_id": upstream_id,
                    "in_scope": True,
                    "matched_query_terms": [],
                    "document_spec": spec,
                    "discovery_evidence": [
                        {
                            "endpoint_id": endpoint["endpoint_id"],
                            "method": "checked_in_document_spec",
                            "official_url": url,
                            "upstream_id": upstream_id,
                        }
                    ],
                }
            )
        return {
            "items": items,
            "raw_pages": [],
            "discovery_status": "complete",
            "pages_completed": 0,
            "reported_total": len(items),
            "raw_hit_count": len(items),
            "filtered_count": 0,
            "pagination_links_seen": 0,
            "single_article": False,
            "completeness_evidence": "checked_in_document_specs",
            "query_execution": "controlled_official_urls",
            "result_limit_reached": False,
            "queries_completed": 0,
            "queries_total": 0,
            "failures": [],
        }

    def parse(
        self,
        endpoint: dict[str, Any],
        item: dict[str, Any],
        fetched: dict[str, Any],
    ) -> dict[str, Any]:
        content_type = str(fetched.get("content_type") or "").lower()
        body_bytes = bytes(fetched.get("body") or b"")
        if "html" not in content_type and not body_bytes.lstrip().startswith(b"<"):
            raise ControlledDocumentError(f"unsupported court content type: {content_type}")

        spec = item.get("document_spec")
        if not isinstance(spec, dict):
            raise ControlledDocumentError("discovered item has no court document spec")
        soup = BeautifulSoup(body_bytes, "html.parser")
        title_nodes = soup.select("div.title")
        if len(title_nodes) != 1:
            raise ControlledDocumentError(
                f"div.title must occur exactly once, got {len(title_nodes)}"
            )
        page_title = title_nodes[0].get_text(" ", strip=True)
        expected_page_title = str(spec.get("page_title_contains") or "")
        if expected_page_title and _compact(expected_page_title) not in _compact(page_title):
            raise ControlledDocumentError(
                f"unexpected official page title for {item.get('upstream_id')}: {page_title}"
            )

        body = _body_node(soup)
        for node in body.select("script,style,noscript,nav,header,footer,form"):
            node.decompose()
        page_text = body.get_text("\n", strip=True)
        segment = spec.get("segment")
        selected_text = _slice_controlled(page_text, segment) if segment else page_text
        plain_text = _normalized_lines(selected_text)
        if len(plain_text) < 20:
            raise ControlledDocumentError("controlled document body is empty or too short")
        _assert_document(plain_text, spec)

        material_lane = str(spec.get("material_lane") or "")
        if material_lane not in {"rule", "reference"}:
            raise ControlledDocumentError(f"invalid court material lane: {material_lane!r}")
        metadata = dict(spec.get("metadata") or {})
        metadata.update(
            {
                "name": str(spec["name"]),
                "publisher": "最高人民法院",
                "pub_org": "最高人民法院",
                "region": "全国",
                "material_lane": material_lane,
                "official_page_title": page_title,
                "official_page_url": str(fetched.get("final_url") or item["url"]),
                "official_page_id": str(spec.get("page_id") or ""),
            }
        )
        if not metadata.get("document_type"):
            metadata["document_type"] = (
                "judicial_interpretation" if material_lane == "rule" else "official_interview"
            )

        html = "<div>" + "".join(
            f"<p>{escape(line)}</p>" for line in plain_text.splitlines()
        ) + "</div>"
        headers = {
            str(key): str(value)
            for key, value in sorted((fetched.get("headers") or {}).items())
        }
        return {
            "metadata": metadata,
            "plain_text": plain_text,
            "content_html": html,
            "assets": [],
            "http_metadata": {
                "status_code": fetched.get("status_code"),
                "content_type": fetched.get("content_type"),
                "final_url": fetched.get("final_url"),
                "headers": headers,
            },
        }


__all__ = [
    "COURT_ENDPOINT_ID",
    "COURT_SOURCE_SYSTEM",
    "ControlledDocumentError",
    "CourtJudicialInterpretationAdapter",
]
