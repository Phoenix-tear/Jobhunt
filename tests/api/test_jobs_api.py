"""GET /api/jobs, /api/jobs/{id}, /api/facets: filters, defaults, convention, search, pagination."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from jobhunt.api.search import to_match_expression, tokenize_query
from tests.api.conftest import add_job, add_relevance, titles


class TestDefaults:
    def test_open_only_hide_senior_newest_first(self, client: TestClient, db: sqlite3.Connection) -> None:
        add_job(db, title="Old Posting", posted_at="2026-09-01T00:00:00Z")
        add_job(db, title="Fresh Posting", posted_at="2026-10-11T00:00:00Z")
        add_job(db, title="Senior Posting", experience_level="senior", posted_at="2026-10-12T00:00:00Z")
        add_job(db, title="Closed Posting", status="closed", posted_at="2026-10-12T00:00:00Z")
        body = client.get("/api/jobs").json()
        assert titles(body) == ["Fresh Posting", "Old Posting"]
        assert body["total"] == 2 and body["sort"] == "newest" and body["page"] == 1

    def test_toggles(self, client: TestClient, db: sqlite3.Connection) -> None:
        add_job(db, title="Open Entry")
        add_job(db, title="Open Senior", experience_level="senior")
        add_job(db, title="Closed Entry", status="closed")
        assert set(titles(client.get("/api/jobs", params={"hide_senior": "false"}).json())) == {"Open Entry", "Open Senior"}
        assert titles(client.get("/api/jobs", params={"status": "closed"}).json()) == ["Closed Entry"]
        assert client.get("/api/jobs", params={"status": "all", "hide_senior": "false"}).json()["total"] == 3

    def test_explicit_senior_level_overrides_hide_senior(self, client: TestClient, db: sqlite3.Connection) -> None:
        add_job(db, title="Senior One", experience_level="senior")
        add_job(db, title="Entry One", experience_level="entry")
        assert titles(client.get("/api/jobs", params={"level": "senior"}).json()) == ["Senior One"]

    def test_newest_falls_back_to_first_seen_when_posted_at_is_missing(self, client: TestClient, db: sqlite3.Connection) -> None:
        add_job(db, title="Posted Early", posted_at="2026-10-01T00:00:00Z")
        add_job(db, title="Never Posted But Seen Late", posted_at=None, first_seen_at="2026-10-11T00:00:00Z")
        assert titles(client.get("/api/jobs").json()) == ["Never Posted But Seen Late", "Posted Early"]


class TestFilters:
    def test_levels_are_multi_valued(self, client: TestClient, db: sqlite3.Connection) -> None:
        for level in ("intern", "new_grad", "entry", "mid"):
            add_job(db, title=f"Job {level}", experience_level=level)
        body = client.get("/api/jobs", params=[("level", "intern"), ("level", "new_grad")]).json()
        assert set(titles(body)) == {"Job intern", "Job new_grad"}

    def test_invalid_enum_values_are_rejected(self, client: TestClient) -> None:
        assert client.get("/api/jobs", params={"level": "wizard"}).status_code == 422
        assert client.get("/api/jobs", params={"employment_type": "unknown"}).status_code == 422
        assert client.get("/api/jobs", params={"country": "India"}).status_code == 422
        assert client.get("/api/jobs", params={"source": "x; DROP TABLE jobs"}).status_code == 422
        assert client.get("/api/jobs", params={"status": "pending"}).status_code == 422

    def test_countries_including_unknown(self, client: TestClient, db: sqlite3.Connection) -> None:
        add_job(db, title="In India", country="IN")
        add_job(db, title="In US", country="US")
        add_job(db, title="Nowhere", country=None)
        assert titles(client.get("/api/jobs", params=[("country", "in")]).json()) == ["In India"]   # case-insensitive
        both = client.get("/api/jobs", params=[("country", "US"), ("country", "unknown")]).json()
        assert set(titles(both)) == {"In US", "Nowhere"}

    def test_remote(self, client: TestClient, db: sqlite3.Connection) -> None:
        add_job(db, title="Remote Job", is_remote=1)
        add_job(db, title="Onsite Job", is_remote=0)
        add_job(db, title="Unknown Job", is_remote=None)
        assert titles(client.get("/api/jobs", params={"remote": "true"}).json()) == ["Remote Job"]
        assert titles(client.get("/api/jobs", params={"remote": "false"}).json()) == ["Onsite Job"]

    def test_source_filter_matches_any_sighting(self, client: TestClient, db: sqlite3.Connection) -> None:
        add_job(db, title="Only Greenhouse", sources=[("greenhouse", "https://boards.greenhouse.io/a/jobs/1")])
        add_job(db, title="Both", sources=[("greenhouse", "https://boards.greenhouse.io/a/jobs/2"),
                                           ("remotive", "https://remotive.com/remote-jobs/x-2")])
        body = client.get("/api/jobs", params={"source": "remotive"}).json()
        assert titles(body) == ["Both"] and body["items"][0]["sources"] == ["greenhouse", "remotive"]

    def test_new_within_hours_uses_posted_then_first_seen(self, client: TestClient, db: sqlite3.Connection) -> None:
        from jobhunt.timeutil import to_iso, utc_now
        from datetime import timedelta

        now = utc_now()
        add_job(db, title="Hours Old", posted_at=to_iso(now - timedelta(hours=3)))
        add_job(db, title="Days Old", posted_at=to_iso(now - timedelta(days=4)))
        add_job(db, title="Unposted Fresh", posted_at=None, first_seen_at=to_iso(now - timedelta(hours=1)))
        add_job(db, title="Unposted Stale", posted_at=None, first_seen_at=to_iso(now - timedelta(days=9)))
        body = client.get("/api/jobs", params={"new_within_hours": 24}).json()
        assert set(titles(body)) == {"Hours Old", "Unposted Fresh"}

    def test_needs_description(self, client: TestClient, db: sqlite3.Connection) -> None:
        add_job(db, title="Has Text", needs_description=0)
        add_job(db, title="Needs Text", needs_description=1, description_text=None)
        assert titles(client.get("/api/jobs", params={"needs_description": "true"}).json()) == ["Needs Text"]
        assert titles(client.get("/api/jobs", params={"needs_description": "false"}).json()) == ["Has Text"]

    def test_filters_combine_with_and(self, client: TestClient, db: sqlite3.Connection) -> None:
        add_job(db, title="Match", country="IN", experience_level="intern", is_remote=1)
        add_job(db, title="Wrong Country", country="US", experience_level="intern", is_remote=1)
        add_job(db, title="Wrong Level", country="IN", experience_level="mid", is_remote=1)
        body = client.get("/api/jobs", params={"country": "IN", "level": "intern", "remote": "true"}).json()
        assert titles(body) == ["Match"]


class TestEmploymentTypeConvention:
    """unknown counts as full-time unless the level is intern; stored values never change."""

    @pytest.fixture()
    def seeded(self, db: sqlite3.Connection) -> None:
        add_job(db, title="Stored Full Time", employment_type="full_time", experience_level="mid")
        add_job(db, title="Unknown Mid", employment_type="unknown", experience_level="mid")
        add_job(db, title="Unknown Level Unknown", employment_type="unknown", experience_level="unknown")
        add_job(db, title="Unknown Intern", employment_type="unknown", experience_level="intern")
        add_job(db, title="Stored Internship", employment_type="internship", experience_level="intern")
        add_job(db, title="Stored Contract", employment_type="contract", experience_level="mid")
        add_job(db, title="Stored Part Time", employment_type="part_time", experience_level="entry")

    def test_full_time_filter_includes_unknown(self, client: TestClient, seeded: None) -> None:
        body = client.get("/api/jobs", params={"employment_type": "full_time"}).json()
        assert set(titles(body)) == {"Stored Full Time", "Unknown Mid", "Unknown Level Unknown"}

    def test_internship_filter_includes_unknown_interns_only(self, client: TestClient, seeded: None) -> None:
        body = client.get("/api/jobs", params={"employment_type": "internship"}).json()
        assert set(titles(body)) == {"Unknown Intern", "Stored Internship"}

    def test_contract_and_part_time_are_exact(self, client: TestClient, seeded: None) -> None:
        assert titles(client.get("/api/jobs", params={"employment_type": "contract"}).json()) == ["Stored Contract"]
        assert titles(client.get("/api/jobs", params={"employment_type": "part_time"}).json()) == ["Stored Part Time"]

    def test_response_shows_effective_and_stored_values(self, client: TestClient, seeded: None) -> None:
        items = {item["title"]: item for item in client.get("/api/jobs", params={"q": "unknown"}).json()["items"]}
        assert (items["Unknown Mid"]["employment_type"], items["Unknown Mid"]["employment_type_raw"]) == ("full_time", "unknown")
        assert (items["Unknown Intern"]["employment_type"], items["Unknown Intern"]["employment_type_raw"]) == ("internship", "unknown")

    def test_nothing_is_written_to_the_database(self, client: TestClient, seeded: None, db: sqlite3.Connection) -> None:
        client.get("/api/jobs", params={"employment_type": "full_time"})
        stored = {row["title"]: row["employment_type"] for row in db.execute("SELECT title, employment_type FROM jobs")}
        assert stored["Unknown Mid"] == "unknown" and stored["Unknown Intern"] == "unknown"

    def test_facets_use_the_same_convention(self, client: TestClient, seeded: None) -> None:
        facets = {item["value"]: item["count"] for item in client.get("/api/facets").json()["employment_types"]}
        assert facets == {"full_time": 3, "internship": 2, "contract": 1, "part_time": 1}


class TestSorting:
    def test_company_and_title(self, client: TestClient, db: sqlite3.Connection) -> None:
        add_job(db, title="Zeta", company_name="beta corp")
        add_job(db, title="Alpha", company_name="Zulu Inc")
        add_job(db, title="Mid", company_name="Alpha Labs")
        assert titles(client.get("/api/jobs", params={"sort": "company"}).json()) == ["Mid", "Zeta", "Alpha"]
        assert titles(client.get("/api/jobs", params={"sort": "title"}).json()) == ["Alpha", "Mid", "Zeta"]

    def test_match_sort_puts_scored_jobs_first_and_unscored_by_newest(
            self, client: TestClient, db: sqlite3.Connection) -> None:
        older = add_job(db, title="Older", posted_at="2026-10-01T00:00:00Z")
        newer = add_job(db, title="Newer", posted_at="2026-10-05T00:00:00Z")
        add_job(db, title="Best", posted_at="2026-09-01T00:00:00Z")
        add_relevance(db, older, final_score=91.5)
        add_relevance(db, newer, final_score=60)
        body = client.get("/api/jobs", params={"sort": "match"}).json()
        assert titles(body) == ["Older", "Newer", "Best"] and body["sort"] == "match"
        assert [item["match_score"] for item in body["items"]] == [91.5, 60, None]

    def test_relevance_ranks_title_hits_first_and_needs_a_query(self, client: TestClient, db: sqlite3.Connection) -> None:
        add_job(db, title="Barista", description_text="python python python", posted_at="2026-10-09T00:00:00Z")
        add_job(db, title="Python Developer", description_text="we build apps", posted_at="2026-10-01T00:00:00Z")
        ranked = client.get("/api/jobs", params={"q": "python", "sort": "relevance"}).json()
        assert titles(ranked)[0] == "Python Developer" and ranked["sort"] == "relevance"
        no_query = client.get("/api/jobs", params={"sort": "relevance"}).json()
        assert no_query["sort"] == "newest"


class TestPagination:
    @pytest.fixture()
    def many(self, db: sqlite3.Connection) -> None:
        for index in range(130):
            add_job(db, title=f"Job {index:03d}", posted_at=f"2026-09-{(index % 28) + 1:02d}T00:00:00Z",
                    first_seen_at="2026-10-10T00:00:00Z")

    def test_pages_partition_the_results(self, client: TestClient, many: None) -> None:
        first = client.get("/api/jobs", params={"page_size": 50, "page": 1}).json()
        second = client.get("/api/jobs", params={"page_size": 50, "page": 2}).json()
        third = client.get("/api/jobs", params={"page_size": 50, "page": 3}).json()
        ids = [i["id"] for p in (first, second, third) for i in p["items"]]
        assert (first["total"], len(first["items"]), len(second["items"]), len(third["items"])) == (130, 50, 50, 30)
        assert len(set(ids)) == 130

    def test_page_size_is_capped_and_floored(self, client: TestClient, many: None) -> None:
        assert client.get("/api/jobs", params={"page_size": 5000}).json()["page_size"] == 100
        assert len(client.get("/api/jobs", params={"page_size": 5000}).json()["items"]) == 100
        assert client.get("/api/jobs", params={"page_size": 0}).json()["page_size"] == 1
        assert client.get("/api/jobs", params={"page_size": -7}).json()["page_size"] == 1

    def test_out_of_range_pages(self, client: TestClient, many: None) -> None:
        beyond = client.get("/api/jobs", params={"page": 9999}).json()
        assert beyond["items"] == [] and beyond["total"] == 130
        assert client.get("/api/jobs", params={"page": 0}).status_code == 422
        assert client.get("/api/jobs", params={"page": "abc"}).status_code == 422
        assert client.get("/api/jobs", params={"page": 10**12}).status_code == 200      # clamped, not an error


class TestSearch:
    @pytest.fixture()
    def seeded(self, db: sqlite3.Connection) -> None:
        add_job(db, title="Backend Engineer", company_name="Globex Corporation", location_raw="Pune, India",
                description_text="Python and PostgreSQL. We love 'quotes' and \"double quotes\" & C++ (node.js).")
        add_job(db, title="Data Scientist", company_name="Initech", location_raw="Remote - Germany",
                description_text="Machine learning at scale. Ingénieur café.")
        add_job(db, title="Frontend Developer", company_name="Umbrella", location_raw="London",
                description_text="React and TypeScript.")

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("backend", ["Backend Engineer"]),
            ("BACKEND ENGINEER", ["Backend Engineer"]),
            ("engineers", ["Backend Engineer"]),                 # stemming
            ("engin", ["Backend Engineer"]),                     # prefix on the last word
            ("globex", ["Backend Engineer"]),                    # company
            ("pune", ["Backend Engineer"]),                      # location
            ("postgresql", ["Backend Engineer"]),                # description
            ("machine learning", ["Data Scientist"]),
            ("cafe ingenieur", ["Data Scientist"]),              # diacritics folded
            ("c++", ["Backend Engineer"]),
            ("node.js", ["Backend Engineer"]),
            ("react typescript", ["Frontend Developer"]),
            ("python react", []),                                # AND semantics
            ("zzzznotthere", []),
        ],
    )
    def test_matches(self, client: TestClient, seeded: None, query: str, expected: list[str]) -> None:
        body = client.get("/api/jobs", params={"q": query}).json()
        assert sorted(titles(body)) == sorted(expected)

    @pytest.mark.parametrize(
        "hostile",
        [
            '"', '""', '"unterminated', "'", "''", "(", ")", "((()))", "*", "**", "^", ":", "title:engineer",
            "NOT", "AND", "OR", "engineer AND", "NEAR(a b)", "a OR b NOT c", "-engineer", "+", "{}", "[]",
            "\\", "%", "_", ";", "--", "/* */", "'; DROP TABLE jobs; --", '" OR 1=1 --', "engineer\"*",
            "😀", "日本語", "\x00", "\n\t", "   ", "", "a" * 10_000, ("word " * 2000), "é" * 500,
        ],
    )
    def test_hostile_input_never_causes_an_error(self, client: TestClient, seeded: None, db: sqlite3.Connection, hostile: str) -> None:
        response = client.get("/api/jobs", params={"q": hostile})
        assert response.status_code == 200, hostile[:40]
        assert isinstance(response.json()["total"], int)
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3     # nothing was dropped

    def test_empty_and_symbol_only_queries_return_everything(self, client: TestClient, seeded: None) -> None:
        assert client.get("/api/jobs", params={"q": ""}).json()["total"] == 3
        assert client.get("/api/jobs", params={"q": "!!!"}).json()["total"] == 3

    def test_search_composes_with_filters_and_counts(self, client: TestClient, seeded: None) -> None:
        body = client.get("/api/jobs", params={"q": "engineer", "country": "IN"}).json()
        assert body["total"] == 1
        assert client.get("/api/jobs", params={"q": "engineer", "country": "US"}).json()["total"] == 0

    def test_index_follows_inserts_updates_and_deletes(self, client: TestClient, db: sqlite3.Connection) -> None:
        job_id = add_job(db, title="Quantum Plumber", description_text="pipes")
        assert titles(client.get("/api/jobs", params={"q": "quantum"}).json()) == ["Quantum Plumber"]
        db.execute("UPDATE jobs SET title = 'Cryogenic Welder' WHERE id = ?", (job_id,))
        assert client.get("/api/jobs", params={"q": "quantum"}).json()["total"] == 0
        assert titles(client.get("/api/jobs", params={"q": "cryogenic"}).json()) == ["Cryogenic Welder"]
        db.execute("UPDATE jobs SET last_seen_at = '2026-10-13T00:00:00Z', missed_runs = 0 WHERE id = ?", (job_id,))
        assert titles(client.get("/api/jobs", params={"q": "cryogenic"}).json()) == ["Cryogenic Welder"]
        db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        assert client.get("/api/jobs", params={"q": "cryogenic"}).json()["total"] == 0
        db.execute("INSERT INTO jobs_fts(jobs_fts, rank) VALUES('integrity-check', 1)")   # raises if inconsistent

    def test_match_expression_builder(self) -> None:
        assert to_match_expression("Machine Learning") == '"Machine" "Learning" *'
        assert to_match_expression('say "hello"') == '"say" "hello" *'
        assert to_match_expression("c++ node.js") == '"c++" "node.js" *'
        assert to_match_expression("c++") == '"c++"' and to_match_expression("go") == '"go"'
        assert to_match_expression("") is None and to_match_expression("!!! ??? ***") is None
        assert tokenize_query("a b c d e f g h i j k l") == list("abcdefgh")           # token cap
        assert all(len(t) <= 40 for t in tokenize_query("x" * 500))


class TestDetail:
    def test_full_detail(self, client: TestClient, db: sqlite3.Connection) -> None:
        company = db.execute(
            "INSERT INTO companies (name, ats_type, slug, status, created_at) VALUES ('Acme Inc','greenhouse','acme','active','t')"
        ).lastrowid
        job_id = add_job(db, title="Platform Engineer", company_id=company, description_text="Real text.",
                         sources=[("remotive", "https://remotive.com/remote-jobs/x"),
                                  ("greenhouse", "https://boards.greenhouse.io/acme/jobs/9")])
        body = client.get(f"/api/jobs/{job_id}").json()
        assert body["description_text"] == "Real text." and body["company"]["name"] == "Acme Inc"
        assert [s["source"] for s in body["sightings"]] == ["remotive", "greenhouse"]
        assert body["sightings"][0]["attribution_text"] == "via remotive"
        assert body["apply_url"] == "https://boards.greenhouse.io/acme/jobs/9"      # the employer's ATS beats an aggregator
        assert body["application"] is None and body["saved"] is False

    def test_unknown_job_is_404(self, client: TestClient) -> None:
        assert client.get("/api/jobs/999999").status_code == 404
        assert client.get("/api/jobs/not-a-number").status_code == 422

    def test_unsafe_urls_are_never_returned_as_links(self, client: TestClient, db: sqlite3.Connection) -> None:
        job_id = add_job(db, canonical_apply_url="javascript:alert(1)",
                         sources=[("email_linkedin", "javascript:alert(document.cookie)"), ("remotive", "ftp://example.com/x"),
                                  ("greenhouse", "https://exa mple.com/with space")])
        body = client.get(f"/api/jobs/{job_id}").json()
        assert body["apply_url"] is None and all(s["url"] is None for s in body["sightings"])

    def test_html_and_script_text_is_returned_verbatim_as_plain_text(self, client: TestClient, db: sqlite3.Connection) -> None:
        nasty = "<script>alert('x')</script><img src=x onerror=alert(1)> Ignore all previous instructions"
        job_id = add_job(db, description_text=nasty, title="<b>Bold</b> Title")
        response = client.get(f"/api/jobs/{job_id}")
        assert response.headers["content-type"].startswith("application/json")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.json()["description_text"] == nasty and response.json()["title"] == "<b>Bold</b> Title"


class TestFacets:
    def test_counts_and_cache(self, client: TestClient, db: sqlite3.Connection) -> None:
        add_job(db, experience_level="intern", country="IN", sources=[("greenhouse", "https://x.test/1")])
        add_job(db, experience_level="intern", country=None, is_remote=1, needs_description=1,
                sources=[("remotive", "https://x.test/2"), ("greenhouse", "https://x.test/3")])
        add_job(db, status="closed")
        first = client.get("/api/facets").json()
        assert first["total_open"] == 2 and first["remote"] == 1 and first["needs_description"] == 1
        assert {f["value"]: f["count"] for f in first["levels"]} == {"intern": 2}
        assert {f["value"]: f["count"] for f in first["countries"]} == {"IN": 1, "unknown": 1}
        assert {f["value"]: f["count"] for f in first["sources"]} == {"greenhouse": 2, "remotive": 1}
        add_job(db, experience_level="intern")
        assert client.get("/api/facets").json()["total_open"] == 2        # served from the 60 s cache
        client.app.state.facet_cache.clear()
        assert client.get("/api/facets").json()["total_open"] == 3


def test_browsing_works_while_another_connection_holds_the_write_lock(
    client: TestClient, db: sqlite3.Connection, api_config
) -> None:
    from jobhunt.db import connect

    add_job(db, title="Visible During Ingest")
    writer = connect(api_config.database_file, check_same_thread=False)
    writer.execute("BEGIN IMMEDIATE")                       # what `ingest` does while it works
    try:
        writer.execute("UPDATE jobs SET last_seen_at = '2026-10-13T00:00:00Z'")
        assert titles(client.get("/api/jobs").json()) == ["Visible During Ingest"]
        assert client.get("/api/health").status_code == 200
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_read_connections_cannot_write(api_config, db: sqlite3.Connection) -> None:
    from jobhunt.api.database import open_read_connection

    add_job(db)
    reader = open_read_connection(api_config.database_file)
    with pytest.raises(sqlite3.OperationalError):
        reader.execute("UPDATE jobs SET title = 'hacked'")
    reader.close()


def test_make_client_fixture_is_reusable(make_client: Callable[..., TestClient]) -> None:
    assert make_client().get("/api/jobs").json()["total"] == 0
