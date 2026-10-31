"""Base resume upload, inspection, and compilation API (Brief v12).

Endpoints:
- GET  /api/resumes/bases: list configured base resumes with file status and page counts
- POST /api/resumes/upload: upload and validate a .tex base resume file
- POST /api/resumes/validate: run sandboxed Tectonic compilation and section diagnostics
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from jobhunt.api.database import ConfigDep
from jobhunt.resumes.validator import (
    validate_and_save_upload,
    validate_resume_text,
)
from jobhunt.tailoring.latex import page_count
from jobhunt.timeutil import to_iso, utc_now

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/resumes", tags=["resumes_base"])


class ValidateRequest(BaseModel):
    base_id: str | None = None
    tex_content: str | None = None


@router.get("/bases")
def list_base_resumes(config: ConfigDep) -> dict[str, Any]:
    """List configured base resumes, disk existence, page counts, and modification times."""
    resume_dir = config.resolve_path(config.profiles.resume_dir)
    bases = []

    for base_id, filename in config.profiles.files.items():
        tex_path = resume_dir / filename
        exists = tex_path.is_file()
        pages = 0
        modified = None
        pdf_path = resume_dir / f"{tex_path.stem}.pdf"

        if exists:
            try:
                mtime = tex_path.stat().st_mtime
                modified = to_iso(utc_now())
            except Exception:
                pass

        if pdf_path.is_file():
            try:
                pages = page_count(pdf_path)
            except Exception:
                pages = 1

        template_info = None
        if exists:
            try:
                from jobhunt.resumes.templates import analyze_template
                analysis = analyze_template(tex_path.read_text(encoding="utf-8"))
                template_info = {
                    "template": analysis.template,
                    "compiles": analysis.compiles,
                    "parses_facts": analysis.parses_facts,
                    "can_tailor": analysis.can_tailor,
                    "reasons": analysis.reasons,
                }
            except Exception:
                pass

        bases.append({
            "id": base_id,
            "filename": filename,
            "file_path": str(tex_path),
            "exists": exists,
            "pages": pages,
            "last_modified": modified,
            "is_authoritative": (base_id == config.profiles.authoritative),
            "template": template_info,
            "can_tailor": template_info["can_tailor"] if template_info else False,
            "compiles": template_info["compiles"] if template_info else False,
        })

    return {"bases": bases, "resume_dir": str(resume_dir)}


# Module-level job progress tracker for profile-build
_BUILD_PROGRESS: dict[str, dict[str, Any]] = {}


@router.get("/build-progress")
def get_build_progress(job_id: str = "default") -> dict[str, Any]:
    """Poll progress of background profile-build job."""
    progress = _BUILD_PROGRESS.get(job_id)
    if progress is None:
        return {"status": "completed", "progress": 100, "message": "No active build job", "facts_count": 0}
    return progress


@router.post("/upload")
async def upload_resume(
    config: ConfigDep,
    file: UploadFile = File(...),
    base_id: str = Form("ai"),
    label: str = Form(""),
) -> dict[str, Any]:
    """Upload a .tex resume file with 2 MB cap, content sniffing, safe trash backup, and profile-build."""
    content = await file.read()
    resume_dir = config.resolve_path(config.profiles.resume_dir)
    resume_dir.mkdir(parents=True, exist_ok=True)

    # Clean destination filename
    from jobhunt.resumes.validator import sanitize_filename
    target_filename = sanitize_filename(file.filename or f"resume_{base_id}.tex")
    dest_path = resume_dir / target_filename

    # If file already exists, move it to data/trash/ rather than silent overwrite
    if dest_path.is_file():
        trash_dir = config.resolve_path("data/trash/resumes")
        trash_dir.mkdir(parents=True, exist_ok=True)
        stamp = utc_now().strftime("%Y%m%d_%H%M%S")
        trash_target = trash_dir / f"{dest_path.stem}_{stamp}{dest_path.suffix}"
        import shutil
        shutil.copy2(dest_path, trash_target)

    try:
        dest_path, text = validate_and_save_upload(content, target_filename, resume_dir)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Run diagnostics and sandboxed compilation
    val_result = validate_resume_text(
        text,
        tex_path=dest_path,
        project_root=config.project_root,
        run_compile=True,
    )

    # Run profile-build to extract facts and populate facts_tex.md
    job_id = f"build_{base_id}_{utc_now().strftime('%H%M%S')}"
    _BUILD_PROGRESS[job_id] = {
        "status": "running",
        "progress": 25,
        "message": "Extracting facts from uploaded resume...",
        "facts_count": 0,
    }

    try:
        from jobhunt.tailoring import facts as fact_module
        from jobhunt.tailoring.texdoc import parse
        from jobhunt.resumes.templates import extract_template_facts

        doc = parse(text)
        new_facts = extract_template_facts(base_id, doc)

        profile_dir = config.resolve_path(config.profiles.profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
        facts_file = profile_dir / "facts_tex.md"
        json_file = profile_dir / "facts.json"

        # Merge with existing facts or initialize
        per_resume = {base_id: new_facts}
        # Also include other configured bases if they exist on disk
        for other_id, other_file in config.profiles.files.items():
            if other_id != base_id:
                other_path = resume_dir / other_file
                if other_path.is_file():
                    try:
                        per_resume[other_id] = extract_template_facts(other_id, parse(other_path.read_text(encoding="utf-8")))
                    except Exception:
                        pass

        merged = fact_module.merge(per_resume)
        existing_ticks = fact_module.read_ticks(facts_file) if facts_file.is_file() else {}
        reconciled = fact_module.reconcile(merged, facts_file, profile_dir / "review_facts.md", {}, base_id)
        # Default new facts to ticked so the user has facts ready to review
        for f in reconciled.facts:
            if f.id not in reconciled.ticked:
                reconciled.ticked[f.id] = True

        facts_file.write_text(fact_module.render_facts_md(reconciled), encoding="utf-8")
        json_file.write_text(fact_module.facts_to_json(reconciled), encoding="utf-8")

        _BUILD_PROGRESS[job_id] = {
            "status": "completed",
            "progress": 100,
            "message": f"Successfully extracted {len(reconciled.facts)} facts",
            "facts_count": len(reconciled.facts),
        }
    except Exception as exc:
        logger.warning("Could not build facts on upload: %s", exc)
        _BUILD_PROGRESS[job_id] = {
            "status": "failed",
            "progress": 100,
            "message": f"Fact extraction warning: {exc}",
            "facts_count": val_result.get("facts_count", 0),
        }

    return {
        "status": "ok",
        "base_id": base_id,
        "job_id": job_id,
        "saved_path": str(dest_path),
        "filename": dest_path.name,
        "validation": val_result,
        "facts_count": _BUILD_PROGRESS[job_id]["facts_count"],
    }


@router.post("/validate")
def validate_resume(body: ValidateRequest, config: ConfigDep) -> dict[str, Any]:
    """Run diagnostics on an existing base resume or raw LaTeX string."""
    resume_dir = config.resolve_path(config.profiles.resume_dir)
    tex_text = ""
    tex_path = None

    if body.tex_content:
        tex_text = body.tex_content
    elif body.base_id:
        filename = config.profiles.files.get(body.base_id)
        if not filename:
            raise HTTPException(status_code=404, detail=f"Base ID '{body.base_id}' not found in configuration")
        tex_path = resume_dir / filename
        if not tex_path.is_file():
            raise HTTPException(status_code=404, detail=f"File '{filename}' does not exist on disk")
        tex_text = tex_path.read_text(encoding="utf-8")
    else:
        raise HTTPException(status_code=400, detail="Provide either base_id or tex_content")

    return validate_resume_text(
        tex_text,
        tex_path=tex_path,
        project_root=config.project_root,
        run_compile=True,
    )
