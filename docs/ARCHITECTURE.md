# Architecture

Generated from the codebase, migrations (`001` through `026`), and configuration specs up to Brief v14. File and line references are to the current commit; symbol and module names are stable.

---

## 1. Module Map (`src/jobhunt/`)

| Package / Module | Responsibility |
| --- | --- |
| `cli.py`, `__main__.py` | CLI entry point (`jobhunt <command>`, `python -m jobhunt <command>`). Dispatches to dedicated command handlers. |
| `config.py` | Strongly typed frozen dataclasses for every section of `config.yaml`. Handles layer merging (`config.local.yaml`, `data/settings.yaml`) and unknown key rejection. |
| `db.py` | SQLite connection management (WAL mode, foreign keys, busy timeouts), Flyway-style checksummed migrations (`apply_migrations`), and verified database backups. |
| `doctor.py` | System diagnostics and health checking (`jobhunt doctor`, `/api/health/doctor`). Verifies Python, Node, OS, disk, ports, Tectonic, Ollama, GPU, keys, and Gmail. |
| `platform/` | Cross-platform compatibility adapters: `paths.py` (executables, paths), `process.py` (detached processes), `locking.py` (file locking across Windows and Unix). |
| `models.py`, `normalize.py`, `classify.py`, `locations.py` | Normalized job schema, title/employment classification, tech signals, and Indian + global location parsing. |
| `sources/` | ATS platform adapters (`Greenhouse`, `Lever`, `Ashby`, `Workable`, `SmartRecruiters`, `Recruitee`) implementing `base.JobSource`. |
| `sources/feeds/` | Aggregators and feeds (`Remotive`, `RemoteOK`, `WeWorkRemotely`, `Himalayas`, `Jobicy`, `Arbeitnow`, `Adzuna`, `Hacker News`, `Workday`, `Amazon`, etc.). |
| `enterprise/` | Large employer career platform detection, polite fetching (robots.txt, host rate limits), and registry. |
| `discovery/` | Discovery of new company career boards via seed lists, Common Crawl indexes, OpenRoles, and snowball URL crawling. |
| `alerts/` | Read-only Gmail job-alert ingestion: per-platform URL and body parsing, token store, and processed email tracking. |
| `runner.py`, `state.py`, `http.py`, `lifecycle.py` | Ingestion loop, per-source state tracking, polite HTTP client (backoff, retry), and job open/closed lifecycle transitions. |
| `dedupe.py` | Deduplication of job postings using canonical URLs and cryptographic content fingerprints across sources. |
| `freshness.py`, `liveness.py`, `source_reports.py` | Age band derivation, Auto-hidden state management, and lightweight HTTP liveness probing of shortlisted jobs. |
| `decisions.py` | Job application decisions (`Saved`, `Applied`, `Not Applying`) with two-way sync, reason recording, and full Undo support. |
| `matching/` | Relevance gate, seniority rules, location policy, vector embeddings, ranker, and LLM triage. Contains: `triage.py` (judge, quote verification), `triage_cascade.py` (cascade decision tree), `triage_local_judge.py` (local Ollama judge), `triage_audit.py` (label voting and metrics), `budget.py` (cost guards and forecasting). |
| `resumes/` | Multi-template resume system (`templates.py`): Jake's Resume, Awesome-CV, Moderncv, Deedy, AltaCV, Simple-Itemize. Enforces formatting and single-page constraints. |
| `tailoring/` | Resume tailoring engine: LaTeX parsing and AST modification, fact extraction, ATS keyword weighting, single-page budgeting, and compilation via Tectonic. |
| `writing/` | AI-assisted cover letter and question answering: staged pipeline (plan, draft, edit), fact-checking validator, and knock-out scanner. |
| `schedule/` | Modular automation scheduling: `definitions.py` (task specs, triggers), `morning_check.py` (daily system report), and backends: `backends/windows.py`, `backends/macos.py`, `backends/in_app.py`. |
| `settings/` | Multi-layer settings and secret management (`secrets.py`, `storage.py`). Safe mutations persist to `data/settings.yaml`. |
| `api/` | FastAPI web application and routes: `routes_jobs`, `routes_today`, `routes_resumes`, `routes_health`, `routes_setup`, `routes_settings`, `routes_audit`, etc. |
| `web/` (root) | React 19 + TypeScript + Vite SPA frontend: Today, Jobs, Tracker, Resumes, Triage Audit, Settings, Setup Wizard, Health. |

