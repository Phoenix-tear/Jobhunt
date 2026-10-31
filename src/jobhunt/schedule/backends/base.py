"""Base class for scheduler backends (Brief v13)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from jobhunt.config import Config


class SchedulerBackend(ABC):
    """Abstract interface for system and in-app task schedulers."""

    name: str

    def __init__(self, config: Config, *, config_path: Path | None = None) -> None:
        self.config = config
        self.config_path = config_path or (config.project_root / "config.yaml")

    @abstractmethod
    def plan_tasks(self) -> list[Any]:
        """Return planned task objects or specifications."""

    @abstractmethod
    def describe_plan(self) -> str:
        """Human-readable description of what will be installed."""

    @abstractmethod
    def install(self, *, skip_unconfigured_email: bool = True) -> list[tuple[str, bool, str]]:
        """Install tasks in scheduler. Returns list of (task_name, success, message)."""

    @abstractmethod
    def remove(self) -> list[tuple[str, bool, str]]:
        """Remove tasks from scheduler. Returns list of (task_name, success, message)."""

    def remove_task(self, task_name_or_key: str) -> tuple[str, bool, str]:
        """Remove a single task from scheduler. Default fallback implementation."""
        return (task_name_or_key, False, "remove_task not supported for this backend")

    @abstractmethod
    def installed_tasks(self) -> list[dict[str, Any]]:
        """List currently installed tasks as dictionaries."""

