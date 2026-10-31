"""Tests for modular scheduler backends (Windows, macOS launchd, and in-app) (Brief v13)."""

from __future__ import annotations

import plistlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jobhunt.config import Config, ScheduleSettings
from jobhunt.db import connect
from jobhunt.schedule.backends import (
    InAppScheduler,
    InAppSchedulerBackend,
    MacOsLaunchdBackend,
    WindowsTaskSchedulerBackend,
    get_scheduler_backend,
    list_supported_backends,
)
from jobhunt.schedule.backends.macos import generate_calendar_intervals, generate_plist_dict
from jobhunt.schedule.definitions import TaskSpec, Trigger


@pytest.fixture
def test_config(tmp_path: Path) -> Config:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "logs").mkdir()
    (tmp_path / ".secrets").mkdir()
    db_path = data_dir / "jobhunt.sqlite3"

    sched = ScheduleSettings(
        backend="windows_task_scheduler",
        timezone="Asia/Kolkata",
        ingest_times=("06:30", "19:00"),
        match_time="06:45",
        email_every_hours=4,
        discover_day="Sunday",
        discover_time="03:00",
    )
    return Config(
        project_root=tmp_path,
        data_dir=data_dir,
        database_path=str(db_path),
        schedule=sched,
    )


class TestSchedulerFactory:
    def test_list_supported_backends(self) -> None:
        backends = list_supported_backends()
        assert "windows_task_scheduler" in backends
        assert "macos_launchd" in backends
        assert "in_app" in backends

    def test_get_scheduler_backend(self, test_config: Config) -> None:
        w = get_scheduler_backend("windows_task_scheduler", test_config)
        assert isinstance(w, WindowsTaskSchedulerBackend)

        m = get_scheduler_backend("macos_launchd", test_config)
        assert isinstance(m, MacOsLaunchdBackend)

        a = get_scheduler_backend("in_app", test_config)
        assert isinstance(a, InAppSchedulerBackend)

    def test_unknown_backend_raises(self, test_config: Config) -> None:
        with pytest.raises(ValueError, match="Unknown scheduler backend"):
            get_scheduler_backend("systemd_unknown", test_config)


class TestMacOsLaunchdBackend:
    def test_generate_calendar_intervals_daily(self) -> None:
        spec = TaskSpec(
            key="ingest",
            description="Fetch jobs",
            heavy=True,
            triggers=(Trigger("daily", ("06:30", "19:00")),),
            steps=(("ingest",),),
        )
        intervals = generate_calendar_intervals(spec)
        assert len(intervals) == 2
        assert intervals[0] == {"Hour": 6, "Minute": 30}
        assert intervals[1] == {"Hour": 19, "Minute": 0}

    def test_generate_calendar_intervals_weekly(self) -> None:
        spec = TaskSpec(
            key="discover",
            description="Find companies",
            heavy=True,
            triggers=(Trigger("weekly", ("03:00",), weekday="Sunday"),),
            steps=(("discover",),),
        )
        intervals = generate_calendar_intervals(spec)
        assert len(intervals) == 1
        assert intervals[0] == {"Weekday": 0, "Hour": 3, "Minute": 0}

    def test_generate_calendar_intervals_interval(self) -> None:
        spec = TaskSpec(
            key="ingest-email",
            description="Read alert emails",
            heavy=False,
            triggers=(Trigger("every_hours", ("00:00",), every_hours=4),),
            steps=(("ingest-email",),),
        )
        intervals = generate_calendar_intervals(spec)
        # Every 4 hours: 0, 4, 8, 12, 16, 20 -> 6 intervals
        assert len(intervals) == 6
        assert intervals[0] == {"Hour": 0, "Minute": 0}
        assert intervals[1] == {"Hour": 4, "Minute": 0}

    def test_plist_generation_structure(self, test_config: Config, tmp_path: Path) -> None:
        spec = TaskSpec(
            key="match",
            description="Score jobs",
            heavy=True,
            triggers=(Trigger("daily", ("06:45",)),),
            steps=(("match",),),
        )
        log_file = tmp_path / "match.log"
        pdict = generate_plist_dict(spec, test_config, "com.jobhunt.match", log_file)

        assert pdict["Label"] == "com.jobhunt.match"
        assert pdict["ProgramArguments"][-2:] == ["scheduled-run", "match"]
        assert pdict["WorkingDirectory"] == str(test_config.project_root.resolve())
        assert pdict["StartCalendarInterval"] == {"Hour": 6, "Minute": 45}

        # Validate that plistlib can serialize and deserialize it
        serialized = plistlib.dumps(pdict)
        loaded = plistlib.loads(serialized)
        assert loaded["Label"] == "com.jobhunt.match"

    def test_install_and_remove_with_mock_runner(self, test_config: Config, tmp_path: Path) -> None:
        agents_dir = tmp_path / "LaunchAgents"
        mock_runner = MagicMock(return_value=subprocess.CompletedProcess([], 0, "loaded", ""))

        backend = MacOsLaunchdBackend(test_config, agents_dir=agents_dir, runner=mock_runner)
        install_res = backend.install()

        # All tasks should succeed
        assert all(ok for _, ok, _ in install_res)
        # Plists should exist on disk
        plists = list(agents_dir.glob("*.plist"))
        assert len(plists) >= 4

        # Remove tasks
        remove_res = backend.remove()
        assert all(ok for _, ok, _ in remove_res)
        # Plists should be unlinked
        assert len(list(agents_dir.glob("*.plist"))) == 0


class TestInAppScheduler:
    def test_in_app_tick_due_tasks(self, test_config: Config) -> None:
        triggered: list[str] = []
        runner = triggered.append

        # Mock current time to 2026-10-31 06:30:15 IST (which is 01:00:15 UTC)
        now_dt = datetime(2026, 10, 31, 1, 0, 15, tzinfo=timezone.utc)

        scheduler = InAppScheduler(
            test_config,
            clock=lambda: now_dt,
            runner=runner,
        )

        due = scheduler.check_due_tasks(now_dt)
        # 'ingest' has trigger at 06:30 IST
        assert "ingest" in due
        assert "ingest" in triggered

        # Re-running tick in same minute should not re-trigger
        due_again = scheduler.check_due_tasks(now_dt)
        assert len(due_again) == 0

    def test_in_app_startup_missed_tasks(self, test_config: Config) -> None:
        triggered: list[str] = []
        runner = triggered.append

        # Populate DB with scheduled_runs showing a task last ran 3 days ago
        conn = connect(test_config.database_path)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS scheduled_runs (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   task TEXT NOT NULL,
                   started_at TEXT NOT NULL,
                   status TEXT NOT NULL,
                   duration_seconds REAL,
                   detail TEXT,
                   error TEXT
               )"""
        )
        conn.execute(
            "INSERT INTO scheduled_runs (task, started_at, status) VALUES ('ingest', '2026-10-25T06:30:00Z', 'ok')"
        )
        conn.commit()
        conn.close()

        # Now is 2026-10-31
        now_dt = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
        scheduler = InAppScheduler(test_config, clock=lambda: now_dt, runner=runner)

        missed = scheduler.check_missed_on_startup(now_dt)
        assert "ingest" in missed
        assert "ingest" in triggered

    def test_in_app_backend_install_remove(self, test_config: Config) -> None:
        backend = InAppSchedulerBackend(test_config)
        res = backend.install()
        assert res[0][1] is True

        # Check installed_tasks returns rows
        tasks = backend.installed_tasks()
        assert len(tasks) >= 4
        assert tasks[0]["State"] == "Enabled"

        res_rem = backend.remove()
        assert res_rem[0][1] is True
