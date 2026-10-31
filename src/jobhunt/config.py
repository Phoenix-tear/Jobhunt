"""Typed view over config.yaml (the application.yml of this project).

Unknown keys are rejected so a typo in config.yaml fails loudly instead of being ignored.
"""

from __future__ import annotations

import dataclasses
import os
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jobhunt import __version__
from jobhunt.errors import ConfigError

DEFAULT_CONFIG_NAME = "config.yaml"
LOCAL_CONFIG_NAME = "config.local.yaml"  # gitignored overrides (contact email, mailbox)
SETTINGS_CONFIG_NAME = "data/settings.yaml"  # gitignored web UI overrides


@dataclass(frozen=True)
class UiSettings:
    privacy_acknowledged: bool = False
    budget_preset: str = "conservative"


@dataclass(frozen=True)
class SetupSettings:
    completed: bool = False
    step: str = "welcome"
    completed_at: str | None = None
    schema_version: int = 1
    first_ingest_time_cap_minutes: int = 15


@dataclass(frozen=True)
class UserSettings:
    """Facts about the person the matching layer works for."""

    home_country: str = "IN"   # ISO-2; "remote jobs open to me" means open to someone living here


@dataclass(frozen=True)
class TargetSettings:
    graduation_year: int = 2027
    employment_types: tuple[str, ...] = ("full_time", "internship")
    regions: tuple[str, ...] = ()
    fields: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class SenioritySettings:
    exclude_at_years: int = 2        # the description asks for this many years or more -> excluded
    maybe_at_years: int = 1          # exactly this many -> "maybe"
    exempt_levels: tuple[str, ...] = ("intern", "new_grad")   # explicit intern / new-grad postings skip the years rule
    exclude_levels: tuple[str, ...] = ("senior",)             # stored experience_level values excluded outright
    maybe_levels: tuple[str, ...] = ("mid",)                  # kept as "maybe"
    mode: str = "junior_only"       # junior_only: title-derived mid/senior/executive levels are excluded; off: ignore
    level_code_rules: tuple[dict[str, Any], ...] = ()         # employer grade codes: {name, pattern, level, scope, note}


@dataclass(frozen=True)
class RoleTitleSettings:
    ai_ml: tuple[str, ...] = ()
    ai_ml_bare: tuple[str, ...] = ()                            # "ai", "ml": a target only next to a role noun
    role_nouns: tuple[str, ...] = ()                            # engineer, developer, scientist, intern, ...
    fullstack: tuple[str, ...] = ()
    fullstack_weak: tuple[str, ...] = ()                        # bare "developer": loses to a lower-priority focus
    maybe: tuple[str, ...] = ()                                 # plausible but needs a second look
    lower_priority: dict[str, tuple[str, ...]] = field(default_factory=dict)   # allowed, ranked lower
    ai_boost: tuple[str, ...] = ()                              # inside a lower-priority family: AI-side, ranked up
    off_domain: dict[str, tuple[str, ...]] = field(default_factory=dict)       # software-looking, other domain: maybe
    cjk_title_is_maybe: bool = True                             # CJK characters in the title: likely local-language role
    exclude: tuple[str, ...] = ()                               # hard excludes (title phrases)
    generic: tuple[str, ...] = ()                               # "engineer": decided by description tech signals
    tech_signals: tuple[str, ...] = ()                          # description words that make a generic title software
    tech_signal_min: int = 4                                    # distinct tech words a generic title needs
    lower_priority_adjust: float = -0.15                        # added to the rank of lower-priority families
    lower_with_software_adjust: float = -0.05                   # "Software Engineer, Data Platform": mild
    ai_boost_adjust: float = 0.10                               # added to the rank of AI-side Salesforce-style roles


