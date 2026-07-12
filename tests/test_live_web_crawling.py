from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import pytest

from csrc_law_crawler.sources.amac.client import AmacClient
from csrc_law_crawler.sources.amac.discovery import (
    discover_industry_research_candidates,
    discover_self_regulatory_management_candidates,
    discover_self_regulatory_measure_candidates,
    discover_xwfb_rule_notice_candidates,
)
from csrc_law_crawler.sources.amac.parser import content_root, title_from_page
from csrc_law_crawler.sources.amac.pipeline import crawl_candidate


pytestmark = pytest.mark.skipif(
    os.environ.get("CSRC_LIVE_WEB_TESTS") != "1",
    reason="set CSRC_LIVE_WEB_TESTS=1 to run live official-webpage checks",
)


def test_live_amac_xwfb_list_page_still_exposes_article_links() -> None:
    client = AmacClient(delay_min=0, delay_max=0)
    response = client.get("https://www.amac.org.cn/xwfb/xhyw/index.html")
    response.encoding = response.apparent_encoding or "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")

    root = soup.select_one(".content-right .c-box") or soup
    anchors = [
        anchor
        for anchor in root.select("a[href]")
        if "/xwfb/" in str(anchor.get("href") or "")
        or str(anchor.get("href") or "").endswith(".html")
    ]

    assert response.status_code == 200
    assert anchors


def test_live_amac_xwfb_rule_notice_discovery_does_not_crash() -> None:
    client = AmacClient(delay_min=0, delay_max=0)

    candidates = discover_xwfb_rule_notice_candidates(
        client,
        sections=[("协会要闻", "xwfb/xhyw/")],
        max_pages=1,
    )

    assert isinstance(candidates, list)


def test_live_amac_article_page_can_be_fetched_and_parsed() -> None:
    client = AmacClient(delay_min=0, delay_max=0)
    response = client.get(
        "https://www.amac.org.cn/xwfb/xhyw/202606/t20260612_27827.html"
    )
    response.encoding = response.apparent_encoding or "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")
    root = content_root(soup)

    assert response.status_code == 200
    assert title_from_page(soup) or root.get_text(" ", strip=True)


def test_live_amac_self_regulatory_measure_list_still_exposes_article_links() -> None:
    client = AmacClient(delay_min=0, delay_max=0)
    response = client.get("https://www.amac.org.cn/zlgl/zlcs/")
    response.encoding = response.apparent_encoding or "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")

    root = soup.select_one(".content-right .c-box") or soup
    anchors = [
        anchor
        for anchor in root.select("a[href]")
        if "/zlgl/zlcs/" in str(anchor.get("href") or "")
        or str(anchor.get("href") or "").endswith(".html")
    ]

    assert response.status_code == 200
    assert anchors


def test_live_amac_self_regulatory_measure_detail_can_be_crawled() -> None:
    client = AmacClient(delay_min=0, delay_max=0)

    candidates = discover_self_regulatory_measure_candidates(client, max_pages=1)

    assert candidates
    record = crawl_candidate(client, candidates[0], download_assets=False)
    assert "/zlgl/zlcs/" in record["source"]["page_url"]
    assert record["metadata"]["name"]
    assert record["content"]["plain_text"]


def test_live_amac_self_regulatory_management_pdf_link_can_be_discovered() -> None:
    client = AmacClient(delay_min=0, delay_max=0)

    candidates = discover_self_regulatory_management_candidates(client, max_pages=1)

    assert candidates
    for candidate in candidates[:20]:
        record = crawl_candidate(
            client,
            candidate,
            download_assets=False,
            asset_suffixes={".pdf"},
        )
        if any(
            Path(urlsplit(str(asset.get("source_url") or "")).path).suffix.lower()
            == ".pdf"
            for asset in record.get("assets") or []
        ):
            assert record["source"]["page_url"].startswith("https://www.amac.org.cn/zlgl/")
            return

    pytest.skip("no explicit PDF links found in the first AMAC management sample")


def test_live_amac_industry_research_pdf_link_can_be_discovered() -> None:
    client = AmacClient(delay_min=0, delay_max=0)

    candidates = discover_industry_research_candidates(client, max_pages=1)

    assert candidates
    assert any(
        Path(urlsplit(str(candidate.get("url") or "")).path).suffix.lower() == ".pdf"
        for candidate in candidates
    )
