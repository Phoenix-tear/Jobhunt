"""Relevance filters, match sort, feedback, job detail and the matching section of /api/health."""

from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from tests.api.conftest import add_job, add_relevance, titles


def _seed(db: sqlite3.Connection) -> dict[str, int]:
    ids = {
        "ml": add_job(db, title="ML Engineer", workplace_type="remote", remote_eligible_india=1, remote_scope="worldwide"),
        "web": add_job(db, title="Full Stack Developer", workplace_type="hybrid"),
        "java": add_job(db, title="Java Developer", workplace_type="onsite"),
        "fresh": add_job(db, title="Unjudged Role"),
    }
    flags = {"scored": {"matched_skills": ["Python"], "missing_skills": ["Rust"],
                        "red_flags": ["needs US work authorization"], "recommended_profile": "ai"}}
    add_relevance(db, ids["ml"], "target", final_score=88.0, llm_score=90.0, llm_status="scored", role_family="ai_ml",
                  best_profile="ai", llm_reason="Strong fit.", llm_flags=json.dumps(flags),
                  score_components=json.dumps({"fit": 90, "location_reason": "remote and open to India"}))
    add_relevance(db, ids["web"], "maybe", final_score=55.0, role_family="fullstack", best_profile="fullstack")
    add_relevance(db, ids["java"], "excluded", role_family="other")
    return ids


def test_relevant_only_is_the_default_and_unjudged_jobs_stay_visible(client: TestClient, db: sqlite3.Connection) -> None:
    _seed(db)
    assert set(titles(client.get("/api/jobs").json())) == {"ML Engineer", "Full Stack Developer", "Unjudged Role"}
    everything = client.get("/api/jobs", params={"relevant_only": "false"}).json()
    assert everything["total"] == 4
    only_excluded = client.get("/api/jobs", params={"gate_status": "excluded"}).json()
    assert titles(only_excluded) == ["Java Developer"]


def test_filters_by_family_profile_score_and_remote(client: TestClient, db: sqlite3.Connection) -> None:
    _seed(db)

    def get(**params: object) -> list[str]:
        return titles(client.get("/api/jobs", params=params).json())

    assert get(role_family="ai_ml") == ["ML Engineer"]
    assert get(best_profile="fullstack") == ["Full Stack Developer"]
    assert get(min_score=60) == ["ML Engineer"]
    assert get(workplace_type=["remote", "hybrid"], sort="match") == ["ML Engineer", "Full Stack Developer"]
    assert get(remote_eligible_india="true") == ["ML Engineer"]
    assert get(remote_scope="worldwide") == ["ML Engineer"]
    assert client.get("/api/jobs", params={"workplace_type": "moon"}).status_code == 422
    assert client.get("/api/jobs", params={"min_score": 500}).status_code == 422


def test_summary_carries_score_and_remote_badges(client: TestClient, db: sqlite3.Connection) -> None:
    _seed(db)
    items = {i["title"]: i for i in client.get("/api/jobs", params={"sort": "match"}).json()["items"]}
    ml = items["ML Engineer"]
    assert (ml["match_score"], ml["gate_status"], ml["best_profile"], ml["llm_scored"]) == (88.0, "target", "ai", True)
    assert (ml["workplace_type"], ml["remote_eligible_india"], ml["remote_scope"]) == ("remote", True, "worldwide")
    assert items["Unjudged Role"]["gate_status"] is None and items["Unjudged Role"]["match_score"] is None


def test_detail_explains_the_match_and_the_exclusion(client: TestClient, db: sqlite3.Connection) -> None:
    ids = _seed(db)
    match = client.get(f"/api/jobs/{ids['ml']}").json()["match"]
    assert match["reason"] == "Strong fit." and match["matched_skills"] == ["Python"]
    assert match["missing_skills"] == ["Rust"] and match["red_flags"] == ["needs US work authorization"]
    assert match["recommended_profile"] == "ai" and match["exclusion_reason"] is None
    hidden = client.get(f"/api/jobs/{ids['java']}").json()["match"]
    assert hidden["gate_status"] == "excluded" and hidden["exclusion_reason"] == "Title names an excluded role."
    assert client.get(f"/api/jobs/{ids['fresh']}").json()["match"] is None


def test_dismiss_hides_the_job_and_undo_brings_it_back(client: TestClient, db: sqlite3.Connection) -> None:
    ids = _seed(db)
    job = ids["web"]
    reply = client.post(f"/api/jobs/{job}/feedback", json={"action": "dismiss", "reason": "wrong_stack"})
    assert reply.status_code == 200 and reply.json() == {"job_id": job, "feedback": "dismiss", "reason": "wrong_stack"}
    assert "Full Stack Developer" not in titles(client.get("/api/jobs").json())
    shown = client.get("/api/jobs", params={"include_dismissed": "true"}).json()
    assert {i["title"]: i["feedback"] for i in shown["items"]}["Full Stack Developer"] == "dismiss"
    assert client.get("/api/facets").json()["relevant"] == 2
    undone = client.post(f"/api/jobs/{job}/feedback", json={"action": "undo"})
    assert undone.json()["feedback"] is None
    assert "Full Stack Developer" in titles(client.get("/api/jobs").json())


def test_feedback_validation(client: TestClient, db: sqlite3.Connection) -> None:
    ids = _seed(db)
    url = f"/api/jobs/{ids['ml']}/feedback"
    assert client.post(url, json={"action": "explode"}).status_code == 422
    assert client.post(url, json={"action": "dismiss", "reason": "because"}).status_code == 422
    assert client.post(url, json={"action": "dismiss", "extra": 1}).status_code == 422
    assert client.post("/api/jobs/99999/feedback", json={"action": "like"}).status_code == 404
    assert client.post(url, json={"action": "like"}).json()["feedback"] == "like"


