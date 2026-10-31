"""Request/response models (Spring Boot: DTO classes). The TypeScript types in web/src/api mirror these."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Stage = Literal["saved", "applied", "oa_interview", "offer", "rejected"]
STAGES: tuple[Stage, ...] = ("saved", "applied", "oa_interview", "offer", "rejected")

ExperienceLevel = Literal["intern", "new_grad", "entry", "mid", "senior", "unknown"]
EmploymentType = Literal["full_time", "internship", "contract", "part_time"]
JobStatus = Literal["open", "closed", "all"]
SortKey = Literal["newest", "newest_posted", "newest_seen", "oldest", "company", "title", "relevance", "match"]
CategoryKey = Literal["relevant", "target", "maybe", "all", "filtered_out"]
FreshnessBand = Literal["fresh", "aging", "stale", "unknown"]
WorkplaceType = Literal["remote", "hybrid", "onsite", "unknown"]
RemoteScope = Literal["worldwide", "regional", "country_restricted", "unknown"]
GateStatus = Literal["target", "maybe", "excluded"]
LocationFit = Literal["india", "remote_open_to_india", "remote_unclear", "abroad_unknown_workplace",
                     "wrong_location", "unknown"]
FeedbackAction = Literal["dismiss", "like", "undo"]
FeedbackReason = Literal["wrong_role", "wrong_stack", "too_senior", "wrong_location", "company", "other"]

MAX_NOTES_CHARS = 10_000


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- jobs ----------------------------------------------------------------------------------------

class EligibilityScreen(BaseModel):
    kind: str
    label: str
    risk: Literal["possible_knockout", "ok_by_your_value", "needs_confirmation"] | None = None


class KnockoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[str] = Field(min_length=1, max_length=30)


class KnockoutOut(BaseModel):
    question: str
    kind: str
    risk: Literal["possible_knockout", "ok_by_your_value", "needs_confirmation"]
    your_value: str | float | bool | None
    needs_confirmation: bool
    note: str


class SignalOut(BaseModel):
    id: str
    level: Literal["info", "caution"]
    text: str


class JobSummary(BaseModel):
    id: int
    title: str
    company_name: str
    location: str | None
    city: str | None
    country: str | None
    is_remote: bool | None
    experience_level: ExperienceLevel
    employment_type: EmploymentType          # effective type: unknown -> full_time (internship for interns)
    employment_type_raw: str                 # what is stored
    posted_at: str | None
    first_seen_at: str
    listed_at: str                           # posted_at, else first_seen_at: what "newest" sorts on
    sources: list[str]
    needs_description: bool
    status: Literal["open", "closed"]
    saved: bool
    application_id: int | None
    stage: Stage | None
    match_score: float | None = None         # final 0-100 score (relevance layer), None when not scored
    gate_status: GateStatus | None = None    # None: the relevance gate has not judged this job yet
    role_family: str | None = None
    best_profile: Literal["ai", "fullstack"] | None = None
    llm_scored: bool = False                 # the score includes an LLM assessment
    workplace_type: WorkplaceType = "unknown"
    remote_scope: RemoteScope = "unknown"
    remote_eligible_india: bool | None = None
    work_auth_required: bool = False
    feedback: Literal["dismiss", "like"] | None = None
    exclusion_reason: str | None = None      # why the gate hid this job (set only when it was excluded)
    seniority_level: Literal["junior", "unspecified", "caution", "mid", "senior", "executive"] | None = None
    seniority_evidence: str | None = None
    location_fit: LocationFit | None = None
    location_reason: str | None = None
    location_matched: str | None = None
    has_caution_signal: bool = False         # a posting signal at level caution exists (see JobDetail.signals)
    decision: Literal["saved", "applied", "not_applying"] | None = None
    decision_reason: str | None = None
    likely_ineligible: str | None = None     # "Likely not eligible: PhD required" (from the posting; reversible)
    applied_company_warning: str | None = None   # "You applied to X on 17 Oct (R-0007)"
    learned_rules: list[dict[str, str | None]] = []
    freshness: FreshnessBand = "unknown"     # fresh <= 30 days, aging 31-45, stale > 45 (config freshness:), unknown = no date
    age_days: int | None = None
    auto_hidden: Literal["auto_stale", "closed_checked"] | None = None   # why it sits in the Auto-hidden view


class TodayJob(JobSummary):
    apply_url: str | None = None
    opened_at: str | None = None             # when I last clicked "open apply URL"
    closed_at: str | None = None
    deadline_at: str | None = None
    tailored_resume_id: int | None = None
    approved_pdf: bool = False
    applied_at: str | None = None
    follow_up_at: str | None = None
    days_since_change: int | None = None
    next_date: str | None = None
    contact_name: str | None = None


class SourceCount(BaseModel):
    source: str
    new_today: int
    relevant_today: int
    open_total: int


class TodayView(BaseModel):
    generated_at: str
    since: str
    new_total: int
    new_jobs: list[TodayJob]
    top: list[TodayJob]
    closing_soon: list[TodayJob]
    disappeared: list[TodayJob]
    follow_ups: list[TodayJob]
    interviews: list[TodayJob]
    approved_not_applied: list[TodayJob]
    week: dict[str, int] = {}              # this week: applied / saved / not_applying / follow_ups_due
    sources: list[SourceCount]


class JobPage(BaseModel):
    total: int
    page: int
    page_size: int
    sort: SortKey                            # the sort actually applied
    items: list[JobSummary]


class SightingOut(BaseModel):
    source: str
    source_job_id: str
    url: str | None                          # http(s) only; None when the stored URL is not safe to link
    attribution_text: str | None
    first_seen_at: str
    last_seen_at: str


class CompanyOut(BaseModel):
    id: int
    name: str
    ats_type: str
    slug: str
    status: str


class ApplicationOut(BaseModel):
    id: int
    job_id: int
    stage: Stage
    applied_at: str | None
    notes: str | None
    follow_up_at: str | None
    created_at: str
    updated_at: str
    tailored_resume_id: int | None = None
    resume_number: int | None = None         # R-0007 of the attached resume
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None


class MatchDetail(BaseModel):
    """Why a job ranks where it does (all values are stored, nothing is computed on request)."""

    final_score: float | None
    llm_score: float | None
    reason: str | None
    matched_skills: list[str]
    missing_skills: list[str]
    red_flags: list[str]
    recommended_profile: str | None
    similarity_ai: float | None
    similarity_fullstack: float | None
    similarity_percentile: float | None
    components: dict[str, object]
    gate_status: GateStatus | None
    gate_rule: str | None
    exclusion_reason: str | None             # the gate's reason; set for filtered-out jobs
    required_years: int | None
    remote_evidence: str | None
    remote_regions: list[str]
    remote_source: str | None


class FeedbackCreate(Model):
    action: FeedbackAction
    reason: FeedbackReason | None = None


VerdictLabel = Literal["wrong_level", "wrong_years", "wrong_location", "other"]


class VerdictWrong(Model):
    label: VerdictLabel = "other"
    reason: str | None = None


class FeedbackResult(BaseModel):
    job_id: int
    feedback: Literal["dismiss", "like"] | None
    reason: str | None


class JobDetail(JobSummary):
    match: MatchDetail | None = None
    description_text: str | None             # plain text; the UI must never render it as HTML
    apply_url: str | None
    sightings: list[SightingOut]
    company: CompanyOut | None
    application: ApplicationOut | None
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    sponsorship_note: str | None
    graduation_year_hint: int | None
    last_seen_at: str
    closed_at: str | None
    eligibility: list[dict[str, object]] = []          # education / citizenship flags with the posting's own words
    why_match: dict[str, object] = {}
    triage: dict[str, object] | None = None      # LLM verdict with quotes, the rule that used it, student chips (v8)
    eligibility_screens: list[EligibilityScreen] = []   # CGPA / backlog / batch screens named in the posting
    signals: list[SignalOut] = []            # posting facts (age, source, repost...); never affect the score
    liveness: dict[str, object] | None = None    # last "still open?" check: result, detail, checked_at, method
    closed_reason: str | None = None
    remember_level_word: str | None = None   # level word of the title the "Remember for this company" box would deny


class FacetCount(BaseModel):
    value: str
    count: int


class Facets(BaseModel):
    total_open: int
    levels: list[FacetCount]
    employment_types: list[FacetCount]
    countries: list[FacetCount]
    sources: list[FacetCount]
    remote: int
    needs_description: int
    new_24h: int
    generated_at: str
    scored: int = 0                          # open jobs with a final score
    relevant: int = 0                        # open jobs the gate did not exclude
    filtered_out: int = 0                    # open jobs the gate excluded
    workplace_types: list[FacetCount] = []   # among relevant jobs
    remote_open_to_india: int = 0            # among relevant jobs
    gate_rules: list[FacetCount] = []        # why jobs were filtered out
    location_fits: list[FacetCount] = []     # among gate-relevant, not-dismissed jobs
    auto_hidden: int = 0                     # jobs waiting in the Auto-hidden view
    freshness_bands: list[FacetCount] = []   # fresh / aging / unknown among visible relevant jobs
    category_counts: list[FacetCount] = []   # jobs per category control value (same filter as the list)
    show_other_locations_default: bool = False


# --- applications --------------------------------------------------------------------------------

class ApplicationCreate(Model):
    job_id: int = Field(ge=1)
    stage: Stage = "saved"


class ApplicationPatch(Model):
    """Only the fields present in the JSON body are changed; an explicit null clears a field."""

    stage: Stage | None = None
    notes: str | None = Field(default=None, max_length=MAX_NOTES_CHARS)
    follow_up_at: str | None = None
    applied_at: str | None = None
    tailored_resume_id: int | None = None   # attach (or null: detach) a tailored resume version
    contact_name: str | None = Field(default=None, max_length=200)
    contact_email: str | None = Field(default=None, max_length=200)
    contact_phone: str | None = Field(default=None, max_length=60)

    @field_validator("follow_up_at", "applied_at")
    @classmethod
    def _iso_date_or_datetime(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        from datetime import date, datetime

        text = value.strip()
        try:
            if len(text) == 10:
                date.fromisoformat(text)
            else:
                datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("must be an ISO date (YYYY-MM-DD) or ISO datetime") from error
        return text


class BoardCard(BaseModel):
    application: ApplicationOut
    title: str
    company_name: str
    location: str | None
    country: str | None
    apply_url: str | None
    job_status: Literal["open", "closed"]


class BoardColumn(BaseModel):
    stage: Stage
    count: int
    cards: list[BoardCard]


class Board(BaseModel):
    total: int
    columns: list[BoardColumn]


# --- manual add ----------------------------------------------------------------------------------

class ManualJob(Model):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    company: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    apply_url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=8, max_length=2048)]
    location: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100_000)] | None = None
    fetch_from_ats: bool = True


class AtsFetchResult(BaseModel):
    attempted: bool
    status: Literal["not_applicable", "skipped", "fetched", "not_found", "failed"]
    detail: str | None = None


class ManualJobResult(BaseModel):
    created: bool                            # False when the URL already matched an existing job
    job: JobDetail
    ats: AtsFetchResult


# --- health --------------------------------------------------------------------------------------

class RunSummaryOut(BaseModel):
    started_at: str
    finished_at: str | None
    companies_checked: int
    jobs_seen: int
    jobs_new: int
    jobs_closed: int
    jobs_skipped_by_prefilter: int
    error_count: int
    first_error: str | None


class SourceHealth(BaseModel):
    source: str
    kind: Literal["ats", "feed", "email"]
    enabled: bool
    last_run_at: str | None
    last_success_at: str | None
    age_hours: float | None                  # hours since last success
    stale: bool
    never_run: bool
    fetch_count_today: int
    last_run: RunSummaryOut | None


class GmailHealth(BaseModel):
    credentials_file_exists: bool
    token_file_exists: bool
    scopes: list[str]
    read_only_scope: bool                    # token carries exactly gmail.readonly
    account_configured: str | None


class HealthWarning(BaseModel):
    code: str
    message: str


class LlmUsage(BaseModel):
    calls_today: int
    daily_call_cap: int
    interactive_calls_today: int = 0
    interactive_call_cap: int = 40
    triage_calls_today: int = 0
    triage_call_cap: int = 20
    usd_today: float
    usd_month: float
    monthly_usd_cap: float
    embedded_texts_today: int
    daily_embedding_cap: int


class LocalStage(BaseModel):
    stage: str
    last_run_at: str | None
    seconds: float | None
    items: int | None
    model: str | None
    status: str | None
    detail: str | None


class CostProjection(BaseModel):
    calls_per_day: int
    avg_input_tokens: int
    avg_output_tokens: int
    model: str
    usd_per_job: float
    usd_per_month: float
    usd_per_month_after_price_change: float
    price_change_on: str | None
    based_on_calls: int


class BudgetForecast(BaseModel):
    month_to_date_usd: float
    avg_daily_usd_7d: float
    projected_month_usd: float
    cap_hit_date: str | None = None
    warning: bool = False
    cap_usd: float


class HeadHealth(BaseModel):
    mode: str = "shadow"
    n_pos: int = 0
    n_neg: int = 0
    precision: float | None = None
    coverage: float | None = None
    auc: float | None = None
    can_activate: bool = False
    needs_hint: str | None = None


class ActingRuleOut(BaseModel):
    field: str
    target_class: str
    sample_count: int
    high_conf_count: int
    precision: float
    recall: float
    can_act: bool
    mode: str
    reason: str
    evaluated_at: str
    ci_lower: float | None = None
    ci_upper: float | None = None
    rule_desc: str = ""


class TriageHealth(BaseModel):
    enabled: bool
    rubric_version: str
    model: str
    verdicts: int
    by_level: dict[str, int] = {}
    verdict_counts_by_field: dict[str, dict[str, int]] = {}
    leak_rate_last_100: float | None = None
    judged_in_window: int = 0
    leaked_in_window: int = 0
    hallucinated_evidence_rate: float | None = None
    not_verbatim_rate: float | None = None
    irrelevant_evidence_rate: float | None = None
    hallucinated_fields: int = 0
    not_verbatim_fields: int = 0
    irrelevant_fields: int = 0
    claimed_fields: int = 0
    demoted_week_llm: int = 0
    demoted_week_classifier: int = 0
    demoted_week_rules: int = 0
    verdict_wrong_marked: int = 0
    verdicts_that_acted: int = 0
    agreement: float | None = None
    daily_calls_cap: int = 30
    calls_today: int = 0
    tier_mix_7d: dict[str, int] = {}
    gemini_calls_saved_7d: int = 0
    heads_status: dict[str, HeadHealth] = {}
    last_trained_at: str | None = None
    last_train_metrics: dict[str, Any] = {}
    label_counts: dict[str, int] = {}
    shadow_summary: dict[str, Any] = {}
    coverage_visible_relevant_judged: int = 0
    coverage_visible_relevant_total: int = 0
    coverage_visible_relevant_pct: float = 0.0
    coverage_gemini: int = 0
    coverage_local_only: int = 0
    coverage_none: int = 0
    queue_length: int = 0
    last_run_duration_seconds: float | None = None
    last_jobhunt_triage_run: str | None = None
    gemini_confirmations_used: int = 0
    models_disagree_count: int = 0
    acting_rules: list[ActingRuleOut] = []
    audit_progress: str = "imported 0 of 60"
    audit_imported: int = 0
    audit_total: int = 60


class MatchingHealth(BaseModel):
    local_stages: list[LocalStage] = []
    projection: CostProjection | None = None
    budget_forecast: BudgetForecast | None = None
    usage: LlmUsage
    embedding_coverage: float                # 0..1 of open target/maybe jobs that have an embedding
    embedded: int
    embeddable: int
    scored_jobs: int
    last_relevance_run_at: str | None
    last_llm_call_at: str | None
    profiles: list[str]


class ScheduledTaskHealth(BaseModel):
    task: str
    name: str
    description: str
    schedule: str
    heavy: bool
    last_run_at: str | None
    last_status: str | None
    last_duration_seconds: float | None
    last_detail: str | None
    last_ok_at: str | None
    next_run_at: str | None
    missed_runs: int
    missed_warning: bool


class ScheduleHealth(BaseModel):
    backend: str = "windows_task_scheduler"
    tasks: list[ScheduledTaskHealth]
    missed_runs: list[dict[str, Any]] = Field(default_factory=list)
    gmail_auth_expired: bool
    gmail_auth_reason: str | None = None
    gmail_auth_since: str | None = None
    gmail_reauth_command: str


class SourceReportOut(BaseModel):
    source: str
    shown: int
    reported_closed: int
    reported_too_old: int
    decided: int
    closed_share: float
    auto_hidden: int
    current_stale_after_days: int
    proposed_stale_after_days: int | None = None
    proposal: str | None = None


class FreshnessHealth(BaseModel):
    enabled: bool
    fresh_days: int
    stale_after_days: int
    auto_hidden_total: int
    auto_hidden_by_source: list[FacetCount] = []
    auto_hidden_by_reason: list[FacetCount] = []
    bands: list[FacetCount] = []                 # fresh / aging / unknown / stale among open jobs
    last_check: str | None = None                # latest "still open?" check
    source_reports: list[SourceReportOut] = []


class ReadinessCheckItem(BaseModel):
    id: str
    label: str
    passed: bool
    link: str
    guidance: str


class ReadinessChecklist(BaseModel):
    key_set: ReadinessCheckItem
    resume_validated: ReadinessCheckItem
    facts_ticked: ReadinessCheckItem
    profile_complete: ReadinessCheckItem
    sources_chosen: ReadinessCheckItem
    first_ingest_done: ReadinessCheckItem
    first_match_done: ReadinessCheckItem
    ready: bool


class Health(BaseModel):
    freshness: FreshnessHealth | None = None
    schedule: ScheduleHealth | None = None
    matching: MatchingHealth | None = None
    triage: TriageHealth | None = None
    budget_forecast: BudgetForecast | None = None
    last_morning_check: str | None = None
    readiness: ReadinessChecklist | None = None
    generated_at: str
    jobs_open: int
    jobs_closed: int
    companies_active: int
    companies_unverified: int
    companies_inactive: int
    applications_by_stage: dict[str, int]
    sources: list[SourceHealth]
    gmail: GmailHealth
    user_agent_contact_set: bool
    schema_version: int
    warnings: list[HealthWarning]


class AuditItemOut(BaseModel):
    job_id: int
    company: str
    title: str
    source: str
    category: str
    snippet: str
    gemini_verdict: str
    gemini_quote: str
    local_verdict: str
    local_quote: str
    deterministic_hint: str | None = None
    row_order: int
    my_verdict: str | None = None
    my_note: str | None = None


class AuditStateOut(BaseModel):
    items: list[AuditItemOut]
    current_index: int
    total_items: int
    answered_count: int
    gemini_correct: int
    local_correct: int
    both_wrong_count: int
    both_right_count: int
    unsure_count: int
    decisive_count: int
    gemini_accuracy: float
    gemini_ci: list[float]
    local_accuracy: float
    local_ci: list[float]
    acting_rules: list[dict[str, Any]] = Field(default_factory=list)


class AuditLabelIn(BaseModel):
    job_id: int
    my_verdict: str
    my_note: str = ""


class AuditUndoIn(BaseModel):
    job_id: int

