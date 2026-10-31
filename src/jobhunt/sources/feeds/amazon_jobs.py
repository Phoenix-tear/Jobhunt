"""amazon.jobs ("company careers site (amazon.jobs)").

The site's own search page loads results from a public, keyless JSON endpoint:

    GET https://www.amazon.jobs/en/search.json?base_query=...&normalized_country_code[]=IND
        &result_limit=100&offset=0

Its robots.txt allows this path (only /internal is disallowed) and the answer carries the full
description, so no per-job request is needed. A single search returns at most 10,000 hits; we
stay far below that with `per_query_cap`, a handful of queries and a low request rate.
The terms of use were not found to forbid it, but they are not machine-readable either: review
them yourself if this matters to you (see docs/SOURCES.md).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any, ClassVar

import httpx

from jobhunt.enterprise.polite import HostBlocked, PoliteFetcher
from jobhunt.errors import InvalidResponseError, TransientFetchError
from jobhunt.http import HttpClient
from jobhunt.models import EmploymentType, FetchResult, NormalizedJob
from jobhunt.normalize import clean_text, html_to_text
from jobhunt.sources.base import FeedContext, FeedSource, as_list
from jobhunt.timeutil import to_utc_iso

logger = logging.getLogger(__name__)

BASE = "https://www.amazon.jobs"
PAGE_SIZE = 100
HARD_RESULT_LIMIT = 10_000
DEFAULT_QUERIES = ("software engineer", "software development engineer", "machine learning", "data scientist",
                   "applied scientist", "intern", "graduate")


class AmazonJobsSource(FeedSource):
    name: ClassVar[str] = "amazon_jobs"
    attribution: ClassVar[str] = "Company careers site (amazon.jobs)"
    min_interval_hours: ClassVar[float] = 12.0
    stale_after_days: ClassVar[int] = 21

    def __init__(self, options: Mapping[str, Any] | None = None, secrets: Mapping[str, str] | None = None,
                 *, fetcher: PoliteFetcher | None = None) -> None:
        super().__init__(options, secrets)
        self._fetcher = fetcher

    def fetch(self, http: HttpClient, context: FeedContext) -> FetchResult:
        fetcher = self._fetcher or PoliteFetcher(
            http.user_agent, requests_per_minute=float(self.option("requests_per_minute", 10)))
        queries = [str(item) for item in as_list(self.options.get("queries"))] or list(DEFAULT_QUERIES)
        countries = [str(item) for item in as_list(self.options.get("countries"))] or ["IND"]
        cap = min(int(self.option("per_run_cap", 600)), HARD_RESULT_LIMIT)
        max_pages = int(self.option("max_pages_per_query", 5))
        found: dict[str, NormalizedJob] = {}
        warnings: list[str] = []
        try:
            for query in queries:
                try:
                    self._search(fetcher, query, countries, max_pages, cap, found, context)
                except HostBlocked as error:
                    warnings.append(f"stopped: {error.reason}")
                    break
                except (InvalidResponseError, httpx.HTTPError) as error:
                    warnings.append(f"{query!r}: {str(error)[:150]}")
                if len(found) >= cap:
                    break
        finally:
            if self._fetcher is None:
                fetcher.close()
        if not found and warnings:
            raise TransientFetchError("; ".join(warnings[:3]))
        return FetchResult(jobs=list(found.values()), warnings=warnings, total_reported=len(found))

    def _search(self, fetcher: PoliteFetcher, query: str, countries: list[str], max_pages: int, cap: int,
                found: dict[str, NormalizedJob], context: FeedContext) -> None:
        for page in range(max_pages):
            params: list[tuple[str, str]] = [("base_query", query), ("result_limit", str(PAGE_SIZE)),
                                             ("offset", str(page * PAGE_SIZE)), ("sort", "recent")]
            params += [("normalized_country_code[]", code) for code in countries]
            response = fetcher.get(f"{BASE}/en/search.json", params=params)  # type: ignore[arg-type]
            if response.status_code != 200:
                raise InvalidResponseError(f"amazon.jobs: HTTP {response.status_code}")
            try:
                payload = response.json()
            except ValueError as error:
                raise InvalidResponseError("amazon.jobs: not JSON") from error
            jobs = payload.get("jobs") if isinstance(payload, dict) else None
            if not isinstance(jobs, list):
                raise InvalidResponseError("amazon.jobs: unexpected JSON shape")
            for item in jobs:
                job = self._to_job(item)
                if job is None or job.source_job_id in found or not context.keep_title(job.title):
                    continue
                found[job.source_job_id] = job
                if len(found) >= cap:
                    return
            if len(jobs) < PAGE_SIZE or (page + 1) * PAGE_SIZE >= HARD_RESULT_LIMIT:
                return

    @staticmethod
    def _to_job(item: Any) -> NormalizedJob | None:
        if not isinstance(item, dict):
            return None
        job_id = clean_text(item.get("id_icims")) or clean_text(item.get("id"))
        title = clean_text(item.get("title"))
        path = clean_text(item.get("job_path"))
        if not job_id or not title or not path:
            return None
        url = f"{BASE}{path}"
        parts = [html_to_text(item.get(key)) for key in ("description", "basic_qualifications", "preferred_qualifications")]
        description = "\n\n".join(part for part in parts if part)
        schedule = (clean_text(item.get("job_schedule_type")) or "").lower()
        posted = None
        raw_date = " ".join(str(item.get("posted_date") or "").split())
        for fmt in ("%B %d, %Y",):
            try:
                posted = to_utc_iso(datetime.strptime(raw_date, fmt).date().isoformat())
            except ValueError:
                continue
        country = clean_text(item.get("country_code"))
        return NormalizedJob(
            source="amazon_jobs", source_job_id=job_id, title=title,
            company_name=clean_text(item.get("company_name")) or "Amazon",
            apply_url=clean_text(item.get("url_next_step")) or url, source_url=url,
            location_raw=clean_text(item.get("normalized_location")) or clean_text(item.get("location")),
            city=clean_text(item.get("city")),
            description_text=description or None, needs_description=not description, posted_at=posted,
            employment_type=(EmploymentType.PART_TIME if "part" in schedule else
                             EmploymentType.FULL_TIME if "full" in schedule else EmploymentType.UNKNOWN),
            attribution_text=f"Company careers site (amazon.jobs) · {item.get('job_category') or 'job'}",
            raw={"country_code": country, "job_category": item.get("job_category")},
        )
