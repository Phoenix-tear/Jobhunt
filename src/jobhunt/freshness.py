"""Posting freshness and automatic hiding of stale jobs (deterministic, no model).

`classify_freshness` is a pure function: (posting dates, kind of role, sources, settings, today) -> band
(fresh | aging | stale | unknown). `run_freshness` stores the band for every open job and moves stale ones to the
"Auto-hidden" view (`job_auto_hidden`, reason `auto_stale`). Nothing is deleted. Restoring a job marks it so it is never
hidden automatically again; jobs I saved, applied to, tailored a resume for, or added by hand are never auto-hidden.

Spring Boot analogy: `classify_freshness` is a pure `@Service` method, `run_freshness` the `@Scheduled` batch around it.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from jobhunt import lifecycle
from jobhunt.config import Config, FreshnessSettings
from jobhunt.matching.gate import internship_term_ends
from jobhunt.timeutil import Clock, to_iso, utc_now

FRESH, AGING, STALE, UNKNOWN = "fresh", "aging", "stale", "unknown"
AUTO_STALE, CLOSED_CHECKED = "auto_stale", "closed_checked"

_EARLY_CAREER_TITLE = re.compile(
    r"\b(?:intern|interns|internship|internships|graduate|graduates|new\s+grad|trainee|apprentice|apprenticeship|"
    r"early\s+careers?|co\s*-?\s*op|campus|university\s+hire|college\s+hire)\b", re.IGNORECASE)

_MONTHS = {name: number for number, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
     "december"), start=1)}
_MONTH_RE = "|".join(_MONTHS) + "|" + "|".join(name[:3] for name in _MONTHS) + "|sept"
_DEADLINE_WORDS = (r"(?:application\s+)?(?:deadline|closing\s+date|closes?\s+on|closes?|apply\s+by|apply\s+before|"
                   r"applications?\s+(?:close|closes|due|accepted\s+until|open\s+until)|last\s+date(?:\s+to\s+apply)?|"
                   r"closing)")
_DATE_FORMS = (
    rf"(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m1>{_MONTH_RE})\.?,?\s+(?P<y1>20\d{{2}})",
    rf"(?P<m2>{_MONTH_RE})\.?\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y2>20\d{{2}})",
    r"(?P<y3>20\d{2})-(?P<m3>\d{2})-(?P<d3>\d{2})",
    r"(?P<d4>\d{1,2})/(?P<m4>\d{1,2})/(?P<y4>20\d{2})",
)
_DEADLINE = re.compile(rf"{_DEADLINE_WORDS}\W{{0,12}}(?:[a-z ]{{0,20}}\W{{0,4}})?(?:{'|'.join(f'(?:{form})' for form in _DATE_FORMS)})",
                       re.IGNORECASE)
_HEAD_CHARS = 3000


def _month_number(name: str) -> int:
    lowered = name.lower().rstrip(".")
    if lowered == "sept":
        return 9
    for full, number in _MONTHS.items():
        if full == lowered or full[:3] == lowered:
            return number
    raise ValueError(name)


def deadline_dates(text: str | None) -> list[date]:
    """Application deadlines named in the text ("Apply by 30 November 2026", "Closing date: 2026-12-01")."""
    found: list[date] = []
    for match in _DEADLINE.finditer((text or "")[:_HEAD_CHARS]):
        groups = match.groupdict()
        try:
            if groups.get("y1"):
                found.append(date(int(groups["y1"]), _month_number(groups["m1"]), int(groups["d1"])))
            elif groups.get("y2"):
                found.append(date(int(groups["y2"]), _month_number(groups["m2"]), int(groups["d2"])))
            elif groups.get("y3"):
                found.append(date(int(groups["y3"]), int(groups["m3"]), int(groups["d3"])))
            elif groups.get("y4"):
                found.append(date(int(groups["y4"]), int(groups["m4"]), int(groups["d4"])))   # day first
        except ValueError:
            continue
    return found


# --- the pure classification --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Freshness:
    band: str                     # fresh | aging | stale | unknown
    age_days: int | None
    basis: str                    # posted | first_seen | none
    stale_after_days: int
    protected_by: str | None = None   # "deadline" | "season": would be stale, but a future date keeps it alive


def parse_when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_early_career(employment_type: str | None, experience_level: str | None, title: str | None) -> bool:
    return (employment_type == "internship" or experience_level in {"intern", "new_grad"}
            or bool(_EARLY_CAREER_TITLE.search(title or "")))


def stale_threshold(settings: FreshnessSettings, sources: Iterable[str], early_career: bool) -> int:
    """Days after which a posting is stale: the most generous override among its sources, longer for internships."""
    listed = list(sources) or [""]
    threshold = max(settings.source_stale_after_days.get(source, settings.stale_after_days) for source in listed)
    return max(threshold, settings.internship_stale_after_days) if early_career else threshold


def classify_freshness(
    *, posted_at: str | None, first_seen_at: str | None, employment_type: str | None = None,
    experience_level: str | None = None, title: str | None = None, description: str | None = None,
    sources: Iterable[str] = (), settings: FreshnessSettings, now: datetime,
) -> Freshness:
    source_list = [str(source) for source in sources]
    early = is_early_career(employment_type, experience_level, title)
    limit = stale_threshold(settings, source_list, early)

    posted, first_seen = parse_when(posted_at), parse_when(first_seen_at)
    if posted is not None:
        age = max(0, (now - posted).days)
        basis = "posted"
    else:
        aggregator = bool(source_list) and all(source in settings.aggregator_sources for source in source_list)
        age = max(0, (now - first_seen).days) if first_seen is not None else None
        if not aggregator or age is None or age <= settings.unknown_stale_after_days:
            return Freshness(UNKNOWN, age if aggregator else None, "first_seen" if aggregator and age is not None else "none", limit)
        return Freshness(STALE, age, "first_seen", settings.unknown_stale_after_days)

    if age <= settings.fresh_days:
        return Freshness(FRESH, age, basis, limit)
    if age <= limit:
        return Freshness(AGING, age, basis, limit)

    today = now.date()
    head = (description or "")[:_HEAD_CHARS]
    if any(day >= today for day in deadline_dates(head)):
        return Freshness(AGING, age, basis, limit, protected_by="deadline")
    if early and any(end >= today for end in internship_term_ends(title, head)):
        return Freshness(AGING, age, basis, limit, protected_by="season")
    return Freshness(STALE, age, basis, limit)


# --- the batch ------------------------------------------------------------------------------------------------------


@dataclass
class FreshnessStats:
    examined: int = 0
    by_band: dict[str, int] = field(default_factory=dict)
    newly_hidden: int = 0
    unhidden: int = 0
    already_hidden: int = 0
    protected: int = 0
    hidden_by_source: dict[str, int] = field(default_factory=dict)


_PROTECTED_SQL = """(
    EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id)
    OR EXISTS (SELECT 1 FROM job_decision_current d WHERE d.job_id = j.id AND d.action IN ('saved', 'applied'))
    OR EXISTS (SELECT 1 FROM tailored_resumes t WHERE t.job_id = j.id)
    OR EXISTS (SELECT 1 FROM job_sightings s WHERE s.job_id = j.id AND s.source = 'manual'))"""


def run_freshness(connection: sqlite3.Connection, config: Config, *, clock: Clock = utc_now) -> FreshnessStats:
    """Store the band of every open job; hide the stale ones (reason auto_stale); un-hide ones that no longer qualify."""
    settings = config.freshness
    stats = FreshnessStats()
    if not settings.enabled:
        return stats
    now = clock()
    now_iso = to_iso(now)
    rows = connection.execute(
        f"""SELECT j.id, j.posted_at, j.first_seen_at, j.employment_type, j.experience_level, j.title,
                   COALESCE(r.gate_status, '') AS gate_status, {_PROTECTED_SQL} AS protected,
                   (SELECT GROUP_CONCAT(DISTINCT s.source) FROM job_sightings s WHERE s.job_id = j.id) AS sources,
                   h.reason AS hidden_reason, h.restored_at AS restored_at
            FROM jobs j LEFT JOIN job_relevance r ON r.job_id = j.id LEFT JOIN job_auto_hidden h ON h.job_id = j.id
            WHERE j.status = 'open'"""
    ).fetchall()
    by_id = {int(row["id"]): row for row in rows}

    def classify(row: sqlite3.Row, description: str | None) -> Freshness:
        return classify_freshness(
            posted_at=row["posted_at"], first_seen_at=row["first_seen_at"], employment_type=row["employment_type"],
            experience_level=row["experience_level"], title=row["title"], description=description,
            sources=(row["sources"] or "").split(",") if row["sources"] else [], settings=settings, now=now)

    results = {job_id: classify(row, None) for job_id, row in by_id.items()}
    # only jobs that look stale need their text: a future deadline or an upcoming season keeps one alive
    looks_stale = [job_id for job_id, result in results.items() if result.band == STALE and result.basis == "posted"]
    for start in range(0, len(looks_stale), 400):
        chunk = looks_stale[start:start + 400]
        marks = ",".join("?" for _ in chunk)
        for text_row in connection.execute(
                f"SELECT id, substr(description_text, 1, {_HEAD_CHARS}) AS head FROM jobs WHERE id IN ({marks})", chunk):
            job_id = int(text_row["id"])
            results[job_id] = classify(by_id[job_id], text_row["head"])

    band_counts: Counter[str] = Counter()
    freshness_rows: list[tuple[Any, ...]] = []
    to_hide: list[tuple[Any, ...]] = []
    to_unhide: list[int] = []
    for job_id, result in results.items():
        row = by_id[job_id]
        band_counts[result.band] += 1
        freshness_rows.append((job_id, result.band, result.age_days, result.basis, now_iso))
        hidden_now = row["hidden_reason"] is not None and row["restored_at"] is None
        if result.band == STALE:
            if row["protected"] or row["gate_status"] == "excluded":
                stats.protected += 1 if row["protected"] else 0
            elif row["hidden_reason"] is None:
                detail = f"{result.age_days} days old ({result.basis}); stale after {result.stale_after_days}"
                to_hide.append((job_id, AUTO_STALE, detail, now_iso))
            elif hidden_now:
                stats.already_hidden += 1
        elif hidden_now and row["hidden_reason"] == AUTO_STALE:
            to_unhide.append(job_id)
    stats.examined = len(rows)
    stats.by_band = dict(band_counts)

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.executemany(
            """INSERT INTO job_freshness (job_id, band, age_days, basis, computed_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET band = excluded.band, age_days = excluded.age_days,
                   basis = excluded.basis, computed_at = excluded.computed_at""", freshness_rows)
        connection.executemany(
            "INSERT OR IGNORE INTO job_auto_hidden (job_id, reason, detail, hidden_at) VALUES (?, ?, ?, ?)", to_hide)
        connection.executemany(
            "DELETE FROM job_auto_hidden WHERE job_id = ? AND reason = 'auto_stale' AND restored_at IS NULL",
            [(job_id,) for job_id in to_unhide])
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
    stats.newly_hidden, stats.unhidden = len(to_hide), len(to_unhide)
    stats.hidden_by_source = hidden_by_source(connection)
    return stats


def hidden_by_source(connection: sqlite3.Connection) -> dict[str, int]:
    """Active auto-hidden jobs per source (a job sighted by two sources counts for both)."""
    return {row[0]: int(row[1]) for row in connection.execute(
        """SELECT s.source, COUNT(DISTINCT s.job_id) FROM job_sightings s JOIN job_auto_hidden h ON h.job_id = s.job_id
           WHERE h.restored_at IS NULL GROUP BY s.source ORDER BY 2 DESC""")}


def hide_job(connection: sqlite3.Connection, job_id: int, reason: str, detail: str, now_iso: str) -> None:
    """Move one job to Auto-hidden (used by the liveness checks). A restored job is hidden again only by an explicit check."""
    connection.execute(
        """INSERT INTO job_auto_hidden (job_id, reason, detail, hidden_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(job_id) DO UPDATE SET reason = excluded.reason, detail = excluded.detail,
               hidden_at = excluded.hidden_at, restored_at = NULL""", (job_id, reason, detail[:300], now_iso))


def restore_job(connection: sqlite3.Connection, job_id: int, now_iso: str) -> bool:
    """One-click Restore: visible again, and never hidden automatically again. A job closed by a check is reopened."""
    row = connection.execute("SELECT reason, restored_at FROM job_auto_hidden WHERE job_id = ?", (job_id,)).fetchone()
    if row is None or row["restored_at"] is not None:
        return False
    connection.execute("UPDATE job_auto_hidden SET restored_at = ? WHERE job_id = ?", (now_iso, job_id))
    if row["reason"] == CLOSED_CHECKED:
        lifecycle.reopen_job(connection, job_id, only_reason=lifecycle.CLOSED_CHECKED)
    return True
