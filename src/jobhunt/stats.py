"""Read-only summary queries behind the `stats` command."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

TOP_COUNTRIES = 15


@dataclass
class Stats:
    open_jobs: int
    closed_jobs: int
    jobs_by_source: list[tuple[str, int]]
    jobs_by_country: list[tuple[str, int]]
    jobs_by_experience: list[tuple[str, int]]
    jobs_by_employment: list[tuple[str, int]]
    remote_jobs: int
    companies_by_status: list[tuple[str, int]]
    companies_by_ats: list[tuple[str, str, int]]
    recent_runs: list[sqlite3.Row]


def collect_stats(connection: sqlite3.Connection) -> Stats:
    def pairs(sql: str, *params: object) -> list[tuple[str, int]]:
        return [(str(row[0]), int(row[1])) for row in connection.execute(sql, params)]

    open_jobs = connection.execute("SELECT COUNT(*) FROM jobs WHERE status = 'open'").fetchone()[0]
    closed_jobs = connection.execute("SELECT COUNT(*) FROM jobs WHERE status = 'closed'").fetchone()[0]
    remote_jobs = connection.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'open' AND is_remote = 1"
    ).fetchone()[0]

    return Stats(
        open_jobs=open_jobs,
        closed_jobs=closed_jobs,
        # A merged job counts once per source that has seen it.
        jobs_by_source=pairs(
            """SELECT s.source, COUNT(DISTINCT j.id) FROM jobs j
               JOIN job_sightings s ON s.job_id = j.id
               WHERE j.status = 'open' GROUP BY s.source ORDER BY 2 DESC, 1"""
        ),
        jobs_by_country=pairs(
            f"""SELECT COALESCE(country, '??'), COUNT(*) FROM jobs
                WHERE status = 'open' GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {TOP_COUNTRIES}"""
        ),
        jobs_by_experience=pairs(
            "SELECT experience_level, COUNT(*) FROM jobs WHERE status = 'open' GROUP BY 1 ORDER BY 2 DESC, 1"
        ),
        jobs_by_employment=pairs(
            "SELECT employment_type, COUNT(*) FROM jobs WHERE status = 'open' GROUP BY 1 ORDER BY 2 DESC, 1"
        ),
        remote_jobs=remote_jobs,
        companies_by_status=pairs("SELECT status, COUNT(*) FROM companies GROUP BY 1 ORDER BY 2 DESC"),
        companies_by_ats=[
            (row[0], row[1], int(row[2]))
            for row in connection.execute(
                """SELECT ats_type, status, COUNT(*) FROM companies
                   GROUP BY ats_type, status ORDER BY ats_type, status"""
            )
        ],
        recent_runs=connection.execute(
            """SELECT source, started_at, finished_at, companies_checked, jobs_seen, jobs_new,
                      jobs_closed, jobs_skipped_by_prefilter
               FROM ingestion_runs ORDER BY id DESC LIMIT 10"""
        ).fetchall(),
    )


def format_stats(stats: Stats) -> str:
    """Plain-text report, readable in a terminal or a cron log."""
    lines: list[str] = []

    def section(title: str, rows: list[tuple[str, int]]) -> None:
        lines.append(f"\n{title}")
        if not rows:
            lines.append("  (none)")
        width = max((len(label) for label, _ in rows), default=0)
        for label, count in rows:
            lines.append(f"  {label:<{width}}  {count:>7}")

    lines.append(f"Jobs: {stats.open_jobs} open, {stats.closed_jobs} closed, {stats.remote_jobs} open+remote")
    section("Open jobs by source", stats.jobs_by_source)
    section(f"Open jobs by country (top {TOP_COUNTRIES}, ?? = unknown)", stats.jobs_by_country)
    section("Open jobs by experience level", stats.jobs_by_experience)
    section("Open jobs by employment type", stats.jobs_by_employment)
    section("Companies by status", stats.companies_by_status)

    lines.append("\nCompanies by ATS and status")
    if not stats.companies_by_ats:
        lines.append("  (none)")
    for ats_type, status, count in stats.companies_by_ats:
        lines.append(f"  {ats_type:<16} {status:<11} {count:>7}")

    lines.append("\nRecent ingestion runs")
    if not stats.recent_runs:
        lines.append("  (none)")
    for run in stats.recent_runs:
        lines.append(
            f"  {run['started_at']}  {run['source']:<16} companies={run['companies_checked']:<5} "
            f"seen={run['jobs_seen']:<6} new={run['jobs_new']:<5} closed={run['jobs_closed']:<4} "
            f"prefiltered={run['jobs_skipped_by_prefilter']}"
        )
    return "\n".join(lines)
