"""Checkpoint and manifest-backed file iteration helpers."""

from __future__ import annotations

from typing import Any

from .io import listed_output_files, load_json, save_json
from .paths import (
    MANIFEST_NAME,
    amac_sources_dir,
    checkpoint_path,
    laws_dir,
    manifest_path,
    reg_file_path,
    utc_now_iso,
    writ_file_path,
    writs_dir,
)


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


def iter_reg_law_files(limit: int | None = None) -> list[Any]:
    files = listed_output_files(
        manifest_path(),
        field="file",
        fallback_dir=laws_dir(),
        pattern="reg_*.json",
    )
    files = sorted(files)
    if limit is not None:
        files = files[:limit]
    return files


def iter_amac_source_files() -> list[Any]:
    manifest_files = listed_output_files(
        amac_sources_dir().parent / MANIFEST_NAME,
        field="file",
        fallback_dir=amac_sources_dir(),
        pattern="amac_*.json",
    )
    return sorted(set(manifest_files) | set(amac_sources_dir().glob("amac_*.json")))


def iter_writ_files(limit: int | None = None) -> list[Any]:
    checkpoint = load_checkpoint()
    writ_ids = checkpoint.get("pass4", {}).get("completed_writ_ids") or checkpoint.get(
        "completed_ids", {}
    ).get("writs", [])
    paths = []
    for writ_id in writ_ids:
        path = writ_file_path(str(writ_id))
        if not path.exists():
            paths = []
            break
        paths.append(path)
    if not paths:
        paths = sorted(writs_dir().glob("writ_*.json"))
    else:
        paths = sorted(paths)
    if limit is not None:
        paths = paths[:limit]
    return paths


def iter_reg_law_ids(limit: int | None = None) -> list[str]:
    return [f.stem.removeprefix("reg_") for f in iter_reg_law_files(limit)]


def load_reg_metadata(law_id: str) -> dict[str, Any] | None:
    path = reg_file_path(law_id)
    if not path.exists():
        return None
    data = load_json(path, {})
    return data.get("metadata") or None


__all__ = [
    "iter_amac_source_files",
    "iter_reg_law_files",
    "iter_reg_law_ids",
    "iter_writ_files",
    "load_checkpoint",
    "load_reg_metadata",
    "save_checkpoint",
]
