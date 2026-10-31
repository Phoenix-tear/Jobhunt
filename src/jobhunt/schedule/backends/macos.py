"""macOS launchd per-user LaunchAgents scheduler backend (Brief v13)."""

from __future__ import annotations

import os
import plistlib
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobhunt.config import Config
from jobhunt.platform.paths import get_python_executable
from jobhunt.schedule.backends.base import SchedulerBackend
from jobhunt.schedule.definitions import (
    TaskSpec,
    Trigger,
    build_task_specs,
    describe_triggers,
)

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@dataclass(frozen=True)
class PlannedLaunchdTask:
    spec: TaskSpec
    label: str
    plist_path: Path
    plist_dict: dict[str, Any]


def generate_calendar_intervals(spec: TaskSpec) -> list[dict[str, int]]:
    """Convert TaskSpec triggers into launchd StartCalendarInterval dicts."""
    intervals: list[dict[str, int]] = []
    weekday_map = {
        "sunday": 0,
        "monday": 1,
        "tuesday": 2,
        "wednesday": 3,
        "thursday": 4,
        "friday": 5,
        "saturday": 6,
    }

    for trigger in spec.triggers:
        if trigger.kind == "daily":
            for t_str in trigger.times:
                h, m = map(int, t_str.split(":"))
                intervals.append({"Hour": h, "Minute": m})
        elif trigger.kind == "weekly":
            for t_str in trigger.times:
                h, m = map(int, t_str.split(":"))
                w = weekday_map.get(trigger.weekday.lower(), 0)
                intervals.append({"Weekday": w, "Hour": h, "Minute": m})
        elif trigger.kind == "every_hours":
            anchor_h, anchor_m = map(int, (trigger.times[0] if trigger.times else "00:00").split(":"))
            step = max(1, trigger.every_hours or 4)
            for h in range(anchor_h % step, 24, step):
                intervals.append({"Hour": h, "Minute": anchor_m})

    return intervals


def generate_plist_dict(
    spec: TaskSpec,
    config: Config,
    label: str,
    log_file: Path,
) -> dict[str, Any]:
    """Generate launchd plist dictionary for task."""
    py_exe = get_python_executable(config.project_root)
    intervals = generate_calendar_intervals(spec)

    plist_data: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": [
            str(py_exe),
            "-m",
            "jobhunt",
            "scheduled-run",
            spec.key,
        ],
        "WorkingDirectory": str(config.project_root.resolve()),
        "StandardOutPath": str(log_file.resolve()),
        "StandardErrorPath": str(log_file.resolve()),
    }

    if len(intervals) == 1:
        plist_data["StartCalendarInterval"] = intervals[0]
    elif len(intervals) > 1:
        plist_data["StartCalendarInterval"] = intervals

    return plist_data


