"""Shared HTTP policy contracts for source adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from download_utils import DownloadedBytes
from failure_taxonomy import FailureReason
from runtime import log_event, log_metric


@dataclass(frozen=True)
class HTTPPolicy:
    source_name: str
    base_url: str
    headers: dict[str, str] = field(default_factory=dict)
    verify_tls: bool = True
    retryable_statuses: tuple[int, ...] = (502, 503, 504)
    blocked_markers: tuple[str, ...] = ()
    timeout_seconds: int = 60


class SourceClient(Protocol):
    source_name: str
    policy: HTTPPolicy

    def get_binary_payload(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> DownloadedBytes: ...


def record_http_retry(
    *,
    source: str,
    url: str,
    attempt: int,
    max_retries: int,
    wait: float,
    reason: FailureReason,
    exc: BaseException | None = None,
) -> None:
    fields: dict[str, str] = {}
    summary = "拦截/超时" if exc is None else f"错误 {exc!r}"
    if exc is not None:
        fields["error_type"] = type(exc).__name__
        fields["error_message"] = str(exc)
    log_event(
        "http_retry",
        level="WARNING",
        message=f"  [{summary}，{wait:.1f}s 后重试 {attempt}/{max_retries}]",
        url=url,
        attempt=attempt,
        max_retries=max_retries,
        wait_seconds=round(wait, 3),
        reason=reason,
        **fields,
    )
    log_metric("http_retries_total", source=source, reason=reason)
