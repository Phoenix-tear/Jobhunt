"""v6 API: categories and sorts are independent, Auto-hidden view, Restore, check-open, new Not applying reasons,
source reports, company-rule endpoints."""

from __future__ import annotations

import dataclasses
import itertools
import sqlite3
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from jobhunt import decisions, freshness
from jobhunt.api import queries
from jobhunt.config import Config
from jobhunt.enterprise.polite import PoliteFetcher
from jobhunt.matching import company_rules
from jobhunt.timeutil import to_iso, utc_now
from tests.api.conftest import add_job, add_relevance, titles

NOW = utc_now()


def ago(days: float) -> str:
    return to_iso(NOW - timedelta(days=days))


def seed_catalog(db: sqlite3.Connection) -> dict[str, int]:
    """Jobs across every category, with distinct dates and scores so each sort has a different order."""
    spec = [
        # name, gate status, posted days ago (None = no date), first seen days ago, score, company
        ("t1", "target", 2, 3, 90, "Zeta"), ("t2", "target", 20, 4, 70, "alpha"), ("t3", "target", None, 1, 85, "Mid"),
        ("m1", "maybe", 5, 6, 60, "Beta"), ("m2", "maybe", 40, 2, 55, "gamma"), ("m3", "maybe", None, 9, None, "Delta"),
        ("x1", "excluded", 1, 1, None, "Excl"), ("x2", "excluded", 3, 3, None, "Excl2"),
    ]
    ids: dict[str, int] = {}
    for name, status, posted, seen, score, company in spec:
        job = add_job(db, title=name, company_name=company, posted_at=ago(posted) if posted is not None else None,
                      first_seen_at=ago(seen))
        add_relevance(db, job, status, final_score=score, location_fit="india")
        ids[name] = job
    far = add_job(db, title="abroad", company_name="Far", posted_at=ago(1), first_seen_at=ago(1))
    add_relevance(db, far, "target", final_score=99, location_fit="wrong_location")
    ids["abroad"] = far
    return ids


SORTS = ["match", "newest_posted", "newest_seen", "oldest", "company", "title", "newest"]
CATEGORIES = ["relevant", "target", "maybe", "all", "filtered_out"]


def listing(client: TestClient, **params: object) -> dict:
    response = client.get("/api/jobs", params={"page_size": 100, **params})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("category", CATEGORIES)
def test_the_same_filter_returns_the_same_set_under_every_sort(client: TestClient, db: sqlite3.Connection, category: str) -> None:
    seed_catalog(db)
    sets = {sort: sorted(item["id"] for item in listing(client, category=category, sort=sort)["items"]) for sort in SORTS}
    assert len({tuple(value) for value in sets.values()}) == 1, sets
    totals = {listing(client, category=category, sort=sort)["total"] for sort in SORTS}
    assert len(totals) == 1


def test_categories_mean_what_they_say(client: TestClient, db: sqlite3.Connection) -> None:
    ids = seed_catalog(db)
    assert set(titles(listing(client, category="relevant"))) == {"t1", "t2", "t3", "m1", "m2", "m3"}
    assert set(titles(listing(client, category="target"))) == {"t1", "t2", "t3"}
    assert set(titles(listing(client, category="maybe"))) == {"m1", "m2", "m3"}
    assert set(titles(listing(client, category="all"))) == {"t1", "t2", "t3", "m1", "m2", "m3", "x1", "x2", "abroad"}
    assert set(titles(listing(client, category="filtered_out"))) == {"x1", "x2"}
    assert "abroad" not in titles(listing(client, category="target")) and ids["abroad"]
    assert set(titles(listing(client, category="target", include_other_locations="true"))) == {"t1", "t2", "t3", "abroad"}


def test_no_category_keeps_the_older_flags_working(client: TestClient, db: sqlite3.Connection) -> None:
    seed_catalog(db)
    assert set(titles(listing(client))) == {"t1", "t2", "t3", "m1", "m2", "m3"}
    assert "x1" in titles(listing(client, relevant_only="false"))


