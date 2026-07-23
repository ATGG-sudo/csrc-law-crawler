from __future__ import annotations

import copy
import json
from pathlib import Path
import re

import pytest

from build_catalog import _merge_multi_source_rules
from csrc_law_crawler.sources.adapters import adapter_for
from csrc_law_crawler.sources.court_judicial_interpretation import (
    COURT_ENDPOINT_ID,
    ControlledDocumentError,
    CourtJudicialInterpretationAdapter,
)
from csrc_law_crawler.sources.court_judicial_interpretation_monitor import (
    COURT_MONITOR_ENDPOINT_ID,
    CourtJudicialInterpretationMonitorAdapter,
)
from csrc_law_crawler.sources.evidence import source_record_id
from csrc_law_crawler.sources.registry import load_registry, registry_query_sha256
from csrc_law_crawler.sources.runner import SourceRunner


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "court_judicial_interpretation"
EXPECTED_RECORD_IDS = {
    "450831": "8fca76bed39438b604e4fcdab1d7382b8e35cc3e4c2792554fe3644e8bf393f4",
    "436481": "6dc61ff48f6127f838b99508e6e5c4da6e2d95f5742884277bbc0459e6639f44",
    "282631:company-law-interpretation-3:2020": (
        "26f31b76989e5bbaec4cee7a07a6126cb179333a5a9e5322afc7f70e048047f8"
    ),
    "438551": "2f02410e3a879dd30b2ca6df81599328c6279a703a8b1e25cf06f145a648e952",
    "6135:company-law-interpretation-3:2014": (
        "24eade52047e20dd337ce3ce2290d93c7141486c3925a4bd32177268721466b4"
    ),
}


def _endpoint() -> tuple[dict, dict]:
    registry = load_registry()
    endpoint = next(
        item for item in registry["endpoints"] if item["endpoint_id"] == COURT_ENDPOINT_ID
    )
    return registry, endpoint


def _items() -> tuple[CourtJudicialInterpretationAdapter, dict, dict[str, dict]]:
    registry, endpoint = _endpoint()
    adapter = CourtJudicialInterpretationAdapter()
    discovery = adapter.discover(endpoint, registry, {})
    return adapter, endpoint, {
        str(item["document_spec"]["page_id"]): item for item in discovery["items"]
    }


def _fetched(page_id: str, body: bytes | None = None) -> dict:
    return {
        "body": body if body is not None else (FIXTURE_ROOT / f"{page_id}.html").read_bytes(),
        "status_code": 200,
        "content_type": "text/html; charset=utf-8",
        "final_url": f"https://www.court.gov.cn/zixun/xiangqing/{page_id}.html",
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "Etag": f'"fixture-{page_id}"',
        },
    }


def _parse(page_id: str) -> dict:
    adapter, endpoint, items = _items()
    return adapter.parse(endpoint, items[page_id], _fetched(page_id))


def test_curated_overlay_is_merged_and_fingerprinted_as_one_registry() -> None:
    registry, endpoint = _endpoint()
    assert len(registry["endpoints"]) == 87
    assert endpoint["url"] == "https://www.court.gov.cn/zixun/xiangqing/450831.html"
    assert endpoint["source_system"] == "court_judicial_interpretation"
    assert endpoint["default_material_lane"] == "rule"
    assert [item["upstream_id"] for item in endpoint["documents"]] == list(
        EXPECTED_RECORD_IDS
    )
    assert isinstance(
        adapter_for("court_judicial_interpretation"),
        CourtJudicialInterpretationAdapter,
    )
    monitor = next(
        item for item in registry["endpoints"] if item["endpoint_id"] == COURT_MONITOR_ENDPOINT_ID
    )
    assert monitor["source_system"] == "court_judicial_interpretation_monitor"
    assert monitor["default_material_lane"] == "clue"
    assert isinstance(
        adapter_for("court_judicial_interpretation_monitor"),
        CourtJudicialInterpretationMonitorAdapter,
    )
    changed = copy.deepcopy(registry)
    changed_endpoint = next(
        item
        for item in changed["endpoints"]
        if item["endpoint_id"] == COURT_ENDPOINT_ID
    )
    changed_endpoint["documents"][0]["required_markers"].append("changed assertion")
    assert registry_query_sha256(changed) != registry_query_sha256(registry)


