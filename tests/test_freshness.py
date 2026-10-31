"""Age bands, deadlines and seasons, auto-hide / restore, and the scoring pool leaving hidden jobs out."""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from jobhunt import freshness
from jobhunt.config import Config, FreshnessSettings
from jobhunt.freshness import AGING, FRESH, STALE, UNKNOWN, classify_freshness, deadline_dates, run_freshness
from jobhunt.matching.gate import run_relevance
from jobhunt.matching import scoring
from tests.conftest import FakeClock
from tests.test_scoring import add_job, seed

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
SETTINGS = FreshnessSettings(aggregator_sources=("simplify_internships", "arbeitnow"))


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def band(posted: float | None, *, seen: float = 5, settings: FreshnessSettings = SETTINGS, **kwargs) -> str:  # noqa: ANN003
    return classify_freshness(posted_at=iso(posted) if posted is not None else None, first_seen_at=iso(seen),
                              settings=settings, now=NOW, **{"sources": ["greenhouse"], **kwargs}).band


@pytest.mark.parametrize(("age", "expected"), [
    (0, FRESH), (1, FRESH), (30, FRESH), (31, AGING), (45, AGING), (46, STALE), (120, STALE), (400, STALE),
])
def test_default_age_bands(age: int, expected: str) -> None:
    assert band(age) == expected


def test_a_future_posted_date_is_fresh() -> None:
    assert classify_freshness(posted_at="2026-12-01T00:00:00Z", first_seen_at=iso(1), settings=SETTINGS, now=NOW,
                              sources=["greenhouse"]).band == FRESH


def test_a_source_override_and_the_most_generous_source_wins() -> None:
    generous = dataclasses.replace(SETTINGS, source_stale_after_days={"greenhouse": 120})
    assert band(100, settings=generous) == AGING and band(130, settings=generous) == STALE
    assert band(100, settings=generous, sources=["lever"]) == STALE
    assert band(100, settings=generous, sources=["lever", "greenhouse"]) == AGING     # sighted by both: the longer limit


@pytest.mark.parametrize(("age", "expected"), [(60, AGING), (90, AGING), (91, STALE)])
def test_internships_and_graduate_programmes_are_stale_only_after_90_days(age: int, expected: str) -> None:
    assert band(age, employment_type="internship", title="Software Intern") == expected
    assert band(age, title="Graduate Software Engineer Programme") == expected
    assert band(age, experience_level="new_grad", title="Software Engineer") == expected
    assert band(age, title="Software Engineer") == STALE


def test_a_future_deadline_keeps_a_job_alive_and_a_past_one_does_not() -> None:
    future = "Great role. Application deadline: 30 November 2026. Apply now."
    past = "Great role. Application deadline: 30 September 2026."
    assert band(200, description=future) == AGING
    assert band(200, description=past) == STALE
    assert classify_freshness(posted_at=iso(200), first_seen_at=iso(5), description=future, settings=SETTINGS, now=NOW,
                              sources=["greenhouse"]).protected_by == "deadline"


def test_an_upcoming_or_current_season_keeps_an_internship_alive() -> None:
    assert band(150, employment_type="internship", title="Software Intern - Summer 2027") == AGING
    assert band(150, employment_type="internship", title="Software Intern - Summer 2026") == STALE
    assert band(150, title="Software Engineer - Summer 2027") == STALE      # a season only protects early-career roles


def test_unknown_date_is_stale_only_for_old_aggregator_lists() -> None:
    assert band(None, seen=45, sources=["simplify_internships"]) == STALE
    assert band(None, seen=10, sources=["simplify_internships"]) == UNKNOWN
    assert band(None, seen=45, sources=["greenhouse"]) == UNKNOWN            # an employer board: age-unknown chip
    assert band(None, seen=45, sources=["workday"]) == UNKNOWN
    assert band(None, seen=45, sources=["arbeitnow", "greenhouse"]) == UNKNOWN   # not only an aggregator
    assert band(None, seen=45, sources=[]) == UNKNOWN


