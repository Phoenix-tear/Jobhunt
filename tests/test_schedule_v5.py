"""v5 scheduler fixes: venv interpreter, catch-up order, ollama start and fail-soft match."""

from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path

import pytest

from jobhunt.config import Config
from jobhunt.db import apply_migrations, connect
from jobhunt.schedule import runner as runner_mod
from jobhunt.schedule import windows
from jobhunt.schedule.runner import TaskRunner
from tests.test_schedule import FakeSchtasks, cfg, last_row, make_fake_venv, make_runner  # noqa: F401  (cfg: fixture)


def test_tasks_launch_the_repo_venv_interpreter_with_the_repo_as_working_directory(cfg: Config, tmp_path: Path) -> None:
    scripts = make_fake_venv(tmp_path)
    planned = windows.plan_tasks(cfg, tmp_path / "config.yaml")
    assert len(planned) == 5
    for task in planned:
        assert task.command == (str(scripts / "pythonw.exe"),)
        assert f"<Command>{scripts / 'pythonw.exe'}</Command>" in task.xml
        assert f"<WorkingDirectory>{cfg.project_root}</WorkingDirectory>" in task.xml
        assert "Python314" not in task.xml


def test_install_refuses_when_the_venv_interpreter_is_missing(cfg: Config, tmp_path: Path) -> None:
    fake = FakeSchtasks()
    with pytest.raises(windows.ScheduleInstallError, match="virtualenv interpreter"):
        windows.install(cfg, tmp_path / "config.yaml", fake)
    assert fake.commands == []                                           # nothing was created


def test_a_console_python_is_used_when_there_is_no_pythonw(cfg: Config, tmp_path: Path) -> None:
    scripts = make_fake_venv(tmp_path)
    (scripts / "pythonw.exe").unlink()
    assert windows.venv_python(tmp_path) == str(scripts / "python.exe")


def test_tasks_that_start_together_take_the_lock_in_catch_up_order(cfg: Config, connection, tmp_path: Path) -> None:  # noqa: ANN001
    """match, email and ingest all start at once, in the wrong order: ingest first, then match, then email."""
    started: list[str] = []
    guard = threading.Lock()
    db_file = tmp_path / "order.sqlite3"
    setup = connect(db_file)
    apply_migrations(setup, Path(__file__).resolve().parents[1] / "migrations")
    setup.close()

    def make(key: str) -> TaskRunner:
        def run_command(command, timeout):  # noqa: ANN001, ANN202
            with guard:
                started.append(f"{key}:{command[-1]}")
            time.sleep(0.05)
            return 0, "done"

        fresh = connect(db_file, check_same_thread=False)
        return make_runner(cfg, fresh, tmp_path, alive=lambda pid: True, sleep=lambda seconds: time.sleep(0.3 if seconds >= 15 else 0.02),   # the grace period is the long one
                           run_command=run_command)

    order_in = ("match", "ingest-email", "ingest")
    runners = {key: make(key) for key in order_in}
    threads = [threading.Thread(target=runners[key].run, args=(key,)) for key in order_in]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    first_seen = [entry.split(":")[0] for entry in started]
    assert first_seen.index("ingest") < first_seen.index("match") < first_seen.index("ingest-email")
    check = connect(db_file)
    rows = check.execute("SELECT task, status FROM scheduled_runs").fetchall()
    assert {r["task"]: r["status"] for r in rows} == {"ingest": "ok", "match": "ok", "ingest-email": "ok"}


def test_waiting_room_ignores_dead_and_equal_order_waiters(tmp_path: Path) -> None:
    early = runner_mod.WaitingRoom(tmp_path, "ingest", 1, alive=lambda pid: True)
    late = runner_mod.WaitingRoom(tmp_path, "match", 2, alive=lambda pid: True)
    early.register()
    late.register()
    assert late.blocked_by() == "ingest" and early.blocked_by() is None
    dead = runner_mod.WaitingRoom(tmp_path, "match", 2, alive=lambda pid: False)
    assert dead.blocked_by() is None                           # the earlier task's process is gone
    early.unregister()
    assert late.blocked_by() is None


def ollama_config(cfg: Config, provider: str = "local") -> Config:
    return dataclasses.replace(cfg, embedding=dataclasses.replace(cfg.embedding, provider=provider,
                                                                  local_model="mxbai-embed-large"))


def test_ensure_ollama_treats_already_running_as_success_and_reports_the_model(cfg: Config) -> None:
    ok, message = runner_mod.ensure_ollama(ollama_config(cfg), answers=lambda url: True,
                                           models=lambda url: ["mxbai-embed-large:latest", "qwen3:4b"])
    assert ok and "already running" in message and "mxbai-embed-large present" in message


def test_ensure_ollama_reports_a_missing_embedding_model_without_pulling(cfg: Config) -> None:
    started: list[int] = []
    ok, message = runner_mod.ensure_ollama(ollama_config(cfg), answers=lambda url: True, models=lambda url: ["qwen3:4b"],
                                           start=lambda: started.append(1))
    assert not ok and "NOT installed" in message and "ollama pull mxbai-embed-large" in message and started == []


