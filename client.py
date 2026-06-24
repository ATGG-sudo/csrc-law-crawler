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


class HumanLikeClient:
    def __init__(
        self,
        *,
        delay_min: float = DELAY_MIN,
        delay_max: float = DELAY_MAX,
        batch_size: int = BATCH_SIZE,
        batch_pause_min: float = BATCH_PAUSE_MIN,
        batch_pause_max: float = BATCH_PAUSE_MAX,
    ) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Origin": "https://neris.csrc.gov.cn",
                "Referer": f"{BASE_URL}",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
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
            print(f"  [休息 {pause:.1f}s，已请求 {self._request_count} 次]")
            time.sleep(pause)

    def _is_blocked(self, response: requests.Response) -> bool:
        text = response.text[:500]
        blocked_markers = ("WAF", "请求已中断", "504 Gateway", "502 Bad Gateway")
        return response.status_code >= 500 or any(m in text for m in blocked_markers)

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
                response = self.session.get(url, timeout=60, headers=headers)
                self._request_count += 1

                if self._is_blocked(response):
                    wait = RETRY_BACKOFF_BASE * attempt + random.uniform(2, 6)
                    print(f"  [拦截/超时，{wait:.1f}s 后重试 {attempt}/{MAX_RETRIES}]")
                    time.sleep(wait)
                    continue

                response.raise_for_status()
                response.encoding = response.apparent_encoding or "utf-8"
                return response.text

            except requests.RequestException as exc:
                last_error = exc
                wait = RETRY_BACKOFF_BASE * attempt + random.uniform(1, 4)
                print(f"  [错误 {exc!r}，{wait:.1f}s 后重试 {attempt}/{MAX_RETRIES}]")
                time.sleep(wait)

        raise RuntimeError(f"请求失败: {url}") from last_error

    def get_binary(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[bytes, str]:
        """GET binary content with the same retry/backoff policy as API calls."""
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            self._human_pause()
            self._maybe_batch_pause()
            try:
                response = self.session.get(
                    url,
                    timeout=60,
                    headers=headers,
                )
                self._request_count += 1

                if self._is_blocked(response):
                    wait = RETRY_BACKOFF_BASE * attempt + random.uniform(2, 6)
                    print(f"  [拦截/超时，{wait:.1f}s 后重试 {attempt}/{MAX_RETRIES}]")
                    time.sleep(wait)
                    continue

                response.raise_for_status()
                data = response.content
                if not data:
                    raise RuntimeError("empty attachment response")
                content_type = (
                    response.headers.get("Content-Type") or ""
                ).split(";")[0].strip().lower()
                return data, content_type

            except requests.RequestException as exc:
                last_error = exc
                wait = RETRY_BACKOFF_BASE * attempt + random.uniform(1, 4)
                print(f"  [错误 {exc!r}，{wait:.1f}s 后重试 {attempt}/{MAX_RETRIES}]")
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
                    timeout=60,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                self._request_count += 1

                if self._is_blocked(response):
                    wait = RETRY_BACKOFF_BASE * attempt + random.uniform(2, 6)
                    print(f"  [拦截/超时，{wait:.1f}s 后重试 {attempt}/{MAX_RETRIES}]")
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
                print(f"  [错误 {exc!r}，{wait:.1f}s 后重试 {attempt}/{MAX_RETRIES}]")
                time.sleep(wait)

        raise RuntimeError(f"请求失败: {url}") from last_error
