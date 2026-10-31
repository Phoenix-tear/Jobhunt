# jobhunt

**Local-first job discovery, relevance gating, ATS keyword alignment, no-fabrication resume tailoring, and job application tracker.**

jobhunt aggregates open tech roles from public ATS platforms (Greenhouse, Lever, Workable, SmartRecruiters, Ashby), RSS/JSON feeds, and curated lists into a personal local SQLite database. It screens listings against your criteria, scores matching roles, tailors single-page LaTeX resumes strictly from verified facts you confirm (it never invents experience), and tracks applications in a modern web interface.

---

## Privacy Principles

- **100% Local Storage:** Everything lives on your machine in `data/jobhunt.sqlite3`. The `data/` and `.secrets/` folders are git-ignored and never committed or uploaded.
- **Zero Live Network Calls by Default:** Runs fully offline out of the box. No scraping of logged-in sites, no CAPTCHA bypassing, no login walls.
- **Strict PII Stripping:** When Gemini API is enabled for scoring or tailoring, profile text is stripped of candidate names, phone numbers, emails, and personal URLs before dispatch.
- **No Telemetry:** No analytics, tracking beacons, or third-party monitoring.

---

## System Requirements

- **Operating System:** Windows 10/11 or macOS 12+ (Intel & Apple Silicon) or Linux.
- **Python:** 3.12 or newer (3.12 and 3.13 are tested in CI; with `venv`).
- **Node.js:** 22.12 or newer (22 LTS; with `npm`, for building and testing the web frontend).
- **Optional Tools:**
  - **Tectonic:** Modern standalone LaTeX engine used to compile tailored 1-page resume PDFs. The setup script downloads it automatically for your OS/architecture into `tools/tectonic/`.
  - **Ollama:** Optional for offline vector embeddings (`mxbai-embed-large`) and optional local LLM triage. Apple Silicon Macs use Metal GPU acceleration automatically (8 GB RAM Macs should keep local LLM off).
  - **Gemini API Key:** Optional for cloud-based embeddings and AI scoring/tailoring suggestions. Pure offline mode ($0 cost) works without any API key.

---

## Quickstart (One Command Setup)

Clone the repository and run the setup script:

### Windows (PowerShell)
```powershell
git clone https://github.com/<your-username>/jobhunt.git
cd jobhunt
.\scripts\setup.ps1
.\scripts\start.ps1
```

### macOS / Linux (Terminal)
```bash
git clone https://github.com/<your-username>/jobhunt.git
cd jobhunt
chmod +x ./scripts/*.sh
./scripts/setup.sh
./scripts/start.sh
```

**What the setup script does:**
1. Creates an isolated virtual environment in `.venv`.
2. Installs pinned dependencies from `requirements.lock`.
3. Verifies Node.js >= 22.12 and builds the React web application (`npm ci && npm run build`).
4. Downloads and verifies the Tectonic LaTeX engine for your OS/architecture into `tools/tectonic/`.
5. Runs `jobhunt doctor` diagnostic checks.
6. Initializes `data/`. It never opens a browser or starts a server; `start.ps1` / `start.sh` do that on a free port (8000, else 8001-8010) and open the browser only on their own server (use `-NoBrowser` / `--no-browser` to skip).

---

## First-Run Setup Wizard

When you open `http://127.0.0.1:8000`, the 10-step onboarding wizard guides you through:

