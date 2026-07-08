#!/usr/bin/env python3
"""Validate the AMAC source layer, source matches, and canonical catalog."""

from __future__ import annotations

import json
import sys

from models import format_model_issues
from runtime import log_event
from storage import (
    catalog_dir,
    catalog_laws_dir,
    catalog_relations_path,
    iter_amac_source_files,
    listed_output_files,
    load_json,
    output_path,
    run_with_context,
    source_matches_path,
)


def catalog_manifest_path():
    return catalog_dir() / "manifest.json"


def _catalog_entity_files():
    return listed_output_files(
        catalog_manifest_path(),
        field="file",
        fallback_dir=catalog_laws_dir(),
        pattern="law_*.json",
    )


def _amac_source_files():
    return iter_amac_source_files()


def validate_catalog() -> tuple[list[str], dict[str, int]]:
    issues: list[str] = []
    catalog_files = _catalog_entity_files()
    entity_ids: set[str] = set()
    source_refs: set[tuple[str, str]] = set()

    for path in catalog_files:
        entity = load_json(path, {})
        issues.extend(format_model_issues("catalog_entity", path.name, entity))
        entity_id = str(entity.get("id") or "")
        if not entity_id:
            issues.append(f"{path.name}: missing id")
            continue
        if path.stem != entity_id:
            issues.append(f"{path.name}: filename/id mismatch")
        if entity_id in entity_ids:
            issues.append(f"{path.name}: duplicate canonical id")
        entity_ids.add(entity_id)
        if not entity.get("sources"):
            issues.append(f"{path.name}: no sources")
        for source in entity.get("sources") or []:
            key = (str(source.get("system")), str(source.get("record_id")))
            source_refs.add(key)
            local_file = source.get("local_file")
            if local_file and not output_path(str(local_file)).exists():
                issues.append(f"{path.name}: missing source file {local_file}")

    amac_ids = set()
    for path in _amac_source_files():
        record = load_json(path, {})
        issues.extend(format_model_issues("source_record", path.name, record))
        amac_ids.add(str(record.get("source_record_id") or path.stem))
    matches = (load_json(source_matches_path(), {}).get("items") or {})
    for source_id, match in matches.items():
        canonical_id = str(match.get("canonical_id") or "")
        if canonical_id not in entity_ids:
            issues.append(f"source match {source_id}: missing canonical {canonical_id}")
        if source_id.startswith("amac_") and source_id not in amac_ids:
            # Attachment source IDs are nested in AMAC records rather than top-level files.
            if not source_id.startswith("amac_asset_"):
                issues.append(f"source match {source_id}: missing AMAC source record")

    relations = load_json(catalog_relations_path(), {}).get("items") or []
    for index, relation in enumerate(relations):
        if str(relation.get("from")) not in entity_ids:
            issues.append(f"relation[{index}]: missing from endpoint")
        if str(relation.get("to")) not in entity_ids:
            issues.append(f"relation[{index}]: missing to endpoint")

    summary = {
        "amac_files": len(amac_ids),
        "catalog_laws": len(entity_ids),
        "source_matches": len(matches),
        "source_refs": len(source_refs),
        "relations": len(relations),
        "issues": len(issues),
    }
    return issues, summary


def main() -> int:
    issues, summary = validate_catalog()
    log_event("validation_summary", message=json.dumps(summary, ensure_ascii=False, indent=2))
    if issues:
        log_event("validation_issues", level="ERROR", message="\n问题:")
        for issue in issues[:100]:
            log_event("validation_issue", level="ERROR", message=f"  - {issue}", issue=issue)
        return 1
    log_event("validation_passed", message="\n多源目录校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(run_with_context(main, "validate-catalog"))
