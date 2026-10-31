"""Tailoring endpoints: create (mocked compiler, no model), list, download, approve, attach to an application."""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobhunt.api import routes_tailor
from jobhunt.config import Config
from jobhunt.tailoring import facts as fact_module
from jobhunt.tailoring.engine import Bases, Tailor
from jobhunt.tailoring.texdoc import parse
from tests.api.conftest import add_job
from tests.test_tailoring import BASE_TEX, FakeCompiler


@pytest.fixture()
def tailor_client(make_client, api_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:  # noqa: ANN001
    config = dataclasses.replace(api_config, project_root=tmp_path, database_path=str(api_config.database_file),
                                 migrations_dir=str(api_config.migrations_directory))
    doc = parse(BASE_TEX)
    per = {"ai": fact_module.extract("ai", doc)}
    merged = fact_module.merge(per)
    bases = Bases({"ai": doc}, per, merged, {f.id: True for f in merged})

    def fake_make_tailor(connection: sqlite3.Connection, cfg: Config, *, use_llm: bool = True):  # noqa: ANN202
        return Tailor(connection, cfg, FakeCompiler(), None, bases=bases), None

    monkeypatch.setattr(routes_tailor, "make_tailor", fake_make_tailor)
    return make_client(config)


def test_tailor_list_download_and_approve(tailor_client: TestClient, db: sqlite3.Connection) -> None:
    job = add_job(db, title="AI Engineer", description_text="Build RAG assistants with FastAPI. Requirements: Python.",
                  company_name="Globex Corp")
    created = tailor_client.post(f"/api/jobs/{job}/tailor", json={"use_llm": False, "base": "ai"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "ok" and body["mode"] == "deterministic" and body["pages"] == 1
    assert body["has_tex"] is True and body["has_pdf"] is True
    assert body["download_name"] == "Resume_Globex_Corp"
    assert body["validation"]["ok"] is True

    listing = tailor_client.get(f"/api/jobs/{job}/tailored").json()
    assert [item["id"] for item in listing] == [body["id"]]
    assert tailor_client.get(f"/api/tailored/{body['id']}/tex").text.startswith("\\documentclass")
    assert tailor_client.get(f"/api/tailored/{body['id']}/pdf").headers["content-type"] == "application/pdf"

    approved = tailor_client.post(f"/api/tailored/{body['id']}/approve", json={"approved": True}).json()
    assert approved["approved"] is True
    # a second tailoring (forced) and approving it un-approves the first: one approved version per job
    second = tailor_client.post(f"/api/jobs/{job}/tailor", json={"use_llm": False, "force": True}).json()
    tailor_client.post(f"/api/tailored/{second['id']}/approve", json={"approved": True})
    states = {item["id"]: item["approved"] for item in tailor_client.get(f"/api/jobs/{job}/tailored").json()}
    assert states == {body["id"]: False, second["id"]: True}


def test_unknown_job_and_unknown_version(tailor_client: TestClient) -> None:
    assert tailor_client.post("/api/jobs/9999/tailor", json={}).status_code == 404
    assert tailor_client.get("/api/tailored/9999/pdf").status_code == 404
    assert tailor_client.post("/api/tailored/9999/approve", json={}).status_code == 404
    assert tailor_client.post("/api/jobs/1/tailor", json={"base": "nope"}).status_code == 422


def test_files_outside_the_tailored_folder_are_never_served(tailor_client: TestClient, db: sqlite3.Connection,
                                                            tmp_path: Path) -> None:
    job = add_job(db, title="AI Engineer")
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    db.execute(
        "INSERT INTO tailored_resumes (job_id, base_profile, mode, cache_key, tex_path, status, created_at) "
        "VALUES (?, 'ai', 'select', 'k', ?, 'ok', 'x')", (job, str(secret)))
    row_id = db.execute("SELECT id FROM tailored_resumes").fetchone()[0]
    assert tailor_client.get(f"/api/tailored/{row_id}/tex").status_code == 404


def test_attach_an_approved_version_to_an_application(tailor_client: TestClient, db: sqlite3.Connection) -> None:
    job = add_job(db, title="AI Engineer", description_text="Build RAG. Requirements: Python.")
    other = add_job(db, title="Other Role", description_text="Build things. Requirements: Java.")
    tailored = tailor_client.post(f"/api/jobs/{job}/tailor", json={"use_llm": False}).json()
    foreign = tailor_client.post(f"/api/jobs/{other}/tailor", json={"use_llm": False}).json()
    application = tailor_client.post("/api/applications", json={"job_id": job}).json()
    attached = tailor_client.patch(f"/api/applications/{application['id']}",
                                   json={"stage": "applied", "tailored_resume_id": tailored["id"]})
    assert attached.status_code == 200 and attached.json()["tailored_resume_id"] == tailored["id"]
    wrong = tailor_client.patch(f"/api/applications/{application['id']}", json={"tailored_resume_id": foreign["id"]})
    assert wrong.status_code == 422
    board = tailor_client.get("/api/applications").json()
    card = next(c for col in board["columns"] for c in col["cards"])
    assert card["application"]["tailored_resume_id"] == tailored["id"]


def test_status_reports_engine_and_spend(tailor_client: TestClient) -> None:
    body = tailor_client.get("/api/tailoring/status").json()
    assert {"engine", "runs", "usd_today", "usd_month", "validator_failure_rate"} <= set(body)
