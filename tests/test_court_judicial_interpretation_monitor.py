from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import requests

from csrc_law_crawler.sources.court_judicial_interpretation_monitor import (
    COURT_DIRECTORY_URL,
    COURT_MONITOR_ENDPOINT_ID,
    COURT_MONITOR_SOURCE_SYSTEM,
    CourtJudicialInterpretationMonitorAdapter,
    classify_candidate,
)
from csrc_law_crawler.sources.court_monitor_artifacts import build_monitor_artifacts
from csrc_law_crawler.sources.runner import SourceRunner


class FakeResponse:
    def __init__(
        self,
        body: bytes = b"",
        *,
        status: int = 200,
        url: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.content = body
        self.status_code = status
        self.url = url
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"HTTP {self.status_code}",
                response=self,  # type: ignore[arg-type]
            )


def _page_url(page: int) -> str:
    return COURT_DIRECTORY_URL if page == 1 else COURT_DIRECTORY_URL[:-5] + f"_{page}.html"


def _listing_html(
    *,
    total: int,
    page: int,
    ids: list[str],
    last_page: int,
    drift: bool = False,
) -> bytes:
    items = []
    for position, page_id in enumerate(ids, start=1):
        year = 2026 if (page + position) % 2 else 2020
        date = f"{year}-{(position % 12) + 1:02d}-{(position % 27) + 1:02d}"
        items.append(
            "<li><a href=\"/fabu/xiangqing/{0}.html\">页面 {0}</a>"
            "<i class=\"date\">{1}</i></li>".format(page_id, date)
        )
    pagination = "".join(
        f'<a href="{_page_url(number)}">{number}</a>'
        for number in range(1, last_page + 1)
    )
    list_class = "changed_list" if drift else "sec_list"
    return (
        f"<html><body><div>共 {total} 篇文章</div>"
        f'<div class="{list_class}"><ul>{"".join(items)}</ul></div>'
        f'<div class="pages">{pagination}</div></body></html>'
    ).encode()


def _listing_responses(
    total: int = 395,
    *,
    duplicate: bool = False,
    drift_page: int | None = None,
) -> dict[str, list[FakeResponse]]:
    last_page = (total + 19) // 20
    ids = [str(900000 - index) for index in range(total)]
    if duplicate:
        ids[-1] = ids[0]
    responses: dict[str, list[FakeResponse]] = {}
    for page in range(1, last_page + 1):
        members = ids[(page - 1) * 20 : page * 20]
        url = _page_url(page)
        responses[url] = [
            FakeResponse(
                _listing_html(
                    total=total,
                    page=page,
                    ids=members,
                    last_page=last_page,
                    drift=page == drift_page,
                ),
                url=url,
                headers={
                    "Content-Type": "text/html; charset=utf-8",
                    "ETag": f'"page-{page}"',
                    "Last-Modified": "Wed, 22 Jul 2026 12:00:00 GMT",
                },
            )
        ]
    return responses


class ListingAdapter(CourtJudicialInterpretationMonitorAdapter):
    def __init__(self, responses: dict[str, list[FakeResponse]]) -> None:
        super().__init__()
        self.responses = responses
        self.request_headers: list[dict[str, str]] = []

    def _get(self, url: str, **kwargs: Any) -> FakeResponse:  # type: ignore[override]
        self.request_headers.append(dict(kwargs.get("headers") or {}))
        return self.responses[url].pop(0)


def _endpoint(page_size: int = 20) -> dict[str, Any]:
    return {
        "endpoint_id": COURT_MONITOR_ENDPOINT_ID,
        "url": COURT_DIRECTORY_URL,
        "healthcheck_url": "https://www.court.gov.cn/fabu/xiangqing/436481.html",
        "source_system": COURT_MONITOR_SOURCE_SYSTEM,
        "adapter": "court_judicial_interpretation_monitor",
        "scope_mode": "enumerable",
        "query_sets": [],
        "default_material_lane": "clue",
        "page_size": page_size,
        "profiles": [
            {
                "profile_id": "profile_court_monitor_test",
                "name": "最高法司法解释监测",
                "publisher": "最高人民法院",
                "material_nature": "监测线索",
                "region": "全国",
            }
        ],
    }


