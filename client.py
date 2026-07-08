"""带限速与重试的 HTTP 客户端。"""

from __future__ import annotations

import random
import time
from typing import Any

import requests

from config import (
    BASE_URL,
    BATCH_PAUSE_MAX,
    BATCH_PAUSE_MIN,
    BATCH_SIZE,
    DELAY_MAX,
    DELAY_MIN,
    MAX_RETRIES,
    RETRY_BACKOFF_BASE,
    USER_AGENT,
)
from download_utils import DownloadedBytes, RetryableContentError, read_binary_response
from failure_taxonomy import FailureReason
from http_policy import HTTPPolicy
from runtime import log_event, log_metric


class HumanLikeClient:
    source_name = "neris"

    def __init__(
        self,
        *,
        delay_min: float = DELAY_MIN,
        delay_max: float = DELAY_MAX,
        batch_size: int = BATCH_SIZE,
        batch_pause_min: float = BATCH_PAUSE_MIN,
        batch_pause_max: float = BATCH_PAUSE_MAX,
    ) -> None:
        self.policy = HTTPPolicy(
            source_name=self.source_name,
            base_url=BASE_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Origin": "https://neris.csrc.gov.cn",
                "Referer": f"{BASE_URL}",
                "X-Requested-With": "XMLHttpRequest",
            },
            blocked_markers=("WAF", "请求已中断", "504 Gateway", "502 Bad Gateway"),
        )
        self.session = requests.Session()
        self.session.headers.update(self.policy.headers)
        self._request_count = 0
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.batch_size = batch_size
        self.batch_pause_min = batch_pause_min
        self.batch_pause_max = batch_pause_max

    def _human_pause(self) -> None:
        if self.delay_max > 0:
            time.sleep(random.uniform(self.delay_min, self.delay_max))

    def _maybe_batch_pause(self) -> None:
        if (
            self.batch_size > 0
            and self._request_count > 0
            and self._request_count % self.batch_size == 0
        ):
            pause = random.uniform(self.batch_pause_min, self.batch_pause_max)
            log_event(
                "http_batch_pause",
                message=f"  [休息 {pause:.1f}s，已请求 {self._request_count} 次]",
                request_count=self._request_count,
                pause_seconds=round(pause, 3),
            )
            time.sleep(pause)

    def _is_blocked(self, response: requests.Response) -> bool:
        text = response.text[:500]
        return response.status_code >= 500 or any(
            marker in text for marker in self.policy.blocked_markers
        )

    def _record_response(
        self,
        method: str,
        url: str,
        response: requests.Response,
    ) -> None:
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
        )

    def get_html(self, path: str, *, referer: str | None = None) -> str:
        """GET 详情页 HTML（执法文书正文在服务端渲染页面中）。"""
        url = path if path.startswith("http") else f"{BASE_URL}{path.lstrip('/')}"
        last_error: Exception | None = None
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": referer or f"{BASE_URL}",
        }

        for attempt in range(1, MAX_RETRIES + 1):
            self._human_pause()
            self._maybe_batch_pause()
            try:
                response = self.session.get(
                    url,
                    timeout=self.policy.timeout_seconds,
                    headers=headers,
                )
                self._request_count += 1
                self._record_response("GET", url, response)

                if self._is_blocked(response):
                    wait = RETRY_BACKOFF_BASE * attempt + random.uniform(2, 6)
                    log_event(
                        "http_retry",
                        level="WARNING",
                        message=(
                            f"  [拦截/超时，{wait:.1f}s 后重试 "
                            f"{attempt}/{MAX_RETRIES}]"
                        ),
                        url=url,
                        attempt=attempt,
                        max_retries=MAX_RETRIES,
                        wait_seconds=round(wait, 3),
                        reason=FailureReason.NETWORK_BLOCKED,
                    )
                    log_metric(
                        "http_retries_total",
                        source=self.source_name,
                        reason=FailureReason.NETWORK_BLOCKED,
                    )
                    time.sleep(wait)
                    continue

                response.raise_for_status()
                response.encoding = response.apparent_encoding or "utf-8"
                return response.text

            except requests.RequestException as exc:
                last_error = exc
                wait = RETRY_BACKOFF_BASE * attempt + random.uniform(1, 4)
                log_event(
                    "http_retry",
                    level="WARNING",
                    message=f"  [错误 {exc!r}，{wait:.1f}s 后重试 {attempt}/{MAX_RETRIES}]",
                    url=url,
                    attempt=attempt,
                    max_retries=MAX_RETRIES,
                    wait_seconds=round(wait, 3),
                    reason=FailureReason.NETWORK_REQUEST_EXCEPTION,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                log_metric(
                    "http_retries_total",
                    source=self.source_name,
                    reason=FailureReason.NETWORK_REQUEST_EXCEPTION,
                )
                time.sleep(wait)

        raise RuntimeError(f"请求失败: {url}") from last_error

    def get_binary(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[bytes, str]:
        """GET binary content with the same retry/backoff policy as API calls."""
        payload = self.get_binary_payload(url, headers=headers)
        return payload.data, payload.content_type

    def get_binary_payload(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> DownloadedBytes:
        """GET binary content and return metadata calculated while streaming."""
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            self._human_pause()
            self._maybe_batch_pause()
            try:
                response = self.session.get(
                    url,
                    timeout=self.policy.timeout_seconds,
                    headers=headers,
                    stream=True,
                )
                self._request_count += 1
                self._record_response("GET", url, response)

                if self._is_blocked(response):
                    wait = RETRY_BACKOFF_BASE * attempt + random.uniform(2, 6)
                    log_event(
                        "http_retry",
                        level="WARNING",
                        message=(
                            f"  [拦截/超时，{wait:.1f}s 后重试 "
                            f"{attempt}/{MAX_RETRIES}]"
                        ),
                        url=url,
                        attempt=attempt,
                        max_retries=MAX_RETRIES,
                        wait_seconds=round(wait, 3),
                        reason=FailureReason.NETWORK_BLOCKED,
                    )
                    log_metric(
                        "http_retries_total",
                        source=self.source_name,
                        reason=FailureReason.NETWORK_BLOCKED,
                    )
                    time.sleep(wait)
                    continue

                response.raise_for_status()
                payload = read_binary_response(response)
                log_metric(
                    "download_bytes_total",
                    source=self.source_name,
                    amount=payload.size_bytes,
                )
                return payload

            except (requests.RequestException, RetryableContentError) as exc:
                last_error = exc
                wait = RETRY_BACKOFF_BASE * attempt + random.uniform(1, 4)
                log_event(
                    "http_retry",
                    level="WARNING",
                    message=f"  [错误 {exc!r}，{wait:.1f}s 后重试 {attempt}/{MAX_RETRIES}]",
                    url=url,
                    attempt=attempt,
                    max_retries=MAX_RETRIES,
                    wait_seconds=round(wait, 3),
                    reason=(
                        exc.reason
                        if isinstance(exc, RetryableContentError)
                        else FailureReason.NETWORK_REQUEST_EXCEPTION
                    ),
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                log_metric(
                    "http_retries_total",
                    source=self.source_name,
                    reason=(
                        exc.reason
                        if isinstance(exc, RetryableContentError)
                        else FailureReason.NETWORK_REQUEST_EXCEPTION
                    ),
                )
                time.sleep(wait)

        raise RuntimeError(f"请求失败: {url}") from last_error

    def post_json(
        self,
        path: str,
        data: dict[str, Any],
        *,
        require_success: bool = True,
    ) -> dict[str, Any]:
        url = f"{BASE_URL}{path.lstrip('/')}"
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            self._human_pause()
            self._maybe_batch_pause()
            try:
                response = self.session.post(
                    url,
                    data=data,
                    timeout=self.policy.timeout_seconds,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                self._request_count += 1
                self._record_response("POST", url, response)

                if self._is_blocked(response):
                    wait = RETRY_BACKOFF_BASE * attempt + random.uniform(2, 6)
                    log_event(
                        "http_retry",
                        level="WARNING",
                        message=(
                            f"  [拦截/超时，{wait:.1f}s 后重试 "
                            f"{attempt}/{MAX_RETRIES}]"
                        ),
                        url=url,
                        attempt=attempt,
                        max_retries=MAX_RETRIES,
                        wait_seconds=round(wait, 3),
                        reason=FailureReason.NETWORK_BLOCKED,
                    )
                    log_metric(
                        "http_retries_total",
                        source=self.source_name,
                        reason=FailureReason.NETWORK_BLOCKED,
                    )
                    time.sleep(wait)
                    continue

                response.raise_for_status()
                payload = response.json()
                if (
                    require_success
                    and isinstance(payload, dict)
                    and payload.get("success") is False
                ):
                    raise ValueError(f"API success=false: {path} {data}")
                return payload

            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                wait = RETRY_BACKOFF_BASE * attempt + random.uniform(1, 4)
                log_event(
                    "http_retry",
                    level="WARNING",
                    message=f"  [错误 {exc!r}，{wait:.1f}s 后重试 {attempt}/{MAX_RETRIES}]",
                    url=url,
                    attempt=attempt,
                    max_retries=MAX_RETRIES,
                    wait_seconds=round(wait, 3),
                    reason=(
                        FailureReason.PARSE_SCHEMA_MISMATCH
                        if isinstance(exc, ValueError)
                        else FailureReason.NETWORK_REQUEST_EXCEPTION
                    ),
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                log_metric(
                    "http_retries_total",
                    source=self.source_name,
                    reason=(
                        FailureReason.PARSE_SCHEMA_MISMATCH
                        if isinstance(exc, ValueError)
                        else FailureReason.NETWORK_REQUEST_EXCEPTION
                    ),
                )
                time.sleep(wait)

        raise RuntimeError(f"请求失败: {url}") from last_error
