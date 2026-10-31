"""Applications: saving, stage changes, history, deletion rules and the board."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import add_job

STAGES = ["saved", "applied", "oa_interview", "offer", "rejected"]


def history(db: sqlite3.Connection, application_id: int) -> list[tuple[str | None, str]]:
    rows = db.execute(
        "SELECT from_stage, to_stage FROM application_stage_history WHERE application_id = ? ORDER BY id",
        (application_id,),
    ).fetchall()
    return [(row["from_stage"], row["to_stage"]) for row in rows]


def test_saving_creates_an_application_and_a_history_row(client: TestClient, db: sqlite3.Connection) -> None:
    job_id = add_job(db, title="Dream Job")
    response = client.post("/api/applications", json={"job_id": job_id})
    assert response.status_code == 201
    body = response.json()
    assert (body["job_id"], body["stage"], body["applied_at"]) == (job_id, "saved", None)
    assert history(db, body["id"]) == [(None, "saved")]


def test_saving_the_same_job_twice_is_idempotent(client: TestClient, db: sqlite3.Connection) -> None:
    job_id = add_job(db)
    first = client.post("/api/applications", json={"job_id": job_id})
    second = client.post("/api/applications", json={"job_id": job_id})
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"]
    assert db.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM application_stage_history").fetchone()[0] == 1


def test_saving_an_unknown_job_is_404(client: TestClient) -> None:
    assert client.post("/api/applications", json={"job_id": 424242}).status_code == 404
    assert client.post("/api/applications", json={"job_id": 0}).status_code == 422
    assert client.post("/api/applications", json={}).status_code == 422
    assert client.post("/api/applications", json={"job_id": 1, "unexpected": True}).status_code == 422


def test_job_list_and_detail_reflect_the_application(client: TestClient, db: sqlite3.Connection) -> None:
    job_id = add_job(db, title="Tracked")
    app_id = client.post("/api/applications", json={"job_id": job_id}).json()["id"]
    item = client.get("/api/jobs").json()["items"][0]
    assert (item["saved"], item["stage"], item["application_id"]) == (True, "saved", app_id)
    assert client.get(f"/api/jobs/{job_id}").json()["application"]["id"] == app_id


def test_stage_change_writes_history_and_stamps_applied_at(client: TestClient, db: sqlite3.Connection) -> None:
    job_id = add_job(db)
    app_id = client.post("/api/applications", json={"job_id": job_id}).json()["id"]
    body = client.patch(f"/api/applications/{app_id}", json={"stage": "applied"}).json()
    assert body["stage"] == "applied" and body["applied_at"] is not None and body["applied_at"].endswith("Z")
    client.patch(f"/api/applications/{app_id}", json={"stage": "oa_interview"})
    client.patch(f"/api/applications/{app_id}", json={"stage": "offer"})
    assert history(db, app_id) == [(None, "saved"), ("saved", "applied"), ("applied", "oa_interview"), ("oa_interview", "offer")]


def test_applied_at_is_not_overwritten_and_is_editable(client: TestClient, db: sqlite3.Connection) -> None:
    app_id = client.post("/api/applications", json={"job_id": add_job(db)}).json()["id"]
    stamped = client.patch(f"/api/applications/{app_id}", json={"stage": "applied"}).json()["applied_at"]
    client.patch(f"/api/applications/{app_id}", json={"stage": "saved"})
    again = client.patch(f"/api/applications/{app_id}", json={"stage": "applied"}).json()
    assert again["applied_at"] == stamped                                           # kept, not re-stamped
    edited = client.patch(f"/api/applications/{app_id}", json={"applied_at": "2026-10-01"}).json()
    assert edited["applied_at"] == "2026-10-01"
    cleared = client.patch(f"/api/applications/{app_id}", json={"applied_at": None}).json()
    assert cleared["applied_at"] is None


def test_explicit_applied_at_in_the_same_request_wins(client: TestClient, db: sqlite3.Connection) -> None:
    app_id = client.post("/api/applications", json={"job_id": add_job(db)}).json()["id"]
    body = client.patch(f"/api/applications/{app_id}", json={"stage": "applied", "applied_at": "2026-09-30"}).json()
    assert body["applied_at"] == "2026-09-30"


def test_same_stage_is_not_a_change(client: TestClient, db: sqlite3.Connection) -> None:
    app_id = client.post("/api/applications", json={"job_id": add_job(db)}).json()["id"]
    client.patch(f"/api/applications/{app_id}", json={"stage": "saved"})
    assert history(db, app_id) == [(None, "saved")]


def test_notes_and_follow_up_can_be_set_and_cleared(client: TestClient, db: sqlite3.Connection) -> None:
    app_id = client.post("/api/applications", json={"job_id": add_job(db)}).json()["id"]
    body = client.patch(f"/api/applications/{app_id}", json={"notes": "ask about <script>", "follow_up_at": "2026-10-20"}).json()
    assert body["notes"] == "ask about <script>" and body["follow_up_at"] == "2026-10-20" and body["stage"] == "saved"
    cleared = client.patch(f"/api/applications/{app_id}", json={"notes": None, "follow_up_at": None}).json()
    assert cleared["notes"] is None and cleared["follow_up_at"] is None
    assert history(db, app_id) == [(None, "saved")]                                 # no stage change, no history


@pytest.mark.parametrize("stage", ["archived", "APPLIED", "", "applied ", 5, None])
def test_invalid_stage_is_422(client: TestClient, db: sqlite3.Connection, stage: object) -> None:
    app_id = client.post("/api/applications", json={"job_id": add_job(db)}).json()["id"]
    assert client.patch(f"/api/applications/{app_id}", json={"stage": stage}).status_code == 422
    assert db.execute("SELECT stage FROM applications WHERE id = ?", (app_id,)).fetchone()[0] == "saved"


@pytest.mark.parametrize("body", [{"follow_up_at": "next tuesday"}, {"applied_at": "2026-13-45"}, {"notes": "x" * 10_001}, {"nope": 1}])
def test_invalid_patch_bodies_are_422(client: TestClient, db: sqlite3.Connection, body: dict) -> None:
    app_id = client.post("/api/applications", json={"job_id": add_job(db)}).json()["id"]
    assert client.patch(f"/api/applications/{app_id}", json=body).status_code == 422


def test_unknown_application_is_404(client: TestClient) -> None:
    assert client.patch("/api/applications/9999", json={"stage": "applied"}).status_code == 404
    assert client.delete("/api/applications/9999").status_code == 404


def test_delete_only_while_saved(client: TestClient, db: sqlite3.Connection) -> None:
    saved_id = client.post("/api/applications", json={"job_id": add_job(db)}).json()["id"]
    applied_id = client.post("/api/applications", json={"job_id": add_job(db), "stage": "applied"}).json()["id"]
    assert client.delete(f"/api/applications/{applied_id}").status_code == 409
    assert db.execute("SELECT COUNT(*) FROM applications WHERE id = ?", (applied_id,)).fetchone()[0] == 1
    assert client.delete(f"/api/applications/{saved_id}").status_code == 204
    assert db.execute("SELECT COUNT(*) FROM applications WHERE id = ?", (saved_id,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM application_stage_history WHERE application_id = ?", (saved_id,)).fetchone()[0] == 0


def test_a_job_can_be_saved_again_after_deleting(client: TestClient, db: sqlite3.Connection) -> None:
    job_id = add_job(db)
    first = client.post("/api/applications", json={"job_id": job_id}).json()["id"]
    client.delete(f"/api/applications/{first}")
    assert client.post("/api/applications", json={"job_id": job_id}).status_code == 201


def test_board_groups_by_stage_with_job_info(client: TestClient, db: sqlite3.Connection) -> None:
    a = add_job(db, title="A", sources=[("greenhouse", "https://boards.greenhouse.io/acme/jobs/1")])
    b = add_job(db, title="B")
    c = add_job(db, title="C", status="closed")
    for job_id, stage in ((a, "saved"), (b, "applied"), (c, "applied")):
        client.post("/api/applications", json={"job_id": job_id, "stage": stage})
    board = client.get("/api/applications").json()
    assert [column["stage"] for column in board["columns"]] == STAGES and board["total"] == 3
    counts = {column["stage"]: column["count"] for column in board["columns"]}
    assert counts == {"saved": 1, "applied": 2, "oa_interview": 0, "offer": 0, "rejected": 0}
    saved_card = board["columns"][0]["cards"][0]
    assert saved_card["title"] == "A" and saved_card["apply_url"] == "https://boards.greenhouse.io/acme/jobs/1"
    closed_card = next(card for card in board["columns"][1]["cards"] if card["title"] == "C")
    assert closed_card["job_status"] == "closed"


def test_empty_board_has_all_five_columns(client: TestClient) -> None:
    board = client.get("/api/applications").json()
    assert board["total"] == 0 and [column["stage"] for column in board["columns"]] == STAGES