def test_full_20_page_enumeration_accepts_id_and_date_inversions() -> None:
    adapter = ListingAdapter(_listing_responses())
    checkpoint: dict[str, Any] = {}
    result = adapter.discover(_endpoint(), {"query_sets": {}}, checkpoint)

    assert result["discovery_status"] == "complete"
    assert result["reported_total"] == 395
    assert result["pages_completed"] == 20
    assert len(result["items"]) == 395
    assert len(checkpoint["listing_pages"]["20"]["members"]) == 15
    assert len({item["upstream_id"] for item in result["items"]}) == 395
    assert {item["listing_date"][:4] for item in result["items"]} == {"2020", "2026"}


def test_backfilled_higher_id_on_an_old_page_does_not_use_max_id_cursor() -> None:
    responses = _listing_responses()
    page = 10
    url = _page_url(page)
    ids = [str(700000 - index) for index in range(20)]
    ids[3] = "999999"
    responses[url] = [
        FakeResponse(
            _listing_html(
                total=395,
                page=page,
                ids=ids,
                last_page=20,
            ),
            url=url,
        )
    ]
    result = ListingAdapter(responses).discover(_endpoint(), {"query_sets": {}}, {})
    assert result["discovery_status"] == "complete"
    assert any(
        item["upstream_id"] == "999999" and item["listing_page"] == 10
        for item in result["items"]
    )


@pytest.mark.parametrize(
    ("responses", "message"),
    [
        (_listing_responses(total=394), "complete"),
        (_listing_responses(duplicate=True), "incomplete"),
        (_listing_responses(drift_page=7), "incomplete"),
    ],
)
def test_listing_completeness_checks(
    responses: dict[str, list[FakeResponse]],
    message: str,
) -> None:
    result = ListingAdapter(responses).discover(_endpoint(), {"query_sets": {}}, {})
    assert result["discovery_status"] == message


def test_reported_total_mismatch_is_incomplete() -> None:
    responses = _listing_responses()
    last_url = _page_url(20)
    responses[last_url] = [
        FakeResponse(
            _listing_html(
                total=395,
                page=20,
                ids=[str(800000 - index) for index in range(14)],
                last_page=20,
            ),
            url=last_url,
        )
    ]
    result = ListingAdapter(responses).discover(_endpoint(), {"query_sets": {}}, {})
    assert result["discovery_status"] == "incomplete"
    assert any(
        "member count" in failure["error_message"] for failure in result["failures"]
    )


def test_conditional_listing_304_reuses_cache_and_304_without_cache_fails() -> None:
    checkpoint: dict[str, Any] = {}
    first = ListingAdapter(_listing_responses())
    assert first.discover(_endpoint(), {"query_sets": {}}, checkpoint)["discovery_status"] == (
        "complete"
    )

    not_modified = {
        _page_url(page): [
            FakeResponse(
                status=304,
                url=_page_url(page),
                headers={"ETag": f'"page-{page}"'},
            )
        ]
        for page in range(1, 21)
    }
    second = ListingAdapter(copy.deepcopy(not_modified))
    result = second.discover(_endpoint(), {"query_sets": {}}, checkpoint)
    assert result["discovery_status"] == "complete"
    assert result["raw_pages"] == []
    assert second.stats["list_not_modified"] == 20
    assert second.request_headers[0]["If-None-Match"] == '"page-1"'

    no_cache = ListingAdapter(
        {
            COURT_DIRECTORY_URL: [
                FakeResponse(status=304, url=COURT_DIRECTORY_URL)
            ]
        }
    )
    failed = no_cache.discover(_endpoint(), {"query_sets": {}}, {})
    assert failed["discovery_status"] == "incomplete"
    assert "without cached membership" in failed["failures"][0]["error_message"]


