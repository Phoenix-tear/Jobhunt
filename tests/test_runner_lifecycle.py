"""Runner + lifecycle: closing rules, failure isolation, false-mass-closure guard, reopening."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import timedelta

import httpx
import pytest
import respx

from jobhunt.config import Config
from jobhunt.errors import AuthRequiredError, InvalidResponseError, NotFoundError, TransientFetchError
from jobhunt.http import HttpClient
from jobhunt.models import FetchResult, NormalizedJob
from jobhunt.runner import IngestionRunner, RunSummary
from jobhunt.sources.greenhouse import GreenhouseSource
from tests.conftest import FakeClock
from tests.helpers import FakeSource, insert_company, make_job


def board(*titles_and_ids: tuple[str, str], name: str | None = "Acme Inc") -> FetchResult:
    jobs = [
        make_job(source_job_id=job_id, title=title, apply_url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
                 location_raw="Pune, India", description_text=f"{title} description")
        for job_id, title in titles_and_ids
    ]
    return FetchResult(jobs=jobs, company_name=name)


def many(prefix: str, n: int) -> FetchResult:
    return board(*[(f"{prefix}{i}", f"Software Engineer {prefix}{i}") for i in range(n)])


@pytest.fixture()
def fake_source() -> FakeSource:
    return FakeSource()


@pytest.fixture()
def build_runner(connection: sqlite3.Connection, config: Config, http_client: HttpClient, clock: FakeClock, fake_source: FakeSource):
    def _build(**http_overrides: object) -> IngestionRunner:
        run_config = config
        if http_overrides:
            run_config = dataclasses.replace(config, http=dataclasses.replace(config.http, **http_overrides))
        return IngestionRunner(connection, run_config, {"greenhouse": fake_source}, http_client, clock=clock)

    return _build


def run_once(runner: IngestionRunner, clock: FakeClock, hours: float = 24) -> RunSummary:
    clock.advance(hours=hours)
    return runner.run(["greenhouse"])[0]


def job_row(connection: sqlite3.Connection, source_job_id: str) -> sqlite3.Row:
    return connection.execute(
        "SELECT j.* FROM jobs j JOIN job_sightings s ON s.job_id = j.id WHERE s.source_job_id = ?", (source_job_id,)
    ).fetchone()


def test_first_run_stores_jobs_and_records_the_run(build_runner, connection, fake_source, clock) -> None:
    company = insert_company(connection, "greenhouse", "acme", name="Acme")
    fake_source.script("acme", board(("1", "Software Engineer"), ("2", "Data Scientist Intern"), name="Acme Inc"))
    summary = run_once(build_runner(), clock)

    assert (summary.companies_checked, summary.jobs_seen, summary.jobs_new) == (1, 2, 2)
    row = connection.execute("SELECT * FROM ingestion_runs").fetchone()
    assert (row["source"], row["companies_checked"], row["jobs_seen"], row["jobs_new"]) == ("greenhouse", 1, 2, 2)
    assert row["started_at"] and row["finished_at"] and row["errors_json"] is None
    stored = connection.execute("SELECT * FROM companies WHERE id = ?", (company.id,)).fetchone()
    assert stored["status"] == "active" and stored["name"] == "Acme Inc" and stored["last_ok_at"]
    intern = job_row(connection, "2")
    assert intern["employment_type"] == "internship" and intern["experience_level"] == "intern"
    assert (intern["country"], intern["city"]) == ("IN", "Pune")
    assert connection.execute("SELECT * FROM source_state WHERE source = 'greenhouse'").fetchone()["last_success_at"]


def test_rerunning_is_idempotent(build_runner, connection, fake_source, clock) -> None:
    insert_company(connection, "greenhouse", "acme")
    fake_source.script("acme", board(("1", "Software Engineer"), ("2", "Backend Engineer")))
    runner = build_runner()
    run_once(runner, clock)
    second = run_once(runner, clock)
    assert second.jobs_new == 0 and second.jobs_seen == 2
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM job_sightings").fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 2


def test_job_closes_after_three_clean_misses(build_runner, connection, fake_source, clock) -> None:
    insert_company(connection, "greenhouse", "acme")
    runner = build_runner()
    fake_source.script("acme", board(("1", "Software Engineer"), ("2", "Backend Engineer")))
    run_once(runner, clock)

    fake_source.script("acme", board(("1", "Software Engineer")))   # job 2 vanished
    run_once(runner, clock)
    assert (job_row(connection, "2")["missed_runs"], job_row(connection, "2")["status"]) == (1, "open")
    run_once(runner, clock)
    assert (job_row(connection, "2")["missed_runs"], job_row(connection, "2")["status"]) == (2, "open")
    closing = run_once(runner, clock)
    closed = job_row(connection, "2")
    assert (closed["status"], closed["missed_runs"]) == ("closed", 3) and closed["closed_at"]
    assert closing.jobs_closed == 1
    assert job_row(connection, "1")["status"] == "open"


@pytest.mark.parametrize(
    "failure",
    [
        NotFoundError("404"), InvalidResponseError("<html>"), TransientFetchError("timeout"),
        AuthRequiredError("401"), RuntimeError("adapter bug"),
    ],
    ids=lambda error: type(error).__name__,
)
def test_failed_fetches_never_count_as_misses(build_runner, connection, fake_source, clock, failure) -> None:
    insert_company(connection, "greenhouse", "acme")
    runner = build_runner()
    fake_source.script("acme", board(("1", "Software Engineer"), ("2", "Backend Engineer")))
    run_once(runner, clock)

    fake_source.script("acme", failure)
    for _ in range(6):   # same UTC day, so the dead-slug rule (2 distinct days) cannot kick in either
        summary = run_once(runner, clock, hours=1)
        assert summary.companies_failed == 1
    assert {row["status"] for row in connection.execute("SELECT status FROM jobs")} == {"open"}
    assert {row["missed_runs"] for row in connection.execute("SELECT missed_runs FROM jobs")} == {0}


def test_suspiciously_empty_board_does_not_close_jobs_until_it_persists(build_runner, connection, fake_source, clock) -> None:
    insert_company(connection, "greenhouse", "acme")
    runner = build_runner()
    fake_source.script("acme", many("j", 12))
    run_once(runner, clock)

    fake_source.script("acme", FetchResult(jobs=[], company_name="Acme Inc"))   # an upstream hiccup?
    first = run_once(runner, clock)
    second = run_once(runner, clock)
    assert first.jobs_closed == second.jobs_closed == 0
    assert {row["missed_runs"] for row in connection.execute("SELECT missed_runs FROM jobs")} == {0}
    assert connection.execute("SELECT consecutive_empty_runs FROM companies").fetchone()[0] == 2
    assert any(error["kind"] == "suspicious_drop" for error in first.errors)

    run_once(runner, clock)    # third consecutive empty run: accepted as real, misses start counting
    assert {row["missed_runs"] for row in connection.execute("SELECT missed_runs FROM jobs")} == {1}
    run_once(runner, clock)
    closing = run_once(runner, clock)
    assert closing.jobs_closed == 12


def test_a_normal_run_resets_the_empty_streak(build_runner, connection, fake_source, clock) -> None:
    insert_company(connection, "greenhouse", "acme")
    runner = build_runner()
    fake_source.script("acme", many("j", 12))
    run_once(runner, clock)
    fake_source.script("acme", FetchResult(jobs=[]))
    run_once(runner, clock)
    fake_source.script("acme", many("j", 12))
    run_once(runner, clock)
    assert connection.execute("SELECT consecutive_empty_runs FROM companies").fetchone()[0] == 0


def test_a_big_drop_is_also_suspicious_but_a_small_board_emptying_is_not(build_runner, connection, fake_source, clock) -> None:
    insert_company(connection, "greenhouse", "big")
    insert_company(connection, "greenhouse", "small")
    runner = build_runner()
    fake_source.script("big", many("b", 20))
    fake_source.script("small", many("s", 3))
    run_once(runner, clock)

    fake_source.script("big", many("b", 1))             # 20 -> 1 (< 10%)
    fake_source.script("small", FetchResult(jobs=[]))  # 3 -> 0 (below the min-previous threshold)
    for _ in range(3):
        run_once(runner, clock)
    big_open = connection.execute("SELECT COUNT(*) FROM jobs WHERE company_id = (SELECT id FROM companies WHERE slug='big') AND status='open'").fetchone()[0]
    small_open = connection.execute("SELECT COUNT(*) FROM jobs WHERE company_id = (SELECT id FROM companies WHERE slug='small') AND status='open'").fetchone()[0]
    assert big_open == 20      # still protected: only 2 suspicious runs so far +1 accepted (misses = 1)
    assert small_open == 0     # tiny board genuinely emptied and closed after 3 clean misses


def test_reappearing_job_reopens(build_runner, connection, fake_source, clock) -> None:
    insert_company(connection, "greenhouse", "acme")
    runner = build_runner()
    fake_source.script("acme", board(("1", "Software Engineer"), ("2", "Backend Engineer")))
    run_once(runner, clock)
    fake_source.script("acme", board(("1", "Software Engineer")))
    for _ in range(3):
        run_once(runner, clock)
    assert job_row(connection, "2")["status"] == "closed"

    fake_source.script("acme", board(("1", "Software Engineer"), ("2", "Backend Engineer")))
    summary = run_once(runner, clock)
    reopened = job_row(connection, "2")
    assert (reopened["status"], reopened["closed_at"], reopened["missed_runs"]) == ("open", None, 0)
    assert summary.jobs_reopened == 1 and summary.jobs_new == 0


def test_prefilter_skips_are_counted_and_not_stored(build_runner, connection, fake_source, clock) -> None:
    insert_company(connection, "greenhouse", "acme")
    fake_source.script("acme", board(
        ("1", "Software Engineer"), ("2", "Senior Staff Engineer"), ("3", "Account Executive"), ("4", "ML Intern"),
    ))
    summary = run_once(build_runner(), clock)
    assert (summary.jobs_seen, summary.jobs_new, summary.jobs_skipped_by_prefilter) == (4, 2, 2)
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    assert connection.execute("SELECT jobs_skipped_by_prefilter FROM ingestion_runs").fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM jobs WHERE raw_json IS NOT NULL").fetchone()[0] == 0  # fake jobs carry no raw


def test_a_held_job_that_now_fails_the_prefilter_is_closed_and_reopens_when_it_passes(
    build_runner, connection, fake_source, clock
) -> None:
    insert_company(connection, "greenhouse", "acme")
    runner = build_runner()
    fake_source.script("acme", board(("1", "Software Engineer")))
    run_once(runner, clock)
    assert job_row(connection, "1")["status"] == "open"

    fake_source.script("acme", board(("1", "Senior Software Engineer")))   # retitled; excluded by config now
    summary = run_once(runner, clock)
    row = job_row(connection, "1")
    assert (row["status"], row["closed_at"] is not None, summary.jobs_closed) == ("closed", True, 1)
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1   # kept, not deleted

    fake_source.script("acme", board(("1", "Software Engineer")))          # passes the filter again
    summary = run_once(runner, clock)
    assert job_row(connection, "1")["status"] == "open" and summary.jobs_reopened == 1


def test_one_broken_board_does_not_stop_the_others(build_runner, connection, fake_source, clock) -> None:
    dead = insert_company(connection, "greenhouse", "dead")
    buggy = insert_company(connection, "greenhouse", "buggy")
    healthy = insert_company(connection, "greenhouse", "healthy")
    fake_source.script("dead", NotFoundError("HTTP 404"))
    fake_source.script("buggy", KeyError("adapter blew up"))
    fake_source.script("healthy", board(("1", "Software Engineer")))
    summary = run_once(build_runner(), clock)

    assert (summary.companies_checked, summary.companies_failed, summary.jobs_new) == (3, 2, 1)
    statuses = {row["slug"]: row for row in connection.execute("SELECT * FROM companies")}
    assert statuses["dead"]["consecutive_failures"] == 1 and "404" in statuses["dead"]["last_error"]
    assert statuses["buggy"]["consecutive_failures"] == 0 and statuses["buggy"]["last_error"]   # not a dead-slug signal
    assert statuses["healthy"]["last_ok_at"]
    errors = json.loads(connection.execute("SELECT errors_json FROM ingestion_runs").fetchone()[0])
    assert {item["company"] for item in errors} == {"greenhouse/dead", "greenhouse/buggy"}
    assert dead.id and buggy.id and healthy.id


def test_auth_required_stops_the_source_for_this_run(build_runner, connection, fake_source, clock) -> None:
    first = insert_company(connection, "greenhouse", "a-first")
    insert_company(connection, "greenhouse", "b-second")
    insert_company(connection, "greenhouse", "c-third")
    fake_source.script("a-first", AuthRequiredError("401 token required"))
    fake_source.script("b-second", board(("1", "Software Engineer")))
    fake_source.script("c-third", board(("2", "Software Engineer")))
    summary = run_once(build_runner(max_workers=1), clock)
    assert summary.blocked is True and summary.companies_checked == 1
    assert fake_source.calls == ["a-first"]
    row = connection.execute("SELECT consecutive_failures FROM companies WHERE id = ?", (first.id,)).fetchone()
    assert row[0] == 0   # a blocked source is not a dead slug


def test_only_active_and_due_inactive_companies_are_fetched(build_runner, connection, fake_source, clock) -> None:
    insert_company(connection, "greenhouse", "active", status="active")
    insert_company(connection, "greenhouse", "unverified", status="unverified")
    due = insert_company(connection, "greenhouse", "due", status="inactive")
    recent = insert_company(connection, "greenhouse", "recent", status="inactive")
    now = clock()
    connection.execute("UPDATE companies SET last_checked_at = ? WHERE id = ?",
                       ((now - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ"), due.id))
    connection.execute("UPDATE companies SET last_checked_at = ? WHERE id = ?",
                       ((now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"), recent.id))
    for slug in ("active", "due", "recent", "unverified"):
        fake_source.script(slug, board((slug, "Software Engineer")))
    run_once(build_runner(), clock, hours=0)
    assert sorted(fake_source.calls) == ["active", "due"]
    assert connection.execute("SELECT status FROM companies WHERE slug = 'due'").fetchone()[0] == "active"  # revived


def test_unknown_source_name_is_rejected(build_runner) -> None:
    with pytest.raises(KeyError, match="nope"):
        build_runner().run(["nope"])


def test_end_to_end_with_the_real_greenhouse_adapter(
    connection: sqlite3.Connection, config: Config, http_client: HttpClient, clock: FakeClock, router: respx.MockRouter
) -> None:
    insert_company(connection, "greenhouse", "acme")
    payload = {"jobs": [
        {"id": 1, "title": "Software Engineer, New Grad (Class of 2027)", "location": {"name": "Bengaluru, Karnataka, India"},
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "first_published": "2026-10-01T09:00:00Z",
         "content": "&lt;p&gt;Join us. Graduating in 2027? Apply.&lt;/p&gt;", "company_name": "Acme"},
        {"id": 2, "title": "Senior Staff Engineer", "location": {"name": "Remote"}, "absolute_url": "https://boards.greenhouse.io/acme/jobs/2"},
        {"id": 3, "title": "Data Scientist Intern", "location": {"name": "Remote - US"},
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/3", "content": "&lt;p&gt;Summer internship.&lt;/p&gt;"},
    ]}
    router.get(url__regex=r"https://boards-api\.greenhouse\.io/v1/boards/acme/jobs.*").mock(return_value=httpx.Response(200, json=payload))
    runner = IngestionRunner(connection, config, {"greenhouse": GreenhouseSource()}, http_client, clock=clock)
    summary = runner.run()[0]

    assert (summary.jobs_seen, summary.jobs_new, summary.jobs_skipped_by_prefilter) == (3, 2, 1)
    grad = job_row(connection, "1")
    assert (grad["experience_level"], grad["country"], grad["city"], grad["graduation_year_hint"]) == ("new_grad", "IN", "Bengaluru", 2027)
    assert grad["description_text"] == "Join us. Graduating in 2027? Apply."
    assert json.loads(grad["raw_json"])["id"] == 1 and "content" not in json.loads(grad["raw_json"])
    intern = job_row(connection, "3")
    assert (intern["employment_type"], intern["is_remote"], intern["country"]) == ("internship", 1, "US")
    assert job_row(connection, "2") is None
