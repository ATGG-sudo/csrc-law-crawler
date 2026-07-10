#!/usr/bin/env python3
"""Audit duplicate canonical JSON and Markdown outputs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from build_catalog import normalize_title
from config import OUTPUT_DIR


REPORT_FIELDS = [
    "kind",
    "severity",
    "group_id",
    "id",
    "title",
    "bucket",
    "file",
    "json_file",
    "content_status",
    "text_length",
    "body_hash",
    "asset_sha",
    "source_system",
    "reason",
    "recommended_action",
]
PLACEHOLDER_TEXT = "正文未能从官方文件中自动抽取"
COLLISION_SUFFIX_RE = re.compile(r" - [0-9a-f]{8}(?:-\d+)?$")
TRIAL_MARKER_RE = re.compile(r"[（(]\s*试行\s*[）)]|试行")
REVISION_MARKER_RE = re.compile(r"[（(]\s*(?:\d{4}年)?修订\s*[）)]")
LEADING_ITEM_MARKER_RE = re.compile(r"^\s*\d+(?:[-.、．]\d+)?[-.、．]?\s*")
WHITESPACE_RE = re.compile(r"\s+")


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _compact(text: Any) -> str:
    return WHITESPACE_RE.sub("", str(text or ""))


def _hash_compact(text: Any, *, min_length: int = 80) -> str:
    compact = _compact(text)
    if len(compact) < min_length:
        return ""
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


def first_h1(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _front_matter(markdown: str) -> dict[str, str]:
    lines = markdown.splitlines()
    if not lines or lines[0] != "---":
        return {}
    values: dict[str, str] = {}
    for line in lines[1:]:
        if line == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = _yaml_string(value.strip())
    return values


def _yaml_string(value: str) -> str:
    if value == '""':
        return ""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def markdown_main_body(markdown: str) -> str:
    lines = markdown.splitlines()
    if lines[:1] == ["---"]:
        for index, line in enumerate(lines[1:], start=1):
            if line == "---":
                lines = lines[index + 1 :]
                break
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines[:2] == ["| 字段 | 值 |", "| --- | --- |"]:
        while lines and lines[0].startswith("|"):
            lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)

    body: list[str] = []
    for line in lines:
        if line.startswith("## 官方来源") or line.startswith("## 资产"):
            break
        body.append(line)
    return "\n".join(body).strip()


def _dedupe_title_key(value: Any) -> str:
    text = str(value or "").strip()
    text = LEADING_ITEM_MARKER_RE.sub("", text)
    text = TRIAL_MARKER_RE.sub("", text)
    text = REVISION_MARKER_RE.sub("", text)
    return normalize_title(text)


def _json_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((root / "canonical" / "json").glob("law_*.json")):
        doc = _load_json(path, {})
        law_id = str(doc.get("id") or path.stem)
        text = str(doc.get("full_text_plain") or "")
        assets = doc.get("assets") or []
        records.append(
            {
                "id": law_id,
                "title": str(doc.get("title") or ""),
                "normalized_title": normalize_title(doc.get("title")),
                "bucket": "",
                "file": "",
                "json_file": _relative(root, path),
                "content_status": str(doc.get("content_status") or ""),
                "text_length": len(text),
                "body_hash": _hash_compact(text),
                "asset_shas": sorted(
                    {str(asset.get("sha256") or "") for asset in assets if asset.get("sha256")}
                ),
                "source_system": str((doc.get("preferred_source") or {}).get("system") or ""),
                "fileno": str((doc.get("metadata") or {}).get("fileno") or ""),
                "pub_date": str((doc.get("metadata") or {}).get("pub_date") or ""),
                "doc": doc,
            }
        )
    return records


def _markdown_records(
    root: Path, json_by_id: dict[str, dict[str, Any]]
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
]:
    manifest_path = root / "work" / "catalog" / "markdown_manifest.json"
    manifest = _load_json(manifest_path, {})
    items = [item for item in manifest.get("items") or [] if isinstance(item, dict)]
    files = sorted((root / "canonical" / "markdown").glob("*/*.md"))
    manifest_paths = [str(item.get("file") or "") for item in items]
    manifest_path_counts = Counter(path for path in manifest_paths if path)
    manifest_path_set = set(manifest_path_counts)
    file_path_set = {_relative(root, path) for path in files}

    records: list[dict[str, Any]] = []
    for item in items:
        rel = str(item.get("file") or "")
        path = root / rel if rel else root / "__missing__"
        law_id = str(item.get("id") or "")
        json_record = json_by_id.get(law_id, {})
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        front_matter = _front_matter(text)
        body = markdown_main_body(text)
        is_placeholder = PLACEHOLDER_TEXT in body
        body_hash = "" if is_placeholder else _hash_compact(body)
        records.append(
            {
                "id": law_id,
                "title": str(item.get("title") or front_matter.get("title") or ""),
                "h1": first_h1(text),
                "normalized_h1": normalize_title(first_h1(text)),
                "bucket": str(item.get("bucket") or ""),
                "file": rel,
                "json_file": str(item.get("source_file") or f"canonical/json/{law_id}.json"),
                "content_status": str(
                    front_matter.get("content_status") or json_record.get("content_status") or ""
                ),
                "text_length": int(item.get("text_length") or 0),
                "body_hash": body_hash,
                "asset_shas": list(json_record.get("asset_shas") or []),
                "source_system": str(json_record.get("source_system") or ""),
                "fileno": str(
                    (json_record.get("doc") or {}).get("metadata", {}).get("fileno") or ""
                ),
                "pub_date": str(
                    (json_record.get("doc") or {}).get("metadata", {}).get("pub_date") or ""
                ),
                "front_matter": front_matter,
                "path": path,
                "exists": path.exists(),
                "placeholder": is_placeholder,
                "json_record": json_record,
            }
        )

    summary = {
        "markdown_manifest_items": len(items),
        "markdown_files": len(files),
        "markdown_missing_files": len(manifest_path_set - file_path_set),
        "markdown_orphan_files": len(file_path_set - manifest_path_set),
        "duplicate_manifest_paths": sum(1 for count in manifest_path_counts.values() if count > 1),
        "duplicate_markdown_filenames": _duplicate_group_count(
            _group(files, lambda path: path.name)
        ),
        "duplicate_markdown_stems": _duplicate_group_count(_group(files, lambda path: path.stem)),
        "missing_paths": sorted(manifest_path_set - file_path_set),
        "orphan_paths": sorted(file_path_set - manifest_path_set),
        "manifest_path_counts": manifest_path_counts,
    }
    return records, summary


def _group(items: list[Any], key_func: Any) -> dict[str, list[Any]]:
    groups: dict[str, list[Any]] = defaultdict(list)
    for item in items:
        key = str(key_func(item) or "")
        if key:
            groups[key].append(item)
    return groups


def _duplicate_groups(groups: dict[str, list[Any]]) -> list[tuple[str, list[Any]]]:
    return [(key, values) for key, values in groups.items() if key and len(values) > 1]


def _duplicate_group_count(groups: dict[str, list[Any]]) -> int:
    return len(_duplicate_groups(groups))


def _row(
    record: dict[str, Any],
    *,
    kind: str,
    severity: str,
    group_id: str,
    reason: str,
    recommended_action: str,
    body_hash: str = "",
    asset_sha: str = "",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "severity": severity,
        "group_id": group_id,
        "id": record.get("id") or "",
        "title": record.get("title") or "",
        "bucket": record.get("bucket") or "",
        "file": record.get("file") or "",
        "json_file": record.get("json_file") or "",
        "content_status": record.get("content_status") or "",
        "text_length": record.get("text_length") or 0,
        "body_hash": body_hash or record.get("body_hash") or "",
        "asset_sha": asset_sha,
        "source_system": record.get("source_system") or "",
        "reason": reason,
        "recommended_action": recommended_action,
    }


def _severity_for_title_group(records: list[dict[str, Any]]) -> str:
    filenos = {record.get("fileno") for record in records if record.get("fileno")}
    dates = {record.get("pub_date") for record in records if record.get("pub_date")}
    lengths = [int(record.get("text_length") or 0) for record in records]
    close_length = False
    if len(lengths) >= 2 and max(lengths) > 0:
        close_length = (max(lengths) - min(lengths)) / max(lengths) <= 0.05
    if len(filenos) == 1 or len(dates) == 1 or close_length:
        return "high"
    return "medium"


def _add_group_rows(
    rows: list[dict[str, Any]],
    *,
    kind: str,
    severity: str,
    groups: list[tuple[str, list[dict[str, Any]]]],
    reason: str,
    recommended_action: str,
    body_hash_key: bool = False,
    asset_sha_key: bool = False,
) -> None:
    for index, (key, records) in enumerate(groups, start=1):
        group_id = f"{kind}:{index:04d}"
        for record in records:
            rows.append(
                _row(
                    record,
                    kind=kind,
                    severity=severity,
                    group_id=group_id,
                    reason=reason,
                    recommended_action=recommended_action,
                    body_hash=key if body_hash_key else "",
                    asset_sha=key if asset_sha_key else "",
                )
            )


def _filename_collision_groups(
    records: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        path = Path(str(record.get("file") or ""))
        if not path.name:
            continue
        base = COLLISION_SUFFIX_RE.sub("", path.stem)
        groups[base].append(record)
    return _duplicate_groups(groups)


def _mismatch_rows(
    records: list[dict[str, Any]],
    *,
    start_index: int = 1,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    counter = start_index
    for record in records:
        issues: list[str] = []
        if not record.get("exists"):
            issues.append("Markdown manifest 指向的文件不存在")
        json_record = record.get("json_record") or {}
        if not json_record:
            issues.append("Markdown id 无法回连 canonical/json")
        front_matter = record.get("front_matter") or {}
        if front_matter:
            for field in ("id", "title", "content_status"):
                expected = str(
                    record.get(field)
                    if field != "title"
                    else (json_record.get("title") or record.get("title") or "")
                )
                actual = str(front_matter.get(field) or "")
                if actual != expected:
                    issues.append(f"front matter {field}={actual!r} != {expected!r}")
            expected_source = str((json_record.get("doc") or {}).get("source_file") or "")
            actual_source = str(front_matter.get("source_file") or "")
            if expected_source and actual_source != expected_source:
                issues.append(f"front matter source_file={actual_source!r} != {expected_source!r}")
        json_len = int(json_record.get("text_length") or 0)
        if json_record and int(record.get("text_length") or 0) != json_len:
            issues.append(
                f"manifest text_length={record.get('text_length')} != json text_length={json_len}"
            )
        if not issues:
            continue
        rows.append(
            _row(
                record,
                kind="markdown_json_mismatch",
                severity="high",
                group_id=f"markdown_json_mismatch:{counter:04d}",
                reason="; ".join(issues),
                recommended_action="先修复 Markdown manifest/front matter 与 canonical JSON 的回连关系",
            )
        )
        counter += 1
    return rows


def build_duplicate_report(root: Path) -> dict[str, Any]:
    root = root.resolve()
    json_records = _json_records(root)
    json_by_id = {record["id"]: record for record in json_records}
    markdown_records, markdown_summary = _markdown_records(root, json_by_id)

    rows: list[dict[str, Any]] = []

    json_title_groups = _duplicate_groups(
        _group(json_records, lambda record: record["normalized_title"])
    )
    for index, (_, records) in enumerate(json_title_groups, start=1):
        _add_group_rows(
            rows,
            kind="json_title",
            severity=_severity_for_title_group(records),
            groups=[("", records)],
            reason="canonical JSON 标题归一化后重复",
            recommended_action="回到 build_catalog.py 合并规则核实 sources/assets；合法多版本保留并写明理由",
        )
        for row in rows[-len(records) :]:
            row["group_id"] = f"json_title:{index:04d}"

    _add_group_rows(
        rows,
        kind="json_body",
        severity="strong",
        groups=_duplicate_groups(_group(json_records, lambda record: record["body_hash"])),
        reason="canonical JSON full_text_plain 归一化 hash 完全相同",
        recommended_action="强重复：优先合并 canonical id，保留正文更完整、来源更权威的记录",
        body_hash_key=True,
    )

    asset_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in json_records:
        for asset_sha in record.get("asset_shas") or []:
            asset_groups[asset_sha].append(record)
    _add_group_rows(
        rows,
        kind="json_asset_sha",
        severity="high",
        groups=_duplicate_groups(asset_groups),
        reason="不同 canonical JSON 复用同一附件 sha256",
        recommended_action="核实同一附件是否被拆成多个 canonical id；需要时合并 sources/assets",
        asset_sha_key=True,
    )

    markdown_h1_groups = _duplicate_groups(
        _group(markdown_records, lambda record: record["normalized_h1"])
    )
    for index, (_, records) in enumerate(markdown_h1_groups, start=1):
        _add_group_rows(
            rows,
            kind="markdown_h1",
            severity=_severity_for_title_group(records),
            groups=[("", records)],
            reason="Markdown 可见 H1 标题归一化后重复",
            recommended_action="人工复核同标题 Markdown，区分多版本、公告正文和附件正文",
        )
        for row in rows[-len(records) :]:
            row["group_id"] = f"markdown_h1:{index:04d}"

    _add_group_rows(
        rows,
        kind="markdown_body",
        severity="strong",
        groups=_duplicate_groups(_group(markdown_records, lambda record: record["body_hash"])),
        reason="Markdown 主正文剥离生成区块后 hash 完全相同",
        recommended_action="不要在导出层去重；回查 canonical JSON 是否应合并",
        body_hash_key=True,
    )

    _add_group_rows(
        rows,
        kind="markdown_filename_collision",
        severity="low",
        groups=_filename_collision_groups(markdown_records),
        reason="导出器用 law id 后缀解决同名 Markdown 路径冲突",
        recommended_action="作为实体重复线索复核；路径本身无需修复",
    )

    rows.extend(_mismatch_rows(markdown_records))

    missing_paths = markdown_summary.pop("missing_paths")
    orphan_paths = markdown_summary.pop("orphan_paths")
    manifest_path_counts = markdown_summary.pop("manifest_path_counts")
    for index, path in enumerate(missing_paths, start=1):
        rows.append(
            _row(
                {"file": path},
                kind="markdown_missing",
                severity="high",
                group_id=f"markdown_missing:{index:04d}",
                reason="Markdown manifest 指向的文件不存在",
                recommended_action="重跑 export_markdown_catalog.py --force --clean",
            )
        )
    for index, path in enumerate(orphan_paths, start=1):
        rows.append(
            _row(
                {"file": path},
                kind="markdown_orphan",
                severity="medium",
                group_id=f"markdown_orphan:{index:04d}",
                reason="canonical/markdown 存在未被 manifest 引用的文件",
                recommended_action="重跑 export_markdown_catalog.py --force --clean 清理孤儿文件",
            )
        )
    duplicate_manifest_paths = [
        path for path, count in manifest_path_counts.items() if path and count > 1
    ]
    for index, path in enumerate(sorted(duplicate_manifest_paths), start=1):
        rows.append(
            _row(
                {"file": path},
                kind="markdown_manifest_path",
                severity="high",
                group_id=f"markdown_manifest_path:{index:04d}",
                reason="Markdown manifest 中同一路径出现多次",
                recommended_action="检查 export_markdown_catalog.py 的路径分配逻辑",
            )
        )

    kind_counts = Counter(str(row["kind"]) for row in rows)
    severity_counts = Counter(str(row["severity"]) for row in rows)
    summary = {
        "output_root": root.as_posix(),
        "json_count": len(json_records),
        "json_exact_title_duplicate_groups": _duplicate_group_count(
            _group(json_records, lambda record: record["title"])
        ),
        "json_title_duplicate_groups": len(json_title_groups),
        "json_body_duplicate_groups": _duplicate_group_count(
            _group(json_records, lambda record: record["body_hash"])
        ),
        "json_asset_duplicate_groups": _duplicate_group_count(asset_groups),
        "markdown_h1_duplicate_groups": len(markdown_h1_groups),
        "markdown_body_duplicate_groups": _duplicate_group_count(
            _group(markdown_records, lambda record: record["body_hash"])
        ),
        "markdown_filename_collision_groups": len(_filename_collision_groups(markdown_records)),
        "metadata_only_placeholder_files": sum(
            1 for record in markdown_records if record.get("placeholder")
        ),
        "rows": len(rows),
        "rows_by_kind": dict(sorted(kind_counts.items())),
        "rows_by_severity": dict(sorted(severity_counts.items())),
        **markdown_summary,
    }
    return {"summary": summary, "rows": rows}


def _decision_for_group(kind: str, rows: list[dict[str, Any]]) -> tuple[str, str]:
    title_keys = {_dedupe_title_key(row.get("title")) for row in rows if row.get("title")}
    title_keys.discard("")
    if kind in {"json_body", "json_asset_sha"} and len(title_keys) == 1:
        return "auto_merge", "强重复且标题等价"
    if kind in {"json_body", "markdown_body"}:
        if len(title_keys) > 1:
            return "content_repair", "正文重复但标题不同，优先排查公告正文误用"
        return "review_only", "正文重复但证据不足，人工复核后再合并"
    if kind in {"json_title", "markdown_h1"}:
        return "review_only", "仅标题重复不自动合并"
    if kind == "markdown_filename_collision":
        return "keep", "导出器已用 canonical id 后缀解决路径冲突"
    return "review_only", "默认人工复核"


def build_dedupe_plan(report: dict[str, Any]) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in report.get("rows") or []:
        kind = str(row.get("kind") or "")
        group_id = str(row.get("group_id") or "")
        if kind and group_id:
            groups[(kind, group_id)].append(row)

    plan_groups: list[dict[str, Any]] = []
    for (kind, group_id), rows in sorted(groups.items()):
        decision, reason = _decision_for_group(kind, rows)
        plan_groups.append(
            {
                "kind": kind,
                "group_id": group_id,
                "decision": decision,
                "reason": reason,
                "ids": [str(row.get("id") or "") for row in rows if row.get("id")],
                "titles": sorted({str(row.get("title") or "") for row in rows if row.get("title")}),
                "rows": len(rows),
            }
        )

    decision_counts = Counter(group["decision"] for group in plan_groups)
    return {
        "schema_version": 1,
        "summary": {
            "groups": len(plan_groups),
            "by_decision": dict(sorted(decision_counts.items())),
        },
        "groups": plan_groups,
    }


def write_report(
    report: dict[str, Any],
    dedupe_plan: dict[str, Any],
    json_path: Path,
    csv_path: Path,
    plan_path: Path,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps(dedupe_plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(report["rows"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查 canonical JSON 与 Markdown 输出中的重复制度")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=OUTPUT_DIR,
        help="输出根目录，默认读取 CSRC_OUTPUT_ROOT/配置",
    )
    parser.add_argument(
        "--json-report",
        type=Path,
        default=None,
        help="JSON 报告路径，默认写入 reports/canonical_duplicate_audit.json",
    )
    parser.add_argument(
        "--csv-report",
        type=Path,
        default=None,
        help="CSV 明细路径，默认写入 reports/canonical_duplicate_audit.csv",
    )
    parser.add_argument(
        "--dedupe-plan",
        type=Path,
        default=None,
        help="组级去重决策路径，默认写入 reports/canonical_dedupe_plan.json",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="只打印 summary，不写报告文件",
    )
    args = parser.parse_args(argv)

    root = args.output_root
    report = build_duplicate_report(root)
    dedupe_plan = build_dedupe_plan(report)
    if not args.no_write:
        json_report = args.json_report or root / "reports" / "canonical_duplicate_audit.json"
        csv_report = args.csv_report or root / "reports" / "canonical_duplicate_audit.csv"
        plan_report = args.dedupe_plan or root / "reports" / "canonical_dedupe_plan.json"
        report["summary"]["json_report"] = json_report.as_posix()
        report["summary"]["csv_report"] = csv_report.as_posix()
        report["summary"]["dedupe_plan"] = plan_report.as_posix()
        write_report(report, dedupe_plan, json_report, csv_report, plan_report)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
