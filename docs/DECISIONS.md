# Design Decisions

One short entry per major decision, distilled from working reports and implementation milestones across versions v1 through v14. "Why" reflects the operational evidence or constraint that motivated the design.

---

## v1: Ingestion and the Local Website

* **Public sources only:** ATS board APIs (Greenhouse, Lever, Workable, SmartRecruiters, Ashby), public feeds, curated lists, and user's own mailbox (read-only Gmail). Never scrapes login-walled, bot-protected, or CAPTCHA-gated pages. *Why:* Legal compliance and operational durability for a personal tool; every source is polite with per-host intervals and daily caps.
* **SQLite plus additive, checksummed migrations:** Data is never deleted by a pipeline rule; exclusions record the rule code and reason, remaining reversible by editing config and re-running. *Why:* Complete auditability, data integrity, and confidence that an aggressive rule won't irreversibly erase opportunities. Verified backups are taken prior to every migration.
* **Deduplicate by fingerprint and canonical URL; close jobs only after multiple missed runs:** Includes a `suspicious_drop_ratio` guard against a source returning empty results. *Why:* A temporary network failure or feed glitch must never close hundreds of open jobs.
* **Local-only website on `127.0.0.1`, no login, no remote host option:** *Why:* Single-user personal tool; omitting network exposure eliminates accidental security vulnerabilities.

---

## v1 to v2: Matching and Resume Tailoring

* **Deterministic relevance gate before paid models:** Filtering applies graduation year, excluded tech stacks, degree rules, and location fit before embeddings or LLMs run. Gemini sees only PII-stripped text for the daily shortlist.
* **Weighted composite score, never saturating:** Blends fit (0.55), similarity percentile (0.25), and location (0.20), plus family and seniority adjustments. Every component is stored and surfaced in "Why this match".
* **Resume tailoring never lets a model write resume text:** The model selects recipes, summary variants, and bullet IDs; every output bullet must exist in the user's base resumes, summaries are byte-identical to approved text, and identity blocks are restored from the base. *Why:* "Zero fabrication" is the core promise.
* **Tectonic standalone LaTeX compilation:** Compiled locally without requiring full TeX Live installations; PDFs are generated directly in a sandboxed directory with strict single-page limits.
* **Tailoring is click-only:** Bulk tailoring requires explicit confirmation. *Why:* Eliminates uncontrolled API spend.

---

## v3: Master Profile, Automation, Writing, and Tracker

* **Master profile:** Any confirmed project across base resumes can be included in tailored outputs; recipes enforce narrative ordering.
* **Task automation:** Native scheduling with file locking, rotating redacted logs, and execution recording in `scheduled_runs`. Times are anchored to the provider's daily quota reset (05:30 IST / 00:00 UTC).
* **Grounded cover letters and answers:** Drafts cite ticked facts and pass an automated validator; failure defaults to a clean facts outline or plain refusal rather than hallucinating experience.
* **Tracker additions:** Kanban board with status history, follow-up flags, and CSV export without altering base database schemas.

---

## v4: Local Ranker, Posting Signals, and Data Contract

* **Pure NumPy local ranker:** Ridge regression plus title TF-IDF stored in plain JSON without pickle. *Why:* Eliminates heavyweight dependencies (scipy/scikit-learn) and ensures reliable execution in minimal virtual environments.
* **Posting signals are strictly factual:** Age chips, direct employer site confirmation, and apply link checks never silently mutate match scores.
* **Knock-out scanner uses no model:** Compares requirements against `application_answers.yaml`; sensitive legal, authorization, and salary questions are never drafted.
* **Untrusted data contract (`docs/UNTRUSTED_DATA_CONTRACT.md`):** Job postings and emails are untrusted data, never execution instructions.

---

## v5: Eligibility Flags, Decisions, and Learning Loop

* **Eligibility flags are factual quotes:** Identifies required degrees or clearances. Required flags lower target status to maybe and cap the score at 50, but never exclude outright.
* **Save / Applied / Not Applying decisions with Undo:** Replaces legacy dismiss actions with structured feedback and immediate rollback capability.
* **Conservative learning loop:** Ranker retrains only with 10+ new role-fit labels and rejects retrained models if cross-validation accuracy degrades.

---

## v6: Freshness, Seniority, and Independent Controls

* **Age bands and Auto-Hidden:** Fresh (<= 30 days) and stale (> 45 days; 90 days for internships). Postings past threshold move to Auto-Hidden; user restoration is permanent. Decided or tailored jobs are never auto-hidden.
* **Liveness probing:** Capped daily check verifying shortlisted postings remain active; ambiguous responses (403, 429, timeout) never mark a job closed.
* **Seniority by two rules:** Title classification plus lower-bound required-years parsing; early-career titles bypass years requirements.
* **Independent UI controls:** Category, tab, location, freshness, and sort operate as independent controls.

