"""Stage, validate, and atomically publish the existing canonical catalog."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Any

from build_canonical_relations import build_canonical_relations
from build_catalog import build_catalog
from export_markdown_catalog import export_catalog_markdown
from normalize_catalog import normalize_catalog
import storage
from storage import publish_directory_atomic, save_json, utc_now_iso
from validate_catalog_exports import validate_catalog_exports


def _link_or_copy(source: str, target: str) -> str:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
    return target


def _copy_inputs(output_root: Path, staging_root: Path) -> None:
    raw = output_root / "raw"
    if raw.exists():
        shutil.copytree(raw, staging_root / "raw", copy_function=_link_or_copy)
    else:
        (staging_root / "raw").mkdir(parents=True)
    relations = output_root / "work" / "relations"
    if relations.exists():
        shutil.copytree(
            relations,
            staging_root / "work" / "relations",
            copy_function=_link_or_copy,
        )


def stage_and_publish_canonical(
    *,
    run_id: str,
    root: Path,
) -> dict[str, Any]:
    staging_root = root / "work" / "canonical_staging" / run_id
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging_root.mkdir(parents=True)
    _copy_inputs(root, staging_root)
    original_output = storage.OUTPUT_DIR
    issues: list[str] = []
    summary: dict[str, Any] = {}
    try:
        storage.OUTPUT_DIR = staging_root
        catalog_manifest = build_catalog(clean=True)
        normalized_manifest = normalize_catalog(force=True, clean=True)
        markdown_manifest = export_catalog_markdown(force=True, clean=True)
        graph = build_canonical_relations()
        issues, summary = validate_catalog_exports()
        if issues:
            raise RuntimeError(
                f"staged canonical validation failed with {len(issues)} issue(s): {issues[0]}"
            )
    except BaseException:
        if staging_root.exists():
            shutil.rmtree(staging_root)
        raise
    finally:
        storage.OUTPUT_DIR = original_output

    try:
        staged_catalog = staging_root / "work" / "catalog"
        if staged_catalog.exists():
            publish_directory_atomic(staged_catalog, root / "work" / "catalog")
        publish_directory_atomic(staging_root / "canonical", root / "canonical")
        report = {
            "schema_version": 1,
            "run_id": run_id,
            "status": "published",
            "published_at": utc_now_iso(),
            "catalog": catalog_manifest,
            "normalized": {
                "count": normalized_manifest.get("count"),
                "empty_content": normalized_manifest.get("empty_content"),
            },
            "markdown": {
                "count": markdown_manifest.get("count"),
                "bucket_counts": markdown_manifest.get("bucket_counts"),
            },
            "relations": graph.get("counts") or {},
            "validation": summary,
        }
        save_json(root / "reports" / "source_publications" / f"{run_id}.json", report)
        save_json(root / "reports" / "source_publications" / "latest.json", report)
        return report
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root)


__all__ = ["stage_and_publish_canonical"]
