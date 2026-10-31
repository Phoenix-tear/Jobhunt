"""Which verdicts may act on which jobs. A verdict only ever reaches the gate and the location step through
`usable_verdicts`, so the safety rules live in one place:

* a job I applied to, saved, tailored a resume for, added by hand, or labelled "yes" in the gold file is never moved;
* a job whose verdict I marked "LLM verdict is wrong" is exempt from every LLM rule.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from jobhunt.config import Config
from jobhunt.matching.triage import StoredVerdict, load_verdicts

GOLD_FILES = ("data/eval/gold_labeled.csv", "data/eval/gold.csv")
_YES = {"yes", "y", "1", "true", "relevant"}
YEARS_PENALTY = -0.10                  # rank_adjust (x rank_adjust_scale points) for a verdict that caps a job at maybe

_PROTECTED_SQL = """SELECT j.id FROM jobs j WHERE
    EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id)
    OR EXISTS (SELECT 1 FROM job_decision_current d WHERE d.job_id = j.id AND d.action IN ('saved', 'applied'))
    OR EXISTS (SELECT 1 FROM tailored_resumes t WHERE t.job_id = j.id)
    OR EXISTS (SELECT 1 FROM job_sightings s WHERE s.job_id = j.id AND s.source = 'manual')"""


def gold_yes_ids(config: Config) -> set[int]:
    """Job ids I labelled relevant in the gold file (the first gold file that exists)."""
    for name in GOLD_FILES:
        path: Path = config.resolve_path(name)
        if not path.is_file():
            continue
        found: set[int] = set()
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if (row.get("relevant") or "").strip().lower() in _YES and (row.get("job_id") or "").strip().isdigit():
                    found.add(int(row["job_id"]))
        if found:
            return found
    return set()


def protected_ids(connection: sqlite3.Connection, config: Config) -> set[int]:
    return {int(row[0]) for row in connection.execute(_PROTECTED_SQL)} | gold_yes_ids(config)


def usable_and_exempt(connection: sqlite3.Connection, config: Config) -> tuple[dict[int, StoredVerdict], set[int]]:
    """(the newest verdict per job that may change that job, the job ids no LLM verdict or student opinion may touch).
    Exempt: protected jobs and jobs whose verdict I marked wrong."""
    if not config.triage.enabled:
        return {}, set()
    protected = protected_ids(connection, config)
    stored = load_verdicts(connection, config.triage.rubric_version)
    exempt = protected | {job_id for job_id, item in stored.items() if item.human_label}
    return {job_id: item for job_id, item in stored.items() if job_id not in exempt}, exempt


def usable_verdicts(connection: sqlite3.Connection, config: Config) -> dict[int, StoredVerdict]:
    """The newest verdict per job that may change that job: none for protected jobs or ones I marked wrong."""
    return usable_and_exempt(connection, config)[0]


def verdict_key(stored: StoredVerdict | None) -> str:
    """Goes into the relevance input hash, so a new, changed or withdrawn verdict recomputes the job."""
    return f"{stored.id}" if stored else ""
