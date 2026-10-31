# Handoff: Briefing for a New AI Session

Facts below reflect the codebase, migrations (`001` through `026`), and verified tests up to the cleanup milestone.

---

## 1. What This Is

`jobhunt` is a single-user, local-first job-discovery and application management pipeline designed for tech professionals and students. It:
1. Ingests public ATS boards (Greenhouse, Lever, Workable, SmartRecruiters, Ashby), RSS/JSON feeds, and read-only Gmail job alerts into a personal SQLite database.
2. Applies a deterministic relevance gate (graduation year, excluded stacks, degree requirements, location fit).
3. Ranks listings with vector embeddings, a local NumPy ranker, and local LLM triage (`qwen2.5:7b-instruct` in Ollama) with Gemini fallback.
4. Enforces strict quote verification: any model claim not matching a verbatim substring in the posting text is dropped.
5. Surfaces shortlisted jobs (score >= 75) on a local React web application (Today, Jobs, Tracker, Resumes, Triage Audit, Settings, Health).
6. Tailors single-page LaTeX resumes strictly from confirmed profile facts (zero fabrication).
7. Drafts grounded cover letters and answers through a staged pipeline.
8. Runs cross-platform automation on Windows, macOS, and Linux using modular scheduler backends (`in_app`, `windows_task_scheduler`, `macos_launchd`).

---

## 2. Current State

- **Database:** Migrations `001` to `026` are fully applied, verified, and additive.
- **Backend Tests:** 2,518 pytest tests passing, 14 skipped (live network calls requiring `--live-smoke` or `--live-gemini`).
- **Frontend Tests:** 130 Vitest tests passing (`npm --prefix web test -- --run`).
- **Frontend Build:** `tsc --noEmit && vite build` compiles cleanly with 0 errors.
- **Secret & PII Scanner:** `scripts/scan_pii.py` passes with 0 findings across tracked files.
- **Local Triage Model:** Configured as `qwen2.5:7b-instruct` in `config.yaml` and verified present in Ollama.
- **Scheduler Status:** Unconfigured optional tasks (such as `ingest-email` when Gmail is expired or missing) are automatically skipped without raising attention alerts in `jobhunt morning-check`.

---

## 3. Where Things Live

- **Core Code:** `src/jobhunt/` (see `docs/ARCHITECTURE.md` Section 1 for module map).
- **Web Frontend:** `web/` (React 19, TypeScript, Tailwind CSS, Vite, Lucide icons).
- **Tests:** `tests/` (backend pytest suites), `web/src/**/*.test.tsx` (frontend Vitest suites).
- **Database Migrations:** `migrations/001_*.sql` to `migrations/026_*.sql`.
- **Public & Production Scripts:**
  - `scripts/setup.ps1` & `scripts/setup.sh`: One-command automated onboarding.
  - `scripts/start.ps1` & `scripts/start.sh`: Server launch wrapper.
  - `scripts/scan_pii.py`: Dynamic runtime secret and PII scanner.
  - `scripts/bench_embeddings.py` & `scripts/bench_local_llm.py`: Offline benchmark utilities.
- **Personal & Local Files (Git-Ignored, Never Commit or Print):**
  - Database: `data/*.sqlite3`, `data/backups/`
  - Resumes: `data/resumes/`, `data/tailored/`
  - Profile & Facts: `data/profile/` (`facts.json`, `profile.yaml`, `pii_terms.txt`, `terms.yaml`, `company_rules.yaml`, `application_answers.yaml`)
  - Secrets & Config: `.secrets/` (`credentials.json`, `token.json`), `.env`, `config.local.yaml`, `data/settings.yaml`

---

## 4. Conventions the Owner Expects

1. **Virtual Environment:** Always invoke Python tools with the repo virtualenv:
   - Windows: `.\.venv\Scripts\python.exe -m pytest`, `.\.venv\Scripts\python.exe -m jobhunt <command>`
   - macOS / Linux: `.venv/bin/python -m pytest`, `.venv/bin/python -m jobhunt <command>`
2. **Git Workflow:**
   - Commit per phase with descriptive conventional commit messages on `main`.
   - **Never push** unless a brief explicitly authorizes one push. Never force-push; never push tags or temporary backup branches.
3. **Database Integrity:**
   - Never delete anything under `data/`. Migrations must be strictly additive.
   - Always verify that a database backup exists in `data/backups/` before applying migrations.
4. **Zero Uncontrolled Model Spend:**
   - Do not call Gemini or Ollama models live ($0 spent) unless a brief specifically authorizes live calls and provides an explicit budget ceiling.
   - Never casually run `match`, `ingest`, or `tailor` on real data during maintenance tasks.
5. **Privacy & Redaction:**
   - **Never print secrets or personal values** in logs, terminal outputs, or artifacts (show path + line + kind only).
   - The scanner `scripts/scan_pii.py` must pass with 0 findings before concluding any task.
6. **Unattended Execution:**
   - Never ask questions during unattended briefs; choose the safest default and document choices in the designated report under `data/eval/`.

---

## 5. Known Operational Gotchas

- **Gmail 7-Day OAuth Expiry:** Google Cloud projects with OAuth consent screens set to "Testing" expire refresh tokens every 7 days. When expired, `ingest-email` and `morning-check` report skipped without failing. Run `jobhunt gmail-auth` to re-authorize.
- **Budget Day Window:** The UTC day for budget caps resets at 00:00 UTC (05:30 IST).
- **Public Repository Publishing:** The git history of the private working repository contains early personal metadata and commit logs. **Never push the raw git history to a public remote.** Always publish public releases exclusively as a clean single-commit export from an isolated directory (e.g. `..\jobhunt-public`).
- **LaTeX Fonts:** Tectonic downloads required LaTeX macro packages on its very first compile (requires internet once); subsequent compilations are 100% offline.
