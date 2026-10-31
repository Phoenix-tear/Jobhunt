"""SmartRecruiters Posting API (public postings).

Docs: https://developers.smartrecruiters.com/docs/endpoints
GET https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings?limit=100&offset=N
GET https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings/{postingId}

Behaviors verified live that the docs do not state:
* `limit` is silently capped at 100.
* An unknown company identifier answers HTTP 200 with `totalFound: 0` (not 404), so an empty
  board does NOT prove the company exists (`proves_board_exists` requires totalFound > 0).
* List items carry no description: the detail endpoint is called only for jobs the runner
  says are worth it (prefilter passed, description not already stored), capped per company.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from jobhunt.errors import FetchError, InvalidResponseError
from jobhunt.http import HttpClient
from jobhunt.models import (
    AtsType, CompanyRef, EmploymentType, ExperienceLevel, FetchResult, NormalizedJob,
)
from jobhunt.normalize import clean_text, html_to_text
from jobhunt.sources.base import FetchContext, JobSource, as_dict, as_list, collect_jobs, load_json
from jobhunt.timeutil import to_utc_iso

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
MAX_PAGES = 200

_EXPERIENCE_LEVELS = {
    "internship": ExperienceLevel.INTERN,
    "entry_level": ExperienceLevel.ENTRY,
    "director": ExperienceLevel.SENIOR,
    "executive": ExperienceLevel.SENIOR,
}


class SmartRecruitersSource(JobSource):
    name = "smartrecruiters"
    ats_type = AtsType.SMARTRECRUITERS
    api_base = "https://api.smartrecruiters.com/v1/companies"

    def fetch_company(self, company: CompanyRef, http: HttpClient, context: FetchContext) -> FetchResult:
        listing_url = f"{self.api_base}/{quote(company.slug, safe='')}/postings"
        raw_items: list[Any] = []
        total_reported: int | None = None

        for _ in range(MAX_PAGES):
            response = http.get(listing_url, params={"limit": PAGE_SIZE, "offset": len(raw_items)})
            payload = load_json(response, what=f"smartrecruiters company '{company.slug}'")
            if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
                raise InvalidResponseError(f"smartrecruiters company '{company.slug}': missing 'content' list")
            page = payload["content"]
            reported = payload.get("totalFound")
            total_reported = reported if isinstance(reported, int) else total_reported
            raw_items.extend(page)
            if not page or len(page) < PAGE_SIZE or (total_reported is not None and len(raw_items) >= total_reported):
                break

        jobs, malformed = collect_jobs(raw_items, lambda item: self._to_job(item, company))
        warnings = self._hydrate_descriptions(jobs, http, context)
        company_name = next(
            (name for item in raw_items if isinstance(item, dict)
             and (name := clean_text(as_dict(item.get("company")).get("name")))),
            None,
        )
        return FetchResult(
            jobs=jobs, company_name=company_name, total_reported=total_reported,
            malformed_items=malformed, warnings=warnings,
        )

    def proves_board_exists(self, result: FetchResult) -> bool:
        return bool(result.total_reported and result.total_reported > 0)

    # -- detail ----------------------------------------------------------------------------

    def _hydrate_descriptions(self, jobs: list[NormalizedJob], http: HttpClient, context: FetchContext) -> list[str]:
        warnings: list[str] = []
        wanted = [job for job in jobs if job.detail_ref and context.wants_detail(job)]
        if len(wanted) > context.detail_cap:
            warnings.append(f"detail fetch capped at {context.detail_cap} of {len(wanted)} jobs")
            wanted = wanted[: context.detail_cap]
        for job in wanted:
            try:
                response = http.get(job.detail_ref)  # type: ignore[arg-type]
                detail = as_dict(load_json(response, what=f"smartrecruiters posting {job.source_job_id}"))
            except FetchError as error:
                warnings.append(f"detail {job.source_job_id}: {error}")
                continue  # job stays needs_description=1 and is retried next run
            sections = as_dict(as_dict(detail.get("jobAd")).get("sections"))
            parts = []
            for key in ("jobDescription", "qualifications", "additionalInformation", "companyDescription"):
                section = as_dict(sections.get(key))
                body = html_to_text(section.get("text"))
                if body:
                    heading = clean_text(section.get("title")) or key
                    parts.append(f"{heading}\n{body}")
            description = "\n\n".join(parts)
            if description:
                job.description_text = description
                job.needs_description = False
            posting_url = clean_text(detail.get("postingUrl"))
            if posting_url:
                job.apply_url = job.source_url = posting_url
        return warnings

    # -- mapping ---------------------------------------------------------------------------

    def _to_job(self, item: Any, company: CompanyRef) -> NormalizedJob | None:
        if not isinstance(item, dict) or item.get("id") is None:
            return None
        title = clean_text(item.get("name"))
        if not title:
            return None
        job_id = str(item["id"])
        company_block = as_dict(item.get("company"))
        identifier = clean_text(company_block.get("identifier")) or company.slug

        location = as_dict(item.get("location"))
        country_code = clean_text(location.get("country"))
        is_remote: bool | None = None
        if location.get("remote") is True:
            is_remote = True
        elif location.get("hybrid") is True:
            is_remote = False
        full_location = clean_text(location.get("fullLocation"))
        if full_location:  # "İstanbul, , Turkey" -> "İstanbul, Turkey"
            full_location = ", ".join(part.strip() for part in full_location.split(",") if part.strip())

        url = f"https://jobs.smartrecruiters.com/{quote(identifier, safe='')}/{job_id}"
        experience_id = (clean_text(as_dict(item.get("experienceLevel")).get("id")) or "").lower()
        employment_type = as_dict(item.get("typeOfEmployment"))
        employment_text = clean_text(employment_type.get("label")) or clean_text(employment_type.get("id")) or ""

        return NormalizedJob(
            source=self.name,
            source_job_id=job_id,
            title=title,
            company_name=clean_text(company_block.get("name")) or company.name,
            apply_url=url,
            source_url=url,
            location_raw=full_location or clean_text(location.get("city")),
            country=country_code.upper() if country_code and len(country_code) == 2 else None,
            city=clean_text(location.get("city")),
            is_remote=is_remote,
            employment_type=_employment_type(employment_text),
            experience_level=_EXPERIENCE_LEVELS.get(experience_id, ExperienceLevel.UNKNOWN),
            needs_description=True,
            posted_at=to_utc_iso(item.get("releasedDate")),
            detail_ref=clean_text(item.get("ref")),
            raw=dict(item),
        )


def _employment_type(label: str) -> EmploymentType:
    text = label.lower()
    if "intern" in text:
        return EmploymentType.INTERNSHIP
    if "part" in text:
        return EmploymentType.PART_TIME
    if "contract" in text or "temp" in text:
        return EmploymentType.CONTRACT
    if "full" in text or "permanent" in text:
        return EmploymentType.FULL_TIME
    return EmploymentType.UNKNOWN
