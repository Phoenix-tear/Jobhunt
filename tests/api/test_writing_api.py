"""Cover letter / answer endpoints with no model (template and refusal paths) and the exports."""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobhunt.api import routes_writing
from jobhunt.config import Config
from jobhunt.tailoring import facts as fact_module
from jobhunt.tailoring.engine import Bases, Tailor
from jobhunt.tailoring.texdoc import parse
from tests.api.conftest import add_job
from tests.test_tailoring import BASE_TEX, FakeCompiler


@pytest.fixture()
def writing_client(make_client, api_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:  # noqa: ANN001
    config = dataclasses.replace(api_config, project_root=tmp_path, database_path=str(api_config.database_file),
                                 migrations_dir=str(api_config.migrations_directory))
    doc = parse(BASE_TEX)
    per = {"ai": fact_module.extract("ai", doc)}
    merged = fact_module.merge(per)
    bases = Bases({"ai": doc}, per, merged, {f.id: True for f in merged})

    def fake_make_tailor(connection: sqlite3.Connection, cfg: Config, *, use_llm: bool = True):  # noqa: ANN202
        return Tailor(connection, cfg, FakeCompiler(), None, bases=bases), None

    monkeypatch.setattr(routes_writing, "make_tailor", fake_make_tailor)
    original = routes_writing.WritingService.__init__

    def init(self, connection, cfg, tailor, *, compiler=None):  # noqa: ANN001, ANN202
        original(self, connection, cfg, tailor, compiler=FakeCompiler())

    monkeypatch.setattr(routes_writing.WritingService, "__init__", init)
    return make_client(config)


def test_cover_letter_without_a_model_is_an_outline_never_a_letter(writing_client: TestClient, db: sqlite3.Connection) -> None:
    job = add_job(db, title="AI Engineer", description_text="Build LLM agents with RAG and FastAPI.",
                  company_name="Globex Corp")
    reply = writing_client.post(f"/api/jobs/{job}/cover-letter", json={})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["source"] == "outline" and body["kind"] == "cover_letter" and body["cost_usd"] == 0
    assert body["validation"]["ok"] is True and body["cited"] and "Globex Corp" in body["text"]
    again = writing_client.post(f"/api/jobs/{job}/cover-letter", json={}).json()
    assert again["cached"] is True and again["id"] == body["id"]
    listing = writing_client.get(f"/api/jobs/{job}/writing").json()
    assert [item["id"] for item in listing] == [body["id"]]
    txt = writing_client.get(f"/api/writing/{body['id']}/export", params={"format": "txt"})
    assert txt.status_code == 409 and "not a letter" in txt.json()["detail"]


def test_answer_without_a_model_is_a_refusal_and_validates_input(writing_client: TestClient, db: sqlite3.Connection) -> None:
    job = add_job(db, title="AI Engineer", description_text="Build agents.", company_name="Globex Corp")
    reply = writing_client.post(f"/api/jobs/{job}/answer", json={"question": "Why do you want to work here?"})
    assert reply.status_code == 200 and reply.json()["source"] == "refusal"
    assert writing_client.post(f"/api/jobs/{job}/answer", json={"question": "hi"}).status_code == 422
    assert writing_client.post("/api/jobs/999999/cover-letter", json={}).status_code == 404
    assert writing_client.get("/api/writing/999999/export").status_code == 404


def test_knockouts_endpoint_uses_the_answers_file_and_no_model(make_client, db, api_config, tmp_path) -> None:  # noqa: ANN001
    import dataclasses

    from tests.api.conftest import add_job

    isolated = dataclasses.replace(                                       # the template lands in tmp_path, not data/
        api_config, project_root=tmp_path, migrations_dir=str(api_config.migrations_directory))
    client = make_client(isolated)
    job_id = add_job(db, description_text="Eligibility: 7.0 CGPA and above. No active backlogs.")
    path = isolated.resolve_path("data/profile/application_answers.yaml")
    assert not path.exists()
    response = client.post(f"/api/jobs/{job_id}/knockouts",
                           json={"questions": ["Minimum 8.5 CGPA?", "<b>Why us?</b>", "Do you have any backlogs?"]})
    assert response.status_code == 200
    items = response.json()
    assert path.is_file()                                        # template created, nothing invented
    assert {i["kind"] for i in items} == {"cgpa_percentage", "backlogs"}
    assert all(i["risk"] == "needs_confirmation" and i["needs_confirmation"] for i in items)
    detail = client.get(f"/api/jobs/{job_id}").json()
    labels = {s["label"] for s in detail["eligibility_screens"]}
    assert "CGPA 7" in labels and "no backlogs" in labels
    assert client.post("/api/jobs/999999/knockouts", json={"questions": ["x"]}).status_code == 404
    assert client.post(f"/api/jobs/{job_id}/knockouts", json={"questions": []}).status_code == 422
    assert client.post(f"/api/jobs/{job_id}/knockouts", json={"questions": ["q"] * 31}).status_code == 422


def test_save_edit_estimate_and_use_endpoints(writing_client: TestClient, db: sqlite3.Connection) -> None:
    job = add_job(db, title="AI Engineer", description_text="Build LLM agents with RAG and FastAPI.",
                  company_name="Globex Corp")
    estimate = writing_client.get(f"/api/jobs/{job}/writing/estimate").json()
    assert estimate["calls"] == 3 and estimate["max_calls"] == 6 and estimate["interactive_used"] == 0
    assert estimate["interactive_limit"] > 0 and estimate["blocked_reason"] == ""
    assert writing_client.get("/api/jobs/999999/writing/estimate").status_code == 404
    letter = writing_client.post(f"/api/jobs/{job}/cover-letter", json={}).json()
    assert letter["pipeline"] == "staged" and letter["stages"] == []
    mine = writing_client.post(f"/api/writing/{letter['id']}/edit",
                               json={"text": letter["text"] + " I would be glad to talk."}).json()
    assert mine["version"] == 2 and mine["source"] == "user_edit" and mine["used_for_export"] is True
    assert mine["parent_id"] == letter["id"] and mine["cost_usd"] == 0
    listing = writing_client.get(f"/api/jobs/{job}/writing").json()
    assert [item["version"] for item in listing] == [2, 1]
    back = writing_client.post(f"/api/writing/{letter['id']}/use").json()
    assert back["used_for_export"] is True
    assert writing_client.post(f"/api/writing/{letter['id']}/edit", json={"text": "short"}).status_code == 422
    assert writing_client.post("/api/writing/999999/edit", json={"text": "x" * 40}).status_code == 404
    assert writing_client.post("/api/writing/999999/use").status_code == 404
