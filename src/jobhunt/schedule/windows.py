"""Windows Task Scheduler integration through `schtasks` (user level, no admin).

Tasks are created from an XML definition because the settings that matter here (run only when logged on, start as
soon as possible after a missed start, stop after N hours, skip on battery, wake to run) are not all reachable with
plain `schtasks /Create` flags. `schtasks` is injected so tests record the commands without touching the machine.
Only tasks whose name starts with the configured prefix are ever created, replaced or deleted.
"""

from __future__ import annotations

import csv
import io
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from jobhunt.config import Config
from jobhunt.errors import JobHuntError
from jobhunt.schedule.definitions import (
    TaskSpec, Trigger, build_task_specs, describe_triggers, local_time,
)

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def default_runner(command: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(command), capture_output=True, text=True, timeout=60, check=False,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@dataclass(frozen=True)
class PlannedTask:
    spec: TaskSpec
    name: str
    xml: str
    command: tuple[str, ...]            # what the task will execute
    arguments: str


class ScheduleInstallError(JobHuntError):
    """The scheduled tasks cannot be installed as configured (for example the repo virtualenv is missing)."""


def venv_interpreters(project_root: Path) -> tuple[Path, Path]:
    """(python.exe, pythonw.exe) of the repo's `.venv`. The files may not exist."""
    scripts = Path(project_root) / ".venv" / "Scripts"
    return scripts / "python.exe", scripts / "pythonw.exe"


def venv_python(project_root: Path) -> str:
    """The interpreter every scheduled task must launch: the repo `.venv` (windowless when available).

    The system Python lacks the gmail, pypdf and other packages the venv has, so a task launched with it fails in
    ways that look like expired credentials. Raises ScheduleInstallError when the venv interpreter is missing."""
    console, windowless = venv_interpreters(project_root)
    if not console.is_file():
        raise ScheduleInstallError(
            f"the virtualenv interpreter {console} does not exist, so the scheduled tasks would run the system Python "
            "(which lacks the gmail and other extras). Create it first: python -m venv .venv, then "
            r".\.venv\Scripts\python.exe -m pip install -e '.[api,gmail,matching,dev]'")
    return str(windowless if windowless.is_file() else console)


def windowless_python() -> str:
    """pythonw.exe next to the running interpreter (no console window pops up), else the interpreter itself."""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.is_file() else exe)


def _iso(day_offset: int, hhmm: str) -> str:
    day = (datetime.now() + timedelta(days=day_offset)).date()
    return f"{day.isoformat()}T{hhmm}:00"


def _trigger_xml(trigger: Trigger, zone_name: str) -> list[str]:
    out: list[str] = []
    for hhmm in trigger.times:
        local, shift = local_time(hhmm, zone_name)
        start = _iso(shift, local)
        if trigger.kind == "weekly":
            from jobhunt.schedule.definitions import WEEKDAYS

            index = (WEEKDAYS.index(trigger.weekday) + shift) % 7
            day = WEEKDAYS[index]
            out.append(
                f"    <CalendarTrigger><StartBoundary>{start}</StartBoundary><Enabled>true</Enabled>"
                f"<ScheduleByWeek><DaysOfWeek><{day} /></DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>"
                f"</CalendarTrigger>")
        else:
            repetition = ""
            if trigger.kind == "every_hours":
                repetition = (f"<Repetition><Interval>PT{trigger.every_hours}H</Interval><Duration>P1D</Duration>"
                              f"<StopAtDurationEnd>false</StopAtDurationEnd></Repetition>")
            out.append(
                f"    <CalendarTrigger>{repetition}<StartBoundary>{start}</StartBoundary><Enabled>true</Enabled>"
                f"<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>")
    return out


def task_xml(spec: TaskSpec, config: Config, *, python: str, arguments: str, working_directory: str) -> str:
    settings = config.schedule
    triggers = [line for trigger in spec.triggers for line in _trigger_xml(trigger, settings.timezone)]
    on_battery_blocked = "true" if spec.heavy else "false"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(spec.description)} (managed by jobhunt; remove with `schedule-remove`)</Description>
  </RegistrationInfo>
  <Triggers>
{chr(10).join(triggers)}
  </Triggers>
  <Principals>
    <Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>{on_battery_blocked}</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <WakeToRun>{'true' if settings.wake_to_run else 'false'}</WakeToRun>
    <ExecutionTimeLimit>PT{spec.time_limit_hours(settings)}H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(python)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(working_directory)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def plan_tasks(config: Config, config_path: Path, *, python: str | None = None) -> list[PlannedTask]:
    """Everything `schedule-install` would create, without touching the machine."""
    python = python or venv_python(config.project_root)
    prefix = config.schedule.task_prefix
    planned: list[PlannedTask] = []
    for spec in build_task_specs(config.schedule).values():
        arguments = f'-m jobhunt --config "{config_path}" scheduled-run {spec.key}'
        xml = task_xml(spec, config, python=python, arguments=arguments, working_directory=str(config.project_root))
        planned.append(PlannedTask(spec, spec.task_name(prefix), xml, (python,), arguments))
    return planned