@pytest.mark.parametrize("status", [403, 429])
def test_blocked_or_rate_limited_listing_is_incomplete(status: int) -> None:
    adapter = ListingAdapter(
        {
            COURT_DIRECTORY_URL: [
                FakeResponse(status=status, url=COURT_DIRECTORY_URL)
            ]
        }
    )
    result = adapter.discover(_endpoint(), {"query_sets": {}}, {})
    assert result["discovery_status"] == "incomplete"
    assert result["pages_completed"] == 0


@pytest.mark.parametrize(
    ("title", "text", "expected"),
    [
        (
            "最高人民法院关于审理测试案件的规定",
            "最高人民法院关于审理测试案件的规定\n法释〔2026〕1号\n第一条 测试。",
            "single_instrument",
        ),
        (
            "最高人民法院发布审理测试案件规定",
            "新闻导语：今天发布相关文件。\n法释〔2026〕1号\n第一条 测试。",
            "release_with_instrument",
        ),
        (
            "最高人民法院关于修改若干司法解释的决定",
            "法释〔2026〕1号\n第一条 修改甲。\n法释〔2026〕2号\n第一条 修改乙。",
            "compound_instruments",
        ),
        (
            "最高人民法院负责人就司法解释答记者问",
            "最高人民法院负责人就有关问题答记者问，介绍制定背景。",
            "reference_or_news",
        ),
        ("栏目说明", "这是无法识别结构的普通说明文本。", "unknown_structure"),
    ],
)
def test_five_candidate_types(title: str, text: str, expected: str) -> None:
    candidate, signals = classify_candidate(title, text)
    assert candidate == expected
    assert "article_count" in signals


