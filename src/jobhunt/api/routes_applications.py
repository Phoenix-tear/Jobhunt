"""Application tracking endpoints (the Kanban board's backend)."""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Response

from jobhunt.api import queries
from jobhunt.api.database import ReadDb, WriteDb
from jobhunt.api.schemas import ApplicationCreate, ApplicationOut, ApplicationPatch, Board
from jobhunt.db import transaction
from jobhunt.timeutil import parse_iso, to_iso, utc_now

router = APIRouter(prefix="/api/applications", tags=["applications"])

DEFAULT_FOLLOW_UP_DAYS = 7      # a follow-up reminder is set this long after "Applied" unless I set one
CSV_COLUMNS = ("id", "job_id", "title", "company", "location", "stage", "applied_at", "follow_up_at", "resume_version",
               "resume_pdf_link", "apply_url", "contact_name", "contact_email", "contact_phone", "notes", "job_status",
               "created_at", "updated_at")


@router.get("", response_model=Board, summary="All applications grouped by stage (the board)")
def get_board(db: ReadDb) -> Board:
    return Board(**queries.fetch_board(db))  # type: ignore[arg-type]


def _csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in {"=", "+", "-", "@", "\t", "\r"} else text      # no spreadsheet formula injection


@router.get("/export.csv", summary="The whole tracker as CSV")
def export_csv(db: ReadDb) -> Response:
    rows = db.execute(
        """SELECT a.*, j.title, j.company_name, j.location_raw, j.status AS job_status, j.canonical_apply_url,
                  t.created_at AS resume_created_at, t.base_profile AS resume_base,
                  (SELECT s.source_url FROM job_sightings s WHERE s.job_id = j.id AND s.source_url IS NOT NULL
                   ORDER BY s.id LIMIT 1) AS sighting_url
           FROM applications a JOIN jobs j ON j.id = a.job_id
           LEFT JOIN tailored_resumes t ON t.id = a.tailored_resume_id
           ORDER BY a.updated_at DESC, a.id DESC""").fetchall()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        attached = row["tailored_resume_id"]
        writer.writerow([_csv_cell(value) for value in (
            row["id"], row["job_id"], row["title"], row["company_name"], row["location_raw"], row["stage"],
            row["applied_at"], row["follow_up_at"],
            f"tailored #{attached} ({row['resume_base']}, {row['resume_created_at']})" if attached else "",
            f"/api/tailored/{attached}/pdf" if attached else "",
            queries.safe_http_url(row["canonical_apply_url"]) or queries.safe_http_url(row["sighting_url"]) or "",
            row["contact_name"], row["contact_email"], row["contact_phone"], row["notes"], row["job_status"],
            row["created_at"], row["updated_at"])])
    return Response(buffer.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="jobhunt-tracker.csv"'})


@router.post("", response_model=ApplicationOut, summary="Save a job (idempotent per job)")
def save_job(payload: ApplicationCreate, response: Response, db: WriteDb) -> ApplicationOut:
    now = to_iso(utc_now())
    with transaction(db):
        if db.execute("SELECT 1 FROM jobs WHERE id = ?", (payload.job_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="job not found")
        existing = db.execute("SELECT id FROM applications WHERE job_id = ?", (payload.job_id,)).fetchone()
        if existing is not None:  # saving twice is a no-op that returns the existing record
            application_id = int(existing["id"])
            response.status_code = 200
        else:
            cursor = db.execute(
                """INSERT INTO applications (job_id, stage, applied_at, follow_up_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (payload.job_id, payload.stage, now if payload.stage == "applied" else None,
                 (utc_now() + timedelta(days=DEFAULT_FOLLOW_UP_DAYS)).date().isoformat()
                 if payload.stage == "applied" else None, now, now),
            )
            application_id = int(cursor.lastrowid)
            _write_history(db, application_id, None, payload.stage, now)
            response.status_code = 201
    result = queries.fetch_application(db, application_id)
    assert result is not None
    return ApplicationOut(**result)  # type: ignore[arg-type]


@router.patch("/{application_id}", response_model=ApplicationOut, summary="Change stage, notes or dates")
def update_application(application_id: int, patch: ApplicationPatch, db: WriteDb) -> ApplicationOut:
    changes = patch.model_dump(exclude_unset=True)
    now = to_iso(utc_now())
    with transaction(db):
        current = db.execute(
            "SELECT stage, applied_at, follow_up_at FROM applications WHERE id = ?", (application_id,)
        ).fetchone()
        if current is None:
            raise HTTPException(status_code=404, detail="application not found")

        assignments: list[str] = []
        values: list[object] = []
        new_stage = changes.get("stage")
        if "stage" in changes and new_stage is None:
            raise HTTPException(status_code=422, detail="stage cannot be null")
        for column in ("notes", "follow_up_at", "applied_at", "contact_name", "contact_email", "contact_phone"):
            if column in changes:
                assignments.append(f"{column} = ?")
                values.append(changes[column] if changes[column] != "" else None)

        if "tailored_resume_id" in changes:
            attached = changes["tailored_resume_id"]
            if attached is not None:
                owner = db.execute(
                    "SELECT t.id FROM tailored_resumes t JOIN applications a ON a.job_id = t.job_id "
                    "WHERE t.id = ? AND a.id = ?", (attached, application_id)).fetchone()
                if owner is None:
                    raise HTTPException(status_code=422, detail="that tailored resume belongs to another job")
            assignments.append("tailored_resume_id = ?")
            values.append(attached)

        stage_changed = new_stage is not None and new_stage != current["stage"]
        if stage_changed:
            assignments.append("stage = ?")
            values.append(new_stage)
            # Moving to "applied" stamps the date unless one is set (already, or in this same request).
            if new_stage == "applied" and not current["applied_at"] and not changes.get("applied_at"):
                assignments.append("applied_at = ?")
                values.append(now)
            if new_stage == "applied" and not current["follow_up_at"] and not changes.get("follow_up_at"):
                applied_on = parse_iso(str(changes.get("applied_at") or current["applied_at"] or now))
                assignments.append("follow_up_at = ?")
                values.append((applied_on + timedelta(days=DEFAULT_FOLLOW_UP_DAYS)).date().isoformat())

        if assignments:
            assignments.append("updated_at = ?")
            values.append(now)
            db.execute(f"UPDATE applications SET {', '.join(assignments)} WHERE id = ?", [*values, application_id])
        if stage_changed:
            _write_history(db, application_id, current["stage"], str(new_stage), now)

    result = queries.fetch_application(db, application_id)
    assert result is not None
    return ApplicationOut(**result)  # type: ignore[arg-type]


@router.delete("/{application_id}", status_code=204, summary="Remove a saved job (only while stage = saved)")
def delete_application(application_id: int, db: WriteDb) -> Response:
    with transaction(db):
        row = db.execute("SELECT stage FROM applications WHERE id = ?", (application_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="application not found")
        if row["stage"] != "saved":
            raise HTTPException(
                status_code=409, detail="only applications still in the 'saved' stage can be deleted"
            )
        db.execute("DELETE FROM applications WHERE id = ?", (application_id,))
    return Response(status_code=204)


def _write_history(
    connection: sqlite3.Connection, application_id: int, from_stage: str | None, to_stage: str, changed_at: str
) -> None:
    connection.execute(
        """INSERT INTO application_stage_history (application_id, from_stage, to_stage, changed_at)
           VALUES (?, ?, ?, ?)""",
        (application_id, from_stage, to_stage, changed_at),
    )

