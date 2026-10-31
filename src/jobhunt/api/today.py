"""The Today page: everything is a query over data that already exists (no model calls, no network).

Relevant means what the default job list shows: gate target/maybe AND location fit india /
remote_open_to_india / remote_unclear. "Since my last visit" uses a session boundary stored in `app_state`: a
visit counts as new when the page was not fetched for 30 minutes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from jobhunt import decisions
from jobhunt.api import queries
from jobhunt.timeutil import parse_iso, to_iso

SESSION_GAP = timedelta(minutes=30)
FIRST_VISIT_WINDOW = timedelta(hours=24)
TOP_N = 15
LIST_LIMIT = 40
CLOSING_WINDOW_DAYS = 7
FOLLOW_UP_AFTER_DAYS = 7

RELEVANT_SQL = (
    "r.gate_status IN ('target', 'maybe') "
    "AND COALESCE(r.location_fit, 'unknown') IN ('india', 'remote_open_to_india', 'remote_unclear')"
)
NOT_DISMISSED_SQL = ("NOT EXISTS (SELECT 1 FROM job_feedback_current f WHERE f.job_id = j.id AND f.action = 'dismiss') "
                     "AND NOT EXISTS (SELECT 1 FROM job_auto_hidden h WHERE h.job_id = j.id AND h.restored_at IS NULL)")
NOT_APPLIED_SQL = ("(a.id IS NULL OR a.stage = 'saved') AND NOT EXISTS (SELECT 1 FROM job_decision_current dc "
                   "WHERE dc.job_id = j.id AND dc.action IN ('applied', 'not_applying'))")
_BASE = "FROM jobs j JOIN job_relevance r ON r.job_id = j.id LEFT JOIN applications a ON a.job_id = j.id"

DEADLINE_KEYS = ("validthrough", "applicationdeadline", "application_deadline", "closingdate", "closing_date",
                 "deadline", "expirydate", "expiry_date", "expires", "expires_at", "expirationdate", "enddate")
_KEY_LIKE = " OR ".join(f"LOWER(j.raw_json) LIKE '%{key}%'" for key in DEADLINE_KEYS)


# --- visit boundary --------------------------------------------------------------------------------------------------


def _get(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row and row[0] else None


def _set(connection: sqlite3.Connection, key: str, value: str, now: datetime) -> None:
    connection.execute(
        """INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, json.dumps(value), to_iso(now)))


def get_visit_baseline(connection: sqlite3.Connection, now: datetime) -> datetime:
    """Return the "since" boundary (the end of the previous session) without writing."""
    last_seen = _get(connection, "today_last_seen")
    baseline = _get(connection, "today_baseline")
    if last_seen is None:
        return now - FIRST_VISIT_WINDOW
    seen = parse_iso(last_seen)
    if now - seen > SESSION_GAP:
        return seen
    return parse_iso(baseline) if baseline else now - FIRST_VISIT_WINDOW


def touch_visit(connection: sqlite3.Connection, now: datetime) -> datetime:
    """Register a page load and return the "since" boundary (the end of the previous session)."""
    baseline_dt = get_visit_baseline(connection, now)
    _set(connection, "today_baseline", to_iso(baseline_dt), now)
    _set(connection, "today_last_seen", to_iso(now), now)
    return baseline_dt


def mark_all_seen(connection: sqlite3.Connection, now: datetime) -> None:
    _set(connection, "today_baseline", to_iso(now), now)
    _set(connection, "today_last_seen", to_iso(now), now)


# --- deadlines -------------------------------------------------------------------------------------------------------


def deadline_from_raw(raw_json: str | None) -> datetime | None:
    """A closing date found under a well-known key of the stored source payload, else None."""
    if not raw_json:
        return None
    try:
        data = json.loads(raw_json)
    except ValueError:
        return None
    found: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower().replace("-", "_") in DEADLINE_KEYS and isinstance(value, str):
                    found.append(value)
                else:
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:20]:
                walk(item, depth + 1)

    walk(data)
    for text in found:
        text = text.strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}", text):
            continue
        try:
            return parse_iso(text if "T" in text else f"{text[:10]}T23:59:59Z")
        except ValueError:
            continue
    return None


# --- assembling -------------------------------------------------------------------------------------------------------


