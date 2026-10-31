"""UTC time helpers. Everything stored in SQLite is ISO-8601 UTC with a trailing Z."""

from __future__ import annotations

import email.utils
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

Clock = Callable[[], datetime]

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_TRAILING_UTC_LABEL = re.compile(r"\s*(UTC|GMT|Z)$", re.IGNORECASE)
_MILLISECOND_EPOCH_THRESHOLD = 100_000_000_000  # 1973 in ms, year 5138 in seconds


def utc_now() -> datetime:
    """Current time as an aware UTC datetime (the default Clock)."""
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """Format an aware or naive (assumed UTC) datetime as YYYY-MM-DDTHH:MM:SSZ."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(_ISO_FORMAT)


def parse_iso(text: str) -> datetime:
    """Parse a timestamp produced by `to_iso` (or any ISO-8601 string) into aware UTC."""
    parsed = _parse_string(text.strip())
    if parsed is None:
        raise ValueError(f"not an ISO-8601 timestamp: {text!r}")
    return parsed


def coerce_timestamp(value: Any) -> datetime | None:
    """Best-effort conversion of the many timestamp shapes ATS APIs emit.

    Handles ISO strings (with offset or Z), "2026-10-05 16:05:18 UTC", plain dates,
    epoch seconds and epoch milliseconds (numbers or numeric strings). Returns None for
    anything unparseable instead of raising: a bad date must never drop a job.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"-?\d+(\.\d+)?", text):
            return _from_epoch(float(text))
        return _parse_string(text)
    return None


def to_utc_iso(value: Any) -> str | None:
    """coerce_timestamp + to_iso in one step (None when unparseable)."""
    moment = coerce_timestamp(value)
    return to_iso(moment) if moment else None


def days_between(earlier: datetime, later: datetime) -> float:
    return (later - earlier) / timedelta(days=1)


def _from_epoch(number: float) -> datetime | None:
    if number != number or number <= 0:  # NaN or non-positive
        return None
    seconds = number / 1000 if number >= _MILLISECOND_EPOCH_THRESHOLD else number
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_string(text: str) -> datetime | None:
    cleaned = _TRAILING_UTC_LABEL.sub("", text)
    if text.endswith(("Z", "z")):
        cleaned = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return _parse_rfc822(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_rfc822(text: str) -> datetime | None:
    """RSS style: 'Tue, 29 Sep 2026 07:30:41 +0000'."""
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
