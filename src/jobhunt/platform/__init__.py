"""Cross-platform adapters for file locking, process management, and path resolution (Brief v13)."""

from jobhunt.platform.locking import FileLock, pid_alive
from jobhunt.platform.paths import (
    find_ollama_executable,
    get_executable_name,
    get_os_arch,
    get_python_executable,
    get_pythonw_executable,
    get_reauth_command,
    get_tectonic_search_paths,
)
from jobhunt.platform.process import on_battery, run_hidden, start_detached

__all__ = [
    "FileLock",
    "pid_alive",
    "find_ollama_executable",
    "get_executable_name",
    "get_os_arch",
    "get_python_executable",
    "get_pythonw_executable",
    "get_reauth_command",
    "get_tectonic_search_paths",
    "on_battery",
    "run_hidden",
    "start_detached",
]

