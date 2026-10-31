"""Morning check CLI command and status reporter (Brief v14 Section 2).

One-screen morning summary:
1. Each scheduled task's last run time and status (flag missed or failed runs since yesterday).
2. Budget forecast (month-to-date, 7-day average, projected month total, cap warning).
3. Ollama server reachability.
4. GPU / VRAM free memory.
5. Triage coverage (judged / visible Relevant, split by Gemini and local model).
6. Unjudged queue length.
7. Count of jobs auto-hidden today.
8. Errors from `scheduled_runs` in the last 24 hours (first line of each).

Exit code:
- 0: All healthy
- 1: Something needs attention (failed task, missed runs, budget cap exceeded, error in last 24h, Ollama down)

Writes text to `data/morning_check.txt` and surfaces on Health page.
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jobhunt.config import Config
from jobhunt.matching.budget import compute_budget_forecast
from jobhunt.matching.triage_apply_llm import get_visible_relevant_jobs
from jobhunt.schedule.definitions import build_task_specs, occurrences
from jobhunt.schedule.runner import ollama_answers
from jobhunt.timeutil import parse_iso, to_iso, utc_now

logger = logging.getLogger(__name__)


def get_gpu_info() -> str:
    """Query NVIDIA GPU memory via nvidia-smi if available."""
    try:
        p = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if p.returncode == 0 and p.stdout.strip():
            line = p.stdout.strip().splitlines()[0]
            parts = [part.strip() for part in line.split(",")]
            if len(parts) >= 3:
                name, total_mb, free_mb = parts[0], parts[1], parts[2]
                return f"{name} ({free_mb} MB free / {total_mb} MB total)"
    except Exception:
        pass
    return "N/A (CPU / Integrated Graphics)"


def run_morning_check(
    connection: sqlite3.Connection,
    config: Config,
    *,
    now: datetime | None = None,
    output_path: Path | None = None,
) -> tuple[int, str]:
    now = now or utc_now()
    today = now.date()
    yesterday = today - timedelta(days=1)
    yesterday_iso = f"{yesterday.isoformat()}T00:00:00Z"
    last_24h = now - timedelta(hours=24)
    last_24h_iso = to_iso(last_24h)

    attention_reasons: list[str] = []
    lines: list[str] = []

    lines.append("=" * 80)
    lines.append("                         JOBHUNT MORNING CHECK")
    lines.append("=" * 80)
    lines.append(f"Timestamp: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append("")

    # 1. Scheduled Tasks Status
    lines.append("1. SCHEDULED TASKS STATUS")
    specs = build_task_specs(config.schedule)
    tasks_healthy = True

    # Query installed tasks from scheduler backend to detect uninstalled tasks
    installed_task_names: set[str] | None = None
    try:
        from jobhunt.schedule.backends import get_scheduler_backend
        backend = get_scheduler_backend(config.schedule.backend, config)
        installed = backend.installed_tasks()
        installed_task_names = {
            str(row.get("TaskName") or row.get("label") or row.get("key") or "")
            for row in installed
        }
    except Exception:
        installed_task_names = None

    for key, spec in specs.items():
        # Check applicability (e.g. Gmail integration for ingest-email)
        if key == "ingest-email":
            from jobhunt.doctor import check_gmail_configured
            gmail_ok, _ = check_gmail_configured(config)
            if not gmail_ok:
                lines.append(f"   {key:<15}: SKIPPED (Gmail not configured)")
                continue

        # Check if task is installed in active scheduler backend
        if installed_task_names is not None:
            expected_name = spec.task_name(config.schedule.task_prefix)
            if (
                expected_name not in installed_task_names
                and key not in installed_task_names
                and f"in_app:{key}" not in installed_task_names
            ):
                lines.append(f"   {key:<15}: NOT INSTALLED")
                continue

        try:
            last_run = connection.execute(
                "SELECT * FROM scheduled_runs WHERE task = ? ORDER BY id DESC LIMIT 1",
                (key,),
            ).fetchone()
            # Check for failed runs since yesterday
            failed_row = connection.execute(
                "SELECT error, started_at FROM scheduled_runs WHERE task = ? AND status = 'failed' AND started_at >= ? "
                "ORDER BY id DESC LIMIT 1",
                (key, yesterday_iso),
            ).fetchone()
            first_run_row = connection.execute(
                "SELECT MIN(started_at) FROM scheduled_runs WHERE task = ?",
                (key,),
            ).fetchone()
            first_run_dt = parse_iso(first_run_row[0]) if (first_run_row and first_run_row[0]) else None
        except sqlite3.OperationalError:
            last_run = None
            failed_row = None
            first_run_dt = None

        # Check missed runs
        missed = 0
        if last_run is not None:
            last_ok = connection.execute(
                "SELECT started_at FROM scheduled_runs WHERE task = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
                (key,),
            ).fetchone()
            last_ok_dt = parse_iso(last_ok[0]) if last_ok and last_ok[0] else None

            # Bound check_from: never count runs before the task was first run/installed or > 24h ago
            base_from = last_ok_dt or first_run_dt
            if base_from is not None:
                check_from = max(base_from, last_24h)
                if last_run["status"] not in ("ok", "running"):
                    due = occurrences(spec, check_from, now, config.schedule.timezone)
                    # Only count runs strictly after check_from and on or before now
                    due_past = [t for t in due if check_from < t <= now]
                    if due_past:
                        missed = len(due_past)

        if failed_row:
            err_line = (failed_row["error"] or "failed").strip().splitlines()[0][:80]
            lines.append(f"   {key:<15}: FAILED since yesterday ({err_line})")
            attention_reasons.append(f"Task '{key}' failed since yesterday: {err_line}")
            tasks_healthy = False
        elif missed > 0:
            lines.append(f"   {key:<15}: MISSED ({missed} due runs)")
            attention_reasons.append(f"Task '{key}' missed {missed} scheduled run(s)")
            tasks_healthy = False
        elif last_run:
            st = last_run["status"]
            started = last_run["started_at"] or "?"
            dur = f"{last_run['duration_seconds']}s" if last_run["duration_seconds"] is not None else ""
            lines.append(f"   {key:<15}: {st.upper():<7} (Last: {started} {dur})")
        else:
            lines.append(f"   {key:<15}: NEVER RUN")

    lines.append("")

    # 2. Budget Forecast
    lines.append("2. AI BUDGET FORECAST")
    forecast = compute_budget_forecast(connection, config, now)
    lines.append(f"   Month-to-Date Spend : ${forecast.month_to_date_usd:.2f} (Monthly Cap: ${forecast.cap_usd:.2f})")
    lines.append(f"   7-Day Daily Average : ${forecast.avg_daily_usd_7d:.2f} / day")
    status_msg = "WARNING: PROJECTION EXCEEDS CAP" if forecast.warning else "OK (within cap)"
    lines.append(f"   Projected Month End : ${forecast.projected_month_usd:.2f} [{status_msg}]")
    if forecast.cap_hit_date:
        lines.append(f"   Cap Hit Projected   : {forecast.cap_hit_date}")
    if forecast.warning:
        attention_reasons.append(
            f"Budget forecast warning: projected spend (${forecast.projected_month_usd:.2f}) exceeds cap (${forecast.cap_usd:.2f})"
        )
    lines.append("")

    # 3. System & Ollama Reachability
    lines.append("3. SYSTEM & HARDWARE")
    ollama_url = getattr(config.models, "ollama_url", "http://127.0.0.1:11434")
    ollama_ok = ollama_answers(ollama_url)
    lines.append(f"   Ollama Server : {'REACHABLE' if ollama_ok else 'UNREACHABLE'} ({ollama_url})")
    if not ollama_ok:
        attention_reasons.append(f"Ollama server is unreachable at {ollama_url}")

    gpu_str = get_gpu_info()
    lines.append(f"   GPU / VRAM    : {gpu_str}")
    lines.append("")

    # 4. Triage Coverage & Unjudged Queue
    lines.append("4. TRIAGE & QUEUE COVERAGE")
    try:
        vis_rel = get_visible_relevant_jobs(connection)
        total_vis = len(vis_rel)

        gemini_judged = set(
            r[0] for r in connection.execute(
                "SELECT job_id FROM job_llm_verdicts WHERE source = 'gemini' AND verified_json IS NOT NULL"
            ).fetchall()
        )
        local_judged = set(
            r[0] for r in connection.execute(
                "SELECT job_id FROM job_llm_verdicts WHERE source != 'gemini' AND verified_json IS NOT NULL"
            ).fetchall()
        )
        gemini_vis = len(set(vis_rel.keys()) & gemini_judged)
        local_vis = len(set(vis_rel.keys()) & (local_judged - gemini_judged))
        all_judged = len(set(vis_rel.keys()) & (gemini_judged | local_judged))
        unjudged_vis = total_vis - all_judged

        cov_pct = (all_judged / total_vis * 100.0) if total_vis > 0 else 0.0
        lines.append(f"   Visible Relevant Jobs : {total_vis}")
        lines.append(f"   Total Judged Coverage : {all_judged}/{total_vis} ({cov_pct:.1f}%)")
        lines.append(f"     - By Gemini         : {gemini_vis}")
        lines.append(f"     - By Local Model    : {local_vis}")
        lines.append(f"   Unjudged Queue Length : {unjudged_vis} jobs")
    except Exception as e:
        lines.append(f"   Triage data error: {e}")
    lines.append("")

    # 5. Jobs Auto-Hidden Today
    lines.append("5. DAILY PIPELINE ACTIVITY")
    try:
        hidden_today = connection.execute(
            "SELECT COUNT(*) FROM job_auto_hidden WHERE hidden_at >= ? AND restored_at IS NULL",
            (f"{today.isoformat()}T00:00:00Z",),
        ).fetchone()[0]
        lines.append(f"   Jobs Auto-Hidden Today : {hidden_today}")
    except sqlite3.OperationalError:
        lines.append("   Jobs Auto-Hidden Today : 0 (table not available)")
    lines.append("")

    # 6. Scheduled Runs Errors in Last 24 Hours
    lines.append("6. RECENT SCHEDULED ERRORS (LAST 24 HOURS)")
    try:
        err_rows = connection.execute(
            "SELECT task, started_at, error FROM scheduled_runs WHERE started_at >= ? AND error IS NOT NULL AND error != '' ORDER BY id DESC",
            (last_24h_iso,),
        ).fetchall()
        if not err_rows:
            lines.append("   None (0 errors recorded)")
        else:
            for task_name, started_at, err_text in err_rows:
                first_line = (err_text or "").strip().splitlines()[0][:100]
                lines.append(f"   [{started_at}] {task_name}: {first_line}")
                attention_reasons.append(f"Error in {task_name} at {started_at}: {first_line}")
    except sqlite3.OperationalError:
        lines.append("   None")
    lines.append("")

    # Status summary
    lines.append("=" * 80)
    if attention_reasons:
        lines.append(f"STATUS: ATTENTION NEEDED ({len(attention_reasons)} item(s)):")
        for reason in attention_reasons:
            lines.append(f"  * {reason}")
        exit_code = 1
    else:
        lines.append("STATUS: ALL SYSTEMS HEALTHY")
        exit_code = 0
    lines.append("=" * 80)

    output_text = "\n".join(lines) + "\n"

    # Write to data/morning_check.txt
    target_path = output_path or config.resolve_path("data/morning_check.txt")
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(output_text, encoding="utf-8")
    except OSError as err:
        logger.warning("Could not write morning check to %s: %s", target_path, err)

    return exit_code, output_text

