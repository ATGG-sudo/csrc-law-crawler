from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from build_catalog import _multi_source_rule_records
from csrc_law_crawler.core.io import publish_directory_atomic
from csrc_law_crawler.sources.adapters import DATE_RE, HttpHtmlAdapter
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
        self.reported_total: int | None = 1
        self.result_limit_reached = False
        self.response_body = b"<html>volatile=1</html>"
        self.plain_text = "第一条 稳定正文"
        self.metadata = {"name": "测试规则", "pub_date": "2026-01-01"}
        self.discover_calls = 0
        self.return_not_modified = False
        self.not_modified_headers: dict[str, str] = {}

    def healthcheck(self, endpoint: dict) -> dict:
        return {
            "access_status": "reachable",
            "status_code": 200,
            "final_url": endpoint["url"],
            "content_type": "text/html",
            "_body": b"health",
        }

    def discover(self, endpoint: dict, registry: dict, checkpoint: dict) -> dict:
        self.discover_calls += 1
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

    def fetch(self, endpoint: dict, item: dict, previous: dict | None = None) -> dict:
        if self.return_not_modified and previous is not None:
            return {
                "not_modified": True,
                "status_code": 304,
                "final_url": item["url"],
                "headers": copy.deepcopy(self.not_modified_headers),
            }
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
    def test_generic_date_extraction_requires_complete_calendar_date(self) -> None:
        self.assertIsNone(DATE_RE.search("发布于2026年1月"))
        self.assertEqual("2026年1月9日", DATE_RE.search("发布于2026年1月9日").group(0))

    def test_health_response_is_reused_for_root_discovery(self) -> None:
        response = type(
            "Response",
            (),
            {
                "url": "https://example.test/rules/",
                "status_code": 200,
                "content": b"<html><body><p>empty directory</p></body></html>",
                "headers": {"Content-Type": "text/html"},
                "raise_for_status": lambda self: None,
            },
        )()
        calls = 0

        def get(url: str):
            nonlocal calls
            calls += 1
            return response

        adapter = HttpHtmlAdapter()
        adapter._get = get  # type: ignore[assignment, method-assign]
        registry = _registry("catalog_filter")
        endpoint = registry["endpoints"][0]
        endpoint["adapter"] = "http_html"
        endpoint["url"] = response.url
        adapter.healthcheck(endpoint)
        adapter.discover(endpoint, registry, {})
        self.assertEqual(1, calls)

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
        adapter._get = lambda url: response  # type: ignore[assignment, misc]
        registry = _registry("catalog_filter")
        endpoint = registry["endpoints"][0]
        endpoint["adapter"] = "http_html"
        endpoint["url"] = response.url
        result = adapter.discover(endpoint, registry, {})
        self.assertEqual("incomplete", result["discovery_status"])
        self.assertEqual("unproven_single_page_directory", result["completeness_evidence"])
        self.assertEqual(1, result["raw_hit_count"])

    def test_checked_registry_has_87_endpoints_and_88_profiles(self) -> None:
        registry = load_registry()
        self.assertEqual(87, len(registry["endpoints"]))
        self.assertEqual(88, sum(len(item["profiles"]) for item in registry["endpoints"]))
        self.assertEqual(87, len({item["url"] for item in registry["endpoints"]}))
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
    def test_resume_skips_finished_incomplete_endpoint_unless_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeAdapter()
            adapter.discovery_status = "incomplete"
            runner = SourceRunner(
                registry=_registry(), adapter_factory=lambda name: adapter, root=Path(tmp)
            )
            first = runner.run(mode="baseline")
            runner.run(mode="baseline", resume_run_id=first["run_id"])
            self.assertEqual(1, adapter.discover_calls)
            runner.run(
                mode="baseline",
                resume_run_id=first["run_id"],
                retry_incomplete=True,
            )
            self.assertEqual(2, adapter.discover_calls)

    def test_resume_retries_failed_endpoint_only_when_requested(self) -> None:
        class FailingAdapter(FakeAdapter):
            def discover(self, endpoint: dict, registry: dict, checkpoint: dict) -> dict:
                self.discover_calls += 1
                raise RuntimeError("discovery failed")

        with tempfile.TemporaryDirectory() as tmp:
            adapter = FailingAdapter()
            runner = SourceRunner(
                registry=_registry(), adapter_factory=lambda name: adapter, root=Path(tmp)
            )
            first = runner.run(mode="baseline")
            runner.run(mode="baseline", resume_run_id=first["run_id"])
            self.assertEqual(1, adapter.discover_calls)
            runner.run(
                mode="baseline",
                resume_run_id=first["run_id"],
                retry_failed=True,
            )
            self.assertEqual(2, adapter.discover_calls)

    def test_http_304_reuses_previous_record_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = FakeAdapter()
            runner = SourceRunner(
                registry=_registry(), adapter_factory=lambda name: adapter, root=root
            )
            runner.run(mode="baseline")
            adapter.return_not_modified = True
            with patch(
                "csrc_law_crawler.sources.runner.save_json",
                wraps=storage.save_json,
            ) as save:
                report = runner.run(mode="incremental")
            record_writes = [
                call for call in save.call_args_list if "/raw/sources/records/" in str(call.args[0])
            ]
            self.assertEqual(0, len(record_writes))
            self.assertEqual(1, report["endpoints"]["endpoint_test"]["not_modified"])

    def test_http_304_enriches_case_insensitive_response_validators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = FakeAdapter()
            runner = SourceRunner(
                registry=_registry(), adapter_factory=lambda name: adapter, root=root
            )
            runner.run(mode="baseline")
            adapter.return_not_modified = True
            adapter.not_modified_headers = {
                "Etag": '"fixture-etag"',
                "Last-Modified": "Wed, 22 Jul 2026 00:00:00 GMT",
            }

            runner.run(mode="incremental")

            record_path = next((root / "raw/sources/records/test").glob("*.json"))
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                {
                    "etag": '"fixture-etag"',
                    "last_modified": "Wed, 22 Jul 2026 00:00:00 GMT",
                },
                record["source"]["http_validators"],
            )

    def test_prefetched_direct_asset_avoids_second_network_request(self) -> None:
        class NoNetworkSession:
            def get(self, *args, **kwargs):
                raise AssertionError("prefetched asset must not be downloaded again")

        with tempfile.TemporaryDirectory() as tmp:
            runner = SourceRunner(
                registry=_registry(),
                adapter_factory=lambda name: FakeAdapter(),
                root=Path(tmp),
            )
            runner.asset_session = NoNetworkSession()  # type: ignore[assignment]
            assets, _, _ = runner._download_assets(
                _registry()["endpoints"][0],
                "record",
                [
                    {
                        "source_url": "https://example.test/a.zip",
                        "file_name": "a.zip",
                        "_prefetched_body": b"PK-prefetched",
                        "_prefetched_content_type": "application/zip",
                        "_prefetched_final_url": "https://example.test/a.zip",
                    }
                ],
                Path(tmp) / "failures.jsonl",
                None,
            )
            self.assertEqual("complete", assets[0]["download_status"])

    def test_checkpoint_is_written_once_after_endpoint_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = FakeAdapter()
            adapter.items = [
                {
                    "url": f"https://example.test/detail/{index}",
                    "title": f"测试规则 {index}",
                    "upstream_id": f"upstream-{index}",
                    "discovery_evidence": [{"page": 1}],
                }
                for index in range(3)
            ]
            adapter.reported_total = len(adapter.items)
            with patch(
                "csrc_law_crawler.sources.runner.save_json",
                wraps=storage.save_json,
            ) as save:
                SourceRunner(
                    registry=_registry(), adapter_factory=lambda name: adapter, root=root
                ).run(mode="baseline")

            checkpoint_writes = [
                call
                for call in save.call_args_list
                if "work/checkpoints/sources" in str(call.args[0])
            ]
            self.assertEqual(1, len(checkpoint_writes))
            checkpoint = json.loads(
                (root / "work" / "checkpoints" / "sources" / "endpoint_test.json").read_text()
            )
            self.assertEqual(3, len(checkpoint["records"]))

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
            with patch(
                "csrc_law_crawler.sources.runner.save_json",
                wraps=storage.save_json,
            ) as save:
                incremental = runner.run(mode="incremental")
            self.assertFalse(
                (root / "work" / "changes" / f"{incremental['run_id']}.jsonl").exists()
            )
            self.assertFalse(
                [
                    call
                    for call in save.call_args_list
                    if "/raw/sources/records/" in str(call.args[0])
                ]
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

    def test_overlapping_endpoints_do_not_oscillate_owner_metadata(self) -> None:
        class EndpointMetadataAdapter(FakeAdapter):
            def parse(self, endpoint: dict, item: dict, fetched: dict) -> dict:
                parsed = super().parse(endpoint, item, fetched)
                parsed["metadata"]["publisher"] = endpoint["profiles"][0]["publisher"]
                return parsed

        registry = _registry()
        second = copy.deepcopy(registry["endpoints"][0])
        second["endpoint_id"] = "endpoint_other"
        second["url"] = "https://other.test/list"
        second["profiles"][0]["profile_id"] = "profile_other"
        second["profiles"][0]["publisher"] = "另一机关"
        registry["endpoints"].append(second)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = SourceRunner(
                registry=registry,
                adapter_factory=lambda name: EndpointMetadataAdapter(),
                root=root,
            )
            runner.run(mode="baseline", workers=1)
            incremental = runner.run(mode="incremental", workers=1)
            changes = root / "work" / "changes" / f"{incremental['run_id']}.jsonl"
            self.assertFalse(changes.exists())
            record_id = source_record_id("test", upstream_id="upstream-1")
            record = json.loads(
                (root / "raw" / "sources" / "records" / "test" / f"{record_id}.json").read_text()
            )
            self.assertEqual("endpoint_test", record["source"]["endpoint_id"])
            self.assertEqual("测试机关", record["metadata"]["publisher"])
            self.assertEqual(
                ["profile_other", "profile_test"],
                record["source"]["profiles"],
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

    def test_subject_query_resume_skips_first_100_of_2660_and_cache_skips_all(self) -> None:
        class SubjectAdapter:
            def __init__(self, interrupt_after: int | None = None) -> None:
                self.calls = 0
                self.interrupt_after = interrupt_after

            def healthcheck(self, endpoint: dict) -> dict:
                return {
                    "access_status": "reachable",
                    "status_code": 200,
                    "final_url": endpoint["url"],
                    "content_type": "text/html",
                    "_body": b"health",
                }

            def discover(self, endpoint: dict, registry: dict, checkpoint: dict) -> dict:
                del registry, checkpoint
                self.calls += 1
                if self.interrupt_after is not None and self.calls > self.interrupt_after:
                    raise KeyboardInterrupt
                return {
                    "items": [],
                    "raw_pages": [],
                    "discovery_status": "complete",
                    "pages_completed": 1,
                    "reported_total": 0,
                    "queries_completed": 1,
                    "queries_total": 1,
                    "failures": [],
                }

            def fetch(
                self,
                endpoint: dict,
                item: dict,
                previous: dict | None = None,
            ) -> dict:
                raise AssertionError("zero-result subject queries do not fetch details")

            def parse(self, endpoint: dict, item: dict, fetched: dict) -> dict:
                raise AssertionError("zero-result subject queries do not parse details")

        endpoint = copy.deepcopy(_registry()["endpoints"][0])
        endpoint.update(
            {
                "endpoint_id": "subject_test",
                "url": "https://gs.amac.org.cn/amac-infodisc/res/pof/manager/index.html",
                "adapter": "subject_query",
                "scope_mode": "subject_query",
                "default_material_lane": "subject_snapshot",
            }
        )
        registry = {
            "schema_version": 1,
            "query_set_version": "subject-v1",
            "query_sets": {},
            "endpoints": [endpoint],
        }
        seeds = [
            {
                "seed_id": f"subject_{index:04d}",
                "entity_type": "institution",
                "normalized_name": f"机构{index}",
                "query_targets": ["amac_institution"],
                "ambiguous": False,
            }
            for index in range(2660)
        ]
        seed_doc = {
            "schema_version": 1,
            "count": len(seeds),
            "queryable_count": len(seeds),
            "items": seeds,
        }

        with (
            tempfile.TemporaryDirectory(dir="/tmp") as tmp,
            patch(
                "csrc_law_crawler.sources.subjects.build_subject_seeds",
                return_value=seed_doc,
            ),
        ):
            root = Path(tmp)
            interrupted = SubjectAdapter(interrupt_after=100)
            with self.assertRaises(KeyboardInterrupt):
                SourceRunner(
                    registry=registry,
                    adapter_factory=lambda name: interrupted,
                    root=root,
                ).run(mode="baseline")
            manifest_path = next((root / "work" / "source_runs").glob("*/manifest.json"))
            run_id = manifest_path.parent.name
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual("interrupted", manifest["status"])
            self.assertEqual(100, interrupted.calls - 1)

            resumed = SubjectAdapter()
            report = SourceRunner(
                registry=registry,
                adapter_factory=lambda name: resumed,
                root=root,
            ).run(mode="baseline", resume_run_id=run_id)
            self.assertEqual(2560, resumed.calls)
            self.assertEqual("complete", report["status"])

            cached = SubjectAdapter()
            cached_report = SourceRunner(
                registry=registry,
                adapter_factory=lambda name: cached,
                root=root,
            ).run(mode="baseline")
            self.assertEqual(0, cached.calls)
            self.assertEqual(2660, cached_report["endpoints"]["subject_test"]["cached_queries"])

    def test_workers_parallelize_hosts_but_serialize_each_host(self) -> None:
        class Tracker:
            def __init__(self) -> None:
                self.lock = threading.Lock()
                self.active = 0
                self.global_max = 0
                self.by_host: dict[str, int] = {}
                self.host_max: dict[str, int] = {}

            def enter(self, host: str) -> None:
                with self.lock:
                    self.active += 1
                    self.global_max = max(self.global_max, self.active)
                    self.by_host[host] = self.by_host.get(host, 0) + 1
                    self.host_max[host] = max(self.host_max.get(host, 0), self.by_host[host])

            def leave(self, host: str) -> None:
                with self.lock:
                    self.active -= 1
                    self.by_host[host] -= 1

        class TrackingAdapter(FakeAdapter):
            def __init__(self, tracker: Tracker) -> None:
                super().__init__()
                self.tracker = tracker
                self.items = []
                self.reported_total = 0

            def healthcheck(self, endpoint: dict) -> dict:
                host = endpoint["url"].split("/", 3)[2]
                self.tracker.enter(host)
                try:
                    time.sleep(0.05)
                finally:
                    self.tracker.leave(host)
                return super().healthcheck(endpoint)

        endpoints = []
        for host_index in range(4):
            for endpoint_index in range(2):
                endpoint = copy.deepcopy(_registry()["endpoints"][0])
                endpoint["endpoint_id"] = f"endpoint_{host_index}_{endpoint_index}"
                endpoint["url"] = f"https://host-{host_index}.test/{endpoint_index}"
                endpoints.append(endpoint)
        registry = {
            "schema_version": 1,
            "query_set_version": "parallel-v1",
            "query_sets": {"q": ["私募"]},
            "endpoints": endpoints,
        }
        tracker = Tracker()
        with tempfile.TemporaryDirectory() as tmp:
            report = SourceRunner(
                registry=registry,
                adapter_factory=lambda name: TrackingAdapter(tracker),
                root=Path(tmp),
            ).run(mode="baseline", workers=4)
        self.assertEqual("complete", report["status"])
        self.assertGreaterEqual(tracker.global_max, 2)
        self.assertTrue(all(value == 1 for value in tracker.host_max.values()))


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

    def test_atomic_directory_publish_preserves_target_when_backup_move_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "canonical"
            staged = root / "staged"
            target.mkdir()
            staged.mkdir()
            (target / "old.txt").write_text("old")
            (staged / "new.txt").write_text("new")

            with (
                patch(
                    "csrc_law_crawler.core.io.os.replace",
                    side_effect=PermissionError("target is busy"),
                ),
                patch("csrc_law_crawler.core.io.time.sleep"),
            ):
                with self.assertRaisesRegex(PermissionError, "target is busy"):
                    publish_directory_atomic(staged, target)

            self.assertEqual("old", (target / "old.txt").read_text())
            self.assertEqual("new", (staged / "new.txt").read_text())

    def test_atomic_directory_publish_retries_transient_permission_error(self) -> None:
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
                if calls == 1:
                    raise PermissionError("target is briefly busy")
                real_replace(source, destination)

            with (
                patch("csrc_law_crawler.core.io.os.replace", side_effect=flaky),
                patch("csrc_law_crawler.core.io.time.sleep") as sleep,
            ):
                publish_directory_atomic(staged, target)

            sleep.assert_called_once()
            self.assertEqual("new", (target / "new.txt").read_text())

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
                ("clue", "complete", "clue"),
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
