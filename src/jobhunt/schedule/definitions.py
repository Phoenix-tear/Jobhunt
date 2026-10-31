"""What runs when. Pure data and date arithmetic: no Windows calls, no database (so it is easy to test).

Every task is a short list of steps. A step is either a CLI command (`("ingest",)` runs `python -m jobhunt ingest`)
or an internal action (`"internal:ollama-ensure"`, `"internal:gmail-check"`). Times are written in the configured
time zone (IST); `local_time` converts them to the machine's clock for Task Scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jobhunt.config import ScheduleSettings

EMAIL_ANCHOR = "00:20"          # the 4-hourly e-mail run repeats from here (00:20, 04:20, ...)
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

Step = tuple[str, ...]


@dataclass(frozen=True)
class Trigger:
    kind: str                        # daily | every_hours | weekly
    times: tuple[str, ...] = ()      # "HH:MM" in the schedule's time zone (anchor time for every_hours)
    every_hours: int = 0
    weekday: str = ""


@dataclass(frozen=True)
class TaskSpec:
    key: str                         # "ingest"; the Windows task is `<prefix><key>`
    description: str
    heavy: bool                      # heavy tasks do not start on battery and may wait long for the lock
    triggers: tuple[Trigger, ...]
    steps: tuple[Step, ...]
    order: int = 5                   # catch-up order when several tasks start together: lower runs first
    uses_lock: bool = True           # False: harmless next to other tasks (no database writes beyond its own row)
    queued: bool = False             # waits behind the run lock for `queued_lock_wait_minutes` instead of skipping

    def lock_wait_minutes(self, settings: ScheduleSettings) -> int:
        if self.queued:
            return settings.queued_lock_wait_minutes
        return settings.heavy_lock_wait_minutes if self.heavy else settings.light_lock_wait_minutes

    def time_limit_hours(self, settings: ScheduleSettings) -> int:
        if self.queued:
            return max(settings.execution_limit_hours, settings.queued_execution_limit_hours)
        return settings.execution_limit_hours

    def task_name(self, prefix: str) -> str:
        return f"{prefix}{self.key}"


def build_task_specs(settings: ScheduleSettings) -> dict[str, TaskSpec]:
    specs = [
        TaskSpec("ingest", "Fetch new jobs from every enabled source, then apply the relevance gate.", True,
                 (Trigger("daily", settings.ingest_times),), (("ingest",), ("relevance-run",)), order=1),
        TaskSpec("ollama-ensure", "Start Ollama if it is not running and wait until it answers (before match).", False,
                 (Trigger("daily", (settings.ollama_ensure_time,)),), (("internal:ollama-ensure",),), order=0, uses_lock=False),
        TaskSpec("match", "Nightly matching: backfill-remote, relevance, embeddings, local ranker, Gemini scoring.", True,
                 (Trigger("daily", (settings.match_time,)),), (("internal:ollama-ensure",), ("match",)), queued=True, order=2),
        TaskSpec("ingest-email", f"Read job-alert e-mails of the last {settings.email_since_days} days "
                 "(skipped, not failed, when the Gmail token expired).", False,
                 (Trigger("every_hours", (EMAIL_ANCHOR,), every_hours=settings.email_every_hours),),
                 (("internal:gmail-check",), ("ingest-email", "--since-days", str(settings.email_since_days))),
                 queued=True, order=3),
        TaskSpec("discover", "Weekly discovery of new company boards (Common Crawl off).", True,
                 (Trigger("weekly", (settings.discover_time,), weekday=settings.discover_day),),
                 (("discover", "--no-common-crawl"),), order=4),
    ]
    triage_time = getattr(settings, "triage_time", None)
    if triage_time:
        specs.append(
            TaskSpec("triage", "Local-LLM-first triage: main reader of postings with high-stakes confirmation.", True,
                     (Trigger("daily", (triage_time,)),),
                     (("internal:ollama-ensure",), ("triage-llm-run", "--apply")), queued=True, order=3)
        )
    morning_check_time = getattr(settings, "morning_check_time", None)
    if morning_check_time:
        specs.append(
            TaskSpec("morning-check", "Daily morning system check, pipeline health, and budget forecast.", False,
                     (Trigger("daily", (morning_check_time,)),),
                     (("morning-check",),), order=5, uses_lock=False)
        )
    return {spec.key: spec for spec in specs}


# Windows has no IANA database unless the `tzdata` package is installed; these zones have no daylight saving.
_FIXED_OFFSETS = {"Asia/Kolkata": 330, "Asia/Calcutta": 330, "UTC": 0, "Etc/UTC": 0}


def zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if name in _FIXED_OFFSETS:
            return timezone(timedelta(minutes=_FIXED_OFFSETS[name]), name)
        raise


def local_time(hhmm: str, zone_name: str, *, on: datetime | None = None) -> tuple[str, int]:
    """(HH:MM on this machine's clock, day shift) for a time given in `zone_name`. Day shift is -1/0/+1."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    base = (on or datetime.now(zone(zone_name))).astimezone(zone(zone_name))
    source = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    local = source.astimezone()
    return local.strftime("%H:%M"), (local.date() - source.date()).days


def occurrences(spec: TaskSpec, start: datetime, end: datetime, zone_name: str) -> list[datetime]:
    """Every time the task is due in the half-open window (start, end] (timezone-aware datetimes)."""
    tz = zone(zone_name)
    start_l, end_l = start.astimezone(tz), end.astimezone(tz)
    found: list[datetime] = []
    day = start_l.date() - timedelta(days=1)
    while day <= end_l.date():
        for trigger in spec.triggers:
            if trigger.kind == "weekly" and WEEKDAYS[day.weekday()].lower() != trigger.weekday.lower():
                continue
            for hhmm in trigger.times:
                hour, minute = (int(p) for p in hhmm.split(":"))
                first = datetime.combine(day, time(hour, minute), tzinfo=tz)
                if trigger.kind == "every_hours":
                    step = max(1, trigger.every_hours)
                    moment = first
                    while moment.date() == day:
                        found.append(moment)
                        moment += timedelta(hours=step)
                else:
                    found.append(first)
        day += timedelta(days=1)
    return sorted({m for m in found if start_l < m <= end_l})


def next_occurrence(spec: TaskSpec, now: datetime, zone_name: str) -> datetime | None:
    window_end = now + timedelta(days=8)
    upcoming = occurrences(spec, now, window_end, zone_name)
    return upcoming[0] if upcoming else None


def describe_triggers(spec: TaskSpec, zone_name: str) -> str:
    parts = []
    for trigger in spec.triggers:
        if trigger.kind == "daily":
            parts.append("daily at " + ", ".join(trigger.times))
        elif trigger.kind == "weekly":
            parts.append(f"every {trigger.weekday} at {', '.join(trigger.times)}")
        else:
            parts.append(f"every {trigger.every_hours} h from {trigger.times[0]}")
    return "; ".join(parts) + f" ({zone_name})"


__all__ = ["EMAIL_ANCHOR", "Step", "TaskSpec", "Trigger", "WEEKDAYS", "build_task_specs", "describe_triggers",
           "local_time", "next_occurrence", "occurrences"]