---

## 2. End-to-End Data Flow

```
Sources & Feeds ──ingest──> normalise ──> dedupe ──> jobs (+ job_sightings)
      │                                                     │
      │ lifecycle (missed runs close jobs)                  ▼
      │                                          relevance-run  ──> job_relevance (target | maybe | excluded)
      │                                                     │
      │   freshness-run (age bands, Auto-hidden)            ▼
      │                                   embed (Ollama or Gemini) ──> vector similarity to candidate profile
      │                                                     ▼
      │             local ranker (numpy)  +  LLM triage (Qwen 7B first + Gemini audit)
      │                                                     ▼
      │                                    final_score ──> Today Feed (>= 75) / Jobs List
      ▼                                                     │
liveness-check (before shortlist scoring)                   ▼
                                           tailor / cover letter (explicit click) ──> tracker (Kanban board)
```

1. **Ingestion (`ingest`):** Fetches listings from ATS APIs, feeds, and Gmail alerts. Prefilters by title keywords, normalizes, deduplicates, and tracks sightings.
2. **Relevance Gate (`relevance-run`):** Deterministic evaluation using target graduation year, excluded tech stacks, degree rules, and location fit. Never deletes postings.
3. **Embeddings (`embed`):** Embeds job text and profiles using Ollama (`mxbai-embed-large`), Gemini, or `none`. When `provider: none`, weights automatically renormalize.
4. **Local Ranker & LLM Triage (`triage-llm-run`, `rank`):** Evaluates postings using local Ollama model (`qwen2.5:7b-instruct`) with strict evidence quote verification; Gemini audits high-stakes edge cases.
5. **Scoring (`score`):** Computes weighted blend of fit, similarity, location fit, seniority adjustments, and approved learned rule penalties.
6. **Tailoring & Writing (Click Only):** Single-page resume tailoring and cover letter drafting execute strictly upon user interaction, drawing facts exclusively from confirmed profile data.

---

## 3. CLI Commands Reference

| Command | Category | Description |
| --- | --- | --- |
| `init-db` | Database | Initialize SQLite schema and apply pending migrations (creates verified backup first). |
| `upgrade` | Maintenance | Safe one-command update: backs up DB, applies migrations, syncs lockfile, rebuilds web UI. |
| `doctor` | Diagnostics | Runs comprehensive environment checks (`--online` tests live API key reachability). |
| `serve` | Web Server | Starts local FastAPI server and serves the compiled React frontend on `127.0.0.1:8000`. |
| `ingest` | Pipeline | Ingests jobs from all configured ATS sources and enabled feeds. |
| `ingest-email` | Pipeline | Ingests job alert emails from Gmail using read-only OAuth credentials. |
| `relevance-run` | Pipeline | Evaluates deterministic relevance gate and seniority rules for all unjudged jobs. |
| `freshness-run` | Pipeline | Categorizes age bands and auto-hides stale postings past expiration limits. |
| `liveness-check` | Pipeline | Performs polite HTTP checks on shortlisted jobs to confirm they remain open. |
| `embed` | Matching | Generates vector embeddings for profiles and candidate jobs. |
| `rank` | Matching | Computes local ridge regression rank scores across target postings. |
| `match` | Pipeline | Nightly pipeline: relevance, embeddings, local ranker, and shortlist triage. |
| `morning-check` | Operations | One-screen morning summary: task runs, budget forecast, Ollama, triage queue, errors. |
| `schedule-install` | Automation | Installs automated tasks into the active backend (`--yes` required). Skips unconfigured Gmail. |
| `schedule-remove` | Automation | Removes automated tasks from the active scheduler backend. |
| `schedule-status` | Automation | Displays installed tasks, last run timestamps, exit status, and next scheduled trigger. |
| `triage-llm-run` | Triage | Runs local LLM triage with strict evidence verification over visible relevant jobs. |
| `triage-audit` | Triage | Prints agreement metrics between local LLM, Gemini, and human audit labels. |
| `golden-snapshot` | Quality | Generates a deterministic regression snapshot of database state and scores. |
| `golden-compare` | Quality | Compares two golden snapshots to guarantee zero unintended drift. |

