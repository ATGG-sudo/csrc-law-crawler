from __future__ import annotations

import unittest

from build_catalog import choose_neris_match, normalize_title
from normalize_catalog import plain_text_to_markdown
from revisions_graph import UnionFind, build_revisions_document


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


if __name__ == "__main__":
    unittest.main()
