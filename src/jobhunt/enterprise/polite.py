"""A deliberately cautious HTTP fetcher for employer career sites.

Rules, in order of importance:
* robots.txt is fetched once per host and obeyed (a 4xx robots.txt means "no rules"; a 5xx or an
  unreachable robots.txt means "do not touch this host tonight").
* one request at a time, at most `requests_per_minute` per host.
* 429 / 5xx are retried with backoff a couple of times; bot protection (a CAPTCHA or challenge
  page, repeated 403, a persistent 429) blocks the host for the rest of the run. We never try to
  get around it, never log in and never send credentials.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.robotparser
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

logger = logging.getLogger(__name__)

_CHALLENGE_MARKERS = ("captcha", "cf-challenge", "just a moment", "access denied", "px-captcha", "are you a robot",
                      "verify you are human", "unusual traffic")
_RETRYABLE = frozenset({429, 500, 502, 503, 504})


class HostBlocked(Exception):
    """The host refused us (robots.txt, bot protection or rate limiting); it is skipped for this run."""

    def __init__(self, host: str, reason: str) -> None:
        super().__init__(f"{host}: {reason}")
        self.host = host
        self.reason = reason


class PoliteFetcher:
    def __init__(
        self,
        user_agent: str,
        *,
        requests_per_minute: float = 20.0,
        timeout: float = 20.0,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._min_interval = 60.0 / max(0.5, requests_per_minute)
        self._max_retries = max_retries
        self._sleep = sleep
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._next_at: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._blocked: dict[str, str] = {}
        self._forbidden_count: dict[str, int] = {}
        self.user_agent = user_agent
        self.request_count = 0
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"},
            timeout=timeout, follow_redirects=False, transport=transport,
        )

    # -- public ----------------------------------------------------------------------------

    @property
    def blocked(self) -> dict[str, str]:
        return dict(self._blocked)

    def close(self) -> None:
        self._client.close()

    def allowed(self, url: str) -> bool:
        """robots.txt verdict for `url`; raises HostBlocked when robots.txt itself cannot be trusted."""
        parts = urlsplit(url)
        host = parts.netloc.lower()
        if host in self._blocked:
            raise HostBlocked(host, self._blocked[host])
        parser = self._robots_for(parts.scheme, host)
        if parser is None:
            return True
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return parser.can_fetch(self.user_agent, path) or parser.can_fetch("*", path)

    def get(self, url: str, *, params: Mapping[str, Any] | None = None, follow: int = 0,
            headers: Mapping[str, str] | None = None) -> httpx.Response:
        """GET (robots-checked). `follow` = how many redirects to follow, each hop re-checked."""
        current = url
        for hop in range(follow + 1):
            response = self._send("GET", current, params=params if hop == 0 else None, headers=headers)
            if follow and response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                current = urljoin(current, response.headers["location"])
                continue
            return response
        return response

    def post_json(self, url: str, payload: Mapping[str, Any]) -> httpx.Response:
        return self._send("POST", url, json=payload, headers={"Content-Type": "application/json"})

    # -- internals -------------------------------------------------------------------------

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        parts = urlsplit(url)
        host = parts.netloc.lower()
        if not self.allowed(url):
            raise HostBlocked(host, f"robots.txt disallows {parts.path}")
        last_status = 0
        for attempt in range(self._max_retries + 1):
            self._wait_slot(host)
            try:
                self.request_count += 1
                response = self._client.request(method, url, **kwargs)
            except httpx.TransportError as error:
                last_status = 0
                if attempt == self._max_retries:
                    raise HostBlocked(host, f"network error ({type(error).__name__})") from error
                self._sleep(2.0 * (attempt + 1))
                continue
            last_status = response.status_code
            if response.status_code == 403:
                count = self._forbidden_count.get(host, 0) + 1
                self._forbidden_count[host] = count
                if count >= 2:
                    self._block(host, "HTTP 403 repeatedly (bot protection?)")
                return response
            if self._looks_like_challenge(response):
                self._block(host, "bot-protection / challenge page")
            if response.status_code in _RETRYABLE and attempt < self._max_retries:
                delay = _retry_after(response) or 5.0 * (2**attempt)
                self._sleep(min(delay, 60.0))
                continue
            if response.status_code == 429:
                self._block(host, "HTTP 429 after retries")
            return response
        raise HostBlocked(host, f"HTTP {last_status} after retries")

    def _block(self, host: str, reason: str) -> None:
        self._blocked[host] = reason
        logger.warning("host %s blocked for this run: %s", host, reason)
        raise HostBlocked(host, reason)

    @staticmethod
    def _looks_like_challenge(response: httpx.Response) -> bool:
        content_type = response.headers.get("content-type", "").lower()
        if response.status_code >= 400 or "json" in content_type:
            return False
        head = response.text[:3000].lower()
        return any(marker in head for marker in _CHALLENGE_MARKERS) and len(head) < 3000

    def _wait_slot(self, host: str) -> None:
        with self._lock:
            now = self._monotonic()
            slot = max(now, self._next_at.get(host, 0.0))
            self._next_at[host] = slot + self._min_interval
            wait = slot - now
        if wait > 0:
            self._sleep(wait)

    def _robots_for(self, scheme: str, host: str) -> urllib.robotparser.RobotFileParser | None:
        if host in self._robots:
            return self._robots[host]
        url = f"{scheme}://{host}/robots.txt"
        parser: urllib.robotparser.RobotFileParser | None
        try:
            self._wait_slot(host)
            self.request_count += 1
            response = self._client.get(url)
        except httpx.TransportError as error:
            self._robots[host] = None
            self._blocked[host] = f"robots.txt unreachable ({type(error).__name__})"
            raise HostBlocked(host, self._blocked[host]) from error
        if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
            try:
                self._wait_slot(host)
                self.request_count += 1
                response = self._client.get(urljoin(url, response.headers["location"]))
            except httpx.TransportError:
                pass
        if response.status_code in (401, 403, 429) or response.status_code >= 500:
            self._robots[host] = None
            self._blocked[host] = f"robots.txt answered HTTP {response.status_code}"
            raise HostBlocked(host, self._blocked[host])
        if response.status_code >= 400 or "html" in response.headers.get("content-type", "").lower():
            parser = None            # no robots.txt: no stated rules
        else:
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(response.text.splitlines())
        self._robots[host] = parser
        return parser


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    return float(value) if value.isdigit() else None
