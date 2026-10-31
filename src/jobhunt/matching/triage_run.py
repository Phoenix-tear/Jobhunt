"""Who gets judged, and when: the daily shortlist guard and the resumable backfill.

Both only choose job ids and limits; the judging, caching and verification are `matching/triage.py`.
A job with an unchanged posting and the current rubric is never judged twice, so repeated runs and a restarted backfill
cost nothing for what is already done.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from jobhunt.config import Config
from jobhunt.matching.budget import BudgetGuard, UsageLedger
from jobhunt.matching.gemini import GeminiClient
from jobhunt.matching.triage import TriageJudge, TriageRun, cached_ids
from jobhunt.matching.triage_apply import protected_ids
from jobhunt.timeutil import Clock, utc_now

# The "Relevant" population: open, target or maybe, location visible, not auto-hidden (same as the page and the reports).
_RELEVANT_SQL = """
SELECT j.id FROM jobs j JOIN job_relevance r ON r.job_id = j.id
WHERE j.status = 'open' AND r.gate_status IN ('target', 'maybe')
  AND COALESCE(r.location_fit, 'unknown') IN ('india', 'remote_open_to_india', 'remote_unclear', 'unknown')
  AND COALESCE(j.description_text, '') != ''
  AND NOT EXISTS (SELECT 1 FROM job_auto_hidden h WHERE h.job_id = j.id AND h.restored_at IS NULL)
