"""My decisions on a job: Save, Applied, Not applying (with a reason), and Undo.

`job_decisions` is append-only; the current state of a job is its latest non-undo row (view `job_decision_current`).
Each action keeps the rest of the system consistent:

* Applied: tracker entry (created if missing) moves to `applied` with `applied_at`, a follow-up reminder 7 days later,
  the chosen resume version is attached to it and marked `sent`;
* Not applying: mirrored as a legacy `dismiss` in `job_feedback` (mapped reason) so every existing "hide dismissed" query,
  the Today lists and the ranker's feedback keep working;
* Undo restores what the decision replaced (`prior_json`).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from jobhunt import lifecycle
from jobhunt.errors import JobHuntError
from jobhunt.matching.feedback import record_feedback
from jobhunt.timeutil import to_iso, utc_now

NOT_APPLYING_REASONS = (
    "ineligible_phd_or_masters", "ineligible_level", "ineligible_other", "wrong_role", "too_senior",
    "wrong_domain_or_stack", "wrong_location", "company_not_interested", "poor_posting_or_suspicious",
    "already_applied_elsewhere", "closed_or_unavailable", "too_old", "other",
)
SOURCE_REPORT_REASONS = ("closed_or_unavailable", "too_old")     # feed the per-source freshness report
APPLY_HOW = ("company_site", "linkedin", "referral", "email", "other")
FOLLOW_UP_DAYS = 7
COMPANY_WARNING_DAYS = 60

# new reason -> the legacy job_feedback reason it is mirrored as (job_feedback has a CHECK constraint)
LEGACY_REASON = {
    "wrong_role": "wrong_role", "too_senior": "too_senior", "wrong_domain_or_stack": "wrong_stack",
    "wrong_location": "wrong_location", "company_not_interested": "company",
}
# old dismiss reason -> new reason (old rows keep working in the labels and suggestions)
FROM_LEGACY_REASON = {"wrong_stack": "wrong_domain_or_stack", "company": "company_not_interested"}

# which reasons teach the ranker "this is the wrong kind of role" (everything else says nothing about role fit)
ROLE_FIT_NEGATIVE_REASONS = frozenset({"wrong_role", "too_senior", "wrong_domain_or_stack"})
# old dismiss reasons that were always role-fit statements
LEGACY_ROLE_FIT_NEGATIVE = frozenset({"wrong_role", "wrong_stack", "too_senior"})


class DecisionError(JobHuntError):
    """Invalid decision input."""


def normalize_reason(reason: str | None) -> str | None:
    return FROM_LEGACY_REASON.get(reason or "", reason) if reason else None


def _now(clock: Any) -> str:
    return to_iso(clock())


def _require_job(connection: sqlite3.Connection, job_id: int) -> None:
    if connection.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
        raise DecisionError(f"job {job_id} not found")


def _insert(connection: sqlite3.Connection, job_id: int, action: str, *, reason: str | None = None,
            note: str | None = None, how: str | None = None, resume_id: int | None = None, happened_at: str,
            created_at: str, prior: dict[str, Any] | None = None) -> int:
    cursor = connection.execute(
        """INSERT INTO job_decisions (job_id, action, reason, note, how, resume_id, happened_at, created_at, prior_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (job_id, action, reason, (note or "").strip()[:1000] or None, how, resume_id, happened_at, created_at,
         json.dumps(prior) if prior is not None else None))
    return int(cursor.lastrowid)


