from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from build_catalog import _multi_source_rule_records
from csrc_law_crawler.core.io import publish_directory_atomic
from csrc_law_crawler.sources.adapters import HttpHtmlAdapter
from csrc_law_crawler.sources.evidence import (
    canonical_final_url,
    record_fingerprints,
    source_record_id,
)
from csrc_law_crawler.sources.registry import load_registry, registry_query_sha256
from csrc_law_crawler.sources.runner import REMOVAL_SCOPE_MODES, SourceRunner
from csrc_law_crawler.sources.wechat import JSON_FILE_NAME, import_wechat_bundle
from models import SOURCE_RECORD_SCHEMA, SourceRecord
from runtime import RunContext
import storage


def _registry(scope_mode: str = "enumerable") -> dict:
    return {
        "schema_version": 1,
        "query_set_version": "test-v1",
        "query_sets": {"q": ["私募"]},
        "endpoints": [
            {
                "endpoint_id": "endpoint_test",
                "url": "https://example.test/list",
                "source_system": "test",
                "adapter": "fake",
                "scope_mode": scope_mode,
                "query_sets": ["q"],
                "default_material_lane": "rule",
                "profiles": [
                    {
                        "profile_id": "profile_test",
                        "name": "测试规则",
                        "publisher": "测试机关",
                        "material_nature": "规则",
                        "region": "全国",
                    }
                ],
            }
        ],
    }


class FakeAdapter:
    def __init__(self) -> None:
        self.items = [
            {
                "url": "https://example.test/detail/1",
                "title": "测试规则",
                "upstream_id": "upstream-1",
                "discovery_evidence": [{"page": 1, "query": "私募"}],
            }
        ]
        self.discovery_status = "complete"
        self.reported_total = 1
        self.result_limit_reached = False
        self.response_body = b"<html>volatile=1</html>"
        self.plain_text = "第一条 稳定正文"
        self.metadata = {"name": "测试规则", "pub_date": "2026-01-01"}

    def healthcheck(self, endpoint: dict) -> dict:
        return {
            "access_status": "reachable",
            "status_code": 200,
            "final_url": endpoint["url"],
            "content_type": "text/html",
            "_body": b"health",
        }

    def discover(self, endpoint: dict, registry: dict, checkpoint: dict) -> dict:
        return {
            "items": copy.deepcopy(self.items),
            "raw_pages": [
                {
                    "url": endpoint["url"],
                    "final_url": endpoint["url"],
                    "content_type": "text/html",
                    "body": b"list",
                }
            ],
            "discovery_status": self.discovery_status,
            "pages_completed": 1,
            "reported_total": self.reported_total,
            "result_limit_reached": self.result_limit_reached,
            "failures": [],
        }

    def fetch(self, endpoint: dict, item: dict) -> dict:
        return {
            "body": self.response_body,
            "content_type": "text/html",
            "final_url": item["url"],
        }

    def parse(self, endpoint: dict, item: dict, fetched: dict) -> dict:
        return {
            "metadata": copy.deepcopy(self.metadata),
            "plain_text": self.plain_text,
            "content_html": "<p>正文</p>",
            "assets": [],
        }


