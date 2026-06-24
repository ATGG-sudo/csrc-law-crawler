"""执法文书抓取：列表摘要 + 详情页正文。"""

from __future__ import annotations

from typing import Any

from api import fetch_writ_detail_html, summarize_writ
from client import HumanLikeClient
from storage import save_json, utc_now_iso, writ_file_path
from writ_parser import merge_writ_document, parse_law_writ_info_html


def writ_has_body(document: dict[str, Any]) -> bool:
    body = (document.get("body") or "").strip()
    return len(body) > 0


def fetch_and_save_writ(
    client: HumanLikeClient,
    writ_id: str,
    *,
    list_row: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """拉取详情页并保存；列表行可选（用于补全 dspt_date_ms 等）。"""
    path = writ_file_path(writ_id)
    if not force and path.exists():
        import json

        existing = json.loads(path.read_text(encoding="utf-8"))
        if writ_has_body(existing):
            return existing

    html_page = fetch_writ_detail_html(client, writ_id)
    detail = parse_law_writ_info_html(html_page)
    summary = summarize_writ(list_row) if list_row else None
    document = merge_writ_document(writ_id, list_summary=summary, detail=detail)
    document["source"] = {
        "crawled_at": utc_now_iso(),
        "detail_url": (
            f"https://neris.csrc.gov.cn/falvfagui/"
            f"rdqsHeader/lawWritInfo?navbarId=1&lawWritId={writ_id}"
        ),
        "list_api": "rdqsHeader/informationController?lawType=2",
        "detail_type": "html",
    }
    save_json(path, document)
    return document
