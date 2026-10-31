"""Polite HTTP client: timeouts, retries with jittered backoff, Retry-After, per-host spacing.

Spring Boot analogy: a RestTemplate/WebClient bean with a retry interceptor and a rate limiter.

Policy
------
* 429 and 5xx responses, timeouts and network errors are retried up to `max_retries` times
  with exponential backoff + jitter. `Retry-After` is honored (and pushes back every thread
  talking to that host). A Retry-After longer than `retry_after_cap_seconds` gives up at once.
* Everything else (200, 404, 401, 3xx ...) is returned untouched; the adapter decides what it
  means. Redirects are never followed automatically.
* Exhausted retries raise `TransientFetchError`.
"""

from __future__ import annotations

import email.utils
import logging
import random
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

import httpx

from jobhunt.config import HttpSettings
from jobhunt.errors import TransientFetchError

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504, 520, 521, 522, 523, 524})


class HttpClient:
    def __init__(
        self,
        settings: HttpSettings,
        user_agent: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._settings = settings
        self.user_agent = user_agent
        self._sleep = sleep
        self._monotonic = monotonic
        self._jitter = jitter
        self._lock = threading.Lock()
        self._next_allowed_at: dict[str, float] = {}
        self._request_count = 0
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "application/json, */*;q=0.5"},
            timeout=httpx.Timeout(settings.timeout_seconds, connect=min(10.0, settings.timeout_seconds)),
            follow_redirects=False,
            transport=transport,
        )

    # -- public API ------------------------------------------------------------------------

    @property
    def request_count(self) -> int:
        """Number of HTTP requests actually sent (retries included)."""
        return self._request_count

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        host = httpx.URL(url).host
        attempts = self._settings.max_retries + 1
        problem = "no attempt made"

        for attempt in range(attempts):
            self._wait_for_slot(host)
            retry_after: float | None = None
            try:
                with self._lock:
                    self._request_count += 1
                response = self._client.get(url, params=params, headers=headers)
            except httpx.TimeoutException:
                problem = "timeout"
            except httpx.TransportError as error:
                problem = f"network error ({type(error).__name__})"
            else:
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    return response
                problem = f"HTTP {response.status_code}"
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                if retry_after is not None:
                    if retry_after > self._settings.retry_after_cap_seconds:
                        raise TransientFetchError(
                            f"{_describe(url)}: {problem}, Retry-After {retry_after:.0f}s exceeds cap"
                        )
                    self._defer_host(host, retry_after)

            if attempt == attempts - 1:
                break
            delay = max(self._backoff_delay(attempt), retry_after or 0.0)
            logger.debug("%s: %s, retry %d/%d in %.1fs", _describe(url), problem, attempt + 1, attempts - 1, delay)
            self._sleep(delay)

        raise TransientFetchError(f"{_describe(url)}: {problem} after {attempts} attempts")

    # -- internals -------------------------------------------------------------------------

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with 'equal jitter': half fixed, half random."""
        ceiling = min(
            self._settings.backoff_max_seconds,
            self._settings.backoff_base_seconds * (2**attempt),
        )
        return ceiling / 2 + self._jitter() * ceiling / 2

    def _interval_for(self, host: str) -> float:
        overrides = self._settings.host_min_interval_seconds
        return overrides.get(host, self._settings.default_min_interval_seconds)

    def _wait_for_slot(self, host: str) -> None:
        """Reserve the next send slot for `host` and sleep (outside the lock) until it opens."""
        interval = self._interval_for(host)
        with self._lock:
            now = self._monotonic()
            slot = max(now, self._next_allowed_at.get(host, 0.0))
            self._next_allowed_at[host] = slot + interval
            wait = slot - now
        if wait > 0:
            self._sleep(wait)

    def _defer_host(self, host: str, seconds: float) -> None:
        with self._lock:
            deadline = self._monotonic() + seconds
            self._next_allowed_at[host] = max(self._next_allowed_at.get(host, 0.0), deadline)


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either delta-seconds or an HTTP-date. Returns seconds, or None."""
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        moment = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())


def _describe(url: str) -> str:
    """URL without its query string, so API keys never reach the logs."""
    parsed = httpx.URL(url)
    return f"{parsed.host}{parsed.path}"