---

## 4. API Endpoints (`/api/*`)

| Path | Method | Purpose |
| --- | --- | --- |
| `/api/setup/state` | GET / POST | Setup wizard progress state, navigation, and completion status. |
| `/api/setup/doctor` | GET | Runs system doctor diagnostics for the setup wizard. |
| `/api/setup/budget` | POST | Configures preset (`free` / `paid`) or custom budget limits. |
| `/api/setup/resume/detect` | POST | Detects LaTeX template family from uploaded `.tex` content. |
| `/api/setup/finish` | POST | Completes wizard and kicks off 15-minute background ingestion. |
| `/api/settings` | GET / POST | Retrieves masked secrets, active settings, and persists overrides. |
| `/api/settings/secrets` | POST | Saves Gemini API key to git-ignored `.secrets/` directory. |
| `/api/settings/scheduler` | POST | Configures scheduler backend and execution times. |
| `/api/jobs` | GET | Paged job search with URL-synced facets, filters, search, and sorting. |
| `/api/jobs/{id}/decision/*` | POST | Sets job decision: `save`, `applied`, `not-applying`, or `undo`. |
| `/api/resumes` | GET | Lists all generated resume versions, lifecycle status, and metrics. |
| `/api/jobs/{id}/tailor` | POST | Tailors a 1-page LaTeX resume against job keywords using confirmed facts. |
| `/api/tailored/{id}/pdf` | GET | Streams compiled 1-page tailored PDF directly to the browser. |
| `/api/audit/labels` | GET / POST | Records human audit verdict votes (`agree`, `disagree`, `skipped`). |
| `/api/health` | GET | Health dashboard: pipeline counts, scheduler status, budget forecast, doctor checks. |

---

## 5. Database Schema & Migrations (`migrations/001_*.sql` to `migrations/026_*.sql`)

Database migrations are strictly additive and versioned. A verified pre-migration backup is created automatically before any pending migration executes.

| Migration | Name | Tables / Columns Added |
| --- | --- | --- |
| `001` | `initial` | `companies`, `jobs`, `job_sightings`, `ingestion_runs`, `source_state`, `processed_emails`. |
| `002` | `search_and_listing_indexes` | `jobs_fts` (SQLite FTS5 full-text search) and compound performance indexes. |
| `003` | `remote_eligibility` | `workplace_type`, `remote_scope`, `remote_regions`, `remote_eligible_india`, `work_auth_required`. |
| `004` | `relevance_and_profiles` | `job_relevance` (gate verdicts, fit scores, components), `profiles` (candidate profiles). |
| `005` | `embeddings_and_usage` | `profile_embeddings`, `llm_usage` (token and USD spend ledger). |
| `006` | `feedback` | `job_feedback` (user decision history and dismiss tracking). |
| `007` | `enterprise_companies` | Registry mapping large enterprise employers to career platforms. |
| `008` | `seniority` | `seniority_level`, `seniority_evidence` on `job_relevance`. |
| `009` | `local_tiers` | `local_rank_score`, `local_rank_version`, `local_llm_*` columns. |
| `010` | `local_runs` | `local_runs` (GPU stage tracking), `llm_batches` (Gemini batch processing). |
| `011` | `location_fit` | `location_fit`, `location_reason`, `location_matched`. |
| `012` | `tailored_resumes` | Versioned tailored resumes, cache hashes, compile statuses, cost records. |
| `013` | `automation_today_writing` | `scheduled_runs`, `app_state`, `job_opens`, `generated_texts`. |
| `014` | `resumes_page` | `resume_number`, `lifecycle`, `change_summary_json`, `sent_at`, `archived`. |
| `015` | `eligibility_decisions` | `eligibility_json`, `learned_json`, `job_decisions`, `rule_decisions`. |
| `016` | `decision_undo` | `job_decisions.prior_json` storing prior state for instant decision rollback. |
| `017` | `freshness_liveness_company_rules` | `job_auto_hidden`, `job_freshness`, `job_liveness`. |
| `018` | `writing_stages_edits` | Multi-stage cover letter generation: `stages_json`, `plan_json`, `quality_json`. |
| `019` | `llm_triage` | `job_llm_verdicts` storing raw and quote-verified LLM verdicts. |
| `020` | `triage_cascade` | `job_relevance.triage_cascade_json` supporting multi-tier evaluation. |
| `021` | `local_llm_first_triage` | Dedicated fields for local Ollama judge output and agreement tracking. |
| `022` | `triage_strict_verification` | Verbatim quote verification audit fields and dropped claim logging. |
| `023` | `job_audit_labels` | `job_audit_labels` table recording human verification votes on LLM verdicts. |
| `024` | `person_agnostic_profiles` | Generalized schema decoupling profile data from specific candidate names. |
| `025` | `setup_state` | `setup_state` key-value store persisting onboarding wizard progress. |
| `026` | `embeddings_provider` | Metadata columns tracking active vector provider, model, and dimensions. |

