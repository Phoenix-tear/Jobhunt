// Mirrors src/jobhunt/api/schemas.py (the Pydantic response models).

export const STAGES = ['saved', 'applied', 'oa_interview', 'offer', 'rejected'] as const
export type Stage = (typeof STAGES)[number]

export const STAGE_LABELS: Record<Stage, string> = {
  saved: 'Saved',
  applied: 'Applied',
  oa_interview: 'OA / Interview',
  offer: 'Offer',
  rejected: 'Rejected',
}

export const EXPERIENCE_LEVELS = ['intern', 'new_grad', 'entry', 'mid', 'senior', 'unknown'] as const
export type ExperienceLevel = (typeof EXPERIENCE_LEVELS)[number]

export const EMPLOYMENT_TYPES = ['full_time', 'internship', 'contract', 'part_time'] as const
export type EmploymentType = (typeof EMPLOYMENT_TYPES)[number]

export type LocationFit =
  | 'india'
  | 'remote_open_to_india'
  | 'remote_unclear'
  | 'abroad_unknown_workplace'
  | 'wrong_location'
  | 'unknown'
export type SeniorityLevel = 'junior' | 'unspecified' | 'caution' | 'mid' | 'senior' | 'executive'
export type GateStatus = 'target' | 'maybe' | 'excluded'
export type WorkplaceType = 'remote' | 'hybrid' | 'onsite' | 'unknown'

export const FEEDBACK_REASONS = ['wrong_role', 'wrong_stack', 'too_senior', 'wrong_location', 'company', 'other'] as const
export type FeedbackReason = (typeof FEEDBACK_REASONS)[number]
export const FEEDBACK_REASON_LABELS: Record<FeedbackReason, string> = {
  wrong_role: 'Wrong role',
  wrong_stack: 'Wrong stack',
  too_senior: 'Too senior',
  wrong_location: 'Wrong location',
  company: 'Not this company',
  other: 'Other',
}

export interface FeedbackResult {
  job_id: number
  feedback: 'dismiss' | 'like' | null
  reason: string | null
}

export interface MatchDetail {
  final_score: number | null
  llm_score: number | null
  reason: string | null
  matched_skills: string[]
  missing_skills: string[]
  red_flags: string[]
  recommended_profile: string | null
  similarity_ai: number | null
  similarity_fullstack: number | null
  similarity_percentile: number | null
  components: Record<string, unknown>
  gate_status: GateStatus | null
  gate_rule: string | null
  exclusion_reason: string | null
  required_years: number | null
  remote_evidence: string | null
  remote_regions: string[]
  remote_source: string | null
}

export type SortKey =
  | 'match'
  | 'newest_posted'
  | 'newest_seen'
  | 'oldest'
  | 'company'
  | 'title'
  | 'relevance'
  | 'newest'

/** Category control: which jobs by gate verdict. Independent of the sort, the view tab, location and freshness. */
export type Category = 'relevant' | 'target' | 'maybe' | 'all' | 'filtered_out'
export type FreshnessBand = 'fresh' | 'aging' | 'stale' | 'unknown'

export interface JobSummary {
  id: number
  title: string
  company_name: string
  location: string | null
  city: string | null
  country: string | null
  is_remote: boolean | null
  experience_level: ExperienceLevel
  employment_type: EmploymentType
  employment_type_raw: string
  posted_at: string | null
  first_seen_at: string
  listed_at: string
  sources: string[]
  needs_description: boolean
  status: 'open' | 'closed'
  saved: boolean
  application_id: number | null
  stage: Stage | null
  match_score: number | null
  gate_status: GateStatus | null
  role_family: string | null
  best_profile: 'ai' | 'fullstack' | null
  llm_scored: boolean
  workplace_type: WorkplaceType
  remote_scope: 'worldwide' | 'regional' | 'country_restricted' | 'unknown'
  remote_eligible_india: boolean | null
  work_auth_required: boolean
  has_caution_signal?: boolean
  decision?: 'saved' | 'applied' | 'not_applying' | null
  decision_reason?: string | null
  likely_ineligible?: string | null
  applied_company_warning?: string | null
  learned_rules?: { id: string | null; phrase: string | null; tooltip: string | null }[]
  feedback: 'dismiss' | 'like' | null
  exclusion_reason: string | null
  seniority_level: SeniorityLevel | null
  seniority_evidence: string | null
  location_fit: LocationFit | null
  location_reason: string | null
  location_matched: string | null
  freshness?: FreshnessBand
  age_days?: number | null
  auto_hidden?: 'auto_stale' | 'closed_checked' | null
}

