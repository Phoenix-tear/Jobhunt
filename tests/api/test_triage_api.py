"""v8 Part B on the API: the verdict block of a job, 'LLM verdict is wrong', and the Health counters."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobhunt.config import Config
from jobhunt.matching.gate import run_relevance
from jobhunt.matching.triage import Verdict
from tests.api.conftest import add_job

SENIOR = Verdict(level="senior", confidence="high", level_quote="Senior Engineer to own the platform",
                 required_years_min=6, years_quote="6+ years of backend experience")


@pytest.fixture()
def tconfig(api_config: Config, tmp_path: Path) -> Config:
    return dataclasses.replace(api_config, project_root=tmp_path, migrations_dir=str(api_config.migrations_directory))


@pytest.fixture()
def tclient(make_client: Callable[..., TestClient], tconfig: Config) -> TestClient:
    return make_client(tconfig)


def store(db: sqlite3.Connection, config: Config, job_id: int, verdict: Verdict, **extra: object) -> None:
    raw = {"level": verdict.level, "required_years_min": verdict.required_years_min or -1, "india_eligible": "unclear",
           "eligibility_blockers": []}
    db.execute(
        "INSERT INTO job_llm_verdicts (job_id, content_hash, rubric_version, model, raw_json, verified_json, created_at) "
        "VALUES (?, 'h', ?, 'gemini-3.8-flash', ?, ?, '2026-10-12T09:00:00Z')",
        (job_id, config.triage.rubric_version, json.dumps(raw), json.dumps(verdict.as_json())))


def seeded(db: sqlite3.Connection, config: Config) -> int:
    job_id = add_job(db, title="Python Backend Engineer", description_text="We build Python APIs for customers.")
    run_relevance(db, config)
    return job_id


def test_job_detail_without_a_verdict_has_no_triage_block(tclient: TestClient, db: sqlite3.Connection, tconfig: Config) -> None:
    job_id = seeded(db, tconfig)
    assert tclient.get(f"/api/jobs/{job_id}").json()["triage"] is None


def test_job_detail_shows_the_verdict_quotes_and_the_rule_that_used_it(
    tclient: TestClient, db: sqlite3.Connection, tconfig: Config
) -> None:
    job_id = seeded(db, tconfig)
    store(db, tconfig, job_id, SENIOR)
    run_relevance(db, tconfig)
    body = tclient.get(f"/api/jobs/{job_id}").json()
    verdict = body["triage"]["verdict"]
    assert body["match"]["gate_rule"] == "llm_seniority" and "Senior Engineer to own the platform" in body["match"]["exclusion_reason"]
    assert verdict["used_by"] == "llm_seniority" and verdict["would_exclude"] is True
    assert {"field": "Level", "value": "senior", "quote": "Senior Engineer to own the platform"} in verdict["quotes"]
    assert verdict["model"] == "gemini-3.8-flash" and verdict["human_label"] is None


def test_protected_job_shows_the_verdict_but_says_it_was_held_back(
    tclient: TestClient, db: sqlite3.Connection, tconfig: Config
) -> None:
    job_id = seeded(db, tconfig)
    db.execute("INSERT INTO job_decisions (job_id, action, happened_at, created_at) VALUES (?, 'saved', 'n', 'n')", (job_id,))
    store(db, tconfig, job_id, SENIOR)
    run_relevance(db, tconfig)
    body = tclient.get(f"/api/jobs/{job_id}").json()
    assert body["match"]["gate_status"] != "excluded"
    assert body["triage"]["verdict"]["used_by"] == "" and "held back" in body["triage"]["verdict"]["used_text"]


def test_verdict_is_wrong_exempts_the_job_and_can_be_undone(
    tclient: TestClient, db: sqlite3.Connection, tconfig: Config
) -> None:
    job_id = seeded(db, tconfig)
    store(db, tconfig, job_id, SENIOR)
    run_relevance(db, tconfig)
    assert tclient.get(f"/api/jobs/{job_id}").json()["match"]["gate_status"] == "excluded"
    answer = tclient.post(f"/api/jobs/{job_id}/llm-verdict-wrong", json={"label": "wrong_level", "reason": "entry role"})
    assert answer.status_code == 200
    body = tclient.get(f"/api/jobs/{job_id}").json()
    assert body["match"]["gate_status"] == "target" and body["triage"]["verdict"]["human_label"] == "wrong_level"
    assert "ignored" in body["triage"]["verdict"]["used_text"]
    assert tclient.delete(f"/api/jobs/{job_id}/llm-verdict-wrong").status_code == 200
    assert tclient.get(f"/api/jobs/{job_id}").json()["match"]["gate_status"] == "excluded"


def test_verdict_is_wrong_needs_a_verdict_and_a_known_label(tclient: TestClient, db: sqlite3.Connection, tconfig: Config) -> None:
    job_id = seeded(db, tconfig)
    assert tclient.post(f"/api/jobs/{job_id}/llm-verdict-wrong", json={"label": "other"}).status_code == 404
    store(db, tconfig, job_id, SENIOR)
    assert tclient.post(f"/api/jobs/{job_id}/llm-verdict-wrong", json={"label": "nonsense"}).status_code == 422


def test_health_reports_verdict_counts_leak_rate_hallucinations_and_my_agreement(
    tclient: TestClient, db: sqlite3.Connection, tconfig: Config
) -> None:
    first = seeded(db, tconfig)
    second = add_job(db, title="Python Backend Engineer", description_text="We build other Python APIs.")
    run_relevance(db, tconfig)
    store(db, tconfig, first, SENIOR)
    store(db, tconfig, second, SENIOR)
    run_relevance(db, tconfig)
    tclient.post(f"/api/jobs/{second}/llm-verdict-wrong", json={"label": "wrong_level"})
    health = tclient.get("/api/health").json()["triage"]
    assert health["verdicts"] == 2 and health["by_level"] == {"senior": 2}
    assert health["verdicts_that_acted"] == 2 and health["verdict_wrong_marked"] == 1 and health["agreement"] == 0.5
    assert health["leak_rate_last_100"] == 0.5          # the one I exempted is visible although its verdict would exclude
    assert health["hallucinated_evidence_rate"] == 0.0 and health["demoted_week_llm"] == 1
    assert health["daily_calls_cap"] == tconfig.budget.triage_daily_calls
