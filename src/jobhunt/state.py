"""Per-source bookkeeping in `source_state` (last run, daily fetch counters, cursors)."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from jobhunt.timeutil import to_iso


def record_source_run(
    connection: sqlite3.Connection,
    source: str,
    now: datetime,
    *,
    success: bool,
    fetches: int = 0,
) -> None:
    """Upsert last_run_at (and last_success_at on success) and add to today's fetch counter."""
    stamp = to_iso(now)
    today = stamp[:10]
    connection.execute(
        """INSERT INTO source_state (source, last_run_at, last_success_at, fetch_count_today, fetch_count_date)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(source) DO UPDATE SET
               last_run_at = excluded.last_run_at,
               last_success_at = COALESCE(excluded.last_success_at, source_state.last_success_at),
               fetch_count_today = CASE
                   WHEN source_state.fetch_count_date = excluded.fetch_count_date
                   THEN source_state.fetch_count_today + excluded.fetch_count_today
                   ELSE excluded.fetch_count_today END,
               fetch_count_date = excluded.fetch_count_date""",
        (source, stamp, stamp if success else None, fetches, today),
    )


def fetches_today(connection: sqlite3.Connection, source: str, now: datetime) -> int:
    """How many fetches `source` has already made on the UTC day of `now` (daily-cap checks)."""
    row = connection.execute(
        "SELECT fetch_count_today, fetch_count_date FROM source_state WHERE source = ?", (source,)
    ).fetchone()
    if row is None or row["fetch_count_date"] != to_iso(now)[:10]:
        return 0
    return int(row["fetch_count_today"])


def get_cursor(connection: sqlite3.Connection, source: str) -> str | None:
    row = connection.execute("SELECT cursor_or_etag FROM source_state WHERE source = ?", (source,)).fetchone()
    return row["cursor_or_etag"] if row else None


def set_cursor(connection: sqlite3.Connection, source: str, cursor: str | None) -> None:
    connection.execute(
        """INSERT INTO source_state (source, cursor_or_etag) VALUES (?, ?)
           ON CONFLICT(source) DO UPDATE SET cursor_or_etag = excluded.cursor_or_etag""",
        (source, cursor),
    )


def get_json_state(connection: sqlite3.Connection, source: str) -> dict:
    """`cursor_or_etag` interpreted as a JSON object (empty dict when unset or not JSON)."""
    import json

    raw = get_cursor(connection, source)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
