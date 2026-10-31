"""Posting signals: each one with a fixture, plus the null cases. Signals are facts, never a score change."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from jobhunt.api import signals
from jobhunt.api.signals import apply_url_shape, compute_signals, has_caution

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def add_job(connection: sqlite3.Connection, *, source: str = "greenhouse", posted_at: str | None = "2026-10-06T00:00:00Z",
            first_seen: str = "2026-10-06T00:00:00Z", last_seen: str = "2026-10-16T00:00:00Z",
            closed_at: str | None = None, missed: int = 0, status: str = "open", title: str = "Software Engineer",
            company: str = "Acme", employment: str = "full_time", level: str = "entry", fingerprint: str = "fp-1",
            apply_url: str | None = "https://boards.greenhouse.io/acme/jobs/1", raw: dict | None = None,
            source_job_id: str | None = None, extra_sources: tuple[str, ...] = ()) -> int:
    cursor = connection.execute(
        """INSERT INTO jobs (fingerprint, canonical_apply_url, title, company_name, employment_type, experience_level,
                             posted_at, first_seen_at, last_seen_at, closed_at, missed_runs, status, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (fingerprint, apply_url, title, company, employment, level, posted_at, first_seen, last_seen, closed_at,
         missed, status, json.dumps(raw) if raw is not None else None))
    job_id = int(cursor.lastrowid)
    for index, name in enumerate((source, *extra_sources)):
        connection.execute(
            """INSERT INTO job_sightings (job_id, source, source_job_id, source_url, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (job_id, name, f"{source_job_id or job_id}-{index}", apply_url, first_seen, last_seen))
    return job_id


def by_id(result: dict[int, list[dict[str, str]]], job_id: int) -> dict[str, dict[str, str]]:
    return {item["id"]: item for item in result[job_id]}


def test_posting_age_bands(connection: sqlite3.Connection) -> None:
    fresh = add_job(connection, posted_at="2026-10-06T00:00:00Z", fingerprint="a")
    mixed = add_job(connection, posted_at="2026-09-01T00:00:00Z", fingerprint="b")
    old = add_job(connection, posted_at="2026-07-25T00:00:00Z", fingerprint="c")        # 83 days
    result = compute_signals(connection, [fresh, mixed, old], NOW)
    assert by_id(result, fresh)["posting_age"] == {"id": "posting_age", "level": "info",
                                                   "text": "listed 10 days ago on Greenhouse"}
    assert by_id(result, mixed)["posting_age"]["level"] == "info"
    assert by_id(result, old)["posting_age"] == {"id": "posting_age", "level": "caution",
                                                 "text": "listed 83 days ago on Greenhouse"}
    assert has_caution(result[old]) and not has_caution(result[fresh])


def test_internships_never_get_an_age_caution(connection: sqlite3.Connection) -> None:
    job = add_job(connection, posted_at="2026-06-01T00:00:00Z", employment="internship", level="intern")
    item = by_id(compute_signals(connection, [job], NOW), job)["posting_age"]
    assert item["level"] == "info" and item["text"].startswith("listed 137 days ago")


def test_unknown_posting_date(connection: sqlite3.Connection) -> None:
    job = add_job(connection, posted_at=None)
    result = by_id(compute_signals(connection, [job], NOW), job)
    assert result["posting_age"] == {"id": "posting_age", "level": "info", "text": "posting date unknown"}
    assert "date_quality" not in result


def test_future_date_is_clamped_to_today(connection: sqlite3.Connection) -> None:
    job = add_job(connection, posted_at="2026-10-20T00:00:00Z")
    assert by_id(compute_signals(connection, [job], NOW), job)["posting_age"]["text"] == "listed today on Greenhouse"


def test_greenhouse_date_quality(connection: sqlite3.Connection) -> None:
    fallback = add_job(connection, raw={"id": 1, "first_published": None, "updated_at": "2026-10-06"}, fingerprint="a")
    real = add_job(connection, raw={"id": 2, "first_published": "2026-10-01T00:00:00Z"}, fingerprint="b")
    unknown_payload = add_job(connection, raw=None, fingerprint="c")
    lever = add_job(connection, source="lever", fingerprint="d")
    result = compute_signals(connection, [fallback, real, unknown_payload, lever], NOW)
    assert "date_quality" in by_id(result, fallback)
    assert by_id(result, fallback)["date_quality"]["text"] == "date from source (may be last-updated)"
    assert "date_quality" not in by_id(result, real)
    assert "date_quality" in by_id(result, unknown_payload)       # cannot verify: say so
    assert "date_quality" not in by_id(result, lever)


def test_employer_confirmed_versus_aggregator_only(connection: sqlite3.Connection) -> None:
    employer = add_job(connection, source="workday", fingerprint="a")
    aggregator = add_job(connection, source="simplify_newgrad", fingerprint="b",
                         apply_url="https://acme.example/careers/1")
    both = add_job(connection, source="simplify_newgrad", extra_sources=("greenhouse",), fingerprint="c")
    nothing = add_job(connection, source="manual", fingerprint="d")
    result = compute_signals(connection, [employer, aggregator, both, nothing], NOW)
    assert by_id(result, employer)["employer_confirmed"]["text"] == "listed on the employer's site (Workday)"
    caution = by_id(result, aggregator)["aggregator_only"]
    assert caution["level"] == "caution" and "not confirmed on the employer's site yet" in caution["text"]
    assert "employer_confirmed" in by_id(result, both) and "aggregator_only" not in by_id(result, both)
    assert "employer_confirmed" not in by_id(result, nothing) and "aggregator_only" not in by_id(result, nothing)


def test_flickering_only_while_open(connection: sqlite3.Connection) -> None:
    flicker = add_job(connection, missed=2, fingerprint="a")
    steady = add_job(connection, missed=0, fingerprint="b")
    closed = add_job(connection, missed=3, status="closed", closed_at="2026-10-10T00:00:00Z", fingerprint="c")
    result = compute_signals(connection, [flicker, steady, closed], NOW)
    assert by_id(result, flicker)["flickering"] == {"id": "flickering", "level": "info",
                                                    "text": "missed in the last 2 fetches"}
    assert "flickering" not in by_id(result, steady) and "flickering" not in by_id(result, closed)


def test_repost_needs_disjoint_windows_and_a_14_day_span(connection: sqlite3.Connection) -> None:
    first = add_job(connection, fingerprint="same", first_seen="2026-08-01T00:00:00Z",
                    last_seen="2026-08-20T00:00:00Z", closed_at="2026-08-21T00:00:00Z", status="closed")
    second = add_job(connection, fingerprint="same", first_seen="2026-10-01T00:00:00Z")
    result = compute_signals(connection, [second], NOW)
    assert by_id(result, second)["repost"]["text"] == \
        "same title, company and location was listed 2 separate times over 61 days"
    assert first and by_id(compute_signals(connection, [first], NOW), first)["repost"]["level"] == "caution"


def test_repost_overlapping_or_short_span_shows_nothing(connection: sqlite3.Connection) -> None:
    a = add_job(connection, fingerprint="overlap", first_seen="2026-09-01T00:00:00Z", last_seen="2026-10-15T00:00:00Z")
    b = add_job(connection, fingerprint="overlap", first_seen="2026-09-20T00:00:00Z", last_seen="2026-10-15T00:00:00Z")
    c = add_job(connection, fingerprint="short", first_seen="2026-10-01T00:00:00Z", last_seen="2026-10-03T00:00:00Z",
                closed_at="2026-10-04T00:00:00Z", status="closed")
    d = add_job(connection, fingerprint="short", first_seen="2026-10-08T00:00:00Z")       # 7 day span
    result = compute_signals(connection, [a, b, c, d], NOW)
    for job_id in (a, b, c, d):
        assert "repost" not in by_id(result, job_id)


@pytest.mark.parametrize("url,company,kind", [
    ("https://bit.ly/3xYz", "Acme", "shortener"),
    ("https://www.gmail.com/jobs", "Acme", "free_mail"),
    ("https://totally-different.io/apply", "Acme Robotics", "mismatch"),
])
def test_apply_url_shape_flags(url: str, company: str, kind: str) -> None:
    shape = apply_url_shape(url, company)
    assert shape is not None and shape[0] == kind


@pytest.mark.parametrize("url,company", [
    ("https://careers.acme.com/jobs/1", "Acme Inc"),
    ("https://www.linkedin.com/jobs/view/123", "Acme"),
    ("https://acme-robotics.myworkdayjobs.com/en/jobs", "Acme Robotics"),
    (None, "Acme"),
    ("not a url", "Acme"),
])
def test_apply_url_shape_ok_and_null_cases(url: str | None, company: str) -> None:
    assert apply_url_shape(url, company) is None


def test_url_shape_only_checked_for_email_sourced_jobs(connection: sqlite3.Connection) -> None:
    email = add_job(connection, source="email_linkedin", apply_url="https://bit.ly/abc", fingerprint="a")
    feed = add_job(connection, source="remoteok", apply_url="https://bit.ly/abc", fingerprint="b")
    result = compute_signals(connection, [email, feed], NOW)
    assert by_id(result, email)["apply_url_shortener"]["level"] == "caution"
    assert not any(key.startswith("apply_url_") for key in by_id(result, feed))


def test_missing_job_and_empty_input(connection: sqlite3.Connection) -> None:
    assert compute_signals(connection, [], NOW) == {}
    assert compute_signals(connection, [99999], NOW) == {}


def test_signal_levels_are_only_info_or_caution(connection: sqlite3.Connection) -> None:
    job = add_job(connection, missed=1, posted_at="2026-05-01T00:00:00Z")
    levels = {item["level"] for item in compute_signals(connection, [job], NOW)[job]}
    assert levels <= {"info", "caution"}
    assert "ghost" not in json.dumps(signals.__dict__.get("_DISPLAY", {})).lower()
