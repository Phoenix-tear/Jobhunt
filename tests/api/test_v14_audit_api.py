"""Tests for Triage Audit API endpoints (Brief v14 Section 4)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

from jobhunt.config import Config


def seed_audit_items(connection: sqlite3.Connection) -> None:
    """Seed test audit items directly."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS triage_audit_items (
            job_id INTEGER PRIMARY KEY,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            category TEXT NOT NULL,
            snippet TEXT NOT NULL,
            gemini_verdict TEXT NOT NULL,
            gemini_quote TEXT NOT NULL,
            local_verdict TEXT NOT NULL,
            local_quote TEXT NOT NULL,
            deterministic_hint TEXT NOT NULL,
            row_order INTEGER NOT NULL
        )
    """)
    items = [
        (101, "Google", "Staff Software Engineer", "greenhouse", "too_senior",
         "Requires 5+ years of distributed systems experience in production.",
         "senior (years=5)", "5+ years", "mid (years=1)", "distributed systems",
         "Snippet contains '5+ years': supports years >= 2 (senior/lead)", 0),
        (102, "Meta", "Software Engineer - Remote US", "lever", "not_india_eligible",
         "Must reside in the US. No international relocation offered.",
         "india=no", "Must reside in the US", "india=yes", "remote",
         "Snippet contains 'Must reside in the US': explicit US restriction (not India eligible)", 1),
        (103, "StartupX", "Junior Developer", "workable", "control_clear_fit",
         "Fresh graduate or 0-1 years experience in Python.",
         "mid (years=0)", "0-1 years", "mid (years=0)", "0-1 years",
         "Title contains 'junior': entry/junior level (not senior)", 2),
    ]
    connection.executemany("""
        INSERT INTO triage_audit_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, items)
    connection.commit()


def test_audit_get_state(client: TestClient, db: sqlite3.Connection) -> None:
    seed_audit_items(db)
    res = client.get("/api/triage/audit")
    assert res.status_code == 200
    data = res.json()
    assert data["total_items"] == 3
    assert data["answered_count"] == 0
    assert data["current_index"] == 0
    assert len(data["items"]) == 3
    assert data["items"][0]["company"] == "Google"
    assert "5+ years" in data["items"][0]["deterministic_hint"]


def test_audit_label_csrf_required(api_config: Config, db: sqlite3.Connection, tmp_path: Path) -> None:
    seed_audit_items(db)
    from jobhunt.api.app import create_app
    app = create_app(api_config, extra_allowed_hosts=("testserver",), frontend_dir=tmp_path / "no-dist")
    # Client without CSRF token header
    raw_client = TestClient(app)
    res = raw_client.post("/api/triage/audit/label", json={"job_id": 101, "my_verdict": "gemini_right"})
    assert res.status_code == 403
    assert "invalid or missing" in res.json()["detail"]


def test_audit_label_validation(client: TestClient, db: sqlite3.Connection) -> None:
    seed_audit_items(db)
    res = client.post("/api/triage/audit/label", json={"job_id": 101, "my_verdict": "invalid_verdict"})
    assert res.status_code == 400
    assert "Invalid verdict" in res.json()["detail"]


def test_audit_label_recording_and_undo(client: TestClient, db: sqlite3.Connection) -> None:
    seed_audit_items(db)

    # 1. Label item 101 as gemini_right
    res = client.post("/api/triage/audit/label", json={
        "job_id": 101,
        "my_verdict": "gemini_right",
        "my_note": "5+ years clearly stated",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["answered_count"] == 1
    assert data["gemini_correct"] == 1
    assert data["local_correct"] == 0
    assert data["gemini_accuracy"] == 1.0
    assert data["current_index"] == 1
    assert data["items"][0]["my_verdict"] == "gemini_right"
    assert data["items"][0]["my_note"] == "5+ years clearly stated"

    # Verify stored in database with source='audit'
    row = db.execute("SELECT * FROM job_audit_labels WHERE job_id = 101").fetchone()
    assert row is not None
    assert row["source"] == "audit"
    assert row["my_verdict"] == "gemini_right"

    # 2. Label item 102 as local_right
    res2 = client.post("/api/triage/audit/label", json={
        "job_id": 102,
        "my_verdict": "local_right",
    })
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["answered_count"] == 2
    assert data2["gemini_correct"] == 1
    assert data2["local_correct"] == 1
    assert data2["current_index"] == 2
    assert data2["gemini_accuracy"] == 0.5
    assert data2["local_accuracy"] == 0.5

    # 3. Undo item 102
    res3 = client.post("/api/triage/audit/undo", json={"job_id": 102})
    assert res3.status_code == 200
    data3 = res3.json()
    assert data3["answered_count"] == 1
    assert data3["gemini_correct"] == 1
    assert data3["local_correct"] == 0
    assert data3["current_index"] == 1

    # Check database row deleted
    row_undone = db.execute("SELECT * FROM job_audit_labels WHERE job_id = 102").fetchone()
    assert row_undone is None