def test_controlled_documents_have_stable_source_record_ids() -> None:
    for upstream_id, expected in EXPECTED_RECORD_IDS.items():
        assert (
            source_record_id("court_judicial_interpretation", upstream_id=upstream_id)
            == expected
        )


def test_2024_reply_requires_fileno_scope_text_and_publisher() -> None:
    parsed = _parse("450831")
    assert parsed["metadata"]["fileno"] == "法释〔2024〕15号"
    assert parsed["metadata"]["pub_org"] == "最高人民法院"
    assert parsed["metadata"]["publisher"] == "最高人民法院"
    assert "仅适用于2024年7月1日之后发生的未届出资期限的股权转让行为" in parsed[
        "plain_text"
    ]
    assert parsed["metadata"]["material_lane"] == "rule"
    assert parsed["http_metadata"]["headers"]["Etag"] == '"fixture-450831"'


def test_2024_temporal_rule_extracts_articles_only_from_news_wrapper() -> None:
    parsed = _parse("436481")
    text = parsed["plain_text"]
    assert parsed["metadata"]["name"] == (
        "最高人民法院关于适用《中华人民共和国公司法》时间效力的若干规定"
    )
    assert parsed["metadata"]["fileno"] == "法释〔2024〕7号"
    assert text.startswith("第一条")
    assert text.endswith("第八条 本规定自2024年7月1日起施行。")
    assert "新闻导语" not in text
    assert "法释〔2024〕7号" not in text
    assert len(re.findall(r"(?m)^第[一二三四五六七八九十]+条", text)) == 8


def test_compound_pages_split_2020_and_2014_versions() -> None:
    revised = _parse("282631")
    historical = _parse("6135")
    official_title = "最高人民法院关于适用《中华人民共和国公司法》若干问题的规定（三）"

    assert revised["metadata"]["name"] == official_title
    assert revised["metadata"]["edition_label"] == "2020年修正"
    assert revised["metadata"]["pub_date"] == "2020-12-29"
    assert revised["metadata"]["effective_date"] == "2021-01-01"
    assert "民法典第三百一十一条" in revised["plain_text"]
    assert "若干问题的规定（四）" not in revised["plain_text"]

    assert historical["metadata"]["name"] == official_title
    assert historical["metadata"]["edition_label"] == "2014年修正"
    assert historical["metadata"]["decision_date"] == "2014-02-17"
    assert historical["metadata"]["pub_date"] == "2014-02-20"
    assert historical["metadata"]["effective_date"] == "2014-03-01"
    assert historical["metadata"]["official_page_published_at"] == (
        "2014-02-27 16:26:00"
    )
    assert "物权法第一百零六条" in historical["plain_text"]
    assert "民法典第三百一十一条" not in historical["plain_text"]


def test_transition_guidance_is_explicit_reference_lane() -> None:
    parsed = _parse("438551")
    assert parsed["metadata"]["material_lane"] == "reference"
    assert parsed["metadata"]["document_type"] == "official_interview"
    assert "五部旧公司法司法解释尚未被废除" in parsed["plain_text"]
    assert "规定原理一致、不存在冲突时" in parsed["plain_text"]