class MacOsLaunchdBackend(SchedulerBackend):
    name = "macos_launchd"

    def __init__(
        self,
        config: Config,
        *,
        config_path: Path | None = None,
        agents_dir: Path | None = None,
        runner: Runner = default_runner,
    ) -> None:
        super().__init__(config, config_path=config_path)
        if agents_dir is not None:
            self.agents_dir = Path(agents_dir)
        elif "LAUNCHD_AGENTS_DIR" in os.environ:
            self.agents_dir = Path(os.environ["LAUNCHD_AGENTS_DIR"])
        else:
            self.agents_dir = Path.home() / "Library" / "LaunchAgents"
        self.runner = runner

    def plan_tasks(self) -> list[PlannedLaunchdTask]:
        specs = build_task_specs(self.config.schedule)
        log_dir = self.config.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        planned: list[PlannedLaunchdTask] = []

        for key, spec in specs.items():
            label = f"com.jobhunt.{key}"
            plist_path = self.agents_dir / f"{label}.plist"
            log_file = log_dir / f"launchd_{key}.log"
            pdict = generate_plist_dict(spec, self.config, label, log_file)
            planned.append(PlannedLaunchdTask(spec=spec, label=label, plist_path=plist_path, plist_dict=pdict))

        return planned

    def describe_plan(self) -> str:
        tasks = self.plan_tasks()
        lines = [
            f"macOS launchd per-user agents ({len(tasks)} tasks):",
            f"LaunchAgents directory: {self.agents_dir}",
            "",
            f"{'task':<16} {'label':<24} {'schedule':<30} plist file",
            "-" * 90,
        ]
        for t in tasks:
            sched = describe_triggers(t.spec, self.config.schedule.timezone)
            lines.append(f"{t.spec.key:<16} {t.label:<24} {sched:<30} {t.plist_path.name}")
        lines.append("\nNote: On macOS, launchd automatically fires missed intervals when waking from sleep.")
        return "\n".join(lines)

    def install(self, *, skip_unconfigured_email: bool = True) -> list[tuple[str, bool, str]]:
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        tasks = self.plan_tasks()
        results: list[tuple[str, bool, str]] = []

        for t in tasks:
            if skip_unconfigured_email and t.spec.key == "ingest-email":
                from jobhunt.doctor import check_gmail_configured
                gmail_ok, gmail_reason = check_gmail_configured(self.config)
                if not gmail_ok:
                    results.append((t.label, True, f"skipped: Gmail not configured ({gmail_reason})"))
                    continue
            try:
                # Write plist file
                with open(t.plist_path, "wb") as f:
                    plistlib.dump(t.plist_dict, f)

                # Unload first if previously loaded to ensure clean state
                self.runner(["launchctl", "unload", "-w", str(t.plist_path)])

                # Load task
                res = self.runner(["launchctl", "load", "-w", str(t.plist_path)])
                if res.returncode == 0:
                    results.append((t.label, True, f"installed -> {t.plist_path}"))
                else:
                    results.append((t.label, False, f"launchctl load failed: {res.stderr.strip() or res.stdout.strip()}"))
            except Exception as exc:
                results.append((t.label, False, str(exc)))

        return results

    def remove(self) -> list[tuple[str, bool, str]]:
        tasks = self.plan_tasks()
        results: list[tuple[str, bool, str]] = []

        for t in tasks:
            if not t.plist_path.is_file():
                continue
            try:
                # Unload task
                self.runner(["launchctl", "unload", "-w", str(t.plist_path)])
                t.plist_path.unlink(missing_ok=True)
                results.append((t.label, True, f"unloaded and deleted {t.plist_path.name}"))
            except Exception as exc:
                results.append((t.label, False, str(exc)))

        return results

    def remove_task(self, task_name_or_key: str) -> tuple[str, bool, str]:
        label = task_name_or_key if task_name_or_key.startswith("com.jobhunt.") else f"com.jobhunt.{task_name_or_key}"
        plist_path = self.agents_dir / f"{label}.plist"
        if not plist_path.is_file():
            return (label, True, "not installed")
        try:
            self.runner(["launchctl", "unload", "-w", str(plist_path)])
            plist_path.unlink(missing_ok=True)
            return (label, True, f"unloaded and deleted {plist_path.name}")
        except Exception as exc:
            return (label, False, str(exc))

    def installed_tasks(self) -> list[dict[str, Any]]:
        installed: list[dict[str, Any]] = []
        if not self.agents_dir.is_dir():
            return installed

        # Query launchctl list
        active_labels: set[str] = set()
        res = self.runner(["launchctl", "list"])
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[2].startswith("com.jobhunt."):
                    active_labels.add(parts[2])

        for plist_file in sorted(self.agents_dir.glob("com.jobhunt.*.plist")):
            label = plist_file.stem
            installed.append({
                "TaskName": label,
                "State": "Loaded" if label in active_labels else "Installed",
                "NextRunTime": "N/A (managed by launchd)",
                "LastRunTime": "N/A",
                "LastResult": 0,
            })

        return installed
