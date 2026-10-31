"""Workable public account endpoint.

Docs: https://help.workable.com/hc/en-us/articles/115012771647 documents
GET https://www.workable.com/api/accounts/{subdomain}?details=true
Verified live (2026-10-12): that URL answers HTTP 302 and redirects to
GET https://apply.workable.com/api/v1/widget/accounts/{subdomain}?details=true
which serves the same payload directly, and answers 404 for an unknown account (whereas the
documented URL redirects an unknown account to the marketing site). The adapter therefore calls
the widget endpoint itself: same data, one request instead of two, a clean 404 for dead slugs.
Public and keyless.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from jobhunt.errors import InvalidResponseError
from jobhunt.http import HttpClient
from jobhunt.models import AtsType, CompanyRef, EmploymentType, FetchResult, NormalizedJob
from jobhunt.normalize import clean_text, html_to_text
from jobhunt.sources.base import FetchContext, JobSource, as_dict, as_list, collect_jobs, join_unique, load_json
from jobhunt.timeutil import to_utc_iso


class WorkableSource(JobSource):
    name = "workable"
    ats_type = AtsType.WORKABLE
    api_base = "https://apply.workable.com/api/v1/widget/accounts"

    def fetch_company(self, company: CompanyRef, http: HttpClient, context: FetchContext) -> FetchResult:
        url = f"{self.api_base}/{quote(company.slug, safe='')}"
        response = http.get(url, params={"details": "true"})
        payload = load_json(response, what=f"workable account '{company.slug}'")
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise InvalidResponseError(f"workable account '{company.slug}': missing 'jobs' list")

        account_name = clean_text(payload.get("name")) or company.name
        jobs, malformed = collect_jobs(payload["jobs"], lambda item: self._to_job(item, company, account_name))
        return FetchResult(jobs=jobs, company_name=account_name, malformed_items=malformed, total_reported=len(payload["jobs"]))

    def _to_job(self, item: Any, company: CompanyRef, account_name: str) -> NormalizedJob | None:
        if not isinstance(item, dict):
            return None
        title = clean_text(item.get("title"))
        job_id = clean_text(item.get("shortcode")) or clean_text(item.get("code"))
        if not title or not job_id:
            return None

        locations = [as_dict(entry) for entry in as_list(item.get("locations"))]
        visible = [entry for entry in locations if not entry.get("hidden")]
        location_parts = [
            ", ".join(part for part in (clean_text(e.get("city")), clean_text(e.get("region")), clean_text(e.get("country"))) if part)
            for e in visible
        ]
        if not any(location_parts):
            location_parts = [
                ", ".join(part for part in (clean_text(item.get("city")), clean_text(item.get("state")), clean_text(item.get("country"))) if part)
            ]
        primary = visible[0] if visible else {}
        country_code = clean_text(primary.get("countryCode"))

        description = "\n\n".join(
            text for text in (
                html_to_text(item.get("description")),
                html_to_text(item.get("requirements")),
                html_to_text(item.get("benefits")),
            ) if text
        )
        url = clean_text(item.get("url")) or clean_text(item.get("shortlink")) or f"https://apply.workable.com/j/{job_id}"

        return NormalizedJob(
            source=self.name,
            source_job_id=job_id,
            title=title,
            company_name=account_name,
            apply_url=url,
            source_url=url,
            location_raw=join_unique(location_parts),
            country=country_code.upper() if country_code and len(country_code) == 2 else None,
            city=clean_text(primary.get("city")),
            is_remote=True if item.get("telecommuting") is True else None,
            employment_type=_employment_type(clean_text(item.get("employment_type"))),
            description_text=description or None,
            needs_description=not description,
            posted_at=to_utc_iso(item.get("published_on")) or to_utc_iso(item.get("created_at")),
            raw={key: value for key, value in item.items() if key not in {"description", "requirements", "benefits", "full_description"}},
        )


def _employment_type(label: str | None) -> EmploymentType:
    text = (label or "").lower()
    if "intern" in text:
        return EmploymentType.INTERNSHIP
    if "part" in text:
        return EmploymentType.PART_TIME
    if "contract" in text or "temp" in text:
        return EmploymentType.CONTRACT
    if "full" in text:
        return EmploymentType.FULL_TIME
    return EmploymentType.UNKNOWN
