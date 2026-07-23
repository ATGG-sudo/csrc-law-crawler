from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from csrc_law_crawler.processing.catalog.curated_relations import (
    CURATED_CATALOG_PATH,
    curated_document_source_keys,
    curated_documents,
    load_curated_catalog,
    resolve_curated_documents,
)
from csrc_law_crawler.processing.catalog.relations import build_catalog_relations
from csrc_law_crawler.sources.evidence import source_record_id
from normalize_catalog import (
    catalog_relation_refs,
    catalog_superseded_by,
    normalize_catalog_entity,
)
from storage import save_json
from validate_catalog_exports import _validate_curated_semantics


DOCUMENT_IDS = {
    "spc_company_law_interpretation_3_2014": "law_interpretation_3_2014",
    "spc_company_law_interpretation_3_2020": "law_interpretation_3_2020",
    "spc_company_law_temporal_effect_2024_7": "law_temporal_effect_2024_7",
    "spc_company_law_reply_2024_15": "law_reply_2024_15",
    "spc_company_law_transition_guidance_438551": "law_transition_guidance",
}


def _source_to_entity() -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for document_key, document in curated_documents().items():
        for source_key in curated_document_source_keys(document):
            result[source_key] = DOCUMENT_IDS[document_key]
    return result


def _curated_relations() -> list[dict[str, object]]:
    return build_catalog_relations(
        neris_records=[],
        amac_records=[],
        entities={},
        source_to_entity=_source_to_entity(),
    )


def _entity_path(
    root: Path,
    *,
    document_key: str,
    title: str,
    effective_date: str | None = None,
    status: str = "现行有效",
) -> Path:
    entity_id = DOCUMENT_IDS[document_key]
    source_system, record_id = next(
        iter(curated_document_source_keys(curated_documents()[document_key]))
    )
    path = root / "work" / "catalog" / "laws" / f"{entity_id}.json"
    save_json(
        path,
        {
            "schema_version": 1,
            "id": entity_id,
            "title": title,
            "document_type": "regulation",
            "status": status,
            "material_lane": "rule",
            "metadata": {
                "name": title,
                "document_type": "regulation",
                "status": status,
                "effective_date": effective_date,
            },
            "preferred_content": {
                "source_system": source_system,
                "source_record_id": record_id,
                "plain_text": "第一条 本司法解释适用于符合条件的案件。",
            },
            "sources": [
                {
                    "system": source_system,
                    "record_id": record_id,
                    "role": "official_text",
                    "material_lane": "rule",
                    "page_role": "normative_instrument",
                }
            ],
            "assets": [],
        },
    )
    return path


