"""Lever Postings API.

Docs: https://github.com/lever/postings-api
GET https://api.lever.co/v0/postings/{site}?mode=json      (EU accounts: api.eu.lever.co)
Public, keyless. The endpoint sometimes answers 200 with an HTML page; that is retried once
and then reported as an invalid response (a failure, never a "board is empty").
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from jobhunt.errors import InvalidResponseError, NotFoundError
from jobhunt.http import HttpClient
from jobhunt.models import AtsType, CompanyRef, EmploymentType, FetchResult, NormalizedJob
from jobhunt.normalize import annualize_salary, clean_text, html_to_text, to_number
from jobhunt.sources.base import (
    FetchContext, JobSource, as_dict, as_list, collect_jobs, join_unique, load_json, looks_like_html,
)
from jobhunt.timeutil import to_utc_iso

PAGE_SIZE = 100
MAX_PAGES = 100  # 10,000 postings per board is far beyond any real account

_HOSTS = {"us": "https://api.lever.co", "eu": "https://api.eu.lever.co"}


class LeverSource(JobSource):
    name = "lever"
    ats_type = AtsType.LEVER

    def fetch_company(self, company: CompanyRef, http: HttpClient, context: FetchContext) -> FetchResult:
        # Unknown region: try the US host first and fall back to EU on 404.
        regions = [company.region] if company.region in _HOSTS else ["us", "eu"]
        last_not_found: NotFoundError | None = None
        for region in regions:
            try:
                postings = self._fetch_all_pages(_HOSTS[region], company.slug, http)
            except NotFoundError as error:
                last_not_found = error
                continue
            jobs, malformed = collect_jobs(postings, lambda item: self._to_job(item, company))
            return FetchResult(jobs=jobs, region=region, malformed_items=malformed, total_reported=len(postings))
        raise last_not_found or NotFoundError(f"lever site '{company.slug}': not found")

    # -- paging ----------------------------------------------------------------------------

    def _fetch_all_pages(self, host: str, slug: str, http: HttpClient) -> list[Any]:
        url = f"{host}/v0/postings/{quote(slug, safe='')}"
        postings: list[Any] = []
        seen_ids: set[str] = set()
        skip = 0
        for _ in range(MAX_PAGES):
            page = self._get_page(url, {"mode": "json", "skip": skip, "limit": PAGE_SIZE}, http, slug)
            new_items = 0
            for item in page:
                item_id = str(item.get("id")) if isinstance(item, dict) and item.get("id") is not None else None
                if item_id is not None:
                    if item_id in seen_ids:
                        continue  # server ignored `skip`, or a duplicate inside the payload
                    seen_ids.add(item_id)
                postings.append(item)
                new_items += 1
            if len(page) < PAGE_SIZE or new_items == 0:
                break
            skip += len(page)
        return postings

    def _get_page(self, url: str, params: dict[str, Any], http: HttpClient, slug: str) -> list[Any]:
        what = f"lever site '{slug}'"
        last_error: InvalidResponseError | None = None
        for _attempt in range(2):  # intermittent HTML responses: retry once, then count as failure
            response = http.get(url, params=params)
            try:
                payload = load_json(response, what=what)
            except InvalidResponseError as error:
                last_error = error
                if looks_like_html(response):
                    continue
                raise
            if not isinstance(payload, list):
                raise InvalidResponseError(f"{what}: expected a JSON array, got {type(payload).__name__}")
            return payload
        assert last_error is not None
        raise last_error

    # -- mapping ---------------------------------------------------------------------------

    def _to_job(self, item: Any, company: CompanyRef) -> NormalizedJob | None:
        if not isinstance(item, dict) or item.get("id") is None:
            return None
        title = clean_text(item.get("text"))
        if not title:
            return None
        job_id = str(item["id"])
        categories = as_dict(item.get("categories"))

        all_locations = [clean_text(entry) for entry in as_list(categories.get("allLocations"))]
        location_raw = join_unique([*all_locations, clean_text(categories.get("location"))])

        workplace = clean_text(item.get("workplaceType"))
        is_remote: bool | None = None
        if workplace:
            normalized = workplace.lower()
            if normalized == "remote":
                is_remote = True
            elif normalized in {"hybrid", "on-site", "onsite"}:
                is_remote = False

        salary = as_dict(item.get("salaryRange"))
        salary_min, salary_max = annualize_salary(
            to_number(salary.get("min")), to_number(salary.get("max")), clean_text(salary.get("interval"))
        )
        country = clean_text(item.get("country"))
        description = self._description(item)
        url = clean_text(item.get("hostedUrl")) or clean_text(item.get("applyUrl"))

        job = NormalizedJob(
            source=self.name,
            source_job_id=job_id,
            title=title,
            company_name=company.name,
            apply_url=url,
            source_url=url,
            location_raw=location_raw,
            country=country.upper() if country and len(country) == 2 else None,
            is_remote=is_remote,
            description_text=description or None,
            needs_description=not description,
            posted_at=to_utc_iso(item.get("createdAt")),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=clean_text(salary.get("currency")) if salary_min or salary_max else None,
            raw={key: value for key, value in item.items()
                 if key not in {"description", "descriptionPlain", "descriptionBody", "descriptionBodyPlain",
                                "additional", "additionalPlain", "lists", "opening", "openingPlain"}},
        )
        commitment = clean_text(categories.get("commitment"))
        if commitment:
            job.employment_type = _employment_from_commitment(commitment)
        return job

    @staticmethod
    def _description(item: dict[str, Any]) -> str:
        """Plain description + the structured lists (requirements, benefits...) + closing text."""
        parts: list[str] = []
        main = html_to_text(item.get("descriptionPlain")) or html_to_text(item.get("description"))
        if main:
            parts.append(main)
        for section in as_list(item.get("lists")):
            section = as_dict(section)
            heading = clean_text(section.get("text"))
            body = html_to_text(section.get("content"))
            if heading or body:
                parts.append("\n".join(part for part in (heading, body) if part))
        closing = html_to_text(item.get("additionalPlain")) or html_to_text(item.get("additional"))
        if closing:
            parts.append(closing)
        return "\n\n".join(parts)


def _employment_from_commitment(commitment: str) -> EmploymentType:
    text = commitment.lower()
    if "intern" in text or "co-op" in text:
        return EmploymentType.INTERNSHIP
    if "part" in text:
        return EmploymentType.PART_TIME
    if any(word in text for word in ("contract", "temp", "freelance")):
        return EmploymentType.CONTRACT
    if "full" in text or "regular" in text or "permanent" in text:
        return EmploymentType.FULL_TIME
    return EmploymentType.UNKNOWN
