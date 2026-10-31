"""Job browsing endpoints (Spring Boot: a @RestController for /api/jobs)."""

from __future__ import annotations

import re
import time
from typing import Annotated

import sqlite3

from fastapi import APIRouter, HTTPException, Query, Request

from jobhunt import freshness, liveness
from jobhunt.api import queries
from jobhunt.api.database import ConfigDep, ReadDb, WriteDb
from jobhunt.api.manual import add_manual_job
from jobhunt.api.queries import JobFilters
from jobhunt.api.schemas import (
    CategoryKey, EmploymentType, ExperienceLevel, Facets, FeedbackCreate, FeedbackResult, GateStatus, JobDetail, LocationFit, JobPage,
    JobStatus, ManualJob, ManualJobResult, RemoteScope, SortKey, VerdictWrong, WorkplaceType,
)
from jobhunt.config import Config
from jobhunt.db import transaction
from jobhunt.matching.gate import run_relevance
from jobhunt.matching.location import run_location
from jobhunt.matching.triage import clear_human_label, set_human_label
from jobhunt.matching.triage_stats import triage_panel
from jobhunt.matching.feedback import FeedbackError, record_feedback
from jobhunt.timeutil import to_iso, utc_now
from jobhunt.writing.knockouts import ANSWERS_FILE, eligibility_screens, load_answers

router = APIRouter(prefix="/api", tags=["jobs"])

_COUNTRY = re.compile(r"^(?:[A-Za-z]{2}|unknown)$")
_SOURCE = re.compile(r"^[a-z0-9_]{1,40}$")
FACET_TTL_SECONDS = 60
MAX_MULTI_VALUES = 40


def _checked(values: list[str] | None, pattern: re.Pattern[str], name: str, upper: bool = False) -> list[str]:
    cleaned = []
    for value in (values or [])[:MAX_MULTI_VALUES]:
        if not pattern.match(value):
            raise HTTPException(status_code=422, detail=f"invalid {name} value: {value[:30]!r}")
        cleaned.append(value.upper() if upper and value != "unknown" else value)
    return cleaned


