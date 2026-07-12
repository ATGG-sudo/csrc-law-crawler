from __future__ import annotations

import os

from bs4 import BeautifulSoup
import pytest

from csrc_law_crawler.sources.amac.client import AmacClient
from csrc_law_crawler.sources.amac.discovery import (
    discover_xwfb_rule_notice_candidates,
)
from csrc_law_crawler.sources.amac.parser import content_root, title_from_page


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
