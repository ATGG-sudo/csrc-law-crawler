"""Pipeline contracts shared by CLI orchestrators."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


STEP_COMPLETE = "complete"
STEP_INCOMPLETE = "incomplete"
STEP_FAILED = "failed"


@dataclass(frozen=True)
class ValidationResult:
    status: str = STEP_COMPLETE
    issues: list[str] = field(default_factory=list)
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STEP_COMPLETE and not self.issues

    @classmethod
    def complete(cls) -> "ValidationResult":
        return cls()

    @classmethod
    def failed(cls, message: str, issues: list[str] | None = None) -> "ValidationResult":
        return cls(status=STEP_FAILED, issues=issues or [], message=message)


@dataclass(frozen=True)
class StepResult:
    stage: str
    status: str
    seen: int = 0
    written: int = 0
    skipped: int = 0
    failed: int = 0
    output_files: list[str] = field(default_factory=list)
    failure_file: str | None = None
    message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "seen": self.seen,
            "written": self.written,
            "skipped": self.skipped,
            "failed": self.failed,
            "output_files": self.output_files,
            "failure_file": self.failure_file,
            "message": self.message,
        }

    @classmethod
    def from_counts(
        cls,
        stage: str,
        counts: dict[str, Any],
        *,
        seen_key: str = "count",
        written_key: str = "written",
        skipped_key: str = "skipped",
        failed_keys: tuple[str, ...] = ("failed",),
        output_files: list[str] | None = None,
        failure_file: str | None = None,
        message: str | None = None,
    ) -> "StepResult":
        failed = sum(int(counts.get(key) or 0) for key in failed_keys)
        status = str(counts.get("status") or "")
        if status not in {STEP_COMPLETE, STEP_INCOMPLETE, STEP_FAILED}:
            status = STEP_INCOMPLETE if failed else STEP_COMPLETE
        if failed and status == STEP_COMPLETE:
            status = STEP_INCOMPLETE
        return cls(
            stage=stage,
            status=status,
            seen=int(counts.get(seen_key) or counts.get("total") or 0),
            written=int(counts.get(written_key) or counts.get("fetched") or 0),
            skipped=int(counts.get(skipped_key) or counts.get("cached") or 0),
            failed=failed,
            output_files=output_files or [],
            failure_file=failure_file,
            message=message,
        )

    @classmethod
    def failed_result(cls, stage: str, exc: BaseException) -> "StepResult":
        return cls(
            stage=stage,
            status=STEP_FAILED,
            failed=1,
            message=f"{type(exc).__name__}: {exc}",
        )


@dataclass(frozen=True)
class PipelineStep:
    name: str
    run: Callable[[], StepResult]
    precondition: Callable[[], ValidationResult] | None = None
    validate: Callable[[StepResult], ValidationResult] | None = None


@dataclass(frozen=True)
class PipelineRun:
    status: str
    items: list[StepResult]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "items": [item.as_dict() for item in self.items],
        }


class PipelineHalted(RuntimeError):
    def __init__(self, result: StepResult) -> None:
        super().__init__(f"{result.stage} 未完成: {result.message or result.failed}")
        self.result = result


class PipelineRunner:
    def __init__(
        self,
        *,
        allow_incomplete: bool = False,
        on_update: Callable[[list[StepResult]], None] | None = None,
    ) -> None:
        self.allow_incomplete = allow_incomplete
        self.on_update = on_update

    def run(self, steps: list[PipelineStep]) -> PipelineRun:
        results: list[StepResult] = []
        for step in steps:
            result = self._run_step(step)
            results.append(result)
            if self.on_update:
                self.on_update(results)
            if result.status != STEP_COMPLETE and not self.allow_incomplete:
                raise PipelineHalted(result)
        status = (
            STEP_COMPLETE
            if all(item.status == STEP_COMPLETE for item in results)
            else STEP_INCOMPLETE
        )
        return PipelineRun(status=status, items=results)

    def _run_step(self, step: PipelineStep) -> StepResult:
        precondition = step.precondition() if step.precondition else ValidationResult.complete()
        if not precondition.ok:
            return StepResult(
                stage=step.name,
                status=precondition.status,
                failed=len(precondition.issues) or 1,
                message=precondition.message or "; ".join(precondition.issues),
            )
        try:
            result = step.run()
        except Exception as exc:
            return StepResult.failed_result(step.name, exc)
        if step.validate is None:
            return result
        validation = step.validate(result)
        if validation.ok:
            return result
        return StepResult(
            stage=step.name,
            status=validation.status,
            seen=result.seen,
            written=result.written,
            skipped=result.skipped,
            failed=len(validation.issues) or result.failed or 1,
            output_files=result.output_files,
            failure_file=result.failure_file,
            message=validation.message or "; ".join(validation.issues),
        )
