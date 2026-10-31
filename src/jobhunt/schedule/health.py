"""What the Health page shows about the scheduled tasks: last run, next run, and missed runs."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from jobhunt.config import Config
from jobhunt.schedule.definitions import build_task_specs, describe_triggers, next_occurrence, occurrences
from jobhunt.schedule.runner import GMAIL_STATE_KEY, REAUTH_COMMAND, get_state
from jobhunt.timeutil import parse_iso, to_iso


def build_schedule_health(connection: sqlite3.Connection, config: Config, now: datetime) -> dict[str, Any]:
    """{'tasks': [...], 'gmail_auth_expired': bool, ...}. `now` is timezone-aware (UTC)."""
    specs = build_task_specs(config.schedule)
    tasks: list[dict[str, Any]] = []
    try:
        any_run = connection.execute("SELECT 1 FROM scheduled_runs LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:
        any_run = False
    for key, spec in specs.items():
        last = last_ok = None
        if any_run:
            last = connection.execute(
                "SELECT * FROM scheduled_runs WHERE task = ? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
            last_ok = connection.execute(
                "SELECT started_at FROM scheduled_runs WHERE task = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
                (key,)).fetchone()
        nxt = next_occurrence(spec, now, config.schedule.timezone)
        missed = 0
        if last_ok is not None:
            since = parse_iso(last_ok["started_at"])
            missed = max(0, len(occurrences(spec, since, now, config.schedule.timezone)) - 1)
            # the due time right after the last success is not "missed" until a full interval has passed
        tasks.append({
            "task": key, "name": spec.task_name(config.schedule.task_prefix), "description": spec.description,
            "schedule": describe_triggers(spec, config.schedule.timezone), "heavy": spec.heavy,
            "last_run_at": last["started_at"] if last else None,
            "last_status": last["status"] if last else None,
            "last_duration_seconds": last["duration_seconds"] if last else None,
            "last_detail": (last["detail"] or last["error"]) if last else None,
            "last_ok_at": last_ok["started_at"] if last_ok else None,
            "next_run_at": to_iso(nxt) if nxt else None,
            "missed_runs": missed,
            "missed_warning": missed > config.schedule.missed_runs_warning,
        })
    gmail = get_state(connection, GMAIL_STATE_KEY) or {}
    missed_list = [
        {"task": t["task"], "name": t["name"], "missed_runs": t["missed_runs"]}
        for t in tasks
        if t["missed_runs"] > 0
    ]
    return {
        "backend": config.schedule.backend,
        "tasks": tasks,
        "missed_runs": missed_list,
        "gmail_auth_expired": bool(gmail.get("expired")),
        "gmail_auth_reason": gmail.get("reason"),
        "gmail_auth_since": gmail.get("since"),
        "gmail_reauth_command": gmail.get("command") or REAUTH_COMMAND,
    }
