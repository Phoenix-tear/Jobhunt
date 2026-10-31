"""Remote-job aggregators with simple public JSON/RSS endpoints.

* Remotive      https://remotive.com/api/remote-jobs   (max ~4 fetches/day, 2/min, attribution required,
                                                         jobs delayed 24h; do not republish to third parties)
* RemoteOK      https://remoteok.com/api               (first array element is a legal notice; attribution)
* We Work Remotely RSS                                  ("attribute the links back to We Work Remotely")
* Himalayas     https://himalayas.app/jobs/api[/search] (cursor paging, 20/page, refreshed daily, 429 on abuse)
* Jobicy        https://jobicy.com/api/v2/remote-jobs   (cursor paging; poll at most hourly; keep source URL)
* Arbeitnow     https://www.arbeitnow.com/api/job-board-api (hourly updates; link back)

Terms were checked on 2026-10-12; see docs/SOURCES.md for the wording and attribution text.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterable
from typing import Any

from jobhunt.errors import FetchError, InvalidResponseError
from jobhunt.http import HttpClient
from jobhunt.models import FetchResult, NormalizedJob
from jobhunt.normalize import annualize_salary, clean_text, html_to_text, parse_salary_text, to_number
from jobhunt.sources.base import FeedContext, FeedSource, as_list, collect_jobs, load_json
from jobhunt.sources.feeds.common import comma_join, employment_from_label, level_from_labels
from jobhunt.timeutil import to_utc_iso


def _dedupe(jobs: Iterable[NormalizedJob]) -> list[NormalizedJob]:
    unique: dict[str, NormalizedJob] = {}
    for job in jobs:
        unique.setdefault(job.source_job_id, job)
    return list(unique.values())


def _sections(
    targets: Iterable[Any],
    fetch_one: Callable[[Any], list[Any]],
    convert: Callable[[Any], NormalizedJob | None],
    context: FeedContext,
    label: str,
) -> FetchResult:
    """Run `fetch_one(target)` for each target (category, feed URL...) with failure isolation.

    One failing section is a warning; only when *every* section fails does the error propagate,
    so a transient problem on one category never discards the others.
    """
    jobs: list[NormalizedJob] = []
    warnings: list[str] = []
    malformed = 0
    succeeded = 0
    last_error: FetchError | None = None
    for target in targets:
        if not context.budget.take():
            warnings.append(f"{label}: daily fetch cap reached, remaining sections skipped")
            break
        try:
            items = fetch_one(target)
        except FetchError as error:
            last_error = error
            warnings.append(f"{label}/{target}: {error}")
            continue
        succeeded += 1
        converted, bad = collect_jobs(items, convert)
        jobs.extend(converted)
        malformed += bad
    if not succeeded and last_error is not None:
        raise last_error
    return FetchResult(jobs=_dedupe(jobs), malformed_items=malformed, warnings=warnings)


# --- Remotive -----------------------------------------------------------------------------------

class RemotiveSource(FeedSource):
    name = "remotive"
    daily_fetch_cap = 4          # Remotive: "max. 4 times a day"; more than 2/minute is blocked
    min_interval_hours = 6.0
    stale_after_days = 21
    attribution = "Jobs via Remotive (https://remotive.com); link back to the Remotive listing is required"
    url = "https://remotive.com/api/remote-jobs"

    def fetch(self, http: HttpClient, context: FeedContext) -> FetchResult:
        def fetch_category(category: str) -> list[Any]:
            payload = load_json(http.get(self.url, params={"category": category}), what=f"remotive/{category}")
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                raise InvalidResponseError(f"remotive/{category}: missing 'jobs' list")
            return payload["jobs"]

        categories = self.option("categories", ["software-dev", "data"])
        return _sections(categories, fetch_category, self._to_job, context, self.name)

    def _to_job(self, item: Any) -> NormalizedJob | None:
        if not isinstance(item, dict) or item.get("id") is None:
            return None
        title, url = clean_text(item.get("title")), clean_text(item.get("url"))
        if not title or not url:
            return None
        salary = parse_salary_text(item.get("salary"))
        description = html_to_text(item.get("description"))
        return NormalizedJob(
            source=self.name, source_job_id=str(item["id"]), title=title,
            company_name=clean_text(item.get("company_name")) or "",
            apply_url=url, source_url=url, location_raw=clean_text(item.get("candidate_required_location")),
            is_remote=True, employment_type=employment_from_label(item.get("job_type")),
            description_text=description or None, needs_description=not description,
            posted_at=to_utc_iso(item.get("publication_date")),
            salary_min=salary[0] if salary else None, salary_max=salary[1] if salary else None,
            salary_currency=salary[2] if salary else None,
            attribution_text=self.attribution,
            raw={key: value for key, value in item.items() if key != "description"},
        )


# --- RemoteOK -----------------------------------------------------------------------------------

class RemoteOkSource(FeedSource):
    name = "remoteok"
    daily_fetch_cap = 4
    min_interval_hours = 6.0
    stale_after_days = 21
    attribution = "Jobs via Remote OK (https://remoteok.com); link back (follow) and credit required"
    url = "https://remoteok.com/api"

    def fetch(self, http: HttpClient, context: FeedContext) -> FetchResult:
        def fetch_all(_: str) -> list[Any]:
            payload = load_json(http.get(self.url), what="remoteok")
            if not isinstance(payload, list):
                raise InvalidResponseError("remoteok: expected a JSON array")
            # Element 0 is the legal notice ({"legal": ...}); real jobs carry an "id".
            return [item for item in payload if isinstance(item, dict) and item.get("id") is not None]

        return _sections(["all"], fetch_all, self._to_job, context, self.name)

    def _to_job(self, item: Any) -> NormalizedJob | None:
        title = clean_text(item.get("position"))
        url = clean_text(item.get("url")) or clean_text(item.get("apply_url"))
        if not title or not url:
            return None
        minimum, maximum = to_number(item.get("salary_min")), to_number(item.get("salary_max"))
        description = html_to_text(item.get("description"))
        return NormalizedJob(
            source=self.name, source_job_id=str(item["id"]), title=title,
            company_name=clean_text(item.get("company")) or "",
            apply_url=url, source_url=url, location_raw=clean_text(item.get("location")) or "Remote",
            is_remote=True,
            description_text=description or None, needs_description=not description,
            posted_at=to_utc_iso(item.get("date")) or to_utc_iso(item.get("epoch")),
            salary_min=minimum, salary_max=maximum,
            salary_currency="USD" if (minimum or maximum) else None,
            attribution_text=self.attribution,
            raw={key: value for key, value in item.items() if key != "description"},
        )


# --- We Work Remotely (RSS) ---------------------------------------------------------------------

def parse_rss_items(xml_text: str, what: str) -> list[dict[str, str]]:
    """Flatten RSS <item> elements to dicts. A DTD or entity declaration is refused outright
    (untrusted XML: protects against entity-expansion attacks without a third-party parser)."""
    lowered = xml_text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise InvalidResponseError(f"{what}: feed contains a DTD, refusing to parse")
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as error:
        raise InvalidResponseError(f"{what}: not valid XML ({error})") from error
    return [
        {child.tag.split("}")[-1]: (child.text or "").strip() for child in element}
        for element in root.iter("item")
    ]


class WeWorkRemotelySource(FeedSource):
    name = "weworkremotely"
    min_interval_hours = 6.0
    stale_after_days = 30
    attribution = "Jobs via We Work Remotely (https://weworkremotely.com); links attributed to We Work Remotely"
    default_feeds = ("https://weworkremotely.com/categories/remote-programming-jobs.rss",)

    def fetch(self, http: HttpClient, context: FeedContext) -> FetchResult:
        def fetch_feed(feed_url: str) -> list[Any]:
            response = http.get(feed_url, headers={"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5"})
            if response.status_code != 200:
                raise InvalidResponseError(f"weworkremotely: HTTP {response.status_code}")
            return parse_rss_items(response.text, "weworkremotely")

        feeds = self.option("feeds", list(self.default_feeds))
        return _sections(feeds, fetch_feed, self._to_job, context, self.name)

    def _to_job(self, item: Any) -> NormalizedJob | None:
        if not isinstance(item, dict):
            return None
        link = clean_text(item.get("link")) or clean_text(item.get("guid"))
        raw_title = clean_text(item.get("title"))
        if not link or not raw_title:
            return None
        company, separator, role = raw_title.partition(": ")  # "Company: Role"
        if not separator:
            company, role = "", raw_title
        description = html_to_text(item.get("description"))
        return NormalizedJob(
            source=self.name, source_job_id=clean_text(item.get("guid")) or link, title=role.strip(),
            company_name=company.strip(), apply_url=link, source_url=link,
            location_raw=clean_text(item.get("region")) or "Remote", is_remote=True,
            employment_type=employment_from_label(item.get("type")),
            description_text=description or None, needs_description=not description,
            posted_at=to_utc_iso(item.get("pubDate")), attribution_text=self.attribution,
            raw={key: value for key, value in item.items() if key != "description"},
        )


# --- Himalayas ----------------------------------------------------------------------------------

class HimalayasSource(FeedSource):
    name = "himalayas"
    min_interval_hours = 24.0    # the feed refreshes every 24h; polling more often gains nothing
    stale_after_days = 30
    attribution = "Jobs via Himalayas (https://himalayas.app); visible link and credit required when displayed"
    browse_url = "https://himalayas.app/jobs/api"
    search_url = "https://himalayas.app/jobs/api/search"
    page_size = 20  # documented maximum

    def fetch(self, http: HttpClient, context: FeedContext) -> FetchResult:
        queries = self.option("queries", ["software engineer", "machine learning", "data engineer", "intern"])
        pages = int(self.option("pages_per_query", 3))

        def fetch_query(query: str) -> list[Any]:
            collected: list[Any] = []
            for page in range(1, pages + 1):
                if page > 1 and not context.budget.take():
                    break
                payload = load_json(
                    http.get(self.search_url, params={"q": query, "page": page}), what=f"himalayas/{query}"
                )
                if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                    raise InvalidResponseError(f"himalayas/{query}: missing 'jobs' list")
                collected.extend(payload["jobs"])
                if len(payload["jobs"]) < self.page_size:
                    break
            return collected

        if not queries:  # browse the newest jobs with cursor pagination instead
            return self._browse(http, context, int(self.option("max_pages", 10)))
        return _sections(queries, fetch_query, self._to_job, context, self.name)

    def _browse(self, http: HttpClient, context: FeedContext, max_pages: int) -> FetchResult:
        collected: list[Any] = []
        cursor: str | None = None
        warnings: list[str] = []
        for _ in range(max_pages):
            if not context.budget.take():
                warnings.append("himalayas: fetch cap reached")
                break
            params: dict[str, Any] = {"limit": self.page_size}
            if cursor:
                params["cursor"] = cursor
            payload = load_json(http.get(self.browse_url, params=params), what="himalayas")
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                raise InvalidResponseError("himalayas: missing 'jobs' list")
            collected.extend(payload["jobs"])
            cursor = payload.get("nextCursor")
            if not cursor:
                break
        jobs, malformed = collect_jobs(collected, self._to_job)
        return FetchResult(jobs=_dedupe(jobs), malformed_items=malformed, warnings=warnings)

    def _to_job(self, item: Any) -> NormalizedJob | None:
        if not isinstance(item, dict):
            return None
        title = clean_text(item.get("title"))
        job_id = clean_text(item.get("guid")) or clean_text(item.get("applicationLink"))
        if not title or not job_id:
            return None
        url = clean_text(item.get("applicationLink")) or job_id
        minimum, maximum = annualize_salary(
            to_number(item.get("minSalary")), to_number(item.get("maxSalary")), clean_text(item.get("salaryPeriod"))
        )
        description = html_to_text(item.get("description"))
        seniority = as_list(item.get("seniority"))
        employment = employment_from_label(item.get("employmentType"))
        level = level_from_labels(*seniority)
        return NormalizedJob(
            source=self.name, source_job_id=job_id, title=title,
            company_name=clean_text(item.get("companyName")) or "",
            apply_url=url, source_url=job_id if job_id.startswith("http") else url,
            location_raw=comma_join(item.get("locationRestrictions")) or "Worldwide", is_remote=True,
            employment_type=employment, experience_level=level,
            description_text=description or None, needs_description=not description,
            posted_at=to_utc_iso(item.get("pubDate")),
            salary_min=minimum, salary_max=maximum,
            salary_currency=clean_text(item.get("currency")) if (minimum or maximum) else None,
            attribution_text=self.attribution,
            raw={key: value for key, value in item.items() if key not in {"description", "excerpt"}},
        )


# --- Jobicy -------------------------------------------------------------------------------------

class JobicySource(FeedSource):
    name = "jobicy"
    min_interval_hours = 6.0     # Jobicy asks for no more than one automated sync per hour
    stale_after_days = 14        # the API only exposes the last 7 days
    attribution = "Jobs via Jobicy (https://jobicy.com); original Jobicy URL preserved"
    url = "https://jobicy.com/api/v2/remote-jobs"

    def fetch(self, http: HttpClient, context: FeedContext) -> FetchResult:
        count = min(int(self.option("count", 100)), 200)
        max_pages = int(self.option("max_pages", 2))

        def fetch_industry(industry: str) -> list[Any]:
            collected: list[Any] = []
            cursor: str | None = None
            for page in range(max_pages):
                if page > 0 and not context.budget.take():
                    break
                params: dict[str, Any] = {"count": count, "industry": industry}
                if cursor:
                    params["cursor"] = cursor
                payload = load_json(http.get(self.url, params=params), what=f"jobicy/{industry}")
                if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                    raise InvalidResponseError(f"jobicy/{industry}: missing 'jobs' list")
                collected.extend(payload["jobs"])
                cursor = payload.get("nextCursor") if payload.get("hasMore") else None
                if not cursor:
                    break
            return collected

        industries = self.option("industries", ["engineering", "data-science"])
        return _sections(industries, fetch_industry, self._to_job, context, self.name)

    def _to_job(self, item: Any) -> NormalizedJob | None:
        if not isinstance(item, dict) or item.get("id") is None:
            return None
        title, url = clean_text(item.get("jobTitle")), clean_text(item.get("url"))
        if not title or not url:
            return None
        minimum, maximum = annualize_salary(
            to_number(item.get("salaryMin")), to_number(item.get("salaryMax")), clean_text(item.get("salaryPeriod"))
        )
        job_types = as_list(item.get("jobType"))
        description = html_to_text(item.get("jobDescription")) or html_to_text(item.get("jobExcerpt"))
        return NormalizedJob(
            source=self.name, source_job_id=str(item["id"]), title=title,
            company_name=clean_text(item.get("companyName")) or "",
            apply_url=url, source_url=url, location_raw=clean_text(item.get("jobGeo")), is_remote=True,
            employment_type=employment_from_label(" ".join(str(value) for value in job_types)),
            experience_level=level_from_labels(item.get("jobLevel")),
            description_text=description or None, needs_description=not html_to_text(item.get("jobDescription")),
            posted_at=to_utc_iso(item.get("pubDate")),
            salary_min=minimum, salary_max=maximum,
            salary_currency=clean_text(item.get("salaryCurrency")) if (minimum or maximum) else None,
            attribution_text=self.attribution,
            raw={key: value for key, value in item.items() if key not in {"jobDescription", "jobExcerpt"}},
        )


# --- Arbeitnow ----------------------------------------------------------------------------------

class ArbeitnowSource(FeedSource):
    name = "arbeitnow"
    min_interval_hours = 12.0    # "Jobs are updated every hour"; twice a day is plenty
    stale_after_days = 30
    attribution = "Jobs via Arbeitnow (https://www.arbeitnow.com); link back appreciated, site terms apply"
    url = "https://www.arbeitnow.com/api/job-board-api"

    def fetch(self, http: HttpClient, context: FeedContext) -> FetchResult:
        pages = [str(page) for page in range(1, int(self.option("max_pages", 2)) + 1)]

        def fetch_page(page: str) -> list[Any]:
            payload = load_json(http.get(self.url, params={"page": page}), what=f"arbeitnow/page {page}")
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise InvalidResponseError(f"arbeitnow page {page}: missing 'data' list")
            return payload["data"]

        return _sections(pages, fetch_page, self._to_job, context, self.name)

    def _to_job(self, item: Any) -> NormalizedJob | None:
        if not isinstance(item, dict):
            return None
        slug, title, url = clean_text(item.get("slug")), clean_text(item.get("title")), clean_text(item.get("url"))
        if not slug or not title or not url:
            return None
        job_types = " ".join(str(value) for value in as_list(item.get("job_types")))
        description = html_to_text(item.get("description"))
        return NormalizedJob(
            source=self.name, source_job_id=slug, title=title,
            company_name=clean_text(item.get("company_name")) or "",
            apply_url=url, source_url=url, location_raw=clean_text(item.get("location")),
            is_remote=True if item.get("remote") is True else None,
            employment_type=employment_from_label(job_types),
            description_text=description or None, needs_description=not description,
            posted_at=to_utc_iso(item.get("created_at")), attribution_text=self.attribution,
            raw={key: value for key, value in item.items() if key != "description"},
        )
