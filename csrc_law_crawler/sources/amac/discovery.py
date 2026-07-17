"""Candidate discovery for the AMAC source adapter."""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from catalog_rules import RULE_WORDS
from config import AMAC_BASE_URL, AMAC_RULES_BASE_URL

from .client import AmacClient
from .identity import canonical_url, clean_text

POLICY_SEARCH_URL = urljoin(
    AMAC_BASE_URL,
    "portal/ESSearch/wcmPolicy/getPolicyDataByKeyword_v2",
)
SITE_SEARCH_URL = urljoin(
    AMAC_BASE_URL,
    "portal/ESSearch/doc/findDocsByKeyword",
)
DEFAULT_XWFB_PAGES = 12
DEFAULT_SELF_REGULATORY_MEASURE_PAGES = 3
DEFAULT_SELF_REGULATORY_MANAGEMENT_PAGES = 0
DEFAULT_INDUSTRY_RESEARCH_PAGES = 0
DEFAULT_XWFB_SECTIONS = [
    ("通知公告", "xwfb/tzgg/"),
    ("协会要闻", "xwfb/xhyw/"),
]
DEFAULT_SELF_REGULATORY_MEASURE_SECTIONS = [
    ("自律措施", "zlgl/zlcs/"),
]
DEFAULT_SELF_REGULATORY_MANAGEMENT_SECTIONS = [
    ("受处分机构", "zlgl/jlcf/scfjg/", "disciplinary_institution"),
    ("受处分人员", "zlgl/jlcf/scfry/", "disciplinary_person"),
    ("异常经营", "zlgl/ycjy/ycjyjgclgg/", "abnormal_operation"),
    ("失联机构", "zlgl/sljg/sljgclgg/", "missing_institution"),
    ("自律措施", "zlgl/zlcs/", "self_regulatory_measure"),
]
DEFAULT_INDUSTRY_RESEARCH_SECTIONS = [
    ("研究报告", "hyyj/hjtj/", "industry_research_report"),
    ("声音", "hyyj/sy/", "industry_voice"),
    ("ESG研究", "hyyj/esgtz/esgyj/", "industry_esg_research"),
]

DEFAULT_PRACTICE_SITE_KEYWORDS = [
    "私募基金登记备案动态",
    "登记备案案例",
    "备案业务问答",
    "备案须知",
    "备案关注要点",
]

# AMAC sometimes publishes new rules first as www.amac.org.cn notices with
# attachments before they appear in the fg.amac.org.cn policy index.
DEFAULT_RULE_NOTICE_KEYWORDS = [
    "关于发布《私募投资基金",
    "私募投资基金信息披露",
    "私募投资基金备案指引",
    "私募投资基金服务业务",
    "私募投资基金监督管理",
    "发布《公开募集证券投资基金",
    "发布《基金经营机构",
    "基金从业人员管理规则",
    "基金从业资格考试管理办法",
]

DEFAULT_SITE_KEYWORDS = [
    *DEFAULT_PRACTICE_SITE_KEYWORDS,
    *DEFAULT_RULE_NOTICE_KEYWORDS,
]

XWFB_PAGE_COUNT_RE = re.compile(r"createPageHTML\((\d+),")
XWFB_ARTICLE_DATE_RE = re.compile(r"t(\d{4})(\d{2})(\d{2})_\d+\.html")
SECTION_ROW_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
RULE_NOTICE_ACTION_WORDS = ("发布", "印发", "修订", "公开征求意见", "征求意见")
NON_RULE_NOTICE_WORDS = ("培训", "解读", "举办", "报名时间", "培训时间", "课程", "会议")


