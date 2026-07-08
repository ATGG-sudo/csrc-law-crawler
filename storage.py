"""路径、checkpoint 与 JSON 读写。"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

from config import (
    AMAC_VERIFY_TLS,
    BATCH_PAUSE_MAX,
    BATCH_PAUSE_MIN,
    BATCH_SIZE,
    CANONICAL_SUBDIR,
    DELAY_MAX,
    DELAY_MIN,
    MAX_DOWNLOAD_BYTES,
    MAX_RETRIES,
    OUTPUT_DIR,
    RAW_SUBDIR,
    REPORTS_SUBDIR,
    RETRY_BACKOFF_BASE,
    WORK_SUBDIR,
    WORKERS,
)
from failure_taxonomy import FailureReason
from runtime import RunContext

CHECKPOINT_NAME = "checkpoint.json"
MANIFEST_NAME = "manifest.json"
REVISIONS_NAME = "revisions.json"
RELATED_LAWS_NAME = "related_laws.json"
CASES_NAME = "cases.json"
COVERAGE_GAPS_NAME = "coverage_gaps.json"
SOURCE_MATCHES_NAME = "source_matches.json"
CATALOG_RELATIONS_NAME = "catalog_relations.json"
REVISION_EVIDENCE_CACHE_SUBDIR = "revision_evidence_cache"
LOCK_NAME = ".csrc-law-crawler.lock"
_LOCK_DEPTH = 0
GLOBAL_CLI_OPTIONS = {
    "--batch-pause-max",
    "--batch-pause-min",
    "--batch-size",
    "--config",
    "--delay-max",
    "--delay-min",
    "--max-download-bytes",
    "--max-retries",
    "--output-root",
    "--retry-backoff-base",
    "--workers",
}


def strip_global_cli_options(argv: list[str]) -> list[str]:
    result: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg in GLOBAL_CLI_OPTIONS:
            skip_next = True
            continue
        if any(arg.startswith(f"{option}=") for option in GLOBAL_CLI_OPTIONS):
            continue
        result.append(arg)
    return result


def _output_root_resolved() -> Path:
    return OUTPUT_DIR.resolve(strict=False)


def _path_requires_lock(path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(_output_root_resolved())
    except ValueError:
        return False
    return True


@contextmanager
def acquire_output_lock(reason: str = "write") -> Iterator[None]:
    """Acquire an exclusive advisory lock for the configured output directory."""
    global _LOCK_DEPTH
    if _LOCK_DEPTH > 0:
        _LOCK_DEPTH += 1
        try:
            yield
        finally:
            _LOCK_DEPTH -= 1
        return

    root = output_dir()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / LOCK_NAME
    if fcntl is None:
        marker_path = lock_path.with_suffix(lock_path.suffix + ".exclusive")
        try:
            fd = os.open(str(marker_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RuntimeError(
                f"输出目录正在被另一个进程使用，不能并发写入: {root}"
            ) from exc
        _LOCK_DEPTH = 1
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as marker:
                marker.write(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "reason": reason,
                            "locked_at": utc_now_iso(),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                marker.flush()
            yield
        finally:
            _LOCK_DEPTH = 0
            marker_path.unlink(missing_ok=True)
        return

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"输出目录正在被另一个进程使用，不能并发写入: {root}"
            ) from exc
        _LOCK_DEPTH = 1
        try:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "reason": reason,
                        "locked_at": utc_now_iso(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            lock_file.flush()
            yield
        finally:
            _LOCK_DEPTH = 0
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def run_with_output_lock(main: Any, reason: str) -> int:
    if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        return int(main())
    original_argv = sys.argv[:]
    sys.argv = [sys.argv[0], *strip_global_cli_options(sys.argv[1:])]
    try:
        with acquire_output_lock(reason):
            return _run_with_context(main, reason, original_argv)
    finally:
        sys.argv = original_argv


def _run_with_context(main: Any, reason: str, original_argv: list[str]) -> int:
    context = RunContext.create(
        runs_root=reports_dir() / "runs",
        stage=reason,
        argv=original_argv[1:],
        settings={
            "output_root": str(OUTPUT_DIR),
            "delay_min": DELAY_MIN,
            "delay_max": DELAY_MAX,
            "batch_size": BATCH_SIZE,
            "batch_pause_min": BATCH_PAUSE_MIN,
            "batch_pause_max": BATCH_PAUSE_MAX,
            "max_retries": MAX_RETRIES,
            "retry_backoff_base": RETRY_BACKOFF_BASE,
            "max_download_bytes": MAX_DOWNLOAD_BYTES,
            "amac_verify_tls": AMAC_VERIFY_TLS,
            "workers": WORKERS,
            "clean_requested": any(
                arg in {"--clean", "--cleanup"} for arg in original_argv[1:]
            ),
        },
    )
    try:
        exit_code = int(main())
    except BaseException as exc:
        context.failure(
            FailureReason.UNCAUGHT_EXCEPTION,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        context.finish(exit_code=1)
        raise
    if exit_code != 0:
        context.failure(FailureReason.NONZERO_EXIT, exit_code=exit_code)
    context.finish(exit_code=exit_code)
    return exit_code


def run_with_context(main: Any, reason: str) -> int:
    if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        return int(main())
    original_argv = sys.argv[:]
    sys.argv = [sys.argv[0], *strip_global_cli_options(sys.argv[1:])]
    try:
        return _run_with_context(main, reason, original_argv)
    finally:
        sys.argv = original_argv


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


def iter_reg_law_files(limit: int | None = None) -> list[Path]:
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


def iter_amac_source_files() -> list[Path]:
    manifest_files = listed_output_files(
        amac_sources_dir().parent / MANIFEST_NAME,
        field="file",
        fallback_dir=amac_sources_dir(),
        pattern="amac_*.json",
    )
    return sorted(set(manifest_files) | set(amac_sources_dir().glob("amac_*.json")))


def iter_writ_files(limit: int | None = None) -> list[Path]:
    checkpoint = load_checkpoint()
    writ_ids = checkpoint.get("pass4", {}).get("completed_writ_ids") or checkpoint.get(
        "completed_ids", {}
    ).get("writs", [])
    paths: list[Path] = []
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


def _save_json_unlocked(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def save_json(path: Path, data: Any) -> None:
    if _LOCK_DEPTH == 0 and _path_requires_lock(path):
        with acquire_output_lock(f"write:{path.name}"):
            _save_json_unlocked(path, data)
        return
    _save_json_unlocked(path, data)


def append_jsonl(path: Path, item: Any) -> None:
    if _LOCK_DEPTH == 0 and _path_requires_lock(path):
        with acquire_output_lock(f"append:{path.name}"):
            append_jsonl(path, item)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


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
    return [f.stem.removeprefix("reg_") for f in iter_reg_law_files(limit)]


def load_reg_metadata(law_id: str) -> dict[str, Any] | None:
    path = reg_file_path(law_id)
    if not path.exists():
        return None
    data = load_json(path, {})
    return data.get("metadata") or None


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
