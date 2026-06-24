"""路径、checkpoint 与 JSON 读写。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import (
    CATALOG_SUBDIR,
    LAWS_SUBDIR,
    OUTPUT_DIR,
    SOURCES_SUBDIR,
    WRITS_SUBDIR,
)

CHECKPOINT_NAME = "checkpoint.json"
MANIFEST_NAME = "manifest.json"
RELATIONS_SUBDIR = "relations"
REVISIONS_NAME = "revisions.json"
RELATED_LAWS_NAME = "related_laws.json"
CASES_NAME = "cases.json"
COVERAGE_GAPS_NAME = "coverage_gaps.json"
SOURCE_MATCHES_NAME = "source_matches.json"
CATALOG_RELATIONS_NAME = "catalog_relations.json"
REVISION_EVIDENCE_CACHE_SUBDIR = "revision_evidence_cache"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def laws_dir() -> Path:
    return OUTPUT_DIR / LAWS_SUBDIR


def writs_dir() -> Path:
    return OUTPUT_DIR / WRITS_SUBDIR


def relations_dir() -> Path:
    return OUTPUT_DIR / RELATIONS_SUBDIR


def sources_dir() -> Path:
    return OUTPUT_DIR / SOURCES_SUBDIR


def amac_sources_dir() -> Path:
    return sources_dir() / "amac"


def catalog_dir() -> Path:
    return OUTPUT_DIR / CATALOG_SUBDIR


def catalog_laws_dir() -> Path:
    return catalog_dir() / "laws"


def catalog_normalized_dir() -> Path:
    return catalog_dir() / "normalized" / "laws"


def catalog_markdown_dir() -> Path:
    return catalog_dir() / "markdown" / "laws"


def checkpoint_path() -> Path:
    return OUTPUT_DIR / CHECKPOINT_NAME


def manifest_path() -> Path:
    return OUTPUT_DIR / MANIFEST_NAME


def revisions_path() -> Path:
    return relations_dir() / REVISIONS_NAME


def related_laws_path() -> Path:
    return relations_dir() / RELATED_LAWS_NAME


def cases_path() -> Path:
    return relations_dir() / CASES_NAME


def coverage_gaps_path() -> Path:
    return relations_dir() / COVERAGE_GAPS_NAME


def source_matches_path() -> Path:
    return relations_dir() / SOURCE_MATCHES_NAME


def catalog_relations_path() -> Path:
    return relations_dir() / CATALOG_RELATIONS_NAME


def revision_evidence_cache_dir() -> Path:
    return relations_dir() / REVISION_EVIDENCE_CACHE_SUBDIR


def revision_evidence_cache_path(law_id: str) -> Path:
    return revision_evidence_cache_dir() / f"{law_id}.json"


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


def patch_reg_revision_ref(law_id: str, family_key: str) -> None:
    path = reg_file_path(law_id)
    if not path.exists():
        return
    data = load_json(path, {})
    data["revision_ref"] = {
        "family_id": family_key,
        "relations_file": f"{RELATIONS_SUBDIR}/{REVISIONS_NAME}",
    }
    save_json(path, data)


def clear_reg_revision_refs() -> int:
    changed = 0
    for path in sorted(laws_dir().glob("reg_*.json")):
        data = load_json(path, {})
        if "revision_ref" not in data:
            continue
        data.pop("revision_ref", None)
        save_json(path, data)
        changed += 1
    return changed
