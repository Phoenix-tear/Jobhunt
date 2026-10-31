"""Domain types shared by adapters, the runner and the persistence helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AtsType(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKABLE = "workable"
    SMARTRECRUITERS = "smartrecruiters"
    RECRUITEE = "recruitee"
    UNKNOWN = "unknown"


class CompanyStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNVERIFIED = "unverified"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    INTERNSHIP = "internship"
    CONTRACT = "contract"
    PART_TIME = "part_time"
    UNKNOWN = "unknown"


class ExperienceLevel(StrEnum):
    INTERN = "intern"
    NEW_GRAD = "new_grad"
    ENTRY = "entry"
    MID = "mid"
    SENIOR = "senior"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CompanyRef:
    """The slice of a `companies` row an adapter needs."""

    id: int
    name: str
    ats_type: str
    slug: str
    region: str | None = None
    status: str = CompanyStatus.UNVERIFIED


@dataclass
class NormalizedJob:
    """One posting in the shape every adapter returns, regardless of source.

    Adapters fill what the source states explicitly. `normalize.enrich_job` then derives
    country/city/remote flag and the classification fields that are still unknown.
    """

    source: str
    source_job_id: str
    title: str
    company_name: str
    apply_url: str | None = None
    source_url: str | None = None
    location_raw: str | None = None
    country: str | None = None
    city: str | None = None
    is_remote: bool | None = None
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    experience_level: ExperienceLevel = ExperienceLevel.UNKNOWN
    graduation_year_hint: int | None = None
    description_text: str | None = None
    needs_description: bool = False
    posted_at: str | None = None  # UTC ISO-8601
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    sponsorship_note: str | None = None
    attribution_text: str | None = None
    raw: dict[str, Any] | None = None
    # Opaque per-source data an adapter may need later (e.g. SmartRecruiters detail URL).
    detail_ref: str | None = None
    company_id: int | None = None


@dataclass
class FetchResult:
    """What one successful, valid fetch of one company board produced."""

    jobs: list[NormalizedJob]
    company_name: str | None = None
    region: str | None = None  # Lever: the host that actually answered
    total_reported: int | None = None  # board total as stated by the API, if any
    malformed_items: int = 0  # items skipped because id/title was unusable
    warnings: list[str] = field(default_factory=list)
    # Feed sources only:
    closed_source_job_ids: set[str] = field(default_factory=set)  # feed says these are no longer open
    cursor: str | None = None   # new value for source_state.cursor_or_etag (etag, rotation, budgets)
    not_modified: bool = False  # conditional GET answered 304: nothing to apply

    @property
    def seen_source_job_ids(self) -> set[str]:
        return {job.source_job_id for job in self.jobs}
