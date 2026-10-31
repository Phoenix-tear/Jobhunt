"""Dedupe: canonical URLs, fingerprints and the idempotent upsert (both merge directions)."""

from __future__ import annotations

import sqlite3

import pytest

from jobhunt.db import transaction
from jobhunt.dedupe import (
    build_fingerprint, canonicalize_url, is_specific_job_url, location_key, normalize_company_name,
    normalize_title, upsert_jobs,
)
from jobhunt.models import EmploymentType, ExperienceLevel
from tests.helpers import insert_company, make_job

T1, T2, T3 = "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-03T00:00:00Z"


def upsert(connection: sqlite3.Connection, *jobs, seen_at: str = T1, company_id: int | None = None):
    with transaction(connection):
        return upsert_jobs(connection, list(jobs), company_id=company_id, seen_at=seen_at)


def count(connection: sqlite3.Connection, table: str) -> int:
    return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class TestCanonicalizeUrl:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://boards.greenhouse.io/acme/jobs/123?utm_source=x&gh_src=abc", "https://boards.greenhouse.io/acme/jobs/123"),
            ("HTTPS://Job-Boards.Greenhouse.io/acme/jobs/123/", "https://boards.greenhouse.io/acme/jobs/123"),
            ("https://stripe.com/jobs/search?gh_jid=8172510&utm_medium=email", "https://stripe.com/jobs/search?gh_jid=8172510"),
            ("https://jobs.lever.co/acme/ad208490-4052-4f91-a57e-433f2d1e484b/apply?lever-source=LinkedIn",
             "https://jobs.lever.co/acme/ad208490-4052-4f91-a57e-433f2d1e484b"),
            ("https://jobs.ashbyhq.com/acme/7458d4e9-da2e/application", "https://jobs.ashbyhq.com/acme/7458d4e9-da2e"),
            ("https://acme.recruitee.com/o/backend-dev/c/new", "https://acme.recruitee.com/o/backend-dev"),
            ("https://jobs.smartrecruiters.com/BoschGroup/744000155007949-brand-manager?oga=true",
             "https://jobs.smartrecruiters.com/BoschGroup/744000155007949?oga=true"),
            ("http://www.example.com/careers/job-1#apply", "https://example.com/careers/job-1"),
            ("https://example.com/jobs?b=2&a=1&ref=newsletter", "https://example.com/jobs?a=1&b=2"),
            ("https://example.com//jobs///1", "https://example.com/jobs/1"),
        ],
    )
    def test_equivalent_urls_collapse(self, raw: str, expected: str) -> None:
        assert canonicalize_url(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "mailto:a@b.c", "javascript:alert(1)", "ftp://x/y", "not a url", 42])
    def test_non_http_urls_are_none(self, raw: object) -> None:
        assert canonicalize_url(raw) is None  # type: ignore[arg-type]

    def test_lever_posting_page_and_apply_page_are_the_same_job(self) -> None:
        job_page = canonicalize_url("https://jobs.lever.co/acme/ad208490-4052-4f91-a57e-433f2d1e484b")
        apply_page = canonicalize_url("https://jobs.lever.co/acme/ad208490-4052-4f91-a57e-433f2d1e484b/apply")
        assert job_page == apply_page


class TestSpecificJobUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://boards.greenhouse.io/acme/jobs/123",
            "https://jobs.lever.co/acme/ad208490-4052-4f91-a57e-433f2d1e484b",
            "https://jobs.ashbyhq.com/acme/7458d4e9-da2e-47bd-98cb-adfda43d42b2",
            "https://apply.workable.com/j/81B46579FE",
            "https://jobs.smartrecruiters.com/BoschGroup/744000155007949",
            "https://acme.recruitee.com/o/backend-dev",
            "https://stripe.com/jobs/search?gh_jid=8172510",
            "https://acme.com/careers/backend-engineer-pune",
        ],
    )
    def test_specific(self, url: str) -> None:
        assert is_specific_job_url(url)

    @pytest.mark.parametrize(
        "url",
        ["https://acme.com", "https://acme.com/", "https://acme.com/careers", "https://acme.com/jobs",
         "https://boards.greenhouse.io/acme", None, ""],
    )
    def test_generic_pages_are_not_specific(self, url: str | None) -> None:
        assert not is_specific_job_url(url)


