"""Gemini triage and scoring of the shortlist, under a budget, plus the final-score blend.

Flow of one `score` run (everything before the first network call is offline and deterministic):

    1. classify   borderline `maybe` jobs (cheap model)            -> relevant_role / role_family
    2. shortlist  top K target (and classified-relevant maybe) jobs by similarity, recently seen,
                  with a description (jobs without one are never sent)
    3. remote     ambiguous "remote" jobs *inside the shortlist* get a yes/no/unclear on India
    4. score      the shortlist with the strong model             -> fit_score + explanation
    5. blend      final_score for every embedded target/maybe job (stored with its components)

Each job is its own unit of work: a bad answer is retried once, then recorded as `failed` for that
job only. Every paid call is checked against the budget first and written to `llm_usage` after.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from jobhunt.config import Config
from jobhunt.matching.budget import BudgetGuard, UsageLedger, estimate_cost, estimate_tokens
from jobhunt.matching.eligibility import required_flags
from jobhunt.matching.gemini import GeminiAuthError, GeminiClient, GeminiError, GeminiQuotaError
from jobhunt.matching.compact import (
    COMPACT_FIT_SCHEMA, COMPACT_PROMPT_VERSION, COMPACT_SCORE_SYSTEM, CompactFit, compact_description, load_cards,
)
from jobhunt.matching.prompts import (
    CLASSIFY_PROMPT_VERSION,
    CLASSIFY_SCHEMA,
    CLASSIFY_SYSTEM,
    FIT_SCHEMA,
    SCORE_PROMPT_VERSION,
    SCORE_SYSTEM,
    WORKPLACE_SCHEMA,
    WORKPLACE_SYSTEM,
    Classification,
    FitAssessment,
    InvalidAnswer,
    JobForPrompt,
    WorkplaceAnswer,
    build_classify_user_text,
    build_score_user_text,
    parse_answer,
)
from jobhunt.timeutil import Clock, to_iso, utc_now

logger = logging.getLogger(__name__)

_ESTIMATED_OUTPUT_TOKENS = {"score": 260, "classify": 70, "remote": 70, "workplace": 60}
_RETRY_NOTE = (
    "\n\nYour previous answer was not valid for the required JSON schema ({problem}). "
    "Return only the JSON object, with every required field, and nothing else."
)


# --- plan -------------------------------------------------------------------------------------


@dataclass
class PlannedCall:
    kind: str                       # classify | remote | workplace | score
    job_id: int
    title: str
    company: str
    model: str
    system: str
    user: str
    input_tokens: int
    estimated_output_tokens: int
    estimated_usd: float
    injection_suspected: bool
    input_hash: str
    description_hash: str | None = None
    profile_hash: str | None = None
    prompt_version: str = SCORE_PROMPT_VERSION
    price_factor: float = 1.0           # 0.5 for Batch API results


@dataclass
class ScoreRunStats:
    dry_run: bool = False
    planned: list[PlannedCall] = field(default_factory=list)
    classified: int = 0
    remote_resolved: int = 0
    workplace_resolved: int = 0
    scored: int = 0
    failed: int = 0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_usd: float = 0.0
    injection_flagged: int = 0
    stopped_reason: str | None = None
    final_scores: int = 0
    errors: list[str] = field(default_factory=list)

    def planned_totals(self) -> dict[str, tuple[int, int, float]]:
        totals: dict[str, tuple[int, int, float]] = {}
        for call in self.planned:
            count, tokens, usd = totals.get(call.kind, (0, 0, 0.0))
            totals[call.kind] = (count + 1, tokens + call.input_tokens, usd + call.estimated_usd)
        return totals


@dataclass(frozen=True)
class _JobRow:
    job_id: int
    title: str
    company: str
    location: str | None
    workplace: str
    remote_eligible: int | None
    remote_source: str | None
    description: str | None
    description_hash: str | None
    gate_status: str
    llm_status: str | None
    llm_input_hash: str | None
    llm_flags: dict[str, Any]
    similarity: float


_SELECT = """
    SELECT j.id, j.title, j.company_name, j.location_raw, j.workplace_type, j.remote_eligible_india,
           j.remote_source, j.description_text, j.description_hash, r.gate_status, r.llm_status, r.llm_input_hash,
           r.llm_flags, MAX(r.similarity_ai, r.similarity_fullstack) AS best_sim, r.rank_adjust
    FROM job_relevance r JOIN jobs j ON j.id = r.job_id
