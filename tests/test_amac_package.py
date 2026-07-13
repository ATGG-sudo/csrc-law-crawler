from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from typing import cast
from unittest.mock import patch

from bs4 import BeautifulSoup

from csrc_law_crawler.sources.amac.client import AmacClient
from csrc_law_crawler.sources.amac.discovery import (
    discover_industry_research_candidates,
    discover_self_regulatory_management_candidates,
    discover_self_regulatory_measure_candidates,
    discover_xwfb_rule_notice_candidates,
    is_xwfb_rule_notice_title,
)
from csrc_law_crawler.sources.amac.identity import canonical_url, source_record_id
from csrc_law_crawler.sources.amac.parser import asset_links
from csrc_law_crawler.sources.amac.pipeline import crawl_amac, crawl_candidate


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

    def test_crawl_candidate_preserves_all_discovered_amac_sections(self) -> None:
        class Client:
            pass

        record = crawl_candidate(
            cast(AmacClient, Client()),
            {
                "title": "构筑ESG实践的伦理基础",
                "url": "https://www.amac.org.cn/hyyj/hjtj/202411/P020241105.pdf",
                "published_at": "2024-11-05",
                "discovery_channel": "industry_research_report",
                "search_keyword": "研究报告",
                "discovery": [
                    {"channel": "industry_research_report", "keyword": "研究报告"},
                    {"channel": "industry_esg_research", "keyword": "ESG研究"},
                ],
            },
            download_assets=False,
            asset_suffixes={".pdf"},
        )

        self.assertEqual(
            "industry_research_report",
            record["metadata"]["source_category"],
        )
        self.assertEqual("研究报告", record["metadata"]["source_section"])
        self.assertEqual(
            ["industry_research_report", "industry_esg_research"],
            record["metadata"]["source_categories"],
        )
        self.assertEqual(["研究报告", "ESG研究"], record["metadata"]["source_sections"])

    def test_self_regulatory_measure_discovery_uses_visible_list_dates(self) -> None:
        class Response:
            apparent_encoding = "utf-8"
            encoding = "utf-8"
            status_code = 200

            text = """
            <html><body>
              <div class="content-right"><div class="c-box">
                <ul>
                  <li>
                    <a href="./202101/t20210104_22417.html">关于暂停成都万华源基金销售有限责任公司私募基金募集业务的决定</a>
                    <span>2020-12-31</span>
                  </li>
                  <li>
                    <a href="./202008/t20200807_17416.html">关于注销恒生网络技术服务有限公司服务业务登记的公告</a>
                    <span>2020-08-07</span>
                  </li>
                </ul>
              </div></div>
            </body></html>
            """

            def raise_for_status(self) -> None:
                return None

        class Client:
            def get(self, _url):  # type: ignore[no-untyped-def]
                return Response()

        candidates = discover_self_regulatory_measure_candidates(
            cast(AmacClient, Client()),
            max_pages=1,
        )

        self.assertEqual(2, len(candidates))
        self.assertEqual(
            "关于暂停成都万华源基金销售有限责任公司私募基金募集业务的决定",
            candidates[0]["title"],
        )
        self.assertEqual("2020-12-31", candidates[0]["published_at"])
        self.assertEqual("self_regulatory_measure", candidates[0]["discovery_channel"])
        self.assertEqual("自律措施", candidates[0]["search_keyword"])
        self.assertEqual(
            "https://www.amac.org.cn/zlgl/zlcs/202101/t20210104_22417.html",
            candidates[0]["url"],
        )

    def test_crawl_amac_can_write_only_self_regulatory_measures(self) -> None:
        measure_title = "关于暂停成都万华源基金销售有限责任公司私募基金募集业务的决定"
        detail_url = "https://www.amac.org.cn/zlgl/zlcs/202101/t20210104_22417.html"

        class Response:
            apparent_encoding = "utf-8"
            encoding = "utf-8"
            status_code = 200

            def __init__(self, text: str) -> None:
                self.text = text

            def raise_for_status(self) -> None:
                return None

        class Client:
            verify_tls = True

            def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
                return None

            def get(self, url: str) -> Response:
                if url.endswith("/zlgl/zlcs/index.html"):
                    return Response(
                        f"""
                        <div class="content-right"><div class="c-box">
                          <ul>
                            <li><a href="./202101/t20210104_22417.html">{measure_title}</a><span>2020-12-31</span></li>
                          </ul>
                        </div></div>
                        """
                    )
                if url == detail_url:
                    return Response(
                        f"""
                        <html><body>
                          <div class="content-right">
                            <div class="title"><h3>{measure_title}</h3></div>
                            <div class="TRS_Editor">自律措施正文。</div>
                          </div>
                        </body></html>
                        """
                    )
                raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_record_id = "amac_existing"
            old_record_path = (
                root / "raw" / "amac" / "records" / f"{old_record_id}.json"
            )
            old_record_path.parent.mkdir(parents=True)
            old_record_path.write_text(
                json.dumps(
                    {
                        "source_record_id": old_record_id,
                        "metadata": {"name": "已有制度", "status": "current"},
                        "assets": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with patch("storage.OUTPUT_DIR", root), patch(
                "csrc_law_crawler.sources.amac.pipeline.AmacClient",
                Client,
            ):
                manifest = crawl_amac(
                    only_self_regulatory_measures=True,
                    self_regulatory_measure_pages=1,
                    download_assets=False,
                    delay_min=0,
                    delay_max=0,
                )

            self.assertEqual(1, manifest["candidate_count"])
            self.assertEqual(2, manifest["count"])
            self.assertEqual(1, manifest["written"])
            manifest_path = root / "raw" / "amac" / "manifest.json"
            self.assertTrue(manifest_path.exists())
            written_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                {old_record_id, source_record_id(detail_url)},
                {item["source_record_id"] for item in written_manifest["items"]},
            )
            record_item = next(
                item
                for item in written_manifest["items"]
                if item["source_record_id"] == source_record_id(detail_url)
            )
            record_path = root / record_item["file"]
            self.assertEqual(root / "raw" / "amac" / "records", record_path.parent)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(measure_title, record["metadata"]["name"])
            self.assertEqual(
                "self_regulatory_measure",
                record["metadata"]["source_category"],
            )
            self.assertEqual("自律措施", record["metadata"]["source_section"])
            self.assertEqual("自律措施正文。", record["content"]["plain_text"])


    def test_crawl_candidate_downloads_only_pdf_assets(self) -> None:
        class Response:
            apparent_encoding = "utf-8"
            encoding = "utf-8"
            text = """
            <html><body>
              <div class="TRS_Editor">
                Body text.
                <a href="./a.pdf">纪律处分复核决定书（罗显志）.pdf</a>
                <a href="./a.pdf">Duplicate PDF asset</a>
                <a href="./b.docx">DOCX asset</a>
              </div>
            </body></html>
            """

            def raise_for_status(self) -> None:
                return None

        class Payload:
            data = b"%PDF sample"
            sha256 = hashlib.sha256(data).hexdigest()
            content_type = "application/pdf"
            size_bytes = len(data)

        class Client:
            downloads: list[str]

            def __init__(self) -> None:
                self.downloads = []

            def get(self, _url):  # type: ignore[no-untyped-def]
                return Response()

            def get_binary_payload(self, url: str) -> Payload:
                self.downloads.append(url)
                return Payload()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client = Client()
            with patch("storage.OUTPUT_DIR", root):
                record = crawl_candidate(
                    cast(AmacClient, client),
                    {
                        "title": "PDF page",
                        "url": "https://www.amac.org.cn/zlgl/jlcf/scfjg/item.html",
                        "published_at": "2023-02-24",
                    },
                    download_assets=True,
                    asset_suffixes={".pdf"},
                )

            self.assertEqual(
                ["https://www.amac.org.cn/zlgl/jlcf/scfjg/a.pdf"],
                client.downloads,
            )
            self.assertEqual(
                ["ok", "skipped_non_pdf"],
                [asset["download_status"] for asset in record["assets"]],
            )
            pdf_asset = record["assets"][0]
            self.assertEqual("raw/assets/amac", pdf_asset["local_file"][:15])
            self.assertTrue(
                pdf_asset["local_file"].endswith(
                    "/纪律处分复核决定书（罗显志） - 2023-02-24.pdf"
                )
            )
            self.assertTrue((root / pdf_asset["local_file"]).exists())
            self.assertEqual(".docx", Path(record["assets"][1]["source_url"]).suffix)
            self.assertIsNone(record["assets"][1]["local_file"])

    def test_crawl_candidate_records_pdf_download_failure(self) -> None:
        class Response:
            apparent_encoding = "utf-8"
            encoding = "utf-8"
            text = """
            <html><body>
              <div class="TRS_Editor">
                <a href="./broken.pdf">Broken PDF</a>
              </div>
            </body></html>
            """

            def raise_for_status(self) -> None:
                return None

        class Client:
            def get(self, _url):  # type: ignore[no-untyped-def]
                return Response()

            def get_binary_payload(self, _url: str):  # type: ignore[no-untyped-def]
                raise RuntimeError("network down")

        record = crawl_candidate(
            cast(AmacClient, Client()),
            {
                "title": "PDF page",
                "url": "https://www.amac.org.cn/zlgl/jlcf/scfjg/item.html",
                "published_at": "2026-01-01",
            },
            download_assets=True,
            asset_suffixes={".pdf"},
        )

        self.assertEqual("failed", record["assets"][0]["download_status"])
        self.assertIn("network down", record["assets"][0]["download_error"])
        self.assertIsNone(record["assets"][0]["content_type"])
        self.assertIsNone(record["assets"][0]["size_bytes"])
        self.assertIsNone(record["assets"][0]["sha256"])

    def test_crawl_amac_records_pdf_download_failure_in_manifest(self) -> None:
        page_url = "https://www.amac.org.cn/zlgl/jlcf/scfjg/202601/t20260101_1.html"
        pdf_url = "https://www.amac.org.cn/zlgl/jlcf/scfjg/202601/broken.pdf"

        class Response:
            apparent_encoding = "utf-8"
            encoding = "utf-8"
            status_code = 200

            def __init__(self, text: str) -> None:
                self.text = text

            def raise_for_status(self) -> None:
                return None

        class Client:
            verify_tls = True

            def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
                return None

            def get(self, url: str) -> Response:
                if url.endswith("/zlgl/jlcf/scfjg/index.html"):
                    return Response(
                        """
                        <div class="content-right"><div class="c-box">
                          <ul><li><a href="./202601/t20260101_1.html">Penalty</a><span>2026-01-01</span></li></ul>
                        </div></div>
                        <script>createPageHTML(1, 0, "index","html");</script>
                        """
                    )
                if url.endswith("/index.html"):
                    return Response(
                        """
                        <div class="content-right"><div class="c-box"><ul></ul></div></div>
                        <script>createPageHTML(1, 0, "index","html");</script>
                        """
                    )
                if url == page_url:
                    return Response(
                        """
                        <div class="TRS_Editor">
                          Body text.
                          <a href="./broken.pdf">Broken PDF</a>
                        </div>
                        """
                    )
                raise AssertionError(f"unexpected URL: {url}")

            def get_binary_payload(self, url: str):  # type: ignore[no-untyped-def]
                if url != pdf_url:
                    raise AssertionError(f"unexpected download URL: {url}")
                raise RuntimeError("download timeout")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch("storage.OUTPUT_DIR", root), patch(
                "csrc_law_crawler.sources.amac.pipeline.AmacClient",
                Client,
            ):
                manifest = crawl_amac(
                    only_self_regulatory_management=True,
                    self_regulatory_management_pages=1,
                    download_assets=True,
                    asset_suffixes={".pdf"},
                    delay_min=0,
                    delay_max=0,
                )

            self.assertEqual(1, manifest["pdf_assets_failed"])
            self.assertEqual(1, manifest["failed"])
            self.assertEqual("asset", manifest["failures"][0]["failure_type"])
            self.assertEqual(pdf_url, manifest["failures"][0]["url"])

            record_files = list((root / "raw" / "amac" / "records").glob("*.json"))
            self.assertEqual(1, len(record_files))
            record = json.loads(record_files[0].read_text(encoding="utf-8"))
            self.assertEqual("failed", record["assets"][0]["download_status"])
            self.assertIn("download timeout", record["assets"][0]["download_error"])

    def test_crawl_amac_pdf_mode_reports_asset_stats_and_skips_existing_ok_pdf(self) -> None:
        page_url = "https://www.amac.org.cn/zlgl/jlcf/scfjg/202601/t20260101_1.html"
        pdf_url = "https://www.amac.org.cn/zlgl/jlcf/scfjg/202601/a.pdf"

        class Response:
            apparent_encoding = "utf-8"
            encoding = "utf-8"
            status_code = 200

            def __init__(self, text: str) -> None:
                self.text = text

            def raise_for_status(self) -> None:
                return None

        class Payload:
            data = b"%PDF sample"
            sha256 = hashlib.sha256(data).hexdigest()
            content_type = "application/pdf"
            size_bytes = len(data)

        class Client:
            verify_tls = True
            downloads = 0

            def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
                return None

            def get(self, url: str) -> Response:
                if url.endswith("/zlgl/jlcf/scfjg/index.html"):
                    return Response(
                        """
                        <div class="content-right"><div class="c-box">
                          <ul><li><a href="./202601/t20260101_1.html">Penalty</a><span>2026-01-01</span></li></ul>
                        </div></div>
                        <script>createPageHTML(1, 0, "index","html");</script>
                        """
                    )
                if url.endswith("/index.html"):
                    return Response(
                        """
                        <div class="content-right"><div class="c-box"><ul></ul></div></div>
                        <script>createPageHTML(1, 0, "index","html");</script>
                        """
                    )
                if url == page_url:
                    return Response(
                        """
                        <div class="TRS_Editor">
                          Body text.
                          <a href="./a.pdf">PDF asset</a>
                          <a href="./b.docx">DOCX asset</a>
                        </div>
                        """
                    )
                raise AssertionError(f"unexpected URL: {url}")

            def get_binary_payload(self, url: str) -> Payload:
                self.__class__.downloads += 1
                if url != pdf_url:
                    raise AssertionError(f"unexpected download URL: {url}")
                return Payload()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            Client.downloads = 0
            with patch("storage.OUTPUT_DIR", root), patch(
                "csrc_law_crawler.sources.amac.pipeline.AmacClient",
                Client,
            ):
                first_manifest = crawl_amac(
                    only_self_regulatory_management=True,
                    self_regulatory_management_pages=1,
                    download_assets=True,
                    asset_suffixes={".pdf"},
                    delay_min=0,
                    delay_max=0,
                )
                second_manifest = crawl_amac(
                    only_self_regulatory_management=True,
                    self_regulatory_management_pages=1,
                    download_assets=True,
                    asset_suffixes={".pdf"},
                    delay_min=0,
                    delay_max=0,
                )

            self.assertEqual(1, first_manifest["pdf_assets_downloaded"])
            self.assertEqual(0, first_manifest["pdf_assets_failed"])
            self.assertEqual(1, first_manifest["non_pdf_assets_skipped"])
            self.assertEqual(1, Client.downloads)
            self.assertEqual(0, second_manifest["written"])
            self.assertEqual(1, second_manifest["skipped"])
            record_path = root / first_manifest["items"][0]["file"]
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(pdf_url, record["assets"][0]["source_url"])
            self.assertEqual(
                "disciplinary_institution",
                record["metadata"]["source_category"],
            )

    def test_crawl_amac_renames_existing_ok_pdf_asset_without_redownloading(self) -> None:
        page_url = "https://www.amac.org.cn/zlgl/jlcf/scfjg/202302/t20230224_1.html"
        pdf_url = "https://www.amac.org.cn/zlgl/jlcf/scfjg/202302/review.pdf"
        record_id = source_record_id(page_url)
        asset_id = source_record_id(pdf_url).replace("amac_", "amac_asset_", 1)

        class Response:
            apparent_encoding = "utf-8"
            encoding = "utf-8"
            status_code = 200

            def __init__(self, text: str) -> None:
                self.text = text

            def raise_for_status(self) -> None:
                return None

        class Client:
            verify_tls = True
            downloads = 0

            def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
                return None

            def get(self, url: str) -> Response:
                if url.endswith("/zlgl/jlcf/scfjg/index.html"):
                    return Response(
                        """
                        <div class="content-right"><div class="c-box">
                          <ul><li><a href="./202302/t20230224_1.html">纪律处分复核决定书（罗显志）</a><span>2023-02-24</span></li></ul>
                        </div></div>
                        <script>createPageHTML(1, 0, "index","html");</script>
                        """
                    )
                if url.endswith("/index.html"):
                    return Response(
                        """
                        <div class="content-right"><div class="c-box"><ul></ul></div></div>
                        <script>createPageHTML(1, 0, "index","html");</script>
                        """
                    )
                raise AssertionError(f"unexpected URL: {url}")

            def get_binary_payload(self, _url: str):  # type: ignore[no-untyped-def]
                self.__class__.downloads += 1
                raise AssertionError("existing ok PDF should not be downloaded again")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_file = (
                root
                / "raw"
                / "assets"
                / "amac"
                / record_id
                / f"{asset_id}.pdf"
            )
            old_file.parent.mkdir(parents=True)
            pdf_bytes = b"%PDF sample"
            old_file.write_bytes(pdf_bytes)
            old_local_file = old_file.relative_to(root).as_posix()
            record = {
                "schema_version": 1,
                "source_record_id": record_id,
                "source_system": "amac",
                "metadata": {
                    "name": "纪律处分复核决定书（罗显志）",
                    "fileno": None,
                    "pub_org": "中国证券投资基金业协会",
                    "pub_date": "2023-02-24",
                    "effective_date": None,
                    "ineffective_date": None,
                    "status": "unknown",
                },
                "content": {"raw_html": "", "plain_text": "", "content_sha256": ""},
                "assets": [
                    {
                        "asset_id": asset_id,
                        "label": "纪律处分复核决定书（罗显志）",
                        "source_url": pdf_url,
                        "local_file": old_local_file,
                        "content_type": "application/pdf",
                        "size_bytes": len(pdf_bytes),
                        "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
                        "download_status": "ok",
                    }
                ],
                "attachment_documents": [],
                "source": {"page_url": page_url},
            }
            record_path = root / "raw" / "amac" / "records" / f"{record_id}.json"
            record_path.parent.mkdir(parents=True)
            record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

            Client.downloads = 0
            with patch("storage.OUTPUT_DIR", root), patch(
                "csrc_law_crawler.sources.amac.pipeline.AmacClient",
                Client,
            ):
                manifest = crawl_amac(
                    only_self_regulatory_management=True,
                    self_regulatory_management_pages=1,
                    download_assets=True,
                    asset_suffixes={".pdf"},
                    delay_min=0,
                    delay_max=0,
                )

            self.assertEqual(0, Client.downloads)
            self.assertEqual(0, manifest["written"])
            self.assertEqual(1, manifest["skipped"])
            self.assertEqual(1, manifest["pdf_assets_renamed"])
            migrated_record = json.loads(record_path.read_text(encoding="utf-8"))
            new_local_file = migrated_record["assets"][0]["local_file"]
            self.assertTrue(
                new_local_file.endswith(
                    "/纪律处分复核决定书（罗显志） - 2023-02-24.pdf"
                )
            )
            self.assertFalse(old_file.exists())
            self.assertTrue((root / new_local_file).exists())

    def test_self_regulatory_management_discovery_includes_all_sections(self) -> None:
        requested_urls: list[str] = []

        class Response:
            apparent_encoding = "utf-8"
            encoding = "utf-8"
            status_code = 200

            def __init__(self, text: str) -> None:
                self.text = text

            def raise_for_status(self) -> None:
                return None

        class Client:
            def get(self, url: str) -> Response:
                requested_urls.append(url)
                if "/scfjg/" in url:
                    title = "Institution penalty"
                elif "/scfry/" in url:
                    title = "Person penalty"
                elif "/ycjyjgclgg/" in url:
                    title = "Abnormal operation"
                elif "/sljgclgg/" in url:
                    title = "Missing institution"
                else:
                    title = "Measure"
                return Response(
                    f"""
                    <div class="content-right"><div class="c-box">
                      <ul><li><a href="./202601/t20260101_1.html">{title}</a><span>2026-01-01</span></li></ul>
                    </div></div>
                    <script>createPageHTML(1, 0, "index","html");</script>
                    """
                )

        candidates = discover_self_regulatory_management_candidates(
            cast(AmacClient, Client()),
            max_pages=1,
        )

        self.assertEqual(5, len(candidates))
        self.assertEqual(
            [
                "disciplinary_institution",
                "disciplinary_person",
                "abnormal_operation",
                "missing_institution",
                "self_regulatory_measure",
            ],
            [candidate["discovery_channel"] for candidate in candidates],
        )
        self.assertTrue(all(url.endswith("index.html") for url in requested_urls))

    def test_industry_research_discovery_includes_all_sections(self) -> None:
        requested_urls: list[str] = []

        class Response:
            apparent_encoding = "utf-8"
            encoding = "utf-8"
            status_code = 200

            def __init__(self, text: str) -> None:
                self.text = text

            def raise_for_status(self) -> None:
                return None

        class Client:
            def get(self, url: str) -> Response:
                requested_urls.append(url)
                if "/hjtj/" in url:
                    title = "青年群体个人养老金投资行为调查报告(2024)"
                elif "/sy/" in url:
                    title = "《声音》2026年第6期"
                else:
                    title = "基金管理人绿色投资自评估报告（2024）"
                return Response(
                    f"""
                    <div class="content-right"><div class="c-box">
                      <ul><li><a href="./202601/P020260101123456789012.pdf">{title}</a><span>2026-01-01</span></li></ul>
                    </div></div>
                    <script>createPageHTML(1, 0, "index","html");</script>
                    """
                )

        candidates = discover_industry_research_candidates(
            cast(AmacClient, Client()),
            max_pages=1,
        )

        self.assertEqual(3, len(candidates))
        self.assertEqual(
            [
                "industry_research_report",
                "industry_voice",
                "industry_esg_research",
            ],
            [candidate["discovery_channel"] for candidate in candidates],
        )
        self.assertTrue(all(candidate["url"].endswith(".pdf") for candidate in candidates))
        self.assertTrue(all(url.endswith("index.html") for url in requested_urls))

    def test_crawl_amac_can_write_only_industry_research_pdf_with_readable_name(self) -> None:
        pdf_url = "https://www.amac.org.cn/hyyj/hjtj/202502/P020250220556896948541.pdf"

        class Response:
            apparent_encoding = "utf-8"
            encoding = "utf-8"
            status_code = 200

            def __init__(self, text: str) -> None:
                self.text = text

            def raise_for_status(self) -> None:
                return None

        class Payload:
            data = b"%PDF industry research"
            sha256 = hashlib.sha256(data).hexdigest()
            content_type = "application/pdf"
            size_bytes = len(data)

        class Client:
            verify_tls = True
            downloads = 0

            def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
                return None

            def get(self, url: str) -> Response:
                if url.endswith("/hyyj/hjtj/index.html"):
                    return Response(
                        """
                        <div class="content-right"><div class="c-box">
                          <ul><li><a href="./202502/P020250220556896948541.pdf">青年群体个人养老金投资行为调查报告(2024)</a><span>2025-02-10</span></li></ul>
                        </div></div>
                        <script>createPageHTML(1, 0, "index","html");</script>
                        """
                    )
                if url.endswith("/index.html"):
                    return Response(
                        """
                        <div class="content-right"><div class="c-box"><ul></ul></div></div>
                        <script>createPageHTML(1, 0, "index","html");</script>
                        """
                    )
                raise AssertionError(f"unexpected URL: {url}")

            def get_binary_payload(self, url: str) -> Payload:
                self.__class__.downloads += 1
                if url != pdf_url:
                    raise AssertionError(f"unexpected download URL: {url}")
                return Payload()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            Client.downloads = 0
            with patch("storage.OUTPUT_DIR", root), patch(
                "csrc_law_crawler.sources.amac.pipeline.AmacClient",
                Client,
            ):
                manifest = crawl_amac(
                    only_industry_research=True,
                    industry_research_pages=1,
                    download_assets=True,
                    asset_suffixes={".pdf"},
                    delay_min=0,
                    delay_max=0,
                )

            self.assertEqual(1, manifest["candidate_count"])
            self.assertEqual(1, manifest["written"])
            self.assertEqual(1, Client.downloads)
            self.assertTrue(manifest["only_industry_research"])
            record_path = root / manifest["items"][0]["file"]
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual("industry_research_report", record["metadata"]["source_category"])
            self.assertEqual("研究报告", record["metadata"]["source_section"])
            local_file = record["assets"][0]["local_file"]
            self.assertTrue(
                local_file.endswith(
                    "/青年群体个人养老金投资行为调查报告(2024) - 2025-02-10.pdf"
                )
            )
            self.assertNotIn("P020250220556896948541", local_file)
            self.assertTrue((root / local_file).exists())


if __name__ == "__main__":
    unittest.main()