@pytest.mark.parametrize(("text", "expected"), [
    ("Apply by 30 November 2026", [date(2026, 11, 30)]),
    ("Closing date: 1st December 2026", [date(2026, 12, 1)]),
    ("Application deadline - Nov 5, 2026", [date(2026, 11, 5)]),
    ("deadline: 2026-12-15", [date(2026, 12, 15)]),
    ("Applications close 15/12/2026", [date(2026, 12, 15)]),
    ("The deadline is 10 September 2026", [date(2026, 9, 10)]),
    ("No dates here", []),
    ("Posted 30 November 2026", []),                 # a date alone is not a deadline
    ("deadline: 31 February 2026", []),              # impossible date
])
def test_deadline_parsing(text: str, expected: list[date]) -> None:
    assert deadline_dates(text) == expected


# --- the batch --------------------------------------------------------------------------------------------------------


@pytest.fixture()
def cfg(config: Config) -> Config:
    return dataclasses.replace(config, freshness=SETTINGS)


def add(connection: sqlite3.Connection, n: int, posted_days: float | None, **values) -> int:  # noqa: ANN003
    return add_job(connection, n, f"Software Engineer {n}", "Python services.",
                   posted_at=iso(posted_days) if posted_days is not None else None, first_seen_at=iso(5), **values)


def run(connection: sqlite3.Connection, cfg: Config):  # noqa: ANN201
    return run_freshness(connection, cfg, clock=FakeClock(NOW))


def hidden(connection: sqlite3.Connection) -> set[int]:
    return {int(r[0]) for r in connection.execute("SELECT job_id FROM job_auto_hidden WHERE restored_at IS NULL")}


def test_stale_jobs_are_hidden_with_a_reason_and_nothing_is_deleted(connection: sqlite3.Connection, cfg: Config) -> None:
    fresh, aging, stale = add(connection, 1, 3), add(connection, 2, 40), add(connection, 3, 100)
    stats = run(connection, cfg)
    assert hidden(connection) == {stale} and stats.newly_hidden == 1
    row = connection.execute("SELECT reason, detail FROM job_auto_hidden WHERE job_id = ?", (stale,)).fetchone()
    assert row["reason"] == "auto_stale" and "100 days" in row["detail"]
    assert {int(r["job_id"]): r["band"] for r in connection.execute("SELECT * FROM job_freshness")} == {
        fresh: "fresh", aging: "aging", stale: "stale"}
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    assert connection.execute("SELECT status FROM jobs WHERE id = ?", (stale,)).fetchone()[0] == "open"


def test_a_second_run_changes_nothing(connection: sqlite3.Connection, cfg: Config) -> None:
    add(connection, 1, 100)
    run(connection, cfg)
    again = run(connection, cfg)
    assert again.newly_hidden == 0 and again.already_hidden == 1 and len(hidden(connection)) == 1


def test_saved_applied_tailored_and_manual_jobs_are_never_auto_hidden(connection: sqlite3.Connection, cfg: Config) -> None:
    saved, applied, tailored, manual, plain = (add(connection, n, 100) for n in range(1, 6))
    connection.execute("INSERT INTO applications (job_id, stage, created_at, updated_at) VALUES (?, 'saved', 'x', 'x')", (saved,))
    connection.execute("INSERT INTO job_decisions (job_id, action, happened_at, created_at) VALUES (?, 'applied', 'x', 'x')", (applied,))
    connection.execute(
        "INSERT INTO tailored_resumes (job_id, base_profile, mode, cache_key, status, created_at, pages, resume_number, "
        "version, lifecycle) VALUES (?, 'ai', 'deterministic', 'k', 'ok', 'x', 1, 1, 1, 'approved')", (tailored,))
    connection.execute("INSERT INTO job_sightings (job_id, source, source_job_id, first_seen_at, last_seen_at) "
                       "VALUES (?, 'manual', 'm', 'x', 'x')", (manual,))
    run(connection, cfg)
    assert hidden(connection) == {plain}


def test_jobs_the_gate_already_excluded_are_not_cluttering_the_auto_hidden_view(connection: sqlite3.Connection, cfg: Config) -> None:
    ids = seed(connection, cfg, FakeClock(NOW), [("Java Developer", "Spring.", {"posted_at": iso(100)}),
                                                  ("Software Engineer", "Python.", {"posted_at": iso(100)})])
    run(connection, cfg)
    assert hidden(connection) == {ids[1]}