export interface JobPage {
  total: number
  page: number
  page_size: number
  sort: SortKey
  items: JobSummary[]
}

export interface Sighting {
  source: string
  source_job_id: string
  url: string | null
  attribution_text: string | null
  first_seen_at: string
  last_seen_at: string
}

export interface Company {
  id: number
  name: string
  ats_type: string
  slug: string
  status: string
}

export interface Application {
  id: number
  job_id: number
  stage: Stage
  applied_at: string | null
  notes: string | null
  follow_up_at: string | null
  created_at: string
  updated_at: string
  tailored_resume_id?: number | null
  resume_number?: number | null
  contact_name?: string | null
  contact_email?: string | null
  contact_phone?: string | null
}

export type KnockoutRisk = 'possible_knockout' | 'ok_by_your_value' | 'needs_confirmation'

export interface EligibilityScreen {
  kind: string
  label: string
  risk: KnockoutRisk | null
}

export interface KnockoutItem {
  question: string
  kind: string
  risk: KnockoutRisk
  your_value: string | number | boolean | null
  needs_confirmation: boolean
  note: string
}

export interface PostingSignal {
  id: string
  level: 'info' | 'caution'
  text: string
}

export interface TriageQuote {
  field: string
  value: string
  quote: string
}

export interface TriageVerdict {
  model: string
  rubric_version: string
  created_at: string
  level: string
  required_years_min: number | null
  suitable_for_new_grad: string
  india_eligible: string
  location_scope: string
  eligibility_blockers: string[]
  role_family: string[]
  confidence: string
  quotes: TriageQuote[]
  unverified_fields: string[]
  used_by: string
  used_text: string
  would_exclude: boolean
  human_label: string | null
  human_reason: string | null
  source?: string
  acted_or_advised?: string
  acted?: boolean
  strength_reason?: string | null
  company_precedence_applied?: boolean
  models_disagree?: boolean
}

export interface CascadeDecision {
  head: string
  tier: number
  action: string
  confidence: number | null
  evidence: string | null
  mode: string
  model: string | null
  decided_at: string
}

export interface TriageStudent {
  p_senior?: number | null
  p_not_india?: number | null
  rules?: { rule: string; precision?: number; support?: number }[]
  demoted_by?: string | null
  chips?: string[]
}

export interface TriagePanel {
  verdict: TriageVerdict | null
  student: TriageStudent | null
  cascade?: CascadeDecision[]
}

export interface HeadHealth {
  mode: string
  n_pos: number
  n_neg: number
  precision: number | null
  coverage: number | null
  auc: number | null
  can_activate: boolean
  needs_hint: string | null
}

export interface ActingRule {
  field: string
  target_class: string
  sample_count: number
  high_conf_count: number
  precision: number
  recall: number
  can_act: boolean
  mode: string
  reason: string
  evaluated_at: string
  ci_lower?: number | null
  ci_upper?: number | null
  rule_desc?: string | null
}

export const VERDICT_WRONG_LABELS = [
  ['wrong_level', 'Level is wrong'],
  ['wrong_years', 'Years are wrong'],
  ['wrong_location', 'Location is wrong'],
  ['other', 'Something else'],
] as const

