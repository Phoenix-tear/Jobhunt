"""Ashby public job posting API.

Docs: https://developers.ashbyhq.com/docs/public-job-posting-api
GET https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true
Public, keyless, whole board in one response. Notes versus the docs: the live payload carries
a job `id` (the field table omits it) and per-job `compensation`. Jobs with `isListed: false`
are only reachable by direct link and are skipped.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from jobhunt.errors import InvalidResponseError
from jobhunt.http import HttpClient
from jobhunt.models import AtsType, CompanyRef, EmploymentType, FetchResult, NormalizedJob
from jobhunt.normalize import annualize_salary, clean_text, html_to_text, parse_salary_text, to_number
from jobhunt.sources.base import FetchContext, JobSource, as_dict, as_list, collect_jobs, join_unique, load_json
from jobhunt.timeutil import to_utc_iso

_EMPLOYMENT_TYPES = {
    "fulltime": EmploymentType.FULL_TIME,
    "parttime": EmploymentType.PART_TIME,
    "intern": EmploymentType.INTERNSHIP,
    "contract": EmploymentType.CONTRACT,
    "temporary": EmploymentType.CONTRACT,
}


class AshbySource(JobSource):
    name = "ashby"
    ats_type = AtsType.ASHBY
    api_base = "https://api.ashbyhq.com/posting-api/job-board"

    def fetch_company(self, company: CompanyRef, http: HttpClient, context: FetchContext) -> FetchResult:
        url = f"{self.api_base}/{quote(company.slug, safe='')}"
        response = http.get(url, params={"includeCompensation": "true"})
        payload = load_json(response, what=f"ashby board '{company.slug}'")
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise InvalidResponseError(f"ashby board '{company.slug}': missing 'jobs' list")

        listed = [item for item in payload["jobs"] if not (isinstance(item, dict) and item.get("isListed") is False)]
        jobs, malformed = collect_jobs(listed, lambda item: self._to_job(item, company))
        return FetchResult(jobs=jobs, malformed_items=malformed, total_reported=len(payload["jobs"]))

    def _to_job(self, item: Any, company: CompanyRef) -> NormalizedJob | None:
        if not isinstance(item, dict):
            return None
        title = clean_text(item.get("title"))
        job_url = clean_text(item.get("jobUrl"))
        job_id = clean_text(item.get("id")) or (job_url.rstrip("/").rsplit("/", 1)[-1] if job_url else None)
        if not title or not job_id:
            return None

        secondary = [
            clean_text(as_dict(entry).get("location")) for entry in as_list(item.get("secondaryLocations"))
        ]
        location_raw = join_unique([clean_text(item.get("location")), *secondary])

        is_remote: bool | None = None
        workplace = (clean_text(item.get("workplaceType")) or "").lower()
        if item.get("isRemote") is True or workplace == "remote":
            is_remote = True
        elif workplace in {"onsite", "hybrid"}:
            is_remote = False

        description = _description(item)
        salary_min, salary_max, currency = self._salary(item)
        apply_url = job_url or clean_text(item.get("applyUrl"))
        employment = _EMPLOYMENT_TYPES.get((clean_text(item.get("employmentType")) or "").lower(), EmploymentType.UNKNOWN)

        postal = as_dict(as_dict(item.get("address")).get("postalAddress"))
        return NormalizedJob(
            source=self.name,
            source_job_id=job_id,
            title=title,
            company_name=company.name,
            apply_url=apply_url,
            source_url=apply_url,
            location_raw=location_raw or clean_text(postal.get("addressLocality")),
            is_remote=is_remote,
            employment_type=employment,
            description_text=description or None,
            needs_description=not description,
            posted_at=to_utc_iso(item.get("publishedAt")),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            raw={key: value for key, value in item.items() if key not in {"descriptionHtml", "descriptionPlain"}},
        )

    @staticmethod
    def _salary(item: dict[str, Any]) -> tuple[float | None, float | None, str | None]:
        compensation = as_dict(item.get("compensation"))
        components: list[Any] = list(as_list(compensation.get("summaryComponents")))
        for tier in as_list(compensation.get("compensationTiers")):
            components.extend(as_list(as_dict(tier).get("components")))

        for component in components:
            component = as_dict(component)
            if str(component.get("compensationType", "")).lower() != "salary":
                continue
            minimum, maximum = to_number(component.get("minValue")), to_number(component.get("maxValue"))
            if minimum is None and maximum is None:
                continue
            annual_min, annual_max = annualize_salary(minimum, maximum, clean_text(component.get("interval")))
            return annual_min, annual_max, clean_text(component.get("currencyCode"))

        parsed = parse_salary_text(compensation.get("scrapeableCompensationSalarySummary"))
        if parsed:
            return parsed[0], parsed[1], parsed[2]
        return None, None, None


def _description(item: dict[str, Any]) -> str:
    """HTML description if present, otherwise the plain-text one."""
    return html_to_text(item.get("descriptionHtml")) or html_to_text(item.get("descriptionPlain"))