@dataclass(frozen=True)
class RelevanceSettings:
    """Deterministic, non-destructive relevance gate (config.yaml `relevance:`)."""

    target_graduation_year: int = 2027
    seniority: SenioritySettings = field(default_factory=SenioritySettings)
    internships_must_be_current: bool = True
    titles: RoleTitleSettings = field(default_factory=RoleTitleSettings)
    excluded_stacks: dict[str, tuple[str, ...]] = field(default_factory=dict)   # name -> language keywords
    allowed_stacks: tuple[str, ...] = ()                        # any of these required => never stack-excluded
    soft_markers: tuple[str, ...] = ()                          # "a plus", "nice to have": not a requirement
    eligibility_source_lists_required: bool = False             # Simplify degree lists (PhD only) cap the score too


@dataclass(frozen=True)
class ProfileSettings:
    """Where the resumes live and which one is authoritative (config.yaml `profiles:`)."""

    resume_dir: str = "data/resumes"
    profile_dir: str = "data/profile"
    files: dict[str, str] = field(default_factory=lambda: {"ai": "resume_ai.tex", "fullstack": "resume_fullstack.tex"})
    optional: tuple[str, ...] = ("backend",)   # bases skipped silently when their file is absent (tailoring only)
    authoritative: str = "ai"            # wins where the resumes disagree
    person_name: str = ""                # only needed when the header line is not the name


@dataclass(frozen=True)
class GeminiSettings:
    """Transport settings for the Gemini REST API (the key itself lives in .env as GEMINI_API_KEY)."""

    base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    timeout_seconds: float = 90.0
    max_retries: int = 5
    backoff_base_seconds: float = 4.0
    backoff_max_seconds: float = 60.0
    retry_after_cap_seconds: float = 120.0


@dataclass(frozen=True)
class EmbeddingSettings:
    provider: str = "gemini"                        # gemini | local (Ollama on 127.0.0.1)
    local_model: str = "nomic-embed-text"           # Ollama embedding model used when provider is local
    local_dimensions: int = 768                     # its vector size (checked against the real answer)
    model: str = "gemini-embedding-2"
    fallback_model: str = "gemini-embedding-001"   # switch by editing `model`; spaces are incompatible
    dimensions: int = 768
    prompt_version: str = "v1"                      # bump when the text/prompt format changes: forces re-embed
    description_token_limit: int = 1500             # description is cut to about this many tokens (4 chars each)
    batch_size: int = 50                            # texts per batchEmbedContents request
    max_concurrent_batches: int = 4
    max_tokens_per_minute: int = 500_000            # self-imposed pacing, below the account's TPM limit


@dataclass(frozen=True)
class LocationSettings:
    """Which jobs are in the right place. Personal overrides belong in config.local.yaml."""

    accept_workplace_in_india: tuple[str, ...] = ("remote", "hybrid", "onsite")
    accept_abroad: tuple[str, ...] = ("remote_open_to_india",)
    show_other_locations_default: bool = False


@dataclass(frozen=True)
class TailoringRecipe:
    """One resume recipe per kind of job (editable in config.yaml `tailoring.recipes`)."""

    base: str = "ai"                                   # preferred base resume
    triggers: tuple[str, ...] = ()                     # keyword concepts that select this recipe
    summary_variant: str = ""                          # preferred approved summary variant (S1..S6)
    projects: tuple[str, ...] = ()                     # project name fragments, best first
    must_keep_phrases: tuple[str, ...] = ()            # bullets containing these are dropped last
    lead_skill_groups: tuple[str, ...] = ()            # skills groups to put first
    certifications_first: bool = False                 # certifications line first, Agentforce first inside it