1. **Welcome & Mode:** Choose **Explore Demo Mode** to test immediately with synthetic persona Alex Chen (Waterloo '26) and pre-seeded sample jobs, or set up your personal job hunt.
2. **Gemini Key (Optional):** Enter a Gemini API key or choose pure offline mode ($0 cost).
3. **Budget:** Select Free Tier ($0/month) or Paid ($5.00/month cap, 60 calls/day), or configure custom daily limits.
4. **Resumes:** Upload your base LaTeX (`.tex`) resume. Detects template style (Jake's, Moderncv, Awesome-CV, etc.) and tests 1-page compilation with Tectonic.
5. **Facts Review:** Review accomplishments extracted from your resume and tick verified facts permitted in tailored output.
6. **Target Profile:** Choose role families, target titles, career stage, and location preferences.
7. **Job Sources:** Select active job board feeds and ATS sources with pre-filtering rules.
8. **Schedule:** Pick your scheduler backend (`in_app` default, `windows_task_scheduler` on Windows, or `macos_launchd` on macOS) and run times.
9. **Gmail (Optional):** Connect read-only Gmail alert ingestion, or skip.
10. **Ready to Hunt (Finish):** Launches the 15-minute background ingestion run so you can start exploring immediately.

---

## Daily Use

Start the server:
- Windows: `.\scripts\start.ps1`
- macOS / Linux: `./scripts/start.sh`

Access the web interface at `http://127.0.0.1:8000`:
- **Today:** Top shortlisted jobs matching your profile with fit breakdowns.
- **Jobs:** Search, filter by tech keywords, workplace (remote/hybrid/onsite), and location.
- **Tracker:** Kanban board tracking Saved, Applied, Interview, and Offer stages.
- **Resumes:** View base resume templates, extracted facts, and generate tailored 1-page PDFs on demand.
- **Health:** System doctor diagnostics, scheduler status, database size, and API budget ledger.

### Common CLI Commands
Run commands inside your virtual environment (`.venv\Scripts\python.exe -m jobhunt` on Windows, `.venv/bin/python -m jobhunt` on macOS/Linux):
```bash
jobhunt doctor           # Environment, tools, and permissions diagnostic check
jobhunt upgrade          # Safe upgrade: backs up DB, runs migrations, updates packages/web
jobhunt schedule-status  # View installed tasks, next run times, and missed executions
jobhunt golden-snapshot  # Export regression snapshot for integrity verification
```

---

## Where Your Data Lives & How to Back It Up

All user data is stored locally:
- Database: `data/jobhunt.sqlite3` (SQLite with Write-Ahead Logging).
- Base Resumes: `data/resumes/`
- Tailored Resumes & PDFs: `data/tailored/`
- Configuration & Overrides: `data/settings.yaml`
- Automatic Database Backups: `data/backups/`

### Upgrading the Repo Safely
A friend or collaborator can update anytime with two commands:
```bash
git pull
jobhunt upgrade
```
`jobhunt upgrade` automatically creates a verified database backup in `data/backups/`, applies new schema migrations, installs updated dependencies if `requirements.lock` changed, and rebuilds the web UI if frontend files changed. It never runs `git pull` on its own.

---

## Troubleshooting

| Problem | Symptoms | Fix |
|---|---|---|
| **Port 8000 in use** | Another local service is using port 8000 | jobhunt automatically binds to the next available free port. Check terminal output for the URL or pass `--port 8080`. |
| **Tectonic missing** | Tailored LaTeX builds without generating PDFs | Run `jobhunt doctor`. Download Tectonic to `tools/tectonic/`, install via package manager (`brew install tectonic` on Mac, `scoop install tectonic` on Windows), or set `TECTONIC_PATH`. |
| **Gemini 429 / Quota limit** | API calls fail with rate limit error | App falls back to free rule-based matching. Adjust `budget.daily_calls` in Settings -> Gemini. |
| **Model not found** | API error citing invalid model name | In Settings -> Gemini, select a model from your API key's live supported model list. |
| **Ollama not running** | Similarity score reports 0 or Ollama unavailable | Start Ollama (`ollama serve`). In Settings -> Embeddings, you can switch provider to `gemini` or `none` (renormalizes fit and location weights automatically). |
| **macOS LaunchAgents error** | `schedule-install` fails on macOS | Ensure your terminal has permission to write to `~/Library/LaunchAgents`. Alternatively, use the default `in_app` backend. |
| **LaTeX PDF font errors** | First compile fails on missing fonts | Tectonic downloads required LaTeX packages on its first compile (requires internet once). Subsequent compiles run 100% offline. |

---

## Frequently Asked Questions (FAQ)

- **How much does it cost?** $0 in Pure Offline Mode or with Ollama embeddings. With Gemini API enabled, standard daily usage costs approximately $0.01 to $0.05 per day under default conservative budget caps.
- **What are the free-tier limits?** Google Gemini provides a free tier with 15 RPM. jobhunt includes a strict rate-limiter and daily budget guard to keep requests well within free limits.
- **How do I reset my setup?** Go to Settings -> Danger Zone -> Move Aside & Reset, or start fresh with `jobhunt --data-dir <new_folder> serve`.
- **How do I delete everything?** Terminate the server and delete the `data/` and `.secrets/` folders. Zero traces remain outside the repo directory.

---

## How the Matching Works (in 10 Lines)

1. Pre-filter excludes non-matching titles, hard knockouts, and unsupported levels.
2. Location classifier resolves country, city, and workplace eligibility against candidate residency.
3. Strict triage evidence verification checks verbatim posting quotes for seniority and citizenship cues.
4. Embedding engine computes vector cosine similarity against candidate profile (Ollama or Gemini).
5. Role family matcher computes keyword and semantic fit across software engineering specializations.
6. Composite formula combines components: `final_score = w_fit * fit + w_sim * similarity + w_loc * location - penalties`.
7. When similarity is disabled (`provider: none`), weights are automatically renormalized: `w_fit` and `w_loc` scale to 1.0.
8. Ranker filters target roles into shortlist; jobs scoring >= 75 surface on the Today feed.
9. Tailor builds customized 1-page LaTeX resume selecting matching bullet points strictly from confirmed facts.
10. Tectonic compiles the tailored LaTeX source inside an isolated sandbox into a publication-ready PDF.

---

## Contributing & Bug Reports

When opening an issue or reporting a bug, please include:
1. The output of `jobhunt doctor`
2. Operating system and architecture (e.g., Windows 11 x64, macOS Sonoma Apple Silicon M2)
3. Relevant error messages from `data/logs/` (sensitive keys and tokens are automatically masked).

**Privacy Notice:** Never include personal resumes, application answers, or unmasked credentials in bug reports.
