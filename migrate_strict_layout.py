#!/usr/bin/env python3
"""Migrate legacy output into strict raw/work/canonical/reports layers."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

from config import OUTPUT_DIR
from storage import (
    amac_sources_dir,
    attachment_index_path,
    canonical_dir,
    catalog_dir,
    laws_dir,
    load_json,
    raw_dir,
    reports_dir,
    save_json,
    utc_now_iso,
    work_dir,
    writs_dir,
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
        OUTPUT_DIR / "laws": laws_dir(),
        OUTPUT_DIR / "writs": writs_dir(),
        OUTPUT_DIR / "sources" / "amac": amac_sources_dir(),
        OUTPUT_DIR / "sources" / "amac_manifest.json": raw_dir()
        / "amac"
        / "manifest.json",
        OUTPUT_DIR / "assets" / "amac": raw_dir() / "assets" / "amac",
        OUTPUT_DIR / "assets" / "neris_attachments": raw_dir()
        / "assets"
        / "neris_attachments",
        OUTPUT_DIR / "assets" / "laws": raw_dir() / "assets" / "embedded",
        OUTPUT_DIR / "relations" / "revision_evidence_cache": raw_dir()
        / "neris"
        / "revision_evidence",
        OUTPUT_DIR / "manifest.json": raw_dir() / "neris" / "manifest.json",
        OUTPUT_DIR / "checkpoint.json": work_dir()
        / "checkpoints"
        / "checkpoint.json",
        OUTPUT_DIR / "normalized": work_dir() / "normalized_neris",
        OUTPUT_DIR / "catalog" / "laws": catalog_dir() / "laws",
        OUTPUT_DIR / "catalog" / "manifest.json": catalog_dir() / "manifest.json",
        OUTPUT_DIR / "catalog" / "review_queue.json": reports_dir()
        / "review_queue.json",
        OUTPUT_DIR / "relations" / "revisions.json": work_dir()
        / "relations"
        / "revisions.json",
        OUTPUT_DIR / "relations" / "related_laws.json": work_dir()
        / "relations"
        / "related_laws.json",
        OUTPUT_DIR / "relations" / "cases.json": work_dir()
        / "relations"
        / "cases.json",
        OUTPUT_DIR / "relations" / "catalog_relations.json": work_dir()
        / "relations"
        / "catalog_relations.json",
        OUTPUT_DIR / "relations" / "source_matches.json": canonical_dir()
        / "indexes"
        / "source_map.json",
        OUTPUT_DIR / "relations" / "coverage_gaps.json": reports_dir()
        / "coverage_gaps.json",
        OUTPUT_DIR / "sample": work_dir() / "samples",
        OUTPUT_DIR / "crawl.log": work_dir() / "logs" / "crawl.log",
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
        OUTPUT_DIR / "markdown",
        OUTPUT_DIR / "catalog",
        OUTPUT_DIR / "relations",
        OUTPUT_DIR / "sources",
        OUTPUT_DIR / "assets",
        OUTPUT_DIR / "normalized",
        OUTPUT_DIR / "laws",
        OUTPUT_DIR / "writs",
    ]
    for path in legacy_paths:
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed.append(str(path.relative_to(OUTPUT_DIR)))
    result = {
        "schema_version": 1,
        "updated_at": utc_now_iso(),
        "status": "legacy_cleaned",
        "removed": removed,
    }
    save_json(reports_dir() / "layout_cleanup.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移严格 raw/work/canonical/reports 布局")
    parser.add_argument("--execute", action="store_true", help="执行迁移")
    parser.add_argument("--cleanup", action="store_true", help="验证后删除旧派生目录")
    args = parser.parse_args()
    if not args.execute and not args.cleanup:
        print("未执行：使用 --execute 迁移，最终使用 --cleanup 清理旧目录")
        return 0
    try:
        if args.execute:
            print(migrate())
        if args.cleanup:
            print(cleanup_legacy())
    except Exception as exc:
        print(f"失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