class RegistryAndFingerprintTests(unittest.TestCase):
    def test_html_adapter_keeps_non_root_catalog_in_its_directory_tree(self) -> None:
        self.assertTrue(
            HttpHtmlAdapter._same_scope_path(
                "https://example.test/rules/", "https://example.test/rules/2026/a.html"
            )
        )
        self.assertFalse(
            HttpHtmlAdapter._same_scope_path(
                "https://example.test/rules/", "https://example.test/cases/a.html"
            )
        )

    def test_html_single_page_directory_is_not_claimed_complete(self) -> None:
        response = type(
            "Response",
            (),
            {
                "url": "https://example.test/rules/",
                "status_code": 200,
                "content": (
                    b"<html><body><ul><li>2026-01-01 "
                    b'<a href="/rules/2026/a.html">Rule item</a>'
                    b"</li></ul></body></html>"
                ),
                "headers": {"Content-Type": "text/html"},
                "raise_for_status": lambda self: None,
            },
        )()
        adapter = HttpHtmlAdapter()
        adapter._get = lambda url: response  # type: ignore[method-assign]
        registry = _registry("catalog_filter")
        endpoint = registry["endpoints"][0]
        endpoint["adapter"] = "http_html"
        endpoint["url"] = response.url
        result = adapter.discover(endpoint, registry, {})
        self.assertEqual("incomplete", result["discovery_status"])
        self.assertEqual("unproven_single_page_directory", result["completeness_evidence"])
        self.assertEqual(1, result["raw_hit_count"])

    def test_checked_registry_has_85_endpoints_and_86_profiles(self) -> None:
        registry = load_registry()
        self.assertEqual(85, len(registry["endpoints"]))
        self.assertEqual(86, sum(len(item["profiles"]) for item in registry["endpoints"]))
        self.assertEqual(85, len({item["url"] for item in registry["endpoints"]}))
        self.assertEqual(64, len(registry_query_sha256(registry)))
        self.assertEqual(
            {"enumerable", "catalog_filter", "query_exhaustive", "subject_query"},
            {item["scope_mode"] for item in registry["endpoints"]},
        )
        self.assertEqual({"enumerable", "catalog_filter"}, REMOVAL_SCOPE_MODES)

    def test_stable_id_ignores_endpoint_and_tracking_query(self) -> None:
        left = canonical_final_url("HTTPS://Example.Test/a?utm_source=x&id=1#part")
        right = canonical_final_url("https://example.test/a?id=1")
        self.assertEqual(left, right)
        self.assertEqual(
            source_record_id("csrc", final_url=left),
            source_record_id("csrc", final_url=right),
        )

    def test_response_noise_does_not_change_semantic_hashes(self) -> None:
        first = record_fingerprints(
            metadata={"name": "规则", "fetched_at": "one"},
            plain_text="规则  正文",
            assets=[],
            response_bytes=b"token=1",
        )
        second = record_fingerprints(
            metadata={"name": "规则", "fetched_at": "two"},
            plain_text="规则 正文",
            assets=[],
            response_bytes=b"token=2",
        )
        self.assertNotEqual(first["response_sha256"], second["response_sha256"])
        self.assertEqual(first["metadata_sha256"], second["metadata_sha256"])
        self.assertEqual(first["content_sha256"], second["content_sha256"])

    def test_source_record_contract_exposes_lane_and_fingerprints(self) -> None:
        record = SourceRecord(
            schema_version=1,
            source_record_id="id",
            source_system="system",
            metadata={},
            content={},
            source={},
        )
        self.assertEqual("complete", record.ingest_status)
        self.assertIn("material_lane", SOURCE_RECORD_SCHEMA["properties"])
        self.assertIn("attachment_documents", SOURCE_RECORD_SCHEMA["properties"])


