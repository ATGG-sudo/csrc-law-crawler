from __future__ import annotations

import base64
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from amac_crawl import (
    AmacClient,
    DEFAULT_SITE_KEYWORDS,
    crawl_candidate,
    discover_site_candidates,
    discover_xwfb_rule_notice_candidates,
    is_xwfb_rule_notice_title,
)
from asset_text import extract_asset_text_bytes
from build_catalog import (
    _build_catalog_relations,
    _catalog_manifest_items,
    _record_plain_text,
    _review_queue_items,
    _seed_neris_entities,
    choose_neris_match,
    infer_trial_replacement_relations,
    is_trial_title,
    normalize_title,
    normalize_title_without_trial,
)
from catalog_rules import (
    MATCH_TITLE_DATE,
    REVIEW_EFFECTIVENESS_UNKNOWN,
    REVIEW_SOURCE_MATCH_AMBIGUOUS,
    REVIEW_SOURCE_MATCH_LOW_CONFIDENCE,
    RELATION_AMAC_PAGE_ATTACHMENT,
    RULES_BY_ID,
    SOURCE_MATCH_REVIEW_CONFIDENCE_THRESHOLD,
    catalog_rule_calibration,
    catalog_rules_manifest,
    classify_amac_document,
    confidence_band,
)
from catalog_services import CatalogRelationIngestor
from client import HumanLikeClient
from download_assets import _normalized_law_files
from download_utils import DownloadTooLargeError, read_binary_response
from export_markdown_catalog import bucket_for_document
from models import JSON_SCHEMAS, schema_snapshot_files, validate_model
from normalize_catalog import (
    _merge_assets,
    effectiveness_for,
    normalize_catalog_entity,
    plain_text_to_markdown,
)
from normalize_laws import _compose_full_text
from pass2_relations import _apply_revision_response, _merge_related_items, run_pass2
from parser import build_law_document
from pipeline import (
    STEP_COMPLETE,
    STEP_INCOMPLETE,
    PipelineHalted,
    PipelineRunner,
    PipelineStep,
    StepResult,
)
from relation_services import CanonicalRelationGraphBuilder
from revisions_graph import UnionFind, build_revisions_document
from settings import Settings
from storage import (
    FileStore,
    iter_amac_source_files,
    iter_reg_law_files,
    iter_reg_law_ids,
    iter_writ_files,
    listed_output_files,
    load_json,
    save_json,
    strip_global_cli_options,
)
from writ_parser import parse_law_writ_info_html


