"""Run one scheduled task: lock, log (rotating, redacted), time limit, battery check, and a `scheduled_runs` row.

`python -m jobhunt scheduled-run <task>` is what Task Scheduler executes. Steps run as child processes of
`python -m jobhunt ...` so a crash in one cannot take the runner (and its bookkeeping) down with it.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jobhunt.config import Config
from jobhunt.platform.locking import pid_alive
from jobhunt.platform.paths import find_ollama_executable as find_ollama, get_python_executable, get_reauth_command
from jobhunt.platform.process import on_battery, run_hidden, start_detached
from jobhunt.schedule.definitions import TaskSpec, build_task_specs
from jobhunt.timeutil import to_iso, utc_now

logger = logging.getLogger(__name__)

GMAIL_STATE_KEY = "gmail_auth"
REAUTH_COMMAND = get_reauth_command()
LOG_BYTES = 1_000_000
LOG_BACKUPS = 5

_SECRET_PATTERNS = [
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "[redacted-key]"),
    (re.compile(r"(?i)(key|token|secret|password|authorization)(=|:\s*)(bearer\s+)?[^\s&\"']{8,}"), r"\1\2[redacted]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"), "Bearer [redacted]"),
    (re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}"), "[redacted-token]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "[redacted-key]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# --- state in the database -----------------------------------------------------------------------------------------


def set_state(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        """INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, json.dumps(value), to_iso(utc_now())))
    connection.commit()


def get_state(connection: sqlite3.Connection, key: str) -> Any:
    try:
        row = connection.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[0] is None:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


# --- lock ----------------------------------------------------------------------------------------------------------