---

## 6. Scoring Formula & Similarity Renormalization

Composite match scoring blends multiple independent signals into an explainable 0–100 integer score:

$$
\text{final\_score} = \text{clamp}(w_{\text{fit}} \cdot \text{fit} + w_{\text{sim}} \cdot \text{percentile} + w_{\text{loc}} \cdot \text{location} + \text{adjustments} - \text{penalties}, 0, 100)
$$

### Weight Renormalization
- Default weights (with active vector embeddings):
  $$w_{\text{fit}} = 0.55, \quad w_{\text{sim}} = 0.25, \quad w_{\text{loc}} = 0.20$$
- Offline mode (`embeddings.provider: none`):
  Vector similarity is omitted. Remaining weights renormalize to preserve the 100-point scale:
  $$w_{\text{fit}} = \frac{0.55}{0.55 + 0.20} \approx 0.733, \quad w_{\text{loc}} = \frac{0.20}{0.55 + 0.20} \approx 0.267$$

### Adjustments & Caps
- **Location Score:** India / Remote open to India = 100, Unclear remote = 40, Abroad = 25, Wrong location = 0.
- **Role Family Adjustment:** Priority track boosts or off-domain deductions ($\pm 10$ to $20$ pts).
- **Seniority & Years Penalty:** $-6$ pts per required year above baseline (capped at $-12$ pts).
- **Learned Rules:** Deductions mined from user's "Not Applying" feedback (capped at $-20$ pts).
- **Eligibility Cap:** Any required unfulfilled degree or citizenship flag caps final score at 50.
- **Today Shortlist Threshold:** Jobs with `final_score >= 75` and confirmed open liveness surface on the Today feed.

---

## 7. Budget Guard & Cost Forecasting Model

All paid model interactions are guarded by `BudgetGuard` (`src/jobhunt/matching/budget.py`), ensuring spend never exceeds configured limits.

### Counters & Enforcement
- `budget.daily_calls` (default 60–200): Enforced per UTC day for automated pipeline calls (`score`, `triage`).
- `budget.interactive_daily_calls` (default 40): Enforced for user-triggered clicks (`tailor`, `cover-letter`).
- `budget.monthly_usd` (default $5.00–$8.00): Hard monthly spend ceiling across all operations.

### Consistent 7-Day Forecasting
To prevent an incomplete day (such as a morning check at 06:00) from artificially diluting daily spend averages, `compute_budget_forecast` evaluates:
- The 7 completed days: $[\text{today} - 7, \text{today} - 1]$
- The trailing 7 days including today: $[\text{today} - 6, \text{today}]$
Selecting $\max(\text{spend}_{\text{completed}}, \text{spend}_{\text{trailing}})$ ensures the projection accurately reflects full 7-day run rates.