def test_facets_report_relevance_counts(client: TestClient, db: sqlite3.Connection) -> None:
    _seed(db)
    facets = client.get("/api/facets").json()
    assert (facets["scored"], facets["relevant"], facets["filtered_out"], facets["remote_open_to_india"]) == (2, 3, 1, 1)
    assert {w["value"]: w["count"] for w in facets["workplace_types"]}["remote"] == 1
    assert facets["gate_rules"] == [{"value": "hard_exclude_title", "count": 1}]


def test_health_reports_llm_usage_and_embedding_coverage(client: TestClient, db: sqlite3.Connection) -> None:
    _seed(db)
    body = client.get("/api/health").json()["matching"]
    assert body["usage"]["monthly_usd_cap"] > 0 and body["usage"]["daily_call_cap"] > 0
    assert body["usage"]["interactive_call_cap"] == 40 and body["usage"]["interactive_calls_today"] >= 0
    assert body["embeddable"] == 2 and body["embedded"] == 0 and body["embedding_coverage"] == 0
    assert body["last_relevance_run_at"] is not None


def test_junior_only_filter_and_seniority_fields(client: TestClient, db: sqlite3.Connection) -> None:
    junior = add_job(db, title="Associate Software Engineer")
    unspecified = add_job(db, title="Software Engineer")
    add_relevance(db, junior, "target", seniority_level="junior", seniority_evidence="title: 'associate software engineer'")
    add_relevance(db, unspecified, "target", seniority_level="unspecified")
    everything = {i["title"]: i for i in client.get("/api/jobs").json()["items"]}
    assert everything["Associate Software Engineer"]["seniority_level"] == "junior"
    assert "associate" in everything["Associate Software Engineer"]["seniority_evidence"]
    only = client.get("/api/jobs", params={"junior_only": "true"}).json()
    assert titles(only) == ["Associate Software Engineer"]


def test_health_includes_cost_projection_and_local_stages(client: TestClient, db: sqlite3.Connection) -> None:
    _seed(db)
    db.execute("INSERT INTO local_runs (stage, started_at, seconds, items, model, status) "
               "VALUES ('embed', '2026-10-14T01:00:00Z', 42.5, 120, 'ollama:nomic-embed-text', 'ok')")
    body = client.get("/api/health").json()["matching"]
    projection = body["projection"]
    assert projection["calls_per_day"] > 0 and projection["usd_per_month"] >= 0
    assert projection["usd_per_month_after_price_change"] >= projection["usd_per_month"]
    stages = {s["stage"]: s for s in body["local_stages"]}
    assert stages["embed"]["seconds"] == 42.5 and stages["local_llm"]["last_run_at"] is None


def _located(db: sqlite3.Connection) -> dict[str, int]:
    ids = {
        "india": add_job(db, title="Pune ML Engineer"),
        "remote": add_job(db, title="Remote ML Engineer"),
        "check": add_job(db, title="Remote Unclear Engineer"),
        "abroad": add_job(db, title="Berlin ML Engineer"),
        "wrong": add_job(db, title="US-only Engineer"),
    }
    add_relevance(db, ids["india"], "target", location_fit="india", location_reason="located in India",
                  location_matched="Pune")
    add_relevance(db, ids["remote"], "target", location_fit="remote_open_to_india")
    add_relevance(db, ids["check"], "target", location_fit="remote_unclear")
    add_relevance(db, ids["abroad"], "target", location_fit="abroad_unknown_workplace")
    add_relevance(db, ids["wrong"], "maybe", location_fit="wrong_location", location_reason="remote, restricted to US")
    return ids


def test_default_list_hides_other_locations_and_the_toggle_shows_them(client: TestClient, db: sqlite3.Connection) -> None:
    _located(db)
    default = set(titles(client.get("/api/jobs").json()))
    assert default == {"Pune ML Engineer", "Remote ML Engineer", "Remote Unclear Engineer"}
    everything = client.get("/api/jobs", params={"include_other_locations": "true"}).json()
    assert everything["total"] == 5
    only_wrong = client.get("/api/jobs", params={"location_fit": "wrong_location"}).json()
    assert titles(only_wrong) == ["US-only Engineer"]
    assert only_wrong["items"][0]["location_reason"] == "remote, restricted to US"


def test_location_filter_group_and_facet_counts(client: TestClient, db: sqlite3.Connection) -> None:
    _located(db)
    both = client.get("/api/jobs", params={"location_fit": ["india", "remote_open_to_india"]}).json()
    assert set(titles(both)) == {"Pune ML Engineer", "Remote ML Engineer"}
    facets = client.get("/api/facets").json()
    counts = {f["value"]: f["count"] for f in facets["location_fits"]}
    assert counts["india"] == 1 and counts["wrong_location"] == 1 and counts["abroad_unknown_workplace"] == 1
    assert facets["show_other_locations_default"] is False


def test_unfiltered_gate_view_still_shows_every_location(client: TestClient, db: sqlite3.Connection) -> None:
    _located(db)
    shown = client.get("/api/jobs", params={"relevant_only": "false"}).json()
    assert shown["total"] == 5          # "show filtered-out jobs" is not narrowed by location
    detail = client.get(f"/api/jobs/{_located_id(db)}").json()
    assert detail["location_fit"] == "india" and detail["location_matched"] == "Pune"


def _located_id(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT j.id FROM jobs j JOIN job_relevance r ON r.job_id = j.id "
                          "WHERE r.location_fit = 'india'").fetchone()[0])
