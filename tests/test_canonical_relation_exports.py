from __future__ import annotations

from pathlib import Path

from export_markdown_catalog import build_catalog_markdown, directional_relation_summary
from relation_services import CanonicalRelationGraphBuilder
from validate_catalog_exports import (
    _catalog_graph_edge_keys,
    _expected_canonical_relation_refs,
    _has_omnibus_parent_page_prefix,
)


def test_markdown_surfaces_scoped_application_relation() -> None:
    doc = {
        "id": "law_15",
        "title": "最高人民法院批复",
        "document_type": "judicial_interpretation",
        "status": "现行有效",
        "content_status": "full_text",
        "effectiveness": {"status": "current"},
        "material_classification": {"lane": "rule"},
        "reference_lifecycle": {},
        "enforcement_classification": {},
        "metadata": {
            "fileno": "法释〔2024〕15号",
            "pub_org": "最高人民法院",
            "edition_label": "2024年发布",
        },
        "preferred_source": {"system": "court_judicial_interpretation"},
        "revision_ref": None,
        "relations": {
            "outgoing": [
                {
                    "canonical_id": "law_7",
                    "relation": "narrows_application_of",
                    "evidence": {
                        "scope": "provision_issue",
                        "target_provision": "第四条第一项",
                        "target_issue": "公司法第八十八条第一款对未届期股权转让的时间适用",
                        "temporal_boundary": "2024-07-01",
                        "applicable_fact_window": (
                            "仅适用于2024年7月1日之后发生的未届出资期限的股权转让行为"
                        ),
                        "event_date_operator": "after",
                        "event_date": "2024-07-01",
                        "relation_effective_date": "2024-12-24",
                        "effect_on_target": "remains_current",
                        "official_url": (
                            "https://www.court.gov.cn/zixun/xiangqing/450831.html"
                        ),
                    },
                }
            ],
            "incoming": [],
        },
        "full_text_markdown": "批复正文。",
        "sources": [],
        "assets": [],
    }

    markdown = build_catalog_markdown(doc, Path("/tmp/law_15.md"))

    assert "## 版本与适用关系" in markdown
    assert "narrows_application_of" in markdown
    assert "第四条第一项" in markdown
    assert "未届期股权转让" in markdown
    assert "remains_current" in markdown
    assert "2024-07-01" in markdown
    assert "之后发生" in markdown
    assert "2024-12-24" in markdown
    assert "law_7" in markdown


def test_relation_graph_node_uses_normalized_effectiveness_and_version() -> None:
    builder = CanonicalRelationGraphBuilder(source_map={}, load_writ=lambda _id: ({}, None))
    builder.add_catalog_entity(
        {
            "id": "law_2014",
            "title": "公司法解释（三）",
            "document_type": "judicial_interpretation",
            "status": "现行有效",
            "effectiveness": {"status": "historical"},
            "metadata": {"edition_label": "2014年修正"},
            "revision_ref": {"family_id": "company-law-interpretation-3"},
        },
        local_file="canonical/json/law_2014.json",
    )

    node = builder.nodes["law_2014"]
    assert node["status"] == "historical"
    assert node["raw_status"] == "现行有效"
    assert node["edition_label"] == "2014年修正"
    assert node["version_family_id"] == "company-law-interpretation-3"


def test_relation_index_summary_preserves_version_direction() -> None:
    doc_2020 = {
        "relations": {
            "outgoing": [
                {"relation": "supersedes", "canonical_id": "law_2014"},
            ],
            "incoming": [],
        }
    }
    doc_2014 = {
        "relations": {
            "outgoing": [],
            "incoming": [
                {"relation": "supersedes", "canonical_id": "law_2020"},
            ],
        }
    }

    assert directional_relation_summary(doc_2020) == (["supersedes:law_2014"], [])
    assert directional_relation_summary(doc_2014) == ([], ["supersedes:law_2020"])


def test_omnibus_title_inside_revision_note_is_not_parent_page_leakage() -> None:
    target_text = (
        "最高人民法院关于适用《中华人民共和国公司法》若干问题的规定（三）"
        "根据《最高人民法院关于修改〈最高人民法院关于破产企业国有划拨土地"
        "使用权应否列入破产财产等问题的批复〉等二十九件商事类司法解释的决定》"
        "第二次修正。"
    )
    parent_text = (
        "最高人民法院关于修改《最高人民法院关于破产企业国有划拨土地使用权"
        "应否列入破产财产等问题的批复》等二十九件商事类司法解释的决定"
    )

    assert not _has_omnibus_parent_page_prefix(target_text)
    assert _has_omnibus_parent_page_prefix(parent_text)


def test_external_relation_requires_only_the_canonical_side_mirror() -> None:
    relation = {
        "from": "external:sse:listing-rules-2026",
        "to": "law_2022",
        "relation": "supersedes",
        "source": "official.sse",
        "confidence": 1.0,
        "evidence": {"rule_id": "relation.official_successor"},
        "rule_id": "relation.official_successor",
    }

    refs = _expected_canonical_relation_refs([relation], {"law_2022"})
    assert len(refs) == 1
    only_ref = next(iter(refs))
    assert only_ref[:3] == (
        "law_2022",
        "incoming",
        "external:sse:listing-rules-2026",
    )
    assert _catalog_graph_edge_keys([relation], {"law_2022"}) == set()
