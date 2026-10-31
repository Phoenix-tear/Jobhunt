"""Cover letter and application-answer endpoints (one model call per click, cached by job + question + profile)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from jobhunt.api.database import ConfigDep, ReadDb, WriteDb
from jobhunt.api.schemas import KnockoutOut, KnockoutRequest
from jobhunt.writing.knockouts import ANSWERS_FILE, load_answers, scan_questions
from jobhunt.tailoring.service import make_tailor
from jobhunt.writing.service import WritingResult, WritingService

router = APIRouter(prefix="/api", tags=["writing"])

ExportFormat = Literal["txt", "pdf"]


class CoverLetterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    regenerate: bool = False


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=5, max_length=2000)
    regenerate: bool = False


class WritingOut(BaseModel):
    id: int | None
    job_id: int
    kind: Literal["cover_letter", "answer"]
    question: str | None
    text: str
    cited: list[str]
    validation: dict[str, Any]
    words: int
    source: Literal["model", "outline", "template", "refusal", "user_edit"]
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    message: str
    cached: bool
    version: int
    created_at: str
    rejected_violations: list[dict[str, str]] = []
    pipeline: str = "single"
    stages: list[dict[str, Any]] = []
    plan: dict[str, Any] = {}
    quality: dict[str, Any] = {}
    parent_id: int | None = None
    used_for_export: bool = False
    calls: int = 0


class EditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=20, max_length=6000)


class EstimateOut(BaseModel):
    calls: int
    max_calls: int
    estimated_usd: float
    max_usd: float
    interactive_used: int
    interactive_limit: int
    interactive_remaining: int
    blocked_reason: str
    model: str
    plan_model: str
    pipeline: str
    voice_samples: bool


def _out(result: WritingResult) -> WritingOut:
    return WritingOut(
        id=result.row_id, job_id=result.job_id, kind=result.kind, question=result.question,  # type: ignore[arg-type]
        text=result.text, cited=result.cited, validation=result.validation, words=result.words,
        source=result.source, model=result.model, input_tokens=result.input_tokens,   # type: ignore[arg-type]
        output_tokens=result.output_tokens, cost_usd=round(result.cost_usd, 5), message=result.message,
        cached=result.cached, version=result.version, created_at=result.created_at,
        rejected_violations=result.rejected_violations, pipeline=result.pipeline, stages=result.stages,
        plan=result.plan, quality=result.quality, parent_id=result.parent_id,
        used_for_export=result.used_for_export, calls=result.calls)


def _service(db: Any, config: Any) -> tuple[WritingService, Any]:
    tailor, closer = make_tailor(db, config, use_llm=True)
    return WritingService(db, config, tailor), closer


def _require_job(db: Any, job_id: int) -> None:
    if db.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="job not found")


@router.post("/jobs/{job_id}/knockouts", response_model=list[KnockoutOut],
             summary="Check pasted form questions for possible knock-outs (no model)")
def knockouts(job_id: int, payload: KnockoutRequest, db: WriteDb, config: ConfigDep) -> list[KnockoutOut]:
    _require_job(db, job_id)
    from jobhunt.profile import get_profile
    p = get_profile(config)
    answers_path = config.resolve_path(p.paths.application_answers) if p else config.resolve_path(ANSWERS_FILE)
    answers = load_answers(answers_path, create_template=True)
    return [KnockoutOut(**item) for item in scan_questions(payload.questions, answers)]


@router.get("/jobs/{job_id}/writing", response_model=list[WritingOut], summary="Saved cover letters and answers")
def list_writing(job_id: int, db: ReadDb) -> list[WritingOut]:
    rows = db.execute("SELECT * FROM generated_texts WHERE job_id = ? ORDER BY id DESC LIMIT 60", (job_id,)).fetchall()
    return [_out(WritingService._from_row(row)) for row in rows]


@router.get("/jobs/{job_id}/writing/estimate", response_model=EstimateOut,
            summary="What one cover-letter or answer click would cost (no model call)")
def estimate(job_id: int, db: ReadDb, config: ConfigDep, kind: Literal["cover_letter", "answer"] = "cover_letter") -> EstimateOut:
    _require_job(db, job_id)
    service, closer = _service(db, config)
    try:
        return EstimateOut(**service.estimate(job_id, kind))
    finally:
        if closer is not None:
            closer.close()


@router.post("/writing/{text_id}/edit", response_model=WritingOut,
             summary="Save my own edit of a text as a new version (no model call)")
def save_edit(text_id: int, payload: EditRequest, db: WriteDb, config: ConfigDep) -> WritingOut:
    service, closer = _service(db, config)
    try:
        return _out(service.save_edit(text_id, payload.text))
    except LookupError as error:
        raise HTTPException(status_code=404, detail="text not found") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        if closer is not None:
            closer.close()


@router.post("/writing/{text_id}/use", response_model=WritingOut, summary="Use this version for export and copy")
def use_version(text_id: int, db: WriteDb, config: ConfigDep) -> WritingOut:
    service, closer = _service(db, config)
    try:
        return _out(service.use_for_export(text_id))
    except LookupError as error:
        raise HTTPException(status_code=404, detail="text not found") from error
    finally:
        if closer is not None:
            closer.close()


@router.post("/jobs/{job_id}/cover-letter", response_model=WritingOut, summary="Write a cover letter (one call)")
def cover_letter(job_id: int, payload: CoverLetterRequest, db: WriteDb, config: ConfigDep) -> WritingOut:
    _require_job(db, job_id)
    service, closer = _service(db, config)
    try:
        return _out(service.cover_letter(job_id, regenerate=payload.regenerate))
    finally:
        if closer is not None:
            closer.close()


@router.post("/jobs/{job_id}/answer", response_model=WritingOut, summary="Answer an application question (one call)")
def answer(job_id: int, payload: AnswerRequest, db: WriteDb, config: ConfigDep) -> WritingOut:
    _require_job(db, job_id)
    service, closer = _service(db, config)
    try:
        return _out(service.answer(job_id, payload.question, regenerate=payload.regenerate))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        if closer is not None:
            closer.close()


@router.get("/writing/{text_id}/export", summary="Download a text as .txt or .pdf")
def export(text_id: int, db: WriteDb, config: ConfigDep, format: ExportFormat = "txt") -> FileResponse:  # noqa: A002
    if db.execute("SELECT 1 FROM generated_texts WHERE id = ?", (text_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="text not found")
    service, closer = _service(db, config)
    try:
        path = service.export(text_id, format)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    finally:
        if closer is not None:
            closer.close()
    root = config.resolve_path("data/cover_letters").resolve()
    if root not in Path(path).resolve().parents:
        raise HTTPException(status_code=404, detail="file not found")
    media = "application/pdf" if format == "pdf" else "text/plain; charset=utf-8"
    return FileResponse(path, media_type=media, filename=f"cover-letter-{text_id}.{format}")

