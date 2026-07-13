"""Output locking and command run-context helpers."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sys
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
    DELAY_MAX,
    DELAY_MIN,
    MAX_DOWNLOAD_BYTES,
    MAX_RETRIES,
    OUTPUT_DIR,
    RETRY_BACKOFF_BASE,
    WORKERS,
)
from failure_taxonomy import FailureReason
from runtime import RunContext

from .paths import output_dir, reports_dir, utc_now_iso

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


def output_root_resolved() -> Path:
    return OUTPUT_DIR.resolve(strict=False)


def path_requires_lock(path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(output_root_resolved())
    except ValueError:
        return False
    return True


def lock_depth() -> int:
    return _LOCK_DEPTH


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
    if exit_code not in {0, 2}:
        context.failure(FailureReason.NONZERO_EXIT, exit_code=exit_code)
    context.finish(
        exit_code=exit_code,
        status={0: "complete", 2: "incomplete"}.get(exit_code, "failed"),
    )
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


__all__ = [
    "GLOBAL_CLI_OPTIONS",
    "LOCK_NAME",
    "acquire_output_lock",
    "lock_depth",
    "output_root_resolved",
    "path_requires_lock",
    "run_with_context",
    "run_with_output_lock",
    "strip_global_cli_options",
]