def discover_policy_candidates(
    client: AmacClient,
    *,
    page_size: int = 100,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    page = 1
    total: int | None = None
    while total is None or len(candidates) < total:
        payload = client.get_json(
            POLICY_SEARCH_URL,
            {
                "keyword": "",
                "sortFlag": 2,
                "program": "",
                "lawPromNum": "",
                "parId": "",
                "childId": "",
                "pageSize": page_size,
                "pageNo": page,
                "searchType": 1,
            },
        )
        data = ((payload.get("data") or {}).get("data") or {})
        result = data.get("searchListVos") or {}
        total = int(result.get("total") or 0)
        rows = result.get("dataList") or []
        for row in rows:
            relative_url = str(row.get("docPubUrl") or "")
            if not relative_url:
                continue
            candidates.append(
                {
                    "title": row.get("docTitle"),
                    "url": urljoin(AMAC_RULES_BASE_URL, relative_url),
                    "published_at": row.get("lawPromTime"),
                    "search_content": row.get("docContent"),
                    "discovery_channel": "policy_search",
                    "search_raw": row,
                }
            )
            if limit is not None and len(candidates) >= limit:
                return candidates
        if not rows:
            break
        page += 1
    return candidates


def discover_site_candidates(
    client: AmacClient,
    keywords: Iterable[str],
    *,
    page_size: int = 100,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for keyword in keywords:
        page = 1
        total: int | None = None
        seen_for_keyword = 0
        while total is None or seen_for_keyword < total:
            payload = client.get_json(
                SITE_SEARCH_URL,
                {
                    "keyword": keyword,
                    "flag": 1,
                    "pageNo": page,
                    "pageSize": page_size,
                    "sortFlag": 2,
                    "searchType": 0,
                },
            )
            data = ((payload.get("data") or {}).get("data") or {})
            result = data.get("wcmDocuments") or {}
            total = int(result.get("total") or 0)
            rows = result.get("dataList") or []
            for row in rows:
                relative_url = str(row.get("docPubUrl") or "")
                if not relative_url:
                    continue
                candidates.append(
                    {
                        "title": row.get("docTitle"),
                        "url": urljoin(AMAC_BASE_URL, relative_url),
                        "published_at": row.get("docRelTime"),
                        "search_content": row.get("docContent"),
                        "discovery_channel": "site_search",
                        "search_keyword": keyword,
                        "search_raw": row,
                    }
                )
                if limit is not None and len(candidates) >= limit:
                    return candidates
            if not rows:
                break
            seen_for_keyword += len(rows)
            page += 1
    return candidates


def is_xwfb_rule_notice_title(title: str) -> bool:
    normalized = clean_text(title)
    if not normalized:
        return False
    if any(word in normalized for word in NON_RULE_NOTICE_WORDS):
        return False
    return (
        any(word in normalized for word in RULE_NOTICE_ACTION_WORDS)
        and any(word in normalized for word in RULE_WORDS)
    )


def section_list_url(section_path: str, page_index: int) -> str:
    filename = "index.html" if page_index == 0 else f"index_{page_index}.html"
    return urljoin(AMAC_BASE_URL, f"{section_path.rstrip('/')}/{filename}")


def xwfb_list_url(section_path: str, page_index: int) -> str:
    return section_list_url(section_path, page_index)


def date_from_xwfb_url(url: str) -> str | None:
    match = XWFB_ARTICLE_DATE_RE.search(url)
    if not match:
        return None
    year, month, day = match.groups()
    return f"{year}-{month}-{day}"


def _section_content_root(soup: BeautifulSoup) -> Any:
    return (
        soup.select_one(".content-right .c-box")
        or soup.select_one(".content-right")
        or soup.select_one(".list-r")
        or soup.select_one(".list")
        or soup.select_one("main")
        or soup
    )


def _visible_row_date(row_text: str) -> str | None:
    match = SECTION_ROW_DATE_RE.search(row_text)
    if not match:
        return None
    return match.group(0)


def discover_section_candidates(
    client: AmacClient,
    *,
    sections: Iterable[tuple[str, str]],
    discovery_channel: str,
    max_pages: int | None,
    title_filter: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    if max_pages is not None and max_pages <= 0:
        return []

    candidates: list[dict[str, Any]] = []
    for section_name, section_path in sections:
        page_count: int | None = None
        page_index = 0
        while (max_pages is None or page_index < max_pages) and (
            page_count is None or page_index < page_count
        ):
            list_url = section_list_url(section_path, page_index)
            try:
                response = client.get(list_url)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    break
                raise
            response.encoding = response.apparent_encoding or "utf-8"
            raw_html = response.text
            if page_count is None:
                match = XWFB_PAGE_COUNT_RE.search(raw_html)
                if match:
                    page_count = int(match.group(1))
            soup = BeautifulSoup(raw_html, "html.parser")
            root = _section_content_root(soup)
            rows_found = 0
            for anchor in root.select("li a[href]"):
                title = clean_text(anchor.get_text(" ", strip=True))
                href = str(anchor.get("href") or "").strip()
                if not title or not href:
                    continue
                if href.startswith("#") or href.lower().startswith("javascript:"):
                    continue
                if title_filter is not None and not title_filter(title):
                    continue
                url = canonical_url(urljoin(list_url, href))
                row_text = clean_text(
                    anchor.parent.get_text(" ", strip=True) if anchor.parent else ""
                )
                rows_found += 1
                candidates.append(
                    {
                        "title": title,
                        "url": url,
                        "published_at": _visible_row_date(row_text)
                        or date_from_xwfb_url(url),
                        "search_content": row_text,
                        "discovery_channel": discovery_channel,
                        "search_keyword": section_name,
                        "search_raw": {
                            "section": section_name,
                            "section_path": section_path,
                            "list_url": list_url,
                            "page_index": page_index,
                            "row_text": row_text,
                        },
                    }
                )
            if rows_found == 0 and page_count is None:
                break
            if max_pages is None and page_count is None:
                break
            page_index += 1
    return candidates


def discover_self_regulatory_measure_candidates(
    client: AmacClient,
    *,
    sections: Iterable[tuple[str, str]] = DEFAULT_SELF_REGULATORY_MEASURE_SECTIONS,
    max_pages: int = DEFAULT_SELF_REGULATORY_MEASURE_PAGES,
) -> list[dict[str, Any]]:
    return discover_section_candidates(
        client,
        sections=sections,
        discovery_channel="self_regulatory_measure",
        max_pages=max_pages,
    )


def _discover_categorized_section_candidates(
    client: AmacClient,
    sections: Iterable[tuple[str, str, str]],
    max_pages: int,
) -> list[dict[str, Any]]:
    page_limit: int | None = None if max_pages == 0 else max_pages
    candidates: list[dict[str, Any]] = []
    for section_name, section_path, discovery_channel in sections:
        candidates.extend(
            discover_section_candidates(
                client,
                sections=[(section_name, section_path)],
                discovery_channel=discovery_channel,
                max_pages=page_limit,
            )
        )
    return candidates


def discover_self_regulatory_management_candidates(
    client: AmacClient,
    *,
    sections: Iterable[tuple[str, str, str]] = DEFAULT_SELF_REGULATORY_MANAGEMENT_SECTIONS,
    max_pages: int = DEFAULT_SELF_REGULATORY_MANAGEMENT_PAGES,
) -> list[dict[str, Any]]:
    return _discover_categorized_section_candidates(client, sections, max_pages)


def discover_industry_research_candidates(
    client: AmacClient,
    *,
    sections: Iterable[tuple[str, str, str]] = DEFAULT_INDUSTRY_RESEARCH_SECTIONS,
    max_pages: int = DEFAULT_INDUSTRY_RESEARCH_PAGES,
) -> list[dict[str, Any]]:
    return _discover_categorized_section_candidates(client, sections, max_pages)


def discover_xwfb_rule_notice_candidates(
    client: AmacClient,
    *,
    sections: Iterable[tuple[str, str]] = DEFAULT_XWFB_SECTIONS,
    max_pages: int = DEFAULT_XWFB_PAGES,
) -> list[dict[str, Any]]:
    return discover_section_candidates(
        client,
        sections=sections,
        discovery_channel="xwfb_rule_notice",
        max_pages=max_pages,
        title_filter=is_xwfb_rule_notice_title,
    )


def deduplicate_candidates(
    candidates: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_url: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        url = canonical_url(str(candidate.get("url") or ""))
        if not url:
            continue
        if url not in by_url:
            by_url[url] = {**candidate, "url": url, "discovery": []}
        by_url[url]["discovery"].append(
            {
                "channel": candidate.get("discovery_channel"),
                "keyword": candidate.get("search_keyword"),
            }
        )
    return list(by_url.values())


__all__ = [
    "DEFAULT_INDUSTRY_RESEARCH_PAGES",
    "DEFAULT_INDUSTRY_RESEARCH_SECTIONS",
    "DEFAULT_PRACTICE_SITE_KEYWORDS",
    "DEFAULT_RULE_NOTICE_KEYWORDS",
    "DEFAULT_SELF_REGULATORY_MANAGEMENT_PAGES",
    "DEFAULT_SELF_REGULATORY_MANAGEMENT_SECTIONS",
    "DEFAULT_SELF_REGULATORY_MEASURE_PAGES",
    "DEFAULT_SELF_REGULATORY_MEASURE_SECTIONS",
    "DEFAULT_SITE_KEYWORDS",
    "DEFAULT_XWFB_PAGES",
    "DEFAULT_XWFB_SECTIONS",
    "NON_RULE_NOTICE_WORDS",
    "POLICY_SEARCH_URL",
    "RULE_NOTICE_ACTION_WORDS",
    "SITE_SEARCH_URL",
    "XWFB_ARTICLE_DATE_RE",
    "XWFB_PAGE_COUNT_RE",
    "date_from_xwfb_url",
    "deduplicate_candidates",
    "discover_industry_research_candidates",
    "discover_policy_candidates",
    "discover_self_regulatory_management_candidates",
    "discover_self_regulatory_measure_candidates",
    "discover_section_candidates",
    "discover_site_candidates",
    "discover_xwfb_rule_notice_candidates",
    "is_xwfb_rule_notice_title",
    "xwfb_list_url",
]
