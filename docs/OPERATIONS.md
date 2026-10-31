# Operations Guide

Operational instructions for running, maintaining, and automating `jobhunt` on Windows, macOS, and Linux.

---

## 1. Starting the Application

Always run within the project virtual environment:
- **Windows (PowerShell):** `.\scripts\start.ps1` (or `.\.venv\Scripts\python.exe -m jobhunt serve`)
- **macOS / Linux (Bash):** `./scripts/start.sh` (or `.venv/bin/python -m jobhunt serve`)

When started, `jobhunt serve`:
1. Creates an automatic backup if pending schema migrations exist, then applies them.
2. Binds strictly to `127.0.0.1:8000` (the start scripts pick the next free port, 8001-8010, if 8000 is occupied and print the real URL).

**Supported versions:** Python >= 3.12 (CI: 3.12 and 3.13), Node.js >= 22.12 (CI: Node 22). `requirements.lock` pins `numpy==2.5.4`, which needs Python 3.12+, and vitest 5 needs Node 22.12+.
3. Serves the prebuilt React web application from `web/dist/` and provides Swagger API docs at `http://127.0.0.1:8000/api/docs`.

### Development Mode (Frontend Live Reload)
For active web UI development with hot module reloading:
1. Terminal 1 (API Server): `python -m jobhunt serve`
2. Terminal 2 (Vite Dev Server): `cd web && npm run dev` (runs on `http://localhost:5173` with API proxying to port 8000)

---

## 2. Upgrading the Installation Safely

To update code without risking data loss or configuration drift:
```bash
git pull
jobhunt upgrade
```

**Order of execution in `jobhunt upgrade`:**
1. **Verified Backup:** Creates a full database backup in `data/backups/jobhunt_pre_upgrade_<timestamp>.sqlite3`.
2. **Database Migrations:** Applies any new SQL migrations additively.
3. **Lockfile Sync:** If `requirements.lock` changed, installs updated Python packages.
4. **Web Build:** If `web/` source code changed, executes `npm ci && npm run build`.
5. `jobhunt upgrade` **never runs `git pull` on its own**, ensuring you retain full control over git branching and checkout state.

---

## 3. Modular Scheduler Backends

`jobhunt` provides three modular scheduler backends:

| Backend Name | Operating System | Execution Mechanism | Privileges Required |
| --- | --- | --- | --- |
| `in_app` *(Default)* | Windows / macOS / Linux | Background thread inside `serve` process | Zero (runs in user space) |
| `windows_task_scheduler` | Windows | Windows Task Scheduler (`jobhunt-*` tasks) | Standard User (no admin) |
| `macos_launchd` | macOS | Per-user LaunchAgents (`~/Library/LaunchAgents/`) | Standard User (no sudo) |

### Managing Scheduled Tasks
```bash
# Check scheduler status, last execution times, and missed runs
jobhunt schedule-status

# Install automated daily schedule into active backend (needs --yes)
jobhunt schedule-install --yes

# Cleanly remove all scheduled tasks
jobhunt schedule-remove
```

### Automatic Skipping of Optional Tasks
- If Gmail integration is not configured or its authorization has expired, `schedule-install` automatically skips `jobhunt-ingest-email` and logs a clean notice rather than creating a failing task.
- `morning-check` recognizes uninstalled or unconfigured optional tasks and skips them cleanly without raising attention alerts.

---

## 4. Daily Morning Check (`jobhunt morning-check`)

The daily morning check provides an automated one-screen health and operational report:
```bash
jobhunt morning-check
```

### Report Contents
1. **Scheduled Tasks Status:** Flags failed runs or missed runs since yesterday.
2. **AI Budget Forecast:** Month-to-date spend, completed 7-day average run rate, projected month total, and cap warnings.
3. **System & Hardware:** Reachability of local Ollama server and available GPU VRAM.
4. **Triage & Queue Coverage:** Count and percentage of visible relevant jobs judged by the local model vs Gemini.
5. **Daily Pipeline Activity:** Number of stale postings auto-hidden today.
6. **Recent Errors:** First line of any error logged in `scheduled_runs` during the last 24 hours.

