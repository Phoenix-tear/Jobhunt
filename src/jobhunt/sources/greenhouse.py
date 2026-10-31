"""Greenhouse Job Board API.

Docs: https://docs.greenhouse.io/job-board.html
GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
Public, keyless. `content=true` adds the description (entity-encoded HTML), departments and
offices. One response carries the whole board (no pagination).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from jobhunt.errors import InvalidResponseError
from jobhunt.http import HttpClient
from jobhunt.models import AtsType, CompanyRef, FetchResult, NormalizedJob
from jobhunt.normalize import clean_text, html_to_text
from jobhunt.sources.base import FetchContext, JobSource, as_dict, collect_jobs, load_json
from jobhunt.timeutil import to_utc_iso


class GreenhouseSource(JobSource):
    name = "greenhouse"
    ats_type = AtsType.GREENHOUSE
    api_base = "https://boards-api.greenhouse.io/v1/boards"

    def fetch_company(self, company: CompanyRef, http: HttpClient, context: FetchContext) -> FetchResult:
        url = f"{self.api_base}/{quote(company.slug, safe='')}/jobs"
        response = http.get(url, params={"content": "true"})
        payload = load_json(response, what=f"greenhouse board '{company.slug}'")
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise InvalidResponseError(f"greenhouse board '{company.slug}': missing 'jobs' list")

        jobs, malformed = collect_jobs(payload["jobs"], lambda item: self._to_job(item, company))
        company_name = next(
            (name for item in payload["jobs"] if isinstance(item, dict) and (name := clean_text(item.get("company_name")))),
            None,
        )
        total = as_dict(payload.get("meta")).get("total")
        return FetchResult(
            jobs=jobs,
            company_name=company_name,
            total_reported=total if isinstance(total, int) else None,
            malformed_items=malformed,
        )

    def _to_job(self, item: Any, company: CompanyRef) -> NormalizedJob | None:
        if not isinstance(item, dict) or item.get("id") is None:
            return None
        title = clean_text(item.get("title"))
        if not title:
            return None
        job_id = str(item["id"])

        location = item.get("location")
        location_raw = clean_text(location.get("name") if isinstance(location, dict) else location)
        description = html_to_text(item.get("content"))
        url = clean_text(item.get("absolute_url")) or f"https://boards.greenhouse.io/{company.slug}/jobs/{job_id}"

        return NormalizedJob(
            source=self.name,
            source_job_id=job_id,
            title=title,
            company_name=clean_text(item.get("company_name")) or company.name,
            apply_url=url,
            source_url=url,
            location_raw=location_raw,
            description_text=description or None,
            needs_description=not description,
            posted_at=to_utc_iso(item.get("first_published")) or to_utc_iso(item.get("updated_at")),
            raw={key: value for key, value in item.items() if key != "content"},  # description is stored separately
        )
