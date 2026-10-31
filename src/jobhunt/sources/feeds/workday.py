"""Workday career sites ("company careers site (Workday)").

Every Workday career site is backed by the public JSON endpoint its own search box calls:

    POST https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
         {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "software engineer"}
    GET  https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}   (one posting)

No login, no key. We stay polite: robots.txt is obeyed, one request at a time, `requests_per_minute`
per host, hard page caps, and a host that shows bot protection or answers 429 is dropped for the
run. Companies come from the `enterprise_companies` table (filled by `detect-ats`).

This is a *feed*: a failed company never closes its jobs; postings age out via `stale_after_days`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any, ClassVar

import httpx

from jobhunt.enterprise.polite import HostBlocked, PoliteFetcher
from jobhunt.errors import InvalidResponseError, TransientFetchError
from jobhunt.http import HttpClient
from jobhunt.models import EmploymentType, FetchResult, NormalizedJob
from jobhunt.normalize import clean_text, html_to_text
from jobhunt.sources.base import FeedContext, FeedSource, as_dict, as_list
from jobhunt.timeutil import to_utc_iso

logger = logging.getLogger(__name__)

PAGE_SIZE = 20
DEFAULT_QUERIES = (
    "software engineer", "SDE", "developer", "data scientist", "machine learning", "AI", "graduate", "intern",
    "associate engineer",
)
_COUNTRY_FACETS = {"locationcountry", "location_country", "country", "locationregion"}
_REQ_ID = re.compile(r"_([A-Za-z]{0,4}-?\d[\w-]*)$")


class WorkdaySource(FeedSource):
    name: ClassVar[str] = "workday"
    attribution: ClassVar[str] = "Company careers site (Workday)"
    min_interval_hours: ClassVar[float] = 12.0
    stale_after_days: ClassVar[int] = 21

    def __init__(self, options: Mapping[str, Any] | None = None, secrets: Mapping[str, str] | None = None,
                 *, fetcher: PoliteFetcher | None = None) -> None:
        super().__init__(options, secrets)
        self._fetcher = fetcher

    # -- config ------------------------------------------------------------------------------

    @property
    def companies(self) -> list[dict[str, Any]]:
        return [item for item in as_list(self.options.get("companies")) if isinstance(item, dict)]

    @property
    def queries(self) -> list[str]:
        configured = self.options.get("queries")
        return [str(item) for item in configured] if isinstance(configured, list) and configured else list(DEFAULT_QUERIES)

    @property
    def per_company_cap(self) -> int:
        return int(self.option("per_company_cap", 400))

    # -- fetch ---------------------------------------------------------------------------------

    def fetch(self, http: HttpClient, context: FeedContext) -> FetchResult:
        companies = self.companies
        if not companies:
            return FetchResult(jobs=[], warnings=["no Workday companies detected yet: run `detect-ats` first"])
        fetcher = self._fetcher or PoliteFetcher(
            http.user_agent, requests_per_minute=float(self.option("requests_per_minute", 20)),
        )
        limit = int(self.option("max_companies_per_run", len(companies)))
        jobs: list[NormalizedJob] = []
        warnings: list[str] = []
        succeeded = 0
        counts: dict[str, int] = {}
        try:
            for company in companies[:limit]:
                name = str(company.get("name") or "?")
                try:
                    found = self._fetch_company(fetcher, company, context)
                except HostBlocked as error:
                    warnings.append(f"{name}: skipped, {error.reason}")
                    continue
                except (InvalidResponseError, httpx.HTTPError, ValueError, KeyError) as error:
                    warnings.append(f"{name}: {type(error).__name__}: {str(error)[:160]}")
                    continue
                succeeded += 1
                counts[name] = len(found)
                jobs.extend(found)
                if not context.budget.take():
                    warnings.append("fetch budget used up; remaining companies skipped")
                    break
        finally:
            if self._fetcher is None:
                fetcher.close()
        if succeeded == 0:
            raise TransientFetchError("; ".join(warnings[:5]) or "no Workday company could be fetched")
        warnings.append("jobs per company: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        self.last_counts = counts
        return FetchResult(jobs=jobs, warnings=warnings, total_reported=len(jobs))

    last_counts: dict[str, int] = {}

    # -- one company -----------------------------------------------------------------------------

    def _fetch_company(self, fetcher: PoliteFetcher, company: Mapping[str, Any], context: FeedContext) -> list[NormalizedJob]:
        params = as_dict(company.get("params"))
        host, tenant, site = str(params["host"]), str(params["tenant"]), str(params["site"])
        locale = str(params.get("locale") or "en-US")
        list_url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        name = str(company["name"])

        listed: dict[str, dict[str, Any]] = {}
        country_facets: list[tuple[str, str]] = []
        for query in self.queries:
            self._collect(fetcher, list_url, {}, query, listed, country_facets)
            if len(listed) >= self.per_company_cap:
                break
        # India (and remote) variants of every query, when the site exposes a country facet.
        if country_facets and len(listed) < self.per_company_cap:
            for query in self.queries:
                for facet, value_id in country_facets:
                    self._collect(fetcher, list_url, {facet: [value_id]}, query, listed, [])
                if len(listed) >= self.per_company_cap:
                    break

        jobs: list[NormalizedJob] = []
        detail_cap = int(self.option("detail_cap_per_company", 150))
        details = 0
        for path, item in list(listed.items())[: self.per_company_cap]:
            title = clean_text(item.get("title"))
            if not title or not context.keep_title(title):
                continue           # cheap title prefilter BEFORE any detail request
            job = self._to_job(item, path, name, host, locale, site, tenant)
            if details < detail_cap:
                details += 1
                self._add_detail(fetcher, job, f"https://{host}/wday/cxs/{tenant}/{site}{path}")
            jobs.append(job)
        return jobs

    def _collect(self, fetcher: PoliteFetcher, url: str, facets: dict[str, list[str]], query: str,
                 listed: dict[str, dict[str, Any]], country_facets: list[tuple[str, str]]) -> None:
        max_pages = int(self.option("max_pages_per_query", 10))
        offset = 0
        for _ in range(max_pages):
            if len(listed) >= self.per_company_cap:
                return
            response = fetcher.post_json(url, {"appliedFacets": facets, "limit": PAGE_SIZE, "offset": offset,
                                               "searchText": query})
            payload = self._json(response, url)
            postings = as_list(payload.get("jobPostings"))
            if offset == 0 and not facets:
                country_facets.extend(self._india_facets(payload))
            for posting in postings:
                posting = as_dict(posting)
                path = clean_text(posting.get("externalPath"))
                if path and path not in listed:
                    listed[path] = posting
            if len(postings) < PAGE_SIZE:
                return
            offset += PAGE_SIZE

    @staticmethod
    def _json(response: httpx.Response, url: str) -> dict[str, Any]:
        if response.status_code in (301, 302, 303, 307, 308):
            raise InvalidResponseError(f"{url}: redirect (maintenance page or moved site)")
        if response.status_code == 404:
            raise InvalidResponseError(f"{url}: HTTP 404 (tenant or site name changed)")
        if response.status_code >= 400:
            raise InvalidResponseError(f"{url}: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise InvalidResponseError(f"{url}: not JSON") from error
        if not isinstance(payload, dict):
            raise InvalidResponseError(f"{url}: unexpected JSON shape")
        return payload

    @staticmethod
    def _india_facets(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for facet in as_list(payload.get("facets")):
            facet = as_dict(facet)
            parameter = str(facet.get("facetParameter") or "")
            if parameter.lower() not in _COUNTRY_FACETS:
                continue
            for value in as_list(facet.get("values")):
                value = as_dict(value)
                if str(value.get("descriptor") or "").strip().lower() == "india" and value.get("id"):
                    found.append((parameter, str(value["id"])))
        return found

    def _add_detail(self, fetcher: PoliteFetcher, job: NormalizedJob, url: str) -> None:
        try:
            response = fetcher.get(url)
            payload = self._json(response, url)
        except (HostBlocked, InvalidResponseError, httpx.HTTPError):
            job.needs_description = True
            return
        info = as_dict(payload.get("jobPostingInfo"))
        description = html_to_text(info.get("jobDescription"))
        if description:
            job.description_text = description
            job.needs_description = False
        locations = [clean_text(info.get("location"))] + [clean_text(x) for x in as_list(info.get("additionalLocations"))]
        place = "; ".join(dict.fromkeys(item for item in locations if item))
        if place:
            job.location_raw = place
        posted = to_utc_iso(info.get("startDate"))
        if posted:
            job.posted_at = posted
        time_type = (clean_text(info.get("timeType")) or "").lower()
        if time_type:
            job.employment_type = (EmploymentType.PART_TIME if "part" in time_type else
                                   EmploymentType.FULL_TIME if "full" in time_type else job.employment_type)
        remote = (clean_text(info.get("remoteType")) or "").lower()
        if remote == "remote":
            job.is_remote = True
        elif remote in {"hybrid", "flex", "on-site", "onsite", "office"}:
            job.is_remote = False
        external = clean_text(info.get("externalUrl"))
        if external and external.startswith("https://"):
            job.apply_url = job.source_url = external
        reqid = clean_text(info.get("jobReqId"))
        if reqid:
            job.raw = {**(job.raw or {}), "jobReqId": reqid, "workplaceType": info.get("remoteType")}

    @staticmethod
    def _to_job(item: Mapping[str, Any], path: str, company: str, host: str, locale: str, site: str,
                tenant: str) -> NormalizedJob:
        url = f"https://{host}/{locale}/{site}{path}"
        match = _REQ_ID.search(path)
        bullets = [clean_text(x) for x in as_list(item.get("bulletFields"))]
        req = bullets[0] if bullets and bullets[0] else (match.group(1) if match else None)
        return NormalizedJob(
            source="workday", source_job_id=f"{tenant}:{path}", title=clean_text(item.get("title")) or "",
            company_name=company, apply_url=url, source_url=url,
            location_raw=clean_text(item.get("locationsText")), needs_description=True,
            attribution_text=f"Company careers site (Workday){f' · {req}' if req else ''}",
            raw={"externalPath": path, "postedOn": item.get("postedOn"), "reqId": req},
        )
