"""Unified labels for the triage cascade (v9).

Builds a single training-label view with `(job_id, head, label, weight, source, confidence)`:
- Human (weight 5.0): applied/saved = not senior and India-eligible; too_senior = senior;
  wrong_location = not India-eligible; ineligible_* feeds the eligibility head; "LLM verdict is wrong"
  flips the corresponding verdict label. Unrelated reasons (closed, too_old, company, etc.) are ignored.
- Verified LLM verdicts (weight 2.0 for Gemini, 1.0 for local LLM): only fields whose quote verified.
- Weak labels from deterministic parsers (weight 0.3): only functions whose measured precision on the
  human + verified set is >= 90%.
- Soft regression target (head `fit_proxy`): stored Gemini fit score (0..100).
- Evaluation contract: evaluation uses ONLY human labels and verified Gemini verdicts (`eval_usable = True`),
  never rule-derived labels or local LLM labels, and never a job's own gate decision.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from jobhunt.config import Config
from jobhunt.matching.triage import Verdict, quote_in
from jobhunt.matching.years import required_years

logger = logging.getLogger(__name__)

# Weight hierarchy
WEIGHT_HUMAN = 5.0
WEIGHT_GEMINI = 2.0
WEIGHT_LOCAL_LLM = 1.0
WEIGHT_WEAK = 0.3

HEADS = ("seniority", "location", "eligibility", "fit_proxy")

# Senior title patterns for weak labeling
_STRONG_SENIOR_TITLE = re.compile(
    r"(?i)\b(?:senior|sr\.?|principal|lead|staff|director|vp|vice president|architect|head of|manager)\b"
)

# Explicit non-India location phrases for weak labeling
_EXPLICIT_NON_INDIA = re.compile(
    r"(?i)\b(?:us citizens? only|u\.s\. citizens? only|must reside in the us|must reside in the u\.s\.|"
    r"authorized to work in the us only|only candidates located in|us only|u\.s\. only)\b"
)


@dataclass(frozen=True)
class UnifiedLabel:
    """One training or evaluation label for a specific triage head."""

    job_id: int
    head: str                          # "seniority" | "location" | "eligibility" | "fit_proxy"
    label: float                       # 1.0 (positive) | 0.0 (negative) | 0..100 (fit_proxy)
    weight: float                      # 5.0 (human) | 2.0 (gemini) | 1.0 (local_llm) | 0.3 (weak)
    source: str                        # "human_application", "human_decision", "human_correction", "gemini_verdict", "local_llm_verdict", "weak_rule", "gemini_fit"
    confidence: float                  # 0.0 .. 1.0
    eval_usable: bool                  # True only for human and verified gemini; False for weak rules
    evidence: str = ""
    company: str = ""
    created_at: str = ""

    @property
    def is_positive(self) -> bool:
        return self.label >= 0.5


def parse_weak_years_ge_2(description: str | None) -> bool:
    """True if deterministic parser finds required years lower bound >= 2."""
    if not description:
        return False
    years, _ = required_years(description)
    return years is not None and years >= 2


def parse_weak_senior_title(title: str | None) -> bool:
    """True if title contains strong seniority tokens."""
    return bool(title and _STRONG_SENIOR_TITLE.search(title))


def parse_weak_non_india(text: str | None) -> bool:
    """True if text contains explicit US/abroad-only residency phrases."""
    return bool(text and _EXPLICIT_NON_INDIA.search(text))


def _extract_human_labels(connection: sqlite3.Connection) -> dict[tuple[int, str], UnifiedLabel]:
    """Extract human ground-truth labels from applications, job_decisions, and job_feedback."""
    labels: dict[tuple[int, str], UnifiedLabel] = {}
    prev_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        # 1. Applications & Saved
        query_applied_saved = """
            SELECT j.id AS job_id, j.company_name, j.first_seen_at
            FROM jobs j
            WHERE EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id)
               OR EXISTS (SELECT 1 FROM job_decision_current d WHERE d.job_id = j.id AND d.action IN ('applied', 'saved'))
        """
        for row in connection.execute(query_applied_saved):
            jid = int(row["job_id"])
            comp = row["company_name"] or ""
            created = row["first_seen_at"] or ""
            labels[(jid, "seniority")] = UnifiedLabel(
                job_id=jid, head="seniority", label=0.0, weight=WEIGHT_HUMAN,
                source="human_application", confidence=1.0, eval_usable=True,
                evidence="applied/saved job", company=comp, created_at=created,
            )
            labels[(jid, "location")] = UnifiedLabel(
                job_id=jid, head="location", label=0.0, weight=WEIGHT_HUMAN,
                source="human_application", confidence=1.0, eval_usable=True,
                evidence="applied/saved job", company=comp, created_at=created,
            )
            labels[(jid, "eligibility")] = UnifiedLabel(
                job_id=jid, head="eligibility", label=0.0, weight=WEIGHT_HUMAN,
                source="human_application", confidence=1.0, eval_usable=True,
                evidence="applied/saved job", company=comp, created_at=created,
            )

        # 2. Not-applying decisions (job_decision_current)
        query_decisions = """
            SELECT d.job_id, d.action, d.reason, j.company_name, d.created_at
            FROM job_decision_current d
            JOIN jobs j ON j.id = d.job_id
            WHERE d.action = 'not_applying'
        """
        for row in connection.execute(query_decisions):
            jid = int(row["job_id"])
            reason = row["reason"] or ""
            comp = row["company_name"] or ""
            created = row["created_at"] or ""

            if reason == "too_senior":
                labels[(jid, "seniority")] = UnifiedLabel(
                    job_id=jid, head="seniority", label=1.0, weight=WEIGHT_HUMAN,
                    source="human_decision", confidence=1.0, eval_usable=True,
                    evidence=f"dismissed: {reason}", company=comp, created_at=created,
                )
            elif reason == "wrong_location":
                labels[(jid, "location")] = UnifiedLabel(
                    job_id=jid, head="location", label=1.0, weight=WEIGHT_HUMAN,
                    source="human_decision", confidence=1.0, eval_usable=True,
                    evidence=f"dismissed: {reason}", company=comp, created_at=created,
                )
            elif reason.startswith("ineligible_"):
                labels[(jid, "eligibility")] = UnifiedLabel(
                    job_id=jid, head="eligibility", label=1.0, weight=WEIGHT_HUMAN,
                    source="human_decision", confidence=1.0, eval_usable=True,
                    evidence=f"dismissed: {reason}", company=comp, created_at=created,
                )

        # 3. Dismiss feedback (job_feedback_current)
        query_feedback = """
            SELECT f.job_id, f.action, f.reason, j.company_name, f.created_at
            FROM job_feedback_current f
            JOIN jobs j ON j.id = f.job_id
            WHERE f.action = 'dismiss'
        """
        for row in connection.execute(query_feedback):
            jid = int(row["job_id"])
            reason = row["reason"] or ""
            comp = row["company_name"] or ""
            created = row["created_at"] or ""

            if reason == "too_senior" and (jid, "seniority") not in labels:
                labels[(jid, "seniority")] = UnifiedLabel(
                    job_id=jid, head="seniority", label=1.0, weight=WEIGHT_HUMAN,
                    source="human_feedback", confidence=1.0, eval_usable=True,
                    evidence=f"dismissed: {reason}", company=comp, created_at=created,
                )
            elif reason == "wrong_location" and (jid, "location") not in labels:
                labels[(jid, "location")] = UnifiedLabel(
                    job_id=jid, head="location", label=1.0, weight=WEIGHT_HUMAN,
                    source="human_feedback", confidence=1.0, eval_usable=True,
                    evidence=f"dismissed: {reason}", company=comp, created_at=created,
                )

        # 4. Audit labels (job_audit_labels from Brief v10b Section 5)
        try:
            query_audit = """
                SELECT a.job_id, a.head, a.label, a.my_verdict, a.my_note, a.created_at, j.company_name
                FROM job_audit_labels a
                JOIN jobs j ON j.id = a.job_id
            """
            for row in connection.execute(query_audit):
                jid = int(row["job_id"])
                head = row["head"]
                lbl = float(row["label"])
                comp = row["company_name"] or ""
                created = row["created_at"] or ""
                labels[(jid, head)] = UnifiedLabel(
                    job_id=jid, head=head, label=lbl, weight=WEIGHT_HUMAN,
                    source="audit", confidence=1.0, eval_usable=True,
                    evidence=f"audit: {row['my_verdict']} ({row['my_note'] or ''})",
                    company=comp, created_at=created,
                )
        except sqlite3.OperationalError:
            pass
    finally:
        connection.row_factory = prev_factory

    return labels


def _extract_verdict_labels(
    connection: sqlite3.Connection,
    rubric_version: str,
    *,
    include_silver: bool = False,
    silver_weight: float = 0.3,
) -> dict[tuple[int, str], UnifiedLabel]:
    """Extract labels from verified verdicts in job_llm_verdicts, with human corrections taking precedence."""
    labels: dict[tuple[int, str], UnifiedLabel] = {}
    prev_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        cols = [r[1] for r in connection.execute("PRAGMA table_info(job_llm_verdicts)").fetchall()]
        has_source = "source" in cols

        source_col = "v.source" if has_source else "'gemini' AS source"
        query = f"""
            SELECT v.id, v.job_id, v.model, {source_col}, v.verified_json, v.created_at,
                   v.human_label, v.human_reason, j.company_name
            FROM job_llm_verdicts v
            JOIN jobs j ON j.id = v.job_id
            WHERE v.rubric_version = ?
            ORDER BY CASE WHEN (v.source = 'gemini' OR v.model LIKE '%gemini%') THEN 1 ELSE 0 END ASC, v.id ASC
        """
        rows = connection.execute(query, (rubric_version,)).fetchall()
    finally:
        connection.row_factory = prev_factory

    for row in rows:
        jid = int(row["job_id"])
        model = row["model"] or ""
        v_source = row["source"] if has_source else ("gemini" if "gemini" in model.lower() else "local_llm")
        is_gemini = v_source == "gemini" or "gemini" in model.lower()

        # Brief v10b Section 6: Stop training on local-LLM silver labels by default.
        if not is_gemini and not include_silver:
            continue

        base_weight = WEIGHT_GEMINI if is_gemini else silver_weight
        source_name = "gemini_verdict" if is_gemini else "local_llm_verdict"
        eval_usable = is_gemini  # only Gemini verdicts are usable for evaluation alongside human labels
        comp = row["company_name"] or ""
        created = row["created_at"] or ""
        human_label = row["human_label"]

        try:
            v_data = json.loads(row["verified_json"])
            verdict = Verdict.from_json(v_data)
        except Exception:
            continue

        conf_mult = 0.95 if verdict.confidence == "high" else (0.75 if verdict.confidence == "medium" else 0.50)

        # 1. Seniority head
        if human_label in ("wrong_level", "wrong_years"):
            # Human correction flips the verdict
            labels[(jid, "seniority")] = UnifiedLabel(
                job_id=jid, head="seniority", label=0.0, weight=WEIGHT_HUMAN,
                source="human_correction", confidence=1.0, eval_usable=True,
                evidence=f"corrected by user: {human_label}", company=comp, created_at=created,
            )
        else:
            # Positive: verified senior/lead OR verified years >= 2
            is_senior = (verdict.level in ("senior", "lead_or_manager") and bool(verdict.level_quote)) or \
                        (verdict.required_years_min is not None and verdict.required_years_min >= 2 and bool(verdict.years_quote))
            # Negative: verified entry/intern OR verified years < 2
            is_entry = (verdict.level in ("entry", "intern") and bool(verdict.level_quote)) or \
                       (verdict.required_years_min is not None and verdict.required_years_min < 2 and bool(verdict.years_quote))

            if is_senior:
                evidence = verdict.level_quote if verdict.level in ("senior", "lead_or_manager") else verdict.years_quote
                labels[(jid, "seniority")] = UnifiedLabel(
                    job_id=jid, head="seniority", label=1.0, weight=base_weight,
                    source=source_name, confidence=conf_mult, eval_usable=eval_usable,
                    evidence=evidence, company=comp, created_at=created,
                )
            elif is_entry:
                evidence = verdict.level_quote or verdict.years_quote
                labels[(jid, "seniority")] = UnifiedLabel(
                    job_id=jid, head="seniority", label=0.0, weight=base_weight,
                    source=source_name, confidence=conf_mult, eval_usable=eval_usable,
                    evidence=evidence, company=comp, created_at=created,
                )

        # 2. Location head
        if human_label == "wrong_location":
            labels[(jid, "location")] = UnifiedLabel(
                job_id=jid, head="location", label=0.0, weight=WEIGHT_HUMAN,
                source="human_correction", confidence=1.0, eval_usable=True,
                evidence="corrected by user: wrong_location", company=comp, created_at=created,
            )
        else:
            if verdict.india_eligible == "no" and verdict.india_quote:
                labels[(jid, "location")] = UnifiedLabel(
                    job_id=jid, head="location", label=1.0, weight=base_weight,
                    source=source_name, confidence=conf_mult, eval_usable=eval_usable,
                    evidence=verdict.india_quote, company=comp, created_at=created,
                )
            elif verdict.india_eligible == "yes" and verdict.india_quote:
                labels[(jid, "location")] = UnifiedLabel(
                    job_id=jid, head="location", label=0.0, weight=base_weight,
                    source=source_name, confidence=conf_mult, eval_usable=eval_usable,
                    evidence=verdict.india_quote, company=comp, created_at=created,
                )

        # 3. Eligibility head
        if human_label == "wrong_eligibility":
            labels[(jid, "eligibility")] = UnifiedLabel(
                job_id=jid, head="eligibility", label=0.0, weight=WEIGHT_HUMAN,
                source="human_correction", confidence=1.0, eval_usable=True,
                evidence="corrected by user: wrong_eligibility", company=comp, created_at=created,
            )
        else:
            if verdict.eligibility_blockers and verdict.blocker_quote:
                labels[(jid, "eligibility")] = UnifiedLabel(
                    job_id=jid, head="eligibility", label=1.0, weight=base_weight,
                    source=source_name, confidence=conf_mult, eval_usable=eval_usable,
                    evidence=verdict.blocker_quote, company=comp, created_at=created,
                )
            elif not verdict.eligibility_blockers and verdict.confidence in ("high", "medium"):
                labels[(jid, "eligibility")] = UnifiedLabel(
                    job_id=jid, head="eligibility", label=0.0, weight=base_weight,
                    source=source_name, confidence=conf_mult * 0.8, eval_usable=eval_usable,
                    evidence="no blockers cited", company=comp, created_at=created,
                )

    return labels


def measure_weak_rule_precision(
    rule_fn: Callable[[sqlite3.Row], bool],
    head: str,
    ground_truth: dict[tuple[int, str], UnifiedLabel],
    connection: sqlite3.Connection,
) -> tuple[float, int, int]:
    """Measure precision of a deterministic parsing function against human+verified ground truth.

    Returns (precision, hits, positives).
    """
    hits = 0
    positives = 0
    gt_job_ids = {jid for (jid, h) in ground_truth if h == head}
    if not gt_job_ids:
        return 0.0, 0, 0

    placeholders = ",".join("?" for _ in gt_job_ids)
    query = f"""
        SELECT id, title, description_text, location_raw, remote_regions
        FROM jobs
        WHERE id IN ({placeholders})
    """
    for row in connection.execute(query, list(gt_job_ids)):
        jid = int(row["id"])
        target = ground_truth.get((jid, head))
        if target is None:
            continue

        if rule_fn(row):
            hits += 1
            if target.label >= 0.5:
                positives += 1

    precision = (positives / hits) if hits > 0 else 0.0
    return precision, hits, positives


def build_unified_labels(
    connection: sqlite3.Connection,
    config: Config,
    *,
    include_weak: bool = True,
    weak_precision_threshold: float = 0.90,
    include_silver: bool = False,
    silver_weight: float = 0.3,
) -> dict[str, list[UnifiedLabel]]:
    """Build unified training and evaluation labels for all heads.

    Priority per (job_id, head):
      Human correction / decision / app / audit > Verified verdict > Weak label

    Evaluation uses ONLY labels with `eval_usable == True` (human + verified Gemini).
    """
    rubric_version = config.triage.rubric_version

    # Step 1: Extract human labels and verified verdicts
    human_labels = _extract_human_labels(connection)
    verdict_labels = _extract_verdict_labels(
        connection,
        rubric_version,
        include_silver=include_silver,
        silver_weight=silver_weight,
    )

    # Combine ground truth (Human wins over Verdict)
    ground_truth: dict[tuple[int, str], UnifiedLabel] = {}
    for key, vlabel in verdict_labels.items():
        ground_truth[key] = vlabel
    for key, hlabel in human_labels.items():
        ground_truth[key] = hlabel

    # Step 2: Measure precision of weak labeling functions on the ground truth
    weak_parsers: list[tuple[str, Callable[[sqlite3.Row], bool], str]] = [
        ("seniority", lambda r: parse_weak_years_ge_2(r["description_text"]), "years>=2 parser"),
        ("seniority", lambda r: parse_weak_senior_title(r["title"]), "senior title token"),
        ("location", lambda r: parse_weak_non_india(f"{r['location_raw'] or ''} {r['description_text'] or ''}"), "explicit US-only phrase"),
    ]

    approved_weak: list[tuple[str, Callable[[sqlite3.Row], bool], str, float]] = []
    if include_weak:
        for head, fn, desc in weak_parsers:
            prec, hits, pos = measure_weak_rule_precision(fn, head, ground_truth, connection)
            if hits >= 5 and prec >= weak_precision_threshold:
                logger.info("Weak parser '%s' on head '%s' qualified: prec=%.3f (%d/%d)", desc, head, prec, pos, hits)
                approved_weak.append((head, fn, desc, prec))
            else:
                logger.info("Weak parser '%s' on head '%s' rejected: prec=%.3f (%d/%d)", desc, head, prec, pos, hits)

    # Step 3: Weak labels on unlabelled open target/maybe jobs
    weak_labels: dict[tuple[int, str], UnifiedLabel] = {}
    if approved_weak:
        query_candidates = """
            SELECT j.id, j.title, j.description_text, j.location_raw, j.company_name, j.first_seen_at
            FROM jobs j
            JOIN job_relevance r ON r.job_id = j.id
            WHERE j.status = 'open' AND r.gate_status IN ('target', 'maybe')
            ORDER BY j.id DESC
            LIMIT 2000
        """
        for row in connection.execute(query_candidates):
            jid = int(row["id"])
            comp = row["company_name"] or ""
            created = row["first_seen_at"] or ""
            for head, fn, desc, prec in approved_weak:
                if (jid, head) in ground_truth:
                    continue  # already has higher-priority ground truth
                if (jid, head) in weak_labels:
                    continue
                if fn(row):
                    weak_labels[(jid, head)] = UnifiedLabel(
                        job_id=jid, head=head, label=1.0, weight=WEIGHT_WEAK,
                        source="weak_rule", confidence=prec * 0.9, eval_usable=False,
                        evidence=f"weak parser: {desc}", company=comp, created_at=created,
                    )

    # Step 4: Soft target fit_proxy (Gemini fit score 0..100)
    fit_labels: dict[tuple[int, str], UnifiedLabel] = {}
    query_fit = """
        SELECT r.job_id, r.llm_score, j.company_name, j.first_seen_at
        FROM job_relevance r
        JOIN jobs j ON j.id = r.job_id
        WHERE r.llm_score IS NOT NULL
    """
    for row in connection.execute(query_fit):
        jid = int(row["job_id"])
        score = float(row["llm_score"])
        comp = row["company_name"] or ""
        created = row["first_seen_at"] or ""
        fit_labels[(jid, "fit_proxy")] = UnifiedLabel(
            job_id=jid, head="fit_proxy", label=score, weight=1.0,
            source="gemini_fit", confidence=0.85, eval_usable=True,
            evidence=f"gemini fit score: {score:.1f}", company=comp, created_at=created,
        )

    # Merge all into per-head lists
    per_head: dict[str, list[UnifiedLabel]] = {h: [] for h in HEADS}

    # Order of precedence: ground_truth (human & verified) -> weak_labels
    all_keys = set(ground_truth.keys()) | set(weak_labels.keys()) | set(fit_labels.keys())
    for key in all_keys:
        head = key[1]
        if key in ground_truth:
            per_head[head].append(ground_truth[key])
        elif key in weak_labels:
            per_head[head].append(weak_labels[key])
        elif key in fit_labels:
            per_head[head].append(fit_labels[key])

    for h in per_head:
        per_head[h].sort(key=lambda u: u.job_id)

    return per_head


def get_eval_dataset(labels: list[UnifiedLabel]) -> list[UnifiedLabel]:
    """Return only labels usable for evaluation (human labels + verified Gemini verdicts).

    Weak parser labels and local LLM labels are never included in evaluation.
    """
    return [u for u in labels if u.eval_usable]


__all__ = [
    "HEADS",
    "UnifiedLabel",
    "WEIGHT_GEMINI",
    "WEIGHT_HUMAN",
    "WEIGHT_LOCAL_LLM",
    "WEIGHT_WEAK",
    "build_unified_labels",
    "get_eval_dataset",
    "measure_weak_rule_precision",
    "parse_weak_non_india",
    "parse_weak_senior_title",
    "parse_weak_years_ge_2",
]
