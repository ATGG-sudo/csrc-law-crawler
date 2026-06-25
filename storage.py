"""路径、checkpoint 与 JSON 读写。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_checkpoint() -> dict[str, Any]:
    return load_json(
        checkpoint_path(),
        {
            "started_at": utc_now_iso(),
            "completed_ids": {"regulations": [], "writs": []},
        },
    )


def save_checkpoint(checkpoint: dict[str, Any]) -> None:
    checkpoint["updated_at"] = utc_now_iso()
    save_json(checkpoint_path(), checkpoint)


def iter_reg_law_ids(limit: int | None = None) -> list[str]:
    files = sorted(laws_dir().glob("reg_*.json"))
    if limit is not None:
        files = files[:limit]
    return [f.stem.removeprefix("reg_") for f in files]


def load_reg_metadata(law_id: str) -> dict[str, Any] | None:
    path = reg_file_path(law_id)
    if not path.exists():
        return None
    data = load_json(path, {})
    return data.get("metadata") or None


def publish_json_bundle(documents: dict[Path, Any]) -> None:
    """Atomically publish a set of JSON files, rolling back on replacement errors."""
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    try:
        for target, data in documents.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            staged_path = target.with_suffix(target.suffix + ".staged")
            save_json(staged_path, data)
            staged[target] = staged_path
        for target in documents:
            backup = target.with_suffix(target.suffix + ".publish-backup")
            if backup.exists():
                backup.unlink()
            if target.exists():
                os.replace(target, backup)
                backups[target] = backup
            os.replace(staged[target], target)
    except BaseException:
        for target in documents:
            if target.exists() and target not in backups:
                target.unlink()
            backup = backups.get(target)
            if backup and backup.exists():
                if target.exists():
                    target.unlink()
                os.replace(backup, target)
        raise
    finally:
        for path in staged.values():
            if path.exists():
                path.unlink()
        for path in backups.values():
            if path.exists():
                path.unlink()
