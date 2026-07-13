"""Publish complete case-lane SourceRecords through the existing writ store."""

from __future__ import annotations

from pathlib import Path

from csrc_law_crawler.sources.registry import load_registry
from storage import load_json, output_dir, save_json


def publish_case_records(root: Path | None = None) -> dict[str, int]:
    output_root = root or output_dir()
    records_root = output_root / "raw" / "sources" / "records"
    writs_root = output_root / "raw" / "neris" / "writs"
    discovered = 0
    written = 0
    skipped = 0
    restricted_endpoints = {
        endpoint["endpoint_id"]
        for endpoint in load_registry()["endpoints"]
        if endpoint["scope_mode"] in {"catalog_filter", "query_exhaustive"}
    }
    for path in sorted(records_root.glob("*/*.json")) if records_root.exists() else []:
        record = load_json(path, {})
        if record.get("material_lane") != "case":
            continue
        discovered += 1
        if record.get("ingest_status") != "complete":
            skipped += 1
            continue
        source = record.get("source") or {}
        if (
            source.get("endpoint_id") in restricted_endpoints
            and source.get("scope_status") != "matched"
        ):
            skipped += 1
            continue
        record_id = str(record.get("source_record_id") or path.stem)
        metadata = dict(record.get("metadata") or {})
        metadata.setdefault("id", record_id)
        metadata.setdefault("name", metadata.get("title") or record_id)
        content = record.get("content") or {}
        writ = {
            "metadata": metadata,
            "body": str(content.get("plain_text") or ""),
            "legal_basis": content.get("legal_basis") or metadata.get("legal_basis") or [],
            "parties": content.get("parties") or metadata.get("parties") or [],
            "source": {
                "source_system": record.get("source_system"),
                "source_record_id": record_id,
                "page_url": source.get("page_url"),
                "source_record_file": str(path.relative_to(output_root)),
            },
        }
        save_json(writs_root / f"writ_{record_id}.json", writ)
        written += 1
    return {"discovered": discovered, "written": written, "skipped": skipped}


__all__ = ["publish_case_records"]