class TestFingerprint:
    def test_company_and_title_noise_is_ignored(self) -> None:
        a = build_fingerprint("Acme, Inc.", "Software Engineer (Remote)", "IN:bengaluru")
        b = build_fingerprint("ACME", "Software  Engineer", "IN:bengaluru")
        assert a == b

    def test_city_changes_the_fingerprint(self) -> None:
        pune = build_fingerprint("Acme", "Software Engineer", location_key("IN", "Pune", None, None))
        mumbai = build_fingerprint("Acme", "Software Engineer", location_key("IN", "Mumbai", None, None))
        assert pune != mumbai

    def test_title_and_company_change_the_fingerprint(self) -> None:
        base = build_fingerprint("Acme", "Software Engineer", "IN:pune")
        assert base != build_fingerprint("Acme", "Data Engineer", "IN:pune")
        assert base != build_fingerprint("Other", "Software Engineer", "IN:pune")

    def test_normalizers(self) -> None:
        assert normalize_company_name("The Acme Corporation, LLC") == "acme"
        assert normalize_company_name("Acme Technologies") == "acme"
        assert normalize_title("SDE-I [Pune] (Hybrid) & Platform") == "sde i and platform"

    def test_location_key_variants(self) -> None:
        assert location_key("IN", "Bengaluru", None, "x") == "IN:bengaluru"
        assert location_key(None, None, True, "Remote") == "remote:"
        assert location_key("US", None, True, "Remote - US") == "remote:US"
        assert location_key("DE", None, None, "Germany") == "DE"
        assert location_key(None, None, None, "  Somewhere  Else ") == "somewhere else"


class TestUpsertIdempotency:
    def test_first_insert_creates_job_and_sighting(self, connection: sqlite3.Connection) -> None:
        stats = upsert(connection, make_job(raw={"id": 1}, salary_min=100.0))
        assert (stats.created, stats.merged) == (1, 0)
        job = connection.execute("SELECT * FROM jobs").fetchone()
        assert job["status"] == "open" and job["first_seen_at"] == T1 and job["raw_json"] == '{"id":1}'
        assert job["canonical_apply_url"] == "https://boards.greenhouse.io/acme/jobs/1"
        assert job["description_hash"] and job["needs_description"] == 0
        assert count(connection, "job_sightings") == 1

    def test_rerun_never_duplicates_and_only_touches_last_seen(self, connection: sqlite3.Connection) -> None:
        upsert(connection, make_job(), seen_at=T1)
        stats = upsert(connection, make_job(), seen_at=T2)
        assert (stats.created, stats.unchanged, stats.updated) == (0, 1, 0)
        assert count(connection, "jobs") == 1 and count(connection, "job_sightings") == 1
        job = connection.execute("SELECT * FROM jobs").fetchone()
        sighting = connection.execute("SELECT * FROM job_sightings").fetchone()
        assert job["first_seen_at"] == T1 and job["last_seen_at"] == T2
        assert sighting["first_seen_at"] == T1 and sighting["last_seen_at"] == T2

    def test_changed_content_updates_in_place_and_invalidates_embedding(self, connection: sqlite3.Connection) -> None:
        upsert(connection, make_job(description_text="Old text"), seen_at=T1)
        connection.execute("UPDATE jobs SET embedded_at = '2026-10-01T12:00:00Z'")
        stats = upsert(connection, make_job(description_text="Brand new text", title="Software Engineer II"), seen_at=T2)
        assert (stats.updated, stats.created) == (1, 0)
        job = connection.execute("SELECT * FROM jobs").fetchone()
        assert job["description_text"] == "Brand new text" and job["title"] == "Software Engineer II"
        assert job["embedded_at"] is None and count(connection, "jobs") == 1

    def test_duplicate_ids_in_one_batch_are_collapsed(self, connection: sqlite3.Connection) -> None:
        stats = upsert(connection, make_job(), make_job(title="Dup"))
        assert stats.created == 1 and count(connection, "jobs") == 1

    def test_empty_description_marks_needs_description(self, connection: sqlite3.Connection) -> None:
        upsert(connection, make_job(description_text=None))
        assert connection.execute("SELECT needs_description FROM jobs").fetchone()[0] == 1

    def test_unknown_values_do_not_clobber_known_ones(self, connection: sqlite3.Connection) -> None:
        upsert(connection, make_job(country="IN", city="Pune", employment_type=EmploymentType.FULL_TIME,
                                    experience_level=ExperienceLevel.ENTRY, is_remote=False))
        upsert(connection, make_job(country=None, city=None), seen_at=T2)
        job = connection.execute("SELECT * FROM jobs").fetchone()
        assert (job["country"], job["city"], job["employment_type"], job["experience_level"]) == (
            "IN", "Pune", "full_time", "entry")


