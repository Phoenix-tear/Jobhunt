"""`GET /api/health`: what has run, what is stale, and what needs attention. Local checks only."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from fastapi import APIRouter

from jobhunt.alerts.gmail import READONLY_SCOPE
from jobhunt.api.database import ConfigDep, ReadDb
from jobhunt.api.schemas import (
    BudgetForecast, CostProjection, FacetCount, FreshnessHealth, LocalStage, GmailHealth, Health, HealthWarning, LlmUsage,
    MatchingHealth, RunSummaryOut, ScheduleHealth, SourceHealth, SourceReportOut, TriageHealth,
)
from jobhunt.config import Config
from jobhunt.matching.budget import _INTERACTIVE_SQL, _NON_PIPELINE_SQL
from jobhunt.schedule.runner import REAUTH_COMMAND
from jobhunt.sources import ATS_SOURCE_NAMES
from jobhunt.timeutil import parse_iso, to_iso, utc_now

router = APIRouter(prefix="/api", tags=["health"])

STALE_AFTER_HOURS = 72.0
GMAIL_SOURCE = "gmail"


def _kind(source: str) -> str:
    if source in ATS_SOURCE_NAMES:
        return "ats"
    if source == GMAIL_SOURCE or source.startswith("email_"):
        return "email"
    return "feed"


def gmail_health(config: Config) -> GmailHealth:
    """Is a read-only token in place? Inspects the local files only; never calls Google."""
    credentials_file = config.resolve_path(config.email.credentials_file)
    token_file = config.resolve_path(config.email.token_file)
    scopes: list[str] = []
    if token_file.is_file():
        try:
            data = json.loads(token_file.read_text(encoding="utf-8"))
            raw_scopes = data.get("scopes") if isinstance(data, dict) else None
            scopes = [str(item) for item in raw_scopes] if isinstance(raw_scopes, list) else []
        except (OSError, ValueError):
            scopes = []
    return GmailHealth(
        credentials_file_exists=credentials_file.is_file(),
        token_file_exists=token_file.is_file(),
        scopes=scopes,
        read_only_scope=scopes == [READONLY_SCOPE],
        account_configured=config.email.account or None,
    )


def _latest_runs(connection: sqlite3.Connection) -> dict[str, RunSummaryOut]:
    rows = connection.execute(
        """SELECT r.* FROM ingestion_runs r
           JOIN (SELECT source, MAX(id) AS id FROM ingestion_runs GROUP BY source) latest ON latest.id = r.id"""
    ).fetchall()
    runs: dict[str, RunSummaryOut] = {}
    for row in rows:
        errors: list[dict[str, str]] = []
        if row["errors_json"]:
            try:
                parsed = json.loads(row["errors_json"])
                errors = [item for item in parsed if isinstance(item, dict) and item.get("kind") not in {"warning", "skipped"}]
            except ValueError:
                errors = []
        first = errors[0] if errors else None
        runs[row["source"]] = RunSummaryOut(
            started_at=row["started_at"], finished_at=row["finished_at"],
            companies_checked=row["companies_checked"], jobs_seen=row["jobs_seen"], jobs_new=row["jobs_new"],
            jobs_closed=row["jobs_closed"], jobs_skipped_by_prefilter=row["jobs_skipped_by_prefilter"],
            error_count=len(errors),
            first_error=f"{first.get('company', '?')}: {first.get('kind', 'error')}: {first.get('message', '')}"[:300] if first else None,
        )
    return runs


def _matching_health(connection: sqlite3.Connection, config: Config, now: datetime) -> MatchingHealth | None:
    """LLM spend vs budget, embedding coverage and last run times; None when matching was never set up."""
    try:
        today = now.date().isoformat()
        month = today[:7]
        usage = connection.execute(
            f"""SELECT COALESCE(SUM(CASE WHEN day = ? AND purpose != 'embed' AND purpose NOT IN ({_NON_PIPELINE_SQL})
                                    THEN calls END), 0),
                      COALESCE(SUM(CASE WHEN day = ? THEN estimated_usd END), 0),
                      COALESCE(SUM(CASE WHEN day LIKE ? THEN estimated_usd END), 0),
                      COALESCE(SUM(CASE WHEN day = ? AND purpose = 'embed' THEN items END), 0),
                      MAX(created_at),
                      COALESCE(SUM(CASE WHEN day = ? AND purpose IN ({_INTERACTIVE_SQL}) THEN calls END), 0)
               FROM llm_usage""",
            (today, today, f"{month}%", today, today),
        ).fetchone()
        embeddable = int(connection.execute(
            """SELECT COUNT(*) FROM jobs j JOIN job_relevance r ON r.job_id = j.id
               WHERE j.status = 'open' AND r.gate_status IN ('target', 'maybe')""").fetchone()[0])
        embedded = int(connection.execute(
            """SELECT COUNT(DISTINCT e.job_id) FROM job_embeddings e JOIN jobs j ON j.id = e.job_id
               JOIN job_relevance r ON r.job_id = j.id
               WHERE j.status = 'open' AND r.gate_status IN ('target', 'maybe')""").fetchone()[0])
        scored = int(connection.execute(
            "SELECT COUNT(*) FROM job_relevance WHERE llm_status = 'scored'").fetchone()[0])
        last_relevance = connection.execute("SELECT MAX(computed_at) FROM job_relevance").fetchone()[0]
        profiles = [row[0] for row in connection.execute("SELECT name FROM profiles ORDER BY name")]
    except sqlite3.OperationalError:       # matching tables not present yet
        return None
    from jobhunt.matching.budget import compute_budget_forecast, project_monthly

    stages: list[LocalStage] = []
    try:
        for stage in ("embed", "ranker", "local_llm"):
            latest = connection.execute(
                "SELECT * FROM local_runs WHERE stage = ? ORDER BY id DESC LIMIT 1", (stage,)).fetchone()
            stages.append(LocalStage(
                stage=stage, last_run_at=latest["started_at"] if latest else None,
                seconds=latest["seconds"] if latest else None, items=latest["items"] if latest else None,
                model=latest["model"] if latest else None, status=latest["status"] if latest else None,
                detail=latest["detail"] if latest else None))
    except sqlite3.OperationalError:
        stages = []
    projection = project_monthly(connection, config, now.date())
    forecast = compute_budget_forecast(connection, config, now)
    return MatchingHealth(
        local_stages=stages, projection=CostProjection(**projection.__dict__),
        budget_forecast=BudgetForecast(**forecast.__dict__),
        usage=LlmUsage(
            calls_today=int(usage[0]), daily_call_cap=config.budget.daily_calls, usd_today=round(float(usage[1]), 4),
            interactive_calls_today=int(usage[5]), interactive_call_cap=config.budget.interactive_daily_calls,
            triage_calls_today=triage_calls_today(connection, now), triage_call_cap=config.budget.triage_daily_calls,
            usd_month=round(float(usage[2]), 4), monthly_usd_cap=config.budget.monthly_usd,
            embedded_texts_today=int(usage[3]), daily_embedding_cap=config.budget.daily_embedding_texts,
        ),
        embedding_coverage=round(embedded / embeddable, 4) if embeddable else 0.0,
        embedded=embedded, embeddable=embeddable, scored_jobs=scored,
        last_relevance_run_at=last_relevance, last_llm_call_at=usage[4], profiles=profiles,
    )


def triage_calls_today(connection: sqlite3.Connection, now: datetime) -> int:
    row = connection.execute("SELECT COALESCE(SUM(calls), 0) FROM llm_usage WHERE day = ? AND purpose = 'triage'",
                             (now.date().isoformat(),)).fetchone()
    return int(row[0])


def _triage_block(connection: sqlite3.Connection, config: Config, now: datetime) -> TriageHealth | None:
    from jobhunt.matching.triage_stats import triage_health

    data = triage_health(connection, config, now)
    return TriageHealth(**data, calls_today=triage_calls_today(connection, now)) if data else None


def build_health(connection: sqlite3.Connection, config: Config, now: datetime) -> Health:
    state_rows = {row["source"]: row for row in connection.execute("SELECT * FROM source_state")}
    runs = _latest_runs(connection)
    configured = {name for name, settings in config.sources.items() if settings.enabled}
    configured.add(GMAIL_SOURCE)

    sources: list[SourceHealth] = []
    for name in sorted(configured | set(state_rows) | set(runs)):
        state = state_rows.get(name)
        last_success = state["last_success_at"] if state else None
        age_hours = (now - parse_iso(last_success)).total_seconds() / 3600 if last_success else None
        enabled = name in configured
        never_run = state is None or state["last_run_at"] is None
        sources.append(
            SourceHealth(
                source=name, kind=_kind(name), enabled=enabled,  # type: ignore[arg-type]
                last_run_at=state["last_run_at"] if state else None, last_success_at=last_success,
                age_hours=round(age_hours, 1) if age_hours is not None else None,
                stale=enabled and (age_hours is None or age_hours > STALE_AFTER_HOURS) and not never_run,
                never_run=never_run, fetch_count_today=int(state["fetch_count_today"]) if state else 0,
                last_run=runs.get(name),
            )
        )

    def scalar(sql: str, *params: object) -> int:
        return int(connection.execute(sql, params).fetchone()[0])

    companies = {row[0]: int(row[1]) for row in connection.execute("SELECT status, COUNT(*) FROM companies GROUP BY 1")}
    stages = {row[0]: int(row[1]) for row in connection.execute("SELECT stage, COUNT(*) FROM applications GROUP BY 1")}
    gmail = gmail_health(config)
    contact_set = bool(config.user_agent_contact.strip())

    warnings: list[HealthWarning] = []
    if not contact_set:
        warnings.append(HealthWarning(
            code="user_agent_contact_missing",
            message="user_agent_contact is empty in config.yaml: requests to job boards carry no way to reach you.",
        ))
    if not gmail.token_file_exists:
        warnings.append(HealthWarning(
            code="gmail_token_missing",
            message=f"No Gmail token found: run `{REAUTH_COMMAND}` (see SETUP.md) to ingest alert emails.",
        ))
    elif not gmail.read_only_scope:
        warnings.append(HealthWarning(
            code="gmail_scope_unexpected",
            message="The Gmail token does not carry exactly the read-only scope; delete it and run gmail-auth again.",
        ))
    for item in sources:
        if item.stale:
            warnings.append(HealthWarning(
                code="source_stale", message=f"{item.source} has not succeeded for {item.age_hours:.0f} h "
                                             f"(threshold {STALE_AFTER_HOURS:.0f} h).",
            ))
        elif item.enabled and item.never_run and item.source != GMAIL_SOURCE:
            warnings.append(HealthWarning(code="source_never_run", message=f"{item.source} is enabled but has never run."))
        if item.last_run and item.last_run.error_count and item.enabled and item.last_run.error_count >= 5:
            warnings.append(HealthWarning(
                code="source_errors", message=f"{item.source}: {item.last_run.error_count} errors in the last run "
                                              f"({item.last_run.first_error})",
            ))

    schedule = None
    try:
        from jobhunt.schedule.health import build_schedule_health

        schedule = ScheduleHealth(**build_schedule_health(connection, config, now))
    except sqlite3.OperationalError:       # migration 013 not applied yet
        schedule = None
    if schedule is not None:
        if schedule.gmail_auth_expired:
            warnings.append(HealthWarning(
                code="gmail_auth_expired",
                message=f"Gmail authorization expired: ingest-email is being skipped. Re-authorize with: "
                        f"{schedule.gmail_reauth_command}"))
        for task in schedule.tasks:
            if task.missed_warning:
                warnings.append(HealthWarning(
                    code="scheduled_task_missed",
                    message=f"{task.name} missed {task.missed_runs} runs (last success {task.last_ok_at or 'never'})."))

    matching = _matching_health(connection, config, now)
    budget_forecast = matching.budget_forecast if matching else None
    if budget_forecast and budget_forecast.warning:
        warnings.append(HealthWarning(
            code="budget_forecast_warning",
            message=f"Projected AI spend (${budget_forecast.projected_month_usd:.2f}) exceeds monthly cap (${budget_forecast.cap_usd:.2f}).",
        ))

    morning_check_path = config.resolve_path("data/morning_check.txt")
    last_morning_check = None
    if morning_check_path.is_file():
        try:
            last_morning_check = morning_check_path.read_text(encoding="utf-8")
        except OSError:
            last_morning_check = None

    return Health(
        freshness=_freshness_health(connection, config, now),
        triage=_triage_block(connection, config, now),
        schedule=schedule,
        matching=matching,
        budget_forecast=budget_forecast,
        last_morning_check=last_morning_check,
        readiness=_readiness_checklist(connection, config),
        generated_at=to_iso(now),
        jobs_open=scalar("SELECT COUNT(*) FROM jobs WHERE status = 'open'"),
        jobs_closed=scalar("SELECT COUNT(*) FROM jobs WHERE status = 'closed'"),
        companies_active=companies.get("active", 0), companies_unverified=companies.get("unverified", 0),
        companies_inactive=companies.get("inactive", 0), applications_by_stage=stages, sources=sources,
        gmail=gmail, user_agent_contact_set=contact_set,
        schema_version=scalar("SELECT COALESCE(MAX(version), 0) FROM schema_migrations"), warnings=warnings,
    )


def _readiness_checklist(connection: sqlite3.Connection, config: Config) -> ReadinessChecklist:
    from jobhunt.api.schemas import ReadinessCheckItem, ReadinessChecklist
    from jobhunt.settings.secrets import get_gemini_key
    from jobhunt.tailoring.facts import read_ticks

    # 1. key set
    key = get_gemini_key(config.project_root)
    key_set = bool(key)

    # 2. resume validated
    resume_dir = config.resolve_path(config.profiles.resume_dir)
    resumes_found = False
    for f in config.profiles.files.values():
        if (resume_dir / f).is_file():
            resumes_found = True
            break

    # 3. facts ticked
    facts_file = config.resolve_path(config.profiles.profile_dir) / "facts_tex.md"
    ticks = read_ticks(facts_file) if facts_file.is_file() else {}
    has_ticked_facts = any(ticks.values())

    # 4. profile complete
    profile_file = config.resolve_path(config.profiles.profile_dir) / "profile.yaml"
    profile_complete = profile_file.is_file()

    # 5. sources chosen
    sources_chosen = any(s.enabled for s in config.sources.values())

    # 6. first ingest done
    ingest_count = 0
    try:
        ingest_count = connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0]
    except sqlite3.OperationalError:
        pass
    first_ingest_done = ingest_count > 0

    # 7. first match done
    relevance_count = 0
    try:
        relevance_count = connection.execute("SELECT COUNT(*) FROM job_relevance").fetchone()[0]
    except sqlite3.OperationalError:
        pass
    first_match_done = relevance_count > 0

    items = [
        ReadinessCheckItem(
            id="key_set",
            label="Gemini API Key Configured",
            passed=key_set,
            link="/settings?tab=gemini",
            guidance="Add your Gemini API key in Settings -> Gemini to enable scoring and tailoring."
        ),
        ReadinessCheckItem(
            id="resume_validated",
            label="Base Resume Validated",
            passed=resumes_found,
            link="/settings?tab=resumes",
            guidance="Upload and validate your .tex base resume."
        ),
        ReadinessCheckItem(
            id="facts_ticked",
            label="Resume Facts Reviewed & Ticked",
            passed=has_ticked_facts,
            link="/settings?tab=facts",
            guidance="Review extracted facts and tick the ones allowed for tailoring."
        ),
        ReadinessCheckItem(
            id="profile_complete",
            label="Target Profile Configured",
            passed=profile_complete,
            link="/settings?tab=profile",
            guidance="Configure your target roles, degree stage, and location preferences."
        ),
        ReadinessCheckItem(
            id="sources_chosen",
            label="Job Sources Selected",
            passed=sources_chosen,
            link="/settings?tab=sources",
            guidance="Enable job feeds and company ATS boards to ingest."
        ),
        ReadinessCheckItem(
            id="first_ingest_done",
            label="First Ingest Completed",
            passed=first_ingest_done,
            link="/settings?tab=schedule",
            guidance="Run your first ingest to fetch open job listings."
        ),
        ReadinessCheckItem(
            id="first_match_done",
            label="Relevance Matching Evaluated",
            passed=first_match_done,
            link="/today",
            guidance="Run match to score jobs against your profile."
        ),
    ]

    all_passed = all(i.passed for i in items)
    return ReadinessChecklist(
        key_set=items[0],
        resume_validated=items[1],
        facts_ticked=items[2],
        profile_complete=items[3],
        sources_chosen=items[4],
        first_ingest_done=items[5],
        first_match_done=items[6],
        ready=all_passed,
    )


def _freshness_health(connection: sqlite3.Connection, config: Config, now: datetime) -> FreshnessHealth | None:
    """Auto-hidden counts per source, age bands, and the per-source closed / too-old report (migration 017 needed)."""
    from dataclasses import asdict

    from jobhunt import freshness
    from jobhunt.source_reports import build_source_reports

    try:
        hidden = freshness.hidden_by_source(connection)
        by_reason = [FacetCount(value=str(row[0]), count=int(row[1])) for row in connection.execute(
            "SELECT reason, COUNT(*) FROM job_auto_hidden WHERE restored_at IS NULL GROUP BY 1 ORDER BY 2 DESC")]
        bands = [FacetCount(value=str(row[0]), count=int(row[1])) for row in connection.execute(
            """SELECT f.band, COUNT(*) FROM job_freshness f JOIN jobs j ON j.id = f.job_id
               WHERE j.status = 'open' GROUP BY 1 ORDER BY 2 DESC""")]
        last = connection.execute("SELECT MAX(checked_at) FROM job_liveness").fetchone()[0]
        total = int(connection.execute("SELECT COUNT(*) FROM job_auto_hidden WHERE restored_at IS NULL").fetchone()[0])
        reports = [SourceReportOut(**asdict(item)) for item in build_source_reports(connection, config, now)]
    except sqlite3.OperationalError:       # migration 017 not applied yet
        return None
    settings = config.freshness
    return FreshnessHealth(
        enabled=settings.enabled, fresh_days=settings.fresh_days, stale_after_days=settings.stale_after_days,
        auto_hidden_total=total, auto_hidden_by_source=[FacetCount(value=k, count=v) for k, v in hidden.items()],
        auto_hidden_by_reason=by_reason, bands=bands, last_check=last, source_reports=reports)


@router.get("/health", response_model=Health, summary="Per-source status, counts and warnings (no network calls)")
def get_health(db: ReadDb, config: ConfigDep) -> Health:
    return build_health(db, config, utc_now())


@router.get("/health/triage-shadow", summary="Shadow report of what the cascade would have demoted and agreement")
def get_triage_shadow(db: ReadDb, config: ConfigDep) -> dict[str, object]:
    from jobhunt.matching.triage_cascade import run_shadow_report

    return run_shadow_report(db, config)


@router.post("/health/triage-llm-run", summary="Run local-LLM triage pipeline now")
def post_triage_llm_run(db: WriteDb, config: ConfigDep) -> dict[str, Any]:
    from jobhunt.matching.ollama import OllamaClient
    from jobhunt.matching.triage_queue import run_triage_llm_pipeline

    client = OllamaClient()
    gemini_client = None
    try:
        from jobhunt.cli import _gemini_client
        gemini_client = _gemini_client(config)
    except Exception:
        gemini_client = None

    # Run for up to 5 minutes on button click
    res = run_triage_llm_pipeline(
        db,
        config,
        max_minutes=5.0,
        dry_run=False,
        client=client,
        gemini_client=gemini_client,
    )
    return res


@router.get("/health/doctor")
def get_system_doctor(config: ConfigDep, online: bool = False) -> dict[str, Any]:
    """Run SystemDoctor checks and return results for Health system check card."""
    from jobhunt.doctor import SystemDoctor

    doctor = SystemDoctor(config, online=online)
    checks = doctor.run_all_checks()
    return {
        "status": "fail" if any(c.status == "fail" for c in checks) else ("warn" if any(c.status == "warn" for c in checks) else "pass"),
        "checks": [c.as_dict() for c in checks],
    }