export interface TriageHealth {
  enabled: boolean
  rubric_version: string
  model: string
  verdicts: number
  by_level: Record<string, number>
  verdict_counts_by_field?: Record<string, Record<string, number>>
  leak_rate_last_100: number | null
  judged_in_window: number
  leaked_in_window: number
  hallucinated_evidence_rate: number | null
  not_verbatim_rate?: number | null
  irrelevant_evidence_rate?: number | null
  hallucinated_fields: number
  not_verbatim_fields?: number
  irrelevant_fields?: number
  claimed_fields: number
  demoted_week_llm: number
  demoted_week_classifier: number
  demoted_week_rules: number
  verdict_wrong_marked: number
  verdicts_that_acted: number
  agreement: number | null
  daily_calls_cap: number
  calls_today: number
  tier_mix_7d?: Record<string, number>
  gemini_calls_saved_7d?: number
  heads_status?: Record<string, HeadHealth>
  last_trained_at?: string | null
  last_train_metrics?: Record<string, unknown>
  label_counts?: Record<string, number>
  shadow_summary?: Record<string, unknown>
  coverage_visible_relevant_judged?: number
  coverage_visible_relevant_total?: number
  coverage_visible_relevant_pct?: number
  coverage_gemini?: number
  coverage_local_only?: number
  coverage_none?: number
  queue_length?: number
  last_run_duration_seconds?: number | null
  last_jobhunt_triage_run?: string | null
  gemini_confirmations_used?: number
  models_disagree_count?: number
  acting_rules?: ActingRule[]
  audit_progress?: string
  audit_imported?: number
  audit_total?: number
}

export interface JobDetail extends JobSummary {
  match: MatchDetail | null
  triage?: TriagePanel | null
  signals?: PostingSignal[]
  eligibility?: EligibilityFlag[]
  why_match?: WhyMatch
  eligibility_screens?: EligibilityScreen[]
  description_text: string | null
  apply_url: string | null
  sightings: Sighting[]
  company: Company | null
  application: Application | null
  salary_min: number | null
  salary_max: number | null
  salary_currency: string | null
  sponsorship_note: string | null
  graduation_year_hint: number | null
  last_seen_at: string
  closed_at: string | null
  closed_reason?: string | null
  liveness?: Liveness | null
  remember_level_word?: string | null
}

export interface Liveness {
  result: 'open' | 'closed' | 'uncertain'
  detail: string | null
  http_status: number | null
  method: string | null
  trigger?: string | null
  checked_at: string
  action?: 'closed' | 'restored' | null
}

export interface FacetCount {
  value: string
  count: number
}

export interface Facets {
  total_open: number
  levels: FacetCount[]
  employment_types: FacetCount[]
  countries: FacetCount[]
  sources: FacetCount[]
  remote: number
  needs_description: number
  new_24h: number
  generated_at: string
  scored: number
  relevant: number
  filtered_out: number
  workplace_types: FacetCount[]
  remote_open_to_india: number
  gate_rules: FacetCount[]
  location_fits: FacetCount[]
  show_other_locations_default: boolean
  auto_hidden?: number
  freshness_bands?: FacetCount[]
  category_counts?: FacetCount[]
}

export interface BoardCard {
  application: Application
  title: string
  company_name: string
  location: string | null
  country: string | null
  apply_url: string | null
  job_status: 'open' | 'closed'
}

export interface BoardColumn {
  stage: Stage
  count: number
  cards: BoardCard[]
}

export interface Board {
  total: number
  columns: BoardColumn[]
}

export interface ManualJobInput {
  title: string
  company: string
  apply_url: string
  location?: string
  description?: string
  fetch_from_ats: boolean
}

export interface ManualJobResult {
  created: boolean
  job: JobDetail
  ats: { attempted: boolean; status: string; detail: string | null }
}

export interface RunSummary {
  started_at: string
  finished_at: string | null
  companies_checked: number
  jobs_seen: number
  jobs_new: number
  jobs_closed: number
  jobs_skipped_by_prefilter: number
  error_count: number
  first_error: string | null
}