---

## v7: Writing Quality and Tailoring Depth

* **Staged letter pipeline:** Plans, drafts, and edits in separate steps with deterministic language checks and a facts-diff comparison.
* **Bounded thinking budget:** Restricts Gemini thinking tokens (`writing.thinking_budget: 1024`) to prevent reasoning loops from consuming generation budgets.
* **Selection by relevance over fill:** Prioritizes depth on relevant skills over superficial breadth, enforcing a 92% vertical page floor.
* **Tailoring strength metrics:** Evaluates job coverage (70%) and base variation (30%), explaining exactly what skills are missing via a read-only Skill Gaps report.

---

## v8: Human-Like Letters and Evidence-First Triage

* **Facts outline fallback:** When drafted letters fail validation, an editable facts outline is provided instead of generic boilerplate. Export is refused until verified.
* **Paraphrase validation:** Allows value-preserving paraphrasing and technological terms while copy checks block verbatim prompt echoing.
* **LLM triage as evidence:** Triage verdicts require verbatim substring quotes from the posting extract, dropping unverified claims.
* **Overlay architecture:** Triage verdicts act as an overlay on the gate; user overrides ("verdict is wrong") take immediate precedence.

---

## v9: Local-First Triage

* **Local LLM primary reader:** Local Ollama model (`qwen2.5:7b-instruct`) reads candidate postings offline, reserving Gemini for high-stakes edge cases and calibration.
* **Zero cloud cost triage:** Enables continuous background triage without consuming Gemini API quotas.

---

## v10 & v10b: Strict Triage Verification and Audit Feedback Loop

* **Verbatim full-text verification:** Replaced extract-level checking with strict substring search across the entire original job posting text. Any claim without an exact match is discarded.
* **Human audit labeling (`job_audit_labels`):** One-click audit interface allowing users to record agreement/disagreement with model verdicts, computing precision and agreement metrics directly.

---

## v11: Prompt Compaction and Overnight Batch API

* **Compact prompt formatting:** Structured prompt compaction reduced input and output tokens by over 35% without degrading scoring accuracy.
* **Overnight Batch API:** Integrated Gemini Batch API for nightly match runs, cutting API costs by 50% compared to standard interactive endpoints.

---

## v12 & v12b: Setup Wizard, Multi-Template Resumes, and Safe Settings

* **10-Step Setup Wizard (`/api/setup/*`):** Onboarding walkthrough supporting instant explore mode with synthetic demo persona Alex Chen (Waterloo '26).
* **Multi-template resume support:** Supports 6 popular LaTeX templates (Jake's, Awesome-CV, Moderncv, Deedy, AltaCV, Simple-Itemize) with strict template contract enforcement (`can_tailor = False` for facts-only templates).
* **15-minute background first ingest:** Replaced 30-second cap with a 15-minute background ingestion run, allowing users to explore the UI immediately while listings load.
* **Safe settings layer (`data/settings.yaml`):** User settings persist safely without touching tracked configuration files.

---

## v13 & v13b: Cross-Platform Portability and Friend-Ready Distribution

* **Run anywhere (Windows, macOS, Linux):** Replaced Windows-only commands and paths with platform abstraction adapters (`platform/paths.py`, `platform/process.py`, `platform/locking.py`).
* **Modular scheduler backends:** Added `in_app` (zero OS permissions, runs inside `serve`) and macOS `launchd` LaunchAgents alongside Windows Task Scheduler.
* **Tectonic automated download per OS/arch:** Setup scripts automatically download the verified Tectonic LaTeX binary for the host architecture.
* **Zero-PII scanner (`scripts/scan_pii.py`):** Dynamically loads personal terms from git-ignored profile files at run time, ensuring no personal names or identifiers are hardcoded into tracked source files.

---

## v14: Operational Automation and Consistent Forecasting

* **Daily morning check (`jobhunt morning-check`):** One-screen summary reporting scheduler status, budget forecast, Ollama reachability, triage coverage, and recent errors.
* **Consistent 7-day budget forecasting:** Fixed dilution bug where an early morning check divided 6 completed days by 7; formula now evaluates both completed days $[\text{today}-7, \text{today}-1]$ and trailing days, selecting the maximum to maintain true run rates.
* **Triage audit click interface:** Converted CSV audits into an interactive web review interface on the Triage Audit page.
* **Clean single-commit export:** To protect personal privacy in early commit history, public releases are published exclusively as a clean single-commit export rather than pushing raw historical commits.
