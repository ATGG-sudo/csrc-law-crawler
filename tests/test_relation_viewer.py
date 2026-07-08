from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import relation_viewer
from storage import save_json


class RelationViewerTests(unittest.TestCase):
    def _write_fixture_package(self, root: Path) -> None:
        save_json(
            root / "canonical" / "relations" / "graph.json",
            {
                "nodes": [
                    {
                        "id": "law_1",
                        "type": "law",
                        "title": "测试制度",
                        "status": "current",
                        "local_file": "canonical/json/law_1.json",
                    },
                    {
                        "id": "neris:missing",
                        "type": "law_stub",
                        "title": "目录外法规",
                        "source_system": "neris",
                        "source_record_id": "missing",
                    },
                    {
                        "id": "writ:w1",
                        "type": "writ",
                        "title": "处罚决定书",
                    },
                ],
                "edges": [
                    {
                        "from": "law_1",
                        "to": "writ:w1",
                        "relation": "cited_by_case",
                        "source": "case_fixture",
                        "confidence": 0.9,
                    },
                    {
                        "from": "law_1",
                        "to": "neris:missing",
                        "relation": "related_to",
                        "source": "related_fixture",
                        "evidence": {"rule_id": "fixture.related"},
                    },
                ],
            },
        )
        save_json(
            root / "canonical" / "json" / "law_1.json",
            {
                "id": "law_1",
                "title": "测试制度",
                "document_type": "regulation",
                "status": "current",
                "metadata": {
                    "fileno": "证监会令第1号",
                    "pub_org": "中国证监会",
                    "pub_date": "2024-01-01",
                    "effective_date": "2024-02-01",
                },
                "effectiveness": {"status": "current", "basis": "fixture"},
                "sources": [{"system": "neris"}, {"system": "amac"}],
            },
        )
        save_json(
            root / "canonical" / "indexes" / "source_map.json",
            {"by_source": {"neris:n1": "law_1"}},
        )

    def test_build_viewer_payload_enriches_nodes_and_rankings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("storage.OUTPUT_DIR", root), patch(
                "relation_viewer.utc_now_iso",
                return_value="2026-06-30T00:00:00+00:00",
            ):
                self._write_fixture_package(root)
                payload = relation_viewer.build_viewer_payload(rank_limit=5)

        self.assertEqual(3, payload["counts"]["nodes"])
        self.assertEqual(2, payload["counts"]["edges"])
        self.assertEqual({"law": 1, "stub": 1, "writ": 1}, payload["counts"]["node_kinds"])
        self.assertEqual(1, payload["counts"]["source_map_entries"])

        law = next(node for node in payload["nodes"] if node["id"] == "law_1")
        self.assertEqual("证监会令第1号", law["fileno"])
        self.assertEqual("中国证监会", law["pub_org"])
        self.assertEqual(2, law["source_count"])
        stub = next(node for node in payload["nodes"] if node["id"] == "neris:missing")
        self.assertFalse(stub["nameless"])
        self.assertFalse(stub["raw_exists"])
        self.assertEqual("raw/neris/laws/reg_missing.json", stub["raw_file"])
        self.assertEqual(0, payload["counts"]["nameless_stub_nodes"])
        self.assertEqual(1, payload["counts"]["raw_missing_stub_nodes"])

        self.assertEqual("law_1", payload["rankings"]["high_cited_laws"][0]["id"])
        self.assertEqual("neris:missing", payload["rankings"]["stub_nodes"][0]["id"])
        self.assertEqual(
            {"related_to": 1},
            payload["rankings"]["stub_nodes"][0]["relations"],
        )

    def test_export_relation_viewer_writes_static_html_and_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("storage.OUTPUT_DIR", root), patch(
                "relation_viewer.utc_now_iso",
                return_value="2026-06-30T00:00:00+00:00",
            ):
                self._write_fixture_package(root)
                manifest = relation_viewer.export_relation_viewer(rank_limit=5)

            html_path = root / "reports" / "relation_viewer" / "index.html"
            payload_path = root / "reports" / "relation_viewer" / "payload.json"
            self.assertTrue(html_path.exists())
            self.assertTrue(payload_path.exists())
            html = html_path.read_text(encoding="utf-8")
            payload = json.loads(payload_path.read_text(encoding="utf-8"))

        self.assertEqual("reports/relation_viewer/index.html", manifest["viewer_file"])
        self.assertEqual("reports/relation_viewer/payload.json", manifest["payload_file"])
        self.assertIn("CSRC 制度关系图查看器", html)
        self.assertIn("测试制度", html)
        self.assertIn('id="stubFilter"', html)
        self.assertIn("隐藏无名 stub", html)
        self.assertIn("stubMode: 'hide_unnamed'", html)
        self.assertNotIn(relation_viewer.HTML_DATA_PLACEHOLDER, html)
        self.assertEqual(3, payload["counts"]["nodes"])


if __name__ == "__main__":
    unittest.main()
