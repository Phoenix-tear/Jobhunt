"""Queue management and execution pipeline for local-LLM-first triage (Brief v10 Section 3).

Judges all open, visible, target/maybe jobs in score order with time box and GPU memory release.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from jobhunt.config import Config
from jobhunt.matching.gemini import GeminiClient
from jobhunt.matching.ollama import OllamaClient
from jobhunt.matching.triage import Evidence, Verdict, build_evidence
from jobhunt.matching.triage_apply_llm import apply_local_llm_triage
from jobhunt.matching.triage_local_judge import TriageLocalJudge
from jobhunt.timeutil import Clock, to_iso, utc_now

logger = logging.getLogger(__name__)


def get_visible_candidate_queue(connection: sqlite3.Connection) -> list[int]:
    """All open, target/maybe, location-visible, non-hidden jobs in score and recency order."""
    query = """
        SELECT j.id
        FROM jobs j
        JOIN job_relevance r ON r.job_id = j.id
        LEFT JOIN job_auto_hidden h ON h.job_id = j.id AND h.restored_at IS NULL
        WHERE j.status = 'open'
          AND r.gate_status IN ('target', 'maybe')
          AND COALESCE(r.location_fit, 'unknown') IN ('india', 'remote_open_to_india', 'remote_unclear', 'unknown')
          AND h.job_id IS NULL
        ORDER BY r.final_score DESC,
                 COALESCE((SELECT confidence FROM job_cascade_decisions WHERE job_id = j.id AND tier = 1 AND head = 'seniority' ORDER BY id DESC LIMIT 1), 0.0) DESC,
                 j.first_seen_at DESC, j.id ASC
    """
    return [int(r["id"]) for r in connection.execute(query).fetchall()]


def get_triage_coverage(connection: sqlite3.Connection, config: Config) -> dict[str, Any]:
    """Calculate judging coverage for visible Relevant jobs and all candidate jobs."""
    rubric = config.triage.rubric_version

    # Check columns of job_llm_verdicts
    cols = [r[1] for r in connection.execute("PRAGMA table_info(job_llm_verdicts)").fetchall()]
    has_source = "source" in cols
    source_expr = "v.source" if has_source else "CASE WHEN v.model LIKE '%gemini%' THEN 'gemini' ELSE 'local_llm' END"

    # Visible Relevant (open, target, location-visible, not auto-hidden)
    rel_rows = connection.execute(
        f"""
        SELECT j.id,
               COALESCE((
                   SELECT CASE
                       WHEN ({source_expr} = 'gemini' OR v.model LIKE '%gemini%') THEN 'gemini'
                       ELSE 'local_llm'
                   END
                   FROM job_llm_verdicts v
                   WHERE v.job_id = j.id AND v.rubric_version = ?
                   ORDER BY CASE WHEN ({source_expr} = 'gemini' OR v.model LIKE '%gemini%') THEN 1 ELSE 0 END DESC, v.id DESC
                   LIMIT 1
               ), 'none') AS verdict_source
        FROM jobs j
        JOIN job_relevance r ON r.job_id = j.id
        LEFT JOIN job_auto_hidden h ON h.job_id = j.id AND h.restored_at IS NULL
        WHERE j.status = 'open'
          AND r.gate_status = 'target'
          AND COALESCE(r.location_fit, 'unknown') IN ('india', 'remote_open_to_india', 'remote_unclear', 'unknown')
          AND h.job_id IS NULL
        """,
        (rubric,),
    ).fetchall()

    total_relevant = len(rel_rows)
    cov_gemini = sum(1 for r in rel_rows if r["verdict_source"] == "gemini")
    cov_local_only = sum(1 for r in rel_rows if r["verdict_source"] == "local_llm")
    cov_none = sum(1 for r in rel_rows if r["verdict_source"] == "none")
    judged_relevant = cov_gemini + cov_local_only
    rel_coverage = float(judged_relevant / max(total_relevant, 1) * 100.0)

    # All candidate jobs (open target/maybe, location-visible, not auto-hidden)
    all_queue = get_visible_candidate_queue(connection)
    total_candidates = len(all_queue)

    if total_candidates == 0:
        return {
            "total_relevant": total_relevant,
            "judged_relevant": judged_relevant,
            "relevant_coverage_pct": round(rel_coverage, 1),
            "coverage_gemini": cov_gemini,
            "coverage_local_only": cov_local_only,
            "coverage_none": cov_none,
            "total_candidates": 0,
            "judged_candidates": 0,
            "candidate_coverage_pct": 100.0,
            "unjudged_queue_len": 0,
        }

    placeholders = ",".join("?" for _ in all_queue)
    judged_set = {
        int(r[0])
        for r in connection.execute(
            f"SELECT DISTINCT job_id FROM job_llm_verdicts WHERE rubric_version = ? AND job_id IN ({placeholders})",
            [rubric, *all_queue],
        ).fetchall()
    }

    judged_cand = len(judged_set)
    cand_coverage = float(judged_cand / max(total_candidates, 1) * 100.0)
    unjudged_len = total_candidates - judged_cand

    return {
        "total_relevant": total_relevant,
        "judged_relevant": judged_relevant,
        "relevant_coverage_pct": round(rel_coverage, 1),
        "coverage_gemini": cov_gemini,
        "coverage_local_only": cov_local_only,
        "coverage_none": cov_none,
        "total_candidates": total_candidates,
        "judged_candidates": judged_cand,
        "candidate_coverage_pct": round(cand_coverage, 1),
        "unjudged_queue_len": unjudged_len,
    }


def run_triage_llm_pipeline(
    connection: sqlite3.Connection,
    config: Config,
    *,
    max_minutes: float | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    client: OllamaClient | None = None,
    gemini_client: GeminiClient | None = None,
    clock: Clock = utc_now,
) -> dict[str, Any]:
    """Execute local-LLM-first triage queue with time-boxing and reversible application."""
    max_mins = max_minutes if max_minutes is not None else float(config.triage.local_llm_max_minutes)
    judge = TriageLocalJudge(connection, config, client=client, clock=clock)

    if not judge.is_available():
        logger.warning("Local LLM model %s is not available on Ollama; skipping triage run cleanly", config.triage.local_llm_model)
        return {
            "status": "skipped",
            "reason": f"Ollama model {config.triage.local_llm_model} unavailable",
            "newly_judged": 0,
            "examined": 0,
            "coverage": get_triage_coverage(connection, config),
        }

    queue_ids = get_visible_candidate_queue(connection)
    if limit is not None:
        queue_ids = queue_ids[:limit]

    # Pre-fetch job metadata for queue
    placeholders = ",".join("?" for _ in queue_ids)
    job_rows = {
        int(r["id"]): r
        for r in connection.execute(
            f"""
            SELECT id, title, company_name, location_raw, workplace_type, remote_regions, description_text
            FROM jobs WHERE id IN ({placeholders})
            """,
            queue_ids,
        ).fetchall()
    } if queue_ids else {}

    # Pre-fetch existing verified verdicts
    existing_verdict_rows = connection.execute(
        f"""
        SELECT job_id, verified_json FROM job_llm_verdicts
        WHERE rubric_version = ? AND job_id IN ({placeholders})
        ORDER BY id ASC
        """,
        [config.triage.rubric_version, *queue_ids],
    ).fetchall() if queue_ids else []

    all_verdicts: dict[int, Verdict] = {}
    for r in existing_verdict_rows:
        try:
            all_verdicts[int(r["job_id"])] = Verdict.from_json(json.loads(r["verified_json"]))
        except Exception:
            pass

    start_time = time.monotonic()
    max_seconds = max_mins * 60.0
    now_iso = to_iso(clock())

    newly_judged = 0
    cached_count = 0
    failed_count = 0

    try:
        for jid in queue_ids:
            # Check time cap
            if (time.monotonic() - start_time) > max_seconds:
                logger.info("Local LLM triage reached run cap of %0.1f minutes", max_mins)
                break

            # If already verified in verdicts, reuse
            if jid in all_verdicts:
                cached_count += 1
                continue

            jdata = job_rows.get(jid)
            if not jdata:
                continue

            ev = build_evidence(
                title=jdata["title"] or "",
                company=jdata["company_name"] or "",
                location=jdata["location_raw"],
                workplace=jdata["workplace_type"],
                remote_regions=jdata["remote_regions"],
                description=jdata["description_text"],
            )

            res = judge.judge_one(jid, ev, now_iso=now_iso)
            if res.error:
                failed_count += 1
                logger.warning("Failed to judge job %d: %s", jid, res.error)
            else:
                all_verdicts[jid] = res.verdict
                if res.from_cache:
                    cached_count += 1
                else:
                    newly_judged += 1
    finally:
        # Release GPU memory sequentially so embedding and match steps do not fight for VRAM
        judge.unload_model()

    # Apply triage actions
    app_res = apply_local_llm_triage(
        connection,
        config,
        all_verdicts,
        dry_run=dry_run,
        gemini_client=gemini_client,
        clock=clock,
    )

    coverage_stats = get_triage_coverage(connection, config)
    duration_sec = round(time.monotonic() - start_time, 2)

    return {
        "status": "success",
        "dry_run": dry_run,
        "duration_seconds": duration_sec,
        "queue_total": len(queue_ids),
        "newly_judged": newly_judged,
        "cached": cached_count,
        "failed": failed_count,
        "application": {
            "excluded_seniority": app_res.excluded_seniority,
            "caution_seniority": app_res.caution_seniority,
            "hidden_location": app_res.hidden_location,
            "blockers_applied": app_res.blockers_applied,
            "advisory_only": app_res.advisory_only,
            "exempt_protected_count": app_res.exempt_protected_count,
            "high_stakes_gemini_called": app_res.high_stakes_gemini_called,
            "high_stakes_disagreements": app_res.high_stakes_disagreements,
            "blast_radius_triggered": app_res.blast_radius_triggered,
            "blast_radius_detail": app_res.blast_radius_detail,
        },
        "coverage": coverage_stats,
    }
