"""Runner behavior for feed sources: intervals, caps, secrets, staleness, closures, merging."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
import respx

from jobhunt.config import Config
from jobhunt.envfile import load_secrets, parse_env_file
from jobhunt.errors import AuthRequiredError, TransientFetchError
from jobhunt.http import HttpClient
from jobhunt.models import FetchResult
from jobhunt.runner import IngestionRunner, RunSummary
from jobhunt.sources.base import FeedContext, FeedSource
from jobhunt.sources.feeds.remote_boards import RemotiveSource
from tests.conftest import FakeClock
from tests.helpers import make_job


class ScriptedFeed(FeedSource):
    name = "scriptedfeed"
    daily_fetch_cap: ClassVar[int | None] = None
    min_interval_hours: ClassVar[float] = 6.0
    stale_after_days: ClassVar[int] = 30
    attribution: ClassVar[str] = "Jobs via ScriptedFeed"

    def __init__(self, options: dict[str, Any] | None = None, secrets: dict[str, str] | None = None) -> None:
        super().__init__(options, secrets)
        self.outcomes: list[FetchResult | Exception] = [FetchResult(jobs=[])]
        self.contexts: list[FeedContext] = []
        self.budgets_offered: list[int | None] = []
        self.fetches_per_run = 1

    def fetch(self, http: HttpClient, context: FeedContext) -> FetchResult:
        self.contexts.append(context)
        self.budgets_offered.append(context.budget.remaining)
        for _ in range(self.fetches_per_run):
            if not context.budget.take():
                break
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def feed_job(job_id: str, title: str = "Software Engineer", **overrides: Any):
    values: dict[str, Any] = {
        "source": "scriptedfeed", "source_job_id": job_id, "title": title, "company_name": "Acme",
        "apply_url": f"https://feed.example/jobs/{job_id}", "location_raw": "Remote", "is_remote": True,
        "description_text": "A description.", "attribution_text": "Jobs via ScriptedFeed",
    }
    values.update(overrides)
    return make_job(**values)


@pytest.fixture()
def feed() -> ScriptedFeed:
    return ScriptedFeed()


@pytest.fixture()
def runner(connection: sqlite3.Connection, config: Config, http_client: HttpClient, clock: FakeClock, feed: ScriptedFeed) -> IngestionRunner:
    return IngestionRunner(connection, config, {"scriptedfeed": feed}, http_client, clock=clock)


def run(runner: IngestionRunner, clock: FakeClock, hours: float = 24, force: bool = False) -> RunSummary:
    clock.advance(hours=hours)
    return runner.run(["scriptedfeed"], force=force)[0]


def jobs(connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = connection.execute(
        "SELECT j.*, s.source_job_id AS sid FROM jobs j JOIN job_sightings s ON s.job_id = j.id WHERE s.source = 'scriptedfeed'"
    )
    return {row["sid"]: row for row in rows}


def test_feed_jobs_are_stored_without_a_company_and_with_attribution(runner, feed, connection, clock) -> None:
    feed.outcomes = [FetchResult(jobs=[feed_job("1"), feed_job("2", "Data Scientist Intern")])]
    summary = run(runner, clock)
    assert (summary.jobs_seen, summary.jobs_new, summary.skipped_reason) == (2, 2, None)
    stored = jobs(connection)
    assert stored["1"]["company_id"] is None and stored["1"]["is_remote"] == 1
    assert stored["2"]["employment_type"] == "internship"
    sighting = connection.execute("SELECT * FROM job_sightings WHERE source_job_id = '1'").fetchone()
    assert sighting["attribution_text"] == "Jobs via ScriptedFeed"
    state = connection.execute("SELECT * FROM source_state WHERE source = 'scriptedfeed'").fetchone()
    assert state["last_success_at"] and state["fetch_count_today"] == 1


def test_minimum_interval_is_enforced_and_force_overrides_it(runner, feed, connection, clock) -> None:
    feed.outcomes = [FetchResult(jobs=[feed_job("1")])]
    run(runner, clock)
    early = run(runner, clock, hours=2)
    assert early.skipped_reason and "minimum interval" in early.skipped_reason and len(feed.contexts) == 1
    assert connection.execute("SELECT COUNT(*) FROM ingestion_runs WHERE source = 'scriptedfeed'").fetchone()[0] == 2
    forced = run(runner, clock, hours=0, force=True)
    assert forced.skipped_reason is None and len(feed.contexts) == 2
    later = run(runner, clock, hours=7)
    assert later.skipped_reason is None


def test_daily_cap_survives_force_and_resets_next_day(runner, feed, connection, clock) -> None:
    feed.daily_fetch_cap = 2
    feed.fetches_per_run = 2
    feed.outcomes = [FetchResult(jobs=[feed_job("1")])]
    run(runner, clock, hours=1)
    capped = run(runner, clock, hours=0, force=True)
    assert capped.skipped_reason and "daily fetch cap of 2" in capped.skipped_reason
    clock.advance(days=1)
    assert runner.run(["scriptedfeed"])[0].skipped_reason is None


def test_remaining_budget_is_handed_to_the_adapter(runner, feed, clock) -> None:
    feed.daily_fetch_cap = 5
    feed.fetches_per_run = 3
    run(runner, clock, hours=1)
    run(runner, clock, hours=0, force=True)
    assert feed.budgets_offered == [5, 2]


def test_missing_secrets_skip_the_source_with_a_helpful_message(connection, config, http_client, clock) -> None:
    class KeyedFeed(ScriptedFeed):
        name = "keyedfeed"
        required_secrets = ("MY_API_KEY",)

    keyed = KeyedFeed()
    summary = IngestionRunner(connection, config, {"keyedfeed": keyed}, http_client, clock=clock).run()[0]
    assert "MY_API_KEY" in (summary.skipped_reason or "") and ".env" in summary.skipped_reason
    assert keyed.contexts == []
    assert connection.execute("SELECT errors_json FROM ingestion_runs").fetchone()[0].count("skipped") == 1


@pytest.mark.parametrize("failure", [TransientFetchError("503s"), AuthRequiredError("401"), RuntimeError("bug")],
                         ids=lambda e: type(e).__name__)
def test_failed_feed_fetch_changes_nothing_and_never_ages_jobs_out(runner, feed, connection, clock, failure) -> None:
    feed.outcomes = [FetchResult(jobs=[feed_job("1")])]
    run(runner, clock)
    feed.outcomes = [failure]
    summary = run(runner, clock, hours=24 * 90)       # far past stale_after_days
    assert jobs(connection)["1"]["status"] == "open"
    assert summary.errors and summary.jobs_closed == 0
    state = connection.execute("SELECT * FROM source_state WHERE source = 'scriptedfeed'").fetchone()
    assert state["last_success_at"] != state["last_run_at"]      # the failed run did not count as success


def test_jobs_not_seen_for_stale_after_days_are_closed_after_a_successful_fetch(runner, feed, connection, clock) -> None:
    feed.outcomes = [FetchResult(jobs=[feed_job("old"), feed_job("fresh")])]
    run(runner, clock)
    feed.outcomes = [FetchResult(jobs=[feed_job("fresh")])]
    run(runner, clock, hours=24 * 20)
    assert jobs(connection)["old"]["status"] == "open"           # 20 days < 30
    summary = run(runner, clock, hours=24 * 15)                  # 35 days since last sighting
    assert jobs(connection)["old"]["status"] == "closed" and summary.jobs_closed == 1
    assert jobs(connection)["fresh"]["status"] == "open"


def test_a_feed_never_closes_a_job_an_ats_still_lists(runner, feed, connection, clock) -> None:
    from jobhunt.db import transaction
    from jobhunt.dedupe import upsert_jobs

    feed.outcomes = [FetchResult(jobs=[feed_job("1", apply_url="https://boards.greenhouse.io/acme/jobs/1")])]
    run(runner, clock)
    with transaction(connection):   # the ATS then sights the same posting (merge by canonical URL)
        stats = upsert_jobs(
            connection,
            [make_job(source="greenhouse", source_job_id="g1", apply_url="https://boards.greenhouse.io/acme/jobs/1")],
            company_id=None, seen_at="2026-12-01T00:00:00Z",
        )
    assert stats.merged == 1
    feed.outcomes = [FetchResult(jobs=[], closed_source_job_ids={"1"})]
    summary = run(runner, clock, hours=24 * 60)
    assert summary.jobs_closed == 0 and jobs(connection)["1"]["status"] == "open"


def test_explicit_closures_from_the_feed(runner, feed, connection, clock) -> None:
    feed.outcomes = [FetchResult(jobs=[feed_job("1"), feed_job("2")])]
    run(runner, clock)
    feed.outcomes = [FetchResult(jobs=[feed_job("2")], closed_source_job_ids={"1", "unknown-id"})]
    summary = run(runner, clock)
    assert jobs(connection)["1"]["status"] == "closed" and jobs(connection)["2"]["status"] == "open"
    assert summary.jobs_closed == 1


def test_a_listed_job_that_reappears_after_closure_reopens(runner, feed, connection, clock) -> None:
    feed.outcomes = [FetchResult(jobs=[feed_job("1")])]
    run(runner, clock)
    feed.outcomes = [FetchResult(jobs=[], closed_source_job_ids={"1"})]
    run(runner, clock)
    assert jobs(connection)["1"]["status"] == "closed"
    feed.outcomes = [FetchResult(jobs=[feed_job("1")])]
    summary = run(runner, clock)
    assert jobs(connection)["1"]["status"] == "open" and summary.jobs_reopened == 1


def test_not_modified_keeps_everything_fresh_so_nothing_ages_out(runner, feed, connection, clock) -> None:
    feed.outcomes = [FetchResult(jobs=[feed_job("1")], cursor='"etag-1"')]
    run(runner, clock)
    feed.outcomes = [FetchResult(jobs=[], not_modified=True, cursor='"etag-1"')]
    for _ in range(5):
        run(runner, clock, hours=24 * 20)           # 100 days of "unchanged"
    assert jobs(connection)["1"]["status"] == "open"
    assert feed.contexts[-1].cursor == '"etag-1"'      # the stored etag is handed back


def test_cursor_is_persisted_between_runs(runner, feed, connection, clock) -> None:
    feed.outcomes = [FetchResult(jobs=[], cursor="rotation:3")]
    run(runner, clock)
    run(runner, clock)
    assert feed.contexts[-1].cursor == "rotation:3"


def test_prefilter_applies_to_feed_jobs_and_retroactively(runner, feed, connection, clock) -> None:
    feed.outcomes = [FetchResult(jobs=[feed_job("1"), feed_job("2", "Senior Software Engineer"), feed_job("3", "Sales Manager")])]
    summary = run(runner, clock)
    assert (summary.jobs_new, summary.jobs_skipped_by_prefilter) == (1, 2)
    assert set(jobs(connection)) == {"1"}
    feed.outcomes = [FetchResult(jobs=[feed_job("1", "Software Engineering Director")])]
    run(runner, clock)
    assert jobs(connection)["1"]["status"] == "closed"        # no longer matches the configured targets


def test_the_title_prefilter_is_given_to_the_adapter(runner, feed, clock) -> None:
    run(runner, clock)
    keep = feed.contexts[0].keep_title
    assert keep("Backend Engineer") and not keep("Senior Backend Engineer")


def test_feed_job_merges_with_an_existing_ats_job_by_url(runner, feed, connection, clock) -> None:
    from jobhunt.db import transaction
    from jobhunt.dedupe import upsert_jobs

    with transaction(connection):
        upsert_jobs(connection, [make_job(source="lever", source_job_id="L1",
                                          apply_url="https://jobs.lever.co/acme/ad208490-4052-4f91-a57e-433f2d1e484b")],
                    company_id=None, seen_at="2026-10-01T00:00:00Z")
    feed.outcomes = [FetchResult(jobs=[feed_job(
        "9", apply_url="https://jobs.lever.co/acme/ad208490-4052-4f91-a57e-433f2d1e484b/apply?utm_source=feed")])]
    summary = run(runner, clock)
    assert (summary.jobs_new, summary.jobs_merged) == (0, 1)
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM job_sightings").fetchone()[0] == 2


def test_real_adapter_through_the_runner_counts_fetches_against_the_cap(
    connection: sqlite3.Connection, config: Config, http_client: HttpClient, clock: FakeClock, router: respx.MockRouter, load_fixture
) -> None:
    route = router.get(url__regex=r"https://remotive\.com/api/remote-jobs.*").mock(
        return_value=httpx.Response(200, json=load_fixture("feeds/remotive.json")))
    remotive = RemotiveSource({"categories": ["software-dev", "data", "devops", "qa", "design"]})
    runner = IngestionRunner(connection, config, {"remotive": remotive}, http_client, clock=clock)
    clock.advance(hours=1)
    summary = runner.run(["remotive"])[0]
    assert route.call_count == 4                                      # provider cap = 4 per day
    assert connection.execute("SELECT fetch_count_today FROM source_state WHERE source = 'remotive'").fetchone()[0] == 4
    again = runner.run(["remotive"], force=True)[0]
    assert "daily fetch cap" in (again.skipped_reason or "") and route.call_count == 4
    assert summary.jobs_seen == 3


class TestEnvFile:
    def test_parsing(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text(
            "# comment\n\nADZUNA_APP_ID=abc123\nexport ADZUNA_APP_KEY = \"quoted value\"\nEMPTY=\n"
            "WITH_COMMENT=value # trailing\nnot a pair\n=novalue\n", encoding="utf-8")
        assert parse_env_file(path) == {
            "ADZUNA_APP_ID": "abc123", "ADZUNA_APP_KEY": "quoted value", "EMPTY": "", "WITH_COMMENT": "value",
        }

    def test_missing_file_and_precedence_and_empty_values_dropped(self, tmp_path: Path) -> None:
        assert parse_env_file(tmp_path / "nope.env") == {}
        (tmp_path / ".env").write_text("A=from-file\nB=file-b\nC=\n", encoding="utf-8")
        secrets = load_secrets(tmp_path, {"A": "from-env"})
        assert secrets == {"A": "from-env", "B": "file-b"}
