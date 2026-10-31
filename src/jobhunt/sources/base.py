"""The JobSource contract (Spring Boot analogy: an interface every adapter bean implements).

An adapter knows how to fetch *one company board* and return normalized jobs. It does not
touch the database, apply the prefilter or track lifecycle: the runner does that, so every
source gets identical behavior.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

import httpx

from jobhunt.errors import AuthRequiredError, InvalidResponseError, NotFoundError
from jobhunt.http import HttpClient
from jobhunt.models import AtsType, CompanyRef, FetchResult, NormalizedJob

logger = logging.getLogger(__name__)

_HTML_MARKERS = ("<!doctype", "<html", "<head", "<body")


@dataclass(frozen=True)
class FetchContext:
    """Runner-provided hooks an adapter may use while fetching one company."""

    # True when a job is worth an extra (expensive) detail request: it passes the title
    # prefilter and we do not already hold its full description.
    wants_detail: Callable[[NormalizedJob], bool] = field(default=lambda job: True)
    detail_cap: int = 200


class JobSource(ABC):
    """One implementation per ATS / aggregator."""

    name: ClassVar[str]               # `job_sightings.source` and the key under `sources:` in config
    ats_type: ClassVar[AtsType]       # which `companies.ats_type` rows this adapter serves
    authoritative: ClassVar[bool] = True  # a successful fetch lists *every* open job of the board

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.options: dict[str, Any] = dict(options or {})

    @abstractmethod
    def fetch_company(self, company: CompanyRef, http: HttpClient, context: FetchContext) -> FetchResult:
        """Fetch and normalize the whole board.

        Raises NotFoundError / InvalidResponseError / TransientFetchError / AuthRequiredError.
        Must never return a partial board: all-or-nothing keeps lifecycle logic honest.
        """

    def proves_board_exists(self, result: FetchResult) -> bool:
        """Whether a *valid* fetch proves the slug is real (discovery promotion rule)."""
        return True


class FetchBudget:
    """How many HTTP fetches a feed may still make today (its terms' daily cap, minus use so far).

    Adapters call `take()` before *every* request and stop when it returns False, so a provider's
    stated limit (Remotive ~4/day, Adzuna 250/day...) can never be exceeded by a long loop.
    """

    def __init__(self, available: int | None) -> None:
        self.remaining = available  # None = unlimited
        self.used = 0

    def take(self, count: int = 1) -> bool:
        if self.remaining is not None:
            if self.remaining < count:
                return False
            self.remaining -= count
        self.used += count
        return True


@dataclass
class FeedContext:
    """Runner-provided state for one feed fetch."""

    now: datetime
    budget: FetchBudget = field(default_factory=lambda: FetchBudget(None))
    cursor: str | None = None   # what the previous run stored in source_state.cursor_or_etag
    keep_title: Callable[[str], bool] = field(default=lambda title: True)  # the title prefilter


class FeedSource(ABC):
    """An aggregator, RSS feed, curated list or search API: not a per-company board.

    Unlike an ATS board, a feed lists only a window of recent/matching jobs, so a job missing
    from a fetch proves nothing. Jobs age out via `stale_after_days`; feeds that state
    closures explicitly (curated lists) report them in `FetchResult.closed_source_job_ids`.
    """

    name: ClassVar[str]
    authoritative: ClassVar[bool] = False
    daily_fetch_cap: ClassVar[int | None] = None   # provider-stated ceiling; options may lower it
    min_interval_hours: ClassVar[float] = 6.0
    stale_after_days: ClassVar[int] = 30
    required_secrets: ClassVar[tuple[str, ...]] = ()
    attribution: ClassVar[str] = ""                # stored on every sighting

    def __init__(self, options: Mapping[str, Any] | None = None, secrets: Mapping[str, str] | None = None) -> None:
        self.options: dict[str, Any] = dict(options or {})
        self._secrets = dict(secrets or {})

    # -- config-overridable limits ---------------------------------------------------------

    def option(self, key: str, default: Any = None) -> Any:
        value = self.options.get(key)
        return default if value is None else value

    @property
    def fetch_cap_per_day(self) -> int | None:
        configured = self.options.get("max_fetches_per_day")
        if configured is None:
            return self.daily_fetch_cap
        return int(configured) if self.daily_fetch_cap is None else min(int(configured), self.daily_fetch_cap)

    @property
    def interval_hours(self) -> float:
        return float(self.option("min_interval_hours", self.min_interval_hours))

    @property
    def stale_days(self) -> int:
        return int(self.option("stale_after_days", self.stale_after_days))

    # -- secrets ---------------------------------------------------------------------------

    def secret(self, key: str) -> str:
        return self._secrets.get(key, "")

    def missing_secrets(self) -> list[str]:
        return [key for key in self.required_secrets if not self._secrets.get(key)]

    @abstractmethod
    def fetch(self, http: HttpClient, context: FeedContext) -> FetchResult:
        """Fetch and normalize. Raises FetchError subclasses; must honor `context.budget`."""


# --- helpers shared by adapters ----------------------------------------------------------------

def looks_like_html(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    if "html" in content_type:
        return True
    head = response.text[:200].lstrip().lower()
    return head.startswith(_HTML_MARKERS)


def load_json(response: httpx.Response, *, what: str) -> Any:
    """Map an HTTP response to parsed JSON or the right FetchError."""
    status = response.status_code
    if status == 404:
        raise NotFoundError(f"{what}: HTTP 404")
    if status in (401, 403):
        raise AuthRequiredError(f"{what}: HTTP {status} (credentials required)")
    if 300 <= status < 400:
        # e.g. Workable answers 302 for an unknown account; a redirect is "no such board".
        raise NotFoundError(f"{what}: HTTP {status} redirect")
    if status >= 400:
        raise InvalidResponseError(f"{what}: unexpected HTTP {status}")
    try:
        return response.json()
    except ValueError as error:
        kind = "HTML page" if looks_like_html(response) else "non-JSON body"
        raise InvalidResponseError(f"{what}: {kind} instead of JSON") from error


def collect_jobs(
    raw_items: Iterable[Any], convert: Callable[[Any], NormalizedJob | None]
) -> tuple[list[NormalizedJob], int]:
    """Convert items one by one; unusable items are counted, never fatal.

    Duplicate source ids inside one payload keep the first occurrence.
    """
    jobs: list[NormalizedJob] = []
    seen: set[str] = set()
    malformed = 0
    for item in raw_items:
        try:
            job = convert(item)
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as error:
            logger.debug("skipping malformed item: %r", error)
            job = None
        if job is None:
            malformed += 1
            continue
        if job.source_job_id in seen:
            continue
        seen.add(job.source_job_id)
        jobs.append(job)
    return jobs, malformed


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def join_unique(parts: Iterable[str | None], separator: str = "; ") -> str | None:
    """Join non-empty parts, dropping case-insensitive duplicates, keeping order."""
    unique: dict[str, str] = {}
    for part in parts:
        if part and part.strip():
            unique.setdefault(part.strip().lower(), part.strip())
    return separator.join(unique.values()) or None
