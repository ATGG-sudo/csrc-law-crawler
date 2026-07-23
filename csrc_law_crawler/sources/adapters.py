"""Minimal adapters for public multi-source acquisition."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http import HTTPStatus
import json
import math
import random
import re
import time
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

from config import USER_AGENT
from settings import SETTINGS

from .evidence import canonical_final_url
from .registry import endpoint_query_terms


PAGE_LIMIT = 500
STRUCTURED_PAGE_LIMIT = 5000
AMAC_SUBJECT_DELAY_MIN = 0.25
AMAC_SUBJECT_DELAY_MAX = 0.7
CONTENT_EXTENSIONS = (".html", ".htm", ".shtml", ".pdf", ".doc", ".docx", ".xls", ".xlsx")
ASSET_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar")
DATE_RE = re.compile(r"(?:19|20)\d{2}[-年./]\d{1,2}[-月./]\d{1,2}日?")
PAGE_QUERY_RE = re.compile(r"(?:^|[?&])(?:page|pageno|pageindex|currentpage)=\d+", re.I)
PAGE_PATH_RE = re.compile(r"(?:index|list)[_-]?\d+\.(?:s?html?)$", re.I)
SKIP_TITLES = {
    "首页",
    "上一页",
    "下一页",
    "下页",
    "上页",
    "尾页",
    "末页",
    "更多",
    "返回",
    "登录",
    "注册",
}


class SourceAdapter(Protocol):
    def healthcheck(self, endpoint: dict[str, Any]) -> dict[str, Any]: ...

    def discover(
        self,
        endpoint: dict[str, Any],
        registry: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> dict[str, Any]: ...

    def fetch(
        self,
        endpoint: dict[str, Any],
        item: dict[str, Any],
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def parse(
        self,
        endpoint: dict[str, Any],
        item: dict[str, Any],
        fetched: dict[str, Any],
    ) -> dict[str, Any]: ...


def access_status_for_response(response: requests.Response) -> str:
    if response.status_code in {401, 407}:
        return "auth_required"
    if response.status_code == 403:
        return "blocked"
    if response.status_code >= 400:
        return "http_error"
    return "reachable"


def access_status_for_exception(exc: BaseException) -> str:
    if isinstance(exc, requests.exceptions.SSLError):
        return "tls_error"
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    return "network_error"


def _fetched_api_record(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "body": json.dumps(item["api_record"], ensure_ascii=False, sort_keys=True).encode("utf-8"),
        "status_code": HTTPStatus.OK.value,
        "content_type": "application/json",
        "final_url": item["url"],
        "headers": {},
    }


def subject_seed_matches_endpoint(endpoint: dict[str, Any], seed: dict[str, Any]) -> bool:
    host = (urlsplit(endpoint["url"]).hostname or "").lower()
    target_prefix = "amac_" if host == "gs.amac.org.cn" else "eid"
    return not seed.get("ambiguous") and any(
        str(target).startswith(target_prefix) for target in seed.get("query_targets") or []
    )


class HttpHtmlAdapter:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            }
        )
        self._health_response: requests.Response | None = None
        self._health_request_url: str | None = None
        self.stats: dict[str, int | float] = {
            "request_attempts": 0,
            "retries": 0,
            "request_seconds": 0.0,
            "sleep_seconds": 0.0,
        }

    def _sleep(self, seconds: float) -> None:
        self.stats["sleep_seconds"] += seconds
        time.sleep(seconds)

    @staticmethod
    def _retry_after(value: str | None, fallback: float) -> float:
        if not value:
            return fallback
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return fallback
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())

    def _pause(self) -> None:
        if SETTINGS.delay_max > 0:
            self._sleep(random.uniform(SETTINGS.delay_min, SETTINGS.delay_max))

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        last_error: BaseException | None = None
        method = str(kwargs.pop("method", "GET"))
        for attempt in range(1, SETTINGS.max_retries + 1):
            self._pause()
            try:
                started = time.monotonic()
                self.stats["request_attempts"] += 1
                try:
                    response = self.session.request(
                        method,
                        url,
                        timeout=60,
                        allow_redirects=True,
                        **kwargs,
                    )
                finally:
                    self.stats["request_seconds"] += time.monotonic() - started
                if response.status_code >= 500 or response.status_code in {408, 429}:
                    last_error = requests.HTTPError(
                        f"retryable HTTP {response.status_code}", response=response
                    )
                    if attempt < SETTINGS.max_retries:
                        self.stats["retries"] += 1
                        wait = self._retry_after(
                            response.headers.get("Retry-After"),
                            SETTINGS.retry_backoff_base * attempt,
                        )
                        response.close()
                        self._sleep(wait)
                        continue
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt < SETTINGS.max_retries:
                    self.stats["retries"] += 1
                    self._sleep(SETTINGS.retry_backoff_base * attempt)
                    continue
                raise
        if isinstance(last_error, BaseException):
            raise last_error
        raise RuntimeError(f"request failed without response: {url}")

    def healthcheck(self, endpoint: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        try:
            response = self._get(endpoint["url"])
        except requests.RequestException as exc:
            return {
                "access_status": access_status_for_exception(exc),
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        self._health_response = response
        self._health_request_url = endpoint["url"]
        return {
            "access_status": access_status_for_response(response),
            "status_code": response.status_code,
            "final_url": response.url,
            "content_type": response.headers.get("Content-Type"),
            "content_length": len(response.content),
            "_body": response.content,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    def _initial_response(self, url: str) -> requests.Response | None:
        response = self._health_response
        requested = self._health_request_url
        if response is None or canonical_final_url(
            str(requested or response.url)
        ) != canonical_final_url(url):
            return None
        self._health_response = None
        self._health_request_url = None
        return response

    @staticmethod
    def _is_page_link(text: str, url: str) -> bool:
        normalized = re.sub(r"\s+", "", text)
        return (
            normalized in {"下一页", "下页", "尾页", ">", "»"}
            or bool(PAGE_QUERY_RE.search(url))
            or bool(PAGE_PATH_RE.search(urlsplit(url).path))
        )

    @staticmethod
    def _same_scope_path(entry_url: str, candidate_url: str) -> bool:
        entry_path = urlsplit(entry_url).path or "/"
        if not entry_path.endswith("/"):
            entry_path = entry_path.rsplit("/", 1)[0] + "/"
        return entry_path == "/" or urlsplit(candidate_url).path.startswith(entry_path)

    @staticmethod
    def _candidate_link(
        *,
        entry_url: str,
        href: str,
        text: str,
        parent_text: str,
        query_terms: list[str],
    ) -> bool:
        if not text or text in SKIP_TITLES or len(text) < 4:
            return False
        if canonical_final_url(href) == canonical_final_url(entry_url):
            return False
        if not HttpHtmlAdapter._same_scope_path(entry_url, href):
            return False
        path = urlsplit(href).path.lower()
        if path.endswith(CONTENT_EXTENSIONS):
            return True
        if DATE_RE.search(parent_text):
            return True
        if any(term.lower() in text.lower() for term in query_terms):
            return True
        return bool(re.search(r"/(?:t?20\d{2}|c\d{4,})/", path))

    @staticmethod
    def _looks_like_article(soup: BeautifulSoup) -> bool:
        if soup.find("article") or soup.find(
            "meta", attrs={"name": re.compile("ArticleTitle", re.I)}
        ):
            return True
        heading = soup.find("h1")
        if not heading:
            return False
        for selector in (".TRS_Editor", ".article-content", ".content", "#zoom"):
            node = soup.select_one(selector)
            if node and len(node.get_text(" ", strip=True)) >= 100:
                return True
        return False

    @staticmethod
    def _matching_query_terms(text: str, query_terms: list[str]) -> list[str]:
        lowered = text.casefold()
        return [term for term in query_terms if term.casefold() in lowered]

    def discover(
        self,
        endpoint: dict[str, Any],
        registry: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> dict[str, Any]:
        del checkpoint
        entry_url = endpoint["url"]
        entry_host = (urlsplit(entry_url).hostname or "").lower()
        query_terms = endpoint_query_terms(registry, endpoint)
        filter_required = endpoint["scope_mode"] in {"catalog_filter", "query_exhaustive"}
        pending: deque[str] = deque([entry_url])
        seen_pages: set[str] = set()
        items: dict[str, dict[str, Any]] = {}
        raw_pages: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        hit_limit = False
        pagination_links: set[str] = set()
        single_article = False

        while pending:
            if len(seen_pages) >= PAGE_LIMIT:
                hit_limit = True
                break
            page_url = pending.popleft()
            page_key = canonical_final_url(page_url)
            if page_key in seen_pages:
                continue
            seen_pages.add(page_key)
            try:
                response = self._initial_response(page_url) or self._get(page_url)
                response.raise_for_status()
            except requests.RequestException as exc:
                failures.append(
                    {
                        "url": page_url,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    }
                )
                continue
            raw_pages.append(
                {
                    "url": page_url,
                    "final_url": response.url,
                    "status_code": response.status_code,
                    "content_type": response.headers.get("Content-Type"),
                    "body": response.content,
                }
            )
            soup = BeautifulSoup(response.content, "html.parser")
            for anchor in soup.find_all("a", href=True):
                text = anchor.get_text(" ", strip=True)
                href = urljoin(response.url, str(anchor.get("href") or ""))
                parts = urlsplit(href)
                if parts.scheme not in {"http", "https"}:
                    continue
                if (parts.hostname or "").lower() != entry_host:
                    continue
                if self._is_page_link(text, href):
                    if (
                        self._same_scope_path(entry_url, href)
                        and canonical_final_url(href) not in seen_pages
                    ):
                        pagination_links.add(canonical_final_url(href))
                        pending.append(href)
                    continue
                parent_text = anchor.parent.get_text(" ", strip=True) if anchor.parent else text
                if not self._candidate_link(
                    entry_url=entry_url,
                    href=href,
                    text=text,
                    parent_text=parent_text,
                    query_terms=query_terms,
                ):
                    continue
                key = canonical_final_url(href)
                evidence = {
                    "endpoint_id": endpoint["endpoint_id"],
                    "list_url": response.url,
                    "title": text,
                }
                matched_query_terms = self._matching_query_terms(
                    f"{text} {parent_text}", query_terms
                )
                existing = items.get(key)
                if existing:
                    existing["discovery_evidence"].append(evidence)
                else:
                    items[key] = {
                        "url": href,
                        "title": text,
                        "upstream_id": None,
                        "in_scope": (True if not filter_required or matched_query_terms else None),
                        "matched_query_terms": matched_query_terms,
                        "discovery_evidence": [evidence],
                    }

            if page_url == entry_url and not items and self._looks_like_article(soup):
                single_article = True
                title = soup.find("h1") or soup.find("title")
                title_text = (
                    title.get_text(" ", strip=True) if title else endpoint["profiles"][0]["name"]
                )
                matched_query_terms = self._matching_query_terms(title_text, query_terms)
                items[canonical_final_url(response.url)] = {
                    "url": response.url,
                    "title": title_text,
                    "upstream_id": None,
                    "in_scope": True if not filter_required or matched_query_terms else None,
                    "matched_query_terms": matched_query_terms,
                    "discovery_evidence": [
                        {
                            "endpoint_id": endpoint["endpoint_id"],
                            "list_url": response.url,
                            "title": title.get_text(" ", strip=True) if title else None,
                        }
                    ],
                }

        root_query_without_listing = endpoint["scope_mode"] == "query_exhaustive" and urlsplit(
            entry_url
        ).path in {"", "/"}
        completeness_evidence = single_article or bool(pagination_links)
        complete = (
            not failures
            and not hit_limit
            and not root_query_without_listing
            and completeness_evidence
        )
        return {
            "items": list(items.values()),
            "raw_pages": raw_pages,
            "discovery_status": "complete" if complete else "incomplete",
            "pages_completed": len(raw_pages),
            "reported_total": None,
            "raw_hit_count": len(items),
            "filtered_count": None,
            "pagination_links_seen": len(pagination_links),
            "single_article": single_article,
            "completeness_evidence": (
                "single_article"
                if single_article
                else "pagination"
                if pagination_links
                else "unproven_single_page_directory"
            ),
            "query_execution": "local_over_enumerated_catalog",
            "result_limit_reached": hit_limit,
            "queries_completed": len(query_terms) if complete else 0,
            "queries_total": len(query_terms),
            "failures": failures,
        }

    def fetch(
        self,
        endpoint: dict[str, Any],
        item: dict[str, Any],
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del endpoint
        validators = ((previous or {}).get("source") or {}).get("http_validators") or {}
        headers = {
            name: value
            for name, value in {
                "If-None-Match": validators.get("etag"),
                "If-Modified-Since": validators.get("last_modified"),
            }.items()
            if value
        }
        response = self._get(item["url"], headers=headers)
        if response.status_code == HTTPStatus.NOT_MODIFIED:
            return {
                "not_modified": True,
                "status_code": response.status_code,
                "final_url": response.url,
                "headers": dict(response.headers),
            }
        response.raise_for_status()
        return {
            "body": response.content,
            "status_code": response.status_code,
            "content_type": response.headers.get("Content-Type") or "",
            "final_url": response.url,
            "headers": dict(response.headers),
        }

    def parse(
        self,
        endpoint: dict[str, Any],
        item: dict[str, Any],
        fetched: dict[str, Any],
    ) -> dict[str, Any]:
        content_type = str(fetched.get("content_type") or "").lower()
        final_url = str(fetched["final_url"])
        if any(urlsplit(final_url).path.lower().endswith(ext) for ext in ASSET_EXTENSIONS):
            title = item.get("title") or final_url.rsplit("/", 1)[-1]
            return {
                "metadata": {
                    "name": title,
                    "publisher": endpoint["profiles"][0].get("publisher"),
                    "document_type": endpoint["profiles"][0].get("material_nature"),
                },
                "plain_text": "",
                "content_html": "",
                "assets": [
                    {
                        "source_url": final_url,
                        "label": title,
                        "file_name": final_url.rsplit("/", 1)[-1],
                        "download_status": "discovered",
                        "_prefetched_body": bytes(fetched["body"]),
                        "_prefetched_content_type": content_type,
                        "_prefetched_final_url": final_url,
                    }
                ],
            }
        if "html" not in content_type and not fetched["body"].lstrip().startswith(b"<"):
            raise ValueError(f"unsupported detail content type: {content_type}")
        soup = BeautifulSoup(fetched["body"], "html.parser")
        title_node = soup.find("h1") or soup.find("title")
        title = title_node.get_text(" ", strip=True) if title_node else str(item.get("title") or "")
        body = None
        for selector in (
            "article",
            ".TRS_Editor",
            ".article-content",
            ".article_content",
            ".content",
            "#zoom",
            ".zw",
        ):
            candidate = soup.select_one(selector)
            if candidate and len(candidate.get_text(" ", strip=True)) > 20:
                body = candidate
                break
        body = body or soup.body or soup
        for node in body.select("script,style,noscript,nav,header,footer,form"):
            node.decompose()
        plain_text = body.get_text("\n", strip=True)
        if len(plain_text) < 20:
            raise ValueError("detail body is empty or too short")
        page_text = soup.get_text(" ", strip=True)
        date_match = DATE_RE.search(page_text)
        assets: list[dict[str, Any]] = []
        for anchor in body.find_all("a", href=True):
            asset_url = urljoin(final_url, str(anchor.get("href") or ""))
            if not urlsplit(asset_url).path.lower().endswith(ASSET_EXTENSIONS):
                continue
            assets.append(
                {
                    "source_url": asset_url,
                    "label": anchor.get_text(" ", strip=True) or asset_url.rsplit("/", 1)[-1],
                    "file_name": asset_url.rsplit("/", 1)[-1],
                    "download_status": "discovered",
                }
            )
        return {
            "metadata": {
                "name": title,
                "publisher": endpoint["profiles"][0].get("publisher"),
                "pub_date": date_match.group(0) if date_match else None,
                "document_type": endpoint["profiles"][0].get("material_nature"),
                "region": endpoint["profiles"][0].get("region"),
            },
            "plain_text": plain_text,
            "content_html": str(body),
            "assets": assets,
        }


class CsrcAdapter(HttpHtmlAdapter):
    @staticmethod
    def _channel_id(body: bytes, endpoint_url: str) -> str:
        soup = BeautifulSoup(body, "html.parser")
        meta = soup.find("meta", attrs={"name": re.compile(r"^channelid$", re.I)})
        if meta and meta.get("content"):
            return str(meta["content"])
        match = re.search(r"[?&]channelid=([a-f0-9]{32})", endpoint_url, re.I)
        if match:
            return match.group(1)
        raise ValueError("CSRC channel id not found")

    def discover(
        self,
        endpoint: dict[str, Any],
        registry: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> dict[str, Any]:
        del checkpoint
        entry = self._initial_response(endpoint["url"]) or self._get(endpoint["url"])
        entry.raise_for_status()
        channel_id = self._channel_id(entry.content, endpoint["url"])
        page_size = 20
        page = 1
        total = None
        items: list[dict[str, Any]] = []
        query_terms = endpoint_query_terms(registry, endpoint)
        filter_required = endpoint["scope_mode"] in {"catalog_filter", "query_exhaustive"}
        raw_pages: list[dict[str, Any]] = [
            {
                "url": endpoint["url"],
                "final_url": entry.url,
                "status_code": entry.status_code,
                "content_type": entry.headers.get("Content-Type"),
                "body": entry.content,
            }
        ]
        while total is None or page <= math.ceil(total / page_size):
            api_url = urljoin(entry.url, f"/searchList/{channel_id}")
            response = self._get(
                api_url,
                params={
                    "_isAgg": "true",
                    "_isJson": "true",
                    "_pageSize": str(page_size),
                    "_template": "index",
                    "page": str(page),
                },
                headers={"Referer": entry.url},
            )
            response.raise_for_status()
            payload = response.json().get("data") or {}
            total = int(payload.get("total") or 0)
            raw_pages.append(
                {
                    "url": response.request.url,
                    "final_url": response.url,
                    "status_code": response.status_code,
                    "content_type": response.headers.get("Content-Type"),
                    "body": response.content,
                }
            )
            for record in payload.get("results") or []:
                url = urljoin(entry.url, str(record.get("url") or ""))
                manuscript_id = str(record.get("manuscriptId") or "") or None
                matched_query_terms = self._matching_query_terms(
                    json.dumps(record, ensure_ascii=False), query_terms
                )
                items.append(
                    {
                        "url": url,
                        "title": record.get("title") or record.get("subTitle"),
                        "upstream_id": manuscript_id,
                        "api_record": record,
                        "in_scope": not filter_required or bool(matched_query_terms),
                        "matched_query_terms": matched_query_terms,
                        "discovery_evidence": [
                            {
                                "endpoint_id": endpoint["endpoint_id"],
                                "list_url": response.url,
                                "page": page,
                                "title": record.get("title"),
                            }
                        ],
                    }
                )
            page += 1
            if page > STRUCTURED_PAGE_LIMIT:
                return {
                    "items": items,
                    "raw_pages": raw_pages,
                    "discovery_status": "incomplete",
                    "pages_completed": page - 1,
                    "reported_total": total,
                    "raw_hit_count": len(items),
                    "filtered_count": sum(not item["in_scope"] for item in items),
                    "query_execution": "local_over_enumerated_catalog",
                    "result_limit_reached": True,
                    "queries_completed": 0,
                    "queries_total": len(endpoint_query_terms(registry, endpoint)),
                    "failures": [],
                }
        return {
            "items": items,
            "raw_pages": raw_pages,
            "discovery_status": "complete" if len(items) == total else "incomplete",
            "pages_completed": page - 1,
            "reported_total": total,
            "raw_hit_count": len(items),
            "filtered_count": sum(not item["in_scope"] for item in items),
            "query_execution": "local_over_enumerated_catalog",
            "result_limit_reached": False,
            "queries_completed": len(endpoint_query_terms(registry, endpoint)),
            "queries_total": len(endpoint_query_terms(registry, endpoint)),
            "failures": [],
        }

    def fetch(
        self,
        endpoint: dict[str, Any],
        item: dict[str, Any],
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del endpoint, previous
        return _fetched_api_record(item)

    def parse(
        self,
        endpoint: dict[str, Any],
        item: dict[str, Any],
        fetched: dict[str, Any],
    ) -> dict[str, Any]:
        del fetched
        record = item["api_record"]
        domain_values: dict[str, Any] = {}
        for domain in record.get("domainMetaList") or []:
            for field in domain.get("resultList") or []:
                if field.get("key"):
                    domain_values[str(field["key"])] = field.get("value")
        content_html = str(record.get("contentHtml") or "")
        plain_text = str(record.get("content") or "")
        if not plain_text and content_html:
            plain_text = BeautifulSoup(content_html, "html.parser").get_text("\n", strip=True)
        if not plain_text.strip():
            raise ValueError("CSRC record has no content")
        assets = []
        for resource in record.get("resList") or []:
            asset_url = urljoin(
                item["url"], str(resource.get("filePath") or resource.get("url") or "")
            )
            assets.append(
                {
                    "source_url": asset_url,
                    "label": resource.get("title") or resource.get("fileName"),
                    "file_name": resource.get("fileName"),
                    "download_status": "discovered",
                }
            )
        return {
            "metadata": {
                "name": record.get("title") or record.get("subTitle"),
                "fileno": domain_values.get("wh") or domain_values.get("wjbh"),
                "pub_date": domain_values.get("fwrq") or record.get("publishedTimeStr"),
                "publisher": domain_values.get("fbjg")
                or domain_values.get("fwdw")
                or endpoint["profiles"][0].get("publisher"),
                "document_type": endpoint["profiles"][0].get("material_nature"),
                "region": endpoint["profiles"][0].get("region"),
                "channel_name": record.get("channelName"),
            },
            "plain_text": plain_text,
            "content_html": content_html,
            "assets": assets,
        }


class SubjectQueryAdapter(HttpHtmlAdapter):
    def _pause(self) -> None:
        self._sleep(random.uniform(AMAC_SUBJECT_DELAY_MIN, AMAC_SUBJECT_DELAY_MAX))

    def _amac_query(
        self,
        endpoint: dict[str, Any],
        seed: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
        entity_type = seed.get("entity_type")
        if entity_type == "institution" and "amac_institution" in seed.get("query_targets", []):
            api_path = "/amac-infodisc/api/pof/manager/query"
            detail_base = "/amac-infodisc/res/pof/manager/"
        elif entity_type == "product" and "amac_product" in seed.get("query_targets", []):
            api_path = "/amac-infodisc/api/pof/fund"
            detail_base = "/amac-infodisc/res/pof/fund/"
        else:
            return [], [], 0
        page = 0
        page_size = 20
        total = None
        items: list[dict[str, Any]] = []
        raw_pages: list[dict[str, Any]] = []
        while total is None or page * page_size < total:
            url = urljoin(endpoint["url"], api_path)
            response = self._get(
                url,
                method="POST",
                params={"page": page, "size": page_size},
                json={"keyword": seed["normalized_name"]},
                headers={"Referer": endpoint["url"]},
            )
            response.raise_for_status()
            payload = response.json()
            total = int(payload.get("totalElements") or 0)
            raw_pages.append(
                {
                    "url": response.request.url,
                    "final_url": response.url,
                    "status_code": response.status_code,
                    "content_type": response.headers.get("Content-Type"),
                    "body": response.content,
                }
            )
            for record in payload.get("content") or []:
                upstream_id = str(record.get("id") or "") or None
                detail_url = urljoin(endpoint["url"], detail_base + str(record.get("url") or ""))
                items.append(
                    {
                        "url": detail_url,
                        "title": record.get("managerName") or record.get("fundName"),
                        "upstream_id": f"{entity_type}:{upstream_id}" if upstream_id else None,
                        "api_record": record,
                        "subject_type": entity_type,
                        "subject_seed_id": seed["seed_id"],
                        "discovery_evidence": [
                            {
                                "endpoint_id": endpoint["endpoint_id"],
                                "query": seed["normalized_name"],
                                "seed_id": seed["seed_id"],
                                "page": page + 1,
                                "list_url": response.url,
                            }
                        ],
                    }
                )
            page += 1
            if page >= PAGE_LIMIT:
                raise RuntimeError(f"AMAC subject query result limit reached: {seed['seed_id']}")
        return items, raw_pages, total or 0

    def discover(
        self,
        endpoint: dict[str, Any],
        registry: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> dict[str, Any]:
        del registry, checkpoint
        host = (urlsplit(endpoint["url"]).hostname or "").lower()
        seeds = [
            item
            for item in endpoint.get("subject_seeds") or []
            if subject_seed_matches_endpoint(endpoint, item)
        ]
        if host != "gs.amac.org.cn":
            response = self._initial_response(endpoint["url"]) or self._get(endpoint["url"])
            return {
                "items": [],
                "raw_pages": [
                    {
                        "url": endpoint["url"],
                        "final_url": response.url,
                        "status_code": response.status_code,
                        "content_type": response.headers.get("Content-Type"),
                        "body": response.content,
                    }
                ],
                "discovery_status": "incomplete",
                "pages_completed": 1,
                "reported_total": None,
                "result_limit_reached": False,
                "failures": [
                    {
                        "url": endpoint["url"],
                        "error_type": "SubjectQueryUnavailable",
                        "error_message": "public subject query cannot be completed without site controls",
                    }
                ],
            }
        items: list[dict[str, Any]] = []
        raw_pages: list[dict[str, Any]] = []
        total = 0
        failures: list[dict[str, Any]] = []
        completed_queries = 0
        for seed in seeds:
            try:
                found, pages, count = self._amac_query(endpoint, seed)
            except requests.RequestException as exc:
                failures.append(
                    {
                        "seed_id": seed.get("seed_id"),
                        "query": seed.get("normalized_name"),
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    }
                )
                continue
            items.extend(found)
            raw_pages.extend(pages)
            total += count
            completed_queries += 1
        return {
            "items": items,
            "raw_pages": raw_pages,
            "discovery_status": "complete" if completed_queries == len(seeds) else "incomplete",
            "pages_completed": len(raw_pages),
            "reported_total": total,
            "result_limit_reached": False,
            "queries_completed": completed_queries,
            "queries_total": len(seeds),
            "failures": failures,
        }

    def fetch(
        self,
        endpoint: dict[str, Any],
        item: dict[str, Any],
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del endpoint, previous
        return _fetched_api_record(item)

    def parse(
        self,
        endpoint: dict[str, Any],
        item: dict[str, Any],
        fetched: dict[str, Any],
    ) -> dict[str, Any]:
        del fetched
        record = item["api_record"]
        entity_type = item["subject_type"]
        name = record.get("managerName") if entity_type == "institution" else record.get("fundName")
        fields = {
            key: value
            for key, value in record.items()
            if key not in {"url", "managerUrl"} and value is not None and value != ""
        }
        return {
            "metadata": {
                "name": name,
                "publisher": endpoint["profiles"][0].get("publisher"),
                "document_type": "subject_snapshot",
                "entity_type": entity_type,
                "official_id": record.get("registerNo") or record.get("fundNo"),
                "seed_id": item["subject_seed_id"],
            },
            "plain_text": json.dumps(fields, ensure_ascii=False, sort_keys=True, indent=2),
            "content_html": "",
            "assets": [],
        }


def adapter_for(name: str) -> SourceAdapter:
    if name == "csrc":
        return CsrcAdapter()
    if name == "subject_query":
        return SubjectQueryAdapter()
    if name == "court_judicial_interpretation":
        from .court_judicial_interpretation import CourtJudicialInterpretationAdapter

        return CourtJudicialInterpretationAdapter()
    if name == "court_judicial_interpretation_monitor":
        from .court_judicial_interpretation_monitor import (
            CourtJudicialInterpretationMonitorAdapter,
        )

        return CourtJudicialInterpretationMonitorAdapter()
    if name in {"http_html", "neris", "amac"}:
        return HttpHtmlAdapter()
    raise ValueError(f"unknown source adapter: {name}")


__all__ = [
    "CsrcAdapter",
    "HttpHtmlAdapter",
    "SourceAdapter",
    "SubjectQueryAdapter",
    "access_status_for_exception",
    "access_status_for_response",
    "adapter_for",
    "subject_seed_matches_endpoint",
]
