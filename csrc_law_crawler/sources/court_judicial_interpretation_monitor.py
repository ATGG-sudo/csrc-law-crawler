"""Monitor the SPC judicial-interpretation directory without publishing law records.

The directory is a discovery surface, not a legal-effect source.  Records from
this adapter therefore stay in the ``clue`` lane until a human adds a precise
document specification to the controlled court source.
"""

from __future__ import annotations

from collections import Counter
from html import escape
from http import HTTPStatus
import math
import os
import random
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
import requests

from runtime import utc_now_iso

from .adapters import (
    HttpHtmlAdapter,
    access_status_for_exception,
    access_status_for_response,
)
from .evidence import sha256_bytes, source_record_id


COURT_MONITOR_ENDPOINT_ID = "court_judicial_interpretation_monitor"
COURT_MONITOR_SOURCE_SYSTEM = "court_judicial_interpretation_monitor"
COURT_DIRECTORY_URL = "https://www.court.gov.cn/fabu/gengduo/16.html"

_TOTAL_RE = re.compile(r"共\s*(\d+)\s*篇文章")
_DETAIL_ID_RE = re.compile(r"/(?:fabu|zixun)/xiangqing/(\d+)\.html(?:$|[?#])")
_PAGE_RE = re.compile(r"/fabu/gengduo/16(?:_(\d+))?\.html(?:$|[?#])")
_DATE_RE = re.compile(r"^(?:19|20)\d{2}-\d{2}-\d{2}$")
_FILENO_RE = re.compile(
    r"(?:法释|法发|法复|法答|法办)\s*[〔\[\(（]\s*\d{4}\s*[〕\]\)）]\s*\d+\s*号"
)
_ARTICLE_RE = re.compile(r"(?m)^[ \t\u3000]*第[一二三四五六七八九十百零〇]+条(?:\s|　)")
_INSTRUMENT_TITLE_RE = re.compile(
    r"最高人民法院\s*关于.{2,100}?(?:规定|解释|批复|决定|意见|通知)"
)
_REFERENCE_TOKENS = ("答记者问", "新闻发布会", "解读", "访谈", "发布会", "典型案例")
_RELEASE_TOKENS = ("发布", "出台", "全文", "新闻", "负责人")
_REVIEW_CLASSES = {"compound_instruments", "unknown_structure"}


class CourtMonitorError(ValueError):
    """Raised for a structural failure in the monitored official pages."""


def _normalized_lines(text: str) -> str:
    return "\n".join(
        line
        for line in (
            re.sub(r"[ \t\u3000]+", " ", item).strip()
            for item in str(text).splitlines()
        )
        if line
    )


def _page_url(entry_url: str, page_number: int) -> str:
    if page_number == 1:
        return entry_url
    if not entry_url.endswith(".html"):
        raise CourtMonitorError(f"unsupported court directory URL: {entry_url}")
    return f"{entry_url[:-5]}_{page_number}.html"


def _headers_from_cache(cache: dict[str, Any] | None) -> dict[str, str]:
    validators = (cache or {}).get("http_validators") or {}
    return {
        name: str(value)
        for name, value in {
            "If-None-Match": validators.get("etag"),
            "If-Modified-Since": validators.get("last_modified"),
        }.items()
        if value
    }


def _validators(headers: dict[str, Any]) -> dict[str, str]:
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    return {
        key: value
        for key, value in {
            "etag": lowered.get("etag"),
            "last_modified": lowered.get("last-modified"),
        }.items()
        if value
    }


def _body_node(soup: BeautifulSoup) -> Tag:
    candidates: list[Tag] = []
    seen: set[int] = set()
    for selector in ("#zoom", ".txt_txt"):
        for node in soup.select(selector):
            if isinstance(node, Tag) and id(node) not in seen:
                candidates.append(node)
                seen.add(id(node))
    if len(candidates) != 1:
        raise CourtMonitorError(
            f"official body selector must resolve to one node, got {len(candidates)}"
        )
    return candidates[0]


