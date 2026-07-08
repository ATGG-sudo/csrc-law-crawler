"""Shared HTTP policy contracts for source adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from download_utils import DownloadedBytes


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