export interface SourceHealth {
  source: string
  kind: 'ats' | 'feed' | 'email'
  enabled: boolean
  last_run_at: string | null
  last_success_at: string | null
  age_hours: number | null
  stale: boolean
  never_run: boolean
  fetch_count_today: number
  last_run: RunSummary | null
}

export interface LocalStage {
  stage: string
  last_run_at: string | null
  seconds: number | null
  items: number | null
  model: string | null
  status: string | null
  detail: string | null
}

export interface CostProjection {
  calls_per_day: number
  avg_input_tokens: number
  avg_output_tokens: number
  model: string
  usd_per_job: number
  usd_per_month: number
  usd_per_month_after_price_change: number
  price_change_on: string | null
  based_on_calls: number
}

export interface BudgetForecast {
  month_to_date_usd: number
  avg_daily_usd_7d: number
  projected_month_usd: number
  cap_hit_date: string | null
  warning: boolean
  cap_usd: number
}

export interface MatchingHealth {
  local_stages: LocalStage[]
  projection: CostProjection | null
  budget_forecast?: BudgetForecast | null
  usage: {
    calls_today: number
    daily_call_cap: number
    interactive_calls_today?: number
    interactive_call_cap?: number
    usd_today: number
    usd_month: number
    monthly_usd_cap: number
    embedded_texts_today: number
    daily_embedding_cap: number
  }
  embedding_coverage: number
  embedded: number
  embeddable: number
  scored_jobs: number
  last_relevance_run_at: string | null
  last_llm_call_at: string | null
  profiles: string[]
}

export interface ScheduledTask {
  task: string
  name: string
  description: string
  schedule: string
  heavy: boolean
  last_run_at: string | null
  last_status: 'running' | 'ok' | 'failed' | 'skipped' | 'timeout' | null
  last_duration_seconds: number | null
  last_detail: string | null
  last_ok_at: string | null
  next_run_at: string | null
  missed_runs: number
  missed_warning: boolean
}

export interface ScheduleHealth {
  backend?: string
  tasks: ScheduledTask[]
  missed_runs?: Array<{ task: string; name: string; missed_runs: number }>
  gmail_auth_expired: boolean
  gmail_auth_reason: string | null
  gmail_auth_since: string | null
  gmail_reauth_command: string
}

export interface DoctorCheck {
  id: string
  name: string
  status: 'pass' | 'warn' | 'fail'
  message: string
  hint: string | null
  details?: Record<string, unknown>
}

export interface DoctorResult {
  status: 'pass' | 'warn' | 'fail'
  checks: DoctorCheck[]
}

export interface SourceReport {
  source: string
  shown: number
  reported_closed: number
  reported_too_old: number
  decided: number
  closed_share: number
  auto_hidden: number
  current_stale_after_days: number
  proposed_stale_after_days: number | null
  proposal: string | null
}

export interface FreshnessHealth {
  enabled: boolean
  fresh_days: number
  stale_after_days: number
  auto_hidden_total: number
  auto_hidden_by_source: FacetCount[]
  auto_hidden_by_reason: FacetCount[]
  bands: FacetCount[]
  last_check: string | null
  source_reports: SourceReport[]
}

export interface CompanyRule {
  index: number
  entry_indexes: number[]
  company: string
  aliases: string[]
  allow_title_patterns: string[]
  deny_title_patterns: string[]
  allow_level_codes: string[]
  deny_level_codes: string[]
  deny_text_patterns: string[]
  note: string
}

export interface CompanyRuleList {
  path: string
  rules: CompanyRule[]
}

export interface CompanyRulePreview {
  company: string
  pattern: string
  text: string
  path: string
  already_denied: boolean
}

export interface ReadinessItem {
  id: string
  label: string
  passed: boolean
  link: string
  guidance: string
}

