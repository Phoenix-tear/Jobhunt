"""Cross-platform process management, detached processes, and battery status (Brief v13)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def start_detached(
    cmd: list[str],
    stdout: Any = subprocess.DEVNULL,
    stderr: Any = subprocess.DEVNULL,
    stdin: Any = subprocess.DEVNULL,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.Popen:
    """Start a detached process that outlives the caller.
    
    On Windows: Uses CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, with breakaway if allowed.
    On POSIX: Uses start_new_session=True (setsid) so process is detached from caller terminal.
    """
    if os.name == "nt":
        flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
        breakaway = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
        try:
            return subprocess.Popen(
                cmd,
                stdout=stdout,
                stderr=stderr,
                stdin=stdin,
                cwd=cwd,
                env=env,
                creationflags=flags | breakaway,
            )
        except OSError:
            # Job forbids breakaway, run with flags alone
            return subprocess.Popen(
                cmd,
                stdout=stdout,
                stderr=stderr,
                stdin=stdin,
                cwd=cwd,
                env=env,
                creationflags=flags,
            )

    # POSIX (macOS, Linux)
    return subprocess.Popen(
        cmd,
        stdout=stdout,
        stderr=stderr,
        stdin=stdin,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )


def run_hidden(
    cmd: list[str],
    timeout: float | None = None,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    **kwargs: Any,
) -> tuple[int, str]:
    """Run command with output captured, suppressing console popups on Windows.
    
    Returns (returncode, combined_stdout_stderr).
    """
    extra_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        extra_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            env=env,
            check=False,
            **extra_kwargs,
            **kwargs,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, output
    except subprocess.TimeoutExpired as expired:
        out = expired.stdout if isinstance(expired.stdout, str) else ""
        err = expired.stderr if isinstance(expired.stderr, str) else ""
        return -9, out + err


def on_battery() -> bool:
    """Return True if the machine is currently running on battery power."""
    # 1. Windows
    if os.name == "nt":
        try:
            import ctypes

            class PowerStatus(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", ctypes.c_byte),
                    ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte),
                    ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong),
                ]

            status = PowerStatus()
            if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                return False
            return status.ACLineStatus == 0
        except Exception:
            return False

    # 2. macOS
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["pmset", "-g", "batt"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            # pmset output contains "Now drawing from 'Battery Power'"
            return "Battery Power" in proc.stdout
        except Exception:
            return False

    # 3. Linux
    try:
        power_supply = Path("/sys/class/power_supply")
        if power_supply.is_dir():
            for child in power_supply.iterdir():
                type_file = child / "type"
                status_file = child / "status"
                if type_file.is_file() and type_file.read_text(encoding="utf-8").strip() == "Battery":
                    if status_file.is_file() and status_file.read_text(encoding="utf-8").strip().lower() == "discharging":
                        return True
    except Exception:
        pass

    return False

