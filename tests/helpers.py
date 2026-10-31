"""Small builders shared by the test modules."""

from __future__ import annotations

import sqlite3
from typing import Any

from jobhunt.models import AtsType, CompanyRef, FetchResult, NormalizedJob
from jobhunt.sources.base import FetchContext, JobSource
from jobhunt.http import HttpClient


def insert_company(
    connection: sqlite3.Connection,
    ats_type: str = "greenhouse",
    slug: str = "acme",
    *,
    name: str = "Acme",
    status: str = "active",
    region: str | None = None,
) -> CompanyRef:
    cursor = connection.execute(
        """INSERT INTO companies (name, ats_type, slug, region, status, created_at)
           VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00Z')""",
        (name, ats_type, slug, region, status),
    )
    return CompanyRef(id=int(cursor.lastrowid), name=name, ats_type=ats_type, slug=slug, region=region, status=status)


def make_job(**overrides: Any) -> NormalizedJob:
    values: dict[str, Any] = {
        "source": "greenhouse",
        "source_job_id": "1",
        "title": "Software Engineer",
        "company_name": "Acme",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
        "location_raw": "Bengaluru, India",
        "description_text": "Build things.",
    }
    values.update(overrides)
    return NormalizedJob(**values)


class FakeSource(JobSource):
    """Scripted adapter: each fetch of a slug pops the next scripted outcome (last one repeats)."""

    name = "greenhouse"
    ats_type = AtsType.GREENHOUSE

    def __init__(self) -> None:
        super().__init__()
        self.scripts: dict[str, list[FetchResult | Exception]] = {}
        self.calls: list[str] = []

    def script(self, slug: str, *outcomes: FetchResult | Exception) -> None:
        self.scripts[slug] = list(outcomes)

    def fetch_company(self, company: CompanyRef, http: HttpClient, context: FetchContext) -> FetchResult:
        self.calls.append(company.slug)
        queue = self.scripts[company.slug]
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, Exception):
            raise outcome
        return FetchResult(
            jobs=[_clone(job) for job in outcome.jobs],
            company_name=outcome.company_name,
            region=outcome.region,
            total_reported=outcome.total_reported,
        )


def _clone(job: NormalizedJob) -> NormalizedJob:
    from dataclasses import replace

    return replace(job)