def _job_rows(connection: sqlite3.Connection, ids: list[int],
              extras: dict[int, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    rows = queries.fetch_summaries(connection, ids)
    meta: dict[int, sqlite3.Row] = {}
    if ids:
        for row in connection.execute(
                f"""SELECT j.id, j.canonical_apply_url,
                           (SELECT s.source_url FROM job_sightings s WHERE s.job_id = j.id AND s.source_url IS NOT NULL
                            ORDER BY s.id LIMIT 1) AS sighting_url,
                           o.last_opened_at, j.closed_at,
                           (SELECT MAX(t.id) FROM tailored_resumes t
                            WHERE t.job_id = j.id AND t.status IN ('ok', 'fallback')) AS tailored_id
                    FROM jobs j LEFT JOIN job_opens o ON o.job_id = j.id
                    WHERE j.id IN ({",".join("?" for _ in ids)})""", ids):
            meta[int(row["id"])] = row
    out = []
    for item in rows:
        extra = meta.get(int(item["id"]))
        item = dict(item)
        item["apply_url"] = (queries.safe_http_url(extra["canonical_apply_url"])
                             or queries.safe_http_url(extra["sighting_url"])) if extra else None
        item["opened_at"] = extra["last_opened_at"] if extra else None
        item["closed_at"] = extra["closed_at"] if extra else None
        item["tailored_resume_id"] = extra["tailored_id"] if extra else None
        item.update((extras or {}).get(int(item["id"]), {}))
        out.append(item)
    return out


def _ids(connection: sqlite3.Connection, sql: str, params: list[Any]) -> list[int]:
    return [int(row[0]) for row in connection.execute(sql, params)]


def build_today(connection: sqlite3.Connection, now: datetime, *, since: datetime,
                day_start: datetime, hide_caution: bool = False) -> dict[str, Any]:
    view = _build_today(connection, now, since=since, day_start=day_start)
    if not hide_caution:
        return view
    removed = 0
    for key in ("new_jobs", "top", "closing_soon", "approved_not_applied"):
        kept = [item for item in view[key] if not item.get("has_caution_signal")]
        if key == "new_jobs":
            removed = len(view[key]) - len(kept)
        view[key] = kept
    view["new_total"] = max(0, int(view["new_total"]) - removed)
    return view


def _build_today(connection: sqlite3.Connection, now: datetime, *, since: datetime,
                 day_start: datetime) -> dict[str, Any]:
    new_ids = _ids(
        connection,
        f"""SELECT j.id {_BASE}
            WHERE j.status = 'open' AND {RELEVANT_SQL} AND {NOT_DISMISSED_SQL} AND {NOT_APPLIED_SQL}
              AND j.first_seen_at > ?
            ORDER BY (r.final_score IS NULL), r.final_score DESC, j.id DESC LIMIT ?""", [to_iso(since), LIST_LIMIT])
    new_total = connection.execute(
        f"""SELECT COUNT(*) {_BASE}
            WHERE j.status = 'open' AND {RELEVANT_SQL} AND {NOT_DISMISSED_SQL} AND {NOT_APPLIED_SQL}
              AND j.first_seen_at > ?""", (to_iso(since),)).fetchone()[0]
    top_ids = _ids(
        connection,
        f"""SELECT j.id {_BASE}
            WHERE j.status = 'open' AND {RELEVANT_SQL} AND {NOT_DISMISSED_SQL} AND {NOT_APPLIED_SQL}
              AND r.final_score IS NOT NULL
            ORDER BY r.final_score DESC, j.id DESC LIMIT ?""", [TOP_N])

    horizon = now + timedelta(days=CLOSING_WINDOW_DAYS)
    candidates = connection.execute(
        f"""SELECT j.id, j.raw_json {_BASE}
            WHERE j.status = 'open' AND {RELEVANT_SQL} AND {NOT_DISMISSED_SQL} AND {NOT_APPLIED_SQL}
              AND j.raw_json IS NOT NULL AND ({_KEY_LIKE}) LIMIT 400""").fetchall()
    deadlines: dict[int, datetime] = {}
    for row in candidates:
        moment = deadline_from_raw(row["raw_json"])
        if moment is not None and now <= moment <= horizon:
            deadlines[int(row["id"])] = moment
    closing_ids = sorted(deadlines, key=lambda i: deadlines[i])[:LIST_LIMIT]
    closing = _job_rows(connection, closing_ids, {i: {"deadline_at": to_iso(deadlines[i])} for i in closing_ids})

    gone_ids = _ids(
        connection,
        """SELECT j.id FROM jobs j JOIN job_opens o ON o.job_id = j.id
           LEFT JOIN applications a ON a.job_id = j.id
           WHERE j.status = 'closed' AND j.closed_at IS NOT NULL AND j.closed_at >= o.first_opened_at
             AND (a.id IS NULL OR a.stage = 'saved')
           ORDER BY j.closed_at DESC LIMIT ?""", [LIST_LIMIT])

    cutoff = to_iso(now - timedelta(days=FOLLOW_UP_AFTER_DAYS))
    follow_rows = connection.execute(
        """SELECT a.id AS application_id, a.job_id, a.applied_at, a.follow_up_at, a.contact_name,
                  (SELECT MAX(h.changed_at) FROM application_stage_history h WHERE h.application_id = a.id) AS last_change
           FROM applications a WHERE a.stage = 'applied'
           ORDER BY COALESCE(a.applied_at, a.created_at) ASC""").fetchall()
    follow_ids: list[int] = []
    follow_extra: dict[int, dict[str, Any]] = {}
    for row in follow_rows:
        since_change = row["last_change"] or row["applied_at"]
        due_by_date = bool(row["follow_up_at"]) and row["follow_up_at"][:10] <= to_iso(now)[:10]
        if (since_change and since_change <= cutoff) or due_by_date:
            follow_ids.append(int(row["job_id"]))
            reference = parse_iso(since_change) if since_change else now
            follow_extra[int(row["job_id"])] = {
                "application_id": int(row["application_id"]), "applied_at": row["applied_at"],
                "follow_up_at": row["follow_up_at"], "days_since_change": max(0, (now - reference).days),
                "contact_name": row["contact_name"]}
    interview_rows = connection.execute(
        """SELECT a.id AS application_id, a.job_id, a.follow_up_at, a.contact_name
           FROM applications a WHERE a.stage = 'oa_interview'
           ORDER BY (a.follow_up_at IS NULL), a.follow_up_at ASC LIMIT 20""").fetchall()
    interview_extra = {int(r["job_id"]): {"application_id": int(r["application_id"]), "next_date": r["follow_up_at"],
                                          "contact_name": r["contact_name"]} for r in interview_rows}
    approved_rows = connection.execute(
        """SELECT t.job_id, t.id AS tailored_id, t.pdf_path
           FROM tailored_resumes t JOIN jobs j ON j.id = t.job_id
           LEFT JOIN applications a ON a.job_id = t.job_id
           WHERE t.approved = 1 AND (a.id IS NULL OR a.stage = 'saved')
             AND t.id = (SELECT MAX(t2.id) FROM tailored_resumes t2 WHERE t2.job_id = t.job_id AND t2.approved = 1)
           ORDER BY t.id DESC LIMIT 20""").fetchall()
    approved_extra = {int(r["job_id"]): {"tailored_resume_id": int(r["tailored_id"]), "approved_pdf": bool(r["pdf_path"])}
                      for r in approved_rows}

    source_rows = connection.execute(
        f"""SELECT s.source AS source,
                   COUNT(DISTINCT CASE WHEN j.first_seen_at >= ? THEN j.id END) AS new_today,
                   COUNT(DISTINCT CASE WHEN j.first_seen_at >= ? AND {RELEVANT_SQL} THEN j.id END) AS relevant_today,
                   COUNT(DISTINCT CASE WHEN j.status = 'open' THEN j.id END) AS open_total
            FROM job_sightings s JOIN jobs j ON j.id = s.job_id
            LEFT JOIN job_relevance r ON r.job_id = j.id
            GROUP BY s.source HAVING new_today > 0 OR open_total > 0
            ORDER BY new_today DESC, open_total DESC""", (to_iso(day_start), to_iso(day_start))).fetchall()

    return {
        "generated_at": to_iso(now),
        "since": to_iso(since),
        "new_total": int(new_total),
        "new_jobs": _job_rows(connection, new_ids),
        "top": _job_rows(connection, top_ids),
        "closing_soon": closing,
        "disappeared": _job_rows(connection, gone_ids),
        "follow_ups": _job_rows(connection, follow_ids[:LIST_LIMIT], follow_extra),
        "interviews": _job_rows(connection, [int(r["job_id"]) for r in interview_rows], interview_extra),
        "approved_not_applied": _job_rows(connection, [int(r["job_id"]) for r in approved_rows], approved_extra),
        "week": decisions.week_summary(connection, now, len(follow_ids)),
        "sources": [{"source": r["source"], "new_today": int(r["new_today"]), "relevant_today": int(r["relevant_today"]),
                     "open_total": int(r["open_total"])} for r in source_rows],
    }
