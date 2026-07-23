"""Reader-facing inventory and review queue for the SPC directory monitor."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any

from storage import load_json, output_dir, save_json, utc_now_iso

from .court_judicial_interpretation_monitor import (
    COURT_MONITOR_ENDPOINT_ID,
    COURT_MONITOR_SOURCE_SYSTEM,
)
from .evidence import source_record_id


DECISIONS_PATH = Path(__file__).with_name(
    "court_judicial_interpretation_review_decisions.json"
)
_CLOSED_STATUSES = {"controlled", "reference_only", "ignored", "closed"}
_ACTIONABLE_CHANGES = {
    "new",
    "content_changed",
    "metadata_changed",
    "removed",
    "restored",
}


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            items.append(value)
    return items


def _latest_complete_run(root: Path) -> dict[str, Any] | None:
    directory = root / "reports" / "source_baselines"
    candidates: list[dict[str, Any]] = []
    for path in directory.glob("*.json"):
        if path.name == "latest.json":
            continue
        report = load_json(path, {})
        state = (report.get("endpoints") or {}).get(COURT_MONITOR_ENDPOINT_ID) or {}
        if (
            state.get("discovery_status") == "complete"
            and state.get("materialization_status") == "complete"
        ):
            candidates.append(report)
    if not candidates:
        return None
    return max(candidates, key=lambda item: str(item.get("generated_at") or ""))


def _canonical_matches(root: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    by_title: dict[str, list[str]] = {}
    by_fileno: dict[str, list[str]] = {}
    for path in (root / "canonical" / "laws").glob("*.json"):
        law = load_json(path, {})
        law_id = str(law.get("id") or path.stem)
        metadata = law.get("metadata") or {}
        title = str(law.get("title") or metadata.get("name") or "").strip()
        fileno = str(metadata.get("fileno") or law.get("fileno") or "").strip()
        if title:
            by_title.setdefault(title, []).append(law_id)
        if fileno:
            by_fileno.setdefault(fileno, []).append(law_id)
    return by_title, by_fileno


def _suggested_action(change_type: str, candidate_type: str) -> str:
    if change_type == "removed":
        return "核对栏目可见性变化；不得据此删除记录或调整法律效力。"
    if change_type == "restored":
        return "核对恢复出现的栏目位置和页面证据。"
    if candidate_type == "compound_instruments":
        return "人工确定每份文书的精确标题、文号、正文边界和条文数后再受控拆分。"
    if candidate_type == "unknown_structure":
        return "人工检查页面DOM和正文载体，确认是否为可入库文书。"
    if change_type == "content_changed":
        return "复核正文差异和官方证据；确认后更新受控文档配置并重建 canonical。"
    if change_type == "metadata_changed":
        return "复核栏目标题、日期或URL变化是否影响受控文档配置。"
    return "判断是否需要入库；需要时补充精确受控文档规格，不需要时记录关闭决定。"


def _markdown(queue: dict[str, Any]) -> str:
    lines = [
        "# 最高法司法解释栏目主动复核队列",
        "",
        f"- 运行：`{queue['run_id']}`",
        f"- 待处理：{queue['actionable_count']} 条",
        "- 栏目变化仅表示发现线索，不直接改变法律效力。",
        "",
    ]
    if not queue["items"]:
        lines.append("当前没有待处理项目。")
        return "\n".join(lines) + "\n"
    lines.extend(
        [
            "| 变化 | 候选类型 | 页面ID | 标题 | 建议动作 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for item in queue["items"]:
        title = str(item.get("title") or "").replace("|", "\\|")
        action = str(item.get("suggested_action") or "").replace("|", "\\|")
        lines.append(
            f"| {item.get('change_type') or ''} | {item.get('candidate_type') or ''} "
            f"| `{item.get('official_page_id') or ''}` | {title} | {action} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def build_monitor_artifacts(
    *,
    run_id: str,
    root: Path | None = None,
) -> dict[str, Any]:
    output_root = root or output_dir()
    report = load_json(
        output_root / "reports" / "source_baselines" / f"{run_id}.json",
        {},
    )
    if not report:
        raise FileNotFoundError(f"source baseline report not found: {run_id}")
    checkpoint = load_json(
        output_root
        / "work"
        / "checkpoints"
        / "sources"
        / f"{COURT_MONITOR_ENDPOINT_ID}.json",
        {},
    )
    records_dir = (
        output_root
        / "raw"
        / "sources"
        / "records"
        / COURT_MONITOR_SOURCE_SYSTEM
    )
    records = {
        str((record.get("metadata") or {}).get("official_page_id") or ""): record
        for path in records_dir.glob("*.json")
        if (record := load_json(path, {}))
    }
    listing_members = {
        str(member.get("upstream_id")): member
        for page in (checkpoint.get("listing_pages") or {}).values()
        for member in (page.get("members") or [])
        if member.get("upstream_id")
    }
    checkpoint_records = checkpoint.get("records") or {}
    current_page_ids = set(checkpoint.get("last_complete_page_ids") or [])
    page_ids = sorted(
        set(listing_members) | set(records),
        key=lambda value: int(value) if value.isdigit() else value,
        reverse=True,
    )
    inventory_items: list[dict[str, Any]] = []
    for page_id in page_ids:
        record = records.get(page_id) or {}
        metadata = record.get("metadata") or {}
        listing = listing_members.get(page_id) or {}
        record_id = str(
            record.get("source_record_id")
            or source_record_id(COURT_MONITOR_SOURCE_SYSTEM, upstream_id=page_id)
        )
        state = checkpoint_records.get(record_id) or {}
        missing_count = int(state.get("missing_count") or 0)
        inventory_items.append(
            {
                "official_page_id": page_id,
                "source_record_id": record_id,
                "page_url": (
                    (record.get("source") or {}).get("page_url")
                    or listing.get("url")
                ),
                "title": metadata.get("name") or listing.get("title"),
                "listing_title": metadata.get("listing_title") or listing.get("title"),
                "listing_date": metadata.get("listing_date") or listing.get("listing_date"),
                "listing_page": listing.get("page_number") or metadata.get("listing_page"),
                "candidate_type": metadata.get("candidate_type"),
                "filenos": metadata.get("filenos") or [],
                "article_count": metadata.get("article_count"),
                "detail_status": (
                    record.get("ingest_status") if record else "not_materialized"
                ),
                "visible_in_latest_complete_enumeration": page_id in current_page_ids,
                "visibility_status": (
                    "removed"
                    if missing_count >= 2
                    else "missing"
                    if missing_count
                    else "visible"
                ),
                "missing_complete_runs": missing_count,
                "first_seen_at": listing.get("first_seen_at")
                or state.get("first_seen_at")
                or (record.get("source") or {}).get("fetched_at"),
                "last_seen_at": listing.get("last_seen_at") or state.get("last_seen_at"),
                "last_detail_verified_at": state.get("last_detail_verified_at"),
                "content_fingerprint": (
                    record.get("fingerprints") or {}
                ).get("content_sha256"),
                "response_fingerprint": (
                    record.get("fingerprints") or {}
                ).get("response_sha256"),
                "http_validators": state.get("http_validators")
                or (record.get("source") or {}).get("http_validators")
                or {},
                "raw_file": (record.get("source") or {}).get("raw_file"),
            }
        )

    endpoint_state = (report.get("endpoints") or {}).get(COURT_MONITOR_ENDPOINT_ID) or {}
    complete = (
        endpoint_state.get("discovery_status") == "complete"
        and endpoint_state.get("materialization_status") == "complete"
    )
    latest_complete = _latest_complete_run(output_root)
    inventory = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at": utc_now_iso(),
        "status": "complete" if complete else "incomplete",
        "reported_total": endpoint_state.get("reported_total"),
        "item_count": len(inventory_items),
        "visible_count": sum(
            item["visible_in_latest_complete_enumeration"] for item in inventory_items
        ),
        "candidate_type_counts": dict(
            sorted(Counter(item.get("candidate_type") or "unclassified" for item in inventory_items).items())
        ),
        "last_complete_run": (
            {
                "run_id": latest_complete.get("run_id"),
                "generated_at": latest_complete.get("generated_at"),
            }
            if latest_complete
            else None
        ),
        "listing_pages": [
            {
                "page_number": page.get("page_number"),
                "url": page.get("url"),
                "member_count": len(page.get("members") or []),
                "response_sha256": page.get("response_sha256"),
                "http_validators": page.get("http_validators") or {},
                "verified_at": page.get("verified_at"),
            }
            for page in sorted(
                (checkpoint.get("listing_pages") or {}).values(),
                key=lambda item: int(item.get("page_number") or 0),
            )
        ],
        "items": inventory_items,
    }

    directory = output_root / "reports" / "court_judicial_interpretation_monitor"
    save_json(directory / "inventory.json", inventory)
    save_json(directory / "runs" / f"{run_id}_inventory.json", inventory)

    existing = load_json(directory / "review_queue.json", {})
    queue_by_record = {
        str(item.get("source_record_id")): item
        for item in (existing.get("items") or [])
        if item.get("source_record_id")
    }
    decisions = (load_json(DECISIONS_PATH, {}) or {}).get("decisions") or {}
    by_title, by_fileno = _canonical_matches(output_root)
    inventory_by_record = {
        str(item["source_record_id"]): item for item in inventory_items
    }
    changes = [
        change
        for change in _jsonl(output_root / "work" / "changes" / f"{run_id}.jsonl")
        if change.get("endpoint_id") == COURT_MONITOR_ENDPOINT_ID
        and change.get("change_type") in _ACTIONABLE_CHANGES
    ]
    for change in changes:
        record_id = str(change.get("source_record_id") or "")
        item = inventory_by_record.get(record_id) or {}
        title = str(item.get("title") or "")
        filenos = [str(value) for value in item.get("filenos") or []]
        candidate_type = str(item.get("candidate_type") or "unknown_structure")
        queue_by_record[record_id] = {
            "source_record_id": record_id,
            "official_page_id": item.get("official_page_id"),
            "page_url": item.get("page_url"),
            "title": title,
            "change_type": change.get("change_type"),
            "candidate_type": candidate_type,
            "filenos": filenos,
            "article_count": item.get("article_count"),
            "raw_evidence": {
                "raw_file": item.get("raw_file"),
                "content_fingerprint": item.get("content_fingerprint"),
                "response_fingerprint": item.get("response_fingerprint"),
                "detected_at": change.get("detected_at"),
            },
            "possible_canonical_matches": sorted(
                set(by_title.get(title, []))
                | {
                    law_id
                    for fileno in filenos
                    for law_id in by_fileno.get(fileno, [])
                }
            ),
            "suggested_action": _suggested_action(
                str(change.get("change_type") or ""),
                candidate_type,
            ),
            "review_status": "pending",
            "first_queued_at": (
                queue_by_record.get(record_id) or {}
            ).get("first_queued_at")
            or utc_now_iso(),
            "last_queued_at": utc_now_iso(),
            "last_run_id": run_id,
        }

    active_items: list[dict[str, Any]] = []
    for record_id, item in queue_by_record.items():
        page_id = str(item.get("official_page_id") or "")
        decision = decisions.get(record_id) or decisions.get(page_id) or {}
        if str(decision.get("status") or "") in _CLOSED_STATUSES:
            continue
        active_items.append(item)
    active_items.sort(
        key=lambda item: (
            str(item.get("last_queued_at") or ""),
            str(item.get("official_page_id") or ""),
        ),
        reverse=True,
    )
    queue = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at": utc_now_iso(),
        "actionable_count": len(active_items),
        "candidate_type_counts": dict(
            sorted(Counter(item.get("candidate_type") or "unknown_structure" for item in active_items).items())
        ),
        "decision_file": str(DECISIONS_PATH),
        "items": active_items,
    }
    save_json(directory / "review_queue.json", queue)
    save_json(directory / "runs" / f"{run_id}_review_queue.json", queue)
    directory.mkdir(parents=True, exist_ok=True)
    markdown = _markdown(queue)
    (directory / "review_queue.md").write_text(markdown, encoding="utf-8")
    (directory / "runs" / f"{run_id}_review_queue.md").write_text(
        markdown,
        encoding="utf-8",
    )
    return {"inventory": inventory, "review_queue": queue}


__all__ = ["DECISIONS_PATH", "build_monitor_artifacts"]
