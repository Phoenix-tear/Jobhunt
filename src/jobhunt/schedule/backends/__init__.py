"""Scheduler backends factory (Brief v13)."""

from __future__ import annotations

import os
from typing import Any

from jobhunt.config import Config
from jobhunt.schedule.backends.base import SchedulerBackend
from jobhunt.schedule.backends.in_app import InAppScheduler, InAppSchedulerBackend
from jobhunt.schedule.backends.macos import MacOsLaunchdBackend
from jobhunt.schedule.backends.windows import WindowsTaskSchedulerBackend

__all__ = [
    "SchedulerBackend",
    "WindowsTaskSchedulerBackend",
    "MacOsLaunchdBackend",
    "InAppSchedulerBackend",
    "InAppScheduler",
    "get_scheduler_backend",
    "list_supported_backends",
]

BACKENDS: dict[str, type[SchedulerBackend]] = {
    "windows_task_scheduler": WindowsTaskSchedulerBackend,
    "macos_launchd": MacOsLaunchdBackend,
    "in_app": InAppSchedulerBackend,
}


def list_supported_backends() -> list[str]:
    return list(BACKENDS.keys())


def get_scheduler_backend(
    backend_name: str | None,
    config: Config,
    **kwargs: Any,
) -> SchedulerBackend:
    """Return backend instance by name or config setting."""
    name = backend_name or config.schedule.backend
    if not name or name == "none":
        name = "in_app"

    cls = BACKENDS.get(name)
    if not cls:
        raise ValueError(
            f"Unknown scheduler backend: '{name}'. Supported backends: {', '.join(BACKENDS.keys())}"
        )
    return cls(config, **kwargs)

