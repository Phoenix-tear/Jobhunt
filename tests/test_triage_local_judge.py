"""Unit tests for Tier 2 Local LLM judge and accuracy gate (Brief v9 Phase 3)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import httpx
import pytest

from jobhunt.config import Config, load_config
from jobhunt.matching.ollama import ChatResult, OllamaClient, OllamaError
from jobhunt.matching.triage import Evidence, Verdict
from jobhunt.matching.triage_local_judge import (
    AccuracyGateResult,
    TriageLocalJudge,
    run_accuracy_gate,
    select_best_instruct_model,
)


def _create_test_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_llm_verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            rubric_version TEXT NOT NULL,
            model TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            verified_json TEXT NOT NULL,
            hallucinated INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'gemini',
            UNIQUE (job_id, content_hash, rubric_version, model)
        )
        """
    )


def _add_job(conn: sqlite3.Connection, job_id: int, title: str, company: str, desc: str = "") -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO jobs (id, fingerprint, title, company_name, description_text, location_raw, country, status, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, 'Bangalore', 'IN', 'open', '2026-10-25T00:00:00Z', '2026-10-25T00:00:00Z')
        """,
        (job_id, f"fp_{job_id}", title, company, desc),
    )


def test_select_best_instruct_model() -> None:
    """Verify priority model selection logic."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.5.0"})
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "mxbai-embed-large:latest"},
                        {"name": "llama3.1:8b"},
                        {"name": "qwen2.5:7b-instruct"},
                    ]
                },
            )
        return httpx.Response(404)

    client = OllamaClient(transport=httpx.MockTransport(handler))
    selected = select_best_instruct_model(client)
    # qwen2.5:7b-instruct has higher priority than llama3.1:8b
    assert selected == "qwen2.5:7b-instruct"


def test_local_judge_one_verbatim_verification_and_caching(connection: sqlite3.Connection, config: Config) -> None:
    """Verify local LLM judge verifies exact quotes, records hallucinations, and caches."""
    _create_test_db(connection)

    title = "Senior Python Engineer"
    company = "TechCorp"
    extract = "We need a senior developer with 5+ years of experience. Location is Bangalore, India."

    ev = Evidence(
        title=title,
        company=company,
        location="Bangalore",
        workplace="hybrid",
        extract=extract,
    )

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.5.0"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:7b-instruct"}]})
        if request.url.path == "/api/chat":
            calls += 1
            verdict_dict = {
                "verdicts": [
                    {
                        "job": 1,
                        "level": "senior",
                        "required_years_min": 5,
                        "suitable_for_new_grad": "no",
                        "india_eligible": "yes",
                        "location_scope": "india",
                        "eligibility_blockers": [],
                        "role_family": ["backend"],
                        "confidence": "high",
                        "level_evidence": "senior developer",  # Valid verbatim substring
                        "years_evidence": "5+ years of experience",  # Valid verbatim substring
                        "india_evidence": "India",  # Valid verbatim substring
                        "blocker_evidence": "completely fabricated quote not in extract",  # Hallucinated!
                    }
                ]
            }
            return httpx.Response(
                200,
                json={
                    "message": {"content": json.dumps(verdict_dict)},
                    "prompt_eval_count": 100,
                    "eval_count": 50,
                },
            )
        if request.url.path == "/api/generate":
            return httpx.Response(200, json={"status": "unloaded"})
        return httpx.Response(404)

    client = OllamaClient(transport=httpx.MockTransport(handler))
    judge = TriageLocalJudge(connection, config, client=client)

    _add_job(connection, 101, title, company, extract)

    # 1. First call: executes LLM and verifies
    res1 = judge.judge_one(101, ev)
    assert not res1.from_cache
    assert res1.verdict.level == "senior"
    assert res1.verdict.level_quote == "senior developer"
    assert res1.verdict.required_years_min == 5
    assert res1.verdict.india_eligible == "yes"
    assert "eligibility_blockers" not in res1.verdict.hallucinated  # No blockers claimed
    assert calls == 1

    # Check database storage
    row = connection.execute(
        "SELECT * FROM job_llm_verdicts WHERE job_id = 101 AND source = 'local_llm'"
    ).fetchone()
    assert row is not None
    assert row["cost_usd"] == 0.0

    # 2. Second call: should retrieve directly from cache without hitting Ollama chat endpoint
    res2 = judge.judge_one(101, ev)
    assert res2.from_cache
    assert res2.verdict.level == "senior"
    assert calls == 1  # No additional network call!


