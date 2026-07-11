from __future__ import annotations

import unittest
from typing import cast

from bs4 import BeautifulSoup

from csrc_law_crawler.sources.amac.client import AmacClient
from csrc_law_crawler.sources.amac.discovery import (
    discover_xwfb_rule_notice_candidates,
    is_xwfb_rule_notice_title,
)
from csrc_law_crawler.sources.amac.identity import canonical_url, source_record_id
from csrc_law_crawler.sources.amac.parser import asset_links
from csrc_law_crawler.sources.amac.pipeline import crawl_candidate


class AmacPackageBoundaryTests(unittest.TestCase):
    def test_identity_helpers_are_available_from_package_modules(self) -> None:
        url = canonical_url("HTTPS://WWW.AMAC.ORG.CN//xwfb/tzgg/item.html?from=list#top")

        self.assertEqual("https://www.amac.org.cn/xwfb/tzgg/item.html", url)
        record_id = source_record_id(url)
        self.assertTrue(record_id.startswith("amac_"))
        self.assertEqual(record_id, source_record_id(url))

    def test_discovery_helpers_are_available_from_package_modules(self) -> None:
        class Response:
            apparent_encoding = "utf-8"
            encoding = "utf-8"

            text = """
            <html><body>
              <div class="content-right"><div class="c-box">
                <ul>
                  <li>
                    <a href="./202606/t20260612_27827.html">关于发布《公开募集证券投资基金主题投资风格管理指引》的公告</a>
                    <i>2026-06-12</i>
                  </li>
                  <li>
                    <a href="./202303/t20230316_18424.html">关于举办《私募投资基金登记备案办法》解读直播培训的通知</a>
                    <i>2023-03-16</i>
                  </li>
                </ul>
              </div></div>
              <script>createPageHTML(1, 0, "index","html");</script>
            </body></html>
            """

            def raise_for_status(self) -> None:
                return None

        class Client:
            def get(self, _url):  # type: ignore[no-untyped-def]
                return Response()

        self.assertTrue(
            is_xwfb_rule_notice_title("关于发布《公开募集证券投资基金主题投资风格管理指引》的公告")
        )
        self.assertFalse(
            is_xwfb_rule_notice_title("关于举办《私募投资基金登记备案办法》解读直播培训的通知")
        )

        candidates = discover_xwfb_rule_notice_candidates(
            cast(AmacClient, Client()),
            sections=[("协会要闻", "xwfb/xhyw/")],
            max_pages=1,
        )

        self.assertEqual(1, len(candidates))
        self.assertEqual(
            "https://www.amac.org.cn/xwfb/xhyw/202606/t20260612_27827.html",
            candidates[0]["url"],
        )

    def test_parser_and_pipeline_helpers_are_available_from_package_modules(self) -> None:
        soup = BeautifulSoup(
            """
            <div class="TRS_Editor">
              <a href="./a.pdf">附件1：规则全文</a>
              <a href="./a.pdf">重复附件</a>
              <a href="./readme.txt">说明</a>
            </div>
            """,
            "html.parser",
        )

        self.assertEqual(
            [("https://www.amac.org.cn/xwfb/tzgg/a.pdf", "附件1：规则全文")],
            asset_links(soup, "https://www.amac.org.cn/xwfb/tzgg/item.html"),
        )

        class Response:
            apparent_encoding = "utf-8"
            encoding = "utf-8"

            text = """
            <html>
              <head><title>列表标题</title></head>
              <body>
                <div class="content-right">
                  <div class="title"><h3>页面完整标题</h3></div>
                  <div class="TRS_Editor">正文第一条。</div>
                </div>
              </body>
            </html>
            """

            def raise_for_status(self) -> None:
                return None

        class Client:
            def get(self, _url):  # type: ignore[no-untyped-def]
                return Response()

        record = crawl_candidate(
            cast(AmacClient, Client()),
            {
                "title": "页面完整标题",
                "url": "https://www.amac.org.cn/xwfb/tzgg/item.html",
                "published_at": "2026-06-12",
            },
            download_assets=False,
        )

        self.assertEqual("页面完整标题", record["metadata"]["name"])
        self.assertEqual("正文第一条。", record["content"]["plain_text"])


if __name__ == "__main__":
    unittest.main()
