"""Cross-platform path, binary resolution, and environment helpers (Brief v13)."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path


def get_os_arch() -> str:
    """Return canonical os-arch identifier:
    windows-x86_64, macos-aarch64, macos-x86_64, linux-x86_64, linux-aarch64.
    """
    sys_name = platform.system().lower()
    machine = platform.machine().lower()

    if sys_name == "windows":
        os_name = "windows"
    elif sys_name == "darwin":
        os_name = "macos"
    elif sys_name == "linux":
        os_name = "linux"
    else:
        os_name = sys_name

    if machine in ("x86_64", "amd64"):
        arch_name = "x86_64"
    elif machine in ("arm64", "aarch64"):
        arch_name = "aarch64"
    else:
        arch_name = machine

    return f"{os_name}-{arch_name}"


def get_executable_name(base: str) -> str:
    """Append .exe on Windows; leave plain on POSIX."""
    if os.name == "nt" and not base.lower().endswith(".exe"):
        return f"{base}.exe"
    return base


def get_python_executable(project_root: Path | None = None) -> Path:
    """Find the Python executable in the virtual environment or current interpreter."""
    root = project_root or Path.cwd()
    if os.name == "nt":
        venv_python = root / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = root / ".venv" / "bin" / "python"

    if venv_python.is_file():
        return venv_python
    return Path(sys.executable)


def get_pythonw_executable(project_root: Path | None = None) -> Path:
    """Find pythonw.exe on Windows (no console window) or python on POSIX."""
    if os.name == "nt":
        root = project_root or Path.cwd()
        venv_pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
        if venv_pythonw.is_file():
            return venv_pythonw
        curr = Path(sys.executable)
        if curr.name.lower() == "pythonw.exe":
            return curr
        cand = curr.with_name("pythonw.exe")
        if cand.is_file():
            return cand
    return get_python_executable(project_root)


def get_reauth_command(project_root: Path | None = None) -> str:
    """Return platform-appropriate re-auth command hint."""
    if os.name == "nt":
        return r".\.venv\Scripts\python.exe -m jobhunt gmail-auth"
    return "./.venv/bin/python -m jobhunt gmail-auth"


def get_tectonic_search_paths(project_root: Path | None = None) -> list[Path]:
    """Candidate paths to search for Tectonic binary in priority order:
    1. project tools/tectonic/<os-arch>/tectonic[.exe]
    2. project tools/tectonic/tectonic[.exe]
    3. TECTONIC_PATH environment variable
    4. System PATH
    """
    candidates: list[Path] = []
    root = project_root or Path.cwd()
    exe_name = get_executable_name("tectonic")
    os_arch = get_os_arch()

    # 1. tools/tectonic/<os-arch>/tectonic[.exe]
    candidates.append(root / "tools" / "tectonic" / os_arch / exe_name)

    # 2. tools/tectonic/tectonic[.exe]
    candidates.append(root / "tools" / "tectonic" / exe_name)

    # 3. TECTONIC_PATH
    env_tectonic = os.environ.get("TECTONIC_PATH")
    if env_tectonic:
        candidates.append(Path(env_tectonic).resolve())

    # 4. PATH lookup
    which_tectonic = shutil.which("tectonic")
    if which_tectonic:
        candidates.append(Path(which_tectonic).resolve())

    return candidates


def find_tectonic_executable(project_root: Path | None = None) -> Path | None:
    """Find working Tectonic executable or return None."""
    for cand in get_tectonic_search_paths(project_root):
        if cand.is_file():
            return cand
    return None


def find_ollama_executable(env_path: str | None = None) -> str | None:
    """Find ollama executable on PATH or in standard OS install folders."""
    found = shutil.which("ollama", path=env_path)
    if found:
        return found

    candidates: list[Path] = []
    if os.name == "nt":
        candidates.extend([
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Ollama" / "ollama.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Ollama" / "ollama.exe",
        ])
    elif sys.platform == "darwin":
        candidates.extend([
            Path("/opt/homebrew/bin/ollama"),
            Path("/usr/local/bin/ollama"),
            Path("/Applications/Ollama.app/Contents/Resources/ollama"),
        ])
    else:  # linux
        candidates.extend([
            Path("/usr/local/bin/ollama"),
            Path("/usr/bin/ollama"),
        ])

    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None