@pytest.mark.parametrize("failure", ["missing", "duplicate"])
def test_compound_page_boundary_mismatch_fails_closed(failure: str) -> None:
    adapter, endpoint, items = _items()
    body = (FIXTURE_ROOT / "282631.html").read_bytes()
    end = "最高人民法院</p>\n  <p>关于适用《中华人民共和国公司法》</p>\n  <p>若干问题的规定（四）"
    if failure == "missing":
        body = body.replace(end.encode(), "边界已变更".encode())
    else:
        duplicate = (
            "最高人民法院关于适用《中华人民共和国公司法》"
            "若干问题的规定(三)"
        )
        body = body.replace(b"<div class=\"txt_txt\">", f'<div class="txt_txt"><p>{duplicate}</p>'.encode())

    with pytest.raises(ControlledDocumentError, match="marker must occur exactly once"):
        adapter.parse(endpoint, items["282631"], _fetched("282631", body))


def test_2014_court_record_merges_with_same_official_neris_entity() -> None:
    parsed = _parse("6135")
    entity_id = "law_existing_2014"
    entities = {
        entity_id: {
            "id": entity_id,
            "title": parsed["metadata"]["name"],
            "document_type": "judicial_interpretation",
            "metadata": {
                "name": parsed["metadata"]["name"],
                "fileno": "法释〔2011〕3号",
                "pub_date": "2014-02-20",
                "pub_org": "最高人民法院",
            },
            "sources": [
                {
                    "system": "neris",
                    "record_id": "existing-2014",
                    "role": "primary",
                }
            ],
            "assets": [],
            "preferred_content": {"plain_text": "旧正文"},
        }
    }
    source_to_entity: dict[tuple[str, str], str] = {}
    record = {
        "system": "court_judicial_interpretation",
        "record_id": EXPECTED_RECORD_IDS["6135:company-law-interpretation-3:2014"],
        "metadata": copy.deepcopy(parsed["metadata"]),
        "plain_text": parsed["plain_text"],
        "local_file": "raw/sources/records/court/6135.json",
        "page_url": "https://www.court.gov.cn/fabu/xiangqing/6135.html",
        "assets": [],
        "material_lane": "rule",
    }

    _merge_multi_source_rules([record], entities, source_to_entity)

    assert list(entities) == [entity_id]
    assert source_to_entity[(record["system"], record["record_id"])] == entity_id
    assert {source["system"] for source in entities[entity_id]["sources"]} == {
        "neris",
        "court_judicial_interpretation",
    }


def test_runner_preserves_raw_html_and_http_metadata_offline(tmp_path: Path) -> None:
    registry, endpoint = _endpoint()

    class FixtureAdapter(CourtJudicialInterpretationAdapter):
        def healthcheck(self, checked_endpoint: dict) -> dict:
            return {
                "access_status": "reachable",
                "status_code": 200,
                "final_url": checked_endpoint["url"],
                "content_type": "text/html",
                "_body": b"<html><body>official list fixture</body></html>",
            }

        def fetch(
            self,
            checked_endpoint: dict,
            item: dict,
            previous: dict | None = None,
        ) -> dict:
            del checked_endpoint, previous
            return _fetched(str(item["document_spec"]["page_id"]))

    runner = SourceRunner(
        registry=registry,
        adapter_factory=lambda _name: FixtureAdapter(),
        root=tmp_path,
    )
    report = runner.run(mode="baseline", endpoint_ids=[endpoint["endpoint_id"]], workers=1)

    assert report["status"] == "complete"
    record_root = tmp_path / "raw" / "sources" / "records" / "court_judicial_interpretation"
    records = [json.loads(path.read_text(encoding="utf-8")) for path in record_root.glob("*.json")]
    assert len(records) == 5
    reply = next(record for record in records if record["metadata"].get("fileno") == "法释〔2024〕15号")
    raw_path = Path(reply["source"]["raw_file"])
    assert raw_path.read_bytes() == (FIXTURE_ROOT / "450831.html").read_bytes()
    assert reply["source"]["http"]["status_code"] == 200
    assert reply["source"]["http"]["headers"]["Etag"] == '"fixture-450831"'
    assert reply["source"]["http_validators"]["etag"] == '"fixture-450831"'
    guidance = next(record for record in records if record["source_record_id"] == EXPECTED_RECORD_IDS["438551"])
    assert guidance["material_lane"] == "reference"
