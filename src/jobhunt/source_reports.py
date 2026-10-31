"""Per-source freshness report for the Health page: how many jobs are auto-hidden, and how often I reported a source's
jobs as closed / too old versus how many I looked at. Proposes (never applies) a tighter `stale_after_days`."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from jobhunt import decisions, freshness
from jobhunt.api import queries
from jobhunt.config import Config

MIN_REPORTS = 10
CLOSED_SHARE = 0.30
TIGHTEN_BY_DAYS = 15
MIN_STALE_AFTER_DAYS = 14


@dataclass
class SourceReport:
    source: str
    shown: int                 # jobs of this source in the default Open list right now
    reported_closed: int       # Not applying: closed_or_unavailable
    reported_too_old: int      # Not applying: too_old
    decided: int               # jobs of this source I made any decision on (saved / applied / not applying / dismissed)
    closed_share: float        # (closed + too old reports) / decided
    auto_hidden: int
    current_stale_after_days: int
    proposed_stale_after_days: int | None = None
    proposal: str | None = None


def build_source_reports(connection: sqlite3.Connection, config: Config, now: datetime) -> list[SourceReport]:
    sources = [row[0] for row in connection.execute("SELECT DISTINCT source FROM job_sightings ORDER BY source")]
    hidden = freshness.hidden_by_source(connection)
    reasons = decisions.SOURCE_REPORT_REASONS
    reason_marks = ",".join("?" for _ in reasons)
    reports: dict[str, dict[str, int]] = {}
    for row in connection.execute(
            f"""SELECT s.source, d.reason, COUNT(DISTINCT d.job_id) FROM job_decision_current d
                JOIN job_sightings s ON s.job_id = d.job_id
                WHERE d.action = 'not_applying' AND d.reason IN ({reason_marks}) GROUP BY s.source, d.reason""", reasons):
        reports.setdefault(row[0], {})[row[1]] = int(row[2])
    decided = {row[0]: int(row[1]) for row in connection.execute(
        """SELECT s.source, COUNT(DISTINCT s.job_id) FROM job_sightings s
           WHERE s.job_id IN (SELECT job_id FROM job_decision_current)
              OR s.job_id IN (SELECT job_id FROM job_feedback_current WHERE action = 'dismiss')
           GROUP BY s.source""")}
    out: list[SourceReport] = []
    for source in sources:
        where, params = queries.build_where(queries.JobFilters(sources=[source]), now)
        shown = int(connection.execute(
            f"SELECT COUNT(*) FROM jobs j LEFT JOIN job_relevance r ON r.job_id = j.id WHERE {where}", params).fetchone()[0])
        closed = reports.get(source, {}).get("closed_or_unavailable", 0)
        too_old = reports.get(source, {}).get("too_old", 0)
        total_reports = closed + too_old
        if not (total_reports or hidden.get(source) or shown):
            continue
        looked_at = decided.get(source, 0)
        share = total_reports / looked_at if looked_at else 0.0
        current = config.freshness.source_stale_after_days.get(source, config.freshness.stale_after_days)
        report = SourceReport(source=source, shown=shown, reported_closed=closed, reported_too_old=too_old,
                              decided=looked_at, closed_share=round(share, 3), auto_hidden=hidden.get(source, 0),
                              current_stale_after_days=current)
        if total_reports >= MIN_REPORTS and share > CLOSED_SHARE:
            proposed = max(MIN_STALE_AFTER_DAYS, current - TIGHTEN_BY_DAYS)
            if proposed < current:
                report.proposed_stale_after_days = proposed
                report.proposal = (f"{total_reports} of {looked_at} jobs you looked at from {source} were closed or too old: "
                                   f"consider freshness.source_stale_after_days: {{{source}: {proposed}}} in config.yaml "
                                   f"(not applied)")
        out.append(report)
    out.sort(key=lambda item: (-(item.reported_closed + item.reported_too_old), -item.auto_hidden, item.source))
    return out