def current(connection: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM job_decision_current WHERE job_id = ?", (job_id,)).fetchone()


def _application(connection: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()


def _history(connection: sqlite3.Connection, application_id: int, from_stage: str | None, to_stage: str, at: str) -> None:
    connection.execute(
        "INSERT INTO application_stage_history (application_id, from_stage, to_stage, changed_at) VALUES (?, ?, ?, ?)",
        (application_id, from_stage, to_stage, at))


# --- actions -------------------------------------------------------------------------------------------------------


def save(connection: sqlite3.Connection, job_id: int, *, clock: Any = utc_now) -> dict[str, Any]:
    _require_job(connection, job_id)
    now = _now(clock)
    existing = _application(connection, job_id)
    prior = {"application_existed": existing is not None}
    if existing is None:
        cursor = connection.execute(
            "INSERT INTO applications (job_id, stage, created_at, updated_at) VALUES (?, 'saved', ?, ?)", (job_id, now, now))
        _history(connection, int(cursor.lastrowid), None, "saved", now)
    _insert(connection, job_id, "saved", happened_at=now, created_at=now, prior=prior)
    return {"job_id": job_id, "action": "saved"}


def apply(connection: sqlite3.Connection, job_id: int, *, applied_at: str | None = None, how: str = "company_site",
          resume_id: int | None = None, note: str | None = None, clock: Any = utc_now) -> dict[str, Any]:
    """Record that I applied: tracker -> applied, follow-up +7 days, resume attached and marked sent."""
    _require_job(connection, job_id)
    if how not in APPLY_HOW:
        raise DecisionError(f"unknown way of applying {how!r}")
    now = _now(clock)
    when = applied_at or now
    try:
        datetime.fromisoformat(when.replace("Z", "+00:00"))
    except ValueError as error:
        raise DecisionError(f"bad applied date {when!r}") from error
    if resume_id is not None:
        owner = connection.execute("SELECT status, lifecycle, sent_at FROM tailored_resumes WHERE id = ? AND job_id = ?",
                                   (resume_id, job_id)).fetchone()
        if owner is None:
            raise DecisionError("that resume belongs to another job")
        if owner["status"] == "failed":
            raise DecisionError("a failed resume version cannot be marked as sent")
    existing = _application(connection, job_id)
    prior: dict[str, Any] = {"application_existed": existing is not None}
    follow_up = (datetime.fromisoformat(when.replace("Z", "+00:00")) + timedelta(days=FOLLOW_UP_DAYS)).date().isoformat()
    if existing is None:
        cursor = connection.execute(
            """INSERT INTO applications (job_id, stage, applied_at, follow_up_at, created_at, updated_at, tailored_resume_id)
               VALUES (?, 'applied', ?, ?, ?, ?, ?)""", (job_id, when, follow_up, now, now, resume_id))
        application_id = int(cursor.lastrowid)
        _history(connection, application_id, None, "applied", now)
    else:
        application_id = int(existing["id"])
        prior.update({"stage": existing["stage"], "applied_at": existing["applied_at"],
                      "follow_up_at": existing["follow_up_at"], "tailored_resume_id": existing["tailored_resume_id"]})
        if existing["stage"] == "saved":
            connection.execute(
                """UPDATE applications SET stage = 'applied', applied_at = ?, follow_up_at = COALESCE(follow_up_at, ?),
                          tailored_resume_id = COALESCE(?, tailored_resume_id), updated_at = ? WHERE id = ?""",
                (when, follow_up, resume_id, now, application_id))
            _history(connection, application_id, "saved", "applied", now)
        else:        # already further along: keep its stage, only attach the resume if none yet
            connection.execute(
                "UPDATE applications SET applied_at = COALESCE(applied_at, ?), tailored_resume_id = COALESCE(?, tailored_resume_id), "
                "updated_at = ? WHERE id = ?", (when, resume_id, now, application_id))
    if resume_id is not None:
        before = connection.execute("SELECT lifecycle, sent_at FROM tailored_resumes WHERE id = ?", (resume_id,)).fetchone()
        prior["resume"] = {"id": resume_id, "lifecycle": before["lifecycle"], "sent_at": before["sent_at"]}
        connection.execute("UPDATE tailored_resumes SET lifecycle = 'sent', sent_at = ? WHERE id = ?", (when, resume_id))
    _insert(connection, job_id, "applied", how=how, resume_id=resume_id, note=note, happened_at=when, created_at=now,
            prior=prior)
    return {"job_id": job_id, "action": "applied", "application_id": application_id, "applied_at": when,
            "follow_up_at": follow_up}


def not_applying(connection: sqlite3.Connection, job_id: int, reason: str, note: str | None = None, *,
                 clock: Any = utc_now) -> dict[str, Any]:
    """One reason (and an optional note). Calling it again edits the reason."""
    _require_job(connection, job_id)
    if reason not in NOT_APPLYING_REASONS:
        raise DecisionError(f"unknown reason {reason!r}")
    now = _now(clock)
    feedback = connection.execute("SELECT action, reason FROM job_feedback_current WHERE job_id = ?", (job_id,)).fetchone()
    prior: dict[str, Any] = {"feedback": dict(feedback) if feedback else None}
    if reason == "closed_or_unavailable" and lifecycle.close_job(connection, job_id, now, lifecycle.REPORTED_CLOSED):
        prior["closed_by_report"] = True          # Undo reopens it; the posting is gone, so it also stops being scored
    _insert(connection, job_id, "not_applying", reason=reason, note=note, happened_at=now, created_at=now, prior=prior)
    record_feedback(connection, job_id, "dismiss", LEGACY_REASON.get(reason, "other"), clock=clock)
    return {"job_id": job_id, "action": "not_applying", "reason": reason}


def undo(connection: sqlite3.Connection, job_id: int, *, clock: Any = utc_now) -> dict[str, Any]:
    """Revert the latest decision (tracker entry, resume state, dismiss) to what it replaced."""
    _require_job(connection, job_id)
    row = connection.execute(
        "SELECT * FROM job_decisions WHERE job_id = ? AND action != 'undo' ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    live = current(connection, job_id)
    if row is None or live is None:
        return {"job_id": job_id, "action": "undo", "reverted": None}
    prior = json.loads(row["prior_json"]) if row["prior_json"] else {}
    now = _now(clock)
    application = _application(connection, job_id)
    action = row["action"]
    if action in ("saved", "applied") and application is not None:
        if not prior.get("application_existed", True):
            connection.execute("DELETE FROM application_stage_history WHERE application_id = ?", (application["id"],))
            connection.execute("DELETE FROM applications WHERE id = ?", (application["id"],))
        elif action == "applied":
            connection.execute(
                """UPDATE applications SET stage = ?, applied_at = ?, follow_up_at = ?, tailored_resume_id = ?, updated_at = ?
                   WHERE id = ?""", (prior.get("stage", "saved"), prior.get("applied_at"), prior.get("follow_up_at"),
                                     prior.get("tailored_resume_id"), now, application["id"]))
            _history(connection, int(application["id"]), "applied", prior.get("stage", "saved"), now)
    if action == "applied" and prior.get("resume"):
        resume = prior["resume"]
        connection.execute("UPDATE tailored_resumes SET lifecycle = ?, sent_at = ? WHERE id = ?",
                           (resume["lifecycle"], resume["sent_at"], resume["id"]))
    if action == "not_applying":
        if prior.get("closed_by_report"):
            lifecycle.reopen_job(connection, job_id, only_reason=lifecycle.REPORTED_CLOSED)
        before = prior.get("feedback")
        if before and before.get("action") == "dismiss":
            record_feedback(connection, job_id, "dismiss", before.get("reason"), clock=clock)
        else:
            record_feedback(connection, job_id, "undo", None, clock=clock)
    _insert(connection, job_id, "undo", happened_at=now, created_at=now)
    if action == "applied" and prior.get("application_existed") and prior.get("stage") == "saved":
        _insert(connection, job_id, "saved", happened_at=now, created_at=now, prior={"application_existed": True})
    return {"job_id": job_id, "action": "undo", "reverted": action}


# --- reads ---------------------------------------------------------------------------------------------------------


def company_warning(connection: sqlite3.Connection, job_id: int, company: str, *, now: datetime | None = None
                    ) -> str | None:
    """"You applied to Collinear AI on 17 Oct (R-0007)" when another job of the same company was applied to < 60 days ago."""
    cutoff = to_iso((now or utc_now()) - timedelta(days=COMPANY_WARNING_DAYS))
    row = connection.execute(
        """SELECT a.applied_at, t.resume_number FROM applications a JOIN jobs j ON j.id = a.job_id
           LEFT JOIN tailored_resumes t ON t.id = a.tailored_resume_id
           WHERE LOWER(j.company_name) = LOWER(?) AND a.job_id != ? AND a.stage != 'saved'
             AND a.applied_at IS NOT NULL AND a.applied_at >= ?
           ORDER BY a.applied_at DESC LIMIT 1""", (company, job_id, cutoff)).fetchone()
    if row is None:
        return None
    day = datetime.fromisoformat(row["applied_at"].replace("Z", "+00:00"))
    code = f" (R-{int(row['resume_number']):04d})" if row["resume_number"] else ""
    return f"You applied to {company} on {day.day} {day.strftime('%b')}{code}"


def week_summary(connection: sqlite3.Connection, now: datetime, follow_ups_due: int) -> dict[str, int]:
    """Counts of jobs whose CURRENT decision was made in the last 7 days (an undone decision does not count)."""
    since = to_iso(now - timedelta(days=7))
    counts = {row[0]: row[1] for row in connection.execute(
        "SELECT action, COUNT(*) FROM job_decision_current WHERE created_at >= ? GROUP BY action", (since,))}
    return {"applied": counts.get("applied", 0), "saved": counts.get("saved", 0),
            "not_applying": counts.get("not_applying", 0), "follow_ups_due": follow_ups_due}