"""


def _row(raw: sqlite3.Row) -> _JobRow:
    try:
        flags = json.loads(raw["llm_flags"]) if raw["llm_flags"] else {}
    except ValueError:
        flags = {}
    return _JobRow(
        job_id=int(raw["id"]), title=raw["title"], company=raw["company_name"], location=raw["location_raw"],
        workplace=raw["workplace_type"], remote_eligible=raw["remote_eligible_india"], remote_source=raw["remote_source"],
        description=raw["description_text"], description_hash=raw["description_hash"], gate_status=raw["gate_status"],
        llm_status=raw["llm_status"], llm_input_hash=raw["llm_input_hash"], llm_flags=flags if isinstance(flags, dict) else {},
        similarity=float(raw["best_sim"] or 0.0) + float(raw["rank_adjust"] or 0.0) * 0.1,
    )


def _input_hash(kind: str, job: _JobRow, model: str, profile_hash: str) -> str:
    version = SCORE_PROMPT_VERSION if kind == "score" else CLASSIFY_PROMPT_VERSION
    material = "\x1f".join([kind, version, model, job.title, job.company, job.location or "", job.workplace,
                            job.description_hash or "", profile_hash if kind == "score" else ""])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _remote_label(job: _JobRow) -> str:
    if job.workplace != "remote":
        return "n/a"
    return {1: "yes", 0: "no"}.get(job.remote_eligible, "unclear")


def _prompt_job(job: _JobRow) -> JobForPrompt:
    return JobForPrompt(job.title, job.company, job.location, job.workplace, _remote_label(job), job.description)


def load_profiles(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = connection.execute("SELECT base_id AS name, stripped_text FROM profiles_v2").fetchall()
        if rows:
            return {row["name"]: row["stripped_text"] for row in rows}
    except sqlite3.OperationalError:
        pass
    return {row["name"]: row["stripped_text"] for row in connection.execute("SELECT name, stripped_text FROM profiles")}


def _profile_hash(profiles: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(profiles, sort_keys=True).encode("utf-8")).hexdigest()[:16]


_HAS_TEXT = "TRIM(COALESCE(j.description_text, '')) != ''"
# stale or closed jobs moved to Auto-hidden never enter a pool: no scoring call is spent on them
_NOT_HIDDEN = "NOT EXISTS (SELECT 1 FROM job_auto_hidden h WHERE h.job_id = j.id AND h.restored_at IS NULL)"


def _classification_candidates(
    connection: sqlite3.Connection, config: Config, today: date, model: str, profile_hash: str
) -> list[_JobRow]:
    """`maybe` jobs with a description that have not been triaged yet (or whose inputs changed)."""
    rows = connection.execute(
        f"""{_SELECT}
            WHERE j.status = 'open' AND r.gate_status = 'maybe' AND r.similarity_ai IS NOT NULL AND {_HAS_TEXT}
              AND {_NOT_HIDDEN}
            ORDER BY best_sim + r.rank_adjust * 0.1 DESC LIMIT ?""",
        (config.scoring.classify_limit * 8,),
    ).fetchall()
    chosen: list[_JobRow] = []
    for raw in rows:
        job = _row(raw)
        wanted = {_input_hash("classify", job, model, profile_hash), _input_hash("score", job, model, profile_hash)}
        if job.llm_status in {"classified", "scored"} and job.llm_input_hash in wanted:
            continue
        if job.llm_status == "failed" and job.llm_flags.get("failed_on") == today.isoformat():
            continue                                      # at most one retry per day
        chosen.append(job)
        if len(chosen) >= config.scoring.classify_limit:
            break
    return chosen


def _scoring_pool(connection: sqlite3.Connection, config: Config, today: date) -> list[_JobRow]:
    cutoff = to_iso_date(today - timedelta(days=config.scoring.recent_days))
    rows = connection.execute(
        f"""{_SELECT}
            WHERE j.status = 'open' AND r.similarity_ai IS NOT NULL AND {_HAS_TEXT} AND {_NOT_HIDDEN}
              AND substr(j.first_seen_at, 1, 10) >= ?
              AND COALESCE(r.location_fit, 'unknown') IN ('india', 'remote_open_to_india', 'remote_unclear', 'unknown')
              AND (r.gate_status = 'target'
                   OR (r.gate_status = 'maybe'
                       AND json_extract(r.llm_flags, '$.classification.relevant_role') = 1))
            ORDER BY (r.local_rank_score IS NULL) ASC, r.local_rank_score DESC,
                     best_sim + r.rank_adjust * 0.1 DESC""",
        (cutoff,),
    ).fetchall()
    return [_row(raw) for raw in rows]


def _workplace_candidates(connection: sqlite3.Connection, config: Config) -> list[_JobRow]:
    """Best-ranked open target/maybe jobs whose workplace is still unknown and not yet asked."""
    rows = connection.execute(
        f"""{_SELECT}
            WHERE j.status = 'open' AND r.gate_status IN ('target', 'maybe') AND j.workplace_type = 'unknown'
              AND {_HAS_TEXT} AND {_NOT_HIDDEN} AND r.similarity_ai IS NOT NULL
              AND COALESCE(j.remote_source, '') != 'llm'
              AND json_extract(r.llm_flags, '$.workplace_check') IS NULL
            ORDER BY COALESCE(r.final_score, 0) DESC, best_sim DESC LIMIT ?""",
        (config.scoring.workplace_limit,),
    ).fetchall()
    return [_row(raw) for raw in rows]


def to_iso_date(value: date) -> str:
    return value.isoformat()


def plan_calls(
    jobs: list[_JobRow], kind: str, model: str, profiles: dict[str, str], config: Config, today: date
) -> list[PlannedCall]:
    profile_hash = _profile_hash(profiles)
    plan: list[PlannedCall] = []
    for job in jobs:
        prompt_version = SCORE_PROMPT_VERSION
        if kind == "score" and config.scoring.prompt_style == "compact":
            cards = load_cards(profiles, config)
            system = COMPACT_SCORE_SYSTEM + "\n\n" + "\n\n".join(
                f'<candidate_card name="{name}">\n{card}\n</candidate_card>' for name, card in sorted(cards.items()))
            prompt_job = _prompt_job(job)
            prompt_job = dataclasses.replace(prompt_job, description=compact_description(job.description),
                                             description_char_limit=2800)
            user, suspected = build_classify_user_text(prompt_job)
            prompt_version = COMPACT_PROMPT_VERSION
        elif kind == "score":
            user, suspected = build_score_user_text(profiles, _prompt_job(job))
            system = SCORE_SYSTEM
        elif kind == "workplace":
            user, suspected = build_classify_user_text(_prompt_job(job))
            system = WORKPLACE_SYSTEM
        else:
            user, suspected = build_classify_user_text(_prompt_job(job))
            system = CLASSIFY_SYSTEM
        tokens = estimate_tokens(system) + estimate_tokens(user)
        out = _ESTIMATED_OUTPUT_TOKENS[kind]
        plan.append(PlannedCall(
            kind=kind, job_id=job.job_id, title=job.title, company=job.company, model=model, system=system, user=user,
            input_tokens=tokens, estimated_output_tokens=out,
            estimated_usd=estimate_cost(config.pricing, model, tokens, out, today),
            injection_suspected=suspected, input_hash=_input_hash("score" if kind == "score" else "classify", job, model,
                                                                   profile_hash),
            description_hash=job.description_hash, profile_hash=profile_hash if kind == "score" else None,
            prompt_version=prompt_version,
        ))
    return plan


def _plan_run(connection: sqlite3.Connection, config: Config, today: date, k: int) -> tuple[list[PlannedCall], list[_JobRow]]:
    """Calls that would be made now, from the current database state (no network)."""
    profiles = load_profiles(connection)
    models = config.models
    profile_hash = _profile_hash(profiles)
    calls: list[PlannedCall] = []
    if config.llm.classify_enabled:
        calls += plan_calls(_classification_candidates(connection, config, today, models.classify, profile_hash),
                            "classify", models.classify, profiles, config, today)
    if config.llm.workplace_enabled:
        calls += plan_calls(_workplace_candidates(connection, config), "workplace", models.classify, profiles, config,
                            today)
    shortlist = _shortlist(connection, config, today, k, models.score, profile_hash)
    if config.llm.remote_enabled:
        remote = [job for job in shortlist if job.workplace == "remote" and job.remote_eligible is None
                  and job.remote_source != "llm"]
        calls += plan_calls(remote, "remote", models.classify, profiles, config, today)
    calls += plan_calls(shortlist, "score", models.score, profiles, config, today)
    return calls, shortlist


def pool_job_ids(connection: sqlite3.Connection, config: Config, today: date) -> list[int]:
    """Ids of the jobs that would be sent for scoring today (the shortlist), in order: used by the liveness step."""
    profiles = load_profiles(connection)
    if not profiles:
        return []
    return [job.job_id for job in _shortlist(connection, config, today, config.scoring.top_k, config.models.score,
                                             _profile_hash(profiles))]


def _score_is_stale(job: _JobRow, profile_hash: str) -> bool:
    """A stored Gemini score is reused until the posting text or the resumes change (never for a model or
    prompt change, never because the workplace label was refined). Legacy scores have no stamps: kept."""
    stamps = job.llm_flags.get("scored") if isinstance(job.llm_flags.get("scored"), dict) else {}
    desc_hash, stored_profile = stamps.get("description_hash"), stamps.get("profile_hash")
    if desc_hash is None or stored_profile is None:
        return False
    return desc_hash != (job.description_hash or "") or stored_profile != profile_hash


def _shortlist(connection: sqlite3.Connection, config: Config, today: date, k: int, model: str, profile_hash: str) -> list[_JobRow]:
    chosen: list[_JobRow] = []
    for job in _scoring_pool(connection, config, today):
        if job.llm_status == "scored" and not _score_is_stale(job, profile_hash):
            continue
        if job.llm_status == "failed" and job.llm_flags.get("failed_on") == today.isoformat():
            continue
        chosen.append(job)
        if len(chosen) >= k:
            break
    return chosen


# --- running ----------------------------------------------------------------------------------


def run_score(
    connection: sqlite3.Connection,
    config: Config,
    client: GeminiClient | None,
    ledger: UsageLedger,
    guard: BudgetGuard,
    *,
    k: int | None = None,
    dry_run: bool = False,
    clock: Clock = utc_now,
    workers: int = 4,
    workplace_only: bool = False,
) -> ScoreRunStats:
    stats = ScoreRunStats(dry_run=dry_run)
    today = clock().date()
    k = k if k is not None else config.scoring.top_k
    profiles = load_profiles(connection)
    if not profiles:
        stats.stopped_reason = "no profiles: run profile-build first"
        return stats

    if dry_run:
        calls, _ = _plan_run(connection, config, today, k)
        stats.planned = calls
        stats.calls = len(calls)
        stats.input_tokens = sum(call.input_tokens for call in calls)
        stats.estimated_usd = sum(call.estimated_usd for call in calls)
        stats.injection_flagged = sum(1 for call in calls if call.injection_suspected)
        return stats

    if client is None:
        raise GeminiAuthError("no Gemini client (GEMINI_API_KEY missing)")
    models = config.models
    profile_hash = _profile_hash(profiles)

    # 1. classify borderline maybes
    if config.llm.classify_enabled:
        classify_jobs = _classification_candidates(connection, config, today, models.classify, profile_hash)
        _run_wave(connection, config, client, ledger, guard, stats, clock,
                  plan_calls(classify_jobs, "classify", models.classify, profiles, config, today), workers)

    # 1b. top-ranked jobs whose workplace the parser could not tell: one cheap read of the posting
    if not stats.stopped_reason and config.llm.workplace_enabled:
        _run_wave(connection, config, client, ledger, guard, stats, clock,
                  plan_calls(_workplace_candidates(connection, config), "workplace", models.classify, profiles,
                             config, today), workers)

    # 2-3. shortlist, then settle ambiguous remote eligibility inside it
    if not stats.stopped_reason and not workplace_only:
        shortlist = _shortlist(connection, config, today, k, models.score, profile_hash)
        if config.llm.remote_enabled:
            remote_jobs = [job for job in shortlist if job.workplace == "remote" and job.remote_eligible is None
                           and job.remote_source != "llm"]
            _run_wave(connection, config, client, ledger, guard, stats, clock,
                      plan_calls(remote_jobs, "remote", models.classify, profiles, config, today), workers)

        # 4. score (re-read the rows: remote answers change the prompt)
        if not stats.stopped_reason:
            fresh = {job.job_id: job for job in _scoring_pool(connection, config, today)}
            to_score = [fresh[job.job_id] for job in shortlist if job.job_id in fresh]
            _run_wave(connection, config, client, ledger, guard, stats, clock,
                      plan_calls(to_score, "score", models.score, profiles, config, today), workers)

    stats.final_scores = compute_final_scores(connection, config, clock=clock)
    return stats


def _run_wave(
    connection: sqlite3.Connection, config: Config, client: GeminiClient, ledger: UsageLedger, guard: BudgetGuard,
    stats: ScoreRunStats, clock: Clock, calls: list[PlannedCall], workers: int,
) -> None:
    for start in range(0, len(calls), workers):
        if stats.stopped_reason:
            return
        wave = calls[start:start + workers]
        estimate = sum(call.estimated_usd for call in wave)
        decision = guard.check_generate(estimate)
        if not decision:
            stats.stopped_reason = decision.reason
            return
        remaining_calls = config.budget.daily_calls - ledger.summary().generate_calls_today   # pipeline counter
        if remaining_calls < len(wave):
            wave = wave[:max(0, remaining_calls)]
            if not wave:
                stats.stopped_reason = "daily call cap reached"
                return
        with ThreadPoolExecutor(max_workers=len(wave)) as pool:
            outcomes = list(pool.map(lambda call: _execute(client, config, call), wave))
        for call, outcome in zip(wave, outcomes, strict=True):
            _apply(connection, config, ledger, stats, clock, call, outcome)
            if isinstance(outcome.error, (GeminiQuotaError, GeminiAuthError)):
                stats.stopped_reason = f"{type(outcome.error).__name__}: {outcome.error}"


@dataclass
class _Outcome:
    parsed: Any = None
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    error: Exception | None = None


def _execute(client: GeminiClient, config: Config, call: PlannedCall) -> _Outcome:
    """One job: a call, one retry on an invalid answer, never an exception out."""
    models = config.models
    is_score = call.kind == "score"
    compact = is_score and call.prompt_version == COMPACT_PROMPT_VERSION
    schema = (COMPACT_FIT_SCHEMA if compact else FIT_SCHEMA) if is_score else (
        WORKPLACE_SCHEMA if call.kind == "workplace" else CLASSIFY_SCHEMA)
    model_class = (CompactFit if compact else FitAssessment) if is_score else (
        WorkplaceAnswer if call.kind == "workplace" else Classification)
    thinking = models.score_thinking if is_score else models.classify_thinking
    max_tokens = config.scoring.max_output_tokens_score if is_score else config.scoring.max_output_tokens_classify
    outcome = _Outcome()
    user = call.user
    for attempt in range(2):
        try:
            result = client.generate_json(
                call.model, system_instruction=call.system, user_text=user, response_schema=schema,
                thinking_level=thinking, max_output_tokens=max_tokens, temperature=0.0,
            )
        except GeminiError as error:
            outcome.error = error
            return outcome
        outcome.calls += 1
        outcome.input_tokens += result.input_tokens or call.input_tokens
        outcome.output_tokens += result.output_tokens
        try:
            outcome.parsed = parse_answer(result.text, model_class)
            outcome.error = None
            return outcome
        except InvalidAnswer as problem:
            outcome.error = problem
            user = call.user + _RETRY_NOTE.format(problem=str(problem)[:120])
    return outcome


def _apply(
    connection: sqlite3.Connection, config: Config, ledger: UsageLedger, stats: ScoreRunStats, clock: Clock,
    call: PlannedCall, outcome: _Outcome,
) -> None:
    now = clock()
    now_iso = to_iso(now)
    purpose = call.kind if call.kind in {"classify", "remote", "workplace"} else "score"
    if outcome.calls:
        stats.calls += outcome.calls
        stats.input_tokens += outcome.input_tokens
        stats.output_tokens += outcome.output_tokens
        stats.estimated_usd += ledger.record(
            call.model, purpose, input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens,
            calls=outcome.calls, items=1, note=f"job {call.job_id} {call.prompt_version}",
            price_factor=call.price_factor,
        )
    if call.injection_suspected:
        stats.injection_flagged += 1

    row = connection.execute("SELECT llm_flags FROM job_relevance WHERE job_id = ?", (call.job_id,)).fetchone()
    try:
        flags: dict[str, Any] = json.loads(row["llm_flags"]) if row and row["llm_flags"] else {}
    except ValueError:
        flags = {}

    if outcome.error is not None or outcome.parsed is None:
        stats.failed += 1
        message = f"{type(outcome.error).__name__}: {outcome.error}"[:200] if outcome.error else "no answer"
        stats.errors.append(f"job {call.job_id}: {message}")
        if call.kind == "workplace":
            flags["workplace_check_failed_on"] = now.date().isoformat()
            connection.execute("UPDATE job_relevance SET llm_flags = ? WHERE job_id = ?", (json.dumps(flags), call.job_id))
            return
        if call.kind == "remote":      # a failed remote tie-break must not block scoring or change the status
            flags["remote_check_failed_on"] = now.date().isoformat()
            connection.execute("UPDATE job_relevance SET llm_flags = ? WHERE job_id = ?", (json.dumps(flags), call.job_id))
            return
        flags["failed_on"] = now.date().isoformat()
        connection.execute(
            """UPDATE job_relevance SET llm_status = 'failed', llm_reason = ?, llm_flags = ?, scored_at = ?,
                      llm_input_hash = ? WHERE job_id = ? AND (llm_status IS NULL OR llm_status != 'scored')""",
            (message, json.dumps(flags), now_iso, call.input_hash, call.job_id),
        )
        return

    flags.pop("failed_on", None)
    if call.injection_suspected:
        flags["injection_suspected"] = True

    if call.kind == "score":
        assessment: FitAssessment | CompactFit = outcome.parsed
        score = assessment.fit_score
        capped_by: list[str] = []
        if call.injection_suspected and score > config.scoring.injection_score_cap:
            score = config.scoring.injection_score_cap
            capped_by.append("injection")
        if assessment.is_excluded_role and score > config.scoring.excluded_role_cap:
            score = config.scoring.excluded_role_cap
            capped_by.append("excluded_role")
        red_flags = list(assessment.red_flags)
        if call.injection_suspected:
            red_flags.append("posting contains instruction-like text (removed before scoring)")
        flags["scored"] = {
            "raw_fit_score": assessment.fit_score, "capped_by": capped_by, "skills": assessment.skills_score,
            "seniority": assessment.seniority_score, "domain": assessment.domain_score,
            "location": assessment.location_score, "matched_skills": assessment.matched_skills,
            "missing_skills": assessment.missing_skills, "red_flags": red_flags,
            "is_excluded_role": assessment.is_excluded_role, "recommended_profile": assessment.recommended_profile,
            "description_hash": call.description_hash or "", "profile_hash": call.profile_hash or "",
        }
        connection.execute(
            """UPDATE job_relevance SET llm_score = ?, llm_reason = ?, llm_flags = ?, llm_model = ?,
                      llm_prompt_version = ?, llm_status = 'scored', scored_at = ?, llm_input_hash = ?,
                      best_profile = ? WHERE job_id = ?""",
            (float(score), assessment.reason, json.dumps(flags), call.model, call.prompt_version, now_iso,
             call.input_hash, assessment.recommended_profile, call.job_id),
        )
        stats.scored += 1
        return

    if call.kind == "workplace":
        answer: WorkplaceAnswer = outcome.parsed
        flags["workplace_check"] = {"answer": answer.workplace_type, "remote_eligible_india": answer.remote_eligible_india,
                                    "reason": answer.reason, "asked_on": now.date().isoformat()}
        connection.execute("UPDATE job_relevance SET llm_flags = ? WHERE job_id = ?", (json.dumps(flags), call.job_id))
        if answer.workplace_type != "unclear":
            eligible = {"yes": 1, "no": 0}.get(answer.remote_eligible_india) if answer.workplace_type == "remote" else None
            connection.execute(
                """UPDATE jobs SET workplace_type = ?, remote_source = 'llm', remote_eligible_india = ?,
                          remote_evidence = substr(COALESCE(remote_evidence, '') || ' | LLM: ' || ?, 1, 500)
                   WHERE id = ? AND workplace_type = 'unknown'""",
                (answer.workplace_type, eligible, answer.reason, call.job_id),
            )
            stats.workplace_resolved += 1
        return

    classification: Classification = outcome.parsed
    if call.kind == "classify":
        flags["classification"] = {
            "relevant_role": classification.relevant_role, "role_family": classification.role_family,
            "reason": classification.reason,
        }
        connection.execute(
            """UPDATE job_relevance SET llm_status = CASE WHEN llm_status = 'scored' THEN llm_status ELSE 'classified' END,
                      llm_flags = ?, llm_reason = COALESCE(llm_reason, ?), llm_model = ?, llm_prompt_version = ?,
                      scored_at = ?, llm_input_hash = ? WHERE job_id = ?""",
            (json.dumps(flags), classification.reason, call.model, CLASSIFY_PROMPT_VERSION, now_iso, call.input_hash,
             call.job_id),
        )
        stats.classified += 1
        return

    # kind == "remote": only a definite yes / no is written back; "unclear" leaves the row as it was
    verdict = {"yes": 1, "no": 0}.get(classification.remote_eligible_india)
    flags["remote_check"] = {"answer": classification.remote_eligible_india, "reason": classification.reason}
    connection.execute("UPDATE job_relevance SET llm_flags = ? WHERE job_id = ?", (json.dumps(flags), call.job_id))
    if verdict is not None:
        connection.execute(
            """UPDATE jobs SET remote_eligible_india = ?, remote_source = 'llm',
                      remote_evidence = substr(COALESCE(remote_evidence, '') || ' | LLM: ' || ?, 1, 500)
               WHERE id = ?""",
            (verdict, classification.reason, call.job_id),
        )
        stats.remote_resolved += 1


# --- the blend --------------------------------------------------------------------------------


def _is_home(country: str | None, home_country: str) -> bool:
    if not country:
        return False
    c = country.strip().upper()
    h = home_country.strip().upper()
    if c == h:
        return True
    iso_map = {
        "IN": "INDIA", "US": "UNITED STATES", "GB": "UNITED KINGDOM", "CA": "CANADA",
        "DE": "GERMANY", "FR": "FRANCE", "AU": "AUSTRALIA",
    }
    return iso_map.get(c) == h or iso_map.get(h) == c or (c == "US" and h == "USA") or (c == "USA" and h == "US")


def location_score(
    country: str | None, workplace: str, remote_eligible: int | None, home_country: str, config: Config,
    fit: str | None = None, profile: Any | None = None,
) -> tuple[float, str]:
    """(0-100 score, human reason) for where a job is and who it is open to.

    With a stored `location_fit` the policy verdict decides; without one (jobs not yet evaluated) the older
    country / workplace logic applies."""
    if profile is not None:
        home_country = profile.location_policy.home_country
    scores = config.scoring.location
    if fit == "india":
        return scores.home_country_onsite, "located in India"
    if fit == "remote_open_to_india":
        return scores.remote_open_to_home, "remote and open to India"
    if fit == "remote_unclear":
        return scores.remote_unclear, "remote, eligibility not stated"
    if fit == "wrong_location":
        return scores.remote_restricted_elsewhere, "wrong location for my policy"
    if fit == "abroad_unknown_workplace":
        return scores.abroad, "abroad"
    if fit == "unknown":
        return scores.unknown, "location unknown"
    if workplace == "remote":
        if remote_eligible == 1:
            return scores.remote_open_to_home, "remote and open to India"
        if remote_eligible == 0:
            return scores.remote_restricted_elsewhere, "remote but restricted to other countries"
        if _is_home(country, home_country):
            return scores.home_country_onsite, "remote, based in India"
        return scores.remote_unclear, "remote, eligibility not stated"
    if _is_home(country, home_country):
        return scores.home_country_onsite, "located in India"
    return scores.abroad, "abroad"


def _loads_list(value: Any) -> list[dict[str, Any]]:
    try:
        data = json.loads(value) if value else []
    except ValueError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _learned_penalty(value: Any) -> float:
    return min(20.0, sum(float(item.get("penalty") or 0.0) for item in _loads_list(value)))


def compute_final_scores(connection: sqlite3.Connection, config: Config, *, clock: Clock = utc_now,
                         profile: Any | None = None) -> int:
    """Blend and store `final_score` + its components for every embedded open target/maybe job."""
    from jobhunt.profile import get_profile

    p = profile or get_profile(config)
    settings = config.scoring
    embed_provider = getattr(config.embedding, "provider", "ollama")
    is_no_embeddings = (embed_provider == "none")

    home = p.location_policy.home_country if p else config.user.home_country
    where_sim = "" if is_no_embeddings else "AND r.similarity_ai IS NOT NULL"
    rows = connection.execute(
        f"""SELECT r.job_id, r.gate_status, r.similarity_ai, r.similarity_fullstack, r.similarity_percentile,
                  r.llm_score, r.llm_status, r.llm_flags, r.rank_adjust, r.required_years, r.seniority_level, r.local_rank_score, r.location_fit, r.eligibility_json, r.learned_json, j.country, j.workplace_type,
                  j.remote_eligible_india
           FROM job_relevance r JOIN jobs j ON j.id = r.job_id
           WHERE j.status = 'open' AND r.gate_status IN ('target', 'maybe') {where_sim}"""
    ).fetchall()
    span = max(1e-6, settings.similarity_ceiling - settings.similarity_floor)
    w_fit_raw = p.scoring.weight_fit if p else settings.weight_fit
    w_loc_raw = p.scoring.weight_location if p else settings.weight_location

    if is_no_embeddings:
        # Renormalize remaining weights (fit and location) so they sum to 1.0 when similarity is disabled
        w_pct_raw = 0.0
        w_sum = w_fit_raw + w_loc_raw
        w_sum = w_sum if w_sum > 0 else 1.0
        w_fit = w_fit_raw / w_sum
        w_pct = 0.0
        w_loc = w_loc_raw / w_sum
    else:
        w_pct_raw = p.scoring.weight_similarity if p else settings.weight_similarity_percentile
        weight_sum = w_fit_raw + w_pct_raw + w_loc_raw
        weight_sum = weight_sum if weight_sum > 0 else 1.0
        w_fit = w_fit_raw / weight_sum
        w_pct = w_pct_raw / weight_sum
        w_loc = w_loc_raw / weight_sum

    years_penalty_cap = p.level_policy.years_penalty_cap if p else settings.years_penalty_cap
    years_penalty_per_year = p.level_policy.years_penalty_per_year if p else settings.years_penalty_per_year
    junior_boost = p.level_policy.junior_boost if p else settings.junior_boost
    updates: list[tuple[Any, ...]] = []
    for row in rows:
        try:
            flags = json.loads(row["llm_flags"]) if row["llm_flags"] else {}
        except ValueError:
            flags = {}
        classification = flags.get("classification") if isinstance(flags, dict) else None

        if is_no_embeddings:
            similarity = 0.0
            scaled = 0.0
            percentile = 0.0
            if row["llm_score"] is not None and row["llm_status"] == "scored":
                fit, fit_source = float(row["llm_score"]), "llm"
            elif isinstance(classification, dict) and classification.get("relevant_role") is False:
                fit, fit_source = settings.irrelevant_fit, "classified_irrelevant"
            elif row["local_rank_score"] is not None:
                fit, fit_source = float(row["local_rank_score"]), "ranker"
            else:
                fit, fit_source = 50.0, "default_neutral"
        else:
            similarity = max(row["similarity_ai"] or 0.0, row["similarity_fullstack"] or 0.0)
            scaled = min(100.0, max(0.0, (similarity - settings.similarity_floor) / span * 100.0))
            percentile = float(row["similarity_percentile"] or 0.0) * 100.0
            if row["llm_score"] is not None and row["llm_status"] == "scored":
                fit, fit_source = float(row["llm_score"]), "llm"
            elif isinstance(classification, dict) and classification.get("relevant_role") is False:
                fit, fit_source = min(scaled, settings.irrelevant_fit), "classified_irrelevant"
            elif row["local_rank_score"] is not None:       # the local ranker's prediction beats raw similarity
                fit, fit_source = float(row["local_rank_score"]), "ranker"
            else:
                fit, fit_source = scaled, "similarity"

        loc_score, loc_reason = location_score(row["country"], row["workplace_type"], row["remote_eligible_india"],
                                               home, config, row["location_fit"], profile=p)
        family_points = float(row["rank_adjust"] or 0.0) * settings.rank_adjust_scale
        years = row["required_years"]
        years_points = -min(years_penalty_cap, years_penalty_per_year * years) if years else 0.0
        junior_points = junior_boost if row["seniority_level"] == "junior" else 0.0
        base = w_fit * fit + w_pct * percentile + w_loc * loc_score
        learned_penalty = _learned_penalty(row["learned_json"])
        final = max(0.0, min(100.0, base + family_points + years_points + junior_points - learned_penalty))
        capped_by: list[str] = []
        if required_flags(_loads_list(row["eligibility_json"])) and final > settings.eligibility_cap:
            final, capped_by = float(settings.eligibility_cap), ["eligibility"]     # PhD / Master's / citizenship required
        components = {
            "fit": round(fit, 2), "fit_source": fit_source, "similarity": round(similarity, 4),
            "similarity_scaled": round(scaled, 2), "similarity_percentile": round(percentile, 2),
            "weights": {"fit": round(w_fit, 3), "percentile": round(w_pct, 3), "location": round(w_loc, 3)},
            "location_score": loc_score, "location_reason": loc_reason, "family_points": round(family_points, 2),
            "years_points": round(years_points, 2), "junior_points": junior_points, "required_years": years, "base": round(base, 2),
            "learned_penalty": learned_penalty, "capped_by": capped_by, "final": round(final, 2),
        }
        if is_no_embeddings:
            components["similarity_disabled"] = True
        updates.append((round(final, 2), json.dumps(components), row["job_id"]))
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.executemany("UPDATE job_relevance SET final_score = ?, score_components = ? WHERE job_id = ?", updates)
        connection.execute(
            """UPDATE job_relevance SET final_score = NULL, score_components = NULL
               WHERE final_score IS NOT NULL AND (gate_status = 'excluded'
                     OR job_id IN (SELECT id FROM jobs WHERE status != 'open'))"""
        )
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
    return len(updates)


# --- Batch API ----------------------------------------------------------------------------------------
BATCH_PRICE_FACTOR = 0.5


@dataclass
class BatchStats:
    submitted: int = 0
    collected: int = 0
    failed: int = 0
    pending_batches: int = 0
    estimated_usd: float = 0.0
    name: str | None = None
    message: str = ""


def submit_score_batch(connection: sqlite3.Connection, config: Config, client: GeminiClient, ledger: UsageLedger,
                       guard: BudgetGuard, *, k: int | None = None, clock: Clock = utc_now) -> BatchStats:
    """Send tonight's shortlist to the Batch API (half price). Results are collected by `collect_score_batch`."""
    from jobhunt.matching.gemini import build_generate_body

    stats = BatchStats()
    today = clock().date()
    k = k if k is not None else config.scoring.top_k
    profiles = load_profiles(connection)
    if not profiles:
        stats.message = "no profiles: run profile-build first"
        return stats
    in_flight: set[int] = set()
    for row in connection.execute("SELECT jobs_json FROM llm_batches WHERE status = 'pending'"):
        in_flight |= {int(item["job_id"]) for item in json.loads(row["jobs_json"])}
    profile_hash = _profile_hash(profiles)
    models = config.models
    shortlist = [job for job in _shortlist(connection, config, today, k + len(in_flight), models.score, profile_hash)
                 if job.job_id not in in_flight][:k]
    calls = plan_calls(shortlist, "score", models.score, profiles, config, today)
    if not calls:
        stats.message = "nothing to score"
        return stats
    estimate = sum(call.estimated_usd for call in calls) * BATCH_PRICE_FACTOR
    decision = guard.check_generate(estimate)
    if not decision:
        stats.message = f"budget: {decision.reason}"
        return stats
    compact = config.scoring.prompt_style == "compact"
    schema = COMPACT_FIT_SCHEMA if compact else FIT_SCHEMA
    items = []
    for call in calls:
        body = build_generate_body(call.system, call.user, schema, config.models.score_thinking,
                                   config.scoring.max_output_tokens_score, 0.0)
        items.append((f"job-{call.job_id}", body))
    name = client.submit_batch(models.score, items, display_name=f"jobhunt-score-{today.isoformat()}")
    meta = [{"key": f"job-{c.job_id}", "job_id": c.job_id, "title": c.title, "company": c.company,
             "input_hash": c.input_hash, "description_hash": c.description_hash, "profile_hash": c.profile_hash,
             "prompt_version": c.prompt_version, "injection": c.injection_suspected, "estimated_usd": c.estimated_usd}
            for c in calls]
    connection.execute(
        "INSERT INTO llm_batches (name, model, created_at, status, jobs_json) VALUES (?, ?, ?, 'pending', ?)",
        (name, models.score, to_iso(clock()), json.dumps(meta)))
    connection.commit()
    stats.submitted, stats.name, stats.estimated_usd = len(calls), name, estimate
    stats.message = f"submitted {len(calls)} jobs as {name} (estimated ${estimate:.4f} at the batch price)"
    return stats


