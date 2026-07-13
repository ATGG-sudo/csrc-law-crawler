"""Compatibility exports for storage, paths, locks, and JSON helpers."""

from __future__ import annotations

import sys
import types

from csrc_law_crawler.core import locking as _locking
from csrc_law_crawler.core import paths as _paths
from csrc_law_crawler.core.storage import (
    CASES_NAME,
    CATALOG_RELATIONS_NAME,
    CHECKPOINT_NAME,
    COVERAGE_GAPS_NAME,
    FileStore,
    GLOBAL_CLI_OPTIONS,
    LOCK_NAME,
    MANIFEST_NAME,
    RELATED_LAWS_NAME,
    REVISION_EVIDENCE_CACHE_SUBDIR,
    REVISIONS_NAME,
    SOURCE_MATCHES_NAME,
    acquire_output_lock,
    amac_sources_dir,
    append_jsonl,
    attachment_index_dir,
    attachment_index_path,
    canonical_dir,
    cases_path,
    catalog_dir,
    catalog_laws_dir,
    catalog_markdown_dir,
    catalog_normalized_dir,
    catalog_relations_path,
    checkpoint_path,
    coverage_gaps_path,
    iter_amac_source_files,
    iter_reg_law_files,
    iter_reg_law_ids,
    iter_writ_files,
    laws_dir,
    listed_output_files,
    load_checkpoint,
    load_json,
    load_reg_metadata,
    manifest_path,
    output_dir,
    output_path,
    publish_directory_atomic,
    publish_json_bundle,
    raw_dir,
    reg_file_path,
    related_laws_path,
    relations_dir,
    relative_to_output,
    reports_dir,
    revision_evidence_cache_dir,
    revision_evidence_cache_path,
    revisions_path,
    run_with_context,
    run_with_output_lock,
    save_bytes,
    save_checkpoint,
    save_json,
    source_matches_path,
    sources_dir,
    strip_global_cli_options,
    utc_now_iso,
    work_dir,
    writ_file_path,
    writs_dir,
)

OUTPUT_DIR = _paths.OUTPUT_DIR

__all__ = [
    "CASES_NAME",
    "CATALOG_RELATIONS_NAME",
    "CHECKPOINT_NAME",
    "COVERAGE_GAPS_NAME",
    "FileStore",
    "GLOBAL_CLI_OPTIONS",
    "LOCK_NAME",
    "MANIFEST_NAME",
    "OUTPUT_DIR",
    "RELATED_LAWS_NAME",
    "REVISION_EVIDENCE_CACHE_SUBDIR",
    "REVISIONS_NAME",
    "SOURCE_MATCHES_NAME",
    "acquire_output_lock",
    "amac_sources_dir",
    "append_jsonl",
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
    "iter_amac_source_files",
    "iter_reg_law_files",
    "iter_reg_law_ids",
    "iter_writ_files",
    "laws_dir",
    "listed_output_files",
    "load_checkpoint",
    "load_json",
    "load_reg_metadata",
    "manifest_path",
    "output_dir",
    "output_path",
    "publish_directory_atomic",
    "publish_json_bundle",
    "raw_dir",
    "reg_file_path",
    "related_laws_path",
    "relations_dir",
    "relative_to_output",
    "reports_dir",
    "revision_evidence_cache_dir",
    "revision_evidence_cache_path",
    "revisions_path",
    "run_with_context",
    "run_with_output_lock",
    "save_bytes",
    "save_checkpoint",
    "save_json",
    "source_matches_path",
    "sources_dir",
    "strip_global_cli_options",
    "utc_now_iso",
    "work_dir",
    "writ_file_path",
    "writs_dir",
]


def _sync_output_dir(value: object) -> None:
    _paths.OUTPUT_DIR = value  # type: ignore[assignment]
    _locking.OUTPUT_DIR = value  # type: ignore[assignment]


class _StorageModule(types.ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        if name == "OUTPUT_DIR":
            _sync_output_dir(value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _StorageModule
