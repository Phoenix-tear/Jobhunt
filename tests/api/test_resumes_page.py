"""Resumes page: migration + backfill, numbering and versions, change summaries, the explicit-click contract."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobhunt.api import routes_tailor
from jobhunt.config import Config
from jobhunt.db import apply_migrations, connect
from jobhunt.tailoring import changes, resumes
from jobhunt.tailoring.engine import Tailor
from jobhunt.tailoring.facts import fact_id
from jobhunt.tailoring.texdoc import Entry, parse
from tests.api.conftest import add_job
from tests.api.test_tailor_api import tailor_client  # noqa: F401  (fixture)
from tests.test_tailoring import BASE_TEX

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


# --- migration and backfill on a populated database ------------------------------------------------------------------


def populated_db_before_014(tmp_path: Path) -> sqlite3.Connection:
    old = tmp_path / "migrations_to_013"
    old.mkdir()
    for path in MIGRATIONS.glob("*.sql"):
        if int(path.name[:3]) <= 13:
            shutil.copy(path, old / path.name)
    connection = connect(tmp_path / "old.sqlite3")
    apply_migrations(connection, old)
    for job_id, company in ((1, "Acme"), (2, "Globex")):
        connection.execute(
            """INSERT INTO jobs (id, fingerprint, title, company_name, first_seen_at, last_seen_at)
               VALUES (?, ?, 'ML Engineer', ?, '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')""",
            (job_id, f"fp{job_id}", company))
    plan = json.dumps({"recipe": "ml_data", "summary_variant": "S2", "fill": 0.95, "keywords": ["python", "rag"],
                       "matched": [{"keyword": "python", "fact": "T-1", "fact_text": "x"}], "missing": ["rag"],
                       "projects_shown": ["AlphaAgent"]})
    rows = [  # (id, job, status, mode, model, approved, pdf)
        (10, 1, "ok", "advised", "gemini-3.8-flash", 0, "D:/t/acme/resume.pdf"),
        (11, 2, "fallback", "deterministic", "gemini-3.8-flash", 0, "D:/t/globex/resume.pdf"),
        (12, 1, "ok", "deterministic", "", 1, "D:/t/acme/resume.pdf"),       # same file as row 10: it was overwritten
        (13, 1, "ok", "deterministic", "", 0, "D:/t/acme/resume.pdf"),
    ]
    for row_id, job, status, mode, model, approved, pdf in rows:
        connection.execute(
            """INSERT INTO tailored_resumes (id, job_id, base_profile, mode, cache_key, plan_json, pdf_path, status, model,
                   created_at, pages, approved, validation_json) VALUES (?, ?, 'ai', ?, ?, ?, ?, ?, ?, ?, 1, ?, '{"ok": true}')""",
            (row_id, job, mode, f"k{row_id}", plan, pdf, status, model, f"2026-10-0{row_id - 8}T00:00:00Z", approved))
    connection.execute("INSERT INTO applications (job_id, stage, created_at, updated_at, tailored_resume_id, applied_at) "
                       "VALUES (2, 'applied', 'x', 'x', 11, '2026-10-05T00:00:00Z')")
    return connection


def test_migration_backfills_numbers_versions_lifecycle_and_notes(tmp_path: Path) -> None:
    connection = populated_db_before_014(tmp_path)
    assert apply_migrations(connection, MIGRATIONS)[0] == 14
    rows = {r["id"]: r for r in connection.execute("SELECT * FROM tailored_resumes")}
    assert [rows[i]["resume_number"] for i in (10, 11, 12, 13)] == [1, 2, 3, 4]               # chronological
    assert [rows[i]["version"] for i in (10, 11, 12, 13)] == [1, 1, 2, 3]                       # per job
    assert [rows[i]["parent_resume_id"] for i in (10, 11, 12, 13)] == [None, None, 10, 12]
    assert rows[10]["lifecycle"] == "superseded" and rows[12]["lifecycle"] == "approved" and rows[13]["lifecycle"] == "draft"
    assert rows[11]["lifecycle"] == "sent" and rows[11]["sent_at"] == "2026-10-05T00:00:00Z"    # attached in the tracker
    assert rows[10]["advice_source"] == "gemini" and rows[11]["advice_source"] == "deterministic"
    assert rows[10]["recipe"] == "ml_data" and rows[10]["summary_variant"] == "S2" and rows[10]["fill_ratio"] == 0.95
    assert json.loads(rows[10]["keywords_matched_json"]) == ["python"] and json.loads(rows[10]["keywords_missing_json"]) == ["rag"]
    assert rows[10]["validator_status"] == "ok" and rows[10]["base"] == "ai"
    assert rows[10]["file_note"] and rows[12]["file_note"] and rows[13]["file_note"] is None    # only the newest file is real
    assert rows[10]["pdf_path"] == "D:/t/acme/resume.pdf"                                       # no path was touched
    assert apply_migrations(connection, MIGRATIONS) == []                                       # idempotent


def test_backfill_marks_overwritten_runs_as_not_fully_recorded(tmp_path: Path, config: Config) -> None:
    connection = populated_db_before_014(tmp_path)
    apply_migrations(connection, MIGRATIONS)
    cfg = dataclasses.replace(config, project_root=tmp_path)
    assert resumes.backfill_change_summaries(connection, cfg) == 4
    old = json.loads(connection.execute("SELECT change_summary_json FROM tailored_resumes WHERE id = 10").fetchone()[0])
    assert old["recorded"] is False and old["partial"] is True and "overwritten" in old["reason"]
    assert old["snapshot"]["summary_variant"] == "S2"                                           # what the plan still says
    assert resumes.backfill_change_summaries(connection, cfg) == 0                              # idempotent


# --- change summary cases ---------------------------------------------------------------------------------------------


def summary_for(new_doc, base_doc=None):  # noqa: ANN001, ANN201
    base = base_doc or parse(BASE_TEX)
    return changes.compute_change_summary(base, new_doc, {"summary_variant": "base", "keywords": ["python"]}, {},
                                          base_name="ai", pages=1)


def test_identical_to_base() -> None:
    summary = summary_for(parse(BASE_TEX))
    assert summary["identical_to_base"] is True and summary["bullets"] == [] and not summary["projects"]["removed"]
    assert "Identical to the base resume" in changes.render_text(summary)[0]


def test_dropped_bullet_and_moved_to_top() -> None:
    base = parse(BASE_TEX)
    project = base.projects[0]
    reordered = dataclasses.replace(project, bullets=(project.bullets[2], project.bullets[0]))     # drop #2, move #3 first
    summary = summary_for(base.with_(projects=(reordered, base.projects[1])))
    entry = summary["bullets"][0]
    assert entry["entry"] == "FinAgent" and entry["kept"] == 2 and len(entry["dropped"]) == 1
    assert entry["moved_to_top"]["words"].startswith("Added a 60-case pytest")
    assert entry["dropped"][0]["fact"].startswith("T-") and len(entry["dropped"][0]["words"].split()) <= 8
    assert any("moved to top" in line for line in changes.render_text(summary))


def test_project_added_from_the_pool_removed_and_reordered() -> None:
    base = parse(BASE_TEX)
    pool = Entry("projectitem", ("BetaPortal", "Next.js", "2025"), ("Designed the MongoDB schema.",))
    summary = summary_for(base.with_(projects=(base.projects[1], pool)))
    projects = summary["projects"]
    assert projects["removed"] == ["FinAgent"] and projects["added_from_pool"] == ["BetaPortal"]
    added = [b for b in summary["bullets"] if b["entry"] == "BetaPortal"][0]
    assert added["status"] == "added_entry" and added["added"][0]["words"] == "Designed the MongoDB schema."
    swapped = summary_for(base.with_(projects=(base.projects[1], base.projects[0])))
    assert swapped["projects"]["reordered"] is True and not swapped["projects"]["removed"]


def test_skills_moved_to_the_front_and_summary_variant() -> None:
    base = parse(BASE_TEX)
    line = base.skills[1]
    moved = dataclasses.replace(line, items=tuple(reversed(line.items)))
    new = base.with_(skills=(base.skills[0], moved), summary="Approved variant text for the role.")
    summary = changes.compute_change_summary(base, new, {"summary_variant": "S3"}, {}, base_name="ai", pages=1,
                                             closing_clauses=("for the role",))
    assert summary["summary"]["changed"] and summary["summary"]["variant"] == "S3"
    assert summary["summary"]["closing_clause"] == "for the role"
    skills = summary["skills"]["lines"][0]
    assert skills["group"] == "Backend" and skills["moved_to_front"] == ["pytest", "GitHub Actions"]


def test_compare_versions_reads_like_a_sentence() -> None:
    base = parse(BASE_TEX)
    pool = Entry("projectitem", ("BetaPortal", "Next.js", "2025"), ("Designed the MongoDB schema.", "Added tests."))
    first = summary_for(base)
    second = changes.compute_change_summary(base, base.with_(projects=(base.projects[1], pool)),
                                            {"summary_variant": "S2"}, {}, base_name="ai", pages=1)
    text = changes.compare_versions(first, second, previous_version=1, current_version=2)
    assert text.startswith("v2 vs v1: swapped FinAgent for BetaPortal")
    assert "+2 bullets" in text and "summary base -> S2" in text
    assert "no comparison recorded" in changes.compare_versions(None, second, previous_version=1, current_version=2)
    assert fact_id("bullet", "x").startswith("T-")


# --- numbering, versions and files through the real engine (mock compiler, no model) -----------------------------------


def make_job(db: sqlite3.Connection) -> int:
    return add_job(db, title="AI Engineer", company_name="Globex Corp",
                   description_text="Build RAG assistants with FastAPI. Requirements: Python.")


def sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_new_version_gets_the_next_number_and_leaves_the_old_pdf_untouched(
        tailor_client: TestClient, db: sqlite3.Connection) -> None:  # noqa: F811
    job = make_job(db)
    first = tailor_client.post(f"/api/jobs/{job}/tailor", json={"use_llm": False, "base": "ai"}).json()
    row1 = db.execute("SELECT * FROM tailored_resumes WHERE id = ?", (first["id"],)).fetchone()
    digest, path1 = sha(row1["pdf_path"]), row1["pdf_path"]
    assert row1["resume_number"] == 1 and row1["version"] == 1 and row1["lifecycle"] == "draft"
    assert Path(path1).parent.name == "v1"

    again = tailor_client.post(f"/api/jobs/{job}/tailor", json={"use_llm": False, "base": "ai"}).json()
    assert again["id"] == first["id"]                                            # same hash: shown, nothing new created
    assert db.execute("SELECT COUNT(*) FROM tailored_resumes").fetchone()[0] == 1

    second = tailor_client.post(f"/api/jobs/{job}/tailor", json={"use_llm": False, "base": "ai", "force": True}).json()
    row2 = db.execute("SELECT * FROM tailored_resumes WHERE id = ?", (second["id"],)).fetchone()
    assert row2["resume_number"] == 2 and row2["version"] == 2 and row2["parent_resume_id"] == first["id"]
    assert Path(row2["pdf_path"]).parent.name == "v2" and row2["pdf_path"] != path1
    assert Path(path1).is_file() and sha(path1) == digest                        # the old PDF is still there, unchanged
    lifecycles = {r["id"]: r["lifecycle"] for r in db.execute("SELECT id, lifecycle FROM tailored_resumes")}
    assert lifecycles == {first["id"]: "superseded", second["id"]: "draft"}
    assert row2["advice_source"] == "deterministic" and row2["profile_hash"] and row2["validator_status"] == "ok"

    detail = tailor_client.get(f"/api/resumes/{second['id']}").json()
    assert detail["code"] == "R-0002" and detail["diff_vs_previous"].startswith("v2 vs v1:")
    assert detail["change_summary"]["recorded"] is True and detail["change_text"]
    assert "deterministic path (no model; reason: user chose)" in detail["how_made"]
    assert "never written by the model" in detail["how_made"]


# --- the explicit-click contract --------------------------------------------------------------------------------------


@pytest.fixture()
def tailored_job(tailor_client: TestClient, db: sqlite3.Connection) -> tuple[int, int]:  # noqa: F811
    job = make_job(db)
    created = tailor_client.post(f"/api/jobs/{job}/tailor", json={"use_llm": False, "base": "ai"}).json()
    return job, created["id"]


def test_reading_never_tailors_or_calls_a_model(tailor_client: TestClient, tailored_job: tuple[int, int],
                                                monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    job, resume = tailored_job

    def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("tailoring or a model client was used by a read-only request")

    monkeypatch.setattr(routes_tailor, "make_tailor", forbidden)
    monkeypatch.setattr(Tailor, "tailor", forbidden)
    monkeypatch.setattr("jobhunt.tailoring.service.make_tailor", forbidden)
    monkeypatch.setattr("jobhunt.matching.gemini.GeminiClient.generate_json", forbidden)
    for url in (f"/api/jobs/{job}", "/api/jobs", f"/api/jobs/{job}/tailored", "/api/resumes", f"/api/resumes/{resume}",
                f"/api/resumes/{resume}/preview", f"/api/tailored/{resume}/pdf", f"/api/jobs/{job}/tailor-estimate",
                "/api/today", "/api/resumes/export.csv", "/api/tailoring/status", "/api/applications"):
        assert tailor_client.get(url).status_code == 200, url


def test_only_a_post_can_tailor(tailor_client: TestClient, tailored_job: tuple[int, int]) -> None:
    job, _ = tailored_job
    for method in ("get", "put", "patch", "delete"):
        assert getattr(tailor_client, method)(f"/api/jobs/{job}/tailor").status_code == 405


def test_preview_is_inline_and_only_download_is_an_attachment(tailor_client: TestClient,
                                                              tailored_job: tuple[int, int]) -> None:
    _, resume = tailored_job
    for url in (f"/api/resumes/{resume}/preview", f"/api/tailored/{resume}/pdf"):
        response = tailor_client.get(url)
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"].startswith("inline"), url
    for url in (f"/api/resumes/{resume}/download", f"/api/tailored/{resume}/download"):
        disposition = tailor_client.get(url).headers["content-disposition"]
        assert disposition.startswith("attachment") and "Resume_R0001_Globex_Corp_AI_Engineer.pdf" in disposition


def test_mark_sent_attaches_to_the_tracker_and_creates_a_saved_entry(
        tailor_client: TestClient, db: sqlite3.Connection, tailored_job: tuple[int, int]) -> None:  # noqa: F811
    job, resume = tailored_job
    assert db.execute("SELECT COUNT(*) FROM applications WHERE job_id = ?", (job,)).fetchone()[0] == 0
    sent = tailor_client.post(f"/api/resumes/{resume}/sent").json()
    application = db.execute("SELECT * FROM applications WHERE job_id = ?", (job,)).fetchone()
    assert application["stage"] == "saved" and application["tailored_resume_id"] == resume
    assert sent["application_id"] == application["id"] and sent["sent_at"]
    board = tailor_client.get("/api/applications").json()
    cards = [card for column in board["columns"] if column["stage"] == "saved" for card in column["cards"]]
    assert cards[0]["application"]["resume_number"] == 1                         # the tracker shows R-0001
    listed = tailor_client.get("/api/resumes").json()["items"][0]
    assert listed["lifecycle"] == "sent" and listed["application_stage"] == "saved" and listed["sent_at"]
    # an existing tracker entry is reused, not duplicated
    tailor_client.post(f"/api/resumes/{resume}/sent")
    assert db.execute("SELECT COUNT(*) FROM applications WHERE job_id = ?", (job,)).fetchone()[0] == 1
    assert tailor_client.post("/api/resumes/9999/sent").status_code == 404


def test_approve_keeps_lifecycle_in_step_and_archive_hides(tailor_client: TestClient,
                                                           tailored_job: tuple[int, int]) -> None:
    job, resume = tailored_job
    tailor_client.post(f"/api/tailored/{resume}/approve", json={"approved": True})
    assert tailor_client.get(f"/api/resumes/{resume}").json()["lifecycle"] == "approved"
    tailor_client.post(f"/api/tailored/{resume}/approve", json={"approved": False})
    assert tailor_client.get(f"/api/resumes/{resume}").json()["lifecycle"] == "draft"
    assert tailor_client.post(f"/api/resumes/{resume}/archive", json={"archived": True}).status_code == 200
    assert tailor_client.get("/api/resumes").json()["items"] == []
    assert len(tailor_client.get("/api/resumes?include_archived=true").json()["items"]) == 1
    assert tailor_client.get(f"/api/tailored/{resume}/pdf").status_code == 200       # the file is still on disk


def test_list_filters_and_estimate(tailor_client: TestClient, tailored_job: tuple[int, int],
                                   db: sqlite3.Connection) -> None:  # noqa: F811
    job, resume = tailored_job
    assert len(tailor_client.get("/api/resumes?company=globex").json()["items"]) == 1
    assert tailor_client.get("/api/resumes?company=nobody").json()["items"] == []
    assert tailor_client.get("/api/resumes?status=sent").json()["items"] == []
    assert tailor_client.get("/api/resumes?base=fullstack").json()["items"] == []
    assert tailor_client.get("/api/resumes?since=2999-01-01").json()["items"] == []
    estimate = tailor_client.get(f"/api/jobs/{job}/tailor-estimate").json()
    assert estimate["existing_versions"] == 1 and estimate["interactive_call_cap"] == 40 and estimate["blocked"] is False
    for _ in range(40):                                                   # use up the interactive limit
        db.execute("INSERT INTO llm_usage (created_at, day, model, purpose, calls, items, input_tokens, output_tokens, "
                   "estimated_usd) VALUES ('x', date('now'), 'gemini-3.8-flash', 'tailor', 1, 1, 1, 1, 0)")
    blocked = tailor_client.get(f"/api/jobs/{job}/tailor-estimate").json()
    assert blocked["blocked"] is True and "interactive limit reached today" in blocked["message"]
    assert "resets 05:30 IST" in blocked["message"] and blocked["estimated_usd"] == 0.0


def test_csv_export_is_spreadsheet_safe(tailor_client: TestClient, tailored_job: tuple[int, int]) -> None:
    response = tailor_client.get("/api/resumes/export.csv")
    assert response.headers["content-disposition"].startswith("attachment")
    lines = response.text.strip().splitlines()
    assert lines[0].startswith("resume,version,job_id") and lines[1].startswith("R-0001,v1,")
