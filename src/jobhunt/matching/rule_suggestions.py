"""Rule proposals mined from my decisions. They are only ever PROPOSALS: nothing is applied without Approve.

Two kinds, both needing evidence from at least `MIN_SUPPORT` jobs (learned_rules.MIN_SUPPORT):
* `title_phrase`: a title word that appears in the titles of jobs I marked wrong_role / too_senior / wrong_domain_or_stack
  (old dismiss reasons count) and rarely in the jobs I kept;
* `eligibility_phrase`: a short phrase from the postings of jobs I marked `ineligible_*` that shows up in at least 5 of them
  (shown with how many jobs I did not mark contain it too).
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from jobhunt.classify import normalize_for_matching
from jobhunt.decisions import FROM_LEGACY_REASON, ROLE_FIT_NEGATIVE_REASONS
from jobhunt.matching.feedback import _STOP, _tokens
from jobhunt.matching.learned_rules import MIN_SUPPORT

MIN_PRECISION = 0.8
_ELIGIBILITY_WORDS = re.compile(r"(?i)ph\.?d|doctora|master|clearance|citizen|enrolled|pursuing|graduate student|sponsor")
_WORDS = re.compile(r"[a-z][a-z0-9']{1,}")


@dataclass
class Proposal:
    key: str                          # title_phrase:researcher
    kind: str
    phrase: str
    count: int                        # supporting jobs
    job_ids: list[int]
    kept: int                         # similar jobs I did NOT mark (a high number weakens the proposal)
    reason: str                       # what the supporting decisions said
    demote: bool = True
    penalty: float = 8.0
    examples: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float:
        total = self.count + self.kept
        return self.count / total if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "phrase": self.phrase, "count": self.count,
                "job_ids": self.job_ids[:20], "kept": self.kept, "reason": self.reason, "demote": self.demote,
                "penalty": self.penalty, "precision": round(self.precision, 2), "examples": self.examples[:3]}


def _decided_reasons(connection: sqlite3.Connection) -> dict[int, str]:
    """job_id -> current not-applying reason (new decisions, else old dismiss reasons mapped)."""
    reasons: dict[int, str] = {}
    for row in connection.execute("SELECT job_id, reason FROM job_feedback_current WHERE action = 'dismiss'"):
        reasons[int(row["job_id"])] = FROM_LEGACY_REASON.get(row["reason"] or "", row["reason"] or "other")
    for row in connection.execute("SELECT job_id, reason FROM job_decision_current WHERE action = 'not_applying'"):
        reasons[int(row["job_id"])] = row["reason"] or "other"
    return reasons


def _shingles(text: str) -> set[str]:
    out: set[str] = set()
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", text[:20000]):
        if not _ELIGIBILITY_WORDS.search(sentence):
            continue
        words = _WORDS.findall(sentence.lower())
        out.update(" ".join(words[i:i + 3]) for i in range(len(words) - 2))
    return {s for s in out if _ELIGIBILITY_WORDS.search(s)}


def suggest(connection: sqlite3.Connection, *, limit: int = 15) -> list[Proposal]:
    reasons = _decided_reasons(connection)
    handled = {row[0] for row in connection.execute("SELECT proposal_key FROM rule_decisions")}
    proposals: list[Proposal] = []

    # title phrases from role-fit negatives
    negatives = [job_id for job_id, reason in reasons.items() if reason in ROLE_FIT_NEGATIVE_REASONS]
    if negatives:
        marks = ",".join("?" for _ in negatives)
        titles = {int(r["id"]): r["title"] for r in connection.execute(
            f"SELECT id, title FROM jobs WHERE id IN ({marks})", negatives)}
        kept_tokens: Counter[str] = Counter()
        for row in connection.execute(
                """SELECT j.id, j.title FROM jobs j JOIN job_relevance r ON r.job_id = j.id
                   WHERE j.status = 'open' AND r.gate_status IN ('target', 'maybe')"""):
            if int(row["id"]) not in reasons:
                kept_tokens.update(_tokens(row["title"]))
        by_token: dict[str, list[int]] = defaultdict(list)
        for job_id, title in titles.items():
            for token in _tokens(title) - _STOP:
                by_token[token].append(job_id)
        for token, ids in by_token.items():
            kinds = Counter(reasons[i] for i in ids)
            proposal = Proposal(f"title_phrase:{token}", "title_phrase", token, len(ids), sorted(ids), kept_tokens.get(token, 0),
                                ", ".join(f"{k} x{v}" for k, v in kinds.most_common()),
                                examples=[titles[i] for i in ids[:3]])
            if proposal.count >= MIN_SUPPORT and proposal.precision >= MIN_PRECISION and proposal.key not in handled:
                proposals.append(proposal)

    # eligibility phrases from ineligible_* decisions
    ineligible = [job_id for job_id, reason in reasons.items() if reason.startswith("ineligible")]
    if len(ineligible) >= MIN_SUPPORT:
        marks = ",".join("?" for _ in ineligible)
        counts: Counter[str] = Counter()
        owners: dict[str, list[int]] = defaultdict(list)
        for row in connection.execute(f"SELECT id, description_text FROM jobs WHERE id IN ({marks})", ineligible):
            for shingle in _shingles(row["description_text"] or ""):
                counts[shingle] += 1
                owners[shingle].append(int(row["id"]))
        candidates = [s for s, n in counts.most_common(60) if n >= MIN_SUPPORT]
        for shingle in candidates[:10]:
            normalized = normalize_for_matching(shingle)
            others = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'open' AND id NOT IN (%s) AND LOWER(description_text) LIKE ?" % marks,
                [*ineligible, f"%{shingle}%"]).fetchone()[0]
            proposal = Proposal(f"eligibility_phrase:{normalized}", "eligibility_phrase", shingle, counts[shingle],
                                sorted(owners[shingle]), int(others), "ineligible_* decisions")
            if proposal.precision >= 0.5 and proposal.key not in handled:
                proposals.append(proposal)

    proposals.sort(key=lambda p: (-p.count, p.kept, p.phrase))
    return proposals[:limit]
