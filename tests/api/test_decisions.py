"""Applied / Not applying / Save / Undo, the views, Today, label mapping, guarded retrain, suggestions and learned rules."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobhunt import decisions
from jobhunt.config import Config
from jobhunt.matching import learned_rules, ranker
from jobhunt.timeutil import to_iso
from tests.api.conftest import add_job, add_relevance
from tests.api.test_today_tracker import relevant


def add_resume(db: sqlite3.Connection, job_id: int, number: int, lifecycle: str = "approved", status: str = "ok") -> int:
    cursor = db.execute(
        """INSERT INTO tailored_resumes (job_id, base_profile, mode, cache_key, status, created_at, pages, resume_number,
               version, lifecycle) VALUES (?, 'ai', 'deterministic', ?, ?, 'x', 1, ?, 1, ?)""",
        (job_id, f"k{number}", status, number, lifecycle))
    return int(cursor.lastrowid)


def stage_of(db: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()


# --- applied -------------------------------------------------------------------------------------------------------


def test_applied_creates_the_tracker_entry_sets_follow_up_attaches_and_marks_the_resume_sent(
        client: TestClient, db: sqlite3.Connection) -> None:
    job = add_job(db, title="ML Engineer", company_name="Collinear AI")
    resume = add_resume(db, job, 7)
    response = client.post(f"/api/jobs/{job}/decision/applied",
                           json={"applied_at": "2026-10-17T10:00:00Z", "how": "linkedin", "resume_id": resume, "note": "via recruiter"})
    assert response.status_code == 200, response.text
    application = stage_of(db, job)
    assert application["stage"] == "applied" and application["applied_at"] == "2026-10-17T10:00:00Z"
    assert application["follow_up_at"] == "2026-10-24" and application["tailored_resume_id"] == resume
    assert db.execute("SELECT lifecycle, sent_at FROM tailored_resumes WHERE id = ?", (resume,)).fetchone()[:] == (
        "sent", "2026-10-17T10:00:00Z")
    decision = db.execute("SELECT action, how, resume_id, note FROM job_decision_current WHERE job_id = ?", (job,)).fetchone()
    assert tuple(decision) == ("applied", "linkedin", resume, "via recruiter")
    history = db.execute("SELECT from_stage, to_stage FROM application_stage_history WHERE application_id = ?",
                         (application["id"],)).fetchall()
    assert [tuple(h) for h in history] == [(None, "applied")]


def test_applied_moves_an_existing_saved_entry_and_rejects_bad_input(client: TestClient, db: sqlite3.Connection) -> None:
    job, other = add_job(db, title="A"), add_job(db, title="B")
    client.post(f"/api/jobs/{job}/decision/save")
    assert stage_of(db, job)["stage"] == "saved"
    client.post(f"/api/jobs/{job}/decision/applied", json={})
    assert stage_of(db, job)["stage"] == "applied"
    assert db.execute("SELECT COUNT(*) FROM applications WHERE job_id = ?", (job,)).fetchone()[0] == 1
    foreign = add_resume(db, other, 3)
    assert client.post(f"/api/jobs/{job}/decision/applied", json={"resume_id": foreign}).status_code == 422
    assert client.post(f"/api/jobs/{job}/decision/applied", json={"how": "carrier pigeon"}).status_code == 422
    failed = add_resume(db, job, 4, status="failed")
    assert client.post(f"/api/jobs/{job}/decision/applied", json={"resume_id": failed}).status_code == 422
    assert client.post("/api/jobs/99999/decision/applied", json={}).status_code == 404


def test_undo_of_applied_restores_the_tracker_and_the_resume(client: TestClient, db: sqlite3.Connection) -> None:
    job = add_job(db)
    resume = add_resume(db, job, 7)
    client.post(f"/api/jobs/{job}/decision/applied", json={"resume_id": resume})
    assert client.post(f"/api/jobs/{job}/decision/undo").json()["reverted"] == "applied"
    assert stage_of(db, job) is None                                   # the entry it created is gone
    assert db.execute("SELECT lifecycle, sent_at FROM tailored_resumes WHERE id = ?", (resume,)).fetchone()[:] == ("approved", None)
    assert db.execute("SELECT 1 FROM job_decision_current WHERE job_id = ?", (job,)).fetchone() is None
    # from a saved entry, undo goes back to saved
    client.post(f"/api/jobs/{job}/decision/save")
    client.post(f"/api/jobs/{job}/decision/applied", json={})
    client.post(f"/api/jobs/{job}/decision/undo")
    assert stage_of(db, job)["stage"] == "saved" and stage_of(db, job)["applied_at"] is None
    assert db.execute("SELECT action FROM job_decision_current WHERE job_id = ?", (job,)).fetchone()[0] == "saved"


# --- not applying --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("reason,legacy", [
    ("ineligible_phd_or_masters", "other"), ("ineligible_other", "other"), ("wrong_role", "wrong_role"),
    ("too_senior", "too_senior"), ("wrong_domain_or_stack", "wrong_stack"), ("wrong_location", "wrong_location"),
    ("company_not_interested", "company"), ("poor_posting_or_suspicious", "other"),
    ("already_applied_elsewhere", "other"), ("other", "other"),
])
def test_every_reason_is_accepted_and_mirrored_as_a_legacy_dismiss(client: TestClient, db: sqlite3.Connection,
                                                                    reason: str, legacy: str) -> None:
    job = add_job(db)
    assert client.post(f"/api/jobs/{job}/decision/not-applying", json={"reason": reason, "note": "n"}).status_code == 200
    assert tuple(db.execute("SELECT action, reason FROM job_decision_current WHERE job_id = ?", (job,)).fetchone()) == ("not_applying", reason)
    assert tuple(db.execute("SELECT action, reason FROM job_feedback_current WHERE job_id = ?", (job,)).fetchone()) == ("dismiss", legacy)


def test_unknown_reason_is_refused_and_a_reason_can_be_edited(client: TestClient, db: sqlite3.Connection) -> None:
    job = add_job(db)
    assert client.post(f"/api/jobs/{job}/decision/not-applying", json={"reason": "because"}).status_code == 422
    client.post(f"/api/jobs/{job}/decision/not-applying", json={"reason": "wrong_role"})
    client.post(f"/api/jobs/{job}/decision/not-applying", json={"reason": "ineligible_other"})
    assert db.execute("SELECT reason FROM job_decision_current WHERE job_id = ?", (job,)).fetchone()[0] == "ineligible_other"
    assert db.execute("SELECT COUNT(*) FROM job_decisions WHERE job_id = ?", (job,)).fetchone()[0] == 2   # history kept
    client.post(f"/api/jobs/{job}/decision/undo")
    assert db.execute("SELECT 1 FROM job_feedback_current WHERE job_id = ?", (job,)).fetchone()[0] == 1   # back to wrong_role's dismiss


def test_undo_of_not_applying_removes_the_dismiss(client: TestClient, db: sqlite3.Connection) -> None:
    job = add_job(db)
    client.post(f"/api/jobs/{job}/decision/not-applying", json={"reason": "wrong_role"})
    client.post(f"/api/jobs/{job}/decision/undo")
    assert db.execute("SELECT 1 FROM job_feedback_current WHERE job_id = ?", (job,)).fetchone() is None
    assert db.execute("SELECT 1 FROM job_decision_current WHERE job_id = ?", (job,)).fetchone() is None


def test_bulk_not_applying_uses_one_reason(client: TestClient, db: sqlite3.Connection) -> None:
    ids = [add_job(db, title=f"Job {i}") for i in range(3)]
    response = client.post("/api/decisions/not-applying", json={"job_ids": ids, "reason": "wrong_location"})
    assert response.status_code == 200 and len(response.json()) == 3
    reasons = {r["reason"] for r in db.execute("SELECT reason FROM job_decision_current")}
    assert reasons == {"wrong_location"}
    assert client.post("/api/decisions/not-applying", json={"job_ids": ids + [99999], "reason": "other"}).status_code == 404
    assert client.post("/api/decisions/not-applying", json={"job_ids": ids, "reason": "nope"}).status_code == 422


# --- views, Today, warnings ------------------------------------------------------------------------------------------


def test_tabs_and_today_hide_decided_jobs_but_keep_them_searchable(client: TestClient, db: sqlite3.Connection) -> None:
    keep = relevant(db, "Open Role", 60.0)
    saved = relevant(db, "Saved Role", 70.0)
    applied = relevant(db, "Applied Role", 80.0)
    skipped = relevant(db, "Skipped Role", 90.0)
    client.post(f"/api/jobs/{saved}/decision/save")
    client.post(f"/api/jobs/{applied}/decision/applied", json={})
    client.post(f"/api/jobs/{skipped}/decision/not-applying", json={"reason": "wrong_role"})

    def titles(view: str) -> set[str]:
        body = client.get(f"/api/jobs?view={view}&relevant_only=false&include_other_locations=true").json()
        return {item["title"] for item in body["items"]}

    assert titles("open") == {"Open Role", "Saved Role"}
    assert titles("saved") == {"Saved Role"}
    assert titles("applied") == {"Applied Role"}
    assert titles("not_applying") == {"Skipped Role"}
    assert titles("all") == {"Open Role", "Saved Role", "Applied Role", "Skipped Role"}
    today = client.get("/api/today").json()
    shown = {item["title"] for key in ("new_jobs", "top") for item in today[key]}
    assert "Applied Role" not in shown and "Skipped Role" not in shown and {"Open Role", "Saved Role"} <= shown
    summary = {item["title"]: item for item in client.get("/api/jobs?view=all&relevant_only=false&include_other_locations=true").json()["items"]}
    assert summary["Skipped Role"]["decision"] == "not_applying" and summary["Skipped Role"]["decision_reason"] == "wrong_role"
    assert summary["Applied Role"]["decision"] == "applied" and summary["Open Role"]["decision"] is None


def test_week_strip_counts_current_decisions(client: TestClient, db: sqlite3.Connection) -> None:
    a, b, c, d = (relevant(db, f"Role {i}", 50.0) for i in range(4))
    client.post(f"/api/jobs/{a}/decision/applied", json={})
    client.post(f"/api/jobs/{b}/decision/save")
    client.post(f"/api/jobs/{c}/decision/not-applying", json={"reason": "other"})
    client.post(f"/api/jobs/{d}/decision/save")
    client.post(f"/api/jobs/{d}/decision/undo")                         # an undone decision does not count
    week = client.get("/api/today").json()["week"]
    assert (week["applied"], week["saved"], week["not_applying"]) == (1, 1, 1) and "follow_ups_due" in week


def test_company_warning_within_sixty_days_only(client: TestClient, db: sqlite3.Connection) -> None:
    first = add_job(db, title="Applied Scientist", company_name="Collinear AI")
    second = add_job(db, title="ML Intern", company_name="Collinear AI")
    third = add_job(db, title="Other", company_name="Elsewhere")
    resume = add_resume(db, first, 7)
    recent = to_iso(datetime.now(timezone.utc) - timedelta(days=3))
    client.post(f"/api/jobs/{first}/decision/applied", json={"applied_at": recent, "resume_id": resume})
    day = datetime.fromisoformat(recent.replace("Z", "+00:00"))
    expected = f"You applied to Collinear AI on {day.day} {day.strftime('%b')} (R-0007)"
    assert client.get(f"/api/jobs/{second}").json()["applied_company_warning"] == expected
    assert client.get(f"/api/jobs/{third}").json()["applied_company_warning"] is None
    assert client.get(f"/api/jobs/{first}").json()["applied_company_warning"] is None      # not warned about itself
    old = to_iso(datetime.now(timezone.utc) - timedelta(days=90))
    db.execute("UPDATE applications SET applied_at = ? WHERE job_id = ?", (old, first))
    assert client.get(f"/api/jobs/{second}").json()["applied_company_warning"] is None


def test_job_detail_shows_eligibility_and_why_this_match(client: TestClient, db: sqlite3.Connection) -> None:
    job = add_job(db, title="Research Scientist Intern", company_name="Collinear AI")
    add_relevance(db, job, "maybe", final_score=50.0, similarity_ai=0.7006, similarity_fullstack=0.7037, best_profile="fullstack",
                  role_family="ai_ml", location_fit="india", llm_reason=None, local_rank_score=87.39,
                  score_components=json.dumps({"fit": 87.39, "fit_source": "ranker", "final": 50.0}),
                  eligibility_json=json.dumps([
                      {"kind": "phd_required", "strength": "required", "snippet": "must be currently pursuing a PhD"},
                      {"kind": "us_work_authorization", "strength": "preferred", "snippet": "authorized to work in the US"}]))
    body = client.get(f"/api/jobs/{job}").json()
    assert body["likely_ineligible"] == "Likely not eligible: PhD required"
    kinds = [f["kind"] for f in body["eligibility"]]
    assert kinds == ["phd_required"]                                   # US work-authorization chip hidden: open to India
    why = body["why_match"]
    assert why["fit_source"] == "ranker" and "local ranker" in why["fit_source_note"] and why["fit_reason"] is None
    assert "near tie" in why["profile_note"] and "AI resume is the better base" in why["profile_note"]
    assert "55% fit" in why["formula"]


# --- learning ------------------------------------------------------------------------------------------------------


@pytest.fixture()
def rcfg(config: Config, tmp_path: Path) -> Config:
    return dataclasses.replace(config, project_root=tmp_path)


def seed_ranker_jobs(db: sqlite3.Connection, count: int = 12) -> list[int]:
    ids = []
    for n in range(count):
        job_id = add_job(db, title=f"Machine Learning Engineer {n}" if n % 2 == 0 else f"Support Engineer {n}")
        add_relevance(db, job_id, "target", similarity_ai=0.7 + n * 0.001, similarity_fullstack=0.6, similarity_percentile=0.5,
                      seniority_level="unspecified", role_family="ai_ml")
        ids.append(job_id)
    return ids


def test_label_mapping_only_role_fit_reasons_teach_role_fit(db: sqlite3.Connection, rcfg: Config) -> None:
    ids = seed_ranker_jobs(db, 10)
    db.execute("UPDATE job_relevance SET llm_status = 'scored', llm_score = 55")
    for index, (action, reason) in enumerate([("applied", None), ("saved", None), ("not_applying", "wrong_role"),
                                              ("not_applying", "too_senior"), ("not_applying", "wrong_domain_or_stack"),
                                              ("not_applying", "ineligible_phd_or_masters"), ("not_applying", "wrong_location"),
                                              ("not_applying", "company_not_interested"), ("not_applying", "other")]):
        db.execute("INSERT INTO job_decisions (job_id, action, reason, happened_at, created_at) VALUES (?, ?, ?, 'x', 'x')",
                   (ids[index], action, reason))
    data = ranker.build_dataset(db, rcfg)
    labels = {job_id: (label, kind) for job_id, label, kind in zip(data.job_ids, data.labels, data.kinds)}
    assert labels[ids[0]] == (100.0, "applied") and labels[ids[1]] == (80.0, "saved")
    for index in (2, 3, 4):
        assert labels[ids[index]] == (0.0, "not_applying_role")
    for index in (5, 6, 7, 8):                       # not role-fit evidence: they keep their Gemini label (55), not a 0
        assert labels[ids[index]] == (55.0, "gemini_score"), index
    assert ranker.HUMAN_KINDS == ("applied", "saved", "not_applying_role", "feedback", "gold")


def test_retrain_waits_for_ten_new_labels_and_keeps_a_better_old_model(db: sqlite3.Connection, rcfg: Config) -> None:
    seed_ranker_jobs(db, 100)
    db.execute("UPDATE job_relevance SET llm_status = 'scored', llm_score = CASE WHEN job_id % 2 = 0 THEN 90 ELSE 15 END")
    first = ranker.train(db, rcfg)
    assert first.trained
    again = ranker.retrain_if_needed(db, rcfg)
    assert again["action"] == "skipped" and "new labels" in again["reason"]
    # 12 more labelled jobs -> eligible, and the new model is as good: retrained
    seed_ranker_jobs(db, 12)
    db.execute("UPDATE job_relevance SET llm_status = 'scored', llm_score = CASE WHEN job_id % 2 = 0 THEN 90 ELSE 15 END "
               "WHERE llm_score IS NULL")
    assert ranker.retrain_if_needed(db, rcfg)["action"] == "retrained"
    # now poison the labels so the cross-validated Spearman collapses: the previous model is kept
    meta = ranker.read_meta(rcfg)
    meta["cv"][meta["chosen"]]["spearman"] = 0.99
    (rcfg.resolve_path("data/models") / ranker.META_FILE).write_text(json.dumps(meta), encoding="utf-8")
    seed_ranker_jobs(db, 14)
    db.execute("UPDATE job_relevance SET llm_status = 'scored', llm_score = 50 WHERE llm_score IS NULL")
    decision = ranker.retrain_if_needed(db, rcfg)
    assert decision["action"] == "kept_previous" and "below" in decision["reason"]
    after = ranker.read_meta(rcfg)
    assert after["in_use"] == "previous" and after["version"] == meta["version"] and after["last_decision"]["action"] == "kept_previous"
    assert "ranker-retrain: kept_previous" in ranker.describe_decision(decision)


def test_retrain_keeps_the_old_model_under_100_labels_when_one_exists(db: sqlite3.Connection, rcfg: Config) -> None:
    seed_ranker_jobs(db, 50)
    db.execute("UPDATE job_relevance SET llm_status = 'scored', llm_score = CASE WHEN job_id % 2 = 0 THEN 90 ELSE 15 END")
    ranker.train(db, rcfg)
    seed_ranker_jobs(db, 15)
    db.execute("UPDATE job_relevance SET llm_status = 'scored', llm_score = 80 WHERE llm_score IS NULL")
    decision = ranker.retrain_if_needed(db, rcfg)
    assert decision["action"] == "kept_previous" and "usable labels" in decision["reason"]


def test_suggestions_need_five_jobs_and_approval_writes_a_removable_rule(
        client: TestClient, db: sqlite3.Connection, tmp_path: Path) -> None:
    import dataclasses as dc

    from jobhunt.api import app as app_module  # noqa: F401  (ensures routes import)

    isolated = dc.replace(client.app.state.config, project_root=tmp_path) if hasattr(client.app.state, "config") else None
    assert isolated is not None
    client.app.state.config = isolated
    for index in range(6):
        job = add_job(db, title=f"Quantum Researcher {index}")
        add_relevance(db, job, "target")
        client.post(f"/api/jobs/{job}/decision/not-applying", json={"reason": "wrong_domain_or_stack"})
    for index in range(2):
        job = add_job(db, title=f"Blockchain Wizard {index}")
        client.post(f"/api/jobs/{job}/decision/not-applying", json={"reason": "wrong_role"})
    learning = client.get("/api/learning").json()
    keys = {p["key"] for p in learning["proposals"]}
    assert "title_phrase:quantum" in keys and "title_phrase:blockchain" not in keys       # 2 jobs < 5
    proposal = next(p for p in learning["proposals"] if p["key"] == "title_phrase:quantum")
    assert proposal["count"] == 6 and len(proposal["job_ids"]) == 6 and "wrong_domain_or_stack" in proposal["reason"]
    assert learning["rules"] == [] and "Only decisions you made yourself" in learning["honesty"]
    assert learning["labels"]["not_applying_role_fit"] == 8
    approved = client.post("/api/learning/proposals/approve", json={"key": "title_phrase:quantum"})
    assert approved.status_code == 200, approved.text
    path = isolated.resolve_path(learned_rules.LEARNED_FILE)
    rules = learned_rules.load_rules(path)
    assert len(rules) == 1 and rules[0].phrase == "quantum" and rules[0].evidence_count == 6 and rules[0].demote
    assert "title_phrase:quantum" not in {p["key"] for p in client.get("/api/learning").json()["proposals"]}
    assert client.delete(f"/api/learning/rules/{rules[0].id}").status_code == 200
    assert learned_rules.load_rules(path) == [] and client.delete("/api/learning/rules/L-0099").status_code == 404
    assert "title_phrase:quantum" in {p["key"] for p in client.get("/api/learning").json()["proposals"]}   # can come back
    client.post("/api/learning/proposals/reject", json={"key": "title_phrase:quantum"})
    assert "title_phrase:quantum" not in {p["key"] for p in client.get("/api/learning").json()["proposals"]}
    assert client.post("/api/learning/proposals/approve", json={"key": "title_phrase:nothing"}).status_code == 404


def test_no_model_client_is_touched_by_decisions_or_learning(client: TestClient, db: sqlite3.Connection,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("a model client was used")

    monkeypatch.setattr("jobhunt.matching.gemini.GeminiClient.generate_json", forbidden)
    monkeypatch.setattr("jobhunt.tailoring.service.make_tailor", forbidden)
    job = add_job(db)
    for call in (lambda: client.post(f"/api/jobs/{job}/decision/save"),
                 lambda: client.post(f"/api/jobs/{job}/decision/applied", json={}),
                 lambda: client.post(f"/api/jobs/{job}/decision/not-applying", json={"reason": "other"}),
                 lambda: client.post(f"/api/jobs/{job}/decision/undo"), lambda: client.get("/api/learning"),
                 lambda: client.get(f"/api/jobs/{job}"), lambda: client.get("/api/today")):
        assert call().status_code == 200
