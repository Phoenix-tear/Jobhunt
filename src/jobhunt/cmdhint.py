"""Command hints that work on this machine.

`python -m jobhunt` here usually starts the SYSTEM Python, which lacks the packages the repo `.venv` has (gmail
extras, pypdf, ...). When the running interpreter is not the venv one, every suggested command is printed with the
venv interpreter in front, and one warning explains why.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from pathlib import Path

VENV_PREFIX = r".\.venv\Scripts\python.exe -m jobhunt" if sys.platform == "win32" else "./.venv/bin/python -m jobhunt"
# Packages the optional extras provide; their absence outside the venv is what breaks scheduled runs.
EXTRA_MODULES = (("google.auth", "gmail"), ("pypdf", "matching"))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def venv_exists() -> bool:
    if sys.platform == "win32":
        return (_PROJECT_ROOT / ".venv" / "Scripts" / "python.exe").is_file()
    return (_PROJECT_ROOT / ".venv" / "bin" / "python").is_file()


def command(name: str) -> str:
    """The command to type for `jobhunt <name>`: venv-qualified when this interpreter is not the venv's."""
    return f"{VENV_PREFIX} {name}" if not in_venv() else f"python -m jobhunt {name}"


def _missing_extras() -> list[str]:
    missing: list[str] = []
    for module, extra in EXTRA_MODULES:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(f"{module} ({extra})")
    return missing


def warn_if_outside_venv(logger: logging.Logger) -> bool:
    """Warn once when running outside the venv while the venv exists and packages are missing here."""
    if in_venv() or not venv_exists():
        return False
    missing = _missing_extras()
    if not missing:
        return False
    logger.warning("running with %s, not the repo virtualenv; missing here: %s. Use %s <command>",
                   sys.executable, ", ".join(missing), VENV_PREFIX)
    return True


_PYTHON_M = re.compile(r"(?<![\w\\./])python(?:\.exe)? -m jobhunt\b")
_BACKTICKED = re.compile(r"`jobhunt ([a-z][a-z-]*)")
_RUN_BARE = re.compile(r"(\brun `)([a-z][a-z-]+)(`)")
_RUN_PAREN = re.compile(r"(\(run )([a-z][a-z-]+)(\))")


def qualify(text: str, commands: set[str] | frozenset[str]) -> str:
    """Rewrite command suggestions in `text` to the venv-qualified form (no-op inside the venv)."""
    if in_venv():
        return text
    text = _PYTHON_M.sub(lambda _m: VENV_PREFIX, text)
    text = _BACKTICKED.sub(lambda m: f"`{VENV_PREFIX} {m.group(1)}", text)
    text = _RUN_BARE.sub(lambda m: f"{m.group(1)}{VENV_PREFIX} {m.group(2)}{m.group(3)}"
                         if m.group(2) in commands else m.group(0), text)
    return _RUN_PAREN.sub(lambda m: f"{m.group(1)}{VENV_PREFIX} {m.group(2)}{m.group(3)}"
                          if m.group(2) in commands else m.group(0), text)


class QualifyCommands(logging.Filter):
    """Logging filter that applies `qualify` to every record, so existing messages need no edits."""

    def __init__(self, commands: set[str] | frozenset[str]) -> None:
        super().__init__()
        self.commands = commands

    def filter(self, record: logging.LogRecord) -> bool:
        if not in_venv():
            record.msg = qualify(record.getMessage(), self.commands)
            record.args = None
        return True
