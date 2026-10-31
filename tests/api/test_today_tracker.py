"""v3: the Today view queries, the opened marker, and the tracker additions (contacts, follow-up, CSV)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from jobhunt.api import today
from jobhunt.timeutil import to_iso
from tests.api.conftest import add_job, add_relevance


def iso(delta: timedelta) -> str:
    return to_iso(datetime.now(timezone.utc) + delta)


def relevant(db: sqlite3.Connection, title: str, score: float | None = 70.0, *, fit: str = "india",
             gate: str = "target", **job) -> int:  # noqa: ANN003
    job_id = add_job(db, title=title, first_seen_at=job.pop("first_seen_at", iso(timedelta(hours=-1))), **job)
    add_relevance(db, job_id, gate, final_score=score, location_fit=fit, seniority_level="junior")
    return job_id


def titles(items: list[dict]) -> list[str]:
    return [item["title"] for item in items]


def test_new_jobs_gate_and_sort(client: TestClient, db: sqlite3.Connection) -> None:
    relevant(db, "Low Score", 40.0)
    relevant(db, "High Score", 90.0)
    relevant(db, "Remote India", 60.0, fit="remote_open_to_india")
    relevant(db, "Remote Unclear", 50.0, fit="remote_unclear")
    relevant(db, "Abroad", 99.0, fit="wrong_location")
    relevant(db, "Excluded", 99.0, gate="excluded")
    relevant(db, "Old", 99.0, first_seen_at="2026-01-01T00:00:00Z")
    view = client.get("/api/today").json()
    assert titles(view["new_jobs"]) == ["High Score", "Remote India", "Remote Unclear", "Low Score"]
    assert view["new_total"] == 4
    first = view["new_jobs"][0]
    assert first["location_fit"] == "india" and first["seniority_level"] == "junior" and first["sources"] == ["greenhouse"]
    assert first["first_seen_at"] and first["apply_url"].startswith("https://")


def test_since_last_visit_uses_a_session_boundary(db: sqlite3.Connection) -> None:
    now = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
    first = today.touch_visit(db, now)
    assert first == now - timedelta(hours=24)                              # first ever visit: the last 24 hours
    same_session = today.touch_visit(db, now + timedelta(minutes=10))
    assert same_session == first                                           # a reload does not move the boundary
    later = today.touch_visit(db, now + timedelta(hours=5))
    assert later == now + timedelta(minutes=10)                            # a new session starts after the last visit
    today.mark_all_seen(db, now + timedelta(hours=6))
    assert today.touch_visit(db, now + timedelta(hours=6, minutes=1)) == now + timedelta(hours=6)


def test_top_excludes_dismissed_and_applied_and_caps_at_fifteen(client: TestClient, db: sqlite3.Connection) -> None:
    for index in range(18):
        relevant(db, f"Job {index:02d}", 50.0 + index, first_seen_at="2026-09-01T00:00:00Z")
    dismissed = relevant(db, "Dismissed", 99.0, first_seen_at="2026-09-01T00:00:00Z")
    applied = relevant(db, "Applied", 98.0, first_seen_at="2026-09-01T00:00:00Z")
    saved = relevant(db, "Saved", 97.0, first_seen_at="2026-09-01T00:00:00Z")
    client.post(f"/api/jobs/{dismissed}/feedback", json={"action": "dismiss", "reason": "wrong_stack"})
    client.post("/api/applications", json={"job_id": applied, "stage": "applied"})
    client.post("/api/applications", json={"job_id": saved, "stage": "saved"})
    top = client.get("/api/today").json()["top"]
    assert len(top) == 15
    assert top[0]["title"] == "Saved" and "Dismissed" not in titles(top) and "Applied" not in titles(top)
    assert [t["match_score"] for t in top] == sorted((t["match_score"] for t in top), reverse=True)


def test_closing_soon_reads_a_known_deadline_only(client: TestClient, db: sqlite3.Connection) -> None:
    soon = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")
    far = (datetime.now(timezone.utc) + timedelta(days=60)).strftime("%Y-%m-%d")
    relevant(db, "Closes Soon", raw_json=json.dumps({"validThrough": soon}))
    relevant(db, "Closes Late", raw_json=json.dumps({"job": {"application_deadline": far}}))
    relevant(db, "No Deadline", raw_json=json.dumps({"title": "x"}))
    view = client.get("/api/today").json()
    assert titles(view["closing_soon"]) == ["Closes Soon"]
    assert view["closing_soon"][0]["deadline_at"].startswith(soon)


def test_deadline_parser_ignores_junk() -> None:
    assert today.deadline_from_raw(None) is None
    assert today.deadline_from_raw("not json") is None
    assert today.deadline_from_raw(json.dumps({"deadline": "soon"})) is None
    assert today.deadline_from_raw(json.dumps({"expires": "2026-12-01"})).year == 2026


def test_jobs_that_disappeared_after_i_opened_them(client: TestClient, db: sqlite3.Connection) -> None:
    gone = relevant(db, "Gone After Open", first_seen_at="2026-10-01T00:00:00Z")
    never_opened = relevant(db, "Gone Unopened", first_seen_at="2026-10-01T00:00:00Z")
    assert client.post(f"/api/jobs/{gone}/opened", json={}).status_code == 200
    assert client.post(f"/api/jobs/{gone}/opened", json={}).json()["job_id"] == gone
    for job_id in (gone, never_opened):
        db.execute("UPDATE jobs SET status = 'closed', closed_at = ? WHERE id = ?", (iso(timedelta(minutes=1)), job_id))
    db.commit()
    view = client.get("/api/today").json()
    assert titles(view["disappeared"]) == ["Gone After Open"]
    assert db.execute("SELECT open_count FROM job_opens WHERE job_id = ?", (gone,)).fetchone()[0] == 2
    assert client.post("/api/jobs/999999/opened", json={}).status_code == 404


def test_tracker_follow_ups_interviews_and_approved_resumes(client: TestClient, db: sqlite3.Connection) -> None:
    old = relevant(db, "Applied Long Ago", first_seen_at="2026-09-01T00:00:00Z")
    recent = relevant(db, "Applied Yesterday", first_seen_at="2026-09-01T00:00:00Z")
    interview = relevant(db, "Interview Soon", first_seen_at="2026-09-01T00:00:00Z")
    approved = relevant(db, "Approved Not Applied", first_seen_at="2026-09-01T00:00:00Z")
    for job_id, stage in ((old, "applied"), (recent, "applied"), (interview, "oa_interview")):
        client.post("/api/applications", json={"job_id": job_id, "stage": stage})
    long_ago = iso(timedelta(days=-9))
    db.execute("UPDATE applications SET applied_at = ?, follow_up_at = NULL WHERE job_id = ?", (long_ago, old))
    db.execute("UPDATE application_stage_history SET changed_at = ? WHERE application_id = "
               "(SELECT id FROM applications WHERE job_id = ?)", (long_ago, old))
    db.execute("INSERT INTO tailored_resumes (job_id, base_profile, mode, cache_key, status, created_at, approved, pdf_path)"
               " VALUES (?, 'ai', 'deterministic', 'k', 'ok', ?, 1, 'x.pdf')", (approved, iso(timedelta(0))))
    db.commit()
    view = client.get("/api/today").json()
    assert titles(view["follow_ups"]) == ["Applied Long Ago"] and view["follow_ups"][0]["days_since_change"] >= 9
    assert titles(view["interviews"]) == ["Interview Soon"]
    assert titles(view["approved_not_applied"]) == ["Approved Not Applied"]
    applying = client.post("/api/applications", json={"job_id": approved, "stage": "applied"})
    assert applying.status_code == 201
    assert "Approved Not Applied" not in titles(client.get("/api/today").json()["approved_not_applied"])


def test_counts_per_source_today(client: TestClient, db: sqlite3.Connection) -> None:
    relevant(db, "A", sources=[("greenhouse", "https://boards.greenhouse.io/a/jobs/1")])
    relevant(db, "B", sources=[("lever", "https://jobs.lever.co/b/1")])
    relevant(db, "C", sources=[("lever", "https://jobs.lever.co/c/1")], first_seen_at="2026-01-01T00:00:00Z")
    sources = {row["source"]: row for row in client.get("/api/today").json()["sources"]}
    assert sources["greenhouse"]["new_today"] == 1 and sources["lever"]["new_today"] == 1
    assert sources["lever"]["open_total"] == 2


# --- tracker additions ------------------------------------------------------------------------------------------------


def test_applied_sets_a_default_follow_up_seven_days_later_and_it_is_editable(client: TestClient, db: sqlite3.Connection) -> None:
    job = relevant(db, "Tracked")
    created = client.post("/api/applications", json={"job_id": job}).json()
    assert created["follow_up_at"] is None
    moved = client.patch(f"/api/applications/{created['id']}", json={"stage": "applied"}).json()
    applied = datetime.fromisoformat(moved["applied_at"].replace("Z", "+00:00"))
    assert moved["follow_up_at"] == (applied + timedelta(days=7)).date().isoformat()
    edited = client.patch(f"/api/applications/{created['id']}", json={"follow_up_at": "2026-12-24"}).json()
    assert edited["follow_up_at"] == "2026-12-24"


def test_contact_fields_and_attached_resume_round_trip(client: TestClient, db: sqlite3.Connection) -> None:
    job = relevant(db, "With Contact")
    app = client.post("/api/applications", json={"job_id": job, "stage": "applied"}).json()
    db.execute("INSERT INTO tailored_resumes (job_id, base_profile, mode, cache_key, status, created_at) "
               "VALUES (?, 'ai', 'deterministic', 'k2', 'ok', '2026-10-15T00:00:00Z')", (job,))
    db.commit()
    tailored_id = db.execute("SELECT id FROM tailored_resumes WHERE job_id = ?", (job,)).fetchone()[0]
    patched = client.patch(f"/api/applications/{app['id']}", json={
        "contact_name": "Priya Recruiter", "contact_email": "priya@example.test", "contact_phone": "+91 99999 00000",
        "notes": "Referred by Sam", "tailored_resume_id": tailored_id}).json()
    assert patched["contact_name"] == "Priya Recruiter" and patched["tailored_resume_id"] == tailored_id
    card = next(c for col in client.get("/api/applications").json()["columns"] for c in col["cards"])
    assert card["application"]["contact_email"] == "priya@example.test"


def test_csv_export_has_every_application_and_is_safe_for_spreadsheets(client: TestClient, db: sqlite3.Connection) -> None:
    first = relevant(db, "=HYPERLINK(\"http://evil\")")
    second = relevant(db, "Normal Role")
    a = client.post("/api/applications", json={"job_id": first, "stage": "applied"}).json()
    client.post("/api/applications", json={"job_id": second})
    client.patch(f"/api/applications/{a['id']}", json={"notes": "line one\nline, two", "contact_name": "@boss"})
    response = client.get("/api/applications/export.csv")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    import csv
    import io

    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 2
    risky = next(r for r in rows if r["stage"] == "applied")
    assert risky["title"].startswith("'=") and risky["contact_name"] == "'@boss"
    assert risky["notes"] == "line one\nline, two" and risky["follow_up_at"]
    assert set(rows[0]) >= {"applied_at", "resume_version", "resume_pdf_link", "contact_email", "notes", "follow_up_at"}


def test_job_detail_has_signals_and_list_has_caution_flag(client: TestClient, db: sqlite3.Connection) -> None:
    old = relevant(db, "Old Posting", posted_at="2026-06-01T00:00:00Z", sources=[("simplify_newgrad", "https://x.test/a")])
    fresh = relevant(db, "Fresh Posting", posted_at=iso(timedelta(days=-3)))
    detail = client.get(f"/api/jobs/{old}").json()
    ids = {item["id"]: item for item in detail["signals"]}
    assert ids["aggregator_only"]["level"] == "caution"
    assert ids["posting_age"]["level"] == "caution" and "days ago on the Simplify new-grad list" in ids["posting_age"]["text"]
    assert detail["has_caution_signal"] is True
    fresh_detail = client.get(f"/api/jobs/{fresh}").json()
    assert fresh_detail["has_caution_signal"] is False
    assert {item["id"] for item in fresh_detail["signals"]} >= {"posting_age", "employer_confirmed"}
    listed = {item["id"]: item for item in client.get("/api/jobs?relevant_only=false&include_other_locations=true").json()["items"]}
    assert listed[old]["has_caution_signal"] is True and listed[fresh]["has_caution_signal"] is False


def test_today_hide_caution_filter_is_off_by_default(client: TestClient, db: sqlite3.Connection) -> None:
    relevant(db, "Caution Role", 90.0, posted_at="2026-06-01T00:00:00Z")
    relevant(db, "Clean Role", 50.0, posted_at=iso(timedelta(days=-2)))
    everything = client.get("/api/today").json()
    assert titles(everything["new_jobs"]) == ["Caution Role", "Clean Role"]
    filtered = client.get("/api/today?hide_caution=true").json()
    assert titles(filtered["new_jobs"]) == ["Clean Role"] and titles(filtered["top"]) == ["Clean Role"]
