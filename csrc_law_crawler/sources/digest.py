"""Build a small auditable digest from source run and change artifacts."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import html
import json
from pathlib import Path
from typing import Any

from storage import load_json, output_dir, save_json, utc_now_iso


def _jsonl(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not path.is_file():
        return items
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            if isinstance(item, dict):
                items.append(item)
    return items


def _markdown(report: dict[str, Any]) -> str:
    counts = report["counts"]
    candidate_counts = "、".join(
        f"{name} {count}"
        for name, count in (counts.get("candidate_type_counts") or {}).items()
    ) or "无"
    last_complete = counts.get("last_complete_run") or {}
    lines = [
        f"# 信源动态摘要 {report['date']}",
        "",
        f"- 运行状态：`{report['status']}`",
        f"- 端点：{counts['attempted']}/{counts['selected_endpoints']} 已尝试，"
        f"{counts['discovery_complete']} 个发现完成，"
        f"{counts['materialization_complete']} 个材料化完成",
        f"- 变化：{counts['changes']} 条",
        f"- 主动复核：{counts.get('actionable_count', 0)} 条",
        f"- 请求：列表 {counts.get('list_requests', 0)} 次（304 "
        f"{counts.get('list_not_modified', 0)}），详情 "
        f"{counts.get('detail_requests', 0)} 次（304 "
        f"{counts.get('detail_not_modified', 0)}）",
        f"- 解析失败：{counts.get('parsing_failures', 0)} 条",
        f"- 候选类型：{candidate_counts}",
        f"- 最近完整运行：`{last_complete.get('run_id') or '无'}`",
        "",
        "## 变化明细",
        "",
    ]
    changes = report["changes"]
    if not changes:
        lines.append("本次没有检测到内容、元数据、删除或恢复变化。")
    else:
        lines.extend(["| 类型 | 信源端点 | 记录 ID |", "| --- | --- | --- |"])
        for item in changes:
            lines.append(
                "| {change_type} | {endpoint_id} | `{source_record_id}` |".format(
                    change_type=str(item.get("change_type") or ""),
                    endpoint_id=str(item.get("endpoint_id") or ""),
                    source_record_id=str(item.get("source_record_id") or ""),
                )
            )
    return "\n".join(lines).rstrip() + "\n"


def _html(report: dict[str, Any]) -> str:
    counts = report["counts"]
    candidate_counts = "、".join(
        f"{name} {count}"
        for name, count in (counts.get("candidate_type_counts") or {}).items()
    ) or "无"
    rows = "".join(
        "<tr><td>{}</td><td>{}</td><td><code>{}</code></td></tr>".format(
            html.escape(str(item.get("change_type") or "")),
            html.escape(str(item.get("endpoint_id") or "")),
            html.escape(str(item.get("source_record_id") or "")),
        )
        for item in report["changes"]
    )
    if not rows:
        rows = '<tr><td colspan="3">没有检测到变化</td></tr>'
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>信源动态摘要</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:1080px;margin:2rem auto;padding:0 1rem}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ddd;padding:.5rem;text-align:left}}th{{background:#f5f5f5}}</style>
</head><body><h1>信源动态摘要 {html.escape(report["date"])}</h1>
    <p>状态：<strong>{html.escape(report["status"])}</strong>；
    变化：{report["counts"]["changes"]} 条；
    主动复核：{report["counts"].get("actionable_count", 0)} 条。</p>
    <p>候选类型：{html.escape(candidate_counts)}；
    列表请求：{counts.get("list_requests", 0)}；
    详情请求：{counts.get("detail_requests", 0)}；
    详情304：{counts.get("detail_not_modified", 0)}；
    解析失败：{counts.get("parsing_failures", 0)}。</p>
<table><thead><tr><th>类型</th><th>信源端点</th><th>记录 ID</th></tr></thead><tbody>{rows}</tbody></table>
</body></html>"""


def build_digest(
    *,
    run_id: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    output_root = root or output_dir()
    latest = load_json(output_root / "reports" / "source_baselines" / "latest.json", {})
    selected_run_id = run_id or str(latest.get("run_id") or "")
    if not selected_run_id:
        raise FileNotFoundError("no source baseline report is available")
    baseline = load_json(
        output_root / "reports" / "source_baselines" / f"{selected_run_id}.json",
        latest if latest.get("run_id") == selected_run_id else {},
    )
    if not baseline:
        raise FileNotFoundError(f"source baseline report not found: {selected_run_id}")
    changes = _jsonl(output_root / "work" / "changes" / f"{selected_run_id}.jsonl")
    change_counts = dict(sorted(Counter(item.get("change_type") for item in changes).items()))
    counts = dict(baseline.get("counts") or {})
    counts["changes"] = len(changes)
    monitor_dir = output_root / "reports" / "court_judicial_interpretation_monitor"
    monitor_inventory = load_json(monitor_dir / "inventory.json", {})
    monitor_queue = load_json(monitor_dir / "review_queue.json", {})
    if monitor_inventory.get("run_id") == selected_run_id:
        counts["actionable_count"] = int(monitor_queue.get("actionable_count") or 0)
        counts["candidate_type_counts"] = (
            monitor_inventory.get("candidate_type_counts") or {}
        )
        counts["last_complete_run"] = monitor_inventory.get("last_complete_run")
    else:
        counts.setdefault("actionable_count", 0)
        counts.setdefault("candidate_type_counts", {})
        counts.setdefault("last_complete_run", None)
    counts["parsing_failures"] = int(counts.get("failed") or 0) + int(
        counts.get("list_parse_failures") or 0
    )
    report = {
        "schema_version": 1,
        "run_id": selected_run_id,
        "date": datetime.now(tz=timezone.utc).date().isoformat(),
        "generated_at": utc_now_iso(),
        "status": baseline.get("status") or "incomplete",
        "counts": counts,
        "change_counts": change_counts,
        "changes": changes,
    }
    directory = output_root / "reports" / "digests"
    save_json(directory / f"{selected_run_id}.json", report)
    save_json(directory / "latest.json", report)
    markdown = _markdown(report)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{selected_run_id}.md").write_text(markdown, encoding="utf-8")
    (directory / "latest.md").write_text(markdown, encoding="utf-8")
    rendered_html = _html(report)
    (directory / f"{selected_run_id}.html").write_text(rendered_html, encoding="utf-8")
    (directory / "latest.html").write_text(rendered_html, encoding="utf-8")
    return report


__all__ = ["build_digest"]