class CuratedCatalogRelationTests(unittest.TestCase):
    def test_config_uses_controlled_upstream_identity_not_canonical_ids(self) -> None:
        payload = load_curated_catalog()
        raw = CURATED_CATALOG_PATH.read_text(encoding="utf-8")
        self.assertNotIn('"canonical_id"', raw)
        self.assertEqual(5, len(payload["documents"]))

        reply_source = curated_document_source_keys(
            curated_documents(payload)["spc_company_law_reply_2024_15"]
        )
        self.assertEqual(
            {
                (
                    "court_judicial_interpretation",
                    source_record_id(
                        "court_judicial_interpretation",
                        upstream_id="450831",
                    ),
                )
            },
            reply_source,
        )
        self.assertEqual(DOCUMENT_IDS, resolve_curated_documents(_source_to_entity(), payload))

    def test_relations_preserve_issue_scope_and_full_version_semantics(self) -> None:
        relations = _curated_relations()
        by_type = {str(item["relation"]): item for item in relations}

        supersedes = by_type["supersedes"]
        self.assertEqual(DOCUMENT_IDS["spc_company_law_interpretation_3_2020"], supersedes["from"])
        self.assertEqual(DOCUMENT_IDS["spc_company_law_interpretation_3_2014"], supersedes["to"])
        self.assertEqual("full_version", supersedes["evidence"]["scope"])

        narrows = by_type["narrows_application_of"]
        self.assertEqual(DOCUMENT_IDS["spc_company_law_reply_2024_15"], narrows["from"])
        self.assertEqual(DOCUMENT_IDS["spc_company_law_temporal_effect_2024_7"], narrows["to"])
        self.assertEqual("provision_issue", narrows["evidence"]["scope"])
        self.assertEqual("第四条第一项", narrows["evidence"]["target_provision"])
        self.assertEqual("remains_current", narrows["evidence"]["effect_on_target"])
        self.assertEqual("after", narrows["evidence"]["event_date_operator"])
        self.assertEqual("2024-07-01", narrows["evidence"]["event_date"])
        self.assertEqual(
            "仅适用于2024年7月1日之后发生的未届出资期限的股权转让行为",
            narrows["evidence"]["applicable_fact_window"],
        )

        qualifies = by_type["qualifies_application_of"]
        self.assertEqual("reference-only", qualifies["evidence"]["scope"])
        self.assertEqual("conditional", qualifies["evidence"]["applicability_mode"])

    def test_version_successor_is_not_also_same_instrument_copy(self) -> None:
        title = "最高人民法院关于适用《中华人民共和国公司法》若干问题的规定（三）"
        old_id = DOCUMENT_IDS["spc_company_law_interpretation_3_2014"]
        new_id = DOCUMENT_IDS["spc_company_law_interpretation_3_2020"]
        entities = {
            old_id: {
                "id": old_id,
                "title": title,
                "metadata": {
                    "name": title,
                    "fileno": "法释〔2011〕3号",
                    "pub_date": "2014-02-20",
                },
            },
            new_id: {
                "id": new_id,
                "title": title,
                "metadata": {
                    "name": title,
                    "fileno": "法释〔2011〕3号",
                    "pub_date": "2020-12-29",
                },
            },
        }

        relations = build_catalog_relations(
            neris_records=[],
            amac_records=[],
            entities=entities,
            source_to_entity=_source_to_entity(),
        )
        pair_relations = {
            str(item["relation"])
            for item in relations
            if {str(item["from"]), str(item["to"])} == {old_id, new_id}
        }

        self.assertEqual({"supersedes"}, pair_relations)

    def test_normalization_keeps_only_old_full_version_historical(self) -> None:
        relations = _curated_relations()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            relations_path = root / "work" / "catalog" / "relations.json"
            save_json(relations_path, {"schema_version": 1, "items": relations})
            with patch("normalize_catalog.catalog_relations_path", return_value=relations_path):
                superseded_by = catalog_superseded_by()
                relation_refs = catalog_relation_refs()

            path_2014 = _entity_path(
                root,
                document_key="spc_company_law_interpretation_3_2014",
                title="最高人民法院关于适用《中华人民共和国公司法》若干问题的规定（三）（2014年修正）",
                effective_date="2014-02-28",
            )
            path_2020 = _entity_path(
                root,
                document_key="spc_company_law_interpretation_3_2020",
                title="最高人民法院关于适用《中华人民共和国公司法》若干问题的规定（三）（2020年修正）",
            )
            path_rule_7 = _entity_path(
                root,
                document_key="spc_company_law_temporal_effect_2024_7",
                title="最高人民法院关于适用《中华人民共和国公司法》时间效力的若干规定",
                status="已废止",
            )

            kwargs = {
                "revision_by_law_id": {},
                "superseded_by_catalog": superseded_by,
                "relations_by_catalog": relation_refs,
                "as_of": "2026-07-22",
            }
            with patch("storage.OUTPUT_DIR", root):
                doc_2014 = normalize_catalog_entity(path_2014, **kwargs)
                doc_2020 = normalize_catalog_entity(path_2020, **kwargs)
                doc_rule_7 = normalize_catalog_entity(path_rule_7, **kwargs)

        self.assertEqual("historical", doc_2014["effectiveness"]["status"])
        self.assertEqual("current", doc_2020["effectiveness"]["status"])
        self.assertEqual("current", doc_rule_7["effectiveness"]["status"])
        self.assertEqual("现行有效", doc_rule_7["status"])
        self.assertEqual([], doc_rule_7["superseded_by"])

        self.assertEqual("2014-03-01", doc_2014["metadata"]["effective_date"])
        self.assertEqual("2014-02-20", doc_2014["metadata"]["pub_date"])
        self.assertEqual("2014-02-17", doc_2014["metadata"]["decision_date"])
        self.assertEqual("2014-02-20", doc_2014["metadata"]["promulgation_date"])
        self.assertEqual(
            "2014-02-27 16:26:00",
            doc_2014["metadata"]["official_page_published_at"],
        )
        self.assertEqual("2014年修正", doc_2014["metadata"]["edition_label"])
        self.assertEqual("法释〔2014〕2号", doc_2014["metadata"]["amending_fileno"])
        self.assertEqual("2014-02-20", doc_2014["metadata"]["revision_date"])
        self.assertEqual("2020年修正", doc_2020["metadata"]["edition_label"])
        self.assertEqual("法释〔2020〕18号", doc_2020["metadata"]["amending_fileno"])
        self.assertEqual("2020-12-29", doc_2020["metadata"]["revision_date"])
        self.assertEqual("2021-01-01", doc_2020["metadata"]["effective_date"])
        self.assertEqual("conditional", doc_2020["metadata"]["applicability_mode"])
        self.assertIn("不存在冲突", doc_2020["metadata"]["applicability_note"])

        self.assertEqual(
            doc_2014["revision_ref"]["family_id"],
            doc_2020["revision_ref"]["family_id"],
        )
        self.assertEqual("2014年修正", doc_2014["revision_ref"]["version_label"])
        self.assertEqual("2020年修正", doc_2020["revision_ref"]["version_label"])

        incoming_rule_7 = doc_rule_7["relations"]["incoming"]
        self.assertEqual(1, len(incoming_rule_7))
        self.assertEqual("narrows_application_of", incoming_rule_7[0]["relation"])
        self.assertEqual("remains_current", incoming_rule_7[0]["evidence"]["effect_on_target"])
        incoming_2020 = doc_2020["relations"]["incoming"]
        self.assertEqual("qualifies_application_of", incoming_2020[0]["relation"])
        self.assertEqual("reference-only", incoming_2020[0]["evidence"]["scope"])

        override_audit = doc_2014["metadata"]["curated_override_evidence"][0]
        self.assertIn("effective_date", override_audit["fields"])
        self.assertEqual(
            "https://www.court.gov.cn/fabu/xiangqing/6135.html",
            override_audit["official_url"],
        )

    def test_config_is_valid_json_for_packaged_data(self) -> None:
        self.assertEqual(
            load_curated_catalog(),
            json.loads(CURATED_CATALOG_PATH.read_text(encoding="utf-8")),
        )

    def test_validation_rejects_silently_missing_curated_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_map_path = Path(temp_dir) / "source_map.json"
            save_json(source_map_path, {"schema_version": 2, "by_source": {}})
            with patch(
                "validate_catalog_exports.source_matches_path",
                return_value=source_map_path,
            ):
                issues, summary = _validate_curated_semantics({}, [])

        self.assertTrue(
            any("curated source resolution incomplete" in issue for issue in issues)
        )
        self.assertEqual(0, summary["documents_resolved"])
        self.assertEqual(0, summary["relations_resolved"])


if __name__ == "__main__":
    unittest.main()
