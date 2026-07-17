#!/usr/bin/env python3
"""Migrate legacy output into strict raw/work/canonical/reports layers."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from storage import (
    amac_sources_dir,
    attachment_index_path,
    canonical_dir,
    catalog_dir,
    laws_dir,
    load_json,
    output_dir,
    raw_dir,
    relative_to_output,
    reports_dir,
    run_with_output_lock,
    save_json,
    utc_now_iso,
    work_dir,
    writs_dir,
)
from runtime import log_event


ROOT_LEGACY_NAMES = (
    "CSRC.zip",
    "check_canonical.py",
    ".codebuddy",
    ".workbuddy",
    "workbuddy",
    "workbuddy.zip",
)


def _merge_move(source: Path, target: Path) -> int:
    if not source.exists():
        return 0
    if source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            source.unlink()
        else:
            shutil.move(str(source), str(target))
        return 1
    moved = 0
    target.mkdir(parents=True, exist_ok=True)
    for child in list(source.iterdir()):
        destination = target / child.name
        if child.is_dir():
            moved += _merge_move(child, destination)
        elif destination.exists():
            child.unlink()
        else:
            shutil.move(str(child), str(destination))
            moved += 1
    try:
        source.rmdir()
    except OSError:
        pass
    return moved


def _rewrite_local_paths(value: Any) -> Any:
    replacements = {
        "assets/amac/": "raw/assets/amac/",
        "assets/neris_attachments/": "raw/assets/neris_attachments/",
        "assets/laws/": "raw/assets/embedded/",
        "laws/": "raw/neris/laws/",
        "writs/": "raw/neris/writs/",
        "sources/amac/": "raw/amac/records/",
    }
    if isinstance(value, dict):
        return {key: _rewrite_local_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_local_paths(item) for item in value]
    if isinstance(value, str):
        for old, new in replacements.items():
            if value.startswith(old):
                return new + value[len(old) :]
    return value


def _separate_law_runtime_state() -> dict[str, int]:
    counts = {"laws": 0, "revision_refs_removed": 0, "attachment_indexes": 0}
    for path in sorted(laws_dir().glob("reg_*.json")):
        doc = load_json(path, {})
        metadata = doc.get("metadata") or {}
        law_id = str(metadata.get("id") or path.stem.removeprefix("reg_"))
        counts["laws"] += 1
        if doc.pop("revision_ref", None) is not None:
            counts["revision_refs_removed"] += 1
        attachments = doc.pop("source_attachments", None)
        source = doc.get("source") or {}
        checked_at = source.pop("attachments_checked_at", None)
        if attachments is not None or checked_at:
            save_json(
                attachment_index_path(law_id),
                {
                    "schema_version": 1,
                    "law_id": law_id,
                    "checked_at": checked_at,
                    "attachments": _rewrite_local_paths(attachments or []),
                },
            )
            counts["attachment_indexes"] += 1
        doc["source"] = source
        save_json(path, _rewrite_local_paths(doc))
    return counts


def _rewrite_json_tree(root: Path) -> int:
    changed = 0
    for path in sorted(root.rglob("*.json")) if root.exists() else []:
        doc = load_json(path, {})
        rewritten = _rewrite_local_paths(doc)
        if rewritten != doc:
            save_json(path, rewritten)
            changed += 1
    return changed


def migrate() -> dict[str, Any]:
    moves = {
        output_dir() / "laws": laws_dir(),
        output_dir() / "writs": writs_dir(),
        output_dir() / "sources" / "amac": amac_sources_dir(),
        output_dir() / "sources" / "amac_manifest.json": raw_dir()
        / "amac"
        / "manifest.json",
        output_dir() / "assets" / "amac": raw_dir() / "assets" / "amac",
        output_dir() / "assets" / "neris_attachments": raw_dir()
        / "assets"
        / "neris_attachments",
        output_dir() / "assets" / "laws": raw_dir() / "assets" / "embedded",
        output_dir() / "relations" / "revision_evidence_cache": raw_dir()
        / "neris"
        / "revision_evidence",
        output_dir() / "manifest.json": raw_dir() / "neris" / "manifest.json",
        output_dir() / "checkpoint.json": work_dir()
        / "checkpoints"
        / "checkpoint.json",
        output_dir() / "normalized": work_dir() / "normalized_neris",
        output_dir() / "catalog" / "laws": catalog_dir() / "laws",
        output_dir() / "catalog" / "manifest.json": catalog_dir() / "manifest.json",
        output_dir() / "catalog" / "review_queue.json": reports_dir()
        / "review_queue.json",
        output_dir() / "relations" / "revisions.json": work_dir()
        / "relations"
        / "revisions.json",
        output_dir() / "relations" / "related_laws.json": work_dir()
        / "relations"
        / "related_laws.json",
        output_dir() / "relations" / "cases.json": work_dir()
        / "relations"
        / "cases.json",
        output_dir() / "relations" / "catalog_relations.json": work_dir()
        / "relations"
        / "catalog_relations.json",
        output_dir() / "relations" / "source_matches.json": canonical_dir()
        / "indexes"
        / "source_map.json",
        output_dir() / "relations" / "coverage_gaps.json": reports_dir()
        / "coverage_gaps.json",
        output_dir() / "sample": work_dir() / "samples",
        output_dir() / "crawl.log": work_dir() / "logs" / "crawl.log",
    }
    moved = 0
    for source, target in moves.items():
        moved += _merge_move(source, target)

    raw_state = _separate_law_runtime_state()
    rewritten = _rewrite_json_tree(raw_dir() / "amac")
    rewritten += _rewrite_json_tree(raw_dir() / "neris" / "attachment_index")
    rewritten += _rewrite_json_tree(raw_dir() / "assets" / "embedded")
    rewritten += _rewrite_json_tree(work_dir() / "relations")
    rewritten += _rewrite_json_tree(work_dir() / "normalized_neris")

    report = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "status": "migrated",
        "moved_files": moved,
        "rewritten_files": rewritten,
        "raw_state": raw_state,
    }
    save_json(reports_dir() / "layout_migration.json", report)
    return report


def cleanup_legacy() -> dict[str, Any]:
    manifest = load_json(canonical_dir() / "manifest.json", {})
    graph = canonical_dir() / "relations" / "graph.json"
    canonical_json = list((canonical_dir() / "json").glob("law_*.json"))
    canonical_markdown = list((canonical_dir() / "markdown").glob("*/*.md"))
    if (
        not graph.exists()
        or manifest.get("json_count") != len(canonical_json)
        or manifest.get("markdown_count") != len(canonical_markdown)
        or len(canonical_json) == 0
    ):
        raise RuntimeError("canonical 校验未完成，拒绝清理旧目录")

    removed: list[str] = []
    legacy_paths = [
        output_dir() / "markdown",
        output_dir() / "catalog",
        output_dir() / "relations",
        output_dir() / "sources",
        output_dir() / "assets",
        output_dir() / "normalized",
        output_dir() / "laws",
        output_dir() / "writs",
    ]
    for path in legacy_paths:
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed.append(relative_to_output(path))
    result = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "status": "legacy_cleaned",
        "removed": removed,
    }
    save_json(reports_dir() / "layout_cleanup.json", result)
    return result


def audit_root_legacy() -> dict[str, Any]:
    """Return a read-only inventory of known legacy root entries."""
    items: list[dict[str, Any]] = []
    for name in ROOT_LEGACY_NAMES:
        path = output_dir() / name
        if not path.exists():
            continue
        items.append(
            {
                "name": name,
                "path": str(path),
                "kind": "directory" if path.is_dir() else "file",
                "size_bytes": path.stat().st_size if path.is_file() else None,
            }
        )
    return {
        "schema_version": 1,
        "audited_at": utc_now_iso(),
        "mode": "read_only",
        "output_root": str(output_dir()),
        "count": len(items),
        "items": items,
    }


def _require_validated_canonical() -> None:
    manifest = load_json(canonical_dir() / "manifest.json", {})
    json_count = len(list((canonical_dir() / "json").glob("law_*.json")))
    markdown_count = len(list((canonical_dir() / "markdown").glob("*/*.md")))
    library_count = len(list((canonical_dir() / "library").glob("**/*.md")))
    if (
        not (canonical_dir() / "relations" / "graph.json").exists()
        or not json_count
        or manifest.get("json_count") != json_count
        or manifest.get("markdown_count") != markdown_count
        or manifest.get("library_count") != library_count
    ):
        raise RuntimeError("canonical/library 校验未完成，拒绝隔离根目录旧文件")


def quarantine_root_legacy(target_root: Path) -> dict[str, Any]:
    """Move known root legacy entries to an explicit archive; never delete."""
    _require_validated_canonical()
    target_root = target_root.resolve()
    if target_root == output_dir().resolve() or output_dir().resolve() in target_root.parents:
        raise ValueError("隔离目录必须位于 CSRC 输出根目录之外")
    inventory = audit_root_legacy()
    target_root.mkdir(parents=True, exist_ok=True)
    moved: list[dict[str, Any]] = []
    for item in inventory["items"]:
        source = Path(str(item["path"]))
        target = target_root / source.name
        if target.exists():
            raise FileExistsError(f"隔离目标已存在，拒绝覆盖: {target}")
        shutil.move(str(source), str(target))
        moved.append({**item, "source": str(source), "target": str(target)})
    result = {
        "schema_version": 1,
        "quarantined_at": utc_now_iso(),
        "status": "quarantined_without_deletion",
        "source_root": str(output_dir()),
        "target_root": str(target_root),
        "count": len(moved),
        "items": moved,
    }
    save_json(target_root / "migration_manifest.json", result)
    save_json(reports_dir() / "root_quarantine.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移严格 raw/work/canonical/reports 布局")
    parser.add_argument("--execute", action="store_true", help="执行迁移")
    parser.add_argument("--cleanup", action="store_true", help="验证后删除旧派生目录")
    parser.add_argument(
        "--audit-root",
        action="store_true",
        help="只读审计 CSRC 根目录中的已知旧文件",
    )
    parser.add_argument(
        "--quarantine-root",
        type=Path,
        help="验证后将根目录旧文件移至明确指定的隔离目录（不删除）",
    )
    args = parser.parse_args()
    if not any((args.execute, args.cleanup, args.audit_root, args.quarantine_root)):
        log_event(
            "cli_message",
            message=(
                "未执行：使用 --audit-root 只读审计、--execute 迁移，"
                "或 --quarantine-root PATH 隔离根目录旧文件"
            ),
        )
        return 0
    try:
        if args.execute:
            log_event("cli_result", message=json.dumps(migrate(), ensure_ascii=False))
        if args.cleanup:
            log_event("cli_result", message=json.dumps(cleanup_legacy(), ensure_ascii=False))
        if args.audit_root:
            log_event(
                "cli_result",
                message=json.dumps(audit_root_legacy(), ensure_ascii=False),
            )
        if args.quarantine_root:
            log_event(
                "cli_result",
                message=json.dumps(
                    quarantine_root_legacy(args.quarantine_root), ensure_ascii=False
                ),
            )
    except Exception as exc:
        log_event("cli_error", level="ERROR", message=f"失败: {exc}", error_message=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_with_output_lock(main, "migrate-strict-layout"))
