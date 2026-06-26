from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from amac_crawl import (
    DEFAULT_SITE_KEYWORDS,
    crawl_candidate,
    discover_site_candidates,
    discover_xwfb_rule_notice_candidates,
    is_xwfb_rule_notice_title,
)
from build_catalog import (
    choose_neris_match,
    infer_trial_replacement_relations,
    is_trial_title,
    normalize_title,
    normalize_title_without_trial,
)
from client import HumanLikeClient
from export_markdown_catalog import bucket_for_document
from normalize_catalog import _merge_assets, effectiveness_for, plain_text_to_markdown
from pass2_relations import run_pass2
from revisions_graph import UnionFind, build_revisions_document
from storage import load_json, save_json


class RevisionGraphTests(unittest.TestCase):
    def test_shared_placeholder_number_does_not_merge_rules(self) -> None:
        uf = UnionFind()
        records = {}
        for law_id, name, version in [
            ("a", "规则甲", "20240101"),
            ("b", "规则乙", "20230101"),
            ("c", "规则丙", "20220101"),
        ]:
            uf.add(law_id)
            records[law_id] = {
                "id": law_id,
                "csrc_number": "amac",
                "version": version,
                "name": name,
            }
        document = build_revisions_document(records, uf)
        self.assertEqual(3, len(document["families"]))
        self.assertEqual(
            0,
            sum(len(family["edges"]) for family in document["families"].values()),
        )

    def test_official_revision_group_generates_evidenced_edge(self) -> None:
        uf = UnionFind()
        uf.union("new", "old")
        records = {
            "new": {"id": "new", "version": "2024", "name": "规则（新版）"},
            "old": {"id": "old", "version": "2020", "name": "规则"},
        }
        document = build_revisions_document(
            records,
            uf,
            [
                {
                    "source": "neris.changeLaw",
                    "queried_law_id": "new",
                    "member_ids": ["new", "old"],
                }
            ],
        )
        family = next(iter(document["families"].values()))
        self.assertEqual(1, len(family["edges"]))
        self.assertEqual("neris.changeLaw", family["edges"][0]["source"])
        self.assertEqual(2, document["schema_version"])

    def test_unknown_or_equal_versions_do_not_create_directional_edge(self) -> None:
        uf = UnionFind()
        uf.union("a", "b")
        document = build_revisions_document(
            {
                "a": {"id": "a", "version": None, "name": "规则甲"},
                "b": {"id": "b", "version": None, "name": "规则乙"},
            },
            uf,
            [{"queried_law_id": "a", "member_ids": ["a", "b"]}],
        )
        family = next(iter(document["families"].values()))
        self.assertEqual([], family["edges"])


class CatalogMatchingTests(unittest.TestCase):
    def test_attachment_prefix_and_punctuation_are_normalized(self) -> None:
        self.assertEqual(
            normalize_title("附件2-1：私募投资基金备案指引第2号——私募股权、创业投资基金.pdf"),
            normalize_title("私募投资基金备案指引第2号—私募股权创业投资基金"),
        )

    def test_unique_title_match_with_richer_assets_is_supplemental(self) -> None:
        neris = {
            "record_id": "n1",
            "metadata": {"name": "关于发布《某指引》的公告", "pub_date": "2023-01-01"},
            "assets": [],
        }
        amac = {
            "record_id": "a1",
            "metadata": {"name": "关于发布《某指引》的公告", "pub_date": "2023-01-01"},
            "assets": [{"asset_id": "asset"}],
        }
        match, status, confidence, _ = choose_neris_match(
            amac,
            {normalize_title(neris["metadata"]["name"]): [neris]},
        )
        self.assertIs(match, neris)
        self.assertEqual("supplemental_copy", status)
        self.assertGreaterEqual(confidence, 0.99)


