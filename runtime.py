"""Run-level observability artifacts for CLI executions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any


CURRENT_RUN_CONTEXT: "RunContext | None" = None


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "run"


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


@dataclass
class RunContext:
    run_id: str
    stage: str
    run_dir: Path
    argv: list[str]
    settings: dict[str, Any]
    started_at: str = field(default_factory=utc_now_iso)
    started_monotonic: float = field(default_factory=time.monotonic)
    counters: dict[str, int] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        runs_root: Path,
        stage: str,
        argv: list[str],
        settings: dict[str, Any],
    ) -> "RunContext":
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_stage = _safe_name(stage)
        run_id = f"{stamp}_{safe_stage}_{os.getpid()}"
        context = cls(
            run_id=run_id,
            stage=stage,
            run_dir=runs_root / run_id,
            argv=argv,
            settings=settings,
        )
        context.run_dir.mkdir(parents=True, exist_ok=True)
        context.failures_path.touch()
        context.write_manifest(status="running", exit_code=None)
        context.event("run_started", level="INFO")
        set_current_run_context(context)
        return context

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / "run_manifest.json"

    @property
    def events_path(self) -> Path:
        return self.run_dir / "events.jsonl"

    @property
    def failures_path(self) -> Path:
        return self.run_dir / "failures.jsonl"

    @property
    def metrics_path(self) -> Path:
        return self.run_dir / "metrics.json"

    def write_manifest(self, *, status: str, exit_code: int | None) -> None:
        manifest = {
            "schema_version": 1,
            "run_id": self.run_id,
            "stage": self.stage,
            "status": status,
            "exit_code": exit_code,
            "started_at": self.started_at,
            "updated_at": utc_now_iso(),
            "argv": self.argv,
            "settings": self.settings,
            "artifacts": {
                "events": str(self.events_path.relative_to(self.run_dir)),
                "failures": str(self.failures_path.relative_to(self.run_dir)),
                "metrics": str(self.metrics_path.relative_to(self.run_dir)),
            },
        }
        _write_json(self.manifest_path, manifest)

    def event(self, event: str, *, level: str = "INFO", **fields: Any) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": utc_now_iso(),
            "level": level,
            "run_id": self.run_id,
            "stage": self.stage,
            "event": event,
            **fields,
        }
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def failure(self, reason: str, **fields: Any) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": utc_now_iso(),
            "run_id": self.run_id,
            "stage": self.stage,
            "reason": reason,
            **fields,
        }
        with self.failures_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def metric(self, name: str, *, amount: int = 1, **labels: Any) -> None:
        label_suffix = ",".join(
            f"{key}={labels[key]}" for key in sorted(labels) if labels[key] is not None
        )
        key = f"{name}{{{label_suffix}}}" if label_suffix else name
        self.counters[key] = self.counters.get(key, 0) + amount

    def finish(self, *, exit_code: int) -> None:
        duration_seconds = time.monotonic() - self.started_monotonic
        status = "complete" if exit_code == 0 else "failed"
        _write_json(
            self.metrics_path,
            {
                "schema_version": 1,
                "run_id": self.run_id,
                "stage": self.stage,
                "exit_code": exit_code,
                "duration_seconds": round(duration_seconds, 3),
                "counters": dict(sorted(self.counters.items())),
            },
        )
        self.event(
            "run_finished",
            level="INFO" if exit_code == 0 else "ERROR",
            exit_code=exit_code,
            duration_seconds=round(duration_seconds, 3),
        )
        self.write_manifest(status=status, exit_code=exit_code)
        if CURRENT_RUN_CONTEXT is self:
            set_current_run_context(None)


def set_current_run_context(context: RunContext | None) -> None:
    global CURRENT_RUN_CONTEXT
    CURRENT_RUN_CONTEXT = context


def log_event(
    event: str,
    *,
    level: str = "INFO",
    message: str | None = None,
    stage: str | None = None,
    **fields: Any,
) -> None:
    if message:
        stream = sys.stderr if level in {"ERROR", "WARNING"} else sys.stdout
        stream.write(f"{message}\n")
        stream.flush()
    context = CURRENT_RUN_CONTEXT
    if context is None:
        return
    if stage is None:
        context.event(event, level=level, **fields)
    else:
        original_stage = context.stage
        context.stage = stage
        try:
            context.event(event, level=level, **fields)
        finally:
            context.stage = original_stage


def log_metric(name: str, *, amount: int = 1, **labels: Any) -> None:
    context = CURRENT_RUN_CONTEXT
    if context is not None:
        context.metric(name, amount=amount, **labels)
