"""Optional end-to-end check against the REAL database. Skipped unless run with --real-db-smoke.

    pytest --real-db-smoke tests/test_real_db_smoke.py -v -s

Walks the user journey through the HTTP API (search -> open -> save -> move across the board ->
manual add) on the real data and removes everything it created, even when an assertion fails.
Starting the app applies any pending migration, exactly as `python -m jobhunt serve` would.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jobhunt.api.app import create_app
from jobhunt.config import Config

pytestmark = pytest.mark.realdb


@pytest.fixture()
def real_client(config: Config) -> Iterator[TestClient]:
    app = create_app(config, extra_allowed_hosts=("testserver",))
    with TestClient(app) as client:
        yield client


def _count(config: Config, table: str) -> int:
    connection = sqlite3.connect(config.database_file)
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def test_search_save_and_move_a_real_job(real_client: TestClient, config: Config) -> None:
    applications_before = _count(config, "applications")
    history_before = _count(config, "application_stage_history")
    jobs_before = _count(config, "jobs")
    created_application: int | None = None
    manual_job_id: int | None = None
    timings: dict[str, float] = {}

    def timed(label: str, call):  # noqa: ANN001, ANN202
        started = time.perf_counter()
        response = call()
        timings[label] = (time.perf_counter() - started) * 1000
        return response

    try:
        health = timed("health", lambda: real_client.get("/api/health")).json()
        assert health["jobs_open"] > 1000, "this smoke test expects the populated database"

        found = timed("search 'software engineer' (new grad)", lambda: real_client.get(
            "/api/jobs", params={"q": "software engineer", "level": "new_grad", "page_size": 5})).json()
        assert found["total"] > 0 and found["items"]
        job = found["items"][0]
        assert job["saved"] is False and job["status"] == "open" and job["experience_level"] == "new_grad"

        detail = timed("job detail", lambda: real_client.get(f"/api/jobs/{job['id']}")).json()
        assert detail["title"] == job["title"] and detail["sightings"]

        saved = timed("save", lambda: real_client.post("/api/applications", json={"job_id": job["id"]}))
        assert saved.status_code == 201
        created_application = saved.json()["id"]
        assert real_client.post("/api/applications", json={"job_id": job["id"]}).status_code == 200  # idempotent

        for stage in ("applied", "oa_interview", "offer"):
            moved = timed(f"move to {stage}", lambda s=stage: real_client.patch(
                f"/api/applications/{created_application}", json={"stage": s}))
            assert moved.status_code == 200 and moved.json()["stage"] == stage

        board = timed("board", lambda: real_client.get("/api/applications")).json()
        offer_ids = [card["application"]["id"] for card in next(c for c in board["columns"] if c["stage"] == "offer")["cards"]]
        assert created_application in offer_ids
        assert real_client.get(f"/api/jobs/{job['id']}").json()["stage"] == "offer"

        manual = timed("manual add", lambda: real_client.post("/api/jobs/manual", json={
            "title": "Smoke Test Engineer", "company": "Smoke Test Co",
            "apply_url": f"https://example.com/careers/smoke-{uuid.uuid4().hex}", "fetch_from_ats": True,
        }))
        assert manual.status_code == 200 and manual.json()["created"] is True and manual.json()["ats"]["status"] == "not_applicable"
        manual_job_id = manual.json()["job"]["id"]
        searchable = real_client.get("/api/jobs", params={"q": "Smoke Test Engineer", "hide_senior": "false"}).json()
        assert manual_job_id in [item["id"] for item in searchable["items"]]
    finally:
        # Undo: move back to "saved" (only saved applications can be deleted), delete, then drop the manual job.
        if created_application is not None:
            real_client.patch(f"/api/applications/{created_application}", json={"stage": "saved"})
            real_client.delete(f"/api/applications/{created_application}")
        if manual_job_id is not None:
            connection = sqlite3.connect(config.database_file)
            try:
                connection.execute("DELETE FROM jobs WHERE id = ?", (manual_job_id,))  # cascades sightings; FTS trigger fires
                connection.commit()
            finally:
                connection.close()

    assert _count(config, "applications") == applications_before
    assert _count(config, "application_stage_history") == history_before
    assert _count(config, "jobs") == jobs_before
    print("\nreal-database smoke timings (ms, in-process):")
    for label, millis in timings.items():
        print(f"  {label:<40} {millis:7.1f}")
