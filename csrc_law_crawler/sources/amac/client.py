"""HTTP client for the AMAC source adapter."""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
import urllib3

from config import (
    AMAC_BASE_URL,
    AMAC_VERIFY_TLS,
    MAX_RETRIES,
    RETRY_BACKOFF_BASE,
    USER_AGENT,
)
from download_utils import DownloadedBytes, RetryableContentError, read_binary_response
from failure_taxonomy import FailureReason
from http_policy import HTTPPolicy
from runtime import log_event, log_metric


class AmacClient:
    source_name = "amac"
    fg_ca_bundle = Path(__file__).with_name("fg_ca_bundle.pem")

    def __init__(
        self,
        *,
        delay_min: float = 0.25,
        delay_max: float = 0.7,
        verify_tls: bool = AMAC_VERIFY_TLS,
    ) -> None:
        self.policy = HTTPPolicy(
            source_name=self.source_name,
            base_url=AMAC_BASE_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": AMAC_BASE_URL,
            },
            verify_tls=verify_tls,
            blocked_markers=("WAF", "请求已中断", "502 Bad Gateway", "504 Gateway"),
        )
        self.session = requests.Session()
        self.session.headers.update(self.policy.headers)
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.verify_tls = verify_tls

    def _verify_for_url(self, url: str) -> bool | str:
        if self.verify_tls and urlsplit(url).hostname == "fg.amac.org.cn":
            return str(self.fg_ca_bundle)
        return self.verify_tls

    def _pause(self) -> None:
        if self.delay_max > 0:
            time.sleep(random.uniform(self.delay_min, self.delay_max))

    def _is_blocked(self, response: requests.Response, *, inspect_body: bool) -> bool:
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 500:
            return True
        if not inspect_body:
            return False
        text = str(getattr(response, "text", ""))[:500]
        return any(marker in text for marker in self.policy.blocked_markers)

    def _record_response(self, method: str, url: str, response: requests.Response) -> None:
        status = getattr(response, "status_code", None)
        log_metric(
            "http_requests_total",
            source=self.source_name,
            method=method,
            status=status,
        )
        log_event(
            "http_request",
            source=self.source_name,
            method=method,
            url=url,
            status=status,
            verify_tls=self.verify_tls,
        )

    def _record_retry(
        self,
        *,
        url: str,
        attempt: int,
        wait: float,
        reason: FailureReason,
        exc: BaseException | None = None,
    ) -> None:
        fields: dict[str, Any] = {}
        summary = "拦截/超时" if exc is None else f"错误 {exc!r}"
        if exc is not None:
            fields["error_type"] = type(exc).__name__
            fields["error_message"] = str(exc)
        log_event(
            "http_retry",
            level="WARNING",
            message=f"  [{summary}，{wait:.1f}s 后重试 {attempt}/{MAX_RETRIES}]",
            url=url,
            attempt=attempt,
            max_retries=MAX_RETRIES,
            wait_seconds=round(wait, 3),
            reason=reason,
            **fields,
        )
        log_metric("http_retries_total", source=self.source_name, reason=reason)

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        last_error: Exception | None = None
        stream = bool(kwargs.get("stream"))
        if not self.verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        for attempt in range(1, MAX_RETRIES + 1):
            self._pause()
            try:
                response = self.session.get(
                    url,
                    timeout=self.policy.timeout_seconds,
                    verify=self._verify_for_url(url),
                    **kwargs,
                )
                self._record_response("GET", url, response)
                if self._is_blocked(response, inspect_body=not stream):
                    last_error = requests.HTTPError(
                        f"retryable HTTP status {response.status_code}",
                        response=response,
                    )
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
                    wait = RETRY_BACKOFF_BASE * attempt + random.uniform(1, 4)
                    if attempt < MAX_RETRIES:
                        self._record_retry(
                            url=url,
                            attempt=attempt,
                            wait=wait,
                            reason=FailureReason.NETWORK_BLOCKED,
                        )
                        time.sleep(wait)
                    continue
                response.raise_for_status()
                return response
            except requests.HTTPError as exc:
                status = int(getattr(exc.response, "status_code", 0) or 0)
                if status < 500 and status not in {408, 429}:
                    raise
                last_error = exc
                wait = RETRY_BACKOFF_BASE * attempt + random.uniform(1, 4)
                if attempt < MAX_RETRIES:
                    self._record_retry(
                        url=url,
                        attempt=attempt,
                        wait=wait,
                        reason=FailureReason.HTTP_STATUS_ERROR,
                        exc=exc,
                    )
                    time.sleep(wait)
            except requests.RequestException as exc:
                last_error = exc
                wait = RETRY_BACKOFF_BASE * attempt + random.uniform(1, 4)
                if attempt < MAX_RETRIES:
                    self._record_retry(
                        url=url,
                        attempt=attempt,
                        wait=wait,
                        reason=FailureReason.NETWORK_REQUEST_EXCEPTION,
                        exc=exc,
                    )
                    time.sleep(wait)

        raise RuntimeError(f"请求失败: {url}: {last_error}") from last_error

    def get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        return self.get(url, params=params).json()

    def get_binary_payload(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> DownloadedBytes:
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            response = self.get(
                url,
                headers=headers or {"Accept": "*/*", "Referer": url},
                stream=True,
            )
            try:
                payload = read_binary_response(response)
                log_metric(
                    "download_bytes_total",
                    source=self.source_name,
                    amount=payload.size_bytes,
                )
                return payload
            except RetryableContentError as exc:
                last_error = exc
                wait = RETRY_BACKOFF_BASE * attempt + random.uniform(1, 4)
                if attempt < MAX_RETRIES:
                    self._record_retry(
                        url=url,
                        attempt=attempt,
                        wait=wait,
                        reason=exc.reason,
                        exc=exc,
                    )
                    time.sleep(wait)

        raise RuntimeError(f"请求失败: {url}: {last_error}") from last_error


def crawl_candidate(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility wrapper for the old package-level client export."""
    from .pipeline import crawl_candidate as _crawl_candidate

    return _crawl_candidate(*args, **kwargs)


__all__ = ["AmacClient", "crawl_candidate", "random", "time"]
