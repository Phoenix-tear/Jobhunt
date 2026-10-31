"""Save / Applied / Not applying / Undo for a job (and Not applying in bulk). Never tailors, never calls a model."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from jobhunt import decisions
from jobhunt.api.database import WriteDb
from jobhunt.db import transaction
from jobhunt.errors import JobHuntError

router = APIRouter(prefix="/api", tags=["decisions"])

HowLiteral = Literal["company_site", "linkedin", "referral", "email", "other"]


class AppliedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied_at: str | None = Field(default=None, max_length=40)
    how: HowLiteral = "company_site"
    resume_id: int | None = None
    note: str | None = Field(default=None, max_length=1000)


class NotApplyingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(max_length=60)
    note: str | None = Field(default=None, max_length=1000)


class BulkNotApplying(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_ids: list[int] = Field(min_length=1, max_length=200)
    reason: str = Field(max_length=60)
    note: str | None = Field(default=None, max_length=1000)


class DecisionOut(BaseModel):
    job_id: int
    action: str
    reason: str | None = None
    application_id: int | None = None
    applied_at: str | None = None
    follow_up_at: str | None = None
    reverted: str | None = None


def _run(db: Any, request: Request, call: Any) -> DecisionOut:
    try:
        with transaction(db):
            result = call()
    except decisions.DecisionError as error:
        raise HTTPException(status_code=404 if "not found" in str(error) else 422, detail=str(error)) from error
    except JobHuntError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    request.app.state.facet_cache.clear()
    return DecisionOut(**result)


@router.post("/jobs/{job_id}/decision/save", response_model=DecisionOut, summary="Save a job (tracker: Saved)")
def save(job_id: int, db: WriteDb, request: Request) -> DecisionOut:
    return _run(db, request, lambda: decisions.save(db, job_id))


@router.post("/jobs/{job_id}/decision/applied", response_model=DecisionOut, summary="I applied")
def applied(job_id: int, payload: AppliedRequest, db: WriteDb, request: Request) -> DecisionOut:
    return _run(db, request, lambda: decisions.apply(
        db, job_id, applied_at=payload.applied_at, how=payload.how, resume_id=payload.resume_id, note=payload.note))


@router.post("/jobs/{job_id}/decision/not-applying", response_model=DecisionOut, summary="Not applying (one reason)")
def not_applying(job_id: int, payload: NotApplyingRequest, db: WriteDb, request: Request) -> DecisionOut:
    return _run(db, request, lambda: decisions.not_applying(db, job_id, payload.reason, payload.note))


@router.post("/jobs/{job_id}/decision/undo", response_model=DecisionOut, summary="Undo the latest decision")
def undo(job_id: int, db: WriteDb, request: Request) -> DecisionOut:
    return _run(db, request, lambda: decisions.undo(db, job_id))


@router.post("/decisions/not-applying", response_model=list[DecisionOut], summary="Not applying for several jobs at once")
def bulk_not_applying(payload: BulkNotApplying, db: WriteDb, request: Request) -> list[DecisionOut]:
    if payload.reason not in decisions.NOT_APPLYING_REASONS:
        raise HTTPException(status_code=422, detail=f"unknown reason {payload.reason!r}")
    out: list[DecisionOut] = []
    try:
        with transaction(db):
            for job_id in dict.fromkeys(payload.job_ids):
                out.append(DecisionOut(**decisions.not_applying(db, job_id, payload.reason, payload.note)))
    except JobHuntError as error:
        raise HTTPException(status_code=404 if "not found" in str(error) else 422, detail=str(error)) from error
    request.app.state.facet_cache.clear()
    return out