def create_command(name: str, xml_file: Path) -> list[str]:
    return ["schtasks", "/Create", "/TN", name, "/XML", str(xml_file), "/F"]


def delete_command(name: str) -> list[str]:
    return ["schtasks", "/Delete", "/TN", name, "/F"]


QUERY_COMMAND = ["schtasks", "/Query", "/FO", "CSV", "/V"]


def describe_plan(planned: list[PlannedTask], config: Config) -> str:
    lines = [f"schedule-install will create {len(planned)} Windows Task Scheduler task(s) for this user (no admin):", ""]
    for task in planned:
        lines.append(f"  {task.name}")
        lines.append(f"      when:     {describe_triggers(task.spec, config.schedule.timezone)}")
        lines.append(f"      runs:     {' ; '.join(' '.join(step) for step in task.spec.steps)}")
        lines.append(f"      command:  {task.command[0]} {task.arguments}")
        lines.append(f"      settings: only while logged on, run ASAP after a missed start, stop after "
                     f"{task.spec.time_limit_hours(config.schedule)} h, runs on battery, wake-to-run ON")
    lines += ["", "A lock file stops two tasks from overlapping; each task logs to data/logs/ and to the "
                  "`scheduled_runs` table. Existing tasks with these names are replaced; nothing else is touched."]
    return "\n".join(lines)


def install(config: Config, config_path: Path, runner: Runner = default_runner, *, skip_unconfigured_email: bool = False) -> list[tuple[str, bool, str]]:
    """Create (or replace) every planned task. Returns (name, ok, message) per task."""
    results: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory() as folder:
        for task in plan_tasks(config, config_path):
            if not task.name.startswith(config.schedule.task_prefix):          # defensive: never a foreign name
                results.append((task.name, False, "refused: name lacks the prefix"))
                continue
            if skip_unconfigured_email and task.spec.key == "ingest-email":
                from jobhunt.doctor import check_gmail_configured
                gmail_ok, gmail_reason = check_gmail_configured(config)
                if not gmail_ok:
                    results.append((task.name, True, f"skipped: Gmail not configured ({gmail_reason})"))
                    continue
            xml_file = Path(folder) / f"{task.name}.xml"
            xml_file.write_bytes(b"\xff\xfe" + task.xml.encode("utf-16-le"))   # UTF-16 LE with BOM, as schtasks wants
            done = runner(create_command(task.name, xml_file))
            results.append((task.name, done.returncode == 0, (done.stdout + done.stderr).strip()[:300]))
    return results


def parse_query(output: str, prefix: str) -> list[dict[str, str]]:
    """Rows of `schtasks /Query /FO CSV /V` whose task name (leading backslash removed) has the prefix."""
    rows: list[dict[str, str]] = []
    reader = csv.reader(io.StringIO(output))
    header: list[str] | None = None
    for row in reader:
        if not row:
            continue
        if row[0] == "HostName":
            header = row
            continue
        if header is None or len(row) != len(header):
            continue
        record = dict(zip(header, row))
        name = record.get("TaskName", "").lstrip("\\")
        if name.startswith(prefix):
            record["TaskName"] = name
            rows.append(record)
    unique: dict[str, dict[str, str]] = {}
    for record in rows:                       # a multi-trigger task is listed once per trigger
        unique.setdefault(record["TaskName"], record)
    return list(unique.values())


def installed_tasks(config: Config, runner: Runner = default_runner) -> list[dict[str, str]]:
    done = runner(QUERY_COMMAND)
    if done.returncode != 0:
        return []
    return parse_query(done.stdout, config.schedule.task_prefix)


def remove(config: Config, runner: Runner = default_runner) -> list[tuple[str, bool, str]]:
    """Delete every task with the prefix, and only those."""
    prefix = config.schedule.task_prefix
    results: list[tuple[str, bool, str]] = []
    for record in installed_tasks(config, runner):
        name = record["TaskName"]
        if not name.startswith(prefix):
            continue
        done = runner(delete_command(name))
        results.append((name, done.returncode == 0, (done.stdout + done.stderr).strip()[:200]))
    return results


def remove_task(config: Config, task_name_or_key: str, runner: Runner = default_runner) -> tuple[str, bool, str]:
    """Delete a single task by key or full task name."""
    prefix = config.schedule.task_prefix
    name = task_name_or_key if task_name_or_key.startswith(prefix) else f"{prefix}{task_name_or_key}"
    done = runner(delete_command(name))
    return (name, done.returncode == 0, (done.stdout + done.stderr).strip()[:200])


__all__ = ["PlannedTask", "QUERY_COMMAND", "create_command", "default_runner", "delete_command", "describe_plan",
           "install", "installed_tasks", "parse_query", "plan_tasks", "remove", "remove_task", "task_xml", "windowless_python",
           "ScheduleInstallError", "venv_interpreters", "venv_python"]