export interface ReadinessChecklist {
  key_set: ReadinessItem
  resume_validated: ReadinessItem
  facts_ticked: ReadinessItem
  profile_complete: ReadinessItem
  sources_chosen: ReadinessItem
  first_ingest_done: ReadinessItem
  first_match_done: ReadinessItem
  ready: boolean
}

export interface SetupState {
  completed: boolean
  step: string
  completed_at: string | null
  schema_version: number
  demo_mode?: boolean
}

export interface Health {
  freshness?: FreshnessHealth | null
  triage?: TriageHealth | null
  schedule?: ScheduleHealth | null
  matching: MatchingHealth | null
  budget_forecast?: BudgetForecast | null
  last_morning_check?: string | null
  readiness?: ReadinessChecklist | null
  generated_at: string
  jobs_open: number
  jobs_closed: number
  companies_active: number
  companies_unverified: number
  companies_inactive: number
  applications_by_stage: Record<string, number>
  sources: SourceHealth[]
  gmail: {
    credentials_file_exists: boolean
    token_file_exists: boolean
    scopes: string[]
    read_only_scope: boolean
    account_configured: string | null
  }
  user_agent_contact_set: boolean
  schema_version: number
  warnings: { code: string; message: string }[]
}

export interface TailorViolation {
  rule: string
  offending: string
  source: string
}

export interface TailorDiffItem {
  kind: string
  entry?: string
  old?: string
  new?: string
}

export interface TailoredResume {
  id: number
  job_id: number
  base_profile: 'ai' | 'fullstack'
  mode: 'advised' | 'deterministic' | 'select' | 'rephrase'
  status: 'ok' | 'fallback' | 'failed'
  model: string | null
  input_tokens: number
  output_tokens: number
  cost_usd: number
  created_at: string
  pages: number
  message: string | null
  approved: boolean
  validation: { ok?: boolean; violations?: TailorViolation[] }
  dropped: string[]
  has_pdf: boolean
  has_tex: boolean
  download_name: string
  diff: TailorDiffItem[]
  info: TailorInfo
}

export interface TailorInfo {
  recipe?: string
  summary_variant?: string
  fill?: number
  keywords?: string[]
  matched?: { keyword: string; fact: string; fact_text: string }[]
  missing?: string[]
  notes?: string[]
  bullets_base?: number
  bullets_output?: number
}

export interface TailoringStatus {
  engine: string | null
  runs: number
  runs_today: number
  usd_today: number
  usd_month: number
  validator_rejections: number
  validator_failure_rate: number
  failed: number
  llm_runs: number
}

// --- Today ---------------------------------------------------------------------------------------

export interface TodayJob extends JobSummary {
  apply_url: string | null
  opened_at: string | null
  closed_at: string | null
  deadline_at: string | null
  tailored_resume_id: number | null
  approved_pdf: boolean
  applied_at: string | null
  follow_up_at: string | null
  days_since_change: number | null
  next_date: string | null
  contact_name: string | null
}

export interface SourceCount {
  source: string
  new_today: number
  relevant_today: number
  open_total: number
}

export interface TodayView {
  generated_at: string
  since: string
  new_total: number
  new_jobs: TodayJob[]
  top: TodayJob[]
  closing_soon: TodayJob[]
  disappeared: TodayJob[]
  follow_ups: TodayJob[]
  interviews: TodayJob[]
  approved_not_applied: TodayJob[]
  week?: { applied: number; saved: number; not_applying: number; follow_ups_due: number }
  sources: SourceCount[]
}

// --- cover letters and answers -------------------------------------------------------------------

export interface WritingViolation {
  rule: string
  offending: string
  source: string
}

export interface WritingStage {
  stage: 'plan' | 'write' | 'edit'
  model: string
  input_tokens: number
  output_tokens: number
  cost_usd: number
  accepted: boolean
  note: string
  attempt: number
}

export interface WritingQuality {
  ok?: boolean
  warnings?: { rule: string; detail: string }[]
  stats?: { words?: number; sentences?: number; average_sentence_words?: number; longest_sentence_words?: number }
}