def test_ensure_ollama_starts_and_polls_up_to_the_configured_wait(cfg: Config) -> None:
    clock = {"now": 0.0}

    def advance(seconds: float) -> None:
        clock["now"] += seconds

    ok, message = runner_mod.ensure_ollama(
        ollama_config(cfg), answers=lambda url: clock["now"] >= 150, start=lambda: None,
        models=lambda url: ["mxbai-embed-large:latest"], sleep=advance, clock=lambda: clock["now"])
    assert cfg.schedule.ollama_start_wait_seconds == 180 and ok and message.startswith("ollama started")
    clock["now"] = 0.0
    giving_up = runner_mod.ensure_ollama(ollama_config(cfg), answers=lambda url: False, start=lambda: None,
                                         sleep=advance, clock=lambda: clock["now"])
    assert giving_up == (False, "ollama did not answer within 180 s")


def test_ensure_ollama_reports_a_missing_executable(cfg: Config) -> None:
    ok, message = runner_mod.ensure_ollama(ollama_config(cfg), answers=lambda url: False, find=lambda: None)
    assert not ok and "not found" in message


def test_find_ollama_checks_the_standard_install_folder(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    exe = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert runner_mod.find_ollama(env_path=str(tmp_path / "empty")) == str(exe)


def test_match_does_not_wait_for_ollama_when_embeddings_are_not_local(cfg: Config, connection, tmp_path: Path) -> None:  # noqa: ANN001
    asked: list[int] = []
    gemini = ollama_config(cfg, "gemini")
    gemini = dataclasses.replace(gemini, local_llm=dataclasses.replace(gemini.local_llm, enabled=False))
    runner = make_runner(gemini, connection, tmp_path, ollama=lambda c: (asked.append(1) or (False, "down")))
    assert runner.run("match") == 0
    assert asked == [] and last_row(connection)["status"] == "ok" and "not needed" in last_row(connection)["detail"]


def test_match_with_ollama_down_fails_soft_with_a_clear_reason(cfg: Config, connection, tmp_path: Path) -> None:  # noqa: ANN001
    local = ollama_config(cfg, "local")
    runner = make_runner(local, connection, tmp_path, ollama=lambda c: (False, "ollama did not answer within 180 s"),
                         run_command=lambda command, timeout: (1, "embedding.provider is local but Ollama is not running"))
    assert runner.run("match") == 1
    row = last_row(connection)
    assert row["status"] == "failed" and "Ollama unavailable" in row["error"]
    assert "180 s" in row["error"] and "other stages ran" in row["error"]
    assert not (tmp_path / "data" / "locks" / "jobhunt.lock").exists()           # nothing left behind


def test_command_hints_are_venv_qualified_outside_the_venv(monkeypatch) -> None:  # noqa: ANN001
    from jobhunt import cmdhint

    monkeypatch.setattr(cmdhint, "in_venv", lambda: False)
    commands = frozenset({"gmail-auth", "train-ranker", "profile-build"})
    assert cmdhint.command("gmail-auth") == r".\.venv\Scripts\python.exe -m jobhunt gmail-auth"
    assert cmdhint.qualify("run `python -m jobhunt profile-build` first", commands) == \
        r"run `.\.venv\Scripts\python.exe -m jobhunt profile-build` first"
    assert cmdhint.qualify("Run `jobhunt gmail-auth` again", commands) == \
        r"Run `.\.venv\Scripts\python.exe -m jobhunt gmail-auth` again"
    assert cmdhint.qualify("no trained model (run train-ranker)", commands) == \
        r"no trained model (run .\.venv\Scripts\python.exe -m jobhunt train-ranker)"
    already = r"use .\.venv\Scripts\python.exe -m jobhunt gmail-auth"
    assert cmdhint.qualify(already, commands) == already                        # never qualified twice
    monkeypatch.setattr(cmdhint, "in_venv", lambda: True)
    assert cmdhint.qualify("run `python -m jobhunt profile-build`", commands) == "run `python -m jobhunt profile-build`"
    assert cmdhint.command("gmail-auth") == "python -m jobhunt gmail-auth"


def test_outside_the_venv_a_warning_names_the_missing_extras(monkeypatch, caplog) -> None:  # noqa: ANN001
    import logging

    from jobhunt import cmdhint

    monkeypatch.setattr(cmdhint, "in_venv", lambda: False)
    monkeypatch.setattr(cmdhint, "venv_exists", lambda: True)
    monkeypatch.setattr(cmdhint, "_missing_extras", lambda: ["google.auth (gmail)"])
    with caplog.at_level(logging.WARNING):
        assert cmdhint.warn_if_outside_venv(logging.getLogger("t")) is True
    assert "google.auth (gmail)" in caplog.text and ".venv" in caplog.text
    monkeypatch.setattr(cmdhint, "_missing_extras", lambda: [])
    assert cmdhint.warn_if_outside_venv(logging.getLogger("t")) is False
