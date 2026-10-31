"""`POST /api/jobs/manual`: add a job found elsewhere.

SSRF rule: the backend never fetches a user-supplied URL. The only network calls possible here are
the *existing ATS adapters*, and only when `extract_ats_reference` recognizes the URL's host as one
of the supported ATS hosts; the adapters then request their own fixed API endpoints using just the
board slug. Any other URL (LinkedIn, a company site...) is stored as a link and never requested.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass

from fastapi import HTTPException

from jobhunt.api.queries import safe_http_url
from jobhunt.api.schemas import AtsFetchResult, ManualJob
from jobhunt.classify import Classifier
from jobhunt.companies import DiscoveredCompany, add_discovered_companies
from jobhunt.config import Config
from jobhunt.db import transaction
from jobhunt.dedupe import canonicalize_url, upsert_jobs
from jobhunt.discovery.patterns import AtsReference, extract_ats_reference
from jobhunt.errors import FetchError, NotFoundError
from jobhunt.http import HttpClient
from jobhunt.locations import LocationParser
from jobhunt.models import CompanyRef, NormalizedJob
from jobhunt.normalize import clean_text, enrich_job, prettify_slug
from jobhunt.sources import build_ats_sources
from jobhunt.sources.base import FetchContext
from jobhunt.timeutil import to_iso, utc_now

logger = logging.getLogger(__name__)


@dataclass
class ManualResult:
    job_id: int
    created: bool
    ats: AtsFetchResult


def add_manual_job(connection: sqlite3.Connection, config: Config, payload: ManualJob) -> ManualResult:
    url = safe_http_url(payload.apply_url.strip())
    canonical = canonicalize_url(url) if url else None
    if url is None or canonical is None:
        raise HTTPException(status_code=422, detail="apply_url must be a valid http(s) URL")

    locations = LocationParser(config.classification.extra_city_countries)
    classifier = Classifier.from_config(config)
    description = payload.description.strip() if payload.description else None
    manual_id = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]
    job = NormalizedJob(
        source="manual", source_job_id=manual_id, title=payload.title, company_name=payload.company,
        apply_url=url, source_url=url, location_raw=payload.location or None,
        description_text=description, needs_description=not description,
        attribution_text="Added manually", raw={"added": "manual"},
    )
    enrich_job(job, locations, classifier)

    now = utc_now()
    with transaction(connection):
        stats = upsert_jobs(connection, [job], company_id=None, seen_at=to_iso(now))
        job_id = _job_id_for(connection, "manual", manual_id)
    created = stats.created == 1

    ats = AtsFetchResult(attempted=False, status="not_applicable")
    reference = extract_ats_reference(url)
    if reference is not None and reference.job_id:
        ats = _resolve_through_ats(connection, config, reference, url, job_id, classifier, locations, payload.fetch_from_ats)
    return ManualResult(job_id=job_id, created=created, ats=ats)


def _job_id_for(connection: sqlite3.Connection, source: str, source_job_id: str) -> int:
    row = connection.execute(
        "SELECT job_id FROM job_sightings WHERE source = ? AND source_job_id = ?", (source, source_job_id)
    ).fetchone()
    assert row is not None
    return int(row["job_id"])


def _resolve_through_ats(
    connection: sqlite3.Connection,
    config: Config,
    reference: AtsReference,
    url: str,
    job_id: int,
    classifier: Classifier,
    locations: LocationParser,
    fetch_from_ats: bool,
) -> AtsFetchResult:
    """Add the company to discovery and, if asked, pull this one posting through the adapter."""
    now = utc_now()
    with transaction(connection):
        add_discovered_companies(
            connection,
            [DiscoveredCompany(reference.ats_type.value, reference.slug, region=reference.region, discovered_via="manual")],
            now,
        )
    if not fetch_from_ats:
        return AtsFetchResult(attempted=False, status="skipped", detail="fetch_from_ats was false")

    adapter = build_ats_sources(config).get(reference.ats_type.value)
    if adapter is None:
        return AtsFetchResult(attempted=False, status="skipped", detail=f"{reference.ats_type.value} is disabled in config.yaml")

    company_row = connection.execute(
        "SELECT id, name, region FROM companies WHERE ats_type = ? AND slug = ?",
        (reference.ats_type.value, reference.slug),
    ).fetchone()
    company = CompanyRef(
        id=int(company_row["id"]) if company_row else 0,
        name=company_row["name"] if company_row else prettify_slug(reference.slug),
        ats_type=reference.ats_type.value, slug=reference.slug,
        region=(company_row["region"] if company_row else None) or reference.region, status="unverified",
    )
    wanted = reference.job_id

    def is_target(posting: NormalizedJob) -> bool:
        return posting.source_job_id == wanted or (posting.raw or {}).get("slug") == wanted

    try:
        with HttpClient(config.http, config.user_agent) as http:
            result = adapter.fetch_company(company, http, FetchContext(wants_detail=is_target, detail_cap=5))
    except NotFoundError as error:
        return AtsFetchResult(attempted=True, status="not_found", detail=str(error)[:200])
    except FetchError as error:
        return AtsFetchResult(attempted=True, status="failed", detail=str(error)[:200])
    except Exception as error:  # an adapter bug must never turn into a 500 for the user
        logger.exception("manual ATS fetch crashed")
        return AtsFetchResult(attempted=True, status="failed", detail=f"{type(error).__name__}: {error}"[:200])

    posting = next((item for item in result.jobs if is_target(item)), None)
    if posting is None:
        return AtsFetchResult(attempted=True, status="not_found", detail="the board no longer lists this posting")

    # Link to the manual job by URL (the user's URL may be a custom careers domain), then merge.
    posting.apply_url = url
    posting.company_id = company.id or None
    enrich_job(posting, locations, classifier)
    with transaction(connection):
        upsert_jobs(connection, [posting], company_id=company.id or None, seen_at=to_iso(utc_now()))
        # The manual sighting stays the primary identity; make sure the company link is recorded.
        if company.id:
            connection.execute("UPDATE jobs SET company_id = COALESCE(company_id, ?) WHERE id = ?", (company.id, job_id))
    return AtsFetchResult(attempted=True, status="fetched", detail=clean_text(posting.title))
