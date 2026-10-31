"""In-app scheduler backend and background thread runner (Brief v13)."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from jobhunt.config import Config
from jobhunt.db import connect
from jobhunt.platform.paths import get_python_executable
from jobhunt.platform.process import start_detached
from jobhunt.schedule.backends.base import SchedulerBackend
from jobhunt.schedule.definitions import (
    TaskSpec,
    Trigger,
    build_task_specs,
    describe_triggers,
    zone,
)
from jobhunt.schedule.health import build_schedule_health
from jobhunt.settings.storage import read_settings_file, write_settings_file
from jobhunt.timeutil import utc_now

logger = logging.getLogger(__name__)


class InAppScheduler:
    """Lightweight scheduler running within `jobhunt serve`.
    
    Checks and runs missed jobs on startup, then evaluates triggers at configured times.
    Fully testable with custom clocks and runners without real threads or delays.
    """

    def __init__(
        self,
        config: Config,
        *,
        clock: Callable[[], datetime] = utc_now,
        runner: Callable[[str], None] | None = None,
        check_interval_seconds: float = 30.0,
    ) -> None:
        self.config = config
        self.clock = clock
        self.runner = runner or self._default_run_task
        self.check_interval_seconds = check_interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_executed_trigger: dict[str, str] = {}  # task -> last executed minute stamp
        self._checked_startup_missed = False

    def _default_run_task(self, task_name: str) -> None:
        """Execute scheduled-run subprocess detached."""
        py_exe = get_python_executable(self.config.project_root)
        cmd = [str(py_exe), "-m", "jobhunt", "scheduled-run", task_name]
        logger.info("InAppScheduler launching background task: %s", task_name)
        start_detached(cmd, cwd=self.config.project_root)

    def check_missed_on_startup(self, now: datetime | None = None) -> list[str]:
        """Check if any task was missed while the app was closed and run it."""
        current_now = now or self.clock()
        db_path = Path(self.config.database_path)
        if not db_path.is_file():
            return []

        conn = connect(db_path)
        try:
            health = build_schedule_health(conn, self.config, current_now)
        finally:
            conn.close()

        triggered: list[str] = []
        for task_info in health.get("tasks", []):
            task_key = task_info["task"]
            missed = task_info.get("missed_runs", 0)
            if missed > 0:
                logger.info("InAppScheduler: task '%s' was missed (%d times), queuing now", task_key, missed)
                self.runner(task_key)
                triggered.append(task_key)

        self._checked_startup_missed = True
        return triggered

    def check_due_tasks(self, now: datetime | None = None) -> list[str]:
        """Evaluate triggers against current time and run due tasks."""
        current_now = now or self.clock()
        tz_name = self.config.schedule.timezone
        specs = build_task_specs(self.config.schedule)
        tz = zone(tz_name)
        now_in_tz = current_now.astimezone(tz)
        current_minute = now_in_tz.strftime("%Y-%m-%d %H:%M")
        current_hhmm = now_in_tz.strftime("%H:%M")
        current_weekday = now_in_tz.strftime("%A")

        due: list[str] = []
        for key, spec in specs.items():
            for trigger in spec.triggers:
                is_due = False
                if trigger.kind == "daily":
                    if current_hhmm in trigger.times:
                        is_due = True
                elif trigger.kind == "weekly":
                    if trigger.weekday.lower() == current_weekday.lower() and current_hhmm in trigger.times:
                        is_due = True
                elif trigger.kind == "every_hours":
                    anchor_h, anchor_m = map(int, (trigger.times[0] if trigger.times else "00:00").split(":"))
                    h = now_in_tz.hour
                    m = now_in_tz.minute
                    step = max(1, trigger.every_hours or 4)
                    if (h - anchor_h) % step == 0 and m == anchor_m:
                        is_due = True

                if is_due:
                    trigger_stamp = f"{key}:{current_minute}"
                    if self._last_executed_trigger.get(key) != trigger_stamp:
                        self._last_executed_trigger[key] = trigger_stamp
                        due.append(key)
                        self.runner(key)
                    break

        return due

    def tick(self, now: datetime | None = None) -> list[str]:
        """Perform one scheduler cycle: check missed on startup then check due."""
        current_now = now or self.clock()
        executed: list[str] = []
        if not self._checked_startup_missed:
            executed.extend(self.check_missed_on_startup(current_now))
        executed.extend(self.check_due_tasks(current_now))
        return executed

    def start(self) -> None:
        """Start daemon background loop."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="JobhuntInAppScheduler", daemon=True)
        self._thread.start()
        logger.info("In-app scheduler background thread started.")

    def stop(self) -> None:
        """Stop background loop."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("In-app scheduler background thread stopped.")

    def _run_loop(self) -> None:
        try:
            self.tick()
        except Exception:
            logger.exception("Error during initial InAppScheduler tick")

        while not self._stop_event.wait(self.check_interval_seconds):
            try:
                self.check_due_tasks()
            except Exception:
                logger.exception("Error checking due tasks in InAppScheduler")


class InAppSchedulerBackend(SchedulerBackend):
    name = "in_app"

    def plan_tasks(self) -> list[TaskSpec]:
        return list(build_task_specs(self.config.schedule).values())

    def describe_plan(self) -> str:
        tasks = self.plan_tasks()
        lines = [
            f"In-App Scheduler ({len(tasks)} tasks):",
            "Runs inside 'jobhunt serve' without requiring OS-level scheduler privileges.",
            "Missed runs are executed automatically on app startup.",
            "",
            f"{'task':<16} {'schedule':<30} description",
            "-" * 80,
        ]
        for t in tasks:
            sched = describe_triggers(t, self.config.schedule.timezone)
            lines.append(f"{t.key:<16} {sched:<30} {t.description}")
        return "\n".join(lines)

    def install(self, *, skip_unconfigured_email: bool = True) -> list[tuple[str, bool, str]]:
        settings = read_settings_file(self.config.project_root, data_dir=self.config.data_dir)
        sched = dict(settings.get("schedule") or {})
        sched["backend"] = "in_app"
        settings["schedule"] = sched
        write_settings_file(settings, self.config.project_root, data_dir=self.config.data_dir)
        return [("in_app", True, "In-app scheduler enabled in settings.yaml (runs inside 'jobhunt serve')")]

    def remove(self) -> list[tuple[str, bool, str]]:
        settings = read_settings_file(self.config.project_root, data_dir=self.config.data_dir)
        sched = dict(settings.get("schedule") or {})
        sched["backend"] = "none"
        settings["schedule"] = sched
        write_settings_file(settings, self.config.project_root, data_dir=self.config.data_dir)
        return [("in_app", True, "In-app scheduler disabled in settings.yaml")]

    def remove_task(self, task_name_or_key: str) -> tuple[str, bool, str]:
        key = task_name_or_key.replace("in_app:", "").replace("jobhunt-", "")
        with self._lock:
            self.jobs.pop(key, None)
        return (f"in_app:{key}", True, "removed from in-app scheduler")

    def installed_tasks(self) -> list[dict[str, Any]]:
        # If active backend is in_app, return all task specs as installed
        specs = build_task_specs(self.config.schedule)
        return [
            {
                "TaskName": f"in_app:{k}",
                "State": "Enabled",
                "NextRunTime": "Managed by app",
                "LastRunTime": "See scheduled_runs",
                "LastResult": 0,
            }
            for k in specs
        ]
