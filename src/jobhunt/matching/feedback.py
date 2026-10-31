"""Dismiss / like / undo feedback and the rule suggestions mined from it.

`suggest_rules` only ever READS: it prints proposed `relevance.titles.exclude` phrases and
companies to ignore, with the evidence, and the person decides whether to paste them into
`config.yaml`. Nothing here writes configuration.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from jobhunt.classify import normalize_for_matching
from jobhunt.errors import JobHuntError
from jobhunt.timeutil import Clock, to_iso, utc_now

ACTIONS = ("dismiss", "like", "undo")
REASONS = ("wrong_role", "wrong_stack", "too_senior", "wrong_location", "company", "other")

_STOP = frozenset(
    "the and for with from into new junior senior intern internship engineer engineering developer software "
    "associate summer winter fall spring class graduate grad program team role remote hybrid onsite full time part".split()
)


class FeedbackError(JobHuntError):
    """Invalid feedback input."""


def record_feedback(
    connection: sqlite3.Connection, job_id: int, action: str, reason: str | None = None, *, clock: Clock = utc_now
) -> int:
    if action not in ACTIONS:
        raise FeedbackError(f"unknown action {action!r}")
    if reason is not None and reason not in REASONS:
        raise FeedbackError(f"unknown reason {reason!r}")
    if connection.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
        raise FeedbackError(f"job {job_id} not found")
    cursor = connection.execute(
        "INSERT INTO job_feedback (job_id, action, reason, created_at) VALUES (?, ?, ?, ?)",
        (job_id, action, reason if action == "dismiss" else None, to_iso(clock())),
    )
    return int(cursor.lastrowid)


@dataclass
class Suggestion:
    kind: str                 # title_phrase | company
    value: str
    dismissed: int
    kept: int
    examples: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float:
        total = self.dismissed + self.kept
        return self.dismissed / total if total else 0.0


@dataclass
class SuggestionReport:
    dismissed_total: int
    by_reason: dict[str, int]
    suggestions: list[Suggestion]


def _tokens(title: str) -> set[str]:
    words = re.findall(r"[a-z][a-z0-9+#.]{2,}", normalize_for_matching(title))
    return {word for word in words if word not in _STOP}


def suggest_rules(
    connection: sqlite3.Connection, *, min_support: int = 3, min_precision: float = 0.8, limit: int = 15
) -> SuggestionReport:
    """Title words and companies that the dismissals say I do not want (read-only)."""
    dismissed = connection.execute(
        """SELECT j.id, j.title, j.company_name, f.reason
           FROM job_feedback_current f JOIN jobs j ON j.id = f.job_id WHERE f.action = 'dismiss'"""
    ).fetchall()
    by_reason = Counter((row["reason"] or "unspecified") for row in dismissed)
    if not dismissed:
        return SuggestionReport(0, {}, [])

    kept = connection.execute(
        """SELECT j.title, j.company_name FROM jobs j JOIN job_relevance r ON r.job_id = j.id
           LEFT JOIN job_feedback_current f ON f.job_id = j.id
           WHERE j.status = 'open' AND r.gate_status IN ('target', 'maybe')
             AND (f.action IS NULL OR f.action != 'dismiss')"""
    ).fetchall()

    kept_tokens: Counter[str] = Counter()
    kept_companies: Counter[str] = Counter()
    for row in kept:
        kept_tokens.update(_tokens(row["title"]))
        kept_companies[row["company_name"].lower()] += 1

    token_jobs: dict[str, list[str]] = defaultdict(list)
    company_jobs: dict[str, list[str]] = defaultdict(list)
    company_reasons: Counter[str] = Counter()
    for row in dismissed:
        for token in _tokens(row["title"]):
            token_jobs[token].append(row["title"])
        company_jobs[row["company_name"]].append(row["title"])
        if row["reason"] == "company":
            company_reasons[row["company_name"]] += 1

    suggestions: list[Suggestion] = []
    for token, titles in token_jobs.items():
        suggestion = Suggestion("title_phrase", token, len(titles), kept_tokens.get(token, 0), titles[:3])
        if suggestion.dismissed >= min_support and suggestion.precision >= min_precision:
            suggestions.append(suggestion)
    for company, titles in company_jobs.items():
        suggestion = Suggestion("company", company, len(titles), kept_companies.get(company.lower(), 0), titles[:3])
        explicit = company_reasons.get(company, 0) >= 2
        if explicit or (suggestion.dismissed >= min_support and suggestion.precision >= min_precision):
            suggestions.append(suggestion)

    suggestions.sort(key=lambda item: (-item.dismissed, item.kept, item.value))
    return SuggestionReport(len(dismissed), dict(by_reason), suggestions[:limit])


def format_suggestions(report: SuggestionReport) -> str:
    if report.dismissed_total == 0:
        return "No dismissed jobs yet: dismiss a few from the site (with a reason) and run this again."
    lines = [
        f"{report.dismissed_total} dismissed job(s). By reason: "
        + ", ".join(f"{reason}={count}" for reason, count in sorted(report.by_reason.items())),
        "",
    ]
    if not report.suggestions:
        lines.append("No title word or company is dismissed often enough to suggest a rule yet.")
        return "\n".join(lines)
    lines.append("Suggestions (nothing is changed; copy what you agree with into config.yaml):")
    phrases = [item for item in report.suggestions if item.kind == "title_phrase"]
    companies = [item for item in report.suggestions if item.kind == "company"]
    if phrases:
        lines.append("")
        lines.append("  relevance.titles.exclude: add")
        for item in phrases:
            lines.append(f"    - {item.value}    (dismissed {item.dismissed}, kept {item.kept}; e.g. "
                         f"{'; '.join(item.examples[:2])})")
    if companies:
        lines.append("")
        lines.append("  companies you keep dismissing (config has no company list yet; consider a title/stack rule):")
        for item in companies:
            lines.append(f"    - {item.value}    (dismissed {item.dismissed}, kept {item.kept})")
    return "\n".join(lines)
