"""Tests for cross-platform platform adapters (Brief v13)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jobhunt.platform.locking import FileLock, pid_alive
from jobhunt.platform.paths import (
    find_ollama_executable,
    find_tectonic_executable,
    get_executable_name,
    get_os_arch,
    get_python_executable,
    get_pythonw_executable,
    get_reauth_command,
    get_tectonic_search_paths,
)
from jobhunt.platform.process import on_battery, run_hidden, start_detached


class TestPlatformPaths:
    @pytest.mark.parametrize(
        "system_name, machine_name, expected",
        [
            ("Windows", "AMD64", "windows-x86_64"),
            ("Windows", "x86_64", "windows-x86_64"),
            ("Darwin", "arm64", "macos-aarch64"),
            ("Darwin", "x86_64", "macos-x86_64"),
            ("Linux", "x86_64", "linux-x86_64"),
            ("Linux", "aarch64", "linux-aarch64"),
        ],
    )
    def test_get_os_arch(self, system_name: str, machine_name: str, expected: str) -> None:
        with patch("platform.system", return_value=system_name), patch("platform.machine", return_value=machine_name):
            assert get_os_arch() == expected

    def test_get_executable_name(self) -> None:
        with patch("os.name", "nt"):
            assert get_executable_name("tectonic") == "tectonic.exe"
            assert get_executable_name("tectonic.exe") == "tectonic.exe"
        with patch("os.name", "posix"):
            assert get_executable_name("tectonic") == "tectonic"
            assert get_executable_name("tectonic.exe") == "tectonic.exe"

    def test_get_reauth_command(self) -> None:
        with patch("os.name", "nt"):
            assert r".\.venv\Scripts\python.exe" in get_reauth_command()
        with patch("os.name", "posix"):
            assert "./.venv/bin/python" in get_reauth_command()

    def test_get_python_executable_venv(self, tmp_path: Path) -> None:
        # Simulate posix venv
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        py = venv_bin / "python"
        py.touch()

        with patch("os.name", "posix"):
            assert get_python_executable(tmp_path) == py

        # Simulate windows venv
        venv_scripts = tmp_path / ".venv" / "Scripts"
        venv_scripts.mkdir(parents=True)
        py_exe = venv_scripts / "python.exe"
        py_exe.touch()

        with patch("os.name", "nt"):
            assert get_python_executable(tmp_path) == py_exe

    def test_tectonic_search_hierarchy(self, tmp_path: Path) -> None:
        custom_bin = tmp_path / "custom" / "tectonic.exe"
        which_bin = tmp_path / "which" / "tectonic.exe"
        with patch("jobhunt.platform.paths.get_os_arch", return_value="windows-x86_64"), \
             patch.dict(os.environ, {"TECTONIC_PATH": str(custom_bin)}), \
             patch("shutil.which", return_value=str(which_bin)):
            paths = get_tectonic_search_paths(tmp_path)
            assert len(paths) >= 4
            exe_name = "tectonic.exe" if os.name == "nt" else "tectonic"  # the suffix follows the real OS
            assert paths[0] == tmp_path / "tools" / "tectonic" / "windows-x86_64" / exe_name
            assert paths[1] == tmp_path / "tools" / "tectonic" / exe_name
            assert paths[2] == custom_bin.resolve()
            assert paths[3] == which_bin.resolve()

    def test_find_tectonic_executable(self, tmp_path: Path) -> None:
        tools_os = tmp_path / "tools" / "tectonic" / "windows-x86_64"
        tools_os.mkdir(parents=True)
        tectonic_bin = tools_os / "tectonic.exe"
        tectonic_bin.touch()

        with patch("jobhunt.platform.paths.get_os_arch", return_value="windows-x86_64"), \
             patch("os.name", "nt"):
            found = find_tectonic_executable(tmp_path)
            assert found == tectonic_bin


class TestPlatformLocking:
    def test_pid_alive_current_process(self) -> None:
        assert pid_alive(os.getpid()) is True

    def test_pid_alive_nonexistent_pid(self) -> None:
        # 99999999 is almost certainly not an active process
        assert pid_alive(99999999) is False
        assert pid_alive(-1) is False
        assert pid_alive(0) is False

    def test_file_lock_mutual_exclusion(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "test.lock"
        lock1 = FileLock(lock_file)
        lock2 = FileLock(lock_file)

        assert lock1.acquire(timeout=0.1) is True
        # Second acquire should fail immediately or after timeout
        assert lock2.acquire(timeout=0.05) is False

        # Release first lock
        lock1.release()

        # Now second acquire succeeds
        assert lock2.acquire(timeout=0.1) is True
        lock2.release()


class TestPlatformProcess:
    def test_run_hidden_success(self) -> None:
        code, out = run_hidden([sys.executable, "-c", "print('platform-adapter-ok')"])
        assert code == 0
        assert "platform-adapter-ok" in out

    def test_on_battery(self) -> None:
        # Should return a boolean without crashing on any platform
        val = on_battery()
        assert isinstance(val, bool)
