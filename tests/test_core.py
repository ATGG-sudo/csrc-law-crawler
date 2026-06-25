from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from build_catalog import choose_neris_match, normalize_title
from client import HumanLikeClient
from export_markdown_catalog import bucket_for_document
from normalize_catalog import plain_text_to_markdown
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
