from __future__ import annotations

import json
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from csrc_law_crawler.processing.catalog.classification import (
    enforcement_classification_for,
    effectiveness_for,
    load_classification_overrides,
    material_classification_for,
    reference_lifecycle_for,
    source_web_classification,
)
from csrc_law_crawler.processing.catalog.matching import (
    infer_draft_finalization_relations,
    infer_explicit_successor_relations,
    infer_same_instrument_relations,
)
from export_markdown_catalog import (
    bucket_for_document,
    export_catalog_markdown,
    library_relative_dir_for_document,
)
from normalize_catalog import _infer_effective_date
from storage import save_json


def _entity(**changes):  # type: ignore[no-untyped-def]
    entity = {
        "id": "law_test",
        "title": "私募投资基金备案规则",
        "document_type": "self_regulatory_rule",
        "status": "现行有效",
        "metadata": {"name": "私募投资基金备案规则"},
        "preferred_content": {
            "source_system": "amac",
            "plain_text": "第一条 本规则反复适用。",
        },
        "sources": [
            {
                "system": "amac",
                "record_id": "test",
                "material_lane": "rule",
            }
        ],
    }
    entity.update(changes)
    return entity


class MaterialClassificationTests(unittest.TestCase):
    def test_repository_overrides_cover_verified_reclassification(self) -> None:
        overrides = load_classification_overrides()
        self.assertEqual("historical", overrides["law_02fc63b05e1a5f71f6394cc6"]["effectiveness"])
        self.assertEqual("current", overrides["law_9515c5745e60acf3cc4b7a03"]["effectiveness"])
        self.assertEqual(
            "interpretation_qa",
            overrides["law_30f8c2e319d5e88ffbc7d7e2"]["material_category"],
        )

    def test_disciplinary_decision_is_penalties(self) -> None:
        entity = _entity(
            title="纪律处分决定书（张三）",
            metadata={"name": "纪律处分决定书（张三）"},
        )
        material = material_classification_for(entity)
        enforcement = enforcement_classification_for(entity, material_classification=material)
        self.assertEqual("reference", material["lane"])
        self.assertEqual("enforcement_reference", material["category"])
        self.assertEqual("penalties", enforcement["category"])
        self.assertEqual("disciplinary_decision", enforcement["subtype"])

    def test_disciplinary_notice_and_review_subtypes(self) -> None:
        for title, expected in (
            ("纪律处分事先告知书（某公司）", "disciplinary_prior_notice"),
            ("纪律处分复核决定书（某人）", "disciplinary_review_decision"),
        ):
            entity = _entity(title=title, metadata={"name": title})
            material = material_classification_for(entity)
            enforcement = enforcement_classification_for(entity, material_classification=material)
            self.assertEqual(expected, enforcement["subtype"])

    def test_disciplinary_implementation_rule_stays_rule(self) -> None:
        title = "中国证券投资基金业协会纪律处分实施办法"
        entity = _entity(
            title=title,
            metadata={"name": title, "source_category": "disciplinary_institution"},
        )
        material = material_classification_for(entity)
        self.assertEqual("rule", material["lane"])
        self.assertIsNone(enforcement_classification_for(entity, material_classification=material))

    def test_missing_and_abnormal_categories_are_not_penalties(self) -> None:
        for category, expected in (
            ("missing_institution", "missing_institution"),
            ("abnormal_operation", "abnormal_operation"),
        ):
            entity = _entity(
                title="机构名单",
                metadata={"name": "机构名单", "source_category": category},
            )
            material = material_classification_for(entity)
            enforcement = enforcement_classification_for(entity, material_classification=material)
            self.assertEqual(expected, enforcement["category"])

    def test_disciplinary_section_does_not_make_cancellation_a_penalty(self) -> None:
        title = "关于注销某私募基金管理人登记的公告"
        entity = _entity(
            title=title,
            metadata={"name": title, "source_category": "disciplinary_institution"},
        )
        material = material_classification_for(entity)
        enforcement = enforcement_classification_for(entity, material_classification=material)
        self.assertEqual("other_enforcement", enforcement["category"])

    def test_generic_prior_notice_in_disciplinary_section_is_penalty_other(self) -> None:
        title = "事先告知书（某人）"
        entity = _entity(
            title=title,
            metadata={"name": title, "source_category": "disciplinary_person"},
        )
        material = material_classification_for(entity)
        enforcement = enforcement_classification_for(entity, material_classification=material)
        self.assertEqual("penalties", enforcement["category"])
        self.assertEqual("other", enforcement["subtype"])

    def test_amac_penalty_url_is_low_tier_web_evidence(self) -> None:
        web = source_web_classification(
            {"name": "纪律处分决定书"},
            page_url="https://www.amac.org.cn/zlgl/jlcf/scfjg/a.pdf",
            material_lane="reference",
        )
        self.assertEqual("disciplinary_institution", web["web_category_leaf"])
        self.assertEqual("url_inference", web["web_category_provenance"])
        self.assertEqual("case_document", web["page_role"])

    def test_agency_publishes_quoted_rule_is_publication_wrapper(self) -> None:
        web = source_web_classification(
            {"name": "中国证监会发布《私募投资基金信息披露监督管理办法》"},
            page_url="https://www.csrc.gov.cn/csrc/c100028/c7601234/content.shtml",
            material_lane="rule",
        )
        self.assertEqual("publication_wrapper", web["page_role"])

    def test_body_draft_mention_does_not_change_formal_rule(self) -> None:
        entity = _entity(
            preferred_content={
                "source_system": "amac",
                "plain_text": "本办法曾于征求意见稿阶段公开征求意见。",
            }
        )
        material = material_classification_for(entity)
        self.assertEqual("rule", material["lane"])
        self.assertEqual(
            "current",
            effectiveness_for(entity, material_classification=material)["status"],
        )

    def test_draft_title_is_reference(self) -> None:
        entity = _entity(title="私募投资基金备案规则（征求意见稿）")
        material = material_classification_for(entity)
        self.assertEqual("reference", material["lane"])
        self.assertEqual("publication_consultation", material["category"])

    def test_draft_lifecycle_changes_only_with_strict_formal_relation(self) -> None:
        entity = _entity(title="私募投资基金备案规则（征求意见稿）")
        material = material_classification_for(entity)
        self.assertEqual(
            "unfinalized",
            reference_lifecycle_for(entity, material_classification=material)["status"],
        )
        self.assertEqual(
            "finalized",
            reference_lifecycle_for(
                entity,
                material_classification=material,
                finalized_by=[{"canonical_id": "law_formal", "confidence": 0.98}],
            )["status"],
        )

    def test_exact_draft_and_formal_names_create_relation(self) -> None:
        entities = {
            "law_draft": _entity(
                title="私募投资基金备案规则（征求意见稿）",
                metadata={
                    "name": "私募投资基金备案规则（征求意见稿）",
                    "pub_date": "2025-01-01",
                    "pub_org": "协会",
                },
            ),
            "law_formal": _entity(
                title="私募投资基金备案规则",
                metadata={
                    "name": "私募投资基金备案规则",
                    "pub_date": "2025-06-01",
                    "pub_org": "协会",
                },
            ),
        }
        relation = infer_draft_finalization_relations(entities)[0]
        self.assertEqual("finalizes_draft", relation["relation"])
        self.assertEqual("law_draft", relation["to"])

    def test_same_title_and_fileno_create_copy_relation(self) -> None:
        entities = {
            "law_a": _entity(
                metadata={"name": "衍生品交易监督管理办法（试行）", "fileno": "证监会令234号"},
                title="衍生品交易监督管理办法（试行）",
            ),
            "law_b": _entity(
                metadata={
                    "name": "关于发布《衍生品交易监督管理办法（试行）》",
                    "fileno": "证监会令234号",
                },
                title="关于发布《衍生品交易监督管理办法（试行）》",
            ),
        }
        self.assertEqual(
            "same_instrument_copy",
            infer_same_instrument_relations(entities)[0]["relation"],
        )

    def test_explicit_repeal_requires_same_strong_clause(self) -> None:
        entities = {
            "law_partnership": _entity(title="中华人民共和国合伙企业法"),
            "law_qa": _entity(
                title="财政部有关负责人就有关通知答记者问",
                preferred_content={
                    "source_system": "neris",
                    "plain_text": (
                        "外商投资企业适用《中华人民共和国合伙企业法》等法律的规定。"
                        "对于外商投资企业根据废止前的其他法律提取的基金，应当明确处理。"
                    ),
                },
            ),
        }
        self.assertEqual([], infer_explicit_successor_relations(entities))

    def test_publication_wrapper_only_repeals_titles_in_action_clause(self) -> None:
        source = _entity(
            title="关于发布《私募投资基金服务业务管理办法（试行）》的通知",
            sources=[
                {
                    "system": "amac",
                    "record_id": "wrapper",
                    "page_role": "publication_wrapper",
                }
            ],
            preferred_content={
                "source_system": "amac",
                "plain_text": (
                    "根据《中华人民共和国证券投资基金法》有关规定，"
                    "现予以发布，原协会《基金业务外包服务指引（试行）》同时废止。"
                ),
            },
        )
        entities = {
            "law_source": source,
            "law_basis": _entity(title="中华人民共和国证券投资基金法"),
            "law_old": _entity(title="基金业务外包服务指引（试行）"),
        }
        relations = infer_explicit_successor_relations(entities)
        self.assertEqual(["law_old"], [relation["to"] for relation in relations])

    def test_rule_same_clause_repeal_is_retained(self) -> None:
        entities = {
            "law_new": _entity(
                title="新管理办法",
                preferred_content={
                    "source_system": "amac",
                    "plain_text": "本办法自发布之日起施行，原《旧管理办法》同时废止。",
                },
            ),
            "law_old": _entity(title="旧管理办法"),
        }
        relations = infer_explicit_successor_relations(entities)
        self.assertEqual("law_old", relations[0]["to"])
        self.assertEqual("原《旧管理办法》同时废止", relations[0]["evidence"]["clause"])

    def test_older_document_cannot_supersede_later_document(self) -> None:
        entities = {
            "law_older": _entity(
                title="旧发布日期文件",
                metadata={"name": "旧发布日期文件", "pub_date": "2020-01-01"},
                preferred_content={
                    "source_system": "amac",
                    "plain_text": "《较新文件》同时废止。",
                },
            ),
            "law_newer": _entity(
                title="较新文件",
                metadata={"name": "较新文件", "pub_date": "2021-01-01"},
            ),
        }
        self.assertEqual([], infer_explicit_successor_relations(entities))

    def test_repeal_survives_layout_newline_inside_quoted_title(self) -> None:
        entities = {
            "law_new": _entity(
                title="新纪律处分办法",
                preferred_content={
                    "source_system": "amac",
                    "plain_text": "《旧纪律处分\n办法》同时废止。",
                },
            ),
            "law_old": _entity(title="旧纪律处分办法"),
        }
        self.assertEqual(
            ["law_old"],
            [relation["to"] for relation in infer_explicit_successor_relations(entities)],
        )

    def test_defined_title_alias_can_be_repealed(self) -> None:
        old_title = "全国中小企业股份转让系统挂牌公司转板办法（试行）"
        entities = {
            "law_new": _entity(
                title="北交所上市公司转板办法（试行）",
                preferred_content={
                    "source_system": "szse",
                    "plain_text": (
                        f"《{old_title}》（以下简称原《转板办法》）进行了修订，"
                        "现予以发布。原《转板办法》同时废止。"
                    ),
                },
            ),
            "law_old": _entity(title=old_title),
        }
        relation = infer_explicit_successor_relations(entities)[0]
        self.assertEqual("law_old", relation["to"])
        self.assertEqual(old_title, relation["evidence"]["old_title"])
        self.assertEqual("转板办法", relation["evidence"]["old_title_mention"])

    def test_alias_definition_does_not_cross_another_quoted_title(self) -> None:
        entities = {
            "law_new": _entity(
                title="新办法",
                preferred_content={
                    "source_system": "gov",
                    "plain_text": (
                        "根据《证券投资基金法》制定《新办法》（以下简称《管理办法》）。"
                        "原《管理办法》同时废止。"
                    ),
                },
            ),
            "law_basis": _entity(title="证券投资基金法"),
        }
        self.assertEqual([], infer_explicit_successor_relations(entities))

    def test_prior_published_list_is_kept_as_one_repeal_clause(self) -> None:
        entities = {
            "law_new": _entity(
                title="新规范运作指引",
                preferred_content={
                    "source_system": "sse",
                    "plain_text": (
                        "本所此前发布的《旧内部控制指引》（文号一）、《旧社会责任指引》同时废止。"
                    ),
                },
            ),
            "law_old_a": _entity(title="旧内部控制指引"),
            "law_old_b": _entity(title="旧社会责任指引"),
        }
        self.assertEqual(
            {"law_old_a", "law_old_b"},
            {relation["to"] for relation in infer_explicit_successor_relations(entities)},
        )

    def test_later_exact_title_revision_supersedes_prior_instrument(self) -> None:
        base_title = "上海证券交易所上市公司自律监管指引第10号——纪律处分实施标准"
        entities = {
            "law_new": _entity(
                title=f"{base_title}（2024年1月修订）",
                metadata={
                    "name": f"{base_title}（2024年1月修订）",
                    "pub_org": "上海证券交易所",
                    "pub_date": "2024-01-18",
                },
                preferred_content={
                    "source_system": "sse",
                    "plain_text": f"本所对《{base_title}》进行了修订（详见附件）。",
                },
            ),
            "law_old": _entity(
                title=base_title,
                metadata={
                    "name": base_title,
                    "pub_org": "上海证券交易所",
                    "pub_date": "2022-01-06",
                },
            ),
        }
        relation = infer_explicit_successor_relations(entities)[0]
        self.assertEqual("law_old", relation["to"])
        self.assertEqual("exact_title_revision", relation["evidence"]["inference"])

    def test_exact_title_revision_requires_same_organization(self) -> None:
        base_title = "上市公司自律监管指引——纪律处分实施标准"
        entities = {
            "law_new": _entity(
                title=f"{base_title}（2024年修订）",
                metadata={
                    "name": f"{base_title}（2024年修订）",
                    "pub_org": "交易所甲",
                    "pub_date": "2024-01-01",
                },
                preferred_content={
                    "source_system": "exchange_a",
                    "plain_text": f"对《{base_title}》进行了修订。",
                },
            ),
            "law_old": _entity(
                title=base_title,
                metadata={
                    "name": base_title,
                    "pub_org": "交易所乙",
                    "pub_date": "2022-01-01",
                },
            ),
        }
        self.assertEqual([], infer_explicit_successor_relations(entities))

    def test_revision_chain_selects_immediately_prior_version(self) -> None:
        base_title = "上市公司自律监管指引——纪律处分实施标准"

        def revision(title: str, pub_date: str) -> dict:  # type: ignore[type-arg]
            return _entity(
                title=title,
                metadata={
                    "name": title,
                    "pub_org": "交易所甲",
                    "pub_date": pub_date,
                },
                preferred_content={
                    "source_system": "exchange_a",
                    "plain_text": f"对《{base_title}》进行了修订。",
                },
            )

        entities = {
            "law_original": revision(base_title, "2020-01-01"),
            "law_2022": revision(f"{base_title}（2022年修订）", "2022-01-01"),
            "law_2024": revision(f"{base_title}（2024年修订）", "2024-01-01"),
        }
        relations = infer_explicit_successor_relations(entities)
        self.assertEqual(
            {("law_2022", "law_original"), ("law_2024", "law_2022")},
            {(relation["from"], relation["to"]) for relation in relations},
        )

    def test_revision_does_not_target_publication_wrapper(self) -> None:
        base_title = "上市公司自律监管指引——纪律处分实施标准"
        entities = {
            "law_new": _entity(
                title=f"{base_title}（2024年修订）",
                metadata={
                    "name": f"{base_title}（2024年修订）",
                    "pub_org": "交易所甲",
                    "pub_date": "2024-01-01",
                },
                preferred_content={
                    "source_system": "exchange_a",
                    "plain_text": f"对《{base_title}》进行了修订。",
                },
            ),
            "law_wrapper": _entity(
                title=f"关于发布《{base_title}》的通知",
                metadata={
                    "name": f"关于发布《{base_title}》的通知",
                    "pub_org": "交易所甲",
                    "pub_date": "2022-01-01",
                },
            ),
        }
        self.assertEqual([], infer_explicit_successor_relations(entities))

    def test_statutory_repeal_phrase_is_explicit(self) -> None:
        entities = {
            "law_new": _entity(
                title="新条例",
                metadata={"name": "新条例", "effective_date": "2026-09-01"},
                preferred_content={
                    "source_system": "gov",
                    "plain_text": "本条例施行时，《旧信访条例》按法定程序废止。",
                },
            ),
            "law_old": _entity(title="旧信访条例"),
        }
        relations = infer_explicit_successor_relations(entities)
        self.assertEqual(["law_old"], [relation["to"] for relation in relations])
        self.assertEqual("2026-09-01", relations[0]["evidence"]["effective_date"])

    def test_explicit_repeal_prefers_exact_instrument_over_publication_wrapper(self) -> None:
        entities = {
            "law_new": _entity(
                title="新办法",
                preferred_content={
                    "source_system": "amac",
                    "plain_text": "《旧管理办法》同时废止。",
                },
            ),
            "law_old": _entity(title="旧管理办法"),
            "law_wrapper": _entity(title="关于发布《旧管理办法》的通知"),
        }

        relations = infer_explicit_successor_relations(entities)

        self.assertEqual(["law_old"], [relation["to"] for relation in relations])

    def test_explicit_supporting_format_guides_are_repealed_on_commencement(self) -> None:
        entities = {
            "law_new": _entity(
                title="私募投资基金信息披露实施细则",
                metadata={
                    "name": "私募投资基金信息披露实施细则",
                    "pub_org": "中国证券投资基金业协会",
                    "pub_date": "2026-06-05",
                    "effective_date": "2026-09-01",
                },
                preferred_content={
                    "source_system": "amac",
                    "plain_text": (
                        "本细则自2026年9月1日起施行。2016年2月4日发布的"
                        "《私募投资基金信息披露管理办法》及配套内容与格式指引同时废止。"
                    ),
                },
            ),
            "law_old": _entity(
                title="私募投资基金信息披露管理办法",
                metadata={
                    "name": "私募投资基金信息披露管理办法",
                    "pub_org": "中国证券投资基金业协会",
                    "pub_date": "2016-02-04",
                },
            ),
            "law_guide_1": _entity(
                title="私募投资基金信息披露内容与格式指引1号",
                metadata={
                    "name": "私募投资基金信息披露内容与格式指引1号",
                    "pub_org": "中国证券投资基金业协会",
                    "pub_date": "2016-02-04",
                },
            ),
            "law_guide_2": _entity(
                title="私募投资基金信息披露内容与格式指引2号",
                metadata={
                    "name": "私募投资基金信息披露内容与格式指引2号",
                    "pub_org": "中国证券投资基金业协会",
                    "pub_date": "2016-02-04",
                },
            ),
        }

        relations = infer_explicit_successor_relations(entities)

        self.assertEqual(
            {"law_old", "law_guide_1", "law_guide_2"},
            {relation["to"] for relation in relations},
        )
        self.assertTrue(
            all(relation["evidence"]["effective_date"] == "2026-09-01" for relation in relations)
        )

    def test_explicit_body_effective_date_is_parsed(self) -> None:
        self.assertEqual(
            "2026-11-16",
            _infer_effective_date({}, "本办法自2026年11月16日起施行。"),
        )

    def test_explicit_effective_date_repairs_wrong_metadata_and_whitespace(self) -> None:
        self.assertEqual(
            "2026-09-01",
            _infer_effective_date(
                {"effective_date": "2026-08-31"},
                "本办法自 2026 年 9 月 1 日起施行。",
            ),
        )

    def test_explicit_effective_date_repairs_line_broken_year_digits(self) -> None:
        self.assertEqual(
            "2026-09-01",
            _infer_effective_date({}, "本细则自\n202\n6\n年\n9\n月\n1\n日起施行。"),
        )

    def test_distant_body_date_does_not_override_valid_source_effective_date(self) -> None:
        self.assertEqual(
            "2025-10-24",
            _infer_effective_date(
                {"effective_date": "2025-10-24"},
                "其他引用文件自2026年6月16日起施行。",
            ),
        )

    def test_publication_commencement_uses_publication_date(self) -> None:
        self.assertEqual(
            "2025-10-24",
            _infer_effective_date(
                {"pub_date": "2025-10-24"},
                "本指引自 发布 之 日起 实施。",
            ),
        )

    def test_trial_requires_commencement_evidence(self) -> None:
        title = "私募投资基金备案办法（试行）"
        entity = _entity(title=title, metadata={"name": title})
        material = material_classification_for(entity)
        self.assertEqual(
            "unknown",
            effectiveness_for(entity, material_classification=material, as_of="2026-07-14")[
                "status"
            ],
        )
        entity["metadata"]["effective_date"] = "2026-01-01"
        self.assertEqual(
            "current",
            effectiveness_for(entity, material_classification=material, as_of="2026-07-14")[
                "status"
            ],
        )

    def test_industry_research_stays_reference_even_with_rule_in_title(self) -> None:
        entity = _entity(
            title="行业规则运行研究报告",
            metadata={
                "name": "行业规则运行研究报告",
                "source_category": "industry_research_report",
            },
        )
        material = material_classification_for(entity)
        self.assertEqual("reference", material["lane"])
        self.assertEqual("research_statistics", material["category"])

    def test_substantive_notice_can_remain_rule(self) -> None:
        entity = _entity(
            title="关于加强私募基金信息披露管理的通知",
            document_type="publication_notice",
        )
        material = material_classification_for(entity)
        self.assertEqual("rule", material["lane"])

    def test_publishing_wrapper_with_separate_attachment_is_reference(self) -> None:
        entity = _entity(title="关于发布私募基金备案规则的公告")
        material = material_classification_for(entity, publishes=["law_attachment"])
        self.assertEqual("reference", material["lane"])
        self.assertEqual("publishing_wrapper", material["basis"])

    def test_custom_source_types_use_source_lane(self) -> None:
        rule = _entity(document_type="正式规则/公告")
        reference = _entity(
            document_type="监管动态",
            status="unknown",
            sources=[
                {
                    "system": "csrc_gov_cn",
                    "record_id": "reference",
                    "material_lane": "reference",
                }
            ],
            material_lane="reference",
        )
        self.assertEqual("rule", material_classification_for(rule)["lane"])
        self.assertEqual("reference", material_classification_for(reference)["lane"])

    def test_future_effective_date_is_pending(self) -> None:
        entity = _entity(
            metadata={
                "name": "私募投资基金备案规则",
                "effective_date": "2026-08-01",
            }
        )
        material = material_classification_for(entity)
        effectiveness = effectiveness_for(
            entity,
            material_classification=material,
            as_of="2026-07-14",
        )
        self.assertEqual("pending", effectiveness["status"])
        self.assertEqual("pending", bucket_for_document({"effectiveness": effectiveness}))

    def test_past_ineffective_date_is_historical(self) -> None:
        entity = _entity(
            metadata={
                "name": "私募投资基金备案规则",
                "ineffective_date": "2026-07-01",
            }
        )
        material = material_classification_for(entity)
        effectiveness = effectiveness_for(
            entity,
            material_classification=material,
            as_of="2026-07-14",
        )
        self.assertEqual("historical", effectiveness["status"])

    def test_superseding_relation_is_historical(self) -> None:
        entity = _entity()
        material = material_classification_for(entity)
        effectiveness = effectiveness_for(
            entity,
            material_classification=material,
            superseded_by=[{"canonical_id": "law_new", "confidence": 1.0}],
            as_of="2026-07-14",
        )
        self.assertEqual("historical", effectiveness["status"])

    def test_future_superseding_relation_activates_on_effective_date(self) -> None:
        entity = _entity()
        material = material_classification_for(entity)
        superseding = {
            "canonical_id": "law_new",
            "confidence": 1.0,
            "effective_date": "2026-09-01",
        }
        before = effectiveness_for(
            entity,
            material_classification=material,
            superseded_by=[superseding],
            as_of="2026-08-31",
        )
        on_date = effectiveness_for(
            entity,
            material_classification=material,
            superseded_by=[superseding],
            as_of="2026-09-01",
        )
        self.assertEqual("current", before["status"])
        self.assertEqual("historical", on_date["status"])

    def test_manual_override_controls_both_axes(self) -> None:
        entity = _entity(status="unknown")
        override = {
            "material_lane": "rule",
            "material_category": "normative_document",
            "effectiveness": "current",
            "verified_at": "2026-07-14",
            "evidence_url": "https://www.csrc.gov.cn/example",
            "note": "官网状态核验",
        }
        material = material_classification_for(entity, override=override)
        effectiveness = effectiveness_for(
            entity,
            material_classification=material,
            override=override,
            as_of="2026-07-14",
        )
        self.assertEqual("manual_override", material["basis"])
        self.assertEqual("current", effectiveness["status"])

    def test_unknown_rule_routes_to_effectiveness_review_library(self) -> None:
        doc = {
            "material_classification": {"lane": "rule", "category": "business_rule"},
            "effectiveness": {"status": "unknown"},
        }
        self.assertEqual(
            "05_待核验/01_制度效力待核验",
            str(library_relative_dir_for_document(doc)),
        )

    def test_export_keeps_compatibility_and_library_views_in_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            docs = [
                ("law_current", "rule", "current", "normative_document"),
                ("law_pending", "rule", "pending", "normative_document"),
                ("law_historical", "rule", "historical", "normative_document"),
                ("law_reference", "reference", "not_applicable", "interpretation_qa"),
                ("law_unknown", "rule", "unknown", "self_regulatory_rule"),
            ]
            manifest_items = []
            for entity_id, lane, status, category in docs:
                path = root / "canonical" / "json" / f"{entity_id}.json"
                save_json(
                    path,
                    {
                        "id": entity_id,
                        "title": entity_id,
                        "document_type": "regulation",
                        "status": "unknown",
                        "material_classification": {
                            "lane": lane,
                            "category": category,
                            "basis": "test",
                            "rule_id": "test",
                            "confidence": 1.0,
                            "evidence": {},
                        },
                        "effectiveness": {
                            "status": status,
                            "basis": "test",
                            "label": status,
                        },
                        "metadata": {"name": entity_id},
                        "preferred_source": {"system": "test", "record_id": entity_id},
                        "sources": [],
                        "assets": [],
                        "content_status": "full_text",
                        "full_text_plain": "第一条 内容。",
                        "full_text_markdown": "第一条 内容。",
                    },
                )
                manifest_items.append({"id": entity_id, "file": f"canonical/json/{entity_id}.json"})
            save_json(
                root / "work" / "catalog" / "normalized_manifest.json",
                {"count": len(docs), "items": manifest_items},
            )
            with patch("storage.OUTPUT_DIR", root):
                manifest = export_catalog_markdown(force=True, clean=True)
            library = root / "canonical" / "library"
            library_manifest = json.loads((library / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(len(docs), manifest["count"])
        self.assertEqual(len(docs), library_manifest["count"])
        self.assertEqual(
            {item["id"] for item in manifest["items"]},
            {item["id"] for item in library_manifest["items"]},
        )
        pending = next(item for item in manifest["items"] if item["id"] == "law_pending")
        self.assertEqual("pending", pending["bucket"])
        self.assertIn("02_待生效制度", pending["library_file"])

    def test_atomic_publish_includes_library(self) -> None:
        import csrc_law_crawler.sources.publish as publish
        import storage

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            def fake_export(**_kwargs):  # type: ignore[no-untyped-def]
                target = storage.OUTPUT_DIR / "canonical" / "library" / "01_现行制度"
                target.mkdir(parents=True)
                (target / "sentinel.md").write_text("ok", encoding="utf-8")
                return {"count": 1, "bucket_counts": {"current": 1}}

            with (
                patch.object(publish, "_copy_inputs", return_value=None),
                patch.object(publish, "build_catalog", return_value={"count": 1}),
                patch.object(publish, "normalize_catalog", return_value={"count": 1}),
                patch.object(publish, "export_catalog_markdown", side_effect=fake_export),
                patch.object(publish, "build_canonical_relations", return_value={"counts": {}}),
                patch.object(publish, "validate_catalog_exports", return_value=([], {})),
            ):
                publish.stage_and_publish_canonical(run_id="test", root=root)

            self.assertTrue(
                (root / "canonical" / "library" / "01_现行制度" / "sentinel.md").exists()
            )


if __name__ == "__main__":
    unittest.main()
