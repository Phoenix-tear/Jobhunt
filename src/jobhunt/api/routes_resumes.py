"""The Resumes page: list every version, show one with what changed, preview inline, download only on request.

Nothing here tailors: tailoring is the explicit `POST /api/jobs/{id}/tailor` (routes_tailor). These endpoints only read,
label (approve / sent / archive) and serve files. `/preview` is inline; only `/download` sends an attachment.
"""

from __future__ import annotations

import csv
import io
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from jobhunt.api.database import ConfigDep, ReadDb, WriteDb
from jobhunt.db import transaction
from jobhunt.tailoring import resumes
from jobhunt.timeutil import to_iso, utc_now

router = APIRouter(prefix="/api", tags=["resumes"])


class ResumeItem(BaseModel):
    id: int
    resume_number: int | None
    code: str
    version: int | None
    job_id: int
    job_title: str
    company_name: str
    apply_url: str | None
    created_at: str
    base: str
    recipe: str | None
    summary_variant: str | None
    fill_ratio: float | None
    page_count: int | None
    validator_status: str | None
    advice_source: str
    model: str
    cost_usd: float
    run_status: str
    lifecycle: str
    sent_at: str | None
    archived: bool
    has_pdf: bool
    file_note: str | None
    application_id: int | None
    application_stage: str | None
    parent_resume_id: int | None
    stale_profile: bool
    tailoring_strength: dict[str, Any] = {}


class ResumeDetail(ResumeItem):
    change_summary: dict[str, Any]
    change_text: list[str]
    diff_vs_previous: str | None
    keywords_matched: list[str]
    keywords_missing: list[str]
    validation: dict[str, Any]
    message: str
    dropped: list[str]
    has_tex: bool
    pdf_path: str | None
    download_name: str
    how_made: str
    profile_hash_recorded: bool


class ResumeList(BaseModel):
    total: int
    items: list[ResumeItem]
    counts: dict[str, int]


class TailorEstimate(BaseModel):
    job_id: int
    model: str
    estimated_usd: float
    interactive_calls_today: int
    interactive_call_cap: int
    blocked: bool
    message: str
    existing_versions: int
    will_use_model: bool


class SentOut(BaseModel):
    application_id: int
    sent_at: str


class ArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    archived: bool = True


def _filters(company: str, status: str, base: str, since: str, job_id: int | None, include_superseded: bool,
             include_archived: bool) -> resumes.ResumeFilters:
    return resumes.ResumeFilters(company=company.strip()[:80], lifecycle=status, base=base, since=since[:10], job_id=job_id,
                                 include_superseded=include_superseded, include_archived=include_archived)


class SkillGapItem(BaseModel):
    concept: str
    label: str
    jobs: int
    examples: list[str]


class SkillGaps(BaseModel):
    jobs_checked: int
    job_limit: int
    concepts_on_your_facts: int
    items: list[SkillGapItem]
    note: str


@router.get("/skill-gaps", response_model=SkillGaps,
            summary="Top job concepts missing from your ticked facts (read-only; adds nothing to any resume)")
def skill_gaps(db: ReadDb, config: ConfigDep, top: Annotated[int, Query(ge=1, le=50)] = 25,
               refresh: bool = False) -> SkillGaps:
    from jobhunt.tailoring.engine import load_bases
    from jobhunt.tailoring.strength import skill_gaps as compute

    bases = load_bases(config)
    texts = [f.text for f in bases.merged if bases.ticked.get(f.id) and f.kind not in {"contact", "summary"}]
    return SkillGaps(**compute(db, texts, top=top, use_cache=not refresh))


@router.get("/resumes", response_model=ResumeList, summary="Every resume version (newest first)")
def list_resumes(
    db: ReadDb, config: ConfigDep, company: Annotated[str, Query(max_length=80)] = "",
    status: Annotated[str, Query(pattern="^(|draft|approved|sent|superseded)$")] = "",
    base: Annotated[str, Query(pattern="^(|ai|fullstack)$")] = "", since: Annotated[str, Query(max_length=10)] = "",
    job_id: int | None = None, include_superseded: bool = False, include_archived: bool = False,
) -> ResumeList:
    return ResumeList(**resumes.list_resumes(
        db, config, _filters(company, status, base, since, job_id, include_superseded, include_archived)))