ORDER BY COALESCE(r.final_score, r.local_rank_score * 100, 0) DESC, COALESCE(r.similarity_percentile, 0) DESC, j.id"""


def relevant_ids(connection: sqlite3.Connection, config: Config) -> list[int]:
    """Relevant jobs, best-ranked first, without the ones a verdict may never touch (applied, saved, tailored, gold-yes)."""
    protected = protected_ids(connection, config)
    return [int(row[0]) for row in connection.execute(_RELEVANT_SQL) if int(row[0]) not in protected]


def unjudged_gemini(connection: sqlite3.Connection, config: Config, job_ids: list[int], judge: TriageJudge) -> list[int]:
    evidence = judge.evidence_for(job_ids)
    done_rows = connection.execute("""
        SELECT job_id, content_hash FROM job_llm_verdicts
        WHERE rubric_version = ? AND (source = 'gemini' OR model LIKE '%gemini%')
    """, (config.triage.rubric_version,)).fetchall()
    done_map = {int(row["job_id"]): row["content_hash"] for row in done_rows}

    unjudged_list = []
    for jid in job_ids:
        if jid not in evidence:
            continue
        cur_hash = evidence[jid].content_hash
        if done_map.get(jid) == cur_hash:
            continue
        unjudged_list.append(jid)
    return unjudged_list


def unjudged(connection: sqlite3.Connection, config: Config, job_ids: list[int], judge: TriageJudge) -> list[int]:
    return unjudged_gemini(connection, config, job_ids, judge)


def _parse_epoch(ts: str) -> float:
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def gemini_queue_jobs(connection: sqlite3.Connection, config: Config, judge: TriageJudge | None = None) -> list[int]:
    """Queue of visible Relevant jobs needing a Gemini verdict.

    Order:
      1. Jobs flagged by the local LLM with verified relevant evidence
         (senior, years >= 2, not India, or blocker) first.
      2. Then by current score DESC.
      3. Then newest (first_seen_at DESC).
    """
    mode = getattr(config.triage, "primary_judge", "gemini")
    if mode == "local_llm":
        return []

    protected = protected_ids(connection, config)

    sql = """
    SELECT j.id, j.first_seen_at,
           COALESCE(r.final_score, r.local_rank_score * 100, 0) AS score
    FROM jobs j
    JOIN job_relevance r ON r.job_id = j.id
    WHERE j.status = 'open' AND r.gate_status IN ('target', 'maybe')
      AND COALESCE(r.location_fit, 'unknown') IN ('india', 'remote_open_to_india', 'remote_unclear', 'unknown')
      AND COALESCE(j.description_text, '') != ''
      AND NOT EXISTS (SELECT 1 FROM job_auto_hidden h WHERE h.job_id = j.id AND h.restored_at IS NULL)
    """
    candidates = []
    for row in connection.execute(sql):
        jid = int(row["id"])
        if jid not in protected:
            candidates.append((jid, float(row["score"] or 0), str(row["first_seen_at"] or "")))

    if not candidates:
        return []

    cand_ids = [c[0] for c in candidates]
    if judge is not None:
        todo_set = set(unjudged_gemini(connection, config, cand_ids, judge))
    else:
        rubric = config.triage.rubric_version
        done = {
            int(r[0])
            for r in connection.execute(
                "SELECT DISTINCT job_id FROM job_llm_verdicts WHERE rubric_version = ? AND (source = 'gemini' OR model LIKE '%gemini%')",
                (rubric,),
            ).fetchall()
        }
        todo_set = set(cand_ids) - done
    todo_candidates = [c for c in candidates if c[0] in todo_set]

    # Check local LLM flags for each candidate
    local_flagged_set = set()
    import json
    rows = connection.execute("""
        SELECT v.job_id, v.verified_json
        FROM job_llm_verdicts v
        WHERE (v.source != 'gemini' AND v.model NOT LIKE '%gemini%')
          AND v.rubric_version = ?
    """, (config.triage.rubric_version,)).fetchall()

    for r in rows:
        jid = int(r["job_id"])
        if jid in todo_set:
            try:
                vdata = json.loads(r["verified_json"])
                irr = set(vdata.get("irrelevant_evidence") or [])
                lvl = vdata.get("level")
                y = vdata.get("required_years_min")
                ind = vdata.get("india_eligible")
                blk = vdata.get("eligibility_blockers")

                flagged = False
                if lvl in ("senior", "lead_or_manager") and "level" not in irr:
                    flagged = True
                elif y is not None and y >= 2 and "required_years_min" not in irr:
                    flagged = True
                elif ind == "no" and "india_eligible" not in irr:
                    flagged = True
                elif blk and "eligibility_blockers" not in irr:
                    flagged = True

                if flagged:
                    local_flagged_set.add(jid)
            except Exception:
                pass

    if mode == "gemini_if_local_flags":
        todo_candidates = [c for c in todo_candidates if c[0] in local_flagged_set]

    todo_candidates.sort(
        key=lambda c: (0 if c[0] in local_flagged_set else 1, -c[1], -_parse_epoch(c[2]))
    )
    return [c[0] for c in todo_candidates]


@dataclass
class DailyResult:
    run: TriageRun
    skipped: str = ""           # why nothing ran (recorded in the scheduled run's detail)

    def line(self) -> str:
        if self.skipped:
            return f"triage: skipped ({self.skipped})"
        run = self.run
        stop = f"; stopped: {run.stopped}" if run.stopped else ""
        return (f"triage: judged={run.judged} cached={run.cached} failed={run.failed} calls={run.calls} "
                f"est_cost=${run.estimated_usd:.4f}{stop}")


def run_daily(connection: sqlite3.Connection, config: Config, client: GeminiClient | None, ledger: UsageLedger,
              guard: BudgetGuard, *, clock: Clock = utc_now) -> DailyResult:
    """Judge the top-ranked un-judged jobs of the shortlist within `budget.triage_daily_calls`; stay silent when blocked."""
    settings = config.triage
    if not settings.enabled:
        return DailyResult(TriageRun(), "triage.enabled is false")
    mode = getattr(settings, "primary_judge", "gemini")
    if mode == "local_llm":
        return DailyResult(TriageRun(), "primary_judge is local_llm (no Gemini calls)")
    if client is None:
        return DailyResult(TriageRun(), "no Gemini key")
    used = ledger.summary().triage_calls_today
    allowed = config.budget.triage_daily_calls - used
    if allowed <= 0:
        return DailyResult(TriageRun(), f"daily triage limit reached ({used}/{config.budget.triage_daily_calls})")
    judge = TriageJudge(connection, config, client, ledger, guard, clock=clock)
    queue = gemini_queue_jobs(connection, config, judge)
    if not queue:
        return DailyResult(TriageRun(), "nothing new to judge")
    run = judge.judge(queue[: settings.shortlist_size], max_calls=allowed, max_usd=config.budget.monthly_usd)
    return DailyResult(run)


def run_backfill(connection: sqlite3.Connection, config: Config, client: GeminiClient, ledger: UsageLedger,
                 guard: BudgetGuard, *, max_calls: int, max_usd: float, clock: Clock = utc_now) -> TriageRun:
    """Judge un-judged Relevant jobs, prioritized by local LLM flags then score, within run budget."""
    spent = ledger.summary().usd_month
    cap = config.budget.monthly_usd
    remaining = cap - spent
    if remaining < 1.00:
        run = TriageRun()
        run.stopped = f"skipped: only ${remaining:.3f} of ${cap:.2f} monthly budget remains (< $1.00 minimum required)"
        return run

    judge = TriageJudge(connection, config, client, ledger, guard, clock=clock)
    queue = gemini_queue_jobs(connection, config, judge)
    return judge.judge(queue, max_calls=max_calls, max_usd=min(max_usd, remaining), enforce_daily=False)

