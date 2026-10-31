"""Company registry and board health (dead-slug) rules.

Health rules
------------
* success   -> counters reset, status = active (also revives an inactive company).
* failure   -> consecutive_failures += 1; distinct_failure_days += 1 when the failure happens
               on a different UTC day than the previous one. After `failures_to_deactivate`
               consecutive failures spanning `distinct_failure_days_to_deactivate` days the
               company becomes inactive.
* inactive  -> rechecked once every `inactive_recheck_days`.
Only NotFound/Invalid responses count as failures; timeouts, 429 and 5xx are transient and
only recorded in `last_error` (see errors.py).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from jobhunt.config import LifecycleSettings
from jobhunt.models import CompanyRef, CompanyStatus
from jobhunt.normalize import prettify_slug
from jobhunt.timeutil import to_iso


@dataclass(frozen=True)
class DiscoveredCompany:
    ats_type: str
    slug: str
    name: str | None = None
    region: str | None = None
    discovered_via: str = "unknown"


def add_discovered_companies(
    connection: sqlite3.Connection, entries: Iterable[DiscoveredCompany], now: datetime
) -> int:
    """Insert unknown (ats, slug) pairs as `unverified`. Returns how many were new.

    Existing rows are left alone, apart from learning a Lever region we did not know yet.
    Caller owns the transaction (one executemany for thousands of slugs).
    """
    entry_list = list(entries)
    if not entry_list:
        return 0
    created_at = to_iso(now)
    before = connection.total_changes
    connection.executemany(
        """INSERT OR IGNORE INTO companies (name, ats_type, slug, region, status, discovered_via, created_at)
           VALUES (?, ?, ?, ?, 'unverified', ?, ?)""",
        [
            (entry.name or prettify_slug(entry.slug), entry.ats_type, entry.slug, entry.region,
             entry.discovered_via, created_at)
            for entry in entry_list
        ],
    )
    inserted = connection.total_changes - before
    regional = [(entry.region, entry.ats_type, entry.slug) for entry in entry_list if entry.region]
    if regional:
        connection.executemany(
            "UPDATE companies SET region = ? WHERE ats_type = ? AND slug = ? AND region IS NULL", regional
        )
    return inserted


def _to_refs(rows: Sequence[sqlite3.Row]) -> list[CompanyRef]:
    return [
        CompanyRef(id=row["id"], name=row["name"], ats_type=row["ats_type"], slug=row["slug"],
                   region=row["region"], status=row["status"])
        for row in rows
    ]


def companies_due_for_ingest(
    connection: sqlite3.Connection,
    ats_type: str,
    now: datetime,
    settings: LifecycleSettings,
    limit: int | None = None,
    only_ids: Collection[int] | None = None,
) -> list[CompanyRef]:
    """Active companies, plus inactive ones whose monthly recheck is due."""
    recheck_before = to_iso(now - timedelta(days=settings.inactive_recheck_days))
    sql = """SELECT id, name, ats_type, slug, region, status FROM companies
             WHERE ats_type = ?
               AND (status = 'active'
                    OR (status = 'inactive' AND (last_checked_at IS NULL OR last_checked_at <= ?)))"""
    parameters: list[object] = [ats_type, recheck_before]
    if only_ids is not None:
        sql += f" AND id IN ({','.join('?' for _ in only_ids) or 'NULL'})"
        parameters.extend(only_ids)
    sql += " ORDER BY last_checked_at IS NOT NULL, last_checked_at, id"
    if limit:
        sql += " LIMIT ?"
        parameters.append(limit)
    return _to_refs(connection.execute(sql, parameters).fetchall())


def companies_to_probe(
    connection: sqlite3.Connection,
    now: datetime,
    min_interval_hours: float,
    ats_types: Sequence[str] | None = None,
    limit: int | None = None,
    only_ids: Collection[int] | None = None,
) -> list[CompanyRef]:
    """Unverified companies not probed within the last `min_interval_hours`."""
    probed_before = to_iso(now - timedelta(hours=min_interval_hours))
    sql = """SELECT id, name, ats_type, slug, region, status FROM companies
             WHERE status = 'unverified' AND (last_checked_at IS NULL OR last_checked_at <= ?)"""
    parameters: list[object] = [probed_before]
    if ats_types:
        sql += f" AND ats_type IN ({','.join('?' for _ in ats_types)})"
        parameters.extend(ats_types)
    if only_ids is not None:
        sql += f" AND id IN ({','.join('?' for _ in only_ids) or 'NULL'})"
        parameters.extend(only_ids)
    sql += " ORDER BY last_checked_at IS NOT NULL, last_checked_at, id"
    if limit:
        sql += " LIMIT ?"
        parameters.append(limit)
    return _to_refs(connection.execute(sql, parameters).fetchall())


def record_success(
    connection: sqlite3.Connection,
    company_id: int,
    now: datetime,
    *,
    name: str | None = None,
    region: str | None = None,
    consecutive_empty_runs: int = 0,
) -> None:
    """A valid response: reset failure counters and mark the board active.

    `name` (from the ATS payload) replaces the slug-derived placeholder name only.
    """
    current = get_company(connection, company_id)
    placeholder = prettify_slug(current["slug"])
    new_name = name if name and current["name"] in (placeholder, "", current["slug"]) else current["name"]
    stamp = to_iso(now)
    connection.execute(
        """UPDATE companies SET
               status = 'active', consecutive_failures = 0, distinct_failure_days = 0,
               consecutive_empty_runs = ?, last_failure_at = NULL, last_error = NULL,
               last_checked_at = ?, last_ok_at = ?, region = COALESCE(?, region), name = ?
           WHERE id = ?""",
        (consecutive_empty_runs, stamp, stamp, region, new_name, company_id),
    )


def record_failure(
    connection: sqlite3.Connection,
    company_id: int,
    now: datetime,
    error: str,
    settings: LifecycleSettings,
) -> str:
    """Count a dead-slug style failure. Returns the company's status afterwards."""
    row = connection.execute(
        """SELECT status, consecutive_failures, distinct_failure_days, last_failure_at
           FROM companies WHERE id = ?""",
        (company_id,),
    ).fetchone()
    stamp = to_iso(now)
    previous_day = row["last_failure_at"][:10] if row["last_failure_at"] else None
    distinct_days = row["distinct_failure_days"] + (0 if previous_day == stamp[:10] else 1)
    consecutive = row["consecutive_failures"] + 1

    status = row["status"]
    if (
        consecutive >= settings.failures_to_deactivate
        and distinct_days >= settings.distinct_failure_days_to_deactivate
    ):
        status = CompanyStatus.INACTIVE.value

    connection.execute(
        """UPDATE companies SET
               status = ?, consecutive_failures = ?, distinct_failure_days = ?, last_failure_at = ?,
               last_checked_at = ?, last_error = ?
           WHERE id = ?""",
        (status, consecutive, distinct_days, stamp, stamp, error[:500], company_id),
    )
    return status


def record_transient_problem(connection: sqlite3.Connection, company_id: int, now: datetime, error: str) -> None:
    """Timeout / 429 / 5xx: remember it, but do not move the company toward inactive."""
    connection.execute(
        "UPDATE companies SET last_checked_at = ?, last_error = ? WHERE id = ?",
        (to_iso(now), error[:500], company_id),
    )


def get_company(connection: sqlite3.Connection, company_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    if row is None:
        raise KeyError(f"no company with id {company_id}")
    return row
