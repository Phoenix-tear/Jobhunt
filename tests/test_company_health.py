"""Dead-slug rules, monthly recheck and counter resets, driven by an injected fake clock."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from jobhunt import companies as repo
from jobhunt.companies import DiscoveredCompany, add_discovered_companies
from jobhunt.config import Config, LifecycleSettings
from jobhunt.db import transaction
from tests.conftest import FakeClock
from tests.helpers import insert_company


@pytest.fixture()
def settings(config: Config) -> LifecycleSettings:
    return config.lifecycle


def company_row(connection: sqlite3.Connection, company_id: int) -> sqlite3.Row:
    return repo.get_company(connection, company_id)


class TestDeadSlugRule:
    def test_three_failures_on_one_day_do_not_deactivate(self, connection, clock: FakeClock, settings) -> None:
        company = insert_company(connection, status="active")
        for _ in range(3):
            clock.advance(hours=1)
            status = repo.record_failure(connection, company.id, clock(), "HTTP 404", settings)
        row = company_row(connection, company.id)
        assert status == "active"
        assert (row["consecutive_failures"], row["distinct_failure_days"]) == (3, 1)

    def test_three_failures_over_two_days_deactivate(self, connection, clock: FakeClock, settings) -> None:
        company = insert_company(connection, status="active")
        repo.record_failure(connection, company.id, clock(), "HTTP 404", settings)
        clock.advance(hours=2)
        repo.record_failure(connection, company.id, clock(), "HTTP 404", settings)
        assert company_row(connection, company.id)["status"] == "active"
        clock.advance(days=1)
        status = repo.record_failure(connection, company.id, clock(), "HTTP 404", settings)
        row = company_row(connection, company.id)
        assert status == "inactive" and row["status"] == "inactive"
        assert (row["consecutive_failures"], row["distinct_failure_days"]) == (3, 2)
        assert row["last_error"] == "HTTP 404"

    def test_two_failures_over_two_days_is_not_enough(self, connection, clock: FakeClock, settings) -> None:
        company = insert_company(connection, status="active")
        repo.record_failure(connection, company.id, clock(), "x", settings)
        clock.advance(days=1)
        repo.record_failure(connection, company.id, clock(), "x", settings)
        assert company_row(connection, company.id)["status"] == "active"

    def test_success_resets_every_counter(self, connection, clock: FakeClock, settings) -> None:
        company = insert_company(connection, slug="acme", name="Acme Holdings", status="active")
        repo.record_failure(connection, company.id, clock(), "x", settings)
        clock.advance(days=1)
        repo.record_failure(connection, company.id, clock(), "x", settings)
        clock.advance(hours=1)
        repo.record_success(connection, company.id, clock(), name="Something Else")
        row = company_row(connection, company.id)
        assert (row["consecutive_failures"], row["distinct_failure_days"], row["last_error"], row["last_failure_at"]) == (0, 0, None, None)
        assert row["status"] == "active" and row["name"] == "Acme Holdings"   # a curated name is kept
        # ...so one more failure starts from zero again, and the earlier ones do not accumulate
        clock.advance(days=1)
        assert repo.record_failure(connection, company.id, clock(), "x", settings) == "active"

    def test_failures_must_be_consecutive(self, connection, clock: FakeClock, settings) -> None:
        company = insert_company(connection, status="active")
        for day in range(2):
            repo.record_failure(connection, company.id, clock(), "x", settings)
            clock.advance(days=1)
        repo.record_success(connection, company.id, clock())
        clock.advance(days=1)
        repo.record_failure(connection, company.id, clock(), "x", settings)
        assert company_row(connection, company.id)["status"] == "active"

    def test_transient_problems_never_count(self, connection, clock: FakeClock, settings) -> None:
        company = insert_company(connection, status="active")
        for _ in range(10):
            clock.advance(days=1)
            repo.record_transient_problem(connection, company.id, clock(), "timeout")
        row = company_row(connection, company.id)
        assert (row["status"], row["consecutive_failures"], row["last_error"]) == ("active", 0, "timeout")

    def test_placeholder_name_is_replaced_by_the_real_one(self, connection, clock: FakeClock) -> None:
        company = insert_company(connection, slug="acme-corp", name="Acme Corp", status="unverified")
        repo.record_success(connection, company.id, clock(), name="ACME Corporation, Inc.")
        assert company_row(connection, company.id)["name"] == "ACME Corporation, Inc."


class TestMonthlyRecheck:
    def test_inactive_company_is_due_only_after_the_recheck_window(self, connection, clock: FakeClock, settings) -> None:
        company = insert_company(connection, status="active")
        repo.record_failure(connection, company.id, clock(), "x", settings)
        clock.advance(days=1)
        repo.record_failure(connection, company.id, clock(), "x", settings)
        repo.record_failure(connection, company.id, clock(), "x", settings)
        assert company_row(connection, company.id)["status"] == "inactive"
        marked_at = clock()

        due_before = repo.companies_due_for_ingest(connection, "greenhouse", marked_at + timedelta(days=29), settings)
        due_after = repo.companies_due_for_ingest(connection, "greenhouse", marked_at + timedelta(days=30), settings)
        assert due_before == [] and [c.id for c in due_after] == [company.id]

    def test_successful_recheck_reactivates(self, connection, clock: FakeClock, settings) -> None:
        company = insert_company(connection, status="inactive")
        clock.advance(days=31)
        repo.record_success(connection, company.id, clock())
        assert company_row(connection, company.id)["status"] == "active"

    def test_failed_recheck_keeps_it_inactive_and_restarts_the_window(self, connection, clock: FakeClock, settings) -> None:
        company = insert_company(connection, status="inactive")
        connection.execute("UPDATE companies SET last_checked_at = ? WHERE id = ?",
                           ((clock() - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ"), company.id))
        assert repo.companies_due_for_ingest(connection, "greenhouse", clock(), settings)
        repo.record_failure(connection, company.id, clock(), "HTTP 404", settings)
        assert company_row(connection, company.id)["status"] == "inactive"
        assert repo.companies_due_for_ingest(connection, "greenhouse", clock() + timedelta(days=5), settings) == []

    def test_other_ats_and_unverified_are_never_returned(self, connection, clock: FakeClock, settings) -> None:
        insert_company(connection, "lever", "l", status="active")
        insert_company(connection, "greenhouse", "u", status="unverified")
        assert repo.companies_due_for_ingest(connection, "greenhouse", clock(), settings) == []


class TestAddDiscoveredCompanies:
    def test_inserts_as_unverified_and_is_idempotent(self, connection, clock: FakeClock) -> None:
        entries = [
            DiscoveredCompany("greenhouse", "stripe", discovered_via="commoncrawl:CC-MAIN-2026-39"),
            DiscoveredCompany("greenhouse", "STRIPE", discovered_via="seed"),   # same slug, other case
            DiscoveredCompany("lever", "stripe", region="eu", discovered_via="seed"),
        ]
        with transaction(connection):
            assert add_discovered_companies(connection, entries, clock()) == 2
            assert add_discovered_companies(connection, entries, clock()) == 0
        rows = {(r["ats_type"], r["slug"].lower()): r for r in connection.execute("SELECT * FROM companies")}
        assert rows[("greenhouse", "stripe")]["status"] == "unverified"
        assert rows[("greenhouse", "stripe")]["discovered_via"] == "commoncrawl:CC-MAIN-2026-39"  # first source wins
        assert rows[("greenhouse", "stripe")]["name"] == "Stripe"
        assert rows[("lever", "stripe")]["region"] == "eu"

    def test_region_is_learned_for_an_existing_company(self, connection, clock: FakeClock) -> None:
        insert_company(connection, "lever", "acme", status="unverified")
        with transaction(connection):
            add_discovered_companies(connection, [DiscoveredCompany("lever", "acme", region="eu")], clock())
        assert connection.execute("SELECT region FROM companies").fetchone()[0] == "eu"

    def test_empty_input(self, connection, clock: FakeClock) -> None:
        assert add_discovered_companies(connection, [], clock()) == 0
