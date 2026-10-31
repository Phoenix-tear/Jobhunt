"""Job lifecycle: counting misses and closing jobs that disappeared from a board.

A *successful, valid* fetch of an ATS board lists every open job, so a job that is absent from
it has missed one run. After `missed_runs_to_close` consecutive misses it is closed. A job that
shows up again is reopened by the upsert (see dedupe._apply_refresh).

Never counted as a miss: failed/timed-out/HTML fetches (the runner never calls into here for
those) and a "suspicious drop" - a board that suddenly returns nothing, or a tiny fraction of
what we held, which is far more likely an upstream hiccup than a mass layoff.
"""

from __future__ import annotations

import sqlite3

from jobhunt.config import LifecycleSettings


def count_open_jobs_in_scope(connection: sqlite3.Connection, source: str, company_id: int) -> int:
    row = connection.execute(
        """SELECT COUNT(DISTINCT j.id) FROM jobs j JOIN job_sightings s ON s.job_id = j.id
           WHERE j.status = 'open' AND j.company_id = ? AND s.source = ?""",
        (company_id, source),
    ).fetchone()
    return int(row[0])


def is_suspicious_drop(previous_open_jobs: int, present_jobs: int, settings: LifecycleSettings) -> bool:
    """True when this fetch must not be trusted for closing jobs."""
    if previous_open_jobs < settings.suspicious_drop_min_previous_jobs:
        return False
    return present_jobs == 0 or present_jobs < previous_open_jobs * settings.suspicious_drop_ratio


def register_misses(
    connection: sqlite3.Connection,
    source: str,
    company_id: int,
    present_source_job_ids: set[str],
    closed_at: str,
    settings: LifecycleSettings,
) -> int:
    """Increment `missed_runs` for open jobs absent from this fetch; close the ones at the limit.

    A job counts as present when *any* of its sightings from `source` is in the fetched set
    (merged duplicates inside one board therefore cannot close each other). Returns the number
    of jobs closed by this call.
    """
    rows = connection.execute(
        """SELECT j.id AS job_id, j.missed_runs, s.source_job_id
           FROM jobs j JOIN job_sightings s ON s.job_id = j.id
           WHERE j.status = 'open' AND j.company_id = ? AND s.source = ?""",
        (company_id, source),
    ).fetchall()

    present_job_ids: set[int] = set()
    missed_runs_by_job: dict[int, int] = {}
    for row in rows:
        missed_runs_by_job[row["job_id"]] = row["missed_runs"]
        if row["source_job_id"] in present_source_job_ids:
            present_job_ids.add(row["job_id"])

    to_increment: list[tuple[int]] = []
    to_close: list[tuple[str, int]] = []
    for job_id, missed in missed_runs_by_job.items():
        if job_id in present_job_ids:
            continue
        if missed + 1 >= settings.missed_runs_to_close:
            to_close.append((closed_at, job_id))
        else:
            to_increment.append((job_id,))

    if to_increment:
        connection.executemany("UPDATE jobs SET missed_runs = missed_runs + 1 WHERE id = ?", to_increment)
    if to_close:
        connection.executemany(
            """UPDATE jobs SET missed_runs = missed_runs + 1, status = 'closed', closed_at = ?
               WHERE id = ?""",
            to_close,
        )
    return len(to_close)


def close_stale_feed_jobs(
    connection: sqlite3.Connection,
    source: str,
    seen_before: str,
    closed_at: str,
    authoritative_sources: frozenset[str],
) -> int:
    """Close open jobs last seen before `seen_before` that `source` has sighted.

    Feeds list "recent" jobs, so absence proves nothing; age is the only closing signal.
    Jobs that any ATS (authoritative) source has sighted are left to the ATS lifecycle rules.
    Call only after a *successful* fetch so an outage can never age everything out.
    """
    placeholders = ",".join("?" for _ in authoritative_sources) or "''"
    cursor = connection.execute(
        f"""UPDATE jobs SET status = 'closed', closed_at = ?
            WHERE status = 'open' AND last_seen_at < ?
              AND id IN (SELECT job_id FROM job_sightings WHERE source = ?)
              AND NOT EXISTS (SELECT 1 FROM job_sightings a
                              WHERE a.job_id = jobs.id AND a.source IN ({placeholders}))""",
        [closed_at, seen_before, source, *authoritative_sources],
    )
    return cursor.rowcount


def close_by_source_ids(
    connection: sqlite3.Connection,
    source: str,
    source_job_ids: set[str],
    closed_at: str,
    authoritative_sources: frozenset[str],
    chunk_size: int = 400,
) -> int:
    """Close open jobs that `source` says are no longer wanted/active.

    A job another authoritative (ATS) source still lists is left alone.
    """
    ids = list(source_job_ids)
    closed = 0
    ats_placeholders = ",".join("?" for _ in authoritative_sources) or "''"
    for start in range(0, len(ids), chunk_size):
        chunk = ids[start : start + chunk_size]
        id_placeholders = ",".join("?" for _ in chunk)
        cursor = connection.execute(
            f"""UPDATE jobs SET status = 'closed', closed_at = ?
                WHERE status = 'open'
                  AND id IN (SELECT job_id FROM job_sightings
                             WHERE source = ? AND source_job_id IN ({id_placeholders}))
                  AND NOT EXISTS (SELECT 1 FROM job_sightings a
                                  WHERE a.job_id = jobs.id AND a.source <> ?
                                    AND a.source IN ({ats_placeholders}))""",
            [closed_at, source, *chunk, source, *authoritative_sources],
        )
        closed += cursor.rowcount
    return closed


CLOSED_CHECKED = "closed_checked"         # a "still open?" check found the posting gone
REPORTED_CLOSED = "reported_closed"       # I reported it as closed / unavailable (Not applying)


def close_job(connection: sqlite3.Connection, job_id: int, closed_at: str, reason: str) -> bool:
    """Close one open job on evidence other than a board listing, and remember why. Returns False if it was not open.

    A later successful sighting reopens it through the normal upsert (dedupe), which also clears the reason."""
    cursor = connection.execute(
        "UPDATE jobs SET status = 'closed', closed_at = ?, closed_reason = ? WHERE id = ? AND status = 'open'",
        (closed_at, reason, job_id),
    )
    return cursor.rowcount > 0


def reopen_job(connection: sqlite3.Connection, job_id: int, only_reason: str | None = None) -> bool:
    """Undo `close_job` (Restore, or Undo of a "closed or unavailable" report)."""
    clause = " AND closed_reason = ?" if only_reason else ""
    cursor = connection.execute(
        f"UPDATE jobs SET status = 'open', closed_at = NULL, closed_reason = NULL, missed_runs = 0 "
        f"WHERE id = ? AND status = 'closed'{clause}", (job_id, only_reason) if only_reason else (job_id,))
    return cursor.rowcount > 0