class CatalogNormalizationTests(unittest.TestCase):
    def test_pdf_hard_wrap_is_reflowed_into_articles(self) -> None:
        markdown = plain_text_to_markdown(
            "1\n某规则\n第一条 这是第\n一段内容。\n第二条 这是第二条。",
            title="某规则",
        )
        self.assertIn("**第一条** 这是第一段内容。", markdown)
        self.assertIn("**第二条** 这是第二条。", markdown)
        self.assertNotIn("\n1\n", f"\n{markdown}\n")

    def test_chapter_line_becomes_markdown_heading(self) -> None:
        markdown = plain_text_to_markdown("第一章 总则\n第一条 内容。")
        self.assertTrue(markdown.startswith("## 第一章 总则"))

    def test_title_prefix_does_not_remove_same_line_body(self) -> None:
        markdown = plain_text_to_markdown(
            "某规则第一条 内容。",
            title="某规则",
        )
        self.assertIn("第一条 内容。", markdown)

    def test_amac_unknown_official_rule_defaults_to_current(self) -> None:
        effectiveness = effectiveness_for(
            {
                "title": "私募投资基金备案指引第1号",
                "document_type": "self_regulatory_rule",
                "status": "unknown",
                "preferred_content": {
                    "source_system": "amac",
                    "plain_text": "第一条 内容。",
                },
                "metadata": {"name": "私募投资基金备案指引第1号"},
            }
        )
        self.assertEqual("current", effectiveness["status"])
        self.assertEqual("amac_official_rule_default", effectiveness["basis"])

    def test_comment_draft_is_reference_even_when_amac_rule_like(self) -> None:
        effectiveness = effectiveness_for(
            {
                "title": "关于就《私募投资基金信息披露实施细则（征求意见稿）》公开征求意见的通知",
                "document_type": "self_regulatory_rule",
                "status": "unknown",
                "preferred_content": {
                    "source_system": "amac",
                    "plain_text": "公开征求意见。",
                },
                "metadata": {
                    "name": "私募投资基金信息披露实施细则（征求意见稿）"
                },
            }
        )
        self.assertEqual("not_applicable", effectiveness["status"])
        self.assertEqual("comment_draft_signal", effectiveness["basis"])
        self.assertEqual("reference", bucket_for_document({"effectiveness": effectiveness}))

    def test_reference_template_is_not_amac_default_current(self) -> None:
        effectiveness = effectiveness_for(
            {
                "title": "附表2-1：基金投资者风险测评问卷参考模板（个人版）",
                "document_type": "self_regulatory_rule",
                "status": "unknown",
                "preferred_content": {"source_system": "amac"},
                "metadata": {
                    "name": "基金投资者风险测评问卷参考模板（个人版）"
                },
            }
        )
        self.assertEqual("not_applicable", effectiveness["status"])
        self.assertEqual("reference_title_signal", effectiveness["basis"])

    def test_trial_title_replacement_marks_trial_historical(self) -> None:
        superseding = {
            "canonical_id": "law_formal",
            "source": "catalog.trial_replacement",
            "confidence": 0.86,
        }
        effectiveness = effectiveness_for(
            {
                "title": "私募投资基金备案指引第1号（试行）",
                "document_type": "self_regulatory_rule",
                "status": "unknown",
                "preferred_content": {"source_system": "amac"},
                "metadata": {"name": "私募投资基金备案指引第1号（试行）"},
            },
            superseded_by=[superseding],
        )
        self.assertEqual("historical", effectiveness["status"])
        self.assertEqual("superseded_by_catalog_relation", effectiveness["basis"])
        self.assertEqual([superseding], effectiveness["superseded_by"])

    def test_trial_relation_inference_requires_later_formal_rule(self) -> None:
        self.assertTrue(is_trial_title("私募投资基金备案指引第1号（试行）"))
        self.assertEqual(
            normalize_title("私募投资基金备案指引第1号"),
            normalize_title_without_trial("私募投资基金备案指引第1号（试行）"),
        )
        entities = {
            "law_trial": {
                "id": "law_trial",
                "title": "私募投资基金备案指引第1号（试行）",
                "document_type": "self_regulatory_rule",
                "metadata": {
                    "pub_date": "2023-01-01",
                    "pub_org": "中国证券投资基金业协会",
                },
            },
            "law_formal": {
                "id": "law_formal",
                "title": "私募投资基金备案指引第1号",
                "document_type": "self_regulatory_rule",
                "metadata": {
                    "pub_date": "2024-01-01",
                    "pub_org": "中国证券投资基金业协会",
                },
            },
        }
        relations = infer_trial_replacement_relations(entities)
        self.assertEqual(1, len(relations))
        self.assertEqual("law_formal", relations[0]["from"])
        self.assertEqual("law_trial", relations[0]["to"])
        self.assertEqual("supersedes", relations[0]["relation"])

    def test_duplicate_assets_merge_by_sha256_with_source_evidence(self) -> None:
        assets = _merge_assets(
            [],
            [
                {
                    "asset_id": "asset_a",
                    "kind": "source_document",
                    "label": "规则.pdf",
                    "source_url": "https://example.test/a.pdf",
                    "local_file": "raw/assets/a.pdf",
                    "sha256": "same",
                    "source_system": "amac",
                    "source_record_id": "a",
                    "source_role": "official_text",
                },
                {
                    "asset_id": "asset_b",
                    "kind": "source_document",
                    "label": "规则.pdf",
                    "source_url": "https://example.test/b.pdf",
                    "local_file": "raw/assets/b.pdf",
                    "sha256": "same",
                    "source_system": "amac",
                    "source_record_id": "b",
                    "source_role": "official_copy",
                },
            ],
        )
        self.assertEqual(1, len(assets))
        self.assertEqual(
            ["https://example.test/a.pdf", "https://example.test/b.pdf"],
            assets[0]["source_urls"],
        )
        self.assertEqual(["raw/assets/a.pdf", "raw/assets/b.pdf"], assets[0]["local_files"])
        self.assertEqual(
            {"a", "b"},
            {record["source_record_id"] for record in assets[0]["source_records"]},
        )

    def test_legacy_asset_merge_does_not_create_empty_source_record(self) -> None:
        assets = _merge_assets(
            [
                {
                    "asset_id": "legacy_asset",
                    "kind": "attachment",
                    "sha256": "legacy",
                }
            ],
            [],
        )
        self.assertEqual(1, len(assets))
        self.assertEqual([], assets[0]["source_records"])


