"""Phase 1: schema + migration runner."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jobhunt.config import Config
from jobhunt.db import apply_migrations, connect, current_version, discover_migrations, transaction
from jobhunt.errors import MigrationError

EXPECTED_TABLES = {
    "companies", "jobs", "job_sightings", "ingestion_runs", "source_state", "processed_emails",
    "job_embeddings", "master_resume", "resume_versions", "applications",
    "application_stage_history", "schema_migrations",
}
RESERVED_EMPTY_TABLES = [
    "job_embeddings", "master_resume", "resume_versions", "applications", "application_stage_history",
]


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row["name"] for row in rows}


def _insert_company(connection: sqlite3.Connection, slug: str = "acme", ats_type: str = "greenhouse") -> int:
    cursor = connection.execute(
        "INSERT INTO companies (name, ats_type, slug, created_at) VALUES (?, ?, ?, '2026-01-01T00:00:00Z')",
        ("Acme", ats_type, slug),
    )
    return int(cursor.lastrowid)


def _insert_job(connection: sqlite3.Connection, **overrides: object) -> int:
    values = {
        "fingerprint": "fp", "title": "Software Engineer", "company_name": "Acme",
        "first_seen_at": "2026-01-01T00:00:00Z", "last_seen_at": "2026-01-01T00:00:00Z",
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", list(values.values()))
    return int(cursor.lastrowid)


def test_all_tables_are_created(connection: sqlite3.Connection) -> None:
    assert EXPECTED_TABLES <= _table_names(connection)


def test_wal_and_foreign_keys_are_enabled(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_reserved_tables_start_empty(connection: sqlite3.Connection) -> None:
    for table in RESERVED_EMPTY_TABLES:
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_expected_indexes_exist(connection: sqlite3.Connection) -> None:
    index_names = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {
        "idx_jobs_status_last_seen", "idx_jobs_company", "idx_jobs_fingerprint", "idx_jobs_canonical_url",
    } <= index_names


def test_migrations_are_idempotent(connection: sqlite3.Connection, config: Config) -> None:
    expected_versions = len(discover_migrations(config.migrations_directory))
    assert apply_migrations(connection, config.migrations_directory) == []
    assert current_version(connection) == expected_versions
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == expected_versions


def test_editing_an_applied_migration_is_rejected(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    script = migrations / "001_first.sql"
    script.write_text("CREATE TABLE a (id INTEGER);", encoding="utf-8")
    conn = connect(tmp_path / "db.sqlite3")
    apply_migrations(conn, migrations)
    script.write_text("CREATE TABLE a (id INTEGER, extra TEXT);", encoding="utf-8")
    with pytest.raises(MigrationError, match="modified after being applied"):
        apply_migrations(conn, migrations)


def test_failed_migration_rolls_back_completely(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_broken.sql").write_text(
        "CREATE TABLE good (id INTEGER);\nCREATE TABLE good (id INTEGER);", encoding="utf-8"
    )
    conn = connect(tmp_path / "db.sqlite3")
    with pytest.raises(MigrationError):
        apply_migrations(conn, migrations)
    assert "good" not in _table_names(conn)
    assert current_version(conn) == 0


def test_migration_file_name_and_duplicate_validation(tmp_path: Path) -> None:
    (tmp_path / "001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "bad-name.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="bad migration file name"):
        discover_migrations(tmp_path)
    (tmp_path / "bad-name.sql").unlink()
    (tmp_path / "001_b.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="duplicate migration version"):
        discover_migrations(tmp_path)


def test_later_migrations_apply_in_order(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "002_second.sql").write_text("INSERT INTO t VALUES (2);", encoding="utf-8")
    (migrations / "001_first.sql").write_text("CREATE TABLE t (n INTEGER); INSERT INTO t VALUES (1);", encoding="utf-8")
    conn = connect(tmp_path / "db.sqlite3")
    assert apply_migrations(conn, migrations) == [1, 2]
    assert [row["n"] for row in conn.execute("SELECT n FROM t ORDER BY rowid")] == [1, 2]


def test_sighting_is_unique_per_source_and_source_job_id(connection: sqlite3.Connection) -> None:
    job_id = _insert_job(connection)
    sql = (
        "INSERT INTO job_sightings (job_id, source, source_job_id, first_seen_at, last_seen_at) "
        "VALUES (?, 'greenhouse', '42', 't', 't')"
    )
    connection.execute(sql, (job_id,))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(sql, (job_id,))


def test_company_slug_is_unique_case_insensitively_per_ats(connection: sqlite3.Connection) -> None:
    _insert_company(connection, slug="Stripe")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_company(connection, slug="stripe")
    _insert_company(connection, slug="stripe", ats_type="lever")  # other ATS is fine


def test_check_constraints_reject_bad_enum_values(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_job(connection, experience_level="wizard")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_job(connection, status="pending")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_company(connection, ats_type="taleo")


def test_deleting_a_job_cascades_to_sightings(connection: sqlite3.Connection) -> None:
    job_id = _insert_job(connection)
    connection.execute(
        "INSERT INTO job_sightings (job_id, source, source_job_id, first_seen_at, last_seen_at) "
        "VALUES (?, 'lever', 'x', 't', 't')",
        (job_id,),
    )
    connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    assert connection.execute("SELECT COUNT(*) FROM job_sightings").fetchone()[0] == 0


def test_application_unique_per_job(connection: sqlite3.Connection) -> None:
    job_id = _insert_job(connection)
    sql = "INSERT INTO applications (job_id, created_at, updated_at) VALUES (?, 't', 't')"
    connection.execute(sql, (job_id,))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(sql, (job_id,))


def test_master_resume_is_single_row(connection: sqlite3.Connection) -> None:
    connection.execute("INSERT INTO master_resume (id, content_json, updated_at) VALUES (1, '{}', 't')")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO master_resume (id, content_json, updated_at) VALUES (2, '{}', 't')")


def test_transaction_commits_and_rolls_back(connection: sqlite3.Connection) -> None:
    with transaction(connection):
        _insert_company(connection, slug="kept")
    with pytest.raises(RuntimeError):
        with transaction(connection):
            _insert_company(connection, slug="discarded")
            raise RuntimeError("boom")
    slugs = [row["slug"] for row in connection.execute("SELECT slug FROM companies")]
    assert slugs == ["kept"]
    assert not connection.in_transaction