class TestCrossSourceMerging:
    def test_same_canonical_url_merges_into_one_job(self, connection: sqlite3.Connection) -> None:
        upsert(connection, make_job(source="greenhouse", source_job_id="1"))
        stats = upsert(connection, make_job(
            source="adzuna", source_job_id="az-9", title="Software Engineer - Acme",   # different title/company text
            company_name="Acme Ltd", apply_url="https://boards.greenhouse.io/acme/jobs/1?utm_source=adzuna",
            attribution_text="Jobs by Adzuna", description_text="Truncated…", needs_description=True,
        ))
        assert (stats.created, stats.merged) == (0, 1)
        assert count(connection, "jobs") == 1 and count(connection, "job_sightings") == 2
        sightings = {row["source"]: row for row in connection.execute("SELECT * FROM job_sightings")}
        assert sightings["adzuna"]["attribution_text"] == "Jobs by Adzuna"
        job = connection.execute("SELECT * FROM jobs").fetchone()
        assert job["title"] == "Software Engineer"                    # primary (first) sighting keeps identity
        assert job["description_text"] == "Build things."             # truncated text did not clobber it

    def test_same_fingerprint_different_urls_merges(self, connection: sqlite3.Connection) -> None:
        upsert(connection, make_job(source="greenhouse", source_job_id="1", country="IN", city="Bengaluru"))
        stats = upsert(connection, make_job(
            source="remotive", source_job_id="r1", apply_url="https://remotive.com/job/12345",
            company_name="ACME Inc.", title="Software Engineer (Remote OK)", country="IN", city="Bengaluru",
        ))
        assert stats.merged == 1 and count(connection, "jobs") == 1

    def test_same_title_in_two_cities_does_not_merge(self, connection: sqlite3.Connection) -> None:
        upsert(connection, make_job(source="greenhouse", source_job_id="1", country="IN", city="Pune",
                                    apply_url="https://boards.greenhouse.io/acme/jobs/1"))
        stats = upsert(connection, make_job(source="adzuna", source_job_id="a2", country="IN", city="Mumbai",
                                            apply_url="https://adzuna.example/ad/2"))
        assert (stats.created, stats.merged) == (1, 0)
        assert count(connection, "jobs") == 2

    def test_same_title_different_company_does_not_merge(self, connection: sqlite3.Connection) -> None:
        upsert(connection, make_job(source="greenhouse", source_job_id="1", country="IN", city="Pune"))
        upsert(connection, make_job(source="adzuna", source_job_id="a1", company_name="Globex", country="IN",
                                    city="Pune", apply_url="https://adzuna.example/ad/1"))
        assert count(connection, "jobs") == 2

    def test_two_postings_of_one_board_with_same_title_and_city_stay_separate(self, connection: sqlite3.Connection) -> None:
        upsert(connection, make_job(source_job_id="1", apply_url="https://boards.greenhouse.io/acme/jobs/1",
                                    country="US", city="New York"))
        upsert(connection, make_job(source_job_id="2", apply_url="https://boards.greenhouse.io/acme/jobs/2",
                                    country="US", city="New York"))
        assert count(connection, "jobs") == 2 and count(connection, "job_sightings") == 2

    def test_generic_careers_url_never_merges_different_jobs(self, connection: sqlite3.Connection) -> None:
        upsert(connection, make_job(source="hn", source_job_id="h1", title="Backend Engineer",
                                    apply_url="https://acme.com/careers", country="US", city="Austin"))
        stats = upsert(connection, make_job(source="hn", source_job_id="h2", title="ML Engineer",
                                            apply_url="https://acme.com/careers", country="US", city="Austin"))
        assert stats.created == 1 and count(connection, "jobs") == 2

    def test_merged_sighting_fills_gaps_but_complete_description_replaces_truncated(self, connection: sqlite3.Connection) -> None:
        upsert(connection, make_job(source="adzuna", source_job_id="a1", apply_url="https://adzuna.example/ad/1",
                                    description_text="Cut off…", needs_description=True, country="IN", city="Pune"))
        upsert(connection, make_job(source="greenhouse", source_job_id="g1", description_text="The full description.",
                                    country="IN", city="Pune"), seen_at=T2)
        job = connection.execute("SELECT * FROM jobs").fetchone()
        assert count(connection, "jobs") == 1
        assert job["description_text"] == "The full description." and job["needs_description"] == 0

    def test_company_id_is_filled_when_an_ats_sighting_arrives_later(self, connection: sqlite3.Connection) -> None:
        company = insert_company(connection)
        upsert(connection, make_job(source="adzuna", source_job_id="a1", apply_url="https://x.example/1",
                                    country="IN", city="Pune"), company_id=None)
        upsert(connection, make_job(source="greenhouse", source_job_id="g1", country="IN", city="Pune"),
               company_id=company.id, seen_at=T2)
        assert connection.execute("SELECT company_id FROM jobs").fetchone()[0] == company.id


class TestReopening:
    def test_closed_job_reopens_when_seen_again(self, connection: sqlite3.Connection) -> None:
        upsert(connection, make_job())
        connection.execute("UPDATE jobs SET status='closed', closed_at=?, missed_runs=3", (T2,))
        stats = upsert(connection, make_job(), seen_at=T3)
        assert stats.reopened == 1
        job = connection.execute("SELECT * FROM jobs").fetchone()
        assert (job["status"], job["closed_at"], job["missed_runs"], job["last_seen_at"]) == ("open", None, 0, T3)

    def test_closed_job_reopens_through_a_cross_source_sighting(self, connection: sqlite3.Connection) -> None:
        upsert(connection, make_job(country="IN", city="Pune"))
        connection.execute("UPDATE jobs SET status='closed', closed_at=?", (T2,))
        upsert(connection, make_job(source="adzuna", source_job_id="a1", apply_url="https://adzuna.example/1",
                                    country="IN", city="Pune"), seen_at=T3)
        assert connection.execute("SELECT status FROM jobs").fetchone()[0] == "open"