FIXTURES = Path(__file__).parent / "fixtures"


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
    def test_catalog_rules_manifest_is_unique_and_auditable(self) -> None:
        manifest = catalog_rules_manifest()
        rule_ids = [item["rule_id"] for item in manifest]

        self.assertEqual(len(rule_ids), len(set(rule_ids)))
        self.assertIn(MATCH_TITLE_DATE.rule_id, RULES_BY_ID)
        self.assertTrue(all("evidence_fields" in item for item in manifest))
        self.assertTrue(all("confidence_band" in item for item in manifest))
        self.assertEqual("certain", confidence_band(MATCH_TITLE_DATE.confidence))
        self.assertEqual(
            SOURCE_MATCH_REVIEW_CONFIDENCE_THRESHOLD,
            catalog_rule_calibration()["source_match_review_confidence_threshold"],
        )

    def test_catalog_manifest_items_include_entity_files(self) -> None:
        items = _catalog_manifest_items(
            {
                "law_b": {
                    "title": "规则乙",
                    "document_type": "regulation",
                    "status": "current",
                    "sources": [{"system": "neris"}],
                },
                "law_a": {
                    "title": "规则甲",
                    "document_type": "self_regulatory_rule",
                    "status": "unknown",
                    "sources": [{"system": "amac"}, {"system": "neris"}],
                },
            }
        )

        self.assertEqual(["law_a", "law_b"], [item["id"] for item in items])
        self.assertEqual("work/catalog/laws/law_a.json", items[0]["file"])
        self.assertEqual(2, items[0]["sources"])

    def test_relation_ingestor_deduplicates_edges_and_skips_self_loops(self) -> None:
        ingestor = CatalogRelationIngestor()
        ingestor.add("a", "a", "publishes", {"source": "fixture"})
        ingestor.add("a", "b", "publishes", {"source": "fixture"})
        ingestor.add("a", "b", "publishes", {"source": "fixture"})

        self.assertEqual(
            [
                {
                    "from": "a",
                    "to": "b",
                    "relation": "publishes",
                    "source": "fixture",
                    "evidence": {"source": "fixture"},
                    "confidence": 1.0,
                }
            ],
            ingestor.items,
        )

    def test_relation_ingestor_promotes_rule_id_when_present(self) -> None:
        ingestor = CatalogRelationIngestor()
        ingestor.add(
            "a",
            "b",
            "publishes",
            {
                "source": "amac.page_attachment",
                "rule_id": RELATION_AMAC_PAGE_ATTACHMENT.rule_id,
                "confidence": RELATION_AMAC_PAGE_ATTACHMENT.confidence,
            },
        )

        self.assertEqual(RELATION_AMAC_PAGE_ATTACHMENT.rule_id, ingestor.items[0]["rule_id"])

    def test_review_queue_items_include_auditable_rule_ids(self) -> None:
        items = _review_queue_items(
            [
                {
                    "record_id": "amac_1",
                    "metadata": {
                        "name": "私募投资基金备案指引",
                        "document_type": "self_regulatory_rule",
                        "status": "unknown",
                    },
                    "page_url": "https://example.test/rule",
                }
            ],
            source_to_entity={("amac", "amac_1"): "law_1"},
            matches={"amac_1": {"match_status": "ambiguous"}},
        )

        self.assertEqual(1, len(items))
        self.assertEqual(
            ["source_match_ambiguous", "effectiveness_unknown"],
            items[0]["reasons"],
        )
        self.assertEqual(
            [
                REVIEW_SOURCE_MATCH_AMBIGUOUS.rule_id,
                REVIEW_EFFECTIVENESS_UNKNOWN.rule_id,
            ],
            items[0]["rule_ids"],
        )

    def test_review_queue_items_include_low_confidence_match_reason(self) -> None:
        items = _review_queue_items(
            [
                {
                    "record_id": "amac_1",
                    "metadata": {"name": "规则甲", "document_type": "publication_notice"},
                    "page_url": "https://example.test/notice",
                }
            ],
            source_to_entity={("amac", "amac_1"): "law_1"},
            matches={
                "amac_1": {
                    "match_status": "same_document",
                    "match_rule_id": "match.unique_normalized_title",
                    "confidence": 0.92,
                }
            },
        )

        self.assertEqual(["source_match_low_confidence"], items[0]["reasons"])
        self.assertEqual(
            [REVIEW_SOURCE_MATCH_LOW_CONFIDENCE.rule_id],
            items[0]["rule_ids"],
        )
        self.assertEqual("match.unique_normalized_title", items[0]["match_rule_id"])
        self.assertEqual(0.92, items[0]["match_confidence"])

    def test_canonical_relation_builder_deduplicates_and_counts_edges(self) -> None:
        builder = CanonicalRelationGraphBuilder(
            source_map={"neris:n1": "law_1"},
            load_writ=lambda _writ_id: ({"metadata": {"name": "文书"}}, "raw/writ.json"),
        )
        builder.add_catalog_entity(
            {"id": "law_1", "title": "规则", "document_type": "regulation", "status": "unknown"},
            local_file="canonical/json/law_1.json",
        )
        builder.add_edge(
            "law_1",
            builder.writ_node("w1"),
            "cited_by_case",
            source="fixture",
            confidence=1.0,
            evidence={"rule_id": "relation.fixture"},
            qualifier="w1",
        )
        builder.add_edge(
            "law_1",
            "writ:w1",
            "cited_by_case",
            source="fixture",
            confidence=1.0,
            evidence={"rule_id": "relation.fixture"},
            qualifier="w1",
        )

        graph = builder.as_graph(updated_at="2026-06-30T00:00:00+00:00")
        self.assertEqual(2, graph["counts"]["nodes"])
        self.assertEqual(1, graph["counts"]["edges"])
        self.assertEqual("relation.fixture", graph["edges"][0]["rule_id"])

    def test_amac_classification_returns_rule_metadata(self) -> None:
        document_type, rule = classify_amac_document(
            "私募投资基金备案指引第2号",
            "https://www.amac.org.cn/rule.html",
        )

        self.assertEqual("self_regulatory_rule", document_type)
        self.assertEqual("classification.amac_self_regulatory_rule", rule.rule_id)

    def test_attachment_prefix_and_punctuation_are_normalized(self) -> None:
        self.assertEqual(
            normalize_title("附件2-1：私募投资基金备案指引第2号——私募股权、创业投资基金.pdf"),
            normalize_title("私募投资基金备案指引第2号—私募股权创业投资基金"),
        )

    def test_seed_neris_entities_merges_same_title_fileno_near_date(self) -> None:
        records = [
            {
                "system": "neris",
                "record_id": "n1",
                "metadata": {
                    "id": "n1",
                    "name": "联合发布规则",
                    "fileno": "上证发〔2026〕1号",
                    "pub_date": "2026-01-01",
                    "status": "现行有效",
                    "number": "sse001",
                },
                "plain_text": "短正文",
            },
            {
                "system": "neris",
                "record_id": "n2",
                "metadata": {
                    "id": "n2",
                    "name": "联合发布规则",
                    "fileno": "上证发〔2026〕1号",
                    "pub_date": "2026-01-02",
                    "status": "现行有效",
                    "number": "csdc001",
                },
                "plain_text": "更长的官方正文",
            },
        ]

        entities, source_to_entity, title_index = _seed_neris_entities(records)

        self.assertEqual(1, len(entities))
        entity_id = next(iter(entities))
        self.assertEqual(entity_id, source_to_entity[("neris", "n1")])
        self.assertEqual(entity_id, source_to_entity[("neris", "n2")])
        self.assertEqual(1, len(title_index[normalize_title("联合发布规则")]))
        self.assertEqual(
            ["official_text", "official_duplicate"],
            [source["role"] for source in entities[entity_id]["sources"]],
        )
        self.assertEqual(
            "n2",
            entities[entity_id]["preferred_content"]["source_record_id"],
        )
        self.assertEqual(
            ["n1", "n2"],
            [
                source["record_id"]
                for source in entities[entity_id]["metadata"]["merged_neris_sources"]
            ],
        )

    def test_seed_neris_entities_keeps_same_fileno_different_titles_separate(
        self,
    ) -> None:
        records = [
            {
                "system": "neris",
                "record_id": "n1",
                "metadata": {
                    "id": "n1",
                    "name": "规则甲",
                    "fileno": "证监会公告〔2026〕1号",
                    "pub_date": "2026-01-01",
                },
                "plain_text": "甲",
            },
            {
                "system": "neris",
                "record_id": "n2",
                "metadata": {
                    "id": "n2",
                    "name": "规则乙",
                    "fileno": "证监会公告〔2026〕1号",
                    "pub_date": "2026-01-01",
                },
                "plain_text": "乙",
            },
        ]

        entities, source_to_entity, _title_index = _seed_neris_entities(records)

        self.assertEqual(2, len(entities))
        self.assertNotEqual(
            source_to_entity[("neris", "n1")],
            source_to_entity[("neris", "n2")],
        )

    def test_unique_title_match_with_richer_assets_is_supplemental(self) -> None:
        neris: dict[str, Any] = {
            "record_id": "n1",
            "metadata": {"name": "关于发布《某指引》的公告", "pub_date": "2023-01-01"},
            "assets": [],
        }
        amac: dict[str, Any] = {
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

    def test_metadata_only_rule_reads_text_from_local_asset(self) -> None:
        with patch(
            "build_catalog.extract_local_asset_text",
            return_value="第一条 制度正文。",
        ) as extract_text:
            text = _record_plain_text(
                {"document_type": "self_regulatory_rule"},
                "",
                "raw/assets/rule.pdf",
            )
        self.assertEqual("第一条 制度正文。", text)
        extract_text.assert_called_once()

    def test_asset_text_fallback_does_not_override_non_rules_or_existing_text(
        self,
    ) -> None:
        with patch(
            "build_catalog.extract_local_asset_text",
            return_value="附件正文",
        ) as extract_text:
            self.assertEqual(
                "已有正文",
                _record_plain_text(
                    {"document_type": "self_regulatory_rule"},
                    "已有正文",
                    "raw/assets/rule.pdf",
                ),
            )
            self.assertEqual(
                "",
                _record_plain_text(
                    {"document_type": "supporting_material"},
                    "",
                    "raw/assets/template.pdf",
                ),
            )
        extract_text.assert_not_called()


class PipelineRunnerTests(unittest.TestCase):
    def test_step_result_from_counts_preserves_incomplete_status(self) -> None:
        result = StepResult.from_counts(
            "pass3",
            {
                "status": STEP_INCOMPLETE,
                "laws": 5,
                "processed": 4,
                "skipped": 1,
                "failed": 0,
            },
            seen_key="laws",
            written_key="processed",
        )

        self.assertEqual(STEP_INCOMPLETE, result.status)
        self.assertEqual(5, result.seen)
        self.assertEqual(4, result.written)
        self.assertEqual(1, result.skipped)

    def test_step_result_from_counts_failed_count_is_not_complete(self) -> None:
        result = StepResult.from_counts(
            "pass4",
            {
                "status": STEP_COMPLETE,
                "targets": 3,
                "saved": 2,
                "failed": 1,
            },
            seen_key="targets",
            written_key="saved",
        )

        self.assertEqual(STEP_INCOMPLETE, result.status)
        self.assertEqual(1, result.failed)

    def test_pipeline_runner_halts_on_incomplete_by_default(self) -> None:
        calls: list[str] = []
        updates: list[list[str]] = []
        runner = PipelineRunner(
            on_update=lambda results: updates.append([item.stage for item in results])
        )

        def first_step() -> StepResult:
            calls.append("first")
            return StepResult(stage="first", status=STEP_INCOMPLETE, failed=1)

        def second_step() -> StepResult:
            calls.append("second")
            return StepResult(stage="second", status=STEP_COMPLETE)

        with self.assertRaises(PipelineHalted):
            runner.run(
                [
                    PipelineStep("first", first_step),
                    PipelineStep("second", second_step),
                ]
            )

        self.assertEqual(["first"], calls)
        self.assertEqual([["first"]], updates)

    def test_pipeline_runner_allow_incomplete_continues(self) -> None:
        calls: list[str] = []
        runner = PipelineRunner(allow_incomplete=True)

        def first_step() -> StepResult:
            calls.append("first")
            return StepResult(stage="first", status=STEP_INCOMPLETE, failed=1)

        def second_step() -> StepResult:
            calls.append("second")
            return StepResult(stage="second", status=STEP_COMPLETE)

        result = runner.run(
            [
                PipelineStep("first", first_step),
                PipelineStep("second", second_step),
            ]
        )

        self.assertEqual(["first", "second"], calls)
        self.assertEqual(STEP_INCOMPLETE, result.status)


class GoldenFixtureTests(unittest.TestCase):
    def test_package_surface_exports_core_api(self) -> None:
        from csrc_law_crawler.cli import main as cli_main
        from csrc_law_crawler.core import FileStore, Settings
        from csrc_law_crawler.export import filename_stem
        from csrc_law_crawler.orchestration import PipelineRunner as PackageRunner
        from csrc_law_crawler.processing.catalog import (
            confidence_band as package_confidence_band,
            plain_text_to_markdown as package_plain_text_to_markdown,
        )
        from csrc_law_crawler.processing.normalize import LawNormalizer as PackageLawNormalizer
        from csrc_law_crawler.processing.relations import (
            CanonicalRelationGraphBuilder as PackageGraphBuilder,
        )
        from csrc_law_crawler.sources.neris import build_law_document as package_builder

        self.assertEqual("main", cli_main.__name__)
        self.assertEqual("FileStore", FileStore.__name__)
        self.assertEqual("Settings", Settings.__name__)
        self.assertIs(PackageRunner, PipelineRunner)
        self.assertEqual("LawNormalizer", PackageLawNormalizer.__name__)
        self.assertIs(PackageGraphBuilder, CanonicalRelationGraphBuilder)
        self.assertIs(package_plain_text_to_markdown, plain_text_to_markdown)
        self.assertIs(package_confidence_band, confidence_band)
        self.assertEqual("测试 - 无文号 - 无施行日期", filename_stem({"name": "测试"}, "id"))
        self.assertIs(package_builder, build_law_document)

    def test_unified_cli_help_lists_core_commands(self) -> None:
        from csrc_law_crawler.cli.main import _usage

        usage = _usage()
        self.assertIn("csrc-crawler <command>", usage)
        self.assertIn("crawl", usage)
        self.assertIn("repair", usage)
        self.assertIn("validate-catalog-exports", usage)

    def test_neris_law_detail_fixture_builds_law_document(self) -> None:
        payload = json.loads((FIXTURES / "neris" / "law_detail.json").read_text())
        document = build_law_document(payload["lawlist"])

        self.assertEqual("fixture-law-1", document["metadata"]["id"])
        self.assertEqual("测试法规", document["metadata"]["name"])
        self.assertIn("第一条 固定样本正文。", document["full_text"])
        self.assertEqual("fixture", document["entry_class_code"])

    def test_neris_writ_html_fixture_parses_body_and_basis(self) -> None:
        html = (FIXTURES / "neris" / "writ_info.html").read_text()
        document = parse_law_writ_info_html(html)

        self.assertEqual("测试处罚决定书", document["name"])
        self.assertEqual("行政处罚决定书", document["writ_type"])
        self.assertIn("作出行政处罚", document["body"])
        self.assertTrue(document["legal_basis"])

    def test_neris_writ_html_fixture_parses_party_table_and_original_link(self) -> None:
        html = (FIXTURES / "neris" / "writ_info_party_table.html").read_text()
        document = parse_law_writ_info_html(html)

        self.assertEqual("含当事人表处罚决定书", document["name"])
        self.assertEqual("https://neris.example.test/original.pdf", document["original_link"])
        self.assertIn("第一段正文。\n第二段正文。", document["body"])
        self.assertEqual(
            [
                {
                    "party_type": "机构",
                    "name": "测试公司",
                    "role": "被处罚人",
                    "violation_type": "信息披露违法",
                    "penalty_amount": "100万元",
                }
            ],
            document["parties"],
        )
        self.assertEqual(
            [
                {
                    "law_id": "fixture-law-1",
                    "entry_id": "entry-1",
                    "law_name": "《测试法规》",
                    "entry_title": "第一条",
                }
            ],
            document["legal_basis"],
        )

    def test_amac_page_fixture_crawls_source_record_without_downloads(self) -> None:
        class Response:
            apparent_encoding = "utf-8"
            encoding = "utf-8"

            def __init__(self, text: str) -> None:
                self.text = text

            def raise_for_status(self) -> None:
                return None

        class Client:
            def get(self, _url):  # type: ignore[no-untyped-def]
                return Response((FIXTURES / "amac" / "page.html").read_text())

        record = crawl_candidate(
            cast(AmacClient, Client()),
            {
                "title": "关于发布《测试私募规则》的公告",
                "url": "https://www.amac.org.cn/xwfb/tzgg/202606/t20260630_1.html",
                "published_at": "2026-06-30",
            },
            download_assets=False,
        )

        self.assertEqual("amac", record["source_system"])
        self.assertEqual("关于发布《测试私募规则》的公告", record["metadata"]["name"])
        self.assertEqual(
            "classification.amac_publication_notice",
            record["metadata"]["document_type_rule_id"],
        )
        self.assertIn("第一条 AMAC固定样本正文。", record["content"]["plain_text"])
        self.assertEqual("pending", record["assets"][0]["download_status"])

    def test_text_asset_fixture_extracts_clean_text(self) -> None:
        data = (FIXTURES / "assets" / "sample_rule.txt").read_bytes()
        text = extract_asset_text_bytes(data, ".txt")

        self.assertIn("第一条 TXT制度正文。", text)
        self.assertIn("第二条 继续内容。", text)
        self.assertNotIn("\n1\n", f"\n{text}\n")

    def test_docx_asset_fixture_extracts_clean_text_when_dependency_exists(self) -> None:
        if importlib.util.find_spec("docx") is None:
            self.skipTest("python-docx is not installed")
        data = base64.b64decode(
            (FIXTURES / "assets" / "sample_rule.docx.b64").read_text().strip()
        )
        text = extract_asset_text_bytes(data, ".docx")

        self.assertIn("第一条 DOCX制度正文。", text)
        self.assertIn("第二条 继续内容。", text)

    def test_pdf_asset_fixture_is_safe_to_parse_when_dependency_exists(self) -> None:
        if importlib.util.find_spec("pypdf") is None:
            self.skipTest("pypdf is not installed")
        data = (FIXTURES / "assets" / "sample_blank.pdf").read_bytes()
        text = extract_asset_text_bytes(data, ".pdf")

        self.assertIsInstance(text, str)


class CatalogNormalizationTests(unittest.TestCase):
    def test_normalized_law_full_text_composition_preserves_entry_order(self) -> None:
        plain, markdown = _compose_full_text(
            {"name": "某规则"},
            {"plain": "总则前言", "markdown": "总则前言"},
            {"plain": "附则", "markdown": "附则"},
            [
                {
                    "title": "第一章",
                    "text_plain": "第一条 内容。",
                    "text_markdown": "**第一条** 内容。",
                    "items": [
                        {
                            "title": "第一项",
                            "text_plain": "项目内容。",
                            "text_markdown": "项目内容。",
                        }
                    ],
                }
            ],
        )

        self.assertEqual("某规则\n\n总则前言\n\n第一章\n\n第一条 内容。\n\n第一项\n\n项目内容。\n\n附则", plain)
        self.assertIn("## 第一章", markdown)
        self.assertIn("### 第一项", markdown)

    def test_text_asset_extraction_decodes_and_cleans_plain_text(self) -> None:
        text = extract_asset_text_bytes(
            "第一条  内容\r\n\r\n第二条 内容".encode("gb18030"),
            ".txt",
        )
        self.assertEqual("第一条 内容\n第二条 内容", text)

    def test_text_asset_extraction_strips_sequential_page_numbers(self) -> None:
        text = extract_asset_text_bytes(
            "1\n标题\n第一条 内容\n2\n续行\n3\n尾段。".encode("utf-8"),
            ".txt",
        )
        self.assertEqual("标题\n第一条 内容\n续行\n尾段。", text)

    def test_article_like_continuation_after_page_number_stays_joined(self) -> None:
        markdown = plain_text_to_markdown(
            "第十八条 私募基金管理人应当按照《信息披露办法》\n"
            "8\n"
            "第二十条以及本细则规定披露年度报告。\n"
            "第十九条 下一条内容。",
        )
        self.assertIn(
            "**第十八条** 私募基金管理人应当按照《信息披露办法》第二十条以及本细则规定披露年度报告。",
            markdown,
        )
        self.assertNotIn("**第二十条** 以及", markdown)
        self.assertIn("**第十九条** 下一条内容。", markdown)

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
        self.assertEqual(
            "effectiveness.amac_official_rule_default",
            effectiveness["rule_id"],
        )

    def test_abolished_title_signal_overrides_amac_current_default(self) -> None:
        effectiveness = effectiveness_for(
            {
                "title": "关于证券投资基金宣传推介材料监管事项的补充规定【已废止】",
                "document_type": "self_regulatory_rule",
                "status": "unknown",
                "preferred_content": {"source_system": "amac"},
                "metadata": {
                    "name": "关于证券投资基金宣传推介材料监管事项的补充规定【已废止】"
                },
            }
        )
        self.assertEqual("historical", effectiveness["status"])
        self.assertEqual("explicit_historical_status", effectiveness["basis"])

    def test_superseded_relation_overrides_stale_current_source_status(self) -> None:
        effectiveness = effectiveness_for(
            {
                "title": "上海证券交易所股票上市规则（2022年1月修订）",
                "document_type": "regulation",
                "status": "现行有效",
                "preferred_content": {"source_system": "neris"},
                "metadata": {
                    "name": "上海证券交易所股票上市规则（2022年1月修订）"
                },
            },
            superseded_by=[
                {
                    "canonical_id": "external:sse:2026-main-listing-rules",
                    "source": "official_override",
                    "confidence": 1.0,
                }
            ],
        )
        self.assertEqual("historical", effectiveness["status"])
        self.assertEqual("superseded_by_catalog_relation", effectiveness["basis"])

    def test_normalization_infers_effective_date_from_publication_clause(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "work" / "catalog" / "laws" / "law_dce.json"
            save_json(
                path,
                {
                    "id": "law_dce",
                    "title": "大连商品交易所套期保值管理办法",
                    "document_type": "regulation",
                    "status": "现行有效",
                    "metadata": {
                        "name": "大连商品交易所套期保值管理办法",
                        "fileno": "〔2022〕98号",
                        "pub_date": "2022-12-15",
                        "effective_date": None,
                        "version": "20221216",
                    },
                    "preferred_content": {
                        "source_system": "neris",
                        "source_record_id": "dce",
                        "plain_text": "第二十八条 本办法自公布之日起实施。",
                    },
                    "sources": [],
                },
            )
            with patch("storage.OUTPUT_DIR", root):
                doc = normalize_catalog_entity(path)

        self.assertEqual("2022-12-16", doc["metadata"]["effective_date"])

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
        self.assertEqual("effectiveness.comment_draft_signal", effectiveness["rule_id"])
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
        self.assertEqual("effectiveness.reference_title_signal", effectiveness["rule_id"])

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
        self.assertEqual(
            "effectiveness.superseded_by_catalog_relation",
            effectiveness["rule_id"],
        )
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
        self.assertEqual(
            "relation.trial_replacement.same_title_later_formal",
            relations[0]["rule_id"],
        )

    def test_known_revision_chain_generates_supersedes_relation(self) -> None:
        relations = _build_catalog_relations(
            neris_records=[],
            amac_records=[],
            source_to_entity={},
            entities={
                "law_2020": {
                    "id": "law_2020",
                    "title": "上海证券交易所股票上市规则（2020年12月修订）",
                    "document_type": "regulation",
                    "metadata": {
                        "fileno": "上证发〔2020〕100号",
                        "pub_org": "上海证券交易所",
                        "pub_date": "2020-12-30",
                    },
                },
                "law_2022": {
                    "id": "law_2022",
                    "title": "上海证券交易所股票上市规则（2022年1月修订）",
                    "document_type": "regulation",
                    "metadata": {
                        "fileno": "上证发〔2022〕1号",
                        "pub_org": "上海证券交易所",
                        "pub_date": "2022-01-06",
                    },
                },
            },
        )
        self.assertIn(
            ("law_2022", "law_2020", "supersedes"),
            [
                (item["from"], item["to"], item["relation"])
                for item in relations
            ],
        )

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
        candidates = discover_site_candidates(
            cast(AmacClient, client),
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

        candidates = discover_xwfb_rule_notice_candidates(
            cast(AmacClient, Client()),
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

        record = crawl_candidate(
            cast(AmacClient, Client()),
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
    def test_schema_snapshots_match_model_contracts(self) -> None:
        for name, path in schema_snapshot_files().items():
            with self.subTest(name=name):
                self.assertEqual(JSON_SCHEMAS[name], json.loads(path.read_text()))

    def test_model_validation_reports_missing_required_fields(self) -> None:
        issues = validate_model(
            "source_record",
            {
                "schema_version": 1,
                "source_record_id": "amac_x",
                "source_system": "amac",
                "metadata": {},
                "content": {},
            },
        )
        self.assertIn("$.source: missing required field", issues)

    def test_relation_edge_schema_validates_type(self) -> None:
        issues = validate_model(
            "relation_edge",
            {
                "from": "a",
                "to": "b",
                "relation": "supersedes",
                "source": "catalog",
                "confidence": "0.9",
                "evidence": {},
            },
        )
        self.assertEqual(["$.confidence: expected number, got string"], issues)

    def test_settings_can_override_output_and_download_policy_from_env(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "CSRC_OUTPUT_ROOT": "/tmp/csrc-law-test",
                "CSRC_MAX_DOWNLOAD_BYTES": "12345",
                "CSRC_AMAC_VERIFY_TLS": "false",
                "CSRC_DELAY_MIN": "0.5",
                "CSRC_MAX_RETRIES": "7",
                "CSRC_WORKERS": "3",
            },
        ):
            settings = Settings.from_env()
        self.assertEqual(Path("/tmp/csrc-law-test"), settings.output_root)
        self.assertEqual(12345, settings.max_download_bytes)
        self.assertFalse(settings.amac_verify_tls)
        self.assertEqual(0.5, settings.delay_min)
        self.assertEqual(7, settings.max_retries)
        self.assertEqual(3, settings.workers)

    def test_settings_can_read_global_cli_overrides(self) -> None:
        with patch.object(
            __import__("sys"),
            "argv",
            [
                "crawl.py",
                "--output-root",
                "/tmp/csrc-cli",
                "--max-download-bytes=4096",
                "--delay-max=1.25",
                "--max-retries",
                "4",
                "--limit",
                "1",
            ],
        ):
            settings = Settings.from_env()
        self.assertEqual(Path("/tmp/csrc-cli"), settings.output_root)
        self.assertEqual(4096, settings.max_download_bytes)
        self.assertEqual(1.25, settings.delay_max)
        self.assertEqual(4, settings.max_retries)

    def test_settings_can_read_json_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "output_root": "/tmp/csrc-config",
                        "max_download_bytes": 2048,
                        "amac_verify_tls": False,
                        "retry_backoff_base": 0.75,
                        "workers": 2,
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"CSRC_CONFIG_FILE": str(config_path)}, clear=True):
                settings = Settings.from_env()

        self.assertEqual(Path("/tmp/csrc-config"), settings.output_root)
        self.assertEqual(2048, settings.max_download_bytes)
        self.assertFalse(settings.amac_verify_tls)
        self.assertEqual(0.75, settings.retry_backoff_base)
        self.assertEqual(2, settings.workers)

    def test_global_cli_options_are_stripped_before_script_argparse(self) -> None:
        self.assertEqual(
            ["--limit", "1"],
            strip_global_cli_options(
                [
                    "--config",
                    "/tmp/config.json",
                    "--output-root",
                    "/tmp/csrc-cli",
                    "--max-download-bytes=4096",
                    "--delay-min=0.1",
                    "--max-retries",
                    "3",
                    "--workers=2",
                    "--limit",
                    "1",
                ]
            ),
        )

    def test_filestore_default_root_is_resolved_at_runtime(self) -> None:
        with patch("storage.OUTPUT_DIR", Path("/tmp/csrc-dynamic-root")):
            store = FileStore()

        self.assertEqual(Path("/tmp/csrc-dynamic-root"), store.root)

    def test_listed_output_files_uses_manifest_before_directory_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            listed = root / "work" / "listed.json"
            listed.parent.mkdir(parents=True)
            listed.write_text("{}", encoding="utf-8")
            fallback = root / "fallback"
            fallback.mkdir()
            (fallback / "fallback.json").write_text("{}", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps({"items": [{"file": "work/listed.json"}]}),
                encoding="utf-8",
            )

            with patch("storage.OUTPUT_DIR", root):
                paths = listed_output_files(
                    manifest,
                    field="file",
                    fallback_dir=fallback,
                    pattern="*.json",
                )

        self.assertEqual([listed], paths)

    def test_listed_output_files_falls_back_when_manifest_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fallback = root / "fallback"
            fallback.mkdir()
            fallback_file = fallback / "fallback.json"
            fallback_file.write_text("{}", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps({"items": [{"file": "work/missing.json"}]}),
                encoding="utf-8",
            )

            with patch("storage.OUTPUT_DIR", root):
                paths = listed_output_files(
                    manifest,
                    field="file",
                    fallback_dir=fallback,
                    pattern="*.json",
                )

        self.assertEqual([fallback_file], paths)

    def test_iter_reg_law_files_uses_manifest_and_preserves_filename_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            law_dir = root / "raw" / "neris" / "laws"
            law_dir.mkdir(parents=True)
            reg_b = law_dir / "reg_b.json"
            reg_a = law_dir / "reg_a.json"
            reg_b.write_text("{}", encoding="utf-8")
            reg_a.write_text("{}", encoding="utf-8")
            manifest = root / "raw" / "neris" / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "items": [
                            {"id": "b", "file": "raw/neris/laws/reg_b.json"},
                            {"id": "a", "file": "raw/neris/laws/reg_a.json"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with patch("storage.OUTPUT_DIR", root):
                files = iter_reg_law_files(limit=1)
                ids = iter_reg_law_ids(limit=2)

        self.assertEqual([reg_a], files)
        self.assertEqual(["a", "b"], ids)

    def test_iter_amac_source_files_includes_manifest_and_directory_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "raw" / "amac" / "records"
            source_dir.mkdir(parents=True)
            manifest_file = source_dir / "amac_a.json"
            extra_file = source_dir / "amac_b.json"
            manifest_file.write_text("{}", encoding="utf-8")
            extra_file.write_text("{}", encoding="utf-8")
            manifest = root / "raw" / "amac" / "manifest.json"
            manifest.write_text(
                json.dumps({"items": [{"file": "raw/amac/records/amac_a.json"}]}),
                encoding="utf-8",
            )

            with patch("storage.OUTPUT_DIR", root):
                files = iter_amac_source_files()

        self.assertEqual([manifest_file, extra_file], files)

    def test_iter_writ_files_uses_checkpoint_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            writ_dir = root / "raw" / "neris" / "writs"
            writ_dir.mkdir(parents=True)
            writ_b = writ_dir / "writ_b.json"
            writ_a = writ_dir / "writ_a.json"
            writ_b.write_text("{}", encoding="utf-8")
            writ_a.write_text("{}", encoding="utf-8")
            fallback_only = writ_dir / "writ_z.json"
            fallback_only.write_text("{}", encoding="utf-8")
            checkpoint = root / "work" / "checkpoints" / "checkpoint.json"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_text(
                json.dumps({"pass4": {"completed_writ_ids": ["b", "a"]}}),
                encoding="utf-8",
            )

            with patch("storage.OUTPUT_DIR", root):
                files = iter_writ_files()

        self.assertEqual([writ_a, writ_b], files)

    def test_download_assets_uses_normalized_manifest_file_index(self) -> None:
        expected = [Path("reg_a.json")]
        with patch("download_assets.listed_output_files", return_value=expected) as listed:
            paths = _normalized_law_files(limit=1)

        self.assertEqual(expected, paths)
        self.assertEqual("file", listed.call_args.kwargs["field"])
        self.assertEqual("reg_*.json", listed.call_args.kwargs["pattern"])
        self.assertEqual(1, listed.call_args.kwargs["limit"])

    def test_validate_catalog_uses_manifest_file_index(self) -> None:
        import validate_catalog

        catalog_expected = [Path("law_a.json")]
        amac_expected = [Path("amac_a.json")]
        with patch("validate_catalog.listed_output_files", return_value=catalog_expected) as listed:
            with patch(
                "validate_catalog.iter_amac_source_files",
                return_value=amac_expected,
            ):
                catalog_paths = validate_catalog._catalog_entity_files()
                amac_paths = validate_catalog._amac_source_files()

        self.assertEqual(catalog_expected, catalog_paths)
        self.assertEqual(amac_expected, amac_paths)
        self.assertEqual("law_*.json", listed.call_args.kwargs["pattern"])

    def test_validate_catalog_exports_uses_manifest_file_indexes(self) -> None:
        import validate_catalog_exports

        expected = [Path("law_a.json")]
        with patch(
            "validate_catalog_exports.listed_output_files",
            return_value=expected,
        ) as listed:
            self.assertEqual(expected, validate_catalog_exports._catalog_entity_files())
            self.assertEqual(expected, validate_catalog_exports._catalog_normalized_files())
            self.assertEqual(expected, validate_catalog_exports._catalog_markdown_files())

        patterns = [call.kwargs["pattern"] for call in listed.call_args_list]
        self.assertEqual(["law_*.json", "law_*.json", "*/*.md"], patterns)

    def test_validate_normalized_uses_manifest_file_index(self) -> None:
        import validate_normalized

        expected = [Path("reg_a.json")]
        with patch("validate_normalized.listed_output_files", return_value=expected) as listed:
            paths = validate_normalized._normalized_law_files()

        self.assertEqual(expected, paths)
        self.assertEqual("file", listed.call_args.kwargs["field"])
        self.assertEqual("reg_*.json", listed.call_args.kwargs["pattern"])

    def test_amac_client_verifies_tls_by_default(self) -> None:
        class Response:
            def raise_for_status(self) -> None:
                return None

        class Session:
            def __init__(self) -> None:
                self.kwargs: dict[str, object] = {}
                self.headers: dict[str, str] = {}

            def get(self, *_args, **kwargs):
                self.kwargs = kwargs
                return Response()

        client = AmacClient(delay_min=0, delay_max=0)
        session = Session()
        client.session = session  # type: ignore[assignment]

        client.get("https://fg.amac.org.cn/example")

        self.assertIs(session.kwargs["verify"], True)

    def test_amac_client_retries_blocked_get_response(self) -> None:
        class Response:
            headers: dict[str, str] = {}

            def __init__(self, status_code: int, text: str) -> None:
                self.status_code = status_code
                self.text = text

            def raise_for_status(self) -> None:
                return None

        class Session:
            def __init__(self) -> None:
                self.headers: dict[str, str] = {}
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return Response(503, "WAF")
                return Response(200, "{}")

        client = AmacClient(delay_min=0, delay_max=0)
        session = Session()
        client.session = session  # type: ignore[assignment]

        with (
            patch("amac_crawl.random.uniform", return_value=0),
            patch("amac_crawl.time.sleep"),
        ):
            response = client.get("https://fg.amac.org.cn/example")

        self.assertEqual(2, session.calls)
        self.assertEqual(200, response.status_code)

    def test_amac_binary_payload_retries_empty_content(self) -> None:
        class Response:
            status_code = 200
            text = ""
            headers = {"Content-Type": "application/pdf"}

            def __init__(self, chunks: list[bytes]) -> None:
                self.chunks = chunks

            def raise_for_status(self) -> None:
                return None

            def iter_content(self, chunk_size: int):
                yield from self.chunks

        class Session:
            def __init__(self) -> None:
                self.headers: dict[str, str] = {}
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return Response([])
                return Response([b"%PDF-data"])

        client = AmacClient(delay_min=0, delay_max=0)
        session = Session()
        client.session = session  # type: ignore[assignment]

        with (
            patch("amac_crawl.random.uniform", return_value=0),
            patch("amac_crawl.time.sleep"),
        ):
            payload = client.get_binary_payload("https://fg.amac.org.cn/example.pdf")

        self.assertEqual(2, session.calls)
        self.assertEqual(b"%PDF-data", payload.data)

    def test_binary_response_rejects_declared_oversized_download(self) -> None:
        class Response:
            headers = {
                "Content-Type": "application/pdf",
                "Content-Length": "10",
            }

            def iter_content(self, chunk_size: int):
                yield b"%PDF-data"

        with self.assertRaisesRegex(DownloadTooLargeError, "exceeds limit"):
            read_binary_response(Response(), max_bytes=5)

    def test_rebuild_relations_rejects_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能与 --limit"):
            run_pass2(None, limit=1, rebuild=True)  # type: ignore[arg-type]

    def test_pass2_revision_response_builds_evidence(self) -> None:
        version_records: dict[str, dict[str, Any]] = {}
        uf = UnionFind()
        with (
            patch("pass2_relations.load_reg_metadata", return_value=None),
            patch("pass2_relations.utc_now_iso", return_value="2026-06-30T00:00:00+00:00"),
        ):
            result = _apply_revision_response(
                queried_law_id="new",
                local_meta={"name": "新版规则"},
                change_resp={
                    "law": {
                        "secFutrsLawId": "new",
                        "secFutrsLawVersion": "2024",
                        "secFutrsLawName": "新版规则",
                    },
                    "evltList": [
                        {
                            "secFutrsLawId": "old",
                            "secFutrsLawVersion": "2020",
                            "secFutrsLawName": "旧版规则",
                        }
                    ],
                },
                version_records=version_records,
                uf=uf,
            )

        self.assertEqual("new", result.current_id)
        self.assertEqual({"new", "old"}, set(version_records))
        self.assertEqual(uf.find("new"), uf.find("old"))
        self.assertEqual(
            {
                "source": "neris.changeLaw",
                "queried_law_id": "new",
                "member_ids": ["new", "old"],
                "retrieved_at": "2026-06-30T00:00:00+00:00",
            },
            result.evidence_record,
        )

    def test_pass2_related_items_merge_without_duplicates(self) -> None:
        related_items = {"a": [{"to_law_id": "b", "name": "规则乙"}]}

        _merge_related_items(
            related_items,
            "a",
            [
                {"secFutrsLawId": "b", "secFutrsLawName": "规则乙"},
                {"secFutrsLawId": "c", "secFutrsLawName": "规则丙"},
            ],
        )

        self.assertEqual(
            [
                {"to_law_id": "b", "name": "规则乙"},
                {
                    "to_law_id": "c",
                    "name": "规则丙",
                    "fileno": None,
                    "relation_type": None,
                    "raw": {"secFutrsLawId": "c", "secFutrsLawName": "规则丙"},
                },
            ],
            related_items["a"],
        )

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