def classify_candidate(title: str, text: str) -> tuple[str, dict[str, Any]]:
    """Return a conservative page-shape classification and its observable signals."""

    filenos = sorted(set(re.sub(r"\s+", "", item) for item in _FILENO_RE.findall(text)))
    article_count = len(_ARTICLE_RE.findall(text))
    first_article_count = len(re.findall(r"(?m)^[ \t\u3000]*第一条(?:\s|　)", text))
    instrument_titles = sorted(
        set(re.sub(r"\s+", "", item) for item in _INSTRUMENT_TITLE_RE.findall(text))
    )
    signals = {
        "filenos": filenos,
        "article_count": article_count,
        "first_article_count": first_article_count,
        "instrument_title_count": len(instrument_titles),
        "reference_tokens": [token for token in _REFERENCE_TOKENS if token in f"{title}\n{text}"],
    }

    if len(filenos) > 1 or first_article_count > 1 or len(instrument_titles) > 1:
        return "compound_instruments", signals
    has_instrument = bool(filenos or article_count or instrument_titles)
    if has_instrument:
        first_marker_positions = [
            position
            for position in (
                text.find(filenos[0]) if filenos else -1,
                text.find("第一条") if article_count else -1,
                text.find("最高人民法院关于") if instrument_titles else -1,
            )
            if position >= 0
        ]
        prefix = text[: min(first_marker_positions) if first_marker_positions else 0]
        if any(token in title or token in prefix for token in _RELEASE_TOKENS):
            return "release_with_instrument", signals
        return "single_instrument", signals
    if signals["reference_tokens"]:
        return "reference_or_news", signals
    return "unknown_structure", signals