def test_each_sort_orders_as_specified(client: TestClient, db: sqlite3.Connection) -> None:
    seed_catalog(db)
    relevant = lambda sort: titles(listing(client, category="relevant", sort=sort))   # noqa: E731
    assert relevant("match")[:5] == ["t1", "t3", "t2", "m1", "m2"] and relevant("match")[-1] == "m3"
    posted = relevant("newest_posted")
    assert posted[:4] == ["t1", "m1", "t2", "m2"] and set(posted[4:]) == {"t3", "m3"}
    assert posted[4:] == ["t3", "m3"]                                     # no date: last, ties by first_seen_at (t3 is newer)
    assert relevant("newest_seen") == ["t3", "m2", "t1", "t2", "m1", "m3"]
    oldest = relevant("oldest")
    assert oldest[:4] == ["m2", "t2", "m1", "t1"] and oldest[4:] == ["m3", "t3"]
    assert relevant("company") == ["t2", "m1", "m3", "m2", "t3", "t1"]
    assert relevant("title") == sorted(relevant("title"))
    for sort in SORTS:
        assert listing(client, category="relevant", sort=sort)["sort"] == sort


def test_unknown_sort_keys_are_rejected(client: TestClient) -> None:
    assert client.get("/api/jobs", params={"sort": "id; drop table jobs"}).status_code == 422
    assert client.get("/api/jobs", params={"category": "everything"}).status_code == 422


def test_pagination_is_stable_with_ties_for_every_sort(client: TestClient, db: sqlite3.Connection) -> None:
    for number in range(30):                                     # identical dates / scores / companies: only the id breaks ties
        job = add_job(db, title=f"Same {number}", company_name="Same Co", posted_at=ago(5), first_seen_at=ago(5))
        add_relevance(db, job, "target", final_score=50, location_fit="india")
    for sort in SORTS:
        seen: list[int] = []
        for page in (1, 2, 3, 4):
            data = client.get("/api/jobs", params={"category": "relevant", "sort": sort, "page": page, "page_size": 8}).json()
            seen.extend(item["id"] for item in data["items"])
        assert len(seen) == 30 == len(set(seen)), sort
        full = [item["id"] for item in listing(client, category="relevant", sort=sort)["items"]]
        assert seen == full, sort


def test_sort_only_uses_whitelisted_sql(db: sqlite3.Connection) -> None:
    assert set(queries.SORT_KEYS) >= set(SORTS) | {"relevance"}
    for key in SORTS:
        assert "id" in queries._ORDER_BY[key].split(",")[-1]


@pytest.mark.parametrize(("category", "sort"), list(itertools.product(CATEGORIES, SORTS)))
def test_every_category_and_sort_combination_works_with_a_view_and_a_location_filter(
        client: TestClient, db: sqlite3.Connection, category: str, sort: str) -> None:
    seed_catalog(db)
    data = listing(client, category=category, sort=sort, location_fit="india")
    assert data["total"] == len(data["items"])
    assert all(item["location_fit"] == "india" for item in data["items"])


# --- freshness filter, Auto-hidden view, Restore ----------------------------------------------------------------------


def test_freshness_filter_and_the_auto_hidden_view(client: TestClient, db: sqlite3.Connection, api_config: Config) -> None:
    fresh = add_job(db, title="fresh one", posted_at=ago(3))
    stale = add_job(db, title="stale one", posted_at=ago(120))
    undated = add_job(db, title="undated one", posted_at=None)
    for job in (fresh, stale, undated):
        add_relevance(db, job, "target", location_fit="india")
    freshness.run_freshness(db, api_config)
    assert set(titles(listing(client))) == {"fresh one", "undated one"}                     # stale is hidden by default
    assert titles(listing(client, freshness="fresh")) == ["fresh one"]
    assert titles(listing(client, freshness="unknown")) == ["undated one"]
    assert set(titles(listing(client, freshness=["fresh", "aging", "unknown"]))) == {"fresh one", "undated one"}
    hidden = listing(client, view="auto_hidden")
    assert titles(hidden) == ["stale one"] and hidden["items"][0]["auto_hidden"] == "auto_stale"
    assert hidden["items"][0]["freshness"] == "stale" and hidden["items"][0]["age_days"] >= 119
    assert "stale one" not in titles(listing(client, view="all"))
    assert client.get("/api/facets").json()["auto_hidden"] == 1


def test_restore_makes_the_job_visible_and_it_stays_visible(client: TestClient, db: sqlite3.Connection, api_config: Config) -> None:
    stale = add_job(db, title="stale one", posted_at=ago(120))
    add_relevance(db, stale, "target", location_fit="india")
    freshness.run_freshness(db, api_config)
    assert client.post(f"/api/jobs/{stale}/restore").json() == {"job_id": stale, "restored": True}
    assert titles(listing(client)) == ["stale one"] and titles(listing(client, view="auto_hidden")) == []
    freshness.run_freshness(db, api_config)
    assert titles(listing(client)) == ["stale one"]
    assert client.post(f"/api/jobs/{stale}/restore").status_code == 404


