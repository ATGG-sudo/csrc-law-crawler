"""JSON persistence helpers for crawler artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from typing import Any

from .locking import acquire_output_lock, lock_depth, path_requires_lock
from .paths import output_path


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def listed_output_files(
    manifest_file: Path,
    *,
    field: str,
    fallback_dir: Path,
    pattern: str,
    limit: int | None = None,
) -> list[Path]:
    """Return manifest-listed output files, falling back to a directory scan."""
    manifest = load_json(manifest_file, {})
    paths: list[Path] = []
    for item in manifest.get("items") or []:
        if not isinstance(item, dict):
            paths = []
            break
        value = item.get(field)
        if not value:
            paths = []
            break
        path = output_path(str(value))
        if not path.exists():
            paths = []
            break
        paths.append(path)
    if not paths:
        paths = sorted(fallback_dir.glob(pattern))
    if limit is not None:
        paths = paths[:limit]
    return paths


def _save_json_unlocked(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _save_bytes_unlocked(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def save_json(path: Path, data: Any) -> None:
    if lock_depth() == 0 and path_requires_lock(path):
        with acquire_output_lock(f"write:{path.name}"):
            _save_json_unlocked(path, data)
        return
    _save_json_unlocked(path, data)


def save_bytes(path: Path, data: bytes) -> None:
    if lock_depth() == 0 and path_requires_lock(path):
        with acquire_output_lock(f"write:{path.name}"):
            _save_bytes_unlocked(path, data)
        return
    _save_bytes_unlocked(path, data)


def append_jsonl(path: Path, item: Any) -> None:
    if lock_depth() == 0 and path_requires_lock(path):
        with acquire_output_lock(f"append:{path.name}"):
            append_jsonl(path, item)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def publish_json_bundle(documents: dict[Path, Any]) -> None:
    """Atomically publish a set of JSON files, rolling back on replacement errors."""
    with acquire_output_lock("publish-json-bundle"):
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
                backup_path = backups.get(target)
                if backup_path and backup_path.exists():
                    if target.exists():
                        target.unlink()
                    os.replace(backup_path, target)
            raise
        finally:
            for path in staged.values():
                if path.exists():
                    path.unlink()
            for path in backups.values():
                if path.exists():
                    path.unlink()


def publish_directory_atomic(staged: Path, target: Path) -> None:
    """Replace one generated directory and restore the last-known-good copy on error."""
    if not staged.is_dir():
        raise FileNotFoundError(f"staged directory does not exist: {staged}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if staged.stat().st_dev != target.parent.stat().st_dev:
        raise OSError("staged and target directories must be on the same filesystem")
    backup = target.with_name(target.name + ".publish-backup")
    with acquire_output_lock(f"publish-directory:{target.name}"):
        if backup.exists():
            shutil.rmtree(backup)
        moved_target = False
        try:
            if target.exists():
                os.replace(target, backup)
                moved_target = True
            os.replace(staged, target)
        except BaseException:
            if target.exists():
                shutil.rmtree(target)
            if moved_target and backup.exists():
                os.replace(backup, target)
            raise
        else:
            if backup.exists():
                shutil.rmtree(backup)


__all__ = [
    "append_jsonl",
    "listed_output_files",
    "load_json",
    "publish_directory_atomic",
    "publish_json_bundle",
    "save_bytes",
    "save_json",
]
