#!/usr/bin/env python3
"""Validate the AMAC source layer, source matches, and canonical catalog."""

from __future__ import annotations

import json
import sys

from config import OUTPUT_DIR
from storage import (
    amac_sources_dir,
    catalog_laws_dir,
    catalog_relations_path,
    load_json,
    source_matches_path,
)


def validate_catalog() -> tuple[list[str], dict[str, int]]:
    issues: list[str] = []
    catalog_files = sorted(catalog_laws_dir().glob("law_*.json"))
    entity_ids: set[str] = set()
    source_refs: set[tuple[str, str]] = set()

    for path in catalog_files:
        entity = load_json(path, {})
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
            if local_file and not (OUTPUT_DIR / str(local_file)).exists():
                issues.append(f"{path.name}: missing source file {local_file}")

    amac_ids = {
        str(load_json(path, {}).get("source_record_id") or path.stem)
        for path in amac_sources_dir().glob("amac_*.json")
    }
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
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if issues:
        print("\n问题:")
        for issue in issues[:100]:
            print(f"  - {issue}")
        return 1
    print("\n多源目录校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