export interface WritingText {
  id: number | null
  job_id: number
  kind: 'cover_letter' | 'answer'
  question: string | null
  text: string
  cited: string[]
  validation: { ok?: boolean; violations?: WritingViolation[]; words?: number; note?: string }
  words: number
  source: 'model' | 'outline' | 'template' | 'refusal' | 'user_edit'
  model: string
  input_tokens: number
  output_tokens: number
  cost_usd: number
  message: string
  cached: boolean
  version: number
  created_at: string
  rejected_violations: WritingViolation[]
  pipeline: string
  stages: WritingStage[]
  plan: { source?: string; job_angle?: string; blocks?: { intent: string; point: string; fact_ids: string[] }[] }
  quality: WritingQuality
  parent_id: number | null
  used_for_export: boolean
  calls: number
}

export interface WritingEstimate {
  calls: number
  max_calls: number
  estimated_usd: number
  max_usd: number
  interactive_used: number
  interactive_limit: number
  interactive_remaining: number
  blocked_reason: string
  model: string
  plan_model: string
  pipeline: string
  voice_samples: boolean
}

// --- Resumes page ---------------------------------------------------------------------------------

export type ResumeLifecycle = 'draft' | 'approved' | 'sent' | 'superseded'

export interface TailoringStrength {
  level: 'Low' | 'Medium' | 'High' | 'Unknown'
  score: number | null
  coverage: number | null
  adaptation: number | null
  parts: Record<string, boolean>
  missing: string[]
  why: string[]
  help: string | null
  text: string
}

export interface SkillGapItem {
  concept: string
  label: string
  jobs: number
  examples: string[]
}

export interface SkillGaps {
  jobs_checked: number
  job_limit: number
  concepts_on_your_facts: number
  items: SkillGapItem[]
  note: string
}

export interface ResumeItem {
  id: number
  resume_number: number | null
  code: string
  version: number | null
  job_id: number
  job_title: string
  company_name: string
  apply_url: string | null
  created_at: string
  base: 'ai' | 'fullstack' | 'backend'
  recipe: string | null
  summary_variant: string | null
  fill_ratio: number | null
  page_count: number | null
  validator_status: 'ok' | 'failed' | null
  advice_source: 'gemini' | 'deterministic'
  model: string
  cost_usd: number
  run_status: 'ok' | 'fallback' | 'failed'
  lifecycle: ResumeLifecycle
  sent_at: string | null
  archived: boolean
  has_pdf: boolean
  file_note: string | null
  application_id: number | null
  application_stage: string | null
  parent_resume_id: number | null
  stale_profile: boolean
  tailoring_strength?: TailoringStrength
}

export interface ResumeDetail extends ResumeItem {
  change_summary: Record<string, unknown>
  change_text: string[]
  diff_vs_previous: string | null
  keywords_matched: string[]
  keywords_missing: string[]
  validation: { ok?: boolean; violations?: { rule: string; offending: string }[] }
  message: string
  dropped: string[]
  has_tex: boolean
  pdf_path: string | null
  download_name: string
  how_made: string
  profile_hash_recorded: boolean
}

export interface ResumeList {
  total: number
  items: ResumeItem[]
  counts: Record<string, number>
}

export interface TailorEstimate {
  job_id: number
  model: string
  estimated_usd: number
  interactive_calls_today: number
  interactive_call_cap: number
  blocked: boolean
  message: string
  existing_versions: number
  will_use_model: boolean
}

// --- v5: eligibility, decisions, learning ----------------------------------------------------------

export interface EligibilityFlag {
  kind: string
  strength: 'required' | 'preferred'
  label: string
  snippet: string
}

export interface WhyMatch {
  fit_reason: string | null
  fit: number | null
  fit_source: string | null
  fit_source_note: string | null
  similarity_ai: number | null
  similarity_fullstack: number | null
  best_profile: string | null
  profile_note: string | null
  formula: string
  components: Record<string, unknown>
}

