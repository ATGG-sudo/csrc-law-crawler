from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from audit_canonical_duplicates import build_dedupe_plan, build_duplicate_report, markdown_main_body


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _markdown(law_id: str, title: str, body: str) -> str:
    return f"""---
id: "{law_id}"
title: "{title}"
content_status: "full_text"
source_file: "work/catalog/laws/{law_id}.json"
---

# {title}

| 字段 | 值 |
| --- | --- |
| 统一法规 ID | {law_id} |

{body}

## 官方来源

| 来源 | 角色 | 记录 ID | 链接 |
| --- | --- | --- | --- |
| amac | official_text | source | [官网](https://example.com) |
"""


class DuplicateAuditTests(unittest.TestCase):
    def test_markdown_main_body_strips_generated_sections(self) -> None:
        text = _markdown("law_a", "规则甲", "第一条 主正文。")

        self.assertEqual("第一条 主正文。", markdown_main_body(text))

    def test_duplicate_report_groups_json_and_markdown_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            json_dir = root / "canonical" / "json"
            md_dir = root / "canonical" / "markdown" / "current"

            body = "第一条 重复正文。第二条 重复正文。" * 8
            docs = [
                ("law_a", "规则甲", body, "same-sha"),
                ("law_b", "规则甲", "第一条 另一个正文。" * 8, "same-sha"),
                ("law_c", "规则乙", body, ""),
            ]
            manifest_items = []
            for law_id, title, plain, asset_sha in docs:
                _write_json(
                    json_dir / f"{law_id}.json",
                    {
                        "id": law_id,
                        "title": title,
                        "content_status": "full_text",
                        "metadata": {
                            "fileno": "",
                            "pub_date": "2026-01-01",
                        },
                        "preferred_source": {"system": "amac"},
                        "source_file": f"work/catalog/laws/{law_id}.json",
                        "full_text_plain": plain,
                        "assets": (
                            [{"sha256": asset_sha, "asset_id": f"asset_{law_id}"}]
                            if asset_sha
                            else []
                        ),
                    },
                )
                md_path = md_dir / f"{title} - {law_id.removeprefix('law_')[:8]}.md"
                md_path.parent.mkdir(parents=True, exist_ok=True)
                md_path.write_text(_markdown(law_id, title, plain), encoding="utf-8")
                manifest_items.append(
                    {
                        "id": law_id,
                        "title": title,
                        "bucket": "current",
                        "source_file": f"canonical/json/{law_id}.json",
                        "file": str(md_path.relative_to(root)),
                        "text_length": len(plain),
                    }
                )

            placeholder_path = md_dir / "规则丙.md"
            placeholder_path.write_text(
                _markdown(
                    "law_d",
                    "规则丙",
                    "> 正文未能从官方文件中自动抽取；请参阅下方官方来源或本地附件。",
                ),
                encoding="utf-8",
            )
            manifest_items.append(
                {
                    "id": "law_d",
                    "title": "规则丙",
                    "bucket": "current",
                    "source_file": "canonical/json/law_d.json",
                    "file": str(placeholder_path.relative_to(root)),
                    "text_length": 0,
                }
            )

            _write_json(
                root / "canonical" / "json" / "law_d.json",
                {
                    "id": "law_d",
                    "title": "规则丙",
                    "content_status": "metadata_only",
                    "metadata": {},
                    "preferred_source": {"system": "amac"},
                    "source_file": "work/catalog/laws/law_d.json",
                    "full_text_plain": "",
                    "assets": [],
                },
            )
            _write_json(
                root / "work" / "catalog" / "markdown_manifest.json",
                {"items": manifest_items},
            )

            report = build_duplicate_report(root)
            dedupe_plan = build_dedupe_plan(report)

        summary = report["summary"]
        self.assertEqual(4, summary["json_count"])
        self.assertEqual(4, summary["markdown_manifest_items"])
        self.assertEqual(1, summary["json_title_duplicate_groups"])
        self.assertEqual(1, summary["json_body_duplicate_groups"])
        self.assertEqual(1, summary["json_asset_duplicate_groups"])
        self.assertEqual(1, summary["markdown_h1_duplicate_groups"])
        self.assertEqual(1, summary["markdown_body_duplicate_groups"])
        self.assertEqual(1, summary["metadata_only_placeholder_files"])
        self.assertEqual(0, summary["markdown_missing_files"])
        self.assertEqual(0, summary["markdown_orphan_files"])
        kinds = {row["kind"] for row in report["rows"]}
        self.assertIn("markdown_body", kinds)
        self.assertIn("json_asset_sha", kinds)
        self.assertGreaterEqual(dedupe_plan["summary"]["groups"], 1)
        self.assertIn(
            "auto_merge",
            {group["decision"] for group in dedupe_plan["groups"]},
        )
        for row in report["rows"]:
            self.assertEqual(
                {
                    "kind",
                    "severity",
                    "group_id",
                    "id",
                    "title",
                    "bucket",
                    "file",
                    "json_file",
                    "content_status",
                    "text_length",
                    "body_hash",
                    "asset_sha",
                    "source_system",
                    "reason",
                    "recommended_action",
                },
                set(row),
            )


if __name__ == "__main__":
    unittest.main()
