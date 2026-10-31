"""Recruitee Careers Site API.

GET https://{company}.recruitee.com/api/offers/
Works keyless today. Per the official docs (docs.recruitee.com/reference/authentication-1) every
request must carry a per-company `X-Careers-Sites-Token` from 2027-02-10, and only the company
itself can mint that token, so this source will stop working for third parties then. HTTP
401/403 is surfaced as AuthRequiredError (source blocked), never as a dead slug.
"""

from __future__ import annotations

import re
from typing import Any

from jobhunt.errors import InvalidResponseError, NotFoundError
from jobhunt.http import HttpClient
from jobhunt.models import AtsType, CompanyRef, EmploymentType, FetchResult, NormalizedJob
from jobhunt.normalize import annualize_salary, clean_text, html_to_text, to_number
from jobhunt.sources.base import FetchContext, JobSource, as_dict, collect_jobs, load_json
from jobhunt.timeutil import to_utc_iso

_SUBDOMAIN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


class RecruiteeSource(JobSource):
    name = "recruitee"
    ats_type = AtsType.RECRUITEE

    def fetch_company(self, company: CompanyRef, http: HttpClient, context: FetchContext) -> FetchResult:
        if not _SUBDOMAIN.match(company.slug):  # the slug becomes part of a hostname: validate it
            raise NotFoundError(f"recruitee slug '{company.slug}' is not a valid subdomain")
        response = http.get(f"https://{company.slug}.recruitee.com/api/offers/")
        payload = load_json(response, what=f"recruitee company '{company.slug}'")
        if not isinstance(payload, dict) or not isinstance(payload.get("offers"), list):
            raise InvalidResponseError(f"recruitee company '{company.slug}': missing 'offers' list")

        published = [
            offer for offer in payload["offers"]
            if not (isinstance(offer, dict) and offer.get("status") not in (None, "published"))
        ]
        jobs, malformed = collect_jobs(published, lambda item: self._to_job(item, company))
        company_name = next(
            (name for offer in payload["offers"] if isinstance(offer, dict) and (name := clean_text(offer.get("company_name")))),
            None,
        )
        return FetchResult(jobs=jobs, company_name=company_name, malformed_items=malformed, total_reported=len(payload["offers"]))

    def _to_job(self, item: Any, company: CompanyRef) -> NormalizedJob | None:
        if not isinstance(item, dict) or item.get("id") is None:
            return None
        title = clean_text(item.get("title"))
        if not title:
            return None
        job_id = str(item["id"])

        description = "\n\n".join(
            text for text in (html_to_text(item.get("description")), html_to_text(item.get("requirements"))) if text
        )
        offer_slug = clean_text(item.get("slug"))
        url = (
            clean_text(item.get("careers_url"))
            or (f"https://{company.slug}.recruitee.com/o/{offer_slug}" if offer_slug else None)
        )

        is_remote: bool | None = None
        if item.get("remote") is True:
            is_remote = True
        elif item.get("hybrid") is True or item.get("on_site") is True:
            is_remote = False

        salary = as_dict(item.get("salary"))
        salary_min, salary_max = annualize_salary(
            to_number(salary.get("min")), to_number(salary.get("max")), clean_text(salary.get("period"))
        )
        country_code = clean_text(item.get("country_code"))

        return NormalizedJob(
            source=self.name,
            source_job_id=job_id,
            title=title,
            company_name=clean_text(item.get("company_name")) or company.name,
            apply_url=url,
            source_url=url,
            location_raw=clean_text(item.get("location")),
            country=country_code.upper() if country_code and len(country_code) == 2 else None,
            city=clean_text(item.get("city")),
            is_remote=is_remote,
            employment_type=_employment_type(clean_text(item.get("employment_type_code"))),
            description_text=description or None,
            needs_description=not description,
            posted_at=to_utc_iso(item.get("published_at")) or to_utc_iso(item.get("created_at")),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=clean_text(salary.get("currency")) if salary_min or salary_max else None,
            raw={key: value for key, value in item.items() if key not in {"description", "requirements", "translations"}},
        )


def _employment_type(code: str | None) -> EmploymentType:
    text = (code or "").lower()
    if "intern" in text:
        return EmploymentType.INTERNSHIP
    if "part" in text:
        return EmploymentType.PART_TIME
    if any(word in text for word in ("contract", "freelance", "temp")):
        return EmploymentType.CONTRACT
    if "full" in text:
        return EmploymentType.FULL_TIME
    return EmploymentType.UNKNOWN