@router.get("/jobs", response_model=JobPage, summary="Search and filter jobs")
def list_jobs(
    db: ReadDb,
    q: Annotated[str | None, Query(description="Free text; matched against title, company, location, description.")] = None,
    level: Annotated[list[ExperienceLevel] | None, Query()] = None,
    employment_type: Annotated[list[EmploymentType] | None, Query(description="Effective type: unknown counts as full_time.")] = None,
    country: Annotated[list[str] | None, Query(description="ISO-2 codes, or `unknown`.")] = None,
    remote: bool | None = None,
    source: Annotated[list[str] | None, Query()] = None,
    status: JobStatus = "open",
    hide_senior: bool = True,
    new_within_hours: Annotated[int | None, Query(ge=1, le=24 * 365)] = None,
    needs_description: bool | None = None,
    sort: SortKey = "newest",
    relevant_only: Annotated[bool, Query(description="Hide jobs the relevance gate excluded.")] = True,
    gate_status: Annotated[list[GateStatus] | None, Query(description="Overrides relevant_only when given.")] = None,
    role_family: Annotated[list[str] | None, Query()] = None,
    best_profile: Annotated[str | None, Query(pattern="^(ai|fullstack)$")] = None,
    min_score: Annotated[float | None, Query(ge=0, le=100)] = None,
    workplace_type: Annotated[list[WorkplaceType] | None, Query()] = None,
    remote_scope: RemoteScope | None = None,
    remote_eligible_india: bool | None = None,
    include_dismissed: bool = False,
    junior_only: bool = False,
    location_fit: Annotated[list[LocationFit] | None, Query()] = None,
    include_other_locations: bool = False,
    view: Annotated[str, Query(pattern="^(open|saved|applied|not_applying|auto_hidden|all)$")] = "open",
    category: Annotated[CategoryKey | None, Query(description="relevant | target | maybe | all | filtered_out; "
                                                  "when given it replaces relevant_only / gate_status.")] = None,
    freshness: Annotated[list[str] | None, Query(description="fresh | aging | unknown (stale jobs are in the "
                                                 "auto_hidden view).")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: int = queries.DEFAULT_PAGE_SIZE,
) -> JobPage:
    filters = JobFilters(
        q=q,
        levels=[str(item) for item in (level or [])],
        employment_types=[str(item) for item in (employment_type or [])],
        countries=_checked(country, _COUNTRY, "country", upper=True),
        remote=remote,
        sources=_checked(source, _SOURCE, "source"),
        status=status,
        hide_senior=hide_senior,
        new_within_hours=new_within_hours,
        needs_description=needs_description,
        relevant_only=relevant_only,
        gate_statuses=[str(item) for item in (gate_status or [])],
        role_families=_checked(role_family, _SOURCE, "role_family"),
        best_profile=best_profile,
        min_score=min_score,
        workplace_types=[str(item) for item in (workplace_type or [])],
        remote_scope=remote_scope,
        remote_eligible_india=remote_eligible_india,
        include_dismissed=include_dismissed,
        junior_only=junior_only,
        location_fits=[str(item) for item in (location_fit or [])],
        include_other_locations=include_other_locations,
        view=view,
        category=category,
        freshness=_checked(freshness, re.compile(r"^(?:fresh|aging|unknown)$"), "freshness"),
    )
    page_size = min(max(page_size, 1), queries.MAX_PAGE_SIZE)
    total, ids, applied_sort = queries.list_job_ids(
        db, filters, sort=sort, page=page, page_size=page_size, now=utc_now()
    )
    return JobPage(
        total=total, page=min(page, queries.MAX_PAGE), page_size=page_size, sort=applied_sort,  # type: ignore[arg-type]
        items=queries.fetch_summaries(db, ids),  # type: ignore[arg-type]
    )


@router.get("/jobs/{job_id}", response_model=JobDetail, summary="One job with description and sightings")
def get_job(job_id: int, db: ReadDb, config: ConfigDep) -> JobDetail:
    detail = queries.fetch_job_detail(db, job_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="job not found")
    from jobhunt.profile import get_profile
    p = get_profile(config)
    answers_path = config.resolve_path(p.paths.application_answers) if p else config.resolve_path(ANSWERS_FILE)
    answers = load_answers(answers_path)
    detail["eligibility_screens"] = eligibility_screens(str(detail.get("description_text") or ""), answers)
    detail["triage"] = triage_panel(db, config, job_id)
    return JobDetail(**detail)  # type: ignore[arg-type]


@router.post("/jobs/{job_id}/llm-verdict-wrong", summary="Mark the LLM verdict of a job as wrong (it is then ignored for this job)")
def llm_verdict_wrong(job_id: int, payload: VerdictWrong, db: WriteDb, config: ConfigDep, request: Request) -> dict[str, object]:
    with transaction(db):
        stored = set_human_label(db, job_id, payload.label, payload.reason or "", to_iso(utc_now()))
    if not stored:
        raise HTTPException(status_code=404, detail="this job has no LLM verdict")
    _regate(db, config, request)
    return {"job_id": job_id, "human_label": payload.label}


@router.delete("/jobs/{job_id}/llm-verdict-wrong", summary="Undo 'LLM verdict is wrong' (the verdict applies again)")
def llm_verdict_wrong_undo(job_id: int, db: WriteDb, config: ConfigDep, request: Request) -> dict[str, object]:
    with transaction(db):
        clear_human_label(db, job_id)
    _regate(db, config, request)
    return {"job_id": job_id, "human_label": None}


def _regate(db: sqlite3.Connection, config: Config, request: Request) -> None:
    """Recompute the relevance and location verdicts that depend on LLM verdicts (incremental; no model call)."""
    run_relevance(db, config)
    run_location(db, config)
    request.app.state.facet_cache.clear()


@router.post("/jobs/{job_id}/feedback", response_model=FeedbackResult, summary="Dismiss, like or undo a job")
def post_feedback(job_id: int, payload: FeedbackCreate, db: WriteDb, request: Request) -> FeedbackResult:
    try:
        with transaction(db):
            record_feedback(db, job_id, payload.action, payload.reason)
    except FeedbackError as error:
        raise HTTPException(status_code=404 if "not found" in str(error) else 422, detail=str(error)) from error
    request.app.state.facet_cache.clear()
    row = db.execute("SELECT action, reason FROM job_feedback_current WHERE job_id = ?", (job_id,)).fetchone()
    return FeedbackResult(job_id=job_id, feedback=row["action"] if row else None, reason=row["reason"] if row else None)


@router.post("/jobs/{job_id}/check-open", summary="Check if the posting is still open (one polite request, explicit click)")
def check_open(job_id: int, db: WriteDb, config: ConfigDep, request: Request) -> dict[str, object]:
    fetcher = request.app.state.liveness_fetcher_factory()
    try:
        result = liveness.check_job(db, liveness.LivenessChecker(fetcher, config.freshness.liveness), job_id,
                                    trigger="button")
    finally:
        fetcher.close()
    if result is None:
        raise HTTPException(status_code=404, detail="job not found")
    request.app.state.facet_cache.clear()
    return result


@router.post("/jobs/{job_id}/restore", summary="Restore an auto-hidden job (it is never hidden automatically again)")
def restore_job(job_id: int, db: WriteDb, request: Request) -> dict[str, object]:
    with transaction(db):
        restored = freshness.restore_job(db, job_id, to_iso(utc_now()))
    if not restored:
        raise HTTPException(status_code=404, detail="this job is not auto-hidden")
    request.app.state.facet_cache.clear()
    return {"job_id": job_id, "restored": True}


@router.get("/facets", response_model=Facets, summary="Filter counts over all open jobs (cached briefly)")
def get_facets(db: ReadDb, request: Request) -> Facets:
    cache: dict[str, tuple[float, dict[str, object]]] = request.app.state.facet_cache
    cached = cache.get("facets")
    if cached and time.monotonic() - cached[0] < FACET_TTL_SECONDS:
        return Facets(**cached[1])  # type: ignore[arg-type]
    facets = queries.compute_facets(db, utc_now())
    facets["show_other_locations_default"] = bool(request.app.state.config.location.show_other_locations_default)
    cache["facets"] = (time.monotonic(), facets)
    return Facets(**facets)  # type: ignore[arg-type]


@router.post("/jobs/manual", response_model=ManualJobResult, summary="Add a job found elsewhere")
def create_manual_job(payload: ManualJob, db: WriteDb, config: ConfigDep, request: Request) -> ManualJobResult:
    result = add_manual_job(db, config, payload)
    request.app.state.facet_cache.clear()
    detail = queries.fetch_job_detail(db, result.job_id)
    assert detail is not None
    return ManualJobResult(created=result.created, job=JobDetail(**detail), ats=result.ats)  # type: ignore[arg-type]