class AmacDiscoveryTests(unittest.TestCase):
    def test_default_site_keywords_cover_recent_rule_notices(self) -> None:
        self.assertIn("私募投资基金信息披露", DEFAULT_SITE_KEYWORDS)
        self.assertIn("关于发布《私募投资基金", DEFAULT_SITE_KEYWORDS)

    def test_xwfb_rule_notice_title_filter_targets_rule_publications(self) -> None:
        self.assertTrue(
            is_xwfb_rule_notice_title(
                "关于发布《公开募集证券投资基金主题投资风格管理指引》的公告"
            )
        )
        self.assertTrue(
            is_xwfb_rule_notice_title(
                "关于就《私募投资基金信息披露实施细则（征求意见稿）》公开征求意见的通知"
            )
        )
        self.assertFalse(
            is_xwfb_rule_notice_title(
                "关于举办《私募投资基金登记备案办法》解读直播培训的通知"
            )
        )

    def test_site_discovery_paginates_keyword_results(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.pages: list[int] = []

            def get_json(self, _url, params):  # type: ignore[no-untyped-def]
                page = int(params["pageNo"])
                self.pages.append(page)
                rows = {
                    1: [
                        {
                            "docTitle": "第一页1",
                            "docPubUrl": "/one.html",
                            "docRelTime": "2026-01-01",
                        },
                        {
                            "docTitle": "第一页2",
                            "docPubUrl": "/two.html",
                            "docRelTime": "2026-01-02",
                        },
                    ],
                    2: [
                        {
                            "docTitle": "第二页1",
                            "docPubUrl": "/three.html",
                            "docRelTime": "2026-01-03",
                        }
                    ],
                }.get(page, [])
                return {
                    "data": {
                        "data": {
                            "wcmDocuments": {
                                "total": 3,
                                "dataList": rows,
                            }
                        }
                    }
                }

        client = Client()
        candidates = discover_site_candidates(  # type: ignore[arg-type]
            client,
            ["分页关键词"],
            page_size=2,
        )
        self.assertEqual([1, 2], client.pages)
        self.assertEqual(3, len(candidates))
        self.assertEqual("第二页1", candidates[-1]["title"])

    def test_xwfb_discovery_reads_section_lists(self) -> None:
        class Response:
            apparent_encoding = "utf-8"
            encoding = "utf-8"

            def __init__(self, text: str) -> None:
                self.text = text

            def raise_for_status(self) -> None:
                return None

        class Client:
            def get(self, _url):  # type: ignore[no-untyped-def]
                return Response(
                    """
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
                )

        candidates = discover_xwfb_rule_notice_candidates(  # type: ignore[arg-type]
            Client(),
            sections=[("协会要闻", "xwfb/xhyw/")],
            max_pages=1,
        )
        self.assertEqual(1, len(candidates))
        self.assertEqual(
            "关于发布《公开募集证券投资基金主题投资风格管理指引》的公告",
            candidates[0]["title"],
        )
        self.assertEqual("2026-06-12", candidates[0]["published_at"])
        self.assertEqual(
            "https://www.amac.org.cn/xwfb/xhyw/202606/t20260612_27827.html",
            candidates[0]["url"],
        )

    def test_crawl_candidate_prefers_full_page_title_when_list_title_is_truncated(
        self,
    ) -> None:
        class Response:
            apparent_encoding = "utf-8"
            encoding = "utf-8"

            text = """
            <html>
              <head><title>关于发布《私募投资基金信息披露实施细则》及《私募投资基金信息披露重要内容模板》的公告</title></head>
              <body>
                <div class="content-right">
                  <div class="title">
                    <h3>关于发布《私募投资基金信息披露实施细则》及《私募投资基金信息披露重要内容模板》的公告</h3>
                  </div>
                  <div class="TRS_Editor">正文</div>
                </div>
              </body>
            </html>
            """

            def raise_for_status(self) -> None:
                return None

        class Client:
            def get(self, _url):  # type: ignore[no-untyped-def]
                return Response()

        record = crawl_candidate(  # type: ignore[arg-type]
            Client(),
            {
                "title": "关于发布《私募投资基金信息披露实施细则》及《私募投资基金信息披露重要内容模板...",
                "url": "https://www.amac.org.cn/xwfb/tzgg/202606/t20260605_27780.html",
                "published_at": "2026-06-05",
            },
            download_assets=False,
        )
        self.assertEqual(
            "关于发布《私募投资基金信息披露实施细则》及《私募投资基金信息披露重要内容模板》的公告",
            record["metadata"]["name"],
        )


class SafetyTests(unittest.TestCase):
    def test_rebuild_relations_rejects_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能与 --limit"):
            run_pass2(None, limit=1, rebuild=True)  # type: ignore[arg-type]

    def test_empty_binary_response_is_retried(self) -> None:
        class Response:
            def __init__(self, content: bytes) -> None:
                self.content = content
                self.headers = {"Content-Type": "application/pdf"}
                self.status_code = 200
                self.text = ""

            def raise_for_status(self) -> None:
                return None

        class Session:
            def __init__(self) -> None:
                self.responses = [Response(b""), Response(b"%PDF-data")]
                self.calls = 0

            def get(self, *_args, **_kwargs):
                response = self.responses[self.calls]
                self.calls += 1
                return response

        client = HumanLikeClient(
            delay_min=0,
            delay_max=0,
            batch_size=0,
        )
        session = Session()
        client.session = session  # type: ignore[assignment]
        with patch("client.time.sleep", return_value=None):
            data, _content_type = client.get_binary("https://example.invalid/file")
        self.assertEqual(b"%PDF-data", data)
        self.assertEqual(2, session.calls)

    def test_unknown_rule_and_reference_are_separate_buckets(self) -> None:
        self.assertEqual(
            "unknown",
            bucket_for_document({"effectiveness": {"status": "unknown"}}),
        )
        self.assertEqual(
            "reference",
            bucket_for_document(
                {"effectiveness": {"status": "not_applicable"}}
            ),
        )

    def test_pass2_failure_keeps_published_graph_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            revisions = root / "revisions.json"
            related = root / "related_laws.json"
            save_json(revisions, {"schema_version": 2, "sentinel": "old"})
            save_json(related, {"items": {"sentinel": []}})

            with (
                patch("pass2_relations.load_checkpoint", return_value={}),
                patch("pass2_relations.save_checkpoint", return_value=None),
                patch("pass2_relations.iter_reg_law_ids", return_value=["a"]),
                patch(
                    "pass2_relations.load_reg_metadata",
                    return_value={"id": "a", "name": "规则甲"},
                ),
                patch(
                    "pass2_relations.revision_evidence_cache_path",
                    return_value=root / "missing-cache.json",
                ),
                patch(
                    "pass2_relations.fetch_change_law",
                    side_effect=RuntimeError("network failed"),
                ),
                patch("pass2_relations.revisions_path", return_value=revisions),
                patch("pass2_relations.related_laws_path", return_value=related),
                patch("pass2_relations.reports_dir", return_value=root / "reports"),
            ):
                with self.assertRaisesRegex(RuntimeError, "正式关系图保持不变"):
                    run_pass2(
                        HumanLikeClient(delay_min=0, delay_max=0),
                        rebuild=True,
                        fetch_related=False,
                    )

            self.assertEqual(
                {"schema_version": 2, "sentinel": "old"},
                load_json(revisions, {}),
            )


if __name__ == "__main__":
    unittest.main()
