"""Tests for morning-check CLI command and health checks (Brief v14 Section 2)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from jobhunt.config import load_config
from jobhunt.schedule.morning_check import run_morning_check


@pytest.fixture(autouse=True)
def _no_real_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the installed-task lookup (schtasks / launchctl on the developer machine): every task counts as installed."""
    def unavailable(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("scheduler lookup disabled in tests")

    monkeypatch.setattr("jobhunt.schedule.backends.get_scheduler_backend", unavailable)


def _init_mock_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task TEXT NOT NULL,
            trigger TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            duration_seconds REAL,
            detail TEXT,
            error TEXT,
            exit_code INTEGER,
            log_file TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            day TEXT NOT NULL,
            model TEXT NOT NULL,
            purpose TEXT NOT NULL,
            calls INTEGER NOT NULL,
            items INTEGER NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            estimated_usd REAL NOT NULL,
            note TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_auto_hidden (
            job_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            detail TEXT,
            hidden_at TEXT NOT NULL,
            restored_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            title TEXT,
            company_name TEXT,
            location_raw TEXT,
            workplace_type TEXT,
            remote_regions TEXT,
            description_text TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_relevance (
            job_id INTEGER PRIMARY KEY,
            gate_status TEXT NOT NULL,
            location_fit TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_llm_verdicts (
            job_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            verified_json TEXT
        )
    """)
    conn.commit()


def test_morning_check_healthy(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init_mock_db(conn)

    cfg = load_config()
    now = datetime(2026, 10, 31, 9, 45, tzinfo=timezone.utc)
    out_file = tmp_path / "morning_check.txt"

    # Seed successful runs for specs
    conn.execute(
        "INSERT INTO scheduled_runs (task, trigger, started_at, finished_at, status, duration_seconds) "
        "VALUES ('ingest', 'scheduler', '2026-10-31T06:30:00Z', '2026-10-31T06:31:00Z', 'ok', 60.0)"
    )
    conn.execute(
        "INSERT INTO scheduled_runs (task, trigger, started_at, finished_at, status, duration_seconds) "
        "VALUES ('match', 'scheduler', '2026-10-31T06:45:00Z', '2026-10-31T06:50:00Z', 'ok', 300.0)"
    )
    conn.commit()

    with patch("jobhunt.schedule.morning_check.ollama_answers", return_value=True), \
         patch("jobhunt.schedule.morning_check.get_gpu_info", return_value="NVIDIA RTX 4060 (8000 MB free)"):
        code, text = run_morning_check(conn, cfg, now=now, output_path=out_file)

    assert code == 0
    assert "JOBHUNT MORNING CHECK" in text
    assert "SCHEDULED TASKS STATUS" in text
    assert "AI BUDGET FORECAST" in text
    assert "Ollama Server : REACHABLE" in text
    assert "GPU / VRAM    : NVIDIA RTX 4060" in text
    assert "STATUS: ALL SYSTEMS HEALTHY" in text
    assert out_file.is_file()
    assert out_file.read_text(encoding="utf-8") == text


def test_morning_check_flags_failures(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init_mock_db(conn)

    cfg = load_config()
    now = datetime(2026, 10, 31, 9, 45, tzinfo=timezone.utc)
    out_file = tmp_path / "morning_check.txt"

    # Seed a failed task since yesterday
    conn.execute(
        "INSERT INTO scheduled_runs (task, trigger, started_at, finished_at, status, error) "
        "VALUES ('ingest', 'scheduler', '2026-10-30T19:00:00Z', '2026-10-30T19:00:10Z', 'failed', 'ConnectionResetError: server disconnected')"
    )
    conn.commit()

    with patch("jobhunt.schedule.morning_check.ollama_answers", return_value=True), \
         patch("jobhunt.schedule.morning_check.get_gpu_info", return_value="NVIDIA RTX 4060 (8000 MB free)"):
        code, text = run_morning_check(conn, cfg, now=now, output_path=out_file)

    assert code == 1
    assert "FAILED since yesterday" in text
    assert "STATUS: ATTENTION NEEDED" in text
    assert "Task 'ingest' failed since yesterday" in text


def test_morning_check_unconfigured_gmail_and_uninstalled_tasks_ignored(tmp_path: Path) -> None:
    """Verify that unconfigured optional tasks (like ingest-email without Gmail) and
    uninstalled scheduler tasks are ignored without triggering attention."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init_mock_db(conn)

    cfg = load_config()
    now = datetime(2026, 10, 31, 9, 45, tzinfo=timezone.utc)
    out_file = tmp_path / "morning_check.txt"

    # Mock Gmail as NOT configured
    with patch("jobhunt.doctor.check_gmail_configured", return_value=(False, "credentials not found")), \
         patch("jobhunt.schedule.morning_check.ollama_answers", return_value=True), \
         patch("jobhunt.schedule.morning_check.get_gpu_info", return_value="N/A"):
        code, text = run_morning_check(conn, cfg, now=now, output_path=out_file)

    # Must be marked SKIPPED (Gmail not configured) and not cause attention
    assert "ingest-email   : SKIPPED (Gmail not configured)" in text
    assert "Task 'ingest-email' missed" not in text


def test_morning_check_never_counts_future_or_pre_install_runs_as_missed(tmp_path: Path) -> None:
    """Verify morning-check does not count runs missed when scheduled time has not yet arrived today
    or before task was first run."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init_mock_db(conn)

    cfg = load_config()
    # Check at 05:32 UTC before 06:30 ingest
    now = datetime(2026, 10, 31, 5, 32, tzinfo=timezone.utc)
    out_file = tmp_path / "morning_check.txt"

    # Yesterday evening run succeeded
    conn.execute(
        "INSERT INTO scheduled_runs (task, trigger, started_at, finished_at, status, duration_seconds) "
        "VALUES ('ingest', 'scheduler', '2026-10-30T19:00:00Z', '2026-10-30T19:01:00Z', 'ok', 60.0)"
    )
    conn.commit()

    with patch("jobhunt.doctor.check_gmail_configured", return_value=(False, "not configured")), \
         patch("jobhunt.schedule.morning_check.ollama_answers", return_value=True), \
         patch("jobhunt.schedule.morning_check.get_gpu_info", return_value="N/A"):
        code, text = run_morning_check(conn, cfg, now=now, output_path=out_file)

    # 06:30 today has not arrived yet, so ingest must NOT be marked missed
    assert "ingest         : MISSED" not in text
    assert "ingest         : OK" in text