@dataclass(frozen=True)
class TailoringSettings:
    """Resume tailoring (`tailor`; it only ever runs on request, one job per click)."""

    llm_enabled: bool = True              # false: deterministic selection only, no model call
    provider: str = "gemini"              # gemini | anthropic (anthropic needs ANTHROPIC_API_KEY, off by default)
    model: str = "gemini-2.5-pro"         # advice model (recipe / variant / keywords / ranking), never free text
    fallback_model: str = "gemini-3.8-flash"   # used on errors or budget exhaustion
    anthropic_model: str = "claude-sonnet-5-5"
    max_output_tokens: int = 4000         # hard limit of the answer (thinking tokens count)
    target_fill_min: float = 0.92         # the page should be at least this full
    blank_threshold: float = 0.06         # more blank space than this triggers the fill pass
    top_n_max: int = 20                   # `tailor-top` (manual, --yes) never tailors more than this
    variants_file: str = "data/profile/summaries.md"
    recipes: dict[str, TailoringRecipe] = field(default_factory=dict)
    project_relevance_threshold: float = 0.12   # share of the job's weighted concepts a project must cover to be shown on merit
    low_relevance_bullets: int = 2              # a below-threshold project that is only here to fill the page keeps this many
    max_bullets_per_project: int = 5            # depth cap when spare space goes to the most relevant entries
    domain_bonus: float = 0.08                  # added to a project's relevance when it matches the job's domain (finance)
    strength_low_below: float = 0.50            # tailoring strength score below this is Low, below `strength_high_from` Medium
    strength_high_from: float = 0.75


@dataclass(frozen=True)
class ScheduleSettings:
    """Daily automation through scheduler backends (`schedule-install`). Times are local to `timezone`."""

    backend: str = "windows_task_scheduler" if os.name == "nt" else "in_app"  # windows_task_scheduler, macos_launchd, in_app, none
    timezone: str = "Asia/Kolkata"         # the Gemini daily cap resets at 00:00 UTC = 05:30 IST
    task_prefix: str = "jobhunt-"          # `schedule-remove` only ever touches tasks with this prefix
    ingest_times: tuple[str, ...] = ("06:30", "19:00")   # ingest + relevance-run
    ollama_ensure_time: str = "06:40"
    match_time: str = "06:45"
    email_every_hours: int = 4             # ingest-email --since-days 2 (skipped when the Gmail token is dead)
    email_since_days: int = 2
    discover_day: str = "Sunday"
    discover_time: str = "03:00"
    morning_check_time: str | None = None
    wake_to_run: bool = False              # wake the computer for a task (off: it runs at the next wake / logon)
    execution_limit_hours: int = 3         # stop a task that runs longer
    missed_runs_warning: int = 2           # Health shows red when a task missed more than this many runs
    heavy_lock_wait_minutes: int = 120     # how long a heavy task waits for the lock before it gives up
    light_lock_wait_minutes: int = 10
    queued_lock_wait_minutes: int = 240    # match and ingest-email queue behind a long ingest this long
    queued_execution_limit_hours: int = 6  # their Task Scheduler time limit (the lock wait counts against it)
    log_dir: str = "data/logs"
    lock_file: str = "data/locks/jobhunt.lock"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_start_wait_seconds: int = 180   # a cold start after boot (GPU discovery) can take over a minute
    start_grace_seconds: int = 15          # tasks that start together register first, then take the lock in order
    triage_time: str | None = None         # set to e.g. "07:30" to run triage cascade as a separate scheduled task


@dataclass(frozen=True)
class WritingSettings:
    """Cover letter and application-answer helper (on click only; uses the tailoring model)."""

    model: str = "gemini-2.5-pro"          # letters and answers write real text: the stronger model; empty = tailoring.model
    cover_words_min: int = 220
    cover_words_max: int = 300
    answer_words_default: int = 150        # when the question states no limit
    max_output_tokens: int = 6000          # includes thinking tokens; v8 probe: gemini-2.5-pro finishes normally with the thinking budget
    max_question_chars: int = 1200
    pipeline: str = "staged"               # staged (plan -> write -> edit) | single (the v1 one-call writer)
    edit_enabled: bool = True              # the separate grammar / flow edit call
    max_attempts: int = 2                  # write+edit rounds per click (the automatic retry is the second one)
    max_usd_per_click: float = 0.10        # the hard dollar limit of one click (all calls of it)
    thinking_budget: int = 1024            # tokens a gemini-2.5 model may think before answering (it used ~3,990 of 4,000 unbounded)
    voice_file: str = "data/profile/writing_samples.md"


