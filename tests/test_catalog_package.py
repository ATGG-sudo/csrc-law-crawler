from __future__ import annotations

import unittest

from csrc_law_crawler.processing.catalog import (
    build_catalog_relations,
    catalog_manifest,
    choose_neris_match_with_rule,
    normalize_title,
    review_queue_items,
)


class CatalogPackageBoundaryTests(unittest.TestCase):
    def test_package_exports_catalog_identity_and_matching_helpers(self) -> None:
        neris = {
            "record_id": "n1",
            "metadata": {
                "name": " Example Rule ",
                "fileno": "No. 1",
                "pub_date": "2026-01-01",
            },
            "assets": [],
        }
        amac = {
            "record_id": "a1",
            "metadata": {
                "name": "Example Rule",
                "fileno": "No. 1",
                "pub_date": "2026-01-02",
            },
            "assets": [],
        }

        match, status, confidence, evidence, rule_id = choose_neris_match_with_rule(
            amac,
            {normalize_title("Example Rule"): [neris]},
        )

        self.assertIs(match, neris)
        self.assertEqual("same_document", status)
        self.assertGreater(confidence, 0.9)
        self.assertTrue(evidence)
        self.assertTrue(rule_id)

    def test_package_exports_catalog_relations_and_manifest_helpers(self) -> None:
        neris_records = [{"record_id": "parent", "metadata": {"name": "Example Rule"}}]
        amac_records = [
            {"record_id": "parent", "parent_record_id": None, "metadata": {}},
            {"record_id": "child", "parent_record_id": "parent", "metadata": {}},
        ]
        entities = {
            "law_parent": {
                "id": "law_parent",
                "title": "Parent Rule",
                "document_type": "regulation",
                "status": "current",
                "sources": [],
                "metadata": {},
            },
            "law_child": {
                "id": "law_child",
                "title": "Child Rule",
                "document_type": "regulation",
                "status": "current",
                "sources": [],
                "metadata": {},
            },
        }
        source_to_entity = {
            ("neris", "parent"): "law_parent",
            ("amac", "parent"): "law_parent",
            ("amac", "child"): "law_child",
        }

        relations = build_catalog_relations(
            neris_records=neris_records,
            amac_records=amac_records,
            entities=entities,
            source_to_entity=source_to_entity,
        )
        review_items = review_queue_items(
            [
                {
                    "record_id": "child",
                    "metadata": {
                        "name": "Child Rule",
                        "document_type": "self_regulatory_rule",
                        "status": "unknown",
                    },
                }
            ],
            source_to_entity=source_to_entity,
            matches={"child": {"match_status": "new_to_neris", "confidence": 0.5}},
        )
        manifest = catalog_manifest(
            neris_records=neris_records,
            amac_records=amac_records,
            entities=entities,
            relations=relations,
            review_items=review_items,
            matches={"child": {"match_status": "new_to_neris"}},
        )

        self.assertEqual(1, len(relations))
        self.assertEqual("law_parent", relations[0]["from"])
        self.assertEqual("law_child", relations[0]["to"])
        self.assertEqual("publishes", relations[0]["relation"])
        self.assertEqual("amac.page_attachment", relations[0]["source"])
        self.assertEqual(1, len(review_items))
        self.assertEqual(2, manifest["canonical_laws"])
        self.assertEqual({"new_to_neris": 1}, manifest["match_counts"])


if __name__ == "__main__":
    unittest.main()
