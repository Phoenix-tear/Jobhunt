# Jobhunt Tester & Friend Guide

Welcome to Jobhunt! This guide walks you through setting up and testing Jobhunt on your computer (Windows, macOS, or Linux). Jobhunt is a local-first, privacy-respecting career assistant that ingests software engineering postings, filters out irrelevant roles using strict deterministic rules, and tailors ATS-compliant 1-page LaTeX resumes.

---

## 1. Prerequisites (Check Before You Begin)

1. **Git:** Installed and available on your PATH.
2. **Python:** Version **3.12 or newer** ([python.org/downloads](https://www.python.org/downloads/)).
3. **Node.js:** Version **22.12 or newer (Node 22 LTS)** ([nodejs.org](https://nodejs.org/)).
4. *(Optional)* **Ollama:** If you want offline vector embeddings ([ollama.com](https://ollama.com/)). If you don't have Ollama or a GPU, Jobhunt runs seamlessly using the free Gemini tier or pure offline mode.

---

## 2. One-Command Setup

Open your terminal or PowerShell and run:

### Windows (PowerShell)
```powershell
git clone <repo-url> jobhunt
cd jobhunt
.\scripts\setup.ps1
```

### macOS / Linux (Terminal)
```bash
git clone <repo-url> jobhunt
cd jobhunt
./scripts/setup.sh
```

### What Setup Does Automatically:
- Creates an isolated Python virtual environment (`.venv`).
- Installs pinned dependencies from `requirements.lock` (no massive torch/scipy packages required).
- Builds the modern React/Vite web frontend (`npm ci && npm run build`).
- Automatically downloads and validates the Tectonic LaTeX engine into `tools/tectonic/`.
- Runs `jobhunt doctor` to verify your environment.
- Does not open a browser or start a server. Run `start.ps1` / `start.sh` next: it uses port 8000, or the next free port up to 8010, prints the real URL and opens only its own server (`-NoBrowser` / `--no-browser` to skip).

---

## 3. Starting Jobhunt

Whenever you want to use Jobhunt later:
- **Windows:** `.\scripts\start.ps1`
- **macOS / Linux:** `./scripts/start.sh`
- Or directly via CLI: `.venv/bin/python -m jobhunt serve` (Windows: `.\.venv\Scripts\python.exe -m jobhunt serve`)

Visit the URL the start script prints (**`http://localhost:8000`** unless that port was busy)

---

## 4. The 10-Step Setup Wizard Walkthrough

When you first open `http://localhost:8000/setup`, the interactive wizard will guide you through:

1. **Welcome & Mode:** Choose between **"Set Up My Own Job Hunt"** or **"Explore Interactive Demo"** (pre-loaded with synthetic candidate Alex Chen and sample jobs).
2. **Gemini API Key (Optional):** Enter a Gemini API key or choose pure offline mode ($0 cost).
3. **Budget:** Select Free Tier ($0/month) or Paid ($5.00/month cap, 60 calls/day), or configure custom limits.
4. **Resumes:** Upload your base LaTeX (`.tex`) resume. Automatically detects template style and validates 1-page compilation with Tectonic.
5. **Facts Review:** Review accomplishments and skills extracted from your resume; tick the verified facts permitted in tailored resumes.
6. **Target Profile:** Configure role families, target titles, career stage, and location preferences.
7. **Job Sources:** Select active sources (Greenhouse, Lever, Workable, Jobbio, etc.) with pre-filtering rules.
8. **Schedule:** Choose your scheduler backend (`in_app` default, `windows_task_scheduler` on Windows, or `macos_launchd` on macOS) and run times.
9. **Gmail (Optional):** Connect read-only Gmail alert ingestion, or skip.
10. **Ready to Hunt (Finish):** Launches the 15-minute background ingestion run so you can start exploring immediately.

---

## 5. 30-Minute Tester Checklist

Try these features to verify everything works smoothly:

- [ ] **System Health:** Run `jobhunt doctor` in your terminal or visit the **Health** tab in the UI. All required checks should show green `[OK]`.
- [ ] **Board Navigation:** Open the **Jobs** page. Filter by category (Target, Maybe, Excluded) and verify location filtering matches your preference.
- [ ] **Resume Tailoring:** Click any target job, open the **Tailor Resume** panel, and click **Preview / Tailor**. Check that the resulting PDF is exactly 1 page and retains all your factual accomplishments.
- [ ] **Tracker & Applications:** Move a job from "Target" to "Applied" on the **Tracker** Kanban board. Add a note or follow-up date.
- [ ] **Scheduler Verification:** Run `jobhunt schedule-status` to inspect your active backend and next scheduled run.

---

## 6. Privacy & Local-First Architecture Guarantee

- **Zero Cloud Leakage:** All job postings, relevance verdicts, user labels, and resume profiles are stored in your local SQLite database at `data/jobhunt.sqlite3`.
- **Secrets Protection:** API keys and credentials live in `.secrets/` or `.env` and are strictly git-ignored.
- **Zero Telemetry:** Jobhunt does not send tracking events, telemetry, or analytics.

---

## 7. How to Update (`jobhunt upgrade`)

When new improvements are pushed to the repository:
```bash
git pull
jobhunt upgrade
```
`jobhunt upgrade` automatically:
1. Backs up your database to `data/backups/`.
2. Applies any additive database migrations safely.
3. Updates Python dependencies if `requirements.lock` changed.
4. Rebuilds the frontend web bundle if `web/` changed.

---

## 8. Reporting Bugs & Getting Help

If you run into any error, unexpected behavior, or crash:

1. Generate a shareable, scrubbed diagnostic report:
   ```bash
   jobhunt doctor --report
   ```
   This creates `doctor_report.txt`. The report is automatically scrubbed of all secrets, emails, phone numbers, and resume content.
2. Open an issue on GitHub attaching `doctor_report.txt` and using the [Bug Report Template](.github/ISSUE_TEMPLATE/bug_report.md).

---

## 9. 10-Question Tester Feedback Form

You can print this questionnaire at any time via `jobhunt feedback-template`:

1. **Environment:** What OS & architecture are you running? (e.g. macOS Sonoma M2, Windows 11 x64)
2. **Setup Script:** Did `setup.ps1` / `setup.sh` complete on the first try without errors?
3. **Setup Wizard:** Was the 10-step wizard easy to understand?
4. **Resume Format:** What LaTeX template did you test? Did Tectonic compile it cleanly to 1 page?
5. **Ingest Performance:** Did the quick initial ingest complete in the background while you explored?
6. **Relevance Gating:** Did the location and level screening match your expectations?
7. **Ranking & Matching:** How accurate were the top match scores?
8. **Tailoring Quality:** Were the tailored resume bullets concise, professional, and truthful?
9. **Scheduler:** Which scheduler backend did you choose (`in_app`, `windows_task_scheduler`, or `macos_launchd`)?
10. **Overall Rating (1-10):** What would make Jobhunt a daily essential tool for your search?

