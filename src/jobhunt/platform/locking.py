"""Cross-platform file locking and process liveness adapters (Brief v13)."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def pid_alive(pid: int) -> bool:
    """Check if a process with given PID is currently alive on Windows or POSIX."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not process:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(process)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class FileLock:
    """Cross-platform advisory file lock using msvcrt on Windows and fcntl on POSIX."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fd: Any | None = None
        self._locked = False

    def acquire(self, timeout: float = 0.0, retry_interval: float = 0.05) -> bool:
        """Attempt to acquire exclusive lock, waiting up to `timeout` seconds."""
        deadline = time.monotonic() + timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)

        while True:
            try:
                if self._fd is None:
                    self._fd = open(self.path, "a+b")

                if os.name == "nt":
                    import msvcrt

                    self._fd.seek(0)
                    msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                self._locked = True
                return True
            except (OSError, IOError, BlockingIOError):
                if time.monotonic() >= deadline:
                    return False
                time.sleep(retry_interval)

    def release(self) -> None:
        """Release the lock and close file descriptor."""
        if not self._locked or self._fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fd.seek(0)
                try:
                    msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                self._fd.close()
            except OSError:
                pass
            self._fd = None
            self._locked = False

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()