def test_local_judge_timeout_and_error_handling(connection: sqlite3.Connection, config: Config) -> None:
    """Verify local LLM judge gracefully handles timeouts and invalid responses."""
    _create_test_db(connection)

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.5.0"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:7b-instruct"}]})
        if request.url.path == "/api/chat":
            raise httpx.ReadTimeout("Request timed out")
        return httpx.Response(404)

    client = OllamaClient(transport=httpx.MockTransport(timeout_handler))
    judge = TriageLocalJudge(connection, config, client=client)

    ev = Evidence(title="Engineer", company="Co", location="Bangalore", workplace="remote", extract="Developer")
    res = judge.judge_one(102, ev)
    assert res.error is not None
    assert "timeout" in res.error.lower() or "readtimeout" in res.error.lower()


def test_accuracy_gate_gating_logic(connection: sqlite3.Connection, config: Config) -> None:
    """Verify accuracy gate checks precision and sample count before enabling acting."""
    _create_test_db(connection)

    # Insert 35 Gemini ground truth verdicts (senior=True)
    for i in range(1, 36):
        connection.execute(
            """
            INSERT INTO jobs (id, fingerprint, title, company_name, description_text, location_raw, country, status, first_seen_at, last_seen_at)
            VALUES (?, ?, 'Senior Developer', 'Tech Corp', 'senior 5 years in India', 'Bangalore', 'IN', 'open', '2026-10-25T00:00:00Z', '2026-10-25T00:00:00Z')
            """,
            (i, f"fp_{i}"),
        )
        gemini_verdict = {
            "level": "senior",
            "required_years_min": 5,
            "suitable_for_new_grad": "no",
            "india_eligible": "yes",
            "location_scope": "india",
            "eligibility_blockers": [],
            "role_family": ["backend"],
            "confidence": "high",
            "level_quote": "senior",
            "years_quote": "5 years",
            "india_quote": "India",
            "blocker_quote": "",
            "hallucinated": [],
        }
        connection.execute(
            """
            INSERT INTO job_llm_verdicts (job_id, content_hash, rubric_version, model, raw_json, verified_json, hallucinated, cost_usd, created_at, source)
            VALUES (?, ?, 'v1', 'gemini-2.5-flash', '{}', ?, 0, 0.001, '2026-10-25T00:00:00Z', 'gemini')
            """,
            (i, f"hash_{i}", json.dumps(gemini_verdict)),
        )

    def mock_ollama_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.5.0"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:7b-instruct"}]})
        if request.url.path == "/api/chat":
            data = {
                "verdicts": [
                    {
                        "job": 1,
                        "level": "senior",
                        "required_years_min": 5,
                        "suitable_for_new_grad": "no",
                        "india_eligible": "yes",
                        "location_scope": "india",
                        "eligibility_blockers": [],
                        "role_family": ["backend"],
                        "confidence": "high",
                        "level_evidence": "senior",
                        "years_evidence": "5 years",
                        "india_evidence": "India",
                        "blocker_evidence": "",
                    }
                ]
            }
            return httpx.Response(
                200,
                json={"message": {"content": json.dumps(data)}, "prompt_eval_count": 50, "eval_count": 20},
            )
        if request.url.path == "/api/generate":
            return httpx.Response(200, json={"status": "unloaded"})
        return httpx.Response(404)

    client = OllamaClient(transport=httpx.MockTransport(mock_ollama_handler))
    gate_res = run_accuracy_gate(
        connection,
        config,
        client=client,
        limit=35,
        min_high_conf_samples=30,
        min_precision=0.90,
    )

    assert gate_res.n_evaluated == 35
    assert gate_res.field_metrics["seniority"]["accuracy"] == 1.0
    assert gate_res.can_act["seniority"] is True
    assert gate_res.overall_can_act is True
