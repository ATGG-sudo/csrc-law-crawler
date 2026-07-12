"""Output path helpers for crawler artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from config import (
    CANONICAL_SUBDIR,
    OUTPUT_DIR,
    RAW_SUBDIR,
    REPORTS_SUBDIR,
    WORK_SUBDIR,
)

CHECKPOINT_NAME = "checkpoint.json"
MANIFEST_NAME = "manifest.json"
REVISIONS_NAME = "revisions.json"
RELATED_LAWS_NAME = "related_laws.json"
CASES_NAME = "cases.json"
COVERAGE_GAPS_NAME = "coverage_gaps.json"
SOURCE_MATCHES_NAME = "source_matches.json"
CATALOG_RELATIONS_NAME = "catalog_relations.json"
REVISION_EVIDENCE_CACHE_SUBDIR = "revision_evidence_cache"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def raw_dir() -> Path:
    return OUTPUT_DIR / RAW_SUBDIR


def output_dir() -> Path:
    return OUTPUT_DIR


def relative_to_output(path: Path) -> str:
    return str(path.relative_to(OUTPUT_DIR))


def output_path(relative: str | Path) -> Path:
    return OUTPUT_DIR / relative


def work_dir() -> Path:
    return OUTPUT_DIR / WORK_SUBDIR


def canonical_dir() -> Path:
    return OUTPUT_DIR / CANONICAL_SUBDIR


def reports_dir() -> Path:
    return OUTPUT_DIR / REPORTS_SUBDIR


def laws_dir() -> Path:
    return raw_dir() / "neris" / "laws"


def writs_dir() -> Path:
    return raw_dir() / "neris" / "writs"


def relations_dir() -> Path:
    return work_dir() / "relations"


def sources_dir() -> Path:
    return raw_dir()


def amac_sources_dir() -> Path:
    return raw_dir() / "amac" / "records"


def catalog_dir() -> Path:
    return work_dir() / "catalog"


def catalog_laws_dir() -> Path:
    return catalog_dir() / "laws"


def catalog_normalized_dir() -> Path:
    return canonical_dir() / "json"


def catalog_markdown_dir() -> Path:
    return canonical_dir() / "markdown"


def checkpoint_path() -> Path:
    return work_dir() / "checkpoints" / CHECKPOINT_NAME


def manifest_path() -> Path:
    return raw_dir() / "neris" / MANIFEST_NAME


def revisions_path() -> Path:
    return relations_dir() / REVISIONS_NAME


def related_laws_path() -> Path:
    return relations_dir() / RELATED_LAWS_NAME


def cases_path() -> Path:
    return relations_dir() / CASES_NAME


def coverage_gaps_path() -> Path:
    return reports_dir() / COVERAGE_GAPS_NAME


def source_matches_path() -> Path:
    return canonical_dir() / "indexes" / "source_map.json"


def catalog_relations_path() -> Path:
    return relations_dir() / CATALOG_RELATIONS_NAME


def revision_evidence_cache_dir() -> Path:
    return raw_dir() / "neris" / "revision_evidence"


def revision_evidence_cache_path(law_id: str) -> Path:
    return revision_evidence_cache_dir() / f"{law_id}.json"


def attachment_index_dir() -> Path:
    return raw_dir() / "neris" / "attachment_index"


def attachment_index_path(law_id: str) -> Path:
    return attachment_index_dir() / f"{law_id}.json"


def reg_file_path(law_id: str) -> Path:
    return laws_dir() / f"reg_{law_id}.json"


def writ_file_path(writ_id: str) -> Path:
    return writs_dir() / f"writ_{writ_id}.json"


__all__ = [
    "CASES_NAME",
    "CATALOG_RELATIONS_NAME",
    "CHECKPOINT_NAME",
    "COVERAGE_GAPS_NAME",
    "MANIFEST_NAME",
    "RELATED_LAWS_NAME",
    "REVISION_EVIDENCE_CACHE_SUBDIR",
    "REVISIONS_NAME",
    "SOURCE_MATCHES_NAME",
    "amac_sources_dir",
    "attachment_index_dir",
    "attachment_index_path",
    "canonical_dir",
    "cases_path",
    "catalog_dir",
    "catalog_laws_dir",
    "catalog_markdown_dir",
    "catalog_normalized_dir",
    "catalog_relations_path",
    "checkpoint_path",
    "coverage_gaps_path",
    "laws_dir",
    "manifest_path",
    "output_dir",
    "output_path",
    "raw_dir",
    "reg_file_path",
    "related_laws_path",
    "relations_dir",
    "relative_to_output",
    "reports_dir",
    "revision_evidence_cache_dir",
    "revision_evidence_cache_path",
    "revisions_path",
    "source_matches_path",
    "sources_dir",
    "utc_now_iso",
    "work_dir",
    "writ_file_path",
    "writs_dir",
]
