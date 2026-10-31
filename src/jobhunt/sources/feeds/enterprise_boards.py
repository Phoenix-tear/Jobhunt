"""Eightfold, Phenom and Oracle Recruiting Cloud career sites ("company careers site (<platform>)").

Each one uses an anonymous JSON endpoint that the employer's own public career page calls on a normal page
load (no login, no key, no authenticated HCM API):

* Eightfold:  GET  https://{host}/api/pcsx/search?domain=...&query=...&location=...   (cookie set by /careers)
* Phenom:     POST https://{careers-host}/widgets                                    (refNum read from the page)
* Oracle RC:  GET  https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?finder=findReqs;...

Politeness is the shared `PoliteFetcher`: robots.txt obeyed (an unreachable or WAF-blocked robots.txt skips the
host), one request at a time, `requests_per_minute` per host, a challenge page / repeated 403 / 429 drops the host.
These are feeds: a failed company never closes jobs. Companies come from `enterprise_companies` (`detect-ats`).
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

DEFAULT_QUERIES = ("software engineer", "developer", "data scientist", "machine learning", "AI", "graduate",
                   "intern", "associate engineer")
DEFAULT_LOCATIONS = ("India", "Remote")


class EnterpriseBoardSource(FeedSource):
    """Shared loop: companies from options, politeness, caps, one company's failure never loses the others."""

    min_interval_hours: ClassVar[float] = 12.0
    stale_after_days: ClassVar[int] = 21
    platform: ClassVar[str] = ""

    def __init__(self, options: Mapping[str, Any] | None = None, secrets: Mapping[str, str] | None = None,
                 *, fetcher: PoliteFetcher | None = None) -> None:
        super().__init__(options, secrets)
        self._fetcher = fetcher
        self.last_counts: dict[str, int] = {}

    @property
    def companies(self) -> list[dict[str, Any]]:
        return [item for item in as_list(self.options.get("companies")) if isinstance(item, dict)]

    @property
    def queries(self) -> list[str]:
        configured = self.options.get("queries")
        return [str(item) for item in configured] if isinstance(configured, list) and configured else list(DEFAULT_QUERIES)

    @property
    def locations(self) -> list[str]:
        configured = self.options.get("locations")
        return [str(item) for item in configured] if isinstance(configured, list) and configured else list(DEFAULT_LOCATIONS)

    @property
    def per_company_cap(self) -> int:
        return int(self.option("per_company_cap", 400))

    def fetch(self, http: HttpClient, context: FeedContext) -> FetchResult:
        companies = self.companies[: int(self.option("max_companies_per_run", 60))]
        if not companies:
            return FetchResult(jobs=[], warnings=[f"no {self.platform} companies detected yet: run `detect-ats`"])
        fetcher = self._fetcher or PoliteFetcher(
            http.user_agent, requests_per_minute=float(self.option("requests_per_minute", 20)))
        jobs: list[NormalizedJob] = []
        warnings: list[str] = []
        counts: dict[str, int] = {}
        try:
            for company in companies:
                name = str(company.get("name") or "?")
                try:
                    found = self.company_jobs(fetcher, company, context)
                except HostBlocked as error:
                    warnings.append(f"{name}: skipped, {error.reason}")
                    continue
                except (InvalidResponseError, httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                    warnings.append(f"{name}: {type(error).__name__}: {str(error)[:160]}")
                    continue
                counts[name] = len(found)
                jobs.extend(found)
        finally:
            if self._fetcher is None:
                fetcher.close()
        if not counts:
            raise TransientFetchError("; ".join(warnings[:5]) or f"no {self.platform} company could be fetched")
        warnings.append("jobs per company: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        self.last_counts = counts
        return FetchResult(jobs=jobs, warnings=warnings, total_reported=len(jobs))

    def company_jobs(self, fetcher: PoliteFetcher, company: Mapping[str, Any], context: FeedContext) -> list[NormalizedJob]:
        raise NotImplementedError

    # -- helpers -----------------------------------------------------------------------------

    @staticmethod
    def json_body(response: httpx.Response, what: str) -> dict[str, Any]:
        if response.status_code in (301, 302, 303, 307, 308):
            raise InvalidResponseError(f"{what}: redirect (maintenance page or moved site)")
        if response.status_code >= 400:
            raise InvalidResponseError(f"{what}: HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as error:
            raise InvalidResponseError(f"{what}: not JSON") from error
        if not isinstance(body, dict):
            raise InvalidResponseError(f"{what}: unexpected JSON shape")
        return body


# --- Eightfold -------------------------------------------------------------------------------------


class EightfoldSource(EnterpriseBoardSource):
    name: ClassVar[str] = "eightfold"
    platform: ClassVar[str] = "Eightfold"
    attribution: ClassVar[str] = "Company careers site (Eightfold)"

    def company_jobs(self, fetcher: PoliteFetcher, company: Mapping[str, Any], context: FeedContext) -> list[NormalizedJob]:
        params = as_dict(company.get("params"))
        host, domain, name = str(params["host"]), str(params["domain"]), str(company["name"])
        # a normal page load first: Eightfold sets the cookies its search API expects
        page = fetcher.get(f"https://{host}/careers", follow=3)
        if page.status_code >= 400:
            raise InvalidResponseError(f"{host}/careers: HTTP {page.status_code}")
        found: dict[str, dict[str, Any]] = {}
        page_size = int(self.option("page_size", 10))
        for location in self.locations:
            for query in self.queries:
                start = 0
                for _ in range(int(self.option("max_pages_per_query", 5))):
                    if len(found) >= self.per_company_cap:
                        break
                    response = fetcher.get(f"https://{host}/api/pcsx/search", params={
                        "domain": domain, "query": query, "location": location, "start": start, "num": page_size,
                        "sort_by": "timestamp"})
                    body = self.json_body(response, f"{host} pcsx search")
                    positions = as_list(as_dict(body.get("data")).get("positions"))
                    for position in positions:
                        position = as_dict(position)
                        if position.get("id") is not None:
                            found.setdefault(str(position["id"]), position)
                    if len(positions) < page_size:
                        break
                    start += len(positions)
        jobs: list[NormalizedJob] = []
        detail_cap, details = int(self.option("detail_cap_per_company", 150)), 0
        for position_id, position in list(found.items())[: self.per_company_cap]:
            title = clean_text(position.get("name"))
            if not title or not context.keep_title(title):
                continue
            locations = [clean_text(x) for x in as_list(position.get("locations"))]
            url = f"https://{host}{position.get('positionUrl') or '/careers/job/' + position_id}"
            workplace = str(position.get("workLocationOption") or "").lower()
            job = NormalizedJob(
                source=self.name, source_job_id=f"{domain}:{position_id}", title=title, company_name=name,
                apply_url=url, source_url=url, location_raw="; ".join(dict.fromkeys(x for x in locations if x)) or None,
                needs_description=True, posted_at=_epoch_iso(position.get("postedTs")),
                is_remote=True if workplace == "remote" else (False if workplace in {"onsite", "hybrid"} else None),
                attribution_text=f"Company careers site (Eightfold) · {position.get('displayJobId') or position_id}",
                raw={"workplaceType": workplace} if workplace else None,
            )
            if details < detail_cap:
                details += 1
                try:
                    detail = self.json_body(fetcher.get(f"https://{host}/api/pcsx/position_details", params={
                        "position_id": position_id, "domain": domain, "hl": "en"}), f"{host} position_details")
                    text = html_to_text(as_dict(detail.get("data")).get("jobDescription"))
                    if text:
                        job.description_text, job.needs_description = text, False
                except (InvalidResponseError, HostBlocked):
                    pass
            jobs.append(job)
        return jobs


# --- Phenom ----------------------------------------------------------------------------------------


class PhenomSource(EnterpriseBoardSource):
    name: ClassVar[str] = "phenom"
    platform: ClassVar[str] = "Phenom"
    attribution: ClassVar[str] = "Company careers site (Phenom)"

    def company_jobs(self, fetcher: PoliteFetcher, company: Mapping[str, Any], context: FeedContext) -> list[NormalizedJob]:
        params = as_dict(company.get("params"))
        host, name = str(params["host"]), str(company["name"])
        locale = str(params.get("locale") or "us/en")
        page = fetcher.get(f"https://{host}/{locale}/search-results", follow=3)
        if page.status_code >= 400:
            raise InvalidResponseError(f"{host} search page: HTTP {page.status_code}")
        reference = re.search(r'"refNum"\s*:\s*"([^"]+)"', page.text)
        if not reference:
            raise InvalidResponseError(f"{host}: no refNum in the search page")
        page_id = re.search(r'"pageId"\s*:\s*"([^"]+)"', page.text)
        found: dict[str, dict[str, Any]] = {}
        size = int(self.option("page_size", 10))
        passes = [{"country": ["India"]}, {}]
        for selected in passes:
            for query in self.queries:
                offset = 0
                for _ in range(int(self.option("max_pages_per_query", 5))):
                    if len(found) >= self.per_company_cap:
                        break
                    body = {
                        "lang": "en_us", "deviceType": "desktop", "country": "us", "pageName": "search-results",
                        "ddoKey": "refineSearch", "sortBy": "", "subsearch": "", "from": offset, "jobs": True,
                        "counts": True, "all_fields": ["category", "country", "state", "city", "type"], "size": size,
                        "clearAll": False, "jdsource": "facets", "isSliderEnable": False,
                        "pageId": page_id.group(1) if page_id else "page15", "siteType": "external",
                        "keywords": query, "global": True, "selected_fields": selected,
                        "sort": {"order": "desc", "field": "postedDate"}, "locationData": {}, "refNum": reference.group(1),
                    }
                    answer = self.json_body(fetcher.post_json(f"https://{host}/widgets", body), f"{host} widgets")
                    jobs = as_list(as_dict(as_dict(as_dict(answer.get("refineSearch")).get("data"))).get("jobs"))
                    for item in jobs:
                        item = as_dict(item)
                        key = clean_text(item.get("jobId")) or clean_text(item.get("reqId"))
                        if key:
                            found.setdefault(key, item)
                    if len(jobs) < size:
                        break
                    offset += len(jobs)
            if selected:      # the India pass alone already returned enough: skip the global pass
                if len(found) >= self.per_company_cap // 2:
                    break
        out: list[NormalizedJob] = []
        for key, item in list(found.items())[: self.per_company_cap]:
            title = clean_text(item.get("title"))
            if not title or not context.keep_title(title):
                continue
            location = clean_text(item.get("location")) or clean_text(item.get("cityStateCountry"))
            skills = ", ".join(str(s) for s in as_list(item.get("ml_skills"))[:20])
            description = " ".join(part for part in (clean_text(item.get("descriptionTeaser")),
                                                     f"Skills: {skills}." if skills else "") if part)
            url = clean_text(item.get("applyUrl")) or f"https://{host}/{locale}/job/{item.get('jobSeqNo') or key}"
            kind = (clean_text(item.get("type")) or "").lower()
            out.append(NormalizedJob(
                source=self.name, source_job_id=f"{host}:{key}", title=title, company_name=name, apply_url=url,
                source_url=url, location_raw=location, country=None,
                description_text=description or None, needs_description=True,
                posted_at=to_utc_iso(item.get("postedDate") or item.get("dateCreated")),
                employment_type=(EmploymentType.PART_TIME if "part" in kind else
                                 EmploymentType.FULL_TIME if "full" in kind else EmploymentType.UNKNOWN),
                attribution_text=f"Company careers site (Phenom) · {key}",
            ))
        return out


# --- Oracle Recruiting Cloud --------------------------------------------------------------------------


class OracleSource(EnterpriseBoardSource):
    name: ClassVar[str] = "oracle"
    platform: ClassVar[str] = "Oracle Recruiting Cloud"
    attribution: ClassVar[str] = "Company careers site (Oracle)"

    def company_jobs(self, fetcher: PoliteFetcher, company: Mapping[str, Any], context: FeedContext) -> list[NormalizedJob]:
        params = as_dict(company.get("params"))
        host, site, name = str(params["host"]), str(params["site"]), str(company["name"])
        found: dict[str, dict[str, Any]] = {}
        limit = int(self.option("page_size", 25))
        for query in self.queries:
            offset = 0
            for _ in range(int(self.option("max_pages_per_query", 6))):
                if len(found) >= self.per_company_cap:
                    break
                finder = f"findReqs;siteNumber={site},facetsList=LOCATIONS,limit={limit},offset={offset}," \
                         f"keyword={query},sortBy=POSTING_DATES_DESC"
                response = fetcher.get(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
                                       params={"onlyData": "true", "expand": "requisitionList.secondaryLocations",
                                               "finder": finder})
                body = self.json_body(response, f"{host} requisitions")
                items = as_list(body.get("items"))
                requisitions = as_list(as_dict(items[0]).get("requisitionList")) if items else []
                for requisition in requisitions:
                    requisition = as_dict(requisition)
                    if requisition.get("Id") is not None:
                        found.setdefault(str(requisition["Id"]), requisition)
                if len(requisitions) < limit:
                    break
                offset += len(requisitions)
        jobs: list[NormalizedJob] = []
        for job_id, item in list(found.items())[: self.per_company_cap]:
            title = clean_text(item.get("Title"))
            if not title or not context.keep_title(title):
                continue
            secondary = [clean_text(as_dict(x).get("Name")) for x in as_list(item.get("secondaryLocations"))]
            places = [clean_text(item.get("PrimaryLocation")), *secondary]
            url = f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}"
            workplace = (clean_text(item.get("WorkplaceType")) or "").lower()
            country = clean_text(item.get("PrimaryLocationCountry"))
            jobs.append(NormalizedJob(
                source=self.name, source_job_id=f"{host}:{job_id}", title=title, company_name=name, apply_url=url,
                source_url=url, location_raw="; ".join(dict.fromkeys(p for p in places if p)) or None,
                country=country.upper() if country and len(country) == 2 else None,
                description_text=clean_text(item.get("ShortDescriptionStr")), needs_description=True,
                posted_at=to_utc_iso(item.get("PostedDate")),
                is_remote=True if "remote" in workplace else (False if workplace else None),
                attribution_text=f"Company careers site (Oracle) · {job_id}",
                raw={"workplaceType": workplace} if workplace else None,
            ))
        return jobs


def _epoch_iso(value: Any) -> str | None:
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return None