@router.get("/resumes/export.csv", summary="The Resumes list as CSV")
def export_csv(db: ReadDb, config: ConfigDep) -> Response:
    data = resumes.list_resumes(db, config, resumes.ResumeFilters(include_superseded=True, include_archived=True))
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["resume", "version", "job_id", "company", "title", "created_at", "base", "recipe", "summary_variant",
                     "fill_pct", "pages", "validator", "advice", "model", "cost_usd", "status", "sent_at", "archived",
                     "tracker_stage", "apply_url", "pdf_available"])
    for item in data["items"]:
        def cell(value: object) -> str:
            text = "" if value is None else str(value)
            return "'" + text if text[:1] in ("=", "+", "-", "@") else text      # spreadsheet formula guard

        writer.writerow([cell(v) for v in (
            item["code"], f"v{item['version']}", item["job_id"], item["company_name"], item["job_title"], item["created_at"],
            item["base"], item["recipe"], item["summary_variant"],
            round(item["fill_ratio"] * 100) if item["fill_ratio"] is not None else "", item["page_count"],
            item["validator_status"], item["advice_source"], item["model"], f"{item['cost_usd']:.4f}", item["lifecycle"],
            item["sent_at"], int(item["archived"]), item["application_stage"], item["apply_url"], int(item["has_pdf"]))])
    return Response(buffer.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="jobhunt-resumes.csv"'})


@router.get("/resumes/{resume_id}", response_model=ResumeDetail, summary="One version with what changed vs the base")
def get_resume(resume_id: int, db: ReadDb, config: ConfigDep) -> ResumeDetail:
    detail = resumes.resume_detail(db, config, resume_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="resume not found")
    return ResumeDetail(**detail)


def _pdf_path(db: ReadDb, config: ConfigDep, resume_id: int):  # noqa: ANN202
    from jobhunt.api.routes_tailor import _file

    return _file(db, resume_id, "pdf_path", config)


@router.get("/resumes/{resume_id}/preview", summary="The PDF, inline (shown in the page, never downloaded)")
def preview(resume_id: int, db: ReadDb, config: ConfigDep) -> FileResponse:
    path = _pdf_path(db, config, resume_id)
    row = db.execute("""SELECT t.resume_number, j.company_name, j.title FROM tailored_resumes t
                        JOIN jobs j ON j.id = t.job_id WHERE t.id = ?""", (resume_id,)).fetchone()
    return FileResponse(path, media_type="application/pdf", content_disposition_type="inline",
                        filename=resumes.download_filename(row["resume_number"], row["company_name"], row["title"]))


@router.get("/resumes/{resume_id}/download", summary="Download the PDF (only on an explicit click)")
def download(resume_id: int, db: ReadDb, config: ConfigDep) -> FileResponse:
    path = _pdf_path(db, config, resume_id)
    row = db.execute("""SELECT t.resume_number, j.company_name, j.title FROM tailored_resumes t
                        JOIN jobs j ON j.id = t.job_id WHERE t.id = ?""", (resume_id,)).fetchone()
    return FileResponse(path, media_type="application/pdf", content_disposition_type="attachment",
                        filename=resumes.download_filename(row["resume_number"], row["company_name"], row["title"]))


@router.post("/resumes/{resume_id}/sent", response_model=SentOut, summary="Mark as sent and attach to the tracker")
def mark_sent(resume_id: int, db: WriteDb) -> SentOut:
    try:
        with transaction(db):
            return SentOut(**resumes.mark_sent(db, resume_id))
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/resumes/{resume_id}/archive", summary="Hide a version from the list (files stay on disk)")
def archive(resume_id: int, payload: ArchiveRequest, db: WriteDb) -> dict[str, Any]:
    try:
        with transaction(db):
            resumes.set_archived(db, resume_id, payload.archived)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"id": resume_id, "archived": payload.archived, "at": to_iso(utc_now())}


@router.get("/jobs/{job_id}/tailor-estimate", response_model=TailorEstimate,
            summary="What a tailor / re-tailor click would cost (no model call)")
def tailor_estimate(job_id: int, db: ReadDb, config: ConfigDep) -> TailorEstimate:
    if db.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="job not found")
    return TailorEstimate(**resumes.tailor_estimate(db, config, job_id))
