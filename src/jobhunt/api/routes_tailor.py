"""Resume tailoring endpoints: create, list, download and approve tailored resumes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from jobhunt.api.database import ConfigDep, ReadDb, WriteDb
from jobhunt.db import transaction
from jobhunt.profile_terms import DOWNLOAD_NAME
from jobhunt.tailoring.service import engine_name, make_tailor
from jobhunt.timeutil import to_iso, utc_now

router = APIRouter(prefix="/api", tags=["tailoring"])


class TailorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base: Literal["ai", "fullstack", "backend"] | None = None
    rephrase: bool | None = None
    use_llm: bool = True
    force: bool = False


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool = True


class TailoredOut(BaseModel):
    id: int
    job_id: int
    base_profile: str
    mode: str
    status: str
    model: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    created_at: str
    pages: int
    message: str | None
    approved: bool
    validation: dict[str, Any]
    dropped: list[str]
    info: dict[str, Any] = {}
    has_pdf: bool
    has_tex: bool
    download_name: str
    diff: list[dict[str, str]] = []


def _out(row: Any, config: Any) -> TailoredOut:
    def loads(value: str | None, default: Any) -> Any:
        try:
            return json.loads(value) if value else default
        except ValueError:
            return default

    company = ""
    if row["company_name"]:
        company = re.sub(r"[^A-Za-z0-9]+", "_", row["company_name"]).strip("_")[:30]
    return TailoredOut(
        id=row["id"], job_id=row["job_id"], base_profile=row["base_profile"], mode=row["mode"], status=row["status"],
        model=row["model"], input_tokens=row["input_tokens"], output_tokens=row["output_tokens"],
        cost_usd=row["cost_usd"], created_at=row["created_at"], pages=row["pages"], message=row["message"],
        approved=bool(row["approved"]), validation=loads(row["validation_json"], {}),
        dropped=loads(row["dropped_json"], []), info=loads(row["plan_json"], {}), has_pdf=bool(row["pdf_path"] and Path(row["pdf_path"]).is_file()),
        has_tex=bool(row["tex_path"] and Path(row["tex_path"]).is_file()),
        download_name=f"{DOWNLOAD_NAME}_{company or 'Resume'}",
        diff=_diff(row, config),
    )


def _diff(row: Any, config: Any) -> list[dict[str, str]]:
    """Plain-text change list vs the base: moved / dropped / added / rephrased bullets and skills."""
    if not row["tex_path"] or not Path(row["tex_path"]).is_file():
        return []
    from jobhunt.tailoring.texdoc import parse, plain

    base_path = config.resolve_path(config.profiles.resume_dir) / f"resume_{row['base_profile']}.tex"
    if not base_path.is_file():
        return []
    try:
        base, new = parse(base_path.read_text(encoding="utf-8")), parse(Path(row["tex_path"]).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []

    def bullets(doc: Any) -> list[tuple[str, str]]:
        return [(plain(e.args[0]), plain(b)) for e in (*doc.experience, *doc.projects) for b in e.bullets]

    old_items, new_items = bullets(base), bullets(new)
    old_set, new_set = set(old_items), set(new_items)
    out: list[dict[str, str]] = []
    if plain(base.summary) != plain(new.summary):
        out.append({"kind": "summary", "old": plain(base.summary), "new": plain(new.summary)})
    for entry, text in new_items:
        if (entry, text) not in old_set:
            out.append({"kind": "rephrased_or_added", "entry": entry, "new": text})
    for entry, text in old_items:
        if (entry, text) not in new_set:
            out.append({"kind": "dropped", "entry": entry, "old": text})
    old_projects, new_projects = [plain(e.args[0]) for e in base.projects], [plain(e.args[0]) for e in new.projects]
    for name in old_projects:
        if name not in new_projects:
            out.append({"kind": "project_dropped", "entry": name})
    kept = [n for n in old_projects if n in new_projects]
    if kept != [n for n in new_projects if n in old_projects]:
        out.append({"kind": "projects_reordered", "new": ", ".join(new_projects)})
    old_skills = {plain(s.group): [plain(i) for i in s.items] for s in base.skills}
    for line in new.skills:
        group = plain(line.group)
        items = [plain(i) for i in line.items]
        if group in old_skills and items != old_skills[group]:
            out.append({"kind": "skills", "entry": group, "old": ", ".join(old_skills[group]), "new": ", ".join(items)})
    return out


_SELECT = """SELECT t.*, j.company_name FROM tailored_resumes t JOIN jobs j ON j.id = t.job_id"""


@router.post("/jobs/{job_id}/tailor", response_model=TailoredOut, summary="Tailor a resume to this job")
def tailor_job(job_id: int, payload: TailorRequest, db: WriteDb, config: ConfigDep) -> TailoredOut:
    if db.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="job not found")
    tailor, client = make_tailor(db, config, use_llm=payload.use_llm)
    try:
        result = tailor.tailor(job_id, base=payload.base, rephrase=payload.rephrase, use_llm=payload.use_llm,
                               force=payload.force)
    except LookupError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        if client is not None:
            client.close()
    row = db.execute(f"{_SELECT} WHERE t.id = ?", (result.row_id,)).fetchone()
    return _out(row, config)


@router.get("/jobs/{job_id}/tailored", response_model=list[TailoredOut], summary="Tailored versions (newest first)")
def list_tailored(job_id: int, db: ReadDb, config: ConfigDep) -> list[TailoredOut]:
    rows = db.execute(f"{_SELECT} WHERE t.job_id = ? ORDER BY t.id DESC LIMIT 20", (job_id,)).fetchall()
    return [_out(row, config) for row in rows]


def _file(db: Any, tailored_id: int, column: str, config: Any) -> Path:
    row = db.execute(f"SELECT {column} AS path FROM tailored_resumes WHERE id = ?", (tailored_id,)).fetchone()
    if row is None or not row["path"]:
        raise HTTPException(status_code=404, detail="file not found")
    path = Path(row["path"]).resolve()
    root = config.resolve_path("data/tailored").resolve()
    if root not in path.parents or not path.is_file():      # only ever serve files inside data/tailored
        raise HTTPException(status_code=404, detail="file not found")
    return path


@router.get("/tailored/{tailored_id}/pdf", summary="The tailored PDF")
def get_pdf(tailored_id: int, db: ReadDb, config: ConfigDep) -> FileResponse:
    # inline: the browser shows it (preview). Only the /download endpoint below ever sends an attachment.
    path = _file(db, tailored_id, "pdf_path", config)
    row = db.execute("""SELECT t.resume_number, j.company_name, j.title FROM tailored_resumes t
                        JOIN jobs j ON j.id = t.job_id WHERE t.id = ?""", (tailored_id,)).fetchone()
    from jobhunt.tailoring.resumes import download_filename

    return FileResponse(path, media_type="application/pdf", content_disposition_type="inline",
                        filename=download_filename(row["resume_number"], row["company_name"], row["title"]))


@router.get("/tailored/{tailored_id}/download", summary="Download the tailored PDF (only on an explicit click)")
def download_pdf(tailored_id: int, db: ReadDb, config: ConfigDep) -> FileResponse:
    path = _file(db, tailored_id, "pdf_path", config)
    row = db.execute("""SELECT t.resume_number, j.company_name, j.title FROM tailored_resumes t
                        JOIN jobs j ON j.id = t.job_id WHERE t.id = ?""", (tailored_id,)).fetchone()
    from jobhunt.tailoring.resumes import download_filename

    return FileResponse(path, media_type="application/pdf", content_disposition_type="attachment",
                        filename=download_filename(row["resume_number"], row["company_name"], row["title"]))


@router.get("/tailored/{tailored_id}/tex", summary="The tailored LaTeX source")
def get_tex(tailored_id: int, db: ReadDb, config: ConfigDep) -> FileResponse:
    return FileResponse(_file(db, tailored_id, "tex_path", config), media_type="text/plain; charset=utf-8",
                        filename=f"resume-{tailored_id}.tex")


@router.post("/tailored/{tailored_id}/approve", response_model=TailoredOut, summary="Approve (or un-approve) a version")
def approve(tailored_id: int, payload: ApproveRequest, db: WriteDb, config: ConfigDep) -> TailoredOut:
    with transaction(db):
        row = db.execute("SELECT job_id, status FROM tailored_resumes WHERE id = ?", (tailored_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="tailored resume not found")
        if payload.approved and row["status"] == "failed":
            raise HTTPException(status_code=409, detail="a failed version cannot be approved")
        if payload.approved:        # one approved version per job
            db.execute("UPDATE tailored_resumes SET approved = 0, approved_at = NULL WHERE job_id = ?", (row["job_id"],))
        db.execute("UPDATE tailored_resumes SET approved = ?, approved_at = ? WHERE id = ?",
                   (1 if payload.approved else 0, to_iso(utc_now()) if payload.approved else None, tailored_id))
        from jobhunt.tailoring.resumes import set_lifecycle_after_approval

        set_lifecycle_after_approval(db, row["job_id"])
    full = db.execute(f"{_SELECT} WHERE t.id = ?", (tailored_id,)).fetchone()
    return _out(full, config)


@router.get("/tailoring/status", summary="LaTeX engine and tailoring spend")
def status(db: ReadDb, config: ConfigDep) -> dict[str, Any]:
    today = utc_now().date().isoformat()
    row = db.execute(
        """SELECT COUNT(*) AS runs, COALESCE(SUM(cost_usd), 0) AS usd,
                  COALESCE(SUM(CASE WHEN substr(created_at, 1, 10) = ? THEN 1 ELSE 0 END), 0) AS runs_today,
                  COALESCE(SUM(CASE WHEN substr(created_at, 1, 10) = ? THEN cost_usd ELSE 0 END), 0) AS usd_today,
                  COALESCE(SUM(CASE WHEN status = 'fallback' THEN 1 ELSE 0 END), 0) AS fallbacks,
                  COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failed
           FROM tailored_resumes""", (today, today)).fetchone()
    month = db.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM tailored_resumes WHERE substr(created_at, 1, 7) = ?", (today[:7],)
    ).fetchone()[0]
    llm_runs = db.execute("SELECT COUNT(*) FROM tailored_resumes WHERE model != '' AND model IS NOT NULL").fetchone()[0]
    return {
        "engine": engine_name(config), "runs": row["runs"], "runs_today": row["runs_today"],
        "usd_today": round(row["usd_today"], 4), "usd_month": round(float(month), 4),
        "validator_rejections": row["fallbacks"], "failed": row["failed"], "llm_runs": llm_runs,
        "validator_failure_rate": round(row["fallbacks"] / llm_runs, 3) if llm_runs else 0.0,
    }
