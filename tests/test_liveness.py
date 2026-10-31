"""Still-open checks with mocked HTTP: every classification, the job-state effects, and the daily shortlist step."""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

import httpx
import pytest

from jobhunt import freshness, liveness
from jobhunt.config import Config, LivenessSettings
from jobhunt.enterprise.polite import PoliteFetcher
from jobhunt.liveness import CLOSED, OPEN, UNCERTAIN, LivenessChecker, Target
from tests.conftest import FakeClock
from tests.test_scoring import add_job, seed

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
Handler = Callable[[httpx.Request], httpx.Response]


def make_fetcher(routes: dict[str, Handler | httpx.Response | int | Exception], log: list[str] | None = None) -> PoliteFetcher:
    """Every host answers /robots.txt with 404 (no rules); other URLs are looked up by 'host/path' (or by host)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        key = f"{request.url.host}{request.url.path}"
        answer = routes.get(key, routes.get(request.url.host, 404))
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(request)
        return answer if isinstance(answer, httpx.Response) else httpx.Response(answer)

    return PoliteFetcher("jobhunt-test", requests_per_minute=6000, sleep=lambda seconds: None,
                         transport=httpx.MockTransport(handler), max_retries=0)


def check(target: Target, routes: dict, settings: LivenessSettings | None = None):  # noqa: ANN201
    return LivenessChecker(make_fetcher(routes), settings or LivenessSettings()).check(target)


def page(url: str = "https://careers.example.com/jobs/123", job_id: str = "123") -> Target:
    return Target(1, "Engineer", "open", url, sightings=(("workday", job_id, url),))


def ats(source: str, slug: str = "acme", job_id: str = "99") -> Target:
    return Target(1, "Engineer", "open", f"https://example.com/{job_id}", sightings=((source, job_id, None),),
                  ats_type=source, slug=slug)


# --- single-job endpoints of the ATS adapters -------------------------------------------------------------------------


@pytest.mark.parametrize(("source", "host_path"), [
    ("greenhouse", "boards-api.greenhouse.io/v1/boards/acme/jobs/99"),
    ("lever", "api.lever.co/v0/postings/acme/99"),
    ("smartrecruiters", "api.smartrecruiters.com/v1/companies/acme/postings/99"),
    ("recruitee", "acme.recruitee.com/api/offers/99"),
])
@pytest.mark.parametrize(("answer", "expected"), [
    (200, OPEN), (404, CLOSED), (410, CLOSED), (403, UNCERTAIN), (429, UNCERTAIN), (500, UNCERTAIN), (503, UNCERTAIN),
    (401, UNCERTAIN), (302, UNCERTAIN),
    (httpx.ConnectTimeout("slow"), UNCERTAIN), (httpx.ConnectError("down"), UNCERTAIN),
])
def test_ats_single_job_endpoints(source: str, host_path: str, answer: object, expected: str) -> None:
    outcome = check(ats(source), {host_path: answer})   # type: ignore[arg-type]
    assert outcome.result == expected and outcome.method in {"ats_api", "page"}
    if expected == CLOSED:
        assert outcome.method == "ats_api"


@pytest.mark.parametrize("source", ["greenhouse", "lever", "smartrecruiters", "recruitee"])
def test_a_slug_cannot_redirect_the_request_to_another_host(source: str) -> None:
    target = dataclasses.replace(ats(source), slug="evil.example.com/../x")
    assert liveness.api_url(target) is not None or source == "recruitee"
    if source != "recruitee":
        url, _ = liveness.api_url(target)    # type: ignore[misc]
        assert url.startswith("https://") and "evil.example.com/../x" not in url
    else:
        assert liveness.api_url(target) is None


def board(*ids: str, key: str = "id") -> httpx.Response:
    return httpx.Response(200, json={"jobs": [{key: job_id, "title": "x"} for job_id in ids]})


@pytest.mark.parametrize(("source", "host_path", "key"), [
    ("ashby", "api.ashbyhq.com/posting-api/job-board/acme", "id"),
    ("workable", "apply.workable.com/api/v1/widget/accounts/acme", "shortcode"),
])
def test_board_listing_membership_for_ashby_and_workable(source: str, host_path: str, key: str) -> None:
    target = ats(source)
    assert check(target, {host_path: board("99", "1", key=key)}).result == OPEN
    gone = check(target, {host_path: board("1", "2", key=key)})
    assert gone.result == CLOSED and "no longer listed" in gone.detail
    assert check(target, {host_path: board()}).result == UNCERTAIN                      # an empty board proves nothing
    assert check(target, {host_path: httpx.Response(200, text="<html>nope</html>")}).result == UNCERTAIN
    assert check(target, {host_path: 404}).result == UNCERTAIN                          # a missing board is not a closed job
    assert check(target, {host_path: 500}).result == UNCERTAIN


# --- plain page checks ------------------------------------------------------------------------------------------------


def test_page_status_classification() -> None:
    url = "careers.example.com/jobs/123"
    assert check(page(), {url: 200}).result == OPEN
    assert check(page(), {url: 404}).result == CLOSED
    assert check(page(), {url: 410}).result == CLOSED
    for status in (403, 429, 500, 502, 503):
        assert check(page(), {url: status}).result == UNCERTAIN, status


@pytest.mark.parametrize("text", [
    "Sorry, this position is no longer available.", "The position has been filled.", "This job has expired.",
    "Job not found", "We are no longer accepting applications for this role.", "This vacancy is closed.",
    "This posting has been removed",
])
def test_closed_wording_on_a_200_page_means_closed(text: str) -> None:
    outcome = check(page(), {"careers.example.com/jobs/123": httpx.Response(200, text=f"<html><body><h1>{text}</h1></body></html>")})
    assert outcome.result == CLOSED and "page says" in outcome.detail


def test_normal_pages_are_open_even_with_the_word_closed_elsewhere() -> None:
    body = "<html>Join us. Our office is closed on Sundays. Apply now! Experience required.</html>"
    assert check(page(), {"careers.example.com/jobs/123": httpx.Response(200, text=body)}).result == OPEN


def redirect(to: str) -> httpx.Response:
    return httpx.Response(302, headers={"location": to})


def test_redirect_away_from_the_posting_is_closed() -> None:
    routes = {"careers.example.com/jobs/123": redirect("/careers"), "careers.example.com/careers": 200}
    assert check(page(), routes).result == CLOSED
    routes = {"careers.example.com/jobs/123": redirect("https://www.other.org/jobs"), "www.other.org": 200}
    assert check(page(), routes).result == CLOSED
    routes = {"careers.example.com/jobs/123": redirect("/search?q=engineer"), "careers.example.com/search": 200}
    assert check(page(), routes).result == CLOSED


def test_harmless_redirects_stay_open() -> None:
    routes = {"careers.example.com/jobs/123": redirect("https://careers.example.com/jobs/123/"),
              "careers.example.com/jobs/123/": 200}
    assert check(page(), routes).result == OPEN
    routes = {"careers.example.com/jobs/123": redirect("/en/jobs/123?utm=x"), "careers.example.com/en/jobs/123": 200}
    assert check(page(), routes).result == OPEN


def test_bot_challenge_robots_and_network_errors_are_uncertain_never_closed() -> None:
    challenge = httpx.Response(200, text="<html>Just a moment... captcha</html>", headers={"content-type": "text/html"})
    assert check(page(), {"careers.example.com/jobs/123": challenge}).result == UNCERTAIN
    assert check(page(), {"careers.example.com/jobs/123": httpx.ReadTimeout("slow")}).result == UNCERTAIN
    assert check(page(), {"careers.example.com/jobs/123": httpx.ConnectError("down")}).result == UNCERTAIN

    def blocked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="User-agent: *\nDisallow: /jobs/") if request.url.path == "/robots.txt" else httpx.Response(200)

    fetcher = PoliteFetcher("jobhunt-test", sleep=lambda seconds: None, transport=httpx.MockTransport(blocked), max_retries=0)
    outcome = LivenessChecker(fetcher, LivenessSettings()).check(page())
    assert outcome.result == UNCERTAIN and "robots" in outcome.detail


def test_no_apply_url_is_uncertain() -> None:
    assert check(Target(1, "x", "open", None), {}).result == UNCERTAIN


def test_classify_page_and_moved_off_posting_directly() -> None:
    assert liveness.classify_page(200, "", "https://a.com/jobs/1", "https://a.com/jobs/1", "1") == (OPEN, "HTTP 200")
    assert liveness.moved_off_posting("https://a.com/jobs/1", "https://a.com/jobs/1", "1") is False
    assert liveness.moved_off_posting("https://a.com/jobs/1", "http://www.a.com/jobs/1/", "1") is False
    assert liveness.moved_off_posting("https://a.com/jobs/1", "https://a.com/", "1") is True
    assert liveness.moved_off_posting("https://a.com/jobs/1", "https://a.com/jobs/other-role-77", "1") is True
    assert liveness.moved_off_posting("https://a.com/jobs/1", "https://jobs.a.com/jobs/1", "1") is False


# --- database effects ------------------------------------------------------------------------------------------------


@pytest.fixture()
def cfg(config: Config) -> Config:
    return dataclasses.replace(config, scoring=dataclasses.replace(config.scoring, top_k=50, recent_days=3650),
                               freshness=dataclasses.replace(config.freshness, liveness=LivenessSettings(
                                   max_requests_per_run=100, seconds_between_requests=1.0, cache_hours=24)))


def add_greenhouse_job(connection: sqlite3.Connection, n: int, title: str = "Software Engineer") -> int:
    company = connection.execute("SELECT id FROM companies WHERE slug = 'acme'").fetchone()
    if company is None:
        connection.execute("INSERT INTO companies (name, ats_type, slug, status, created_at) "
                           "VALUES ('Acme', 'greenhouse', 'acme', 'active', 'x')")
        company = connection.execute("SELECT id FROM companies WHERE slug = 'acme'").fetchone()
    job = add_job(connection, n, f"{title} Platform{n}", "Python services.", company_id=company["id"])
    connection.execute("INSERT INTO job_sightings (job_id, source, source_job_id, source_url, first_seen_at, last_seen_at) "
                       "VALUES (?, 'greenhouse', ?, ?, 'x', 'x')", (job, f"gh{n}", f"https://boards.greenhouse.io/acme/jobs/{n}"))
    return job


def job_row(connection: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    return connection.execute("SELECT status, closed_at, closed_reason FROM jobs WHERE id = ?", (job_id,)).fetchone()


def hidden_row(connection: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM job_auto_hidden WHERE job_id = ?", (job_id,)).fetchone()


def test_closed_closes_the_job_locally_hides_it_and_records_the_check(connection: sqlite3.Connection) -> None:
    job = add_greenhouse_job(connection, 1)
    checker = LivenessChecker(make_fetcher({"boards-api.greenhouse.io/v1/boards/acme/jobs/gh1": 404}), LivenessSettings())
    result = liveness.check_job(connection, checker, job, clock=FakeClock(NOW))
    assert result["result"] == "closed" and result["action"] == "closed" and result["method"] == "ats_api"
    state = job_row(connection, job)
    assert state["status"] == "closed" and state["closed_reason"] == "closed_checked" and state["closed_at"]
    hidden = hidden_row(connection, job)
    assert hidden["reason"] == "closed_checked" and hidden["restored_at"] is None
    stored = connection.execute("SELECT result, http_status, method, trigger FROM job_liveness WHERE job_id = ?", (job,)).fetchone()
    assert tuple(stored) == ("closed", 404, "ats_api", "button")


@pytest.mark.parametrize("answer", [403, 429, 500, 503, httpx.ConnectTimeout("t")])
def test_uncertain_never_closes_or_hides(connection: sqlite3.Connection, answer: object) -> None:
    job = add_greenhouse_job(connection, 1)
    checker = LivenessChecker(make_fetcher({"boards-api.greenhouse.io/v1/boards/acme/jobs/gh1": answer}), LivenessSettings())  # type: ignore[dict-item]
    result = liveness.check_job(connection, checker, job, clock=FakeClock(NOW))
    assert result["result"] == "uncertain" and result["action"] is None
    assert job_row(connection, job)["status"] == "open" and hidden_row(connection, job) is None
    assert connection.execute("SELECT result FROM job_liveness WHERE job_id = ?", (job,)).fetchone()[0] == "uncertain"


def test_open_on_a_job_hidden_as_closed_restores_it(connection: sqlite3.Connection) -> None:
    job = add_greenhouse_job(connection, 1)
    routes: dict = {"boards-api.greenhouse.io/v1/boards/acme/jobs/gh1": 404}
    liveness.check_job(connection, LivenessChecker(make_fetcher(routes), LivenessSettings()), job, clock=FakeClock(NOW))
    routes["boards-api.greenhouse.io/v1/boards/acme/jobs/gh1"] = 200
    result = liveness.check_job(connection, LivenessChecker(make_fetcher(routes), LivenessSettings()), job, clock=FakeClock(NOW))
    assert result["action"] == "restored"
    assert job_row(connection, job)["status"] == "open" and hidden_row(connection, job)["restored_at"]


def test_restoring_a_closed_check_reopens_the_job(connection: sqlite3.Connection) -> None:
    job = add_greenhouse_job(connection, 1)
    liveness.check_job(connection, LivenessChecker(make_fetcher({"boards-api.greenhouse.io/v1/boards/acme/jobs/gh1": 404}),
                                                    LivenessSettings()), job, clock=FakeClock(NOW))
    assert freshness.restore_job(connection, job, "2026-10-21T12:00:00Z") is True
    state = job_row(connection, job)
    assert state["status"] == "open" and state["closed_reason"] is None and state["closed_at"] is None


def test_an_explicit_check_after_a_restore_can_hide_it_again(connection: sqlite3.Connection) -> None:
    job = add_greenhouse_job(connection, 1)
    closed = LivenessChecker(make_fetcher({"boards-api.greenhouse.io/v1/boards/acme/jobs/gh1": 404}), LivenessSettings())
    liveness.check_job(connection, closed, job, clock=FakeClock(NOW))
    freshness.restore_job(connection, job, "2026-10-21T12:00:00Z")
    liveness.check_job(connection, closed, job, clock=FakeClock(NOW))
    assert hidden_row(connection, job)["restored_at"] is None and job_row(connection, job)["status"] == "closed"


def test_unknown_job_returns_none(connection: sqlite3.Connection) -> None:
    assert liveness.check_job(connection, LivenessChecker(make_fetcher({}), LivenessSettings()), 999) is None


# --- the daily shortlist step ----------------------------------------------------------------------------------------


def seed_pool(connection: sqlite3.Connection, cfg: Config, count: int) -> list[int]:
    connection.execute(
        "INSERT INTO profiles (name, source_file, source_hash, text_hash, stripped_text, built_at) VALUES "
        "('ai', 'a.pdf', 'h', 't1', 'Builds RAG agents.', 'x'), ('fullstack', 'f.pdf', 'h', 't2', 'React.', 'x')")
    ids = [add_greenhouse_job(connection, n) for n in range(1, count + 1)]
    from jobhunt.matching.gate import run_relevance

    run_relevance(connection, cfg, clock=FakeClock(NOW))
    for n, job_id in enumerate(ids):
        connection.execute("UPDATE job_relevance SET similarity_ai = ?, similarity_fullstack = 0.5, best_profile = 'ai', "
                           "similarity_percentile = 0.5 WHERE job_id = ?", (0.8 - n * 0.001, job_id))
    return ids


def test_shortlist_closes_dead_postings_and_skips_them_from_the_pool(connection: sqlite3.Connection, cfg: Config) -> None:
    from jobhunt.matching import scoring

    ids = seed_pool(connection, cfg, 4)
    routes = {f"boards-api.greenhouse.io/v1/boards/acme/jobs/gh{n}": (404 if n == 2 else 200) for n in range(1, 5)}
    sleeps: list[float] = []
    stats = liveness.run_shortlist_liveness(connection, cfg, make_fetcher(routes), clock=FakeClock(NOW), sleep=sleeps.append)
    assert (stats.candidates, stats.checked, stats.open, stats.closed, stats.uncertain) == (4, 4, 3, 1, 0)
    assert sleeps == [1.0, 1.0, 1.0]                                    # one second apart
    assert job_row(connection, ids[1])["status"] == "closed" and hidden_row(connection, ids[1])["reason"] == "closed_checked"
    assert ids[1] not in {job.job_id for job in scoring._scoring_pool(connection, cfg, NOW.date())}


def test_shortlist_caches_for_24_hours(connection: sqlite3.Connection, cfg: Config) -> None:
    seed_pool(connection, cfg, 3)
    routes = {f"boards-api.greenhouse.io/v1/boards/acme/jobs/gh{n}": 200 for n in range(1, 4)}
    log: list[str] = []
    first = liveness.run_shortlist_liveness(connection, cfg, make_fetcher(routes, log), clock=FakeClock(NOW), sleep=lambda s: None)
    requests_after_first = len(log)
    second = liveness.run_shortlist_liveness(connection, cfg, make_fetcher(routes, log), clock=FakeClock(NOW.replace(hour=20)),
                                             sleep=lambda s: None)
    assert first.checked == 3 and second.checked == 0 and second.cached == 3 and len(log) == requests_after_first
    later = liveness.run_shortlist_liveness(connection, cfg, make_fetcher(routes, log),
                                            clock=FakeClock(datetime(2026, 10, 23, tzinfo=timezone.utc)), sleep=lambda s: None)
    assert later.checked == 3


def test_shortlist_never_sends_more_than_the_request_cap(connection: sqlite3.Connection, cfg: Config) -> None:
    seed_pool(connection, cfg, 40)
    tight = dataclasses.replace(cfg, freshness=dataclasses.replace(
        cfg.freshness, liveness=dataclasses.replace(cfg.freshness.liveness, max_requests_per_run=12)))
    routes = {"boards-api.greenhouse.io": 200}
    log: list[str] = []
    stats = liveness.run_shortlist_liveness(connection, tight, make_fetcher(routes, log), clock=FakeClock(NOW), sleep=lambda s: None)
    api_requests = [url for url in log if "/robots.txt" not in url]
    assert stats.capped and 0 < stats.checked <= 12 and stats.requests <= 12
    assert len(api_requests) == stats.checked


def test_shortlist_skips_restored_jobs_and_checks_saved_jobs_first(connection: sqlite3.Connection, cfg: Config) -> None:
    ids = seed_pool(connection, cfg, 3)
    connection.execute("INSERT INTO applications (job_id, stage, created_at, updated_at) VALUES (?, 'saved', 'x', 'x')", (ids[2],))
    freshness.hide_job(connection, ids[0], "auto_stale", "old", "2026-10-01T00:00:00Z")
    freshness.restore_job(connection, ids[0], "2026-10-02T00:00:00Z")
    order = liveness.shortlist_candidates(connection, cfg, NOW.date())
    assert order[0] == ids[2] and set(order) == set(ids)
    stats = liveness.run_shortlist_liveness(connection, cfg, make_fetcher({"boards-api.greenhouse.io": 200}),
                                            clock=FakeClock(NOW), sleep=lambda s: None)
    assert stats.skipped_restored == 1 and stats.checked == 2


def test_shortlist_includes_tailored_not_applied_jobs_and_excludes_applied(connection: sqlite3.Connection, cfg: Config) -> None:
    ids = seed_pool(connection, cfg, 3)
    for number, job in enumerate(ids[:2], 1):
        connection.execute(
            "INSERT INTO tailored_resumes (job_id, base_profile, mode, cache_key, status, created_at, pages, resume_number, "
            "version, lifecycle) VALUES (?, 'ai', 'deterministic', ?, 'ok', 'x', 1, ?, 1, 'approved')", (job, f"k{number}", number))
    connection.execute("INSERT INTO applications (job_id, stage, created_at, updated_at) VALUES (?, 'applied', 'x', 'x')", (ids[1],))
    saved_or_tailored = [row[0] for row in connection.execute(
        "SELECT j.id FROM jobs j WHERE EXISTS (SELECT 1 FROM tailored_resumes t WHERE t.job_id = j.id) "
        "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id AND a.stage != 'saved')")]
    assert saved_or_tailored == [ids[0]]
    assert liveness.shortlist_candidates(connection, cfg, NOW.date())[0] == ids[0]


def test_network_trouble_never_fails_the_step(connection: sqlite3.Connection, cfg: Config) -> None:
    seed_pool(connection, cfg, 3)
    stats = liveness.run_shortlist_liveness(connection, cfg, make_fetcher({"boards-api.greenhouse.io": httpx.ConnectError("down")}),
                                            clock=FakeClock(NOW), sleep=lambda s: None)
    assert stats.checked == 3 and stats.uncertain == 3 and stats.closed == 0
    assert connection.execute("SELECT COUNT(*) FROM jobs WHERE status = 'closed'").fetchone()[0] == 0
    assert seed_pool is not None and seed is not None