def _batch_state(payload: dict[str, Any]) -> str:
    raw = ((payload.get("metadata") or {}).get("state") or payload.get("state") or "").upper()
    if "SUCCEEDED" in raw:
        return "succeeded"
    if any(word in raw for word in ("FAILED", "CANCELLED", "EXPIRED")):
        return "failed"
    return "pending"


def _batch_responses(payload: dict[str, Any]) -> list[dict[str, Any]]:
    response = payload.get("response") or payload.get("dest") or {}
    inlined = response.get("inlinedResponses") if isinstance(response, dict) else None
    if isinstance(inlined, dict):                    # {"inlinedResponses": {"inlinedResponses": [...]}}
        inlined = inlined.get("inlinedResponses")
    return [item for item in inlined if isinstance(item, dict)] if isinstance(inlined, list) else []


def collect_score_batch(connection: sqlite3.Connection, config: Config, client: GeminiClient, ledger: UsageLedger,
                        *, clock: Clock = utc_now) -> BatchStats:
    from jobhunt.matching.gemini import parse_generate_response

    stats = BatchStats()
    score_stats = ScoreRunStats()
    compact_class = CompactFit if config.scoring.prompt_style == "compact" else FitAssessment
    for batch in connection.execute("SELECT * FROM llm_batches WHERE status = 'pending' ORDER BY id").fetchall():
        payload = client.get_batch(batch["name"])
        state = _batch_state(payload)
        if state == "pending" and not payload.get("done"):
            stats.pending_batches += 1
            continue
        if state == "failed":
            connection.execute("UPDATE llm_batches SET status = 'failed', applied_at = ?, detail = ? WHERE id = ?",
                               (to_iso(clock()), f"batch ended in state {state}", batch["id"]))
            connection.commit()
            stats.failed += 1
            continue
        meta = {item["key"]: item for item in json.loads(batch["jobs_json"])}
        for entry in _batch_responses(payload):
            key = ((entry.get("metadata") or {}).get("key")) or entry.get("key")
            info = meta.get(key)
            if info is None:
                continue
            call = PlannedCall(
                kind="score", job_id=int(info["job_id"]), title=info.get("title", ""), company=info.get("company", ""),
                model=batch["model"], system="", user="", input_tokens=0, estimated_output_tokens=0, estimated_usd=0.0,
                injection_suspected=bool(info.get("injection")), input_hash=info["input_hash"],
                description_hash=info.get("description_hash"), profile_hash=info.get("profile_hash"),
                prompt_version=info.get("prompt_version", SCORE_PROMPT_VERSION), price_factor=BATCH_PRICE_FACTOR,
            )
            outcome = _Outcome()
            try:
                if entry.get("error") or not isinstance(entry.get("response"), dict):
                    raise InvalidAnswer(str(entry.get("error"))[:120] or "no response")
                result = parse_generate_response(batch["model"], entry["response"])
                outcome.calls, outcome.input_tokens, outcome.output_tokens = 1, result.input_tokens, result.output_tokens
                outcome.parsed = parse_answer(result.text, compact_class)
            except (InvalidAnswer, GeminiError) as error:
                outcome.error = error
            _apply(connection, config, ledger, score_stats, clock, call, outcome)
        connection.execute("UPDATE llm_batches SET status = 'applied', applied_at = ? WHERE id = ?",
                           (to_iso(clock()), batch["id"]))
        connection.commit()
    stats.collected, stats.failed = stats.collected + score_stats.scored, stats.failed + score_stats.failed
    stats.estimated_usd = score_stats.estimated_usd
    if stats.collected:
        compute_final_scores(connection, config, clock=clock)
    stats.message = (f"collected {score_stats.scored} scores ({score_stats.failed} failed), "
                     f"{stats.pending_batches} batch(es) still running")
    return stats