@dataclass(frozen=True)
class LocalLlmSettings:
    """The optional local LLM tier (Ollama). Switch a task on only after its benchmark met the threshold."""

    enabled: bool = False
    model: str = "qwen3:4b"
    workplace: bool = False                # extract workplace / remote from the posting
    classify: bool = False                 # borderline `maybe` triage
    prescore: bool = False                 # fit pre-score for the ranker's top candidates
    workplace_limit: int = 150
    classify_limit: int = 100
    prescore_top: int = 150
    per_job_timeout_seconds: float = 90.0
    time_box_seconds: float = 1500.0       # the whole stage stops after this long (resumable)


@dataclass(frozen=True)
class LlmSettings:
    """Which cheap side tasks may still call Gemini. Off by default: rules (and the local model) answer them."""

    classify_enabled: bool = False         # triage of borderline `maybe` jobs
    workplace_enabled: bool = False        # read the workplace type of top-ranked unknown jobs
    remote_enabled: bool = False           # "open to India?" tie-break for ambiguous remote jobs


@dataclass(frozen=True)
class TriageHeadSettings:
    """Settings per head (seniority, location, eligibility). Default mode is shadow."""

    mode: str = "shadow"             # shadow | active
    act_threshold: float = 0.90      # probability above which it demotes (held-out precision >= 95%)
    abstain_low: float = 0.40        # lower bound of abstain band


@dataclass(frozen=True)
class TriageSettings:
    """LLM-assisted triage and cascade: Tier 0 rules -> Tier 1 local classifier -> Tier 2 local LLM -> Tier 3 Gemini."""

    enabled: bool = True
    model: str = "gemini-3.8-flash"
    thinking: str = "low"                           # thinking_level; thinking tokens bill as output
    rubric_version: str = "v1"
    jobs_per_call: int = 5
    max_output_tokens: int = 3000
    shortlist_size: int = 100                       # top-ranked un-judged jobs the daily pipeline may judge
    exclude_years_min: int = 2                      # a verified required_years_min at or above this excludes
    student_enabled: bool = True                    # rules and classifier mined from verdicts (demote only)
    student_precision: float = 0.90
    student_min_support: int = 5
    # Cascade additions:
    local_llm_model: str = "qwen2.5:7b-instruct"    # Tier 2 local model in Ollama
    local_llm_max_minutes: int = 90                 # per-run cap in minutes (default 90 in v10)
    local_llm_timeout_seconds: float = 90.0
    candidate_shortlist_top: int = 300              # candidates considered for Tier 2
    gemini_shortlist_top: int = 100                 # candidates considered for Tier 3 Gemini
    # Brief v10 additions:
    acting_rule_min_examples: int = 100             # Section 1: at least 100 examples to act
    acting_rule_min_precision: float = 0.90         # Section 1: precision >= 90%
    high_stakes_top_n: int = 50                     # Section 4: top 50 candidates check Gemini first
    blast_radius_max_ratio: float = 0.25            # Section 2: max 25% moved out of Relevant per run
    # Brief v10b additions:
    primary_judge: str = "gemini"                   # gemini | local_llm | gemini_if_local_flags
                                                    # - gemini: Gemini is primary judge for visible Relevant jobs (default);
                                                    #   local LLM runs first look, reorders queue, shows 2nd opinion chips.
                                                    # - local_llm: Local LLM is the sole primary judge (zero Gemini budget).
                                                    # - gemini_if_local_flags: Local LLM screens all jobs; Gemini only judges
                                                    #   jobs where local LLM flagged senior/years>=2/not-India/blocker.
    heads: dict[str, TriageHeadSettings] = field(default_factory=lambda: {
        "seniority": TriageHeadSettings(mode="shadow"),
        "location": TriageHeadSettings(mode="shadow"),
        "eligibility": TriageHeadSettings(mode="shadow"),
    })


