"""Windows Task Scheduler backend adapter (Brief v13)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jobhunt.config import Config
from jobhunt.schedule import windows
from jobhunt.schedule.backends.base import SchedulerBackend


class WindowsTaskSchedulerBackend(SchedulerBackend):
    name = "windows_task_scheduler"

    def __init__(self, config: Config, *, config_path: Path | None = None, runner: Any = windows.default_runner) -> None:
        super().__init__(config, config_path=config_path)
        self.runner = runner

    def plan_tasks(self) -> list[windows.PlannedTask]:
        return windows.plan_tasks(self.config, self.config_path)

    def describe_plan(self) -> str:
        tasks = self.plan_tasks()
        return windows.describe_plan(tasks, self.config)

    def install(self, *, skip_unconfigured_email: bool = True) -> list[tuple[str, bool, str]]:
        return windows.install(self.config, self.config_path, runner=self.runner, skip_unconfigured_email=skip_unconfigured_email)

    def remove(self) -> list[tuple[str, bool, str]]:
        return windows.remove(self.config, runner=self.runner)

    def remove_task(self, task_name_or_key: str) -> tuple[str, bool, str]:
        return windows.remove_task(self.config, task_name_or_key, runner=self.runner)

    def installed_tasks(self) -> list[dict[str, Any]]:
        return windows.installed_tasks(self.config, runner=self.runner)

