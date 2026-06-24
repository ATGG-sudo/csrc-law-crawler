"""NERIS 接口封装。"""

from __future__ import annotations

from typing import Any

from client import HumanLikeClient
from config import LAW_TYPE_WRIT, PAGE_SIZE


def fetch_change_law(client: HumanLikeClient, law_id: str) -> dict[str, Any]:
    return client.post_json("rdqsHeader/changeLaw", {"secFutrsLawId": law_id})


def fetch_relative_files(client: HumanLikeClient, law_id: str) -> dict[str, Any]:
    return client.post_json(
        "rdqsHeader/relativeFiles",
        {"secFutrsLawId": law_id, "navbarId": "1"},
    )


def fetch_count_law_writ(client: HumanLikeClient, law_id: str) -> dict[str, Any]:
    return client.post_json(
        "rdqsHeader/countLawWrit",
        {"secFutrsLawId": law_id},
        require_success=False,
    )


def fetch_relative_examples(
    client: HumanLikeClient,
    *,
    law_id: str,
    relative_type: str,
    entry_id: str = "",
    page_no: int = 1,
) -> dict[str, Any]:
    return client.post_json(
        "rdqsHeader/relativeExample",
        {
            "secFutrsLawId": law_id,
            "navbarId": "1",
            "relativeType": relative_type,
            "secFutrsLawEntryId": entry_id,
            "pageNo": str(page_no),
        },
        require_success=False,
    )


def fetch_writ_list_page(client: HumanLikeClient, page_no: int) -> dict[str, Any]:
    return client.post_json(
        "rdqsHeader/informationController",
        {"pageNo": page_no, "lawType": LAW_TYPE_WRIT},
    )


def fetch_writ_detail_html(client: HumanLikeClient, writ_id: str) -> str:
    return client.get_html(
        f"rdqsHeader/lawWritInfo?navbarId=1&lawWritId={writ_id}",
        referer=f"https://neris.csrc.gov.cn/falvfagui/rdqsHeader/lawWritInfo?navbarId=1&lawWritId={writ_id}",
    )


def paginate_relative_examples(
    client: HumanLikeClient,
    *,
    law_id: str,
    relative_type: str,
    entry_id: str = "",
) -> list[dict[str, Any]]:
    first = fetch_relative_examples(
        client,
        law_id=law_id,
        relative_type=relative_type,
        entry_id=entry_id,
        page_no=1,
    )
    page_util = first.get("relativeExample") or {}
    items: list[dict[str, Any]] = list(page_util.get("pageList") or [])
    row_count = int(page_util.get("rowCount") or 0)
    total_pages = (row_count + PAGE_SIZE - 1) // PAGE_SIZE if row_count else 1

    for page in range(2, total_pages + 1):
        resp = fetch_relative_examples(
            client,
            law_id=law_id,
            relative_type=relative_type,
            entry_id=entry_id,
            page_no=page,
        )
        items.extend((resp.get("relativeExample") or {}).get("pageList") or [])
    return items


def summarize_writ(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("lawWritId"),
        "name": row.get("name"),
        "fileno": row.get("fileno"),
        "issue_org": row.get("issueOrgName"),
        "dspt_date_ms": row.get("dsptDate"),
        "body": row.get("body"),
        "link_addr": row.get("linkAddr"),
    }
