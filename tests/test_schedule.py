"""v3 daily automation: task definitions, schtasks command generation (mocked), runner guards, Gmail expiry."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import time
import xml.dom.minidom
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jobhunt.config import Config
from jobhunt.schedule import windows
from jobhunt.schedule.definitions import build_task_specs, local_time, next_occurrence, occurrences
from jobhunt.schedule.health import build_schedule_health
from jobhunt.schedule.runner import (
    GMAIL_STATE_KEY, RunLock, TaskRunner, ensure_ollama, get_state, mark_interrupted, redact,
)

UTC = timezone.utc


@pytest.fixture()
def cfg(config: Config, tmp_path: Path) -> Config:
    return dataclasses.replace(config, project_root=tmp_path)


class FakeSchtasks:
    def __init__(self, installed: list[str] | None = None) -> None:
        self.commands: list[list[str]] = []
        self.installed = installed or []
        self.xml_files: dict[str, str] = {}

    def __call__(self, command):  # noqa: ANN001, ANN204
        self.commands.append(list(command))
        if command[1] == "/Create":
            self.xml_files[command[3]] = Path(command[5]).read_bytes().decode("utf-16")
            return subprocess.CompletedProcess(command, 0, "SUCCESS", "")
        if command[1] == "/Query":
            header = '"HostName","TaskName","Next Run Time","Status"\n'
            rows = "".join(f'"PC","\\{name}","N/A","Ready"\n' for name in self.installed)
            return subprocess.CompletedProcess(command, 0, header + rows, "")
        if command[1] == "/Delete":
            return subprocess.CompletedProcess(command, 0, "SUCCESS", "")
        return subprocess.CompletedProcess(command, 1, "", "unexpected")


# --- definitions ---------------------------------------------------------------------------------------------------


def test_the_five_tasks_and_their_commands(cfg: Config) -> None:
    specs = build_task_specs(cfg.schedule)
    assert list(specs) == ["ingest", "ollama-ensure", "match", "ingest-email", "discover"]
    assert specs["ingest"].triggers[0].times == ("06:30", "19:00")
    assert specs["ingest-email"].steps[-1] == ("ingest-email", "--since-days", "2")
    assert specs["ingest-email"].triggers[0].every_hours == 4
    assert specs["discover"].steps == (("discover", "--no-common-crawl"),)
    assert specs["discover"].triggers[0].weekday == "Sunday" and specs["discover"].triggers[0].times == ("03:00",)
    assert specs["match"].steps[0] == ("internal:ollama-ensure",)       # Ollama is up before match


def test_ist_times_convert_to_the_local_clock() -> None:
    hhmm, shift = local_time("06:30", "Asia/Kolkata")
    assert len(hhmm) == 5 and shift in {-1, 0, 1}


def test_occurrences_and_next_run(cfg: Config) -> None:
    specs = build_task_specs(cfg.schedule)
    ist = timezone(timedelta(hours=5, minutes=30))
    day = datetime(2026, 10, 15, 0, 0, tzinfo=ist)
    ingest = occurrences(specs["ingest"], day, day + timedelta(days=1), "Asia/Kolkata")
    assert [m.astimezone(ist).strftime("%H:%M") for m in ingest] == ["06:30", "19:00"]
    email = occurrences(specs["ingest-email"], day, day + timedelta(days=1), "Asia/Kolkata")
    assert len(email) == 6
    sunday = occurrences(specs["discover"], day, day + timedelta(days=7), "Asia/Kolkata")
    assert len(sunday) == 1 and sunday[0].astimezone(ist).strftime("%A %H:%M") == "Sunday 03:00"
    nxt = next_occurrence(specs["match"], datetime(2026, 10, 15, 7, 0, tzinfo=ist), "Asia/Kolkata")
    assert nxt is not None and nxt.astimezone(ist).strftime("%d %H:%M") == "16 06:45"


# --- schtasks command generation (mocked) ----------------------------------------------------------------------------


def test_plan_has_prefixed_names_and_valid_xml_with_the_requested_settings(cfg: Config, tmp_path: Path) -> None:
    planned = windows.plan_tasks(cfg, tmp_path / "config.yaml", python=r"C:\py\pythonw.exe")
    assert [p.name for p in planned] == ["jobhunt-ingest", "jobhunt-ollama-ensure", "jobhunt-match",
                                         "jobhunt-ingest-email", "jobhunt-discover"]
    for task in planned:
        document = xml.dom.minidom.parseString(task.xml.split("?>", 1)[1])
        text = document.toxml()
        assert "<LogonType>InteractiveToken</LogonType>" in text          # only while logged on
        assert "<StartWhenAvailable>true</StartWhenAvailable>" in text    # ASAP after a missed start
        limit = "PT6H" if task.name in {"jobhunt-match", "jobhunt-ingest-email"} else "PT3H"   # queued tasks: 6 h
        assert f"<ExecutionTimeLimit>{limit}</ExecutionTimeLimit>" in text
        assert "<WakeToRun>false</WakeToRun>" in text                     # wake-to-run off by default
        assert "<RunLevel>LeastPrivilege</RunLevel>" in text              # no admin
        heavy = task.spec.heavy
        assert f"<DisallowStartIfOnBatteries>{'true' if heavy else 'false'}</DisallowStartIfOnBatteries>" in text
        assert f"scheduled-run {task.spec.key}" in text


def test_wake_to_run_is_a_config_flag(cfg: Config, tmp_path: Path) -> None:
    woken = dataclasses.replace(cfg, schedule=dataclasses.replace(cfg.schedule, wake_to_run=True))
    xml_text = windows.plan_tasks(woken, tmp_path / "c.yaml", python="p")[0].xml
    assert "<WakeToRun>true</WakeToRun>" in xml_text


def test_email_task_repeats_every_four_hours_and_discover_is_weekly(cfg: Config, tmp_path: Path) -> None:
    planned = {p.spec.key: p for p in windows.plan_tasks(cfg, tmp_path / "c.yaml", python="p")}
    assert "<Interval>PT4H</Interval>" in planned["ingest-email"].xml
    # 03:00 IST on Sunday is Saturday evening in UTC: the weekday follows the machine's time-zone shift.
    from jobhunt.schedule.definitions import WEEKDAYS

    _, shift = local_time(cfg.schedule.discover_time, cfg.schedule.timezone)
    expected_day = WEEKDAYS[(WEEKDAYS.index(cfg.schedule.discover_day) + shift) % 7]
    assert "<ScheduleByWeek>" in planned["discover"].xml and f"<{expected_day} />" in planned["discover"].xml
    assert planned["ingest"].xml.count("<CalendarTrigger>") == 2


def make_fake_venv(root: Path) -> Path:
    scripts = root / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_bytes(b"")
    (scripts / "pythonw.exe").write_bytes(b"")
    return scripts


def test_install_creates_only_prefixed_tasks_via_schtasks(cfg: Config, tmp_path: Path) -> None:
    make_fake_venv(tmp_path)
    fake = FakeSchtasks()
    results = windows.install(cfg, tmp_path / "config.yaml", fake)
    assert all(ok for _, ok, _ in results) and len(results) == 5
    assert all(cmd[:2] == ["schtasks", "/Create"] and cmd[3].startswith("jobhunt-") and "/XML" in cmd and "/F" in cmd
               for cmd in fake.commands)
    assert "scheduled-run ingest" in fake.xml_files["jobhunt-ingest"]


def test_remove_deletes_only_the_prefixed_tasks(cfg: Config) -> None:
    fake = FakeSchtasks(installed=["jobhunt-ingest", "jobhunt-match", "OneDrive Standalone Update", "jobhuntX-other"])
    results = windows.remove(cfg, fake)
    deleted = [cmd[3] for cmd in fake.commands if cmd[1] == "/Delete"]
    assert sorted(deleted) == ["jobhunt-ingest", "jobhunt-match"]
    assert len(results) == 2


def test_cli_install_prints_the_plan_and_needs_yes(cfg: Config, tmp_path: Path, monkeypatch, capsys) -> None:  # noqa: ANN001
    from jobhunt import cli

    make_fake_venv(tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(windows, "install", lambda *a, **k: calls.append(a) or [])
    args = cli.build_parser().parse_args(["schedule-install", "--backend", "windows_task_scheduler"])
    assert cli._command_schedule_install(args, cfg) == 0
    out = capsys.readouterr().out
    assert "jobhunt-ingest" in out and "Nothing was created" in out and calls == []
    args = cli.build_parser().parse_args(
        ["--config", str(tmp_path / "c.yaml"), "schedule-install", "--backend", "windows_task_scheduler", "--yes"])
    cli._command_schedule_install(args, cfg)
    assert len(calls) == 1


# --- lock, battery, log redaction ----------------------------------------------------------------------------------


def test_lock_blocks_a_second_holder_and_steals_a_stale_one(tmp_path: Path) -> None:
    path = tmp_path / "locks" / "x.lock"
    first = RunLock(path, "ingest", 3600, alive=lambda pid: True)
    assert first.try_acquire()
    second = RunLock(path, "match", 3600, alive=lambda pid: True)
    assert not second.try_acquire() and "ingest" in second.holder_description()
    dead = RunLock(path, "match", 3600, alive=lambda pid: False)       # holder process is gone
    assert dead.try_acquire()
    dead.release()
    assert not path.exists()


def test_an_old_lock_is_stale_even_if_the_pid_is_alive(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    path.write_text(json.dumps({"pid": 1, "task": "old", "started_epoch": time.time() - 99999}), encoding="utf-8")
    assert RunLock(path, "n", 3600, alive=lambda pid: True).try_acquire()


def test_secrets_are_redacted_from_logs() -> None:
    line = "GET https://x/v1?key=AIzaSyA1234567890abcdefghijklmnopqrstu&alt=json Authorization: Bearer ya29.abcdefghijklmnopqrstuvwxyz"
    cleaned = redact(line)
    assert "AIza" not in cleaned and "ya29" not in cleaned and "abcdefghijklmnop" not in cleaned


# --- the runner -----------------------------------------------------------------------------------------------------


def make_runner(cfg: Config, connection, tmp_path: Path, **kwargs) -> TaskRunner:  # noqa: ANN001, ANN003
    defaults = dict(battery=lambda: False, gmail_check=lambda c: (True, "ok"), ollama=lambda c: (True, "up"),
                    run_command=lambda command, timeout: (0, "done"), alive=lambda pid: False, sleep=lambda s: None)
    defaults.update(kwargs)
    return TaskRunner(cfg, connection, tmp_path / "config.yaml", python="py", **defaults)


def last_row(connection):  # noqa: ANN001, ANN201
    return connection.execute("SELECT * FROM scheduled_runs ORDER BY id DESC LIMIT 1").fetchone()


def test_a_successful_task_records_status_duration_and_writes_a_log(cfg: Config, connection, tmp_path: Path) -> None:  # noqa: ANN001
    commands: list[list[str]] = []
    runner = make_runner(cfg, connection, tmp_path,
                         run_command=lambda command, timeout: (commands.append(command) or 0, "fetched 12 jobs"))
    assert runner.run("ingest") == 0
    row = last_row(connection)
    assert row["task"] == "ingest" and row["status"] == "ok" and row["duration_seconds"] is not None
    assert [c[-1] for c in commands] == ["ingest", "relevance-run"]
    assert (tmp_path / "data" / "logs" / "ingest.log").read_text(encoding="utf-8").count("fetched 12 jobs") == 2
    assert not (tmp_path / "data" / "locks" / "jobhunt.lock").exists()           # lock released


def test_a_failing_step_is_recorded_as_failed_with_the_error(cfg: Config, connection, tmp_path: Path) -> None:  # noqa: ANN001
    runner = make_runner(cfg, connection, tmp_path, run_command=lambda command, timeout: (2, "boom"))
    assert runner.run("discover") == 1
    row = last_row(connection)
    assert row["status"] == "failed" and "exit 2" in row["error"]


def test_a_heavy_task_does_not_start_on_battery(cfg: Config, connection, tmp_path: Path) -> None:  # noqa: ANN001
    ran: list[int] = []
    runner = make_runner(cfg, connection, tmp_path, battery=lambda: True,
                         run_command=lambda command, timeout: (ran.append(1) or 0, ""))
    assert runner.run("match") == 0
    assert ran == [] and last_row(connection)["status"] == "skipped" and "battery" in last_row(connection)["detail"]


def test_a_light_task_may_run_on_battery(cfg: Config, connection, tmp_path: Path) -> None:  # noqa: ANN001
    runner = make_runner(cfg, connection, tmp_path, battery=lambda: True)
    runner.run("ingest-email")
    assert last_row(connection)["status"] == "ok"


def test_overlapping_runs_are_prevented(cfg: Config, connection, tmp_path: Path) -> None:  # noqa: ANN001
    lock_path = cfg.resolve_path(cfg.schedule.lock_file)
    other = RunLock(lock_path, "ingest", 3600, alive=lambda pid: True)
    assert other.try_acquire()
    quick = dataclasses.replace(cfg, schedule=dataclasses.replace(cfg.schedule, queued_lock_wait_minutes=0))
    ran: list[int] = []
    runner = make_runner(quick, connection, tmp_path, alive=lambda pid: True,
                         run_command=lambda command, timeout: (ran.append(1) or 0, ""))
    assert runner.run("ingest-email") == 0
    row = last_row(connection)
    assert ran == [] and row["status"] == "skipped" and "lock" in row["detail"]
    other.release()


def test_an_expired_gmail_token_skips_only_the_email_task(cfg: Config, connection, tmp_path: Path) -> None:  # noqa: ANN001
    ran: list[list[str]] = []
    runner = make_runner(cfg, connection, tmp_path,
                         gmail_check=lambda c: (False, "Google rejected the refresh token (expired or revoked)"),
                         run_command=lambda command, timeout: (ran.append(command) or 0, ""))
    assert runner.run("ingest-email") == 0                    # not a failure
    row = last_row(connection)
    assert row["status"] == "skipped" and row["detail"] == "gmail auth expired"
    assert ran == []                                          # no ingest-email subprocess was started
    state = get_state(connection, GMAIL_STATE_KEY)
    assert state["expired"] is True and "gmail-auth" in state["command"]
    # the other tasks carry on normally
    assert runner.run("ingest") == 0 and last_row(connection)["status"] == "ok"
    # a valid token clears the banner
    fixed = make_runner(cfg, connection, tmp_path)
    fixed.run("ingest-email")
    assert get_state(connection, GMAIL_STATE_KEY)["expired"] is False


def test_the_health_report_shows_the_gmail_banner_and_missed_runs(cfg: Config, connection, tmp_path: Path) -> None:  # noqa: ANN001
    make_runner(cfg, connection, tmp_path, gmail_check=lambda c: (False, "revoked")).run("ingest-email")
    now = datetime.now(UTC)
    old = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    connection.execute("INSERT INTO scheduled_runs (task, started_at, finished_at, status, duration_seconds) "
                       "VALUES ('ingest', ?, ?, 'ok', 60)", (old, old))
    connection.commit()
    report = build_schedule_health(connection, cfg, now)
    assert report["gmail_auth_expired"] is True and "gmail-auth" in report["gmail_reauth_command"]
    by_task = {t["task"]: t for t in report["tasks"]}
    assert by_task["ingest"]["missed_runs"] >= 3 and by_task["ingest"]["missed_warning"] is True
    assert by_task["ingest"]["next_run_at"] is not None
    assert by_task["match"]["last_run_at"] is None and by_task["match"]["missed_warning"] is False


def test_a_crashed_run_row_is_finalized(connection) -> None:  # noqa: ANN001
    connection.execute("INSERT INTO scheduled_runs (task, started_at, status) VALUES ('ingest', '2026-10-01T00:00:00Z', 'running')")
    connection.commit()
    assert mark_interrupted(connection) == 1
    assert last_row(connection)["status"] == "failed"


# --- ollama -----------------------------------------------------------------------------------------------------------


def test_ollama_is_started_only_when_not_answering(cfg: Config) -> None:
    started: list[int] = []
    ok, message = ensure_ollama(cfg, answers=lambda url: True, start=lambda: started.append(1))
    assert ok and started == [] and "already" in message

    state = {"up": False}

    def start() -> None:
        started.append(1)
        state["up"] = True

    ok, message = ensure_ollama(cfg, answers=lambda url: state["up"], start=start, sleep=lambda s: None)
    assert ok and started == [1] and "started" in message


def test_ollama_that_never_answers_fails_after_the_wait(cfg: Config) -> None:
    ticks = iter(range(0, 10_000, 30))
    ok, message = ensure_ollama(cfg, answers=lambda url: False, start=lambda: None, sleep=lambda s: None,
                                clock=lambda: float(next(ticks)))
    assert not ok and "did not answer" in message


def test_match_and_email_queue_behind_a_long_ingest_instead_of_skipping(cfg: Config, connection, tmp_path: Path) -> None:  # noqa: ANN001
    assert cfg.schedule.queued_lock_wait_minutes == 240 and cfg.schedule.queued_execution_limit_hours == 6
    for task in ("match", "ingest-email"):
        lock_path = cfg.resolve_path(cfg.schedule.lock_file)
        ingest = RunLock(lock_path, "ingest", 3 * 3600 + 600, alive=lambda pid: True)
        assert ingest.try_acquire()
        waited: list[float] = []

        def release_after_two_hours(seconds: float) -> None:
            waited.append(seconds)
            if len(waited) * seconds >= 2.2 * 3600:        # the ingest finishes after about 2.2 h (simulated)
                ingest.release()

        # a fake clock makes the 2.2 h wait instantaneous while `acquire` still measures its 4 h deadline
        ticks = {"now": 0.0}
        real_monotonic = time.monotonic
        time.monotonic = lambda: ticks["now"]                    # type: ignore[assignment]
        try:
            def sleeper(seconds: float) -> None:
                ticks["now"] += seconds
                release_after_two_hours(seconds)

            ran: list[list[str]] = []
            runner = make_runner(cfg, connection, tmp_path, alive=lambda pid: True, sleep=sleeper,
                                 run_command=lambda command, timeout: (ran.append(command) or 0, "done"))
            assert runner.run(task) == 0
        finally:
            time.monotonic = real_monotonic                      # type: ignore[assignment]
        row = last_row(connection)
        assert row["task"] == task and row["status"] == "ok", row["detail"]
        assert ran, "the task must have run after the lock was released"
        assert sum(waited) >= 2.2 * 3600


def test_a_queued_task_still_skips_when_the_lock_outlasts_four_hours(cfg: Config, connection, tmp_path: Path) -> None:  # noqa: ANN001
    holder = RunLock(cfg.resolve_path(cfg.schedule.lock_file), "ingest", 99 * 3600, alive=lambda pid: True)
    assert holder.try_acquire()
    ticks = {"now": 0.0}
    real_monotonic = time.monotonic
    time.monotonic = lambda: ticks["now"]                        # type: ignore[assignment]
    try:
        def sleeper(seconds: float) -> None:
            ticks["now"] += seconds

        runner = make_runner(cfg, connection, tmp_path, alive=lambda pid: True, sleep=sleeper)
        runner.run("match")
    finally:
        time.monotonic = real_monotonic                          # type: ignore[assignment]
        holder.release()
    row = last_row(connection)
    assert row["status"] == "skipped" and "lock" in row["detail"]
    assert 4 * 3600 <= ticks["now"] <= 4 * 3600 + 60       # includes the 15 s start grace


def test_task_xml_gives_queued_tasks_a_six_hour_limit_and_others_three(cfg: Config) -> None:
    from jobhunt.schedule.definitions import build_task_specs
    from jobhunt.schedule.windows import task_xml

    specs = build_task_specs(cfg.schedule)
    xml = {key: task_xml(spec, cfg, python="py", arguments="a", working_directory="wd") for key, spec in specs.items()}
    assert "<ExecutionTimeLimit>PT6H</ExecutionTimeLimit>" in xml["match"]
    assert "<ExecutionTimeLimit>PT6H</ExecutionTimeLimit>" in xml["ingest-email"]
    assert "<ExecutionTimeLimit>PT3H</ExecutionTimeLimit>" in xml["ingest"]
    assert "<ExecutionTimeLimit>PT3H</ExecutionTimeLimit>" in xml["discover"]
