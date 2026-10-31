"""Curated GitHub lists maintained by SimplifyJobs, read from their machine-readable listings.json.

Verified 2026-10-12:
* path is `.github/scripts/listings.json` on branch `dev` (404 on `main`)
* `Summer2027-Internships` ships ONE file for every internship term; the "off-season" README is a
  rendering of the same data (terms such as "Fall 2026", "Winter 2027"). There is no second file,
  so one source covers summer and off-season; narrow it with the `terms` option.
* the files are ~13 MB and hold the full history (about a quarter of entries are `active`)
  -> conditional requests (ETag in source_state) make an unchanged list nearly free
* scope: mostly US, Canada and remote roles
* GitHub reports no license for either repository: the data is read for personal use only,
  never republished (the database is gitignored), and every sighting carries attribution.

Entry shape: id, title, company_name, url, locations[], active, is_visible, date_posted,
date_updated (epoch seconds), sponsorship ("Offers Sponsorship" | "Does Not Offer Sponsorship" |
"U.S. Citizenship is Required" | "Other"), degrees[], category, terms[] (internships only).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from typing import Any, ClassVar

from jobhunt.errors import InvalidResponseError
from jobhunt.http import HttpClient
from jobhunt.models import EmploymentType, ExperienceLevel, FetchResult, NormalizedJob
from jobhunt.normalize import clean_text
from jobhunt.sources.base import FeedContext, FeedSource, as_dict, as_list
from jobhunt.timeutil import coerce_timestamp, to_utc_iso

logger = logging.getLogger(__name__)

UNINFORMATIVE_SPONSORSHIP = {"other", "unknown", ""}


class _SimplifyList(FeedSource):
    repo: ClassVar[str]
    branch: ClassVar[str] = "dev"
    listings_path: ClassVar[str] = ".github/scripts/listings.json"
    default_employment: ClassVar[EmploymentType]
    default_level: ClassVar[ExperienceLevel]
    min_interval_hours = 12.0
    stale_after_days = 90         # closures normally come from the `active` flag
    stale_list_days = 30          # warn when the repository itself looks abandoned

    @property
    def raw_url(self) -> str:
        return f"https://raw.githubusercontent.com/{self.repo}/{self.branch}/{self.listings_path}"

    def fetch(self, http: HttpClient, context: FeedContext) -> FetchResult:
        warnings: list[str] = []
        stale_warning = self._staleness_warning(http, context)
        if stale_warning:
            warnings.append(stale_warning)

        if not context.budget.take():
            return FetchResult(jobs=[], warnings=[*warnings, f"{self.name}: fetch cap reached"], not_modified=True)
        headers = {"Accept": "application/json"}
        signature = self._filter_signature()
        stored_etag, _, stored_signature = (context.cursor or "").rpartition("|")
        if stored_etag and stored_signature == signature:   # changed filters invalidate the cache
            headers["If-None-Match"] = stored_etag
        response = http.get(self.raw_url, headers=headers)
        if response.status_code == 304:
            return FetchResult(jobs=[], warnings=warnings, not_modified=True, cursor=context.cursor)
        if response.status_code == 404:
            raise InvalidResponseError(f"{self.name}: {self.raw_url} not found (list moved?)")
        if response.status_code != 200:
            raise InvalidResponseError(f"{self.name}: unexpected HTTP {response.status_code}")
        try:
            entries = response.json()
        except ValueError as error:
            raise InvalidResponseError(f"{self.name}: listings.json is not valid JSON") from error
        if not isinstance(entries, list):
            raise InvalidResponseError(f"{self.name}: expected a JSON array of listings")

        terms_filter = {str(term).lower() for term in self.option("terms", [])}
        categories_filter = {str(category).lower() for category in self.option("categories", [])}
        jobs: dict[str, NormalizedJob] = {}
        closed: set[str] = set()
        malformed = 0
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("id") or not clean_text(entry.get("title")):
                malformed += 1
                continue
            source_job_id = str(entry["id"])
            if not entry.get("active") or entry.get("is_visible") is False:
                closed.add(source_job_id)
                continue
            # Entries the configured filters reject are reported as closed, so tightening the
            # filters also closes what we already hold (and loosening them reopens it).
            if terms_filter and not terms_filter & {str(t).lower() for t in as_list(entry.get("terms"))}:
                closed.add(source_job_id)
                continue
            if categories_filter and str(entry.get("category", "")).lower() not in categories_filter:
                closed.add(source_job_id)
                continue
            job = self._to_job(entry)
            if job is None:
                malformed += 1
            else:
                jobs.setdefault(job.source_job_id, job)
        return FetchResult(
            jobs=list(jobs.values()), closed_source_job_ids=closed - set(jobs), malformed_items=malformed,
            warnings=warnings,
            cursor=f"{response.headers['etag']}|{signature}" if response.headers.get("etag") else None,
        )

    def _filter_signature(self) -> str:
        parts = sorted(str(term).lower() for term in self.option("terms", []))
        parts += sorted("c:" + str(category).lower() for category in self.option("categories", []))
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]

    def _staleness_warning(self, http: HttpClient, context: FeedContext) -> str | None:
        """Warn when the newest commit touching listings.json is older than `stale_list_days`."""
        if not context.budget.take():
            return None
        url = f"https://api.github.com/repos/{self.repo}/commits"
        try:
            response = http.get(
                url, params={"path": self.listings_path, "sha": self.branch, "per_page": 1},
                headers={"Accept": "application/vnd.github+json"},
            )
            commits = response.json() if response.status_code == 200 else None
            moment = coerce_timestamp(as_dict(as_dict(commits[0]).get("commit")).get("committer", {}).get("date")) if commits else None
        except Exception as error:  # a rate-limited GitHub API must never fail the ingest itself
            logger.debug("%s: commit date check failed: %s", self.name, error)
            return None
        if moment is None:
            return None
        age = context.now - moment
        if age > timedelta(days=self.stale_list_days):
            message = f"{self.name}: list looks stale, last commit {age.days} days ago ({self.repo})"
            logger.warning(message)
            return message
        return None

    def _to_job(self, entry: dict[str, Any]) -> NormalizedJob | None:
        title, url = clean_text(entry.get("title")), clean_text(entry.get("url"))
        company = clean_text(entry.get("company_name"))
        if not title or not url or not company:
            return None
        sponsorship = clean_text(entry.get("sponsorship"))
        location_parts = [clean_text(location) for location in as_list(entry.get("locations"))]
        return NormalizedJob(
            source=self.name, source_job_id=str(entry["id"]), title=title, company_name=company,
            apply_url=url, source_url=url,
            location_raw="; ".join(dict.fromkeys(part for part in location_parts if part)) or None,
            employment_type=self.default_employment, experience_level=self.default_level,
            needs_description=True,  # the lists carry no description text
            posted_at=to_utc_iso(entry.get("date_posted")),
            sponsorship_note=None if (sponsorship or "").lower() in UNINFORMATIVE_SPONSORSHIP else sponsorship,
            attribution_text=self.attribution,
            raw={key: value for key, value in entry.items() if key not in {"company_url"}},
        )


class SimplifyInternshipsSource(_SimplifyList):
    name = "simplify_internships"
    repo = "SimplifyJobs/Summer2027-Internships"
    attribution = "Curated list by SimplifyJobs (https://github.com/SimplifyJobs/Summer2027-Internships)"
    default_employment = EmploymentType.INTERNSHIP
    default_level = ExperienceLevel.INTERN


class SimplifyNewGradSource(_SimplifyList):
    name = "simplify_newgrad"
    repo = "SimplifyJobs/New-Grad-Positions"
    attribution = "Curated list by SimplifyJobs (https://github.com/SimplifyJobs/New-Grad-Positions)"
    default_employment = EmploymentType.FULL_TIME
    default_level = ExperienceLevel.NEW_GRAD