class SourceRunnerTests(unittest.TestCase):
    def test_query_scope_filters_nonmatching_details_without_source_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = FakeAdapter()
            adapter.plain_text = "与目标主题无关的正文"
            adapter.metadata = {"name": "一般规则", "pub_date": "2026-01-01"}
            report = SourceRunner(
                registry=_registry("query_exhaustive"),
                adapter_factory=lambda name: adapter,
                root=root,
            ).run(mode="baseline")
            state = report["endpoints"]["endpoint_test"]
            self.assertEqual(1, state["discovered"])
            self.assertEqual(1, state["filtered_out"])
            self.assertEqual(0, state["in_scope"])
            self.assertEqual("complete", state["materialization_status"])
            self.assertFalse(list((root / "raw" / "sources" / "records").rglob("*.json")))

    def test_query_scope_materializes_body_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = FakeAdapter()
            adapter.plain_text = "本规则适用于私募基金管理人。"
            report = SourceRunner(
                registry=_registry("query_exhaustive"),
                adapter_factory=lambda name: adapter,
                root=root,
            ).run(mode="baseline")
            state = report["endpoints"]["endpoint_test"]
            self.assertEqual(1, state["in_scope"])
            self.assertEqual(1, state["materialized"])
            self.assertEqual("complete", state["materialization_status"])

    def test_unchanged_asset_sha_reuses_previous_extracted_text(self) -> None:
        class Response:
            url = "https://example.test/rules/a.pdf"
            headers = {"Content-Type": "application/pdf"}

            def raise_for_status(self) -> None:
                return None

            def iter_content(self, chunk_size: int):
                del chunk_size
                yield b"cached-pdf"

            def close(self) -> None:
                return None

        class Session:
            def get(self, *args, **kwargs):
                return Response()

        with tempfile.TemporaryDirectory() as tmp:
            runner = SourceRunner(
                registry=_registry(),
                adapter_factory=lambda name: FakeAdapter(),
                root=Path(tmp),
            )
            runner.asset_session = Session()  # type: ignore[assignment]
            digest = hashlib.sha256(b"cached-pdf").hexdigest()
            previous = {
                "assets": [
                    {
                        "sha256": digest,
                        "download_status": "complete",
                        "text_extraction_status": "complete",
                    }
                ],
                "attachment_documents": [
                    {
                        "source_record_id": f"record:asset:{digest}",
                        "metadata": {"asset_sha256": digest},
                        "content": {"plain_text": "已抽取正文"},
                    }
                ],
            }
            with patch(
                "csrc_law_crawler.sources.runner._extract_asset_text_with_timeout",
                side_effect=AssertionError("must reuse cached text"),
            ):
                assets, documents, complete = runner._download_assets(
                    _registry()["endpoints"][0],
                    "record",
                    [
                        {
                            "source_url": Response.url,
                            "file_name": "a.pdf",
                        }
                    ],
                    Path(tmp) / "failures.jsonl",
                    previous,
                )
            self.assertTrue(complete)
            self.assertEqual("complete", assets[0]["text_extraction_status"])
            self.assertEqual("已抽取正文", documents[0]["content"]["plain_text"])

    def test_baseline_then_noise_only_incremental_has_no_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = FakeAdapter()
            runner = SourceRunner(
                registry=_registry(),
                adapter_factory=lambda name: adapter,
                root=root,
            )
            baseline = runner.run(mode="baseline")
            self.assertEqual("complete", baseline["status"])
            self.assertEqual(1, baseline["counts"]["attempted"])
            self.assertEqual(1, baseline["counts"]["materialization_complete"])
            self.assertFalse((root / "work" / "changes" / f"{baseline['run_id']}.jsonl").exists())

            adapter.response_body = b"<html>volatile=2</html>"
            incremental = runner.run(mode="incremental")
            self.assertFalse(
                (root / "work" / "changes" / f"{incremental['run_id']}.jsonl").exists()
            )
            record_id = source_record_id("test", upstream_id="upstream-1")
            record = json.loads(
                (root / "raw" / "sources" / "records" / "test" / f"{record_id}.json").read_text()
            )
            self.assertEqual("complete", record["ingest_status"])
            self.assertTrue(Path(record["source"]["raw_file"]).is_absolute())
            self.assertTrue(
                list((root / "raw" / "sources" / "endpoint_test" / "discoveries").glob("*.jsonl"))
            )

    def test_content_change_is_reported_but_baseline_is_not_new(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = FakeAdapter()
            runner = SourceRunner(
                registry=_registry(), adapter_factory=lambda name: adapter, root=root
            )
            runner.run(mode="baseline")
            adapter.plain_text = "第二条 真实更新"
            report = runner.run(mode="incremental")
            changes = (root / "work" / "changes" / f"{report['run_id']}.jsonl").read_text()
            self.assertIn('"change_type": "content_changed"', changes)

    def test_two_complete_misses_are_required_for_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = FakeAdapter()
            runner = SourceRunner(
                registry=_registry(), adapter_factory=lambda name: adapter, root=root
            )
            runner.run(mode="baseline")
            adapter.items = []
            adapter.reported_total = 0
            first = runner.run(mode="incremental")
            first_changes = root / "work" / "changes" / f"{first['run_id']}.jsonl"
            self.assertFalse(first_changes.exists())
            second = runner.run(mode="incremental")
            self.assertIn(
                '"change_type": "removed"',
                (root / "work" / "changes" / f"{second['run_id']}.jsonl").read_text(),
            )

    def test_incomplete_run_does_not_advance_missing_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = FakeAdapter()
            runner = SourceRunner(
                registry=_registry(), adapter_factory=lambda name: adapter, root=root
            )
            runner.run(mode="baseline")
            adapter.items = []
            adapter.reported_total = None
            adapter.discovery_status = "incomplete"
            runner.run(mode="incremental")
            checkpoint = json.loads(
                (root / "work" / "checkpoints" / "sources" / "endpoint_test.json").read_text()
            )
            record_state = next(iter(checkpoint["records"].values()))
            self.assertEqual(0, record_state["missing_count"])

    def test_resume_refuses_registry_query_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = FakeAdapter()
            first = SourceRunner(
                registry=_registry(), adapter_factory=lambda name: adapter, root=root
            ).run(mode="baseline")
            changed = _registry()
            changed["query_sets"]["q"] = ["变更"]
            with self.assertRaisesRegex(RuntimeError, "resume fingerprint mismatch"):
                SourceRunner(registry=changed, adapter_factory=lambda name: adapter, root=root).run(
                    mode="baseline", resume_run_id=first["run_id"]
                )

    def test_resume_refuses_source_tree_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = FakeAdapter()
            first_runner = SourceRunner(
                registry=_registry(), adapter_factory=lambda name: adapter, root=root
            )
            first = first_runner.run(mode="baseline")
            changed_runner = SourceRunner(
                registry=_registry(), adapter_factory=lambda name: adapter, root=root
            )
            changed_runner.code_sha256 = "0" * 64
            with self.assertRaisesRegex(RuntimeError, "code_sha256"):
                changed_runner.run(mode="baseline", resume_run_id=first["run_id"])

    def test_result_limit_forces_incomplete_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeAdapter()
            adapter.result_limit_reached = True
            report = SourceRunner(
                registry=_registry("query_exhaustive"),
                adapter_factory=lambda name: adapter,
                root=Path(tmp),
            ).run(mode="baseline")
            state = report["endpoints"]["endpoint_test"]
            self.assertEqual("incomplete", state["discovery_status"])
            self.assertEqual("incomplete", report["status"])

    def test_failed_detail_has_evidence_but_no_source_record_or_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = FakeAdapter()

            def fail_parse(endpoint: dict, item: dict, fetched: dict) -> dict:
                raise ValueError("bad detail")

            adapter.parse = fail_parse  # type: ignore[method-assign]
            report = SourceRunner(
                registry=_registry(), adapter_factory=lambda name: adapter, root=root
            ).run(mode="baseline")
            record_id = source_record_id("test", upstream_id="upstream-1")
            self.assertFalse(
                (root / "raw" / "sources" / "records" / "test" / f"{record_id}.json").exists()
            )
            self.assertTrue(
                list((root / "raw" / "sources" / "endpoint_test" / "details" / record_id).glob("*"))
            )
            failures = root / "work" / "source_runs" / report["run_id"] / "failures.jsonl"
            self.assertIn("bad detail", failures.read_text())
            checkpoint = json.loads(
                (root / "work" / "checkpoints" / "sources" / "endpoint_test.json").read_text()
            )
            self.assertNotIn(record_id, checkpoint["records"])


class PublicationAndRuntimeTests(unittest.TestCase):
    def test_atomic_directory_publish_replaces_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "canonical"
            staged = root / "staged"
            target.mkdir()
            staged.mkdir()
            (target / "old.txt").write_text("old")
            (staged / "new.txt").write_text("new")
            publish_directory_atomic(staged, target)
            self.assertFalse((target / "old.txt").exists())
            self.assertEqual("new", (target / "new.txt").read_text())

    def test_atomic_directory_publish_rolls_back_on_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "canonical"
            staged = root / "staged"
            target.mkdir()
            staged.mkdir()
            (target / "old.txt").write_text("old")
            (staged / "new.txt").write_text("new")
            real_replace = os.replace
            calls = 0

            def flaky(source: Path, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("publish failed")
                real_replace(source, destination)

            with patch("csrc_law_crawler.core.io.os.replace", side_effect=flaky):
                with self.assertRaisesRegex(OSError, "publish failed"):
                    publish_directory_atomic(staged, target)
            self.assertEqual("old", (target / "old.txt").read_text())

    def test_run_context_maps_exit_two_to_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = RunContext.create(runs_root=Path(tmp), stage="test", argv=[], settings={})
            context.finish(exit_code=2)
            manifest = json.loads(context.manifest_path.read_text())
            self.assertEqual("incomplete", manifest["status"])


class WechatAndCatalogGateTests(unittest.TestCase):
    def _wechat_fixture(self, root: Path, *, comments: object = None) -> None:
        article = {
            "aid": "1001",
            "fakeid": "fake-account",
            "title": "测试文章",
            "content": "微信公众号正文",
            "comments": comments,
            "_accountName": "基小律",
            "link": "https://mp.weixin.qq.com/s/test",
        }
        (root / JSON_FILE_NAME).write_text(json.dumps([article], ensure_ascii=False))
        html_dir = root / "1001"
        html_dir.mkdir()
        (html_dir / "index.html").write_text(
            '<html><body><div id="js_content"><script>alert(1)</script>'
            '<iframe src="bad"></iframe><a href="https://www.csrc.gov.cn/rule">规则</a>'
            "正文</div><!-- 评论数据 --></body></html>",
            encoding="utf-8",
        )

    def test_wechat_import_pairs_html_and_json_and_sanitizes(self) -> None:
        with tempfile.TemporaryDirectory() as incoming, tempfile.TemporaryDirectory() as output:
            input_root = Path(incoming)
            self._wechat_fixture(input_root)
            registry = load_registry()
            registry["wechat"]["wechat_jixiaolv"]["expected_fakeid"] = "fake-account"
            report = import_wechat_bundle(input_root, root=Path(output), registry=registry)
            self.assertEqual("complete", report["status"])
            record_path = next(
                (Path(output) / "raw" / "sources" / "records" / "wechat").glob("*.json")
            )
            record = json.loads(record_path.read_text())
            self.assertEqual("clue", record["material_lane"])
            self.assertNotIn("<script", record["content"]["html"])
            self.assertNotIn("<iframe", record["content"]["html"])
            manifest = (
                Path(output)
                / "raw"
                / "wechat"
                / "imports"
                / report["batch_id"]
                / "import_manifest.json"
            )
            self.assertTrue(manifest.is_file())

    def test_wechat_rejects_actual_json_comments(self) -> None:
        with tempfile.TemporaryDirectory() as incoming, tempfile.TemporaryDirectory() as output:
            input_root = Path(incoming)
            self._wechat_fixture(input_root, comments=[{"content": "评论"}])
            registry = load_registry()
            registry["wechat"]["wechat_jixiaolv"]["expected_fakeid"] = "fake-account"
            with self.assertRaisesRegex(ValueError, "comments must be disabled"):
                import_wechat_bundle(input_root, root=Path(output), registry=registry)

    def test_wechat_rejects_actual_html_comments_but_not_empty_marker(self) -> None:
        with tempfile.TemporaryDirectory() as incoming, tempfile.TemporaryDirectory() as output:
            input_root = Path(incoming)
            self._wechat_fixture(input_root)
            html_path = input_root / "1001" / "index.html"
            html_path.write_text(
                '<div id="js_content">正文</div><!-- 评论数据 --><div>真实评论</div>',
                encoding="utf-8",
            )
            registry = load_registry()
            registry["wechat"]["wechat_jixiaolv"]["expected_fakeid"] = "fake-account"
            with self.assertRaisesRegex(ValueError, "comments must be disabled"):
                import_wechat_bundle(input_root, root=Path(output), registry=registry)

    def test_wechat_requires_matching_fakeid_and_html_pair(self) -> None:
        with tempfile.TemporaryDirectory() as incoming, tempfile.TemporaryDirectory() as output:
            input_root = Path(incoming)
            self._wechat_fixture(input_root)
            registry = load_registry()
            registry["wechat"]["wechat_jixiaolv"]["expected_fakeid"] = "different"
            with self.assertRaisesRegex(ValueError, "fakeid mismatch"):
                import_wechat_bundle(input_root, root=Path(output), registry=registry)
            (input_root / "1001" / "index.html").unlink()
            registry["wechat"]["wechat_jixiaolv"]["expected_fakeid"] = "fake-account"
            with self.assertRaisesRegex(FileNotFoundError, "missing HTML export"):
                import_wechat_bundle(input_root, root=Path(output), registry=registry)

    def test_wechat_duplicate_import_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as incoming, tempfile.TemporaryDirectory() as output:
            input_root = Path(incoming)
            output_root = Path(output)
            self._wechat_fixture(input_root)
            registry = load_registry()
            registry["wechat"]["wechat_jixiaolv"]["expected_fakeid"] = "fake-account"
            first = import_wechat_bundle(input_root, root=output_root, registry=registry)
            second = import_wechat_bundle(input_root, root=output_root, registry=registry)
            self.assertEqual(first["batch_id"], second["batch_id"])
            self.assertEqual(
                1,
                len(list((output_root / "raw" / "sources" / "records" / "wechat").glob("*.json"))),
            )
            self.assertEqual(2, len(first["files"]))

    def test_catalog_gate_only_loads_complete_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "raw" / "sources" / "records" / "test"
            records.mkdir(parents=True)
            base = {
                "schema_version": 1,
                "source_system": "test",
                "metadata": {"name": "规则"},
                "content": {"plain_text": "正文"},
                "source": {"page_url": "https://example.test/rule"},
                "assets": [],
            }
            for record_id, status, lane in [
                ("rule", "complete", "rule"),
                ("failed", "incomplete", "rule"),
                ("case", "complete", "case"),
            ]:
                (records / f"{record_id}.json").write_text(
                    json.dumps(
                        {
                            **base,
                            "source_record_id": record_id,
                            "ingest_status": status,
                            "material_lane": lane,
                        }
                    )
                )
            for record_id, scope_status in [("stale", None), ("matched", "matched")]:
                (records / f"{record_id}.json").write_text(
                    json.dumps(
                        {
                            **base,
                            "source_record_id": record_id,
                            "ingest_status": "complete",
                            "material_lane": "rule",
                            "source": {
                                "endpoint_id": "csrc_7d5d5adf7a",
                                "scope_status": scope_status,
                            },
                        }
                    )
                )
            original = storage.OUTPUT_DIR
            try:
                storage.OUTPUT_DIR = root
                loaded = _multi_source_rule_records()
            finally:
                storage.OUTPUT_DIR = original
            self.assertEqual(["matched", "rule"], [item["record_id"] for item in loaded])


if __name__ == "__main__":
    unittest.main()
