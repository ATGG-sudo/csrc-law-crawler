"""Shared guarded response readers for binary downloads."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any

from config import MAX_DOWNLOAD_BYTES
from failure_taxonomy import FailureReason


class RetryableContentError(RuntimeError):
    """A syntactically successful response whose content is unusable."""

    def __init__(
        self,
        message: str,
        *,
        reason: FailureReason = FailureReason.CONTENT_EMPTY_RESPONSE,
    ) -> None:
        super().__init__(message)
        self.reason = reason


class DownloadTooLargeError(RuntimeError):
    """The response exceeds the configured download safety limit."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.reason = FailureReason.CONTENT_TOO_LARGE


@dataclass(frozen=True)
class DownloadedBytes:
    data: bytes
    content_type: str
    size_bytes: int
    sha256: str


def content_type_from_response(response: Any) -> str:
    return (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()


def _content_length(response: Any) -> int | None:
    raw = response.headers.get("Content-Length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _iter_response_chunks(response: Any, chunk_size: int) -> Any:
    iter_content = getattr(response, "iter_content", None)
    if callable(iter_content):
        yield from iter_content(chunk_size=chunk_size)
        return
    data = getattr(response, "content", b"")
    if data:
        yield data


def read_binary_response(
    response: Any,
    *,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    chunk_size: int = 64 * 1024,
) -> DownloadedBytes:
    declared_size = _content_length(response)
    if declared_size is not None and declared_size > max_bytes:
        raise DownloadTooLargeError(
            f"response declares {declared_size} bytes, exceeds limit {max_bytes}"
        )

    data = bytearray()
    digest = hashlib.sha256()
    for chunk in _iter_response_chunks(response, chunk_size):
        if not chunk:
            continue
        data.extend(chunk)
        if len(data) > max_bytes:
            raise DownloadTooLargeError(
                f"response exceeded limit {max_bytes} bytes while downloading"
            )
        digest.update(chunk)

    payload = bytes(data)
    if not payload:
        raise RetryableContentError(
            "empty attachment response",
            reason=FailureReason.CONTENT_EMPTY_RESPONSE,
        )

    content_type = content_type_from_response(response)
    prefix = payload[:500].lstrip().lower()
    if content_type == "text/html" and (
        prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html")
    ):
        raise RetryableContentError(
            "attachment endpoint returned an HTML error page",
            reason=FailureReason.CONTENT_HTML_ERROR_PAGE,
        )

    return DownloadedBytes(
        data=payload,
        content_type=content_type,
        size_bytes=len(payload),
        sha256=digest.hexdigest(),
    )