def test_summary_and_detail_carry_freshness_and_liveness(client: TestClient, db: sqlite3.Connection, api_config: Config) -> None:
    job = add_job(db, title="x", posted_at=ago(10))
    add_relevance(db, job, "target", location_fit="india")
    freshness.run_freshness(db, api_config)
    detail = client.get(f"/api/jobs/{job}").json()
    assert detail["freshness"] == "fresh" and detail["liveness"] is None and detail["auto_hidden"] is None


def test_closed_saved_jobs_stay_in_the_saved_view(client: TestClient, db: sqlite3.Connection) -> None:
    job = add_job(db, title="saved but closed", status="closed")
    add_relevance(db, job, "target", location_fit="india")
    client.post(f"/api/jobs/{job}/decision/save")
    assert titles(listing(client, view="saved")) == ["saved but closed"]
    assert titles(listing(client)) == []


# --- check if still open -----------------------------------------------------------------------------------------------


def mock_fetcher(app_client: TestClient, answer: int | Exception, seen: list[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer)

    app_client.app.state.liveness_fetcher_factory = lambda: PoliteFetcher(     # type: ignore[attr-defined]
        "jobhunt-test", sleep=lambda s: None, transport=httpx.MockTransport(handler), max_retries=0)


def test_check_open_button_closed_open_and_uncertain(client: TestClient, db: sqlite3.Connection) -> None:
    job = add_job(db, title="x", sources=[("manual", "https://careers.example.com/jobs/9")])
    add_relevance(db, job, "target", location_fit="india")
    seen: list[str] = []
    mock_fetcher(client, 503, seen)
    body = client.post(f"/api/jobs/{job}/check-open").json()
    assert body["result"] == "uncertain" and body["method"] == "page" and body["checked_at"]
    assert db.execute("SELECT status FROM jobs WHERE id = ?", (job,)).fetchone()[0] == "open"
    mock_fetcher(client, 200, seen)
    assert client.post(f"/api/jobs/{job}/check-open").json()["result"] == "open"
    mock_fetcher(client, 404, seen)
    closed = client.post(f"/api/jobs/{job}/check-open").json()
    assert closed["result"] == "closed" and closed["action"] == "closed"
    assert db.execute("SELECT status, closed_reason FROM jobs WHERE id = ?", (job,)).fetchone()[:] == ("closed", "closed_checked")
    assert titles(listing(client, view="auto_hidden")) == ["x"] and titles(listing(client)) == []
    detail = client.get(f"/api/jobs/{job}").json()
    assert detail["liveness"]["result"] == "closed" and detail["auto_hidden"] == "closed_checked"
    assert all("careers.example.com" in url for url in seen)
    assert client.post("/api/jobs/99999/check-open").status_code == 404


def test_check_open_is_never_triggered_by_reading(client: TestClient, db: sqlite3.Connection) -> None:
    job = add_job(db, title="x")
    seen: list[str] = []
    mock_fetcher(client, 200, seen)
    client.get(f"/api/jobs/{job}")
    client.get("/api/jobs")
    client.get("/api/health")
    assert seen == []


# --- Not applying reasons ---------------------------------------------------------------------------------------------


def test_closed_or_unavailable_closes_the_job_locally_and_undo_reopens_it(client: TestClient, db: sqlite3.Connection) -> None:
    job = add_job(db, title="gone")
    add_relevance(db, job, "target", location_fit="india")
    assert client.post(f"/api/jobs/{job}/decision/not-applying", json={"reason": "closed_or_unavailable"}).status_code == 200
    state = db.execute("SELECT status, closed_reason FROM jobs WHERE id = ?", (job,)).fetchone()
    assert tuple(state) == ("closed", "reported_closed")
    assert titles(listing(client, view="not_applying")) == ["gone"]
    client.post(f"/api/jobs/{job}/decision/undo")
    assert tuple(db.execute("SELECT status, closed_reason FROM jobs WHERE id = ?", (job,)).fetchone()) == ("open", None)


def test_too_old_and_ineligible_level_are_recorded_without_closing(client: TestClient, db: sqlite3.Connection) -> None:
    old, senior = add_job(db, title="old"), add_job(db, title="Vice President")
    for job, reason in ((old, "too_old"), (senior, "ineligible_level")):
        assert client.post(f"/api/jobs/{job}/decision/not-applying", json={"reason": reason}).status_code == 200
        assert db.execute("SELECT status FROM jobs WHERE id = ?", (job,)).fetchone()[0] == "open"
        assert db.execute("SELECT reason FROM job_decision_current WHERE job_id = ?", (job,)).fetchone()[0] == reason
    assert {"closed_or_unavailable", "too_old", "ineligible_level"} <= set(decisions.NOT_APPLYING_REASONS)


def test_source_report_counts_reports_versus_shown_and_proposes_without_applying(
        client: TestClient, db: sqlite3.Connection, api_config: Config) -> None:
    for number in range(12):
        job = add_job(db, title=f"Dead role {number}", sources=[("arbeitnow", f"https://x.example/{number}")])
        add_relevance(db, job, "target", location_fit="india")
        reason = "closed_or_unavailable" if number % 2 else "too_old"
        client.post(f"/api/jobs/{job}/decision/not-applying", json={"reason": reason})
    for number in range(5):
        job = add_job(db, title=f"Fine {number}", sources=[("arbeitnow", f"https://y.example/{number}")])
        add_relevance(db, job, "target", location_fit="india")
    other = add_job(db, title="Other", sources=[("lever", "https://l.example/1")])
    add_relevance(db, other, "target", location_fit="india")
    health = client.get("/api/health").json()["freshness"]
    report = next(item for item in health["source_reports"] if item["source"] == "arbeitnow")
    assert report["reported_closed"] == 6 and report["reported_too_old"] == 6 and report["shown"] == 5 and report["decided"] == 12
    assert report["closed_share"] == 1.0 and report["proposed_stale_after_days"] == 30 and "not applied" in report["proposal"]
    lever = next(item for item in health["source_reports"] if item["source"] == "lever")
    assert lever["proposal"] is None and lever["shown"] == 1
    assert api_config.freshness.stale_after_days == 45                       # a proposal is never applied
    assert health["auto_hidden_total"] == 0 and health["stale_after_days"] == 45


def test_fewer_than_ten_reports_never_propose(client: TestClient, db: sqlite3.Connection) -> None:
    for number in range(9):
        job = add_job(db, title=f"Dead {number}", sources=[("arbeitnow", f"https://x.example/{number}")])
        client.post(f"/api/jobs/{job}/decision/not-applying", json={"reason": "closed_or_unavailable"})
    report = next(i for i in client.get("/api/health").json()["freshness"]["source_reports"] if i["source"] == "arbeitnow")
    assert report["proposal"] is None


def test_health_lists_auto_hidden_by_source(client: TestClient, db: sqlite3.Connection, api_config: Config) -> None:
    for number in range(3):
        add_job(db, title=f"old {number}", posted_at=ago(100), sources=[("greenhouse", f"https://g.example/{number}")])
    add_job(db, title="old lever", posted_at=ago(100), sources=[("lever", "https://l.example/1")])
    freshness.run_freshness(db, api_config)
    health = client.get("/api/health").json()["freshness"]
    assert {item["value"]: item["count"] for item in health["auto_hidden_by_source"]} == {"greenhouse": 3, "lever": 1}
    assert health["auto_hidden_total"] == 4 and health["auto_hidden_by_reason"] == [{"value": "auto_stale", "count": 4}]


# --- company rules endpoints --------------------------------------------------------------------------------------------


@pytest.fixture()
def rules_client(make_client: Callable[..., TestClient], api_config: Config, tmp_path: Path) -> tuple[TestClient, Path]:
    config = dataclasses.replace(api_config, project_root=tmp_path, migrations_dir=str(api_config.migrations_directory))
    path = tmp_path / company_rules.COMPANY_RULES_FILE
    company_rules.ensure_template(path)
    return make_client(config), path


def test_company_rules_list_and_remove(rules_client: tuple[TestClient, Path]) -> None:
    client, path = rules_client
    data = client.get("/api/company-rules").json()
    assert data["rules"][0]["company"] == "Barclays" and data["rules"][0]["deny_level_codes"] == ["BA3", "BA2", "BA1"]
    assert client.delete("/api/company-rules/0").json()["rules"] == []
    assert "company: Barclays" not in path.read_text(encoding="utf-8") and client.delete("/api/company-rules/0").status_code == 404


def test_remember_for_this_company_previews_then_writes_only_after_confirmation(
        rules_client: tuple[TestClient, Path], db: sqlite3.Connection) -> None:
    client, path = rules_client
    job = add_job(db, title="Vice President, Technology", company_name="Citi")
    before = path.read_text(encoding="utf-8")
    preview = client.post("/api/company-rules/preview", json={"job_id": job}).json()
    assert preview["company"] == "Citi" and preview["pattern"] == "Vice President" and preview["already_denied"] is False
    assert '- company: "Citi"' in preview["text"] and 'deny_title_patterns: ["Vice President"]' in preview["text"]
    assert path.read_text(encoding="utf-8") == before                                      # a preview writes nothing
    assert client.post("/api/company-rules", json={"job_id": job, "pattern": "Vice President", "text": preview["text"]}).status_code == 422
    assert client.post("/api/company-rules", json={"job_id": job, "pattern": "Vice President", "text": "tampered",
                                                   "confirmed": True}).status_code == 409
    assert path.read_text(encoding="utf-8") == before
    written = client.post("/api/company-rules", json={"job_id": job, "pattern": "Vice President", "text": preview["text"],
                                                      "confirmed": True}).json()
    assert [rule["company"] for rule in written["rules"]] == ["Barclays", "Citi"]
    assert path.read_text(encoding="utf-8") == before + preview["text"]
    again = client.post("/api/company-rules/preview", json={"job_id": job}).json()
    assert again["already_denied"] is True
    client.post("/api/company-rules", json={"job_id": job, "pattern": "Vice President", "text": again["text"], "confirmed": True})
    assert path.read_text(encoding="utf-8").count("Vice President\"]") == 1                  # no duplicate line


def test_preview_needs_a_level_word_and_a_real_job(rules_client: tuple[TestClient, Path], db: sqlite3.Connection) -> None:
    client, _ = rules_client
    job = add_job(db, title="Data Analyst", company_name="Citi")
    assert client.post("/api/company-rules/preview", json={"job_id": job}).status_code == 422
    assert client.post("/api/company-rules/preview", json={"job_id": job, "pattern": "Director"}).json()["pattern"] == "Director"
    assert client.post("/api/company-rules/preview", json={"job_id": 99999}).status_code == 404


def test_the_detail_offers_the_level_word_to_remember(client: TestClient, db: sqlite3.Connection) -> None:
    senior = add_job(db, title="Assistant Vice President, Data")
    plain = add_job(db, title="Data Analyst")
    assert client.get(f"/api/jobs/{senior}").json()["remember_level_word"] == "Assistant Vice President"
    assert client.get(f"/api/jobs/{plain}").json()["remember_level_word"] is None
    assert timedelta(0) is not None


def test_facets_count_each_category_with_the_same_filter_as_the_list(client: TestClient, db: sqlite3.Connection) -> None:
    seed_catalog(db)
    counts = {item["value"]: item["count"] for item in client.get("/api/facets").json()["category_counts"]}
    for category in CATEGORIES:
        assert counts[category] == listing(client, category=category)["total"], category
    assert counts["relevant"] == 6 and counts["target"] == 3 and counts["maybe"] == 3 and counts["all"] == 9


def test_a_job_the_gate_has_not_judged_yet_is_only_in_all(client: TestClient, db: sqlite3.Connection) -> None:
    """The real cause of 'Newest first shows everything': a freshly ingested job has no verdict and no score, so Best match
    sorts it last but Newest puts it on top; it must not be listed as Relevant, Target or Maybe."""
    seed_catalog(db)
    add_job(db, title="just ingested", company_name="New", posted_at=ago(0), first_seen_at=ago(0))
    for category in ("relevant", "target", "maybe", "filtered_out"):
        for sort in SORTS:
            assert "just ingested" not in titles(listing(client, category=category, sort=sort)), (category, sort)
    assert "just ingested" in titles(listing(client, category="all", sort="newest_posted"))
    assert titles(listing(client, category="relevant", sort="newest_posted"))[0] == "t1"
    counts = {item["value"]: item["count"] for item in client.get("/api/facets").json()["category_counts"]}
    assert counts["relevant"] == 6 and counts["all"] == 10