class RunLock:
    """A lock file holding {pid, task, started}. A lock whose process is gone, or older than `max_age`, is stale."""

    def __init__(self, path: Path, task: str, max_age_seconds: float, *, alive: Callable[[int], bool] = pid_alive) -> None:
        self.path = path
        self.task = task
        self.max_age = max_age_seconds
        self.alive = alive
        self.held = False

    def _holder(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _stale(self) -> bool:
        holder = self._holder()
        if holder is None:
            return True
        age = time.time() - float(holder.get("started_epoch", 0))
        return age > self.max_age or not self.alive(int(holder.get("pid", 0)))

    def try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._stale():
                    try:
                        self.path.unlink()
                    except OSError:
                        return False
                    continue
                return False
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump({"pid": os.getpid(), "task": self.task, "started_epoch": time.time()}, stream)
            self.held = True
            return True
        return False

    def acquire(self, wait_seconds: float, *, poll_seconds: float = 5.0,
                sleep: Callable[[float], None] = time.sleep, ready: Callable[[], bool] = lambda: True) -> bool:
        """Try until `wait_seconds` pass; `ready()` False means "an earlier task in the catch-up order is waiting"."""
        deadline = time.monotonic() + wait_seconds
        while True:
            if ready() and self.try_acquire():
                return True
            if time.monotonic() >= deadline:
                return False
            sleep(poll_seconds)

    def holder_description(self) -> str:
        holder = self._holder() or {}
        return f"{holder.get('task', '?')} (pid {holder.get('pid', '?')})"

    def release(self) -> None:
        if self.held:
            self.held = False
            try:
                self.path.unlink()
            except OSError:
                pass


class WaitingRoom:
    """Tasks that start together (Windows catch-up after sleep or boot) take the lock in `TaskSpec.order`.

    Each task registers a small file before it tries for the lock and removes it afterwards; a task does not try
    while a live task with a lower order number is registered. The short grace period at the start lets tasks that
    began in the same second register before anyone tries."""

    def __init__(self, directory: Path, task: str, order: int, *, alive: Callable[[int], bool] = pid_alive,
                 max_age_seconds: float = 8 * 3600) -> None:
        self.directory = directory
        self.task = task
        self.order = order
        self.alive = alive
        self.max_age = max_age_seconds
        self.path = directory / f"{order}-{task}-{os.getpid()}.json"

    def register(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"pid": os.getpid(), "task": self.task, "order": self.order,
                                         "started_epoch": time.time()}), encoding="utf-8")

    def unregister(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass

    def blocked_by(self) -> str | None:
        """Name of a live, earlier-ordered waiting task, else None."""
        try:
            entries = list(self.directory.glob("*.json"))
        except OSError:
            return None
        for entry in entries:
            if entry == self.path:
                continue
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
                order, pid = int(data["order"]), int(data["pid"])
                age = time.time() - float(data["started_epoch"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if age > self.max_age or not self.alive(pid):
                try:
                    entry.unlink()                      # a crashed task never blocks the others
                except OSError:
                    pass
                continue
            if order < self.order:
                return str(data.get("task", "?"))
        return None


# --- battery -------------------------------------------------------------------------------------------------------


def on_battery() -> bool:
    """True when the machine runs on battery power (Windows). Unknown or desktop: False."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        class PowerStatus(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                        ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

        status = PowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return False
        return status.ACLineStatus == 0
    except Exception:  # noqa: BLE001 - unknown means "not on battery"
        return False


# --- ollama --------------------------------------------------------------------------------------------------------


def ollama_answers(url: str, timeout: float = 3.0) -> bool:
    import httpx

    try:
        return httpx.get(f"{url.rstrip('/')}/api/tags", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


def find_ollama(env_path: str | None = None) -> str | None:
    """`ollama.exe` from PATH, then the standard Windows install folders (a scheduled task may have a shorter PATH)."""
    import shutil

    found = shutil.which("ollama", path=env_path)
    if found:
        return found
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Ollama" / "ollama.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Ollama" / "ollama.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def ollama_model_names(url: str, timeout: float = 5.0) -> list[str]:
    import httpx

    try:
        response = httpx.get(f"{url.rstrip('/')}/api/tags", timeout=timeout)
        return [str(item.get("name", "")) for item in response.json().get("models", [])]
    except (httpx.HTTPError, ValueError, AttributeError):
        return []


def needs_ollama(config: Config) -> bool:
    """True when something in `match` uses Ollama: local embeddings or the optional local LLM tier."""
    return config.embedding.provider == "local" or bool(config.local_llm.enabled)


def _model_present(wanted: str, names: list[str]) -> bool:
    return any(name == wanted or name.split(":")[0] == wanted.split(":")[0] and ":" not in wanted for name in names)


def start_ollama_detached(exe: str, log_file: Path | None = None) -> None:
    """Start `ollama serve` so it outlives this task: no window, own process group, outside the task's job if allowed."""
    output: Any = subprocess.DEVNULL
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        output = open(log_file, "ab")                                # noqa: SIM115 - the child owns it from here
    start_detached([exe, "serve"], stdout=output, stderr=output)


def ensure_ollama(config: Config, *, answers: Callable[[str], bool] = ollama_answers,
                  start: Callable[[], None] | None = None, sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic,
                  models: Callable[[str], list[str]] = ollama_model_names,
                  find: Callable[[], str | None] = find_ollama) -> tuple[bool, str]:
    """Start Ollama when it is not answering, wait until it does, and report the embedding model.

    "Already running" is success. Models are never pulled here. Returns (ok, message)."""
    url = config.schedule.ollama_url
    started = False
    if not answers(url):
        if start is None:
            exe = find()
            if exe is None:
                return False, "ollama.exe not found on PATH or in the standard install folders"

            def start() -> None:
                start_ollama_detached(exe, config.resolve_path(config.schedule.log_dir) / "ollama-serve.log")
        try:
            start()
        except (OSError, FileNotFoundError) as error:
            return False, f"could not start ollama: {error}"
        started = True
        deadline = clock() + config.schedule.ollama_start_wait_seconds
        while not answers(url):
            if clock() >= deadline:
                return False, f"ollama did not answer within {config.schedule.ollama_start_wait_seconds} s"
            sleep(2.0)
    message = "ollama started" if started else "ollama already running"
    if config.embedding.provider == "local":
        wanted = config.embedding.local_model
        if _model_present(wanted, models(url)):
            message += f"; embedding model {wanted} present"
        else:
            return False, (f"{message}; embedding model {wanted} is NOT installed "
                           f"(not pulling automatically: run `ollama pull {wanted}`)")
    return True, message


# --- gmail ---------------------------------------------------------------------------------------------------------


def gmail_token_status(config: Config) -> tuple[bool, str]:
    """(usable, reason). Refreshes the access token when needed; never reads any mail."""
    from jobhunt.alerts.gmail import MailAuthError, TokenStore

    store = TokenStore(config.resolve_path(config.email.credentials_file), config.resolve_path(config.email.token_file))
    try:
        store.bearer_token()
    except MailAuthError as error:
        return False, str(error)
    return True, "token valid"


# --- the runner ----------------------------------------------------------------------------------------------------


@dataclass
class StepResult:
    step: str
    status: str          # ok | failed | skipped | timeout
    detail: str = ""
    exit_code: int | None = None


def _log_handler(path: Path) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    return handler


class TaskRunner:
    def __init__(self, config: Config, connection: sqlite3.Connection, config_path: Path, *,
                 python: str | None = None, battery: Callable[[], bool] = on_battery,
                 gmail_check: Callable[[Config], tuple[bool, str]] = gmail_token_status,
                 ollama: Callable[[Config], tuple[bool, str]] | None = None,
                 run_command: Callable[[list[str], float], tuple[int, str]] | None = None,
                 alive: Callable[[int], bool] = pid_alive, sleep: Callable[[float], None] = time.sleep) -> None:
        self.config = config
        self.connection = connection
        self.config_path = config_path
        if not python:
            from jobhunt.schedule.windows import venv_interpreters
            console, _ = venv_interpreters(config.project_root)
            self.python = str(console) if console.is_file() else _console_python()
        else:
            self.python = python
        self.battery = battery
        self.gmail_check = gmail_check
        self.ollama = ollama or ensure_ollama
        self.run_command = run_command or self._run_subprocess
        self.alive = alive
        self.sleep = sleep

    # -- public ----------------------------------------------------------------------------------

    def run(self, key: str, *, trigger: str = "scheduler") -> int:
        specs = dict(build_task_specs(self.config.schedule))
        if key == "morning-check" and key not in specs:
            from jobhunt.schedule.definitions import TaskSpec, Trigger
            morning_time = getattr(self.config.schedule, "morning_check_time", None) or "09:45"
            specs[key] = TaskSpec(
                "morning-check",
                "Daily morning system check, pipeline health, and budget forecast.",
                False,
                (Trigger("daily", (morning_time,)),),
                (("morning-check",),),
                order=5,
                uses_lock=False,
            )
        if key not in specs:
            raise KeyError(f"unknown task {key!r}; choose from {', '.join(specs)}")
        spec = specs[key]
        settings = self.config.schedule
        log_path = self.config.resolve_path(settings.log_dir) / f"{key}.log"
        handler = _log_handler(log_path)
        log = logging.getLogger(f"jobhunt.scheduled.{key}")
        log.setLevel(logging.INFO)
        log.addHandler(handler)
        started = time.monotonic()
        run_id = self._start_row(key, trigger, log_path)
        status, detail, error, exit_code = "ok", "", "", 0
        lock = RunLock(self.config.resolve_path(settings.lock_file), key,
                       settings.execution_limit_hours * 3600 + 600, alive=self.alive)
        room = WaitingRoom(lock.path.parent / "waiting", key, spec.order, alive=self.alive)
        try:
            if spec.heavy and self.battery():
                status, detail = "skipped", "on battery power (heavy task)"
                log.info("skipped: %s", detail)
            elif not spec.uses_lock:
                status, detail, error, exit_code = self._run_steps(spec, log, started)
            else:
                room.register()
                self.sleep(settings.start_grace_seconds)               # let tasks that started together register
                wait = spec.lock_wait_minutes(settings) * 60
                if not lock.acquire(wait, sleep=self.sleep, ready=lambda: self._my_turn(room, log)):
                    status, detail = "skipped", f"another run holds the lock: {lock.holder_description()}"
                    log.info("skipped: %s", detail)
                else:
                    room.unregister()
                    status, detail, error, exit_code = self._run_steps(spec, log, started)
        except Exception as problem:  # noqa: BLE001 - the row must always be finalized
            status, error, exit_code = "failed", redact(f"{type(problem).__name__}: {problem}")[:500], 1
            log.exception("task crashed")
        finally:
            room.unregister()
            lock.release()
            self._finish_row(run_id, status, detail, error, exit_code, time.monotonic() - started)
            log.removeHandler(handler)
            handler.close()
        return 0 if status in {"ok", "skipped"} else 1

    def _my_turn(self, room: WaitingRoom, log: logging.Logger) -> bool:
        blocker = room.blocked_by()
        if blocker is not None and blocker != getattr(self, "_logged_blocker", None):
            self._logged_blocker = blocker
            log.info("waiting for %s to go first (catch-up order)", blocker)
        return blocker is None

    # -- steps -----------------------------------------------------------------------------------

    def _run_steps(self, spec: TaskSpec, log: logging.Logger, started: float) -> tuple[str, str, str, int]:
        limit = spec.time_limit_hours(self.config.schedule) * 3600      # the lock wait counts against it
        results: list[StepResult] = []
        for step in spec.steps:
            remaining = limit - (time.monotonic() - started)
            if remaining <= 0:
                results.append(StepResult(" ".join(step), "timeout", "time limit reached"))
                break
            result = self._run_step(step, remaining, log, spec.key)
            results.append(result)
            log.info("step %s -> %s %s", result.step, result.status, result.detail)
            if result.status == "skipped" and step[0] == "internal:gmail-check":
                break                                               # nothing else to do; not a failure
        failed = [r for r in results if r.status in {"failed", "timeout"}]
        skipped = [r for r in results if r.status == "skipped"]
        if any(r.status == "timeout" for r in results):
            return "timeout", "; ".join(r.detail for r in failed)[:300], "time limit reached", 1
        if failed and spec.key == "match" and results[0].step.startswith("internal:ollama-ensure")                 and results[0].status == "failed":
            return "failed", "", (f"Ollama unavailable ({results[0].detail}); embeddings were skipped, the other "
                                  f"stages ran" + ("" if len(failed) == 1 else "; match: " + failed[-1].detail))[:500], 1
        if failed:
            return "failed", "", "; ".join(f"{r.step}: {r.detail}" for r in failed)[:500], 1
        if skipped:                                  # only the Gmail pre-check can skip: not a failure
            return "skipped", "; ".join(r.detail for r in skipped)[:300], "", 0
        return "ok", "; ".join(r.detail for r in results if r.detail)[:300], "", 0

    def _run_step(self, step: tuple[str, ...], remaining: float, log: logging.Logger,
                  spec_key: str = "") -> StepResult:
        name = " ".join(step)
        if step[0] == "internal:ollama-ensure":
            if spec_key == "match" and not needs_ollama(self.config):
                return StepResult(name, "ok", "ollama not needed (embedding.provider is not local, local_llm is off)")
            ok, message = self.ollama(self.config)
            return StepResult(name, "ok" if ok else "failed", message)
        if step[0] == "internal:gmail-check":
            usable, reason = self.gmail_check(self.config)
            if usable:
                set_state(self.connection, GMAIL_STATE_KEY, {"expired": False, "checked_at": to_iso(utc_now())})
                return StepResult(name, "ok", "gmail token valid")
            set_state(self.connection, GMAIL_STATE_KEY, {
                "expired": True, "since": to_iso(utc_now()), "reason": redact(reason)[:300],
                "command": REAUTH_COMMAND})
            return StepResult(name, "skipped", "gmail auth expired")
        command = [self.python, "-m", "jobhunt", "--config", str(self.config_path), *step]
        code, output = self.run_command(command, remaining)
        for line in output.splitlines()[-400:]:
            log.info("| %s", redact(line))
        if code == -9:
            return StepResult(name, "timeout", "killed at the time limit", code)
        note = next((line.strip() for line in reversed(output.splitlines())
                     if line.startswith(("ranker-retrain:", "triage:", "triage-student:"))), "")
        detail = f"exit {code}" + (f"; {redact(note)[:200]}" if note else "")       # the retrain decision, for Health
        return StepResult(name, "ok" if code == 0 else "failed", detail, code)

    @staticmethod
    def _run_subprocess(command: list[str], timeout: float) -> tuple[int, str]:
        return run_hidden(command, timeout=timeout)

    # -- database rows ----------------------------------------------------------------------------

    def _start_row(self, key: str, trigger: str, log_path: Path) -> int:
        cursor = self.connection.execute(
            "INSERT INTO scheduled_runs (task, trigger, started_at, status, log_file) VALUES (?, ?, ?, 'running', ?)",
            (key, trigger, to_iso(utc_now()), str(log_path)))
        self.connection.commit()
        return int(cursor.lastrowid)

    def _finish_row(self, run_id: int, status: str, detail: str, error: str, exit_code: int, seconds: float) -> None:
        self.connection.execute(
            """UPDATE scheduled_runs SET finished_at = ?, status = ?, detail = ?, error = ?, exit_code = ?,
                   duration_seconds = ? WHERE id = ?""",
            (to_iso(utc_now()), status, redact(detail)[:500], redact(error)[:500], exit_code, round(seconds, 1), run_id))
        self.connection.commit()


def _console_python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").is_file():
        return str(exe.with_name("python.exe"))
    return str(exe)


def mark_interrupted(connection: sqlite3.Connection, older_than: datetime | None = None) -> int:
    """Rows still 'running' from a process that no longer exists become 'failed' (called on each run start)."""
    cutoff = to_iso(older_than) if older_than else None
    query = "UPDATE scheduled_runs SET status = 'failed', error = 'interrupted (process ended without a result)' " \
            "WHERE status = 'running'"
    params: tuple[Any, ...] = ()
    if cutoff:
        query += " AND started_at < ?"
        params = (cutoff,)
    cursor = connection.execute(query, params)
    connection.commit()
    return cursor.rowcount


__all__ = ["GMAIL_STATE_KEY", "REAUTH_COMMAND", "RunLock", "TaskRunner", "ensure_ollama", "get_state",
           "WaitingRoom", "find_ollama", "needs_ollama", "gmail_token_status", "mark_interrupted", "on_battery", "pid_alive", "redact", "set_state"]
