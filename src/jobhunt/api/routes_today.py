"""The Today page and the "opened" marker behind the apply button."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from jobhunt.api import today
from jobhunt.api.database import ConfigDep, WriteDb
from jobhunt.api.schemas import TodayView
from jobhunt.db import transaction
from jobhunt.schedule.definitions import zone
from jobhunt.timeutil import to_iso, utc_now

router = APIRouter(prefix="/api", tags=["today"])


@router.get("/today", response_model=TodayView, summary="What to look at now (no model calls)")
def get_today(db: WriteDb, config: ConfigDep, hide_caution: bool = False) -> TodayView:
    now = utc_now()
    tz = zone(config.schedule.timezone)
    day_start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        with transaction(db):                       # the visit boundary is the only write
            since = today.touch_visit(db, now)
    except sqlite3.OperationalError:
        since = today.get_visit_baseline(db, now)
    return TodayView(**today.build_today(db, now, since=since, day_start=day_start, hide_caution=hide_caution))


@router.post("/today/seen", status_code=204, summary="Mark everything on Today as seen")
def mark_seen(db: WriteDb) -> None:
    with transaction(db):
        today.mark_all_seen(db, utc_now())


@router.post("/jobs/{job_id}/opened", summary="Record that I opened the apply URL")
def mark_opened(job_id: int, db: WriteDb) -> dict[str, object]:
    now = to_iso(utc_now())
    with transaction(db):
        if db.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="job not found")
        db.execute(
            """INSERT INTO job_opens (job_id, first_opened_at, last_opened_at, open_count) VALUES (?, ?, ?, 1)
               ON CONFLICT(job_id) DO UPDATE SET last_opened_at = excluded.last_opened_at,
                                                 open_count = open_count + 1""", (job_id, now, now))
    return {"job_id": job_id, "opened_at": now}