def test_restore_marks_the_job_so_it_is_not_hidden_again(connection: sqlite3.Connection, cfg: Config) -> None:
    job = add(connection, 1, 100)
    run(connection, cfg)
    assert freshness.restore_job(connection, job, "2026-10-21T12:00:00Z") is True
    assert hidden(connection) == set()
    again = run(connection, cfg)
    assert hidden(connection) == set() and again.newly_hidden == 0
    assert freshness.restore_job(connection, job, "2026-10-21T12:00:00Z") is False       # already restored
    assert connection.execute("SELECT restored_at FROM job_auto_hidden WHERE job_id = ?", (job,)).fetchone()[0]


def test_loosening_the_config_releases_auto_stale_jobs_but_not_restored_or_checked_ones(
        connection: sqlite3.Connection, cfg: Config) -> None:
    old, closed = add(connection, 1, 100), add(connection, 2, 100)
    run(connection, cfg)
    freshness.hide_job(connection, closed, "closed_checked", "HTTP 404", "2026-10-21T00:00:00Z")
    loose = dataclasses.replace(cfg, freshness=dataclasses.replace(SETTINGS, stale_after_days=200))
    stats = run(connection, loose)
    assert stats.unhidden == 1 and hidden(connection) == {closed}
    assert old not in hidden(connection)


def test_closed_jobs_are_not_examined_and_hidden_by_source_is_counted(connection: sqlite3.Connection, cfg: Config) -> None:
    add(connection, 1, 100)
    other = add(connection, 2, 100)
    connection.execute("INSERT INTO job_sightings (job_id, source, source_job_id, first_seen_at, last_seen_at) "
                       "VALUES (?, 'lever', 'l1', 'x', 'x')", (other,))
    gone = add(connection, 3, 100, status="closed")
    stats = run(connection, cfg)
    assert stats.examined == 2 and gone not in hidden(connection)
    assert stats.hidden_by_source == {"lever": 1} or sum(stats.hidden_by_source.values()) >= 2


def test_disabled_does_nothing(connection: sqlite3.Connection, cfg: Config) -> None:
    add(connection, 1, 100)
    off = dataclasses.replace(cfg, freshness=dataclasses.replace(SETTINGS, enabled=False))
    stats = run(connection, off)
    assert stats.examined == 0 and hidden(connection) == set()


def test_the_scoring_pool_leaves_hidden_jobs_out(connection: sqlite3.Connection, cfg: Config) -> None:
    clock = FakeClock(NOW)
    ids = seed(connection, cfg, clock, [("Software Engineer A", "Python.", {"posted_at": iso(100)}),
                                         ("Software Engineer B", "Python.", {"posted_at": iso(3)})])
    wide = dataclasses.replace(cfg, scoring=dataclasses.replace(cfg.scoring, recent_days=3650))
    before = {job.job_id for job in scoring._scoring_pool(connection, wide, NOW.date())}
    assert before == set(ids)
    run(connection, wide)
    assert {job.job_id for job in scoring._scoring_pool(connection, wide, NOW.date())} == {ids[1]}
    assert scoring.pool_job_ids(connection, wide, NOW.date()) == [ids[1]]
    freshness.restore_job(connection, ids[0], "2026-10-21T12:00:00Z")
    assert {job.job_id for job in scoring._scoring_pool(connection, wide, NOW.date())} == set(ids)
    assert run_relevance is not None


def test_the_shipped_config_gives_greenhouse_90_days_and_everything_else_45(config: Config) -> None:
    assert config.freshness.source_stale_after_days == {"greenhouse": 90}
    assert (config.freshness.fresh_days, config.freshness.stale_after_days) == (30, 45)


def test_the_per_source_override_decides_what_is_auto_hidden(connection: sqlite3.Connection, config: Config) -> None:
    cfg = dataclasses.replace(config, freshness=dataclasses.replace(config.freshness, aggregator_sources=()))
    ids = {name: add(connection, n, days) for n, (name, days) in enumerate((("gh_80", 80), ("gh_100", 100), ("lever_80", 80)), 1)}
    for name, source in (("gh_80", "greenhouse"), ("gh_100", "greenhouse"), ("lever_80", "lever")):
        connection.execute("INSERT INTO job_sightings (job_id, source, source_job_id, first_seen_at, last_seen_at) "
                           "VALUES (?, ?, ?, 'x', 'x')", (ids[name], source, name))
    run(connection, cfg)
    assert hidden(connection) == {ids["gh_100"], ids["lever_80"]}     # greenhouse 80 days stays, lever 80 days goes