class ModeAdapter:
    def __init__(self) -> None:
        self.items = [
            {
                "url": f"https://www.court.gov.cn/fabu/xiangqing/{page_id}.html",
                "title": f"司法解释 {page_id}",
                "upstream_id": page_id,
                "listing_date": "2026-07-23",
                "in_scope": True,
                "discovery_evidence": [{"page": 1}],
            }
            for page_id in ("101", "102", "103")
        ]
        self.detail_requests = 0
        self.stats: dict[str, int] = {
            "request_attempts": 0,
            "list_requests": 1,
            "detail_requests": 0,
            "detail_not_modified": 0,
            "detail_reused": 0,
        }

    def healthcheck(self, endpoint: dict[str, Any]) -> dict[str, Any]:
        return {
            "access_status": "reachable",
            "status_code": 200,
            "final_url": endpoint["healthcheck_url"],
            "content_type": "text/html",
            "_body": b"health",
        }

    def discover(
        self,
        endpoint: dict[str, Any],
        registry: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> dict[str, Any]:
        del endpoint, registry, checkpoint
        return {
            "items": copy.deepcopy(self.items),
            "raw_pages": [],
            "discovery_status": "complete",
            "pages_completed": 1,
            "reported_total": len(self.items),
            "failures": [],
        }

    def fetch(
        self,
        endpoint: dict[str, Any],
        item: dict[str, Any],
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if previous and not endpoint.get("_refresh_details") and not item.get(
            "listing_metadata_changed"
        ):
            self.stats["detail_reused"] += 1
            return {
                "not_modified": True,
                "reused_without_request": True,
                "final_url": item["url"],
                "headers": {},
            }
        self.detail_requests += 1
        self.stats["detail_requests"] += 1
        if previous and endpoint.get("_refresh_details"):
            self.stats["detail_not_modified"] += 1
            return {
                "not_modified": True,
                "detail_requested": True,
                "status_code": 304,
                "final_url": item["url"],
                "headers": {},
            }
        return {
            "body": f"<html>{item['title']}</html>".encode(),
            "status_code": 200,
            "content_type": "text/html",
            "final_url": item["url"],
            "headers": {"ETag": f'"{item["upstream_id"]}"'},
            "detail_requested": True,
        }

    def parse(
        self,
        endpoint: dict[str, Any],
        item: dict[str, Any],
        fetched: dict[str, Any],
    ) -> dict[str, Any]:
        del endpoint, fetched
        page_id = str(item["upstream_id"])
        return {
            "metadata": {
                "name": f"司法解释 {page_id}",
                "listing_title": item["title"],
                "listing_date": item["listing_date"],
                "official_page_id": item["upstream_id"],
                "candidate_type": "single_instrument",
                "filenos": [],
                "article_count": 1,
                "material_lane": "clue",
            },
            "plain_text": f"第一条 司法解释 {page_id} 正文。",
            "content_html": "<p>正文</p>",
            "assets": [],
        }


def _monitor_registry() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "query_set_version": "monitor-test-v1",
        "query_sets": {},
        "endpoints": [_endpoint()],
    }


def test_baseline_daily_weekly_modes_and_review_artifacts(tmp_path: Path) -> None:
    adapter = ModeAdapter()
    runner = SourceRunner(
        registry=_monitor_registry(),
        adapter_factory=lambda _name: adapter,
        root=tmp_path,
    )
    baseline = runner.run(mode="baseline", workers=1)
    assert baseline["status"] == "complete"
    assert adapter.detail_requests == 3
    assert not (tmp_path / "work" / "changes" / f"{baseline['run_id']}.jsonl").exists()
    artifacts = build_monitor_artifacts(run_id=baseline["run_id"], root=tmp_path)
    assert artifacts["inventory"]["item_count"] == 3
    assert artifacts["review_queue"]["actionable_count"] == 0

    adapter.stats["detail_requests"] = 0
    adapter.stats["detail_reused"] = 0
    daily = runner.run(mode="incremental", workers=1)
    assert daily["counts"]["detail_requests"] == 0
    assert daily["counts"]["detail_reused"] == 3

    adapter.stats["detail_requests"] = 0
    adapter.stats["detail_not_modified"] = 0
    weekly = runner.run(mode="incremental", workers=1, refresh_details=True)
    assert weekly["refresh_details"] is True
    assert weekly["counts"]["detail_requests"] == 3
    assert weekly["counts"]["detail_not_modified"] == 3

    adapter.items[0]["title"] = "司法解释 101（栏目标题调整）"
    adapter.items[0]["listing_metadata_changed"] = True
    adapter.stats["detail_requests"] = 0
    adapter.stats["detail_not_modified"] = 0
    adapter.stats["detail_reused"] = 0
    changed = runner.run(mode="incremental", workers=1)
    changed_artifacts = build_monitor_artifacts(run_id=changed["run_id"], root=tmp_path)
    assert changed["counts"]["detail_requests"] == 1
    assert changed_artifacts["review_queue"]["actionable_count"] == 1
    assert changed_artifacts["review_queue"]["items"][0]["change_type"] == "metadata_changed"


def test_resume_refuses_refresh_detail_mode_change(tmp_path: Path) -> None:
    adapter = ModeAdapter()
    runner = SourceRunner(
        registry=_monitor_registry(),
        adapter_factory=lambda _name: adapter,
        root=tmp_path,
    )
    report = runner.run(mode="baseline")
    with pytest.raises(RuntimeError, match="refresh_details"):
        runner.run(
            mode="baseline",
            resume_run_id=report["run_id"],
            refresh_details=True,
        )


def test_monitor_records_are_clues_and_not_rule_catalog_input(tmp_path: Path) -> None:
    adapter = ModeAdapter()
    report = SourceRunner(
        registry=_monitor_registry(),
        adapter_factory=lambda _name: adapter,
        root=tmp_path,
    ).run(mode="baseline")
    assert report["status"] == "complete"
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (
            tmp_path
            / "raw"
            / "sources"
            / "records"
            / COURT_MONITOR_SOURCE_SYSTEM
        ).glob("*.json")
    ]
    assert len(records) == 3
    assert {record["material_lane"] for record in records} == {"clue"}