@dataclass(frozen=True)
class ModelSettings:
    classify: str = "gemini-3.5-flash-lite"         # borderline `maybe` jobs and ambiguous remote cases
    score: str = "gemini-3.8-flash"                 # final fit scoring of the daily shortlist
    classify_thinking: str = "minimal"              # thinking_level; thinking tokens bill as output
    score_thinking: str = "low"                     # 3.8 Flash's lowest documented level is "low"


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1M tokens. `then_*` applies from `changes_on` (Google announced a price change)."""

    input: float | None = None
    output: float | None = None
    changes_on: str | None = None
    then_input: float | None = None
    then_output: float | None = None


@dataclass(frozen=True)
class PricingSettings:
    as_of: str = ""                                 # the date the official page was read
    source: str = ""
    models: dict[str, ModelPrice] = field(default_factory=dict)
    fallback_input_per_million: float = 1.0         # conservative placeholder for a model not on the page
    fallback_output_per_million: float = 4.0


@dataclass(frozen=True)
class LocationScores:
    """A 0-100 score per kind of location; blended with `weight_location` (editable in config.yaml)."""

    home_country_onsite: float = 100.0     # the job sits in the home country
    remote_open_to_home: float = 100.0     # remote and open to someone living in the home country
    remote_unclear: float = 40.0           # remote, but who may apply is not stated
    abroad: float = 25.0                   # abroad roles stay included, they just rank lower
    remote_restricted_elsewhere: float = 0.0
    unknown: float = 30.0                  # no location information at all: a small penalty


@dataclass(frozen=True)
class ScoringSettings:
    top_k: int = 30                        # daily shortlist sent for final scoring
    batch: bool = False                    # nightly scoring through the Batch API (50% price, next-day results)
    prompt_style: str = "compact"          # compact: profile cards + trimmed posting + small answer; full: v2 prompt
    junior_boost: float = 6.0              # points added for an explicit junior role (unspecified gets none)
    recent_days: int = 14                  # only jobs first seen this recently are shortlisted
    classify_limit: int = 40               # borderline `maybe` jobs triaged per run
    workplace_limit: int = 80              # top-ranked jobs with an unknown workplace asked per run
    weight_fit: float = 0.55               # LLM score, or scaled similarity if unscored
    weight_similarity_percentile: float = 0.25
    weight_location: float = 0.20
    similarity_floor: float = 0.55         # cosine mapped to 0 for an unscored job
    similarity_ceiling: float = 0.75       # cosine mapped to 100
    rank_adjust_scale: float = 100.0       # gate rank_adjust (-0.15) x this = points
    years_penalty_per_year: float = 6.0    # points off per year of required experience ...
    years_penalty_cap: float = 12.0        # ... up to this many
    irrelevant_fit: float = 10.0           # fit used for a `maybe` the classifier calls irrelevant
    eligibility_cap: int = 50              # a posting that requires a PhD / Master's / citizenship is capped here
    injection_score_cap: int = 50          # an LLM score for a posting with injection-like text is capped here
    excluded_role_cap: int = 30            # fit is capped here when the model says the role is an excluded kind
    max_output_tokens_score: int = 700
    max_output_tokens_classify: int = 250
    location: LocationScores = field(default_factory=LocationScores)


@dataclass(frozen=True)
class BudgetSettings:
    daily_calls: int = 200                          # generateContent calls per UTC day (classify + score)
    monthly_usd: float = 10.0                       # estimated spend per UTC month, all purposes
    interactive_daily_calls: int = 40               # tailor / cover letter / answer / experiment calls per UTC day
    daily_embedding_texts: int = 20000              # texts embedded per UTC day
    triage_daily_calls: int = 30                    # LLM triage (verdict) calls per UTC day in the daily pipeline


@dataclass(frozen=True)
class PrefilterSettings:
    extra_include_title_keywords: tuple[str, ...] = ()
    protected_title_phrases: tuple[str, ...] = ()
    exclude_title_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClassificationSettings:
    intern_keywords: tuple[str, ...] = ()
    new_grad_keywords: tuple[str, ...] = ()
    entry_keywords: tuple[str, ...] = ()
    mid_keywords: tuple[str, ...] = ()
    senior_keywords: tuple[str, ...] = ()
    part_time_keywords: tuple[str, ...] = ()
    contract_keywords: tuple[str, ...] = ()
    full_time_keywords: tuple[str, ...] = ()
    extra_city_countries: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class HttpSettings:
    timeout_seconds: float = 20.0
    max_retries: int = 3
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    retry_after_cap_seconds: float = 60.0
    max_workers: int = 8
    default_min_interval_seconds: float = 0.25
    host_min_interval_seconds: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class LifecycleSettings:
    missed_runs_to_close: int = 3
    failures_to_deactivate: int = 3
    distinct_failure_days_to_deactivate: int = 2
    inactive_recheck_days: int = 30
    suspicious_drop_min_previous_jobs: int = 10
    suspicious_drop_ratio: float = 0.1
    suspicious_empty_runs_before_accept: int = 3
    detail_fetch_cap_per_company: int = 200


@dataclass(frozen=True)
class LivenessSettings:
    """The "is it still open?" check (explicit button, and the daily shortlist step of `match`)."""

    max_requests_per_run: int = 100          # the shortlist step never sends more than this many requests
    seconds_between_requests: float = 1.0
    cache_hours: int = 24                    # a result younger than this is reused by the shortlist step
    timeout_seconds: float = 15.0
    max_redirects: int = 4


@dataclass(frozen=True)
class FreshnessSettings:
    """Posting age bands and auto-hiding of stale jobs (jobhunt.freshness). No model involved."""

    enabled: bool = True
    fresh_days: int = 30                     # age <= this: fresh
    stale_after_days: int = 45               # age > this: stale -> moved to Auto-hidden (31..45: aging)
    source_stale_after_days: dict[str, int] = field(default_factory=dict)   # per source override of stale_after_days
    internship_stale_after_days: int = 90    # internships / graduate programmes: stale only after this many days ...
    unknown_stale_after_days: int = 30       # no posting date: stale if first seen longer ago AND an aggregator source
    aggregator_sources: tuple[str, ...] = ()
    liveness: LivenessSettings = field(default_factory=LivenessSettings)


@dataclass(frozen=True)
class CrawlPattern:
    ats: str
    url: str


@dataclass(frozen=True)
class CommonCrawlSettings:
    enabled: bool = True
    index_base_url: str = "https://index.commoncrawl.org"
    crawls_to_query: int = 1
    max_pages_per_pattern: int = 25
    patterns: tuple[CrawlPattern, ...] = ()


@dataclass(frozen=True)
class OpenRolesSettings:
    enabled: bool = False
    base_url: str = "https://raw.githubusercontent.com/datascry/openroles/main/data/tenants"
    only_status: str = "live"


@dataclass(frozen=True)
class DiscoverySettings:
    probe_batch_size: int = 50
    probe_min_interval_hours: float = 12.0
    default_probe_limit: int | None = 300   # boards probed per `discover` run when --probe-limit is not given
    snowball: bool = True
    seed_file: str | None = None
    common_crawl: CommonCrawlSettings = field(default_factory=CommonCrawlSettings)
    openroles: OpenRolesSettings = field(default_factory=OpenRolesSettings)


@dataclass(frozen=True)
class EmailPlatformSettings:
    """One job-alert platform: which sender addresses/domains are trusted for it."""

    enabled: bool = True
    senders: tuple[str, ...] = ()   # "alerts@x.com" (exact) or "@x.com" (domain and subdomains)


@dataclass(frozen=True)
class EmailSettings:
    account: str = ""               # expected mailbox; a different authorized account is refused
    credentials_file: str = ".secrets/credentials.json"
    token_file: str = ".secrets/token.json"
    lookback_days: int = 14         # first run (no stored position) reads this far back
    max_messages_per_run: int = 200
    resolve_ats_links: bool = True  # fetch ATS-hosted postings through the ATS API right away
    stale_after_days: int = 30
    platforms: dict[str, EmailPlatformSettings] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceSettings:
    """One block under `sources:`. `enabled` is reserved; everything else is an option."""

    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "SourceSettings":
        data = dict(data or {})
        enabled = bool(data.pop("enabled", True))
        return cls(enabled=enabled, options=data)


@dataclass(frozen=True)
class Config:
    user_agent_contact: str = ""
    database_path: str = "data/jobhunt.sqlite3"
    cache_dir: str = "data/cache"
    migrations_dir: str = "migrations"
    user: UserSettings = field(default_factory=UserSettings)
    targets: TargetSettings = field(default_factory=TargetSettings)
    prefilter: PrefilterSettings = field(default_factory=PrefilterSettings)
    relevance: RelevanceSettings = field(default_factory=RelevanceSettings)
    profiles: ProfileSettings = field(default_factory=ProfileSettings)
    gemini: GeminiSettings = field(default_factory=GeminiSettings)
    embedding: EmbeddingSettings = field(default_factory=EmbeddingSettings)
    llm: LlmSettings = field(default_factory=LlmSettings)
    local_llm: LocalLlmSettings = field(default_factory=LocalLlmSettings)
    location: LocationSettings = field(default_factory=LocationSettings)
    tailoring: TailoringSettings = field(default_factory=TailoringSettings)
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    writing: WritingSettings = field(default_factory=WritingSettings)
    models: ModelSettings = field(default_factory=ModelSettings)
    triage: TriageSettings = field(default_factory=TriageSettings)
    pricing: PricingSettings = field(default_factory=PricingSettings)
    budget: BudgetSettings = field(default_factory=BudgetSettings)
    scoring: ScoringSettings = field(default_factory=ScoringSettings)
    classification: ClassificationSettings = field(default_factory=ClassificationSettings)
    http: HttpSettings = field(default_factory=HttpSettings)
    lifecycle: LifecycleSettings = field(default_factory=LifecycleSettings)
    freshness: FreshnessSettings = field(default_factory=FreshnessSettings)
    discovery: DiscoverySettings = field(default_factory=DiscoverySettings)
    email: EmailSettings = field(default_factory=EmailSettings)
    sources: dict[str, SourceSettings] = field(default_factory=dict)
    ui: UiSettings = field(default_factory=UiSettings)
    setup: SetupSettings = field(default_factory=SetupSettings)
    project_root: Path = field(default_factory=Path.cwd)
    data_dir: Path | None = None

    @property
    def user_agent(self) -> str:
        """Polite, identifying User-Agent. The contact part comes from config only."""
        base = f"jobhunt-personal/{__version__} (personal job discovery"
        contact = self.user_agent_contact.strip()
        return f"{base}; {contact})" if contact else f"{base})"

    def resolve_path(self, relative: str) -> Path:
        path = Path(relative)
        if path.is_absolute():
            return path
        if self.data_dir is not None:
            parts = path.parts
            if parts and parts[0] == "data":
                return self.data_dir.joinpath(*parts[1:])
        return self.project_root / path

    @property
    def data_directory(self) -> Path:
        return self.data_dir if self.data_dir is not None else self.project_root / "data"

    @property
    def database_file(self) -> Path:
        return self.resolve_path(self.database_path)

    @property
    def cache_directory(self) -> Path:
        return self.resolve_path(self.cache_dir)

    @property
    def backup_directory(self) -> Path:
        return self.database_file.parent / "backups"

    @property
    def migrations_directory(self) -> Path:
        return self.resolve_path(self.migrations_dir)

    @property
    def prefilter_include_keywords(self) -> tuple[str, ...]:
        """Include list = every keyword of every target field + the explicit extras."""
        collected: list[str] = []
        for keywords in self.targets.fields.values():
            collected.extend(keywords)
        collected.extend(self.prefilter.extra_include_title_keywords)
        return tuple(dict.fromkeys(collected))  # de-duplicate, keep order

    def source_enabled(self, name: str) -> bool:
        settings = self.sources.get(name)
        return settings.enabled if settings else False


def load_config(
    path: str | Path | None = None,
    *,
    use_local: bool = True,
    use_settings: bool | None = None,
    data_dir: str | Path | None = None,
) -> Config:
    """Load config.yaml with precedence:
    config.yaml (committed defaults) < config.local.yaml (hand-written local overrides) < data/settings.yaml (UI-written settings).
    
    Raises ConfigError on a missing file, bad YAML or unknown keys.
    """
    effective_settings = use_local if use_settings is None else use_settings
    effective_data_dir = None
    env_data_dir = data_dir or __import__("os").environ.get("JOBHUNT_DATA_DIR")
    if env_data_dir:
        effective_data_dir = Path(env_data_dir).resolve()

    config_path = Path(path) if path else Path.cwd() / DEFAULT_CONFIG_NAME
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        local_path = config_path.with_name(LOCAL_CONFIG_NAME)
        if use_local and local_path.is_file():  # personal overrides: gitignored, never committed
            local = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict) and isinstance(local, dict):
                data = _deep_merge(data, local)
        # UI settings overrides (data/settings.yaml)
        if effective_data_dir:
            settings_path = effective_data_dir / "settings.yaml"
        else:
            settings_path = config_path.resolve().parent / SETTINGS_CONFIG_NAME
        if effective_settings and settings_path.is_file():
            settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict) and isinstance(settings, dict):
                data = _deep_merge(data, settings)
    except yaml.YAMLError as error:
        raise ConfigError(f"invalid YAML in {config_path}: {error}") from error
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path} must contain a mapping at the top level")
    data["project_root"] = config_path.resolve().parent
    if effective_data_dir:
        data["data_dir"] = effective_data_dir
    return _build(Config, data)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _build(cls: type, data: Any) -> Any:
    """Recursively turn a parsed-YAML mapping into the dataclass `cls`."""
    if hasattr(cls, "from_mapping"):
        return cls.from_mapping(data)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"expected a mapping for {cls.__name__}, got {type(data).__name__}")

    hints = typing.get_type_hints(cls)
    known = {item.name for item in dataclasses.fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"unknown key(s) {sorted(unknown)} in section {cls.__name__}")

    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        kwargs[name] = _convert(hints[name], value)
    return cls(**kwargs)


def _convert(hint: Any, value: Any) -> Any:
    origin = typing.get_origin(hint)
    arguments = typing.get_args(hint)

    # Optional[...] / X | None  ->  unwrap
    if origin in (typing.Union, types.UnionType):
        non_none = [item for item in arguments if item is not type(None)]
        if value is None or not non_none:
            return None
        return _convert(non_none[0], value)
    if dataclasses.is_dataclass(hint):
        return _build(hint, value)
    if origin is tuple:
        item_type = arguments[0] if arguments else Any
        return tuple(_convert(item_type, item) for item in (value or ()))
    if origin is dict:
        value_type = arguments[1] if len(arguments) == 2 else Any
        return {key: _convert(value_type, item) for key, item in (value or {}).items()}
    if hint is Path:
        return Path(value)
    if hint is str and value is not None and not isinstance(value, str):
        return str(value)               # YAML reads 99.9 / 06:30 as numbers; these fields are text
    return value