### Exit Codes & Integration
- **`0`:** All systems healthy.
- **`1`:** Attention needed (e.g., failed scheduled run, budget cap exceeded, Ollama unreachable).
- Output is automatically mirrored to `data/morning_check.txt` and surfaced prominently on the web UI **Health** page.

---

## 5. Gmail Job Alert Ingestion & 7-Day OAuth Expiry

`jobhunt` supports optional read-only ingestion of job alert emails from LinkedIn, Indeed, and Google Alerts via Gmail API (`jobhunt ingest-email`).

### Why Gmail Refresh Tokens Expire in 7 Days
When you create a Google Cloud OAuth Client ID, Google defaults the application publishing status to **"Testing"**. 
> [!IMPORTANT]
> Under Google Cloud's security policies, OAuth refresh tokens for projects in **"Testing"** mode automatically expire after **7 days**.

### Re-Authorizing Gmail
When a token expires, `jobhunt` handles it gracefully:
1. `ingest-email` logs a `skipped: gmail auth expired` status instead of throwing errors or corrupting state.
2. `morning-check` reports `SKIPPED (Gmail token expired)` without breaking core job matching.
3. To refresh your authorization:
   ```bash
   jobhunt gmail-auth
   ```
   This opens your browser to grant read-only access and updates `.secrets/token.json`.
4. *Tip for permanent tokens:* To prevent 7-day token expiration, navigate to your Google Cloud Console -> **OAuth consent screen** and set Publishing Status to **"In Production"** (requires no verification if kept for personal use).

---

## 6. Local Ollama & Hardware Acceleration

jobhunt supports 100% offline vector embeddings and local LLM triage using Ollama:
- **Default Embeddings Model:** `mxbai-embed-large` (1024 dimensions) or `nomic-embed-text`.
- **Default Local Triage Model:** `qwen2.5:7b-instruct` (configured in `config.yaml`).
- **Apple Silicon (macOS):** Metal acceleration is utilized automatically by Ollama. Macs with 8 GB unified memory should keep local LLM triage disabled and use the deterministic relevance gate.
- **Windows / Linux:** NVIDIA GPUs with CUDA are automatically leveraged for fast inference; CPU fallback executes gracefully when no dedicated GPU is detected.

---

## 7. Tectonic LaTeX Engine Sandboxing

Tailored 1-page resumes are compiled using the modern Tectonic LaTeX engine:
- **Executable Discovery:** Checks `tools/tectonic/<os-arch>/`, `tools/tectonic/`, `TECTONIC_PATH`, and system `PATH`.
- **Security Sandboxing:** Tectonic runs with `--untrusted` / `-no-shell-escape`, disallowing system commands or filesystem traversal.
- **Strict Limits:** Output is budgeted to 1 page, with process execution timeouts and PDF size caps.
- **First Compile Cache:** Tectonic automatically pulls needed LaTeX packages on the very first compilation (requires internet once). All subsequent compilations run 100% offline.

---

## 8. Backups and Database Recovery

All application data lives in `data/jobhunt.sqlite3`.

### Automated Backups
- Before any database migration executes, `db.migrate_with_backup` automatically creates a full, verified copy in `data/backups/jobhunt_pre_upgrade_<timestamp>.sqlite3`.

### Manual Backup & Restore
- **Backup:** Stop `jobhunt serve` and copy `data/jobhunt.sqlite3` to your preferred backup location, or use SQLite online backup:
  ```powershell
  sqlite3 data/jobhunt.sqlite3 ".backup 'data/backups/manual_backup.sqlite3'"
  ```
- **Restore:** Terminate `jobhunt serve`, replace `data/jobhunt.sqlite3` with your backup copy, and restart the server.