export type JobView = 'open' | 'saved' | 'applied' | 'not_applying' | 'auto_hidden' | 'all'

export const NOT_APPLYING_REASONS = [
  'ineligible_phd_or_masters', 'ineligible_level', 'ineligible_other', 'wrong_role', 'too_senior', 'wrong_domain_or_stack',
  'wrong_location', 'company_not_interested', 'poor_posting_or_suspicious', 'already_applied_elsewhere',
  'closed_or_unavailable', 'too_old', 'other',
] as const
export type NotApplyingReason = (typeof NOT_APPLYING_REASONS)[number]
export const NOT_APPLYING_LABELS: Record<NotApplyingReason, string> = {
  ineligible_phd_or_masters: 'Ineligible: PhD or Master\'s required',
  ineligible_level: 'Ineligible: grade / level (VP, director, ...)',
  ineligible_other: 'Ineligible: other (clearance, citizenship, graduation year...)',
  wrong_role: 'Wrong role',
  too_senior: 'Too senior',
  wrong_domain_or_stack: 'Wrong domain or stack',
  wrong_location: 'Wrong location',
  company_not_interested: 'Not interested in the company',
  poor_posting_or_suspicious: 'Poor or suspicious posting',
  already_applied_elsewhere: 'Already applied elsewhere',
  closed_or_unavailable: 'Closed or no longer available',
  too_old: 'Too old (posted long ago)',
  other: 'Other',
}
export const APPLY_HOW = ['company_site', 'linkedin', 'referral', 'email', 'other'] as const
export type ApplyHow = (typeof APPLY_HOW)[number]
export const APPLY_HOW_LABELS: Record<ApplyHow, string> = {
  company_site: 'Company site', linkedin: 'LinkedIn', referral: 'Referral', email: 'Email', other: 'Other',
}

export interface DecisionResult {
  job_id: number
  action: string
  reason?: string | null
  application_id?: number | null
  applied_at?: string | null
  follow_up_at?: string | null
  reverted?: string | null
}

export interface LearningProposal {
  key: string
  kind: string
  phrase: string
  count: number
  job_ids: number[]
  kept: number
  reason: string
  precision: number
  examples: string[]
}

export interface LearningView {
  labels: Record<string, number>
  labels_in_last_training: Record<string, number>
  human_labels: number
  ranker: {
    trained_at: string | null
    rows: number | null
    chosen: string | null
    cv_spearman: number | null
    similarity_only_spearman: number | null
    in_use: string | null
    version: string | null
    last_decision: { action: string; reason: string; rows: number; new_spearman?: number | null; old_spearman?: number | null; at: string } | null
  }
  proposals: LearningProposal[]
  rules: { id: string; kind: string; phrase: string; demote: boolean; penalty: number; count: number; job_ids: number[]; created: string; reason: string }[]
  honesty: string
  applies: string
}

export interface AuditItem {
  job_id: number
  company: string
  title: string
  source: string
  category: string
  snippet: string
  gemini_verdict: string
  gemini_quote: string
  local_verdict: string
  local_quote: string
  deterministic_hint?: string | null
  row_order: number
  my_verdict?: string | null
  my_note?: string | null
}

export interface AuditActingRule {
  id?: number
  field: string
  operator: string
  threshold: number
  target_class: string
  weight: number
  sample_size?: number
  precision?: number
  created_at?: string
  acting?: number
}

export interface AuditReviewState {
  items: AuditItem[]
  current_index: number
  total_items: number
  answered_count: number
  gemini_correct: number
  local_correct: number
  both_wrong_count: number
  both_right_count: number
  unsure_count: number
  decisive_count: number
  gemini_accuracy: number
  gemini_ci: [number, number]
  local_accuracy: number
  local_ci: [number, number]
  acting_rules: AuditActingRule[]
}

