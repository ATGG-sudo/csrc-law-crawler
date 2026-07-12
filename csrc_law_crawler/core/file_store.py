"""FileStore facade for crawler output IO."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from .io import append_jsonl, load_json, save_json
from .locking import acquire_output_lock
from .paths import output_dir


class FileStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or output_dir()

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def save_json_atomic(self, path: Path, data: Any) -> None:
        save_json(path, data)

    def load_json(self, path: Path, default: Any) -> Any:
        return load_json(path, default)

    def append_jsonl(self, path: Path, item: Any) -> None:
        append_jsonl(path, item)

    def acquire_lock(self, reason: str = "write") -> AbstractContextManager[None]:
        return acquire_output_lock(reason)


__all__ = ["FileStore"]