class CourtJudicialInterpretationMonitorAdapter(HttpHtmlAdapter):
    """Full-directory monitor with conditional list and detail requests."""

    def __init__(self) -> None:
        super().__init__()
        self.stats.update(
            {
                "list_requests": 0,
                "list_not_modified": 0,
                "list_parse_failures": 0,
                "detail_requests": 0,
                "detail_not_modified": 0,
                "detail_reused": 0,
            }
        )

    def _pause(self) -> None:
        minimum = float(os.environ.get("CSRC_COURT_MONITOR_DELAY_MIN", "2"))
        maximum = float(os.environ.get("CSRC_COURT_MONITOR_DELAY_MAX", "5"))
        if minimum < 0 or maximum < minimum:
            raise ValueError("court monitor delay must satisfy 0 <= min <= max")
        if maximum:
            self._sleep(random.uniform(minimum, maximum))

    def healthcheck(self, endpoint: dict[str, Any]) -> dict[str, Any]:
        """Use a fixed detail page; list page ownership stays with discovery."""

        health_url = str(endpoint.get("healthcheck_url") or "").strip()
        if not health_url:
            raise CourtMonitorError("court monitor requires healthcheck_url")
        started = __import__("time").monotonic()
        try:
            response = self._get(health_url)
        except requests.RequestException as exc:
            return {
                "access_status": access_status_for_exception(exc),
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "elapsed_seconds": round(__import__("time").monotonic() - started, 3),
            }
        return {
            "access_status": access_status_for_response(response),
            "status_code": response.status_code,
            "final_url": response.url,
            "content_type": response.headers.get("Content-Type"),
            "content_length": len(response.content),
            "_body": response.content,
            "elapsed_seconds": round(__import__("time").monotonic() - started, 3),
        }

    @staticmethod
    def _parse_listing(
        body: bytes,
        *,
        page_url: str,
        page_number: int,
    ) -> tuple[list[dict[str, Any]], int | None, int | None]:
        soup = BeautifulSoup(body, "html.parser")
        containers = soup.select(".sec_list > ul")
        if len(containers) != 1:
            raise CourtMonitorError(
                f"page {page_number}: .sec_list > ul must occur once, got {len(containers)}"
            )
        members: list[dict[str, Any]] = []
        for index, node in enumerate(containers[0].find_all("li", recursive=False), start=1):
            anchors = [
                anchor
                for anchor in node.find_all("a", href=True)
                if _DETAIL_ID_RE.search(
                    urljoin(page_url, str(anchor.get("href") or ""))
                )
            ]
            dates = node.select("i.date")
            if len(anchors) != 1 or len(dates) != 1:
                raise CourtMonitorError(
                    f"page {page_number} item {index}: expected one detail link and date"
                )
            url = urljoin(page_url, str(anchors[0].get("href") or ""))
            match = _DETAIL_ID_RE.search(url)
            title = anchors[0].get_text(" ", strip=True)
            date = dates[0].get_text(" ", strip=True)
            if match is None or not title or not _DATE_RE.fullmatch(date):
                raise CourtMonitorError(
                    f"page {page_number} item {index}: invalid id, title, or date"
                )
            members.append(
                {
                    "upstream_id": match.group(1),
                    "url": url,
                    "title": title,
                    "listing_date": date,
                    "page_number": page_number,
                    "position": index,
                }
            )

        page_text = soup.get_text(" ", strip=True)
        total_match = _TOTAL_RE.search(page_text)
        total = int(total_match.group(1)) if total_match else None
        page_numbers = [
            int(match.group(1) or 1)
            for anchor in soup.find_all("a", href=True)
            if (match := _PAGE_RE.search(urljoin(page_url, str(anchor.get("href") or ""))))
        ]
        last_page = max(page_numbers) if page_numbers else None
        return members, total, last_page

    def _listing_page(
        self,
        *,
        url: str,
        page_number: int,
        cache: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        self.stats["list_requests"] = int(self.stats["list_requests"]) + 1
        try:
            response = self._get(url, headers=_headers_from_cache(cache))
            if response.status_code == HTTPStatus.NOT_MODIFIED:
                self.stats["list_not_modified"] = int(self.stats["list_not_modified"]) + 1
                if not cache or not isinstance(cache.get("members"), list):
                    raise CourtMonitorError(
                        f"page {page_number}: HTTP 304 received without cached membership"
                    )
                return dict(cache), None, None
            response.raise_for_status()
            members, total, last_page = self._parse_listing(
                response.content,
                page_url=response.url,
                page_number=page_number,
            )
            parsed = {
                "url": url,
                "final_url": response.url,
                "page_number": page_number,
                "members": members,
                "reported_total": total,
                "last_page": last_page,
                "response_sha256": sha256_bytes(response.content),
                "http_validators": _validators(dict(response.headers)),
                "verified_at": utc_now_iso(),
            }
            raw = {
                "url": url,
                "final_url": response.url,
                "status_code": response.status_code,
                "content_type": response.headers.get("Content-Type"),
                "body": response.content,
            }
            return parsed, raw, None
        except (requests.RequestException, CourtMonitorError) as exc:
            self.stats["list_parse_failures"] = int(self.stats["list_parse_failures"]) + 1
            return (
                None,
                None,
                {
                    "url": url,
                    "page_number": page_number,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )

    def discover(
        self,
        endpoint: dict[str, Any],
        registry: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> dict[str, Any]:
        del registry
        entry_url = str(endpoint["url"])
        old_pages = checkpoint.get("listing_pages") or {}
        new_pages: dict[str, dict[str, Any]] = {}
        raw_pages: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []

        first, raw, failure = self._listing_page(
            url=entry_url,
            page_number=1,
            cache=old_pages.get("1"),
        )
        if raw:
            raw_pages.append(raw)
        if failure:
            failures.append(failure)
        if first is None:
            return {
                "items": [],
                "raw_pages": raw_pages,
                "discovery_status": "incomplete",
                "pages_completed": 0,
                "reported_total": None,
                "raw_hit_count": 0,
                "pagination_links_seen": 0,
                "completeness_evidence": "page_1_unavailable",
                "query_execution": "full_directory_enumeration",
                "result_limit_reached": False,
                "failures": failures,
            }
        new_pages["1"] = first
        reported_total = first.get("reported_total")
        last_page = first.get("last_page")
        if not isinstance(reported_total, int) or reported_total <= 0:
            failures.append(
                {
                    "url": entry_url,
                    "page_number": 1,
                    "error_type": "CourtMonitorError",
                    "error_message": "page 1 does not expose a positive article total",
                }
            )
            expected_pages = 1
        else:
            expected_pages = math.ceil(reported_total / int(endpoint.get("page_size") or 20))
        if last_page != expected_pages:
            failures.append(
                {
                    "url": entry_url,
                    "page_number": 1,
                    "error_type": "CourtMonitorError",
                    "error_message": (
                        f"pagination last page {last_page!r} does not match "
                        f"reported total {reported_total!r} ({expected_pages} pages)"
                    ),
                }
            )

        for page_number in range(2, expected_pages + 1):
            url = _page_url(entry_url, page_number)
            parsed, raw, failure = self._listing_page(
                url=url,
                page_number=page_number,
                cache=old_pages.get(str(page_number)),
            )
            if raw:
                raw_pages.append(raw)
            if failure:
                failures.append(failure)
                continue
            if parsed:
                new_pages[str(page_number)] = parsed

        members: list[dict[str, Any]] = []
        page_size = int(endpoint.get("page_size") or 20)
        for page_number in range(1, expected_pages + 1):
            page = new_pages.get(str(page_number))
            if not page:
                continue
            page_members = page.get("members") or []
            expected_count = (
                reported_total - page_size * (expected_pages - 1)
                if page_number == expected_pages and isinstance(reported_total, int)
                else page_size
            )
            if len(page_members) != expected_count:
                failures.append(
                    {
                        "url": page.get("url"),
                        "page_number": page_number,
                        "error_type": "CourtMonitorError",
                        "error_message": (
                            f"page member count {len(page_members)} != expected {expected_count}"
                        ),
                    }
                )
            members.extend(page_members)

        id_counts = Counter(str(item["upstream_id"]) for item in members)
        duplicates = sorted(key for key, count in id_counts.items() if count > 1)
        if duplicates:
            failures.append(
                {
                    "url": entry_url,
                    "error_type": "CourtMonitorError",
                    "error_message": "duplicate page ids: " + ", ".join(duplicates[:20]),
                }
            )
        unique_members = {
            str(item["upstream_id"]): item for item in members
        }
        if isinstance(reported_total, int) and len(unique_members) != reported_total:
            failures.append(
                {
                    "url": entry_url,
                    "error_type": "CourtMonitorError",
                    "error_message": (
                        f"unique id count {len(unique_members)} != reported total {reported_total}"
                    ),
                }
            )

        old_members = {
            str(item.get("upstream_id")): item
            for page in old_pages.values()
            for item in (page.get("members") or [])
            if item.get("upstream_id")
        }
        items: list[dict[str, Any]] = []
        observed_at = utc_now_iso()
        for page_id, member in unique_members.items():
            previous = old_members.get(page_id) or {}
            member["first_seen_at"] = previous.get("first_seen_at") or observed_at
            member["last_seen_at"] = observed_at
            metadata_changed = any(
                previous.get(key) != member.get(key)
                for key in ("title", "listing_date", "url")
            ) if previous else False
            items.append(
                {
                    "url": member["url"],
                    "title": member["title"],
                    "upstream_id": page_id,
                    "in_scope": True,
                    "matched_query_terms": [],
                    "listing_date": member["listing_date"],
                    "listing_page": member["page_number"],
                    "listing_position": member["position"],
                    "listing_metadata_changed": metadata_changed,
                    "detail_http_validators": (
                        (
                            checkpoint.get("records", {}).get(
                                source_record_id(
                                    COURT_MONITOR_SOURCE_SYSTEM,
                                    upstream_id=page_id,
                                ),
                                {},
                            )
                        ).get("http_validators")
                        or {}
                    ),
                    "discovery_evidence": [
                        {
                            "endpoint_id": endpoint["endpoint_id"],
                            "method": "full_directory_enumeration",
                            "list_url": _page_url(entry_url, int(member["page_number"])),
                            "page_number": member["page_number"],
                            "position": member["position"],
                            "title": member["title"],
                            "listing_date": member["listing_date"],
                        }
                    ],
                }
            )
        items.sort(key=lambda item: (int(item["listing_page"]), int(item["listing_position"])))

        checkpoint["listing_pages"] = {**old_pages, **new_pages}
        complete = not failures and len(new_pages) == expected_pages
        if complete:
            checkpoint["last_complete_listing_at"] = utc_now_iso()
            checkpoint["last_complete_reported_total"] = reported_total
            checkpoint["last_complete_page_ids"] = sorted(unique_members)
        return {
            "items": items,
            "raw_pages": raw_pages,
            "discovery_status": "complete" if complete else "incomplete",
            "pages_completed": len(new_pages),
            "reported_total": reported_total,
            "raw_hit_count": len(members),
            "filtered_count": 0,
            "pagination_links_seen": max(0, expected_pages - 1),
            "completeness_evidence": (
                "reported_total_tail_page_unique_ids_and_dom"
                if complete
                else "directory_validation_failed"
            ),
            "query_execution": "full_directory_enumeration",
            "result_limit_reached": False,
            "queries_completed": expected_pages if complete else len(new_pages),
            "queries_total": expected_pages,
            "failures": failures,
        }

    def fetch(
        self,
        endpoint: dict[str, Any],
        item: dict[str, Any],
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        refresh_details = bool(endpoint.get("_refresh_details"))
        if previous is not None and not refresh_details and not item.get(
            "listing_metadata_changed"
        ):
            self.stats["detail_reused"] = int(self.stats["detail_reused"]) + 1
            return {
                "not_modified": True,
                "reused_without_request": True,
                "status_code": None,
                "final_url": item["url"],
                "headers": {},
            }

        validators = item.get("detail_http_validators") or (
            ((previous or {}).get("source") or {}).get("http_validators") or {}
        )
        headers = (
            {}
            if item.get("listing_metadata_changed")
            else {
                name: value
                for name, value in {
                    "If-None-Match": validators.get("etag"),
                    "If-Modified-Since": validators.get("last_modified"),
                }.items()
                if value
            }
        )
        self.stats["detail_requests"] = int(self.stats["detail_requests"]) + 1
        response = self._get(str(item["url"]), headers=headers)
        if response.status_code == HTTPStatus.NOT_MODIFIED:
            self.stats["detail_not_modified"] = int(self.stats["detail_not_modified"]) + 1
            return {
                "not_modified": True,
                "detail_requested": True,
                "status_code": response.status_code,
                "final_url": response.url,
                "headers": dict(response.headers),
            }
        response.raise_for_status()
        return {
            "body": response.content,
            "status_code": response.status_code,
            "content_type": response.headers.get("Content-Type"),
            "final_url": response.url,
            "headers": dict(response.headers),
            "detail_requested": True,
        }

    def parse(
        self,
        endpoint: dict[str, Any],
        item: dict[str, Any],
        fetched: dict[str, Any],
    ) -> dict[str, Any]:
        del endpoint
        body_bytes = bytes(fetched.get("body") or b"")
        content_type = str(fetched.get("content_type") or "").lower()
        if "html" not in content_type and not body_bytes.lstrip().startswith(b"<"):
            raise CourtMonitorError(f"unsupported court detail content type: {content_type}")
        soup = BeautifulSoup(body_bytes, "html.parser")
        title_nodes = soup.select("div.title")
        if len(title_nodes) != 1:
            raise CourtMonitorError(f"div.title must occur once, got {len(title_nodes)}")
        page_title = title_nodes[0].get_text(" ", strip=True)
        body = _body_node(soup)
        for node in body.select("script,style,noscript,nav,header,footer,form"):
            node.decompose()
        plain_text = _normalized_lines(body.get_text("\n", strip=True))
        if len(plain_text) < 20:
            raise CourtMonitorError("court detail body is empty or too short")
        candidate_type, signals = classify_candidate(page_title, plain_text)
        filenos = list(signals["filenos"])
        metadata = {
            "name": page_title,
            "listing_title": str(item.get("title") or ""),
            "listing_date": str(item.get("listing_date") or ""),
            "publisher": "最高人民法院",
            "pub_org": "最高人民法院",
            "region": "全国",
            "material_lane": "clue",
            "document_type": "monitor_clue",
            "official_page_id": str(item["upstream_id"]),
            "official_page_title": page_title,
            "official_page_url": str(fetched.get("final_url") or item["url"]),
            "candidate_type": candidate_type,
            "candidate_signals": signals,
            "filenos": filenos,
            "fileno": filenos[0] if len(filenos) == 1 else None,
            "article_count": int(signals["article_count"]),
            "review_status": "pending_review",
            "requires_structural_review": candidate_type in _REVIEW_CLASSES,
        }
        html = "<div>" + "".join(
            f"<p>{escape(line)}</p>" for line in plain_text.splitlines()
        ) + "</div>"
        return {
            "metadata": metadata,
            "plain_text": plain_text,
            "content_html": html,
            "assets": [],
            "http_metadata": {
                "status_code": fetched.get("status_code"),
                "content_type": fetched.get("content_type"),
                "final_url": fetched.get("final_url"),
                "headers": {
                    str(key): str(value)
                    for key, value in sorted((fetched.get("headers") or {}).items())
                },
            },
        }


__all__ = [
    "COURT_DIRECTORY_URL",
    "COURT_MONITOR_ENDPOINT_ID",
    "COURT_MONITOR_SOURCE_SYSTEM",
    "CourtJudicialInterpretationMonitorAdapter",
    "CourtMonitorError",
    "classify_candidate",
]
