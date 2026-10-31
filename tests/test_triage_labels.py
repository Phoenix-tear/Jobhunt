"""Tests for unified triage labels (Brief v9)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from jobhunt.config import Config, load_config
from jobhunt.matching.triage import Verdict
from jobhunt.matching.triage_labels import (
    WEIGHT_GEMINI,
    WEIGHT_HUMAN,
    WEIGHT_LOCAL_LLM,
    WEIGHT_WEAK,
    UnifiedLabel,
    build_unified_labels,
    get_eval_dataset,
    measure_weak_rule_precision,
    parse_weak_non_india,
    parse_weak_senior_title,
    parse_weak_years_ge_2,
)


def _add_job(conn: sqlite3.Connection, job_id: int, title: str, company: str, desc: str = "", loc: str = "India") -> int:
    conn.execute(
        "INSERT INTO jobs (id, fingerprint, title, company_name, description_text, location_raw, country, status, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'IN', 'open', '2026-10-25T00:00:00Z', '2026-10-25T00:00:00Z')",
        (job_id, f"fp{job_id}", title, company, desc, loc),
    )
    conn.execute(
        "INSERT INTO job_relevance (job_id, gate_status, gate_rule, gate_reason, rules_hash, input_hash, computed_at) "
        "VALUES (?, 'target', 'title_ai_ml', 'target title', 'h1', 'h2', '2026-10-25T00:00:00Z')",
        (job_id,),
    )
    return job_id


def test_human_applied_and_dismissed_labels(connection: sqlite3.Connection, config: Config) -> None:
    # 1. Applied job
    j1 = _add_job(connection, 1, "Junior Python Developer", "Acme Corp", "0-1 years required")
    connection.execute("INSERT INTO applications (job_id, stage, created_at, updated_at) VALUES (?, 'applied', '2026-10-25T00:00:00Z', '2026-10-25T00:00:00Z')", (j1,))

    # 2. Too senior dismissal
    j2 = _add_job(connection, 2, "Senior Lead Engineer", "BigCo", "10+ years required")
    connection.execute(
        "INSERT INTO job_decisions (job_id, action, reason, happened_at, created_at) VALUES (?, 'not_applying', 'too_senior', '2026-10-25T00:00:00Z', '2026-10-25T00:00:00Z')",
        (j2,),
    )

    # 3. Wrong location dismissal
    j3 = _add_job(connection, 3, "ML Engineer", "GlobalCo", "Must reside in London")
    connection.execute(
        "INSERT INTO job_decisions (job_id, action, reason, happened_at, created_at) VALUES (?, 'not_applying', 'wrong_location', '2026-10-25T00:00:00Z', '2026-10-25T00:00:00Z')",
        (j3,),
    )

    # 4. Ineligible masters
    j4 = _add_job(connection, 4, "Research Scientist", "ResearchLab", "Must have PhD")
    connection.execute(
        "INSERT INTO job_decisions (job_id, action, reason, happened_at, created_at) VALUES (?, 'not_applying', 'ineligible_phd_or_masters', '2026-10-25T00:00:00Z', '2026-10-25T00:00:00Z')",
        (j4,),
    )

    # 5. Unrelated dismissal (closed/too_old)
    j5 = _add_job(connection, 5, "Software Engineer", "OldCo", "Python.")
    connection.execute(
        "INSERT INTO job_decisions (job_id, action, reason, happened_at, created_at) VALUES (?, 'not_applying', 'closed_or_unavailable', '2026-10-25T00:00:00Z', '2026-10-25T00:00:00Z')",
        (j5,),
    )

    labels = build_unified_labels(connection, config, include_weak=False)

    # j1 should be negative for seniority, location, eligibility with weight 5.0
    sen_j1 = next((u for u in labels["seniority"] if u.job_id == j1), None)
    assert sen_j1 is not None and sen_j1.label == 0.0 and sen_j1.weight == WEIGHT_HUMAN and sen_j1.eval_usable

    loc_j1 = next((u for u in labels["location"] if u.job_id == j1), None)
    assert loc_j1 is not None and loc_j1.label == 0.0 and loc_j1.weight == WEIGHT_HUMAN and loc_j1.eval_usable

    # j2 should be positive for seniority
    sen_j2 = next((u for u in labels["seniority"] if u.job_id == j2), None)
    assert sen_j2 is not None and sen_j2.label == 1.0 and sen_j2.weight == WEIGHT_HUMAN and sen_j2.eval_usable

    # j3 should be positive for location (not India eligible)
    loc_j3 = next((u for u in labels["location"] if u.job_id == j3), None)
    assert loc_j3 is not None and loc_j3.label == 1.0 and loc_j3.weight == WEIGHT_HUMAN and loc_j3.eval_usable

    # j4 should be positive for eligibility (ineligible)
    el_j4 = next((u for u in labels["eligibility"] if u.job_id == j4), None)
    assert el_j4 is not None and el_j4.label == 1.0 and el_j4.weight == WEIGHT_HUMAN and el_j4.eval_usable

    # j5 should NOT have any label for seniority or location
    assert not any(u.job_id == j5 for u in labels["seniority"])
    assert not any(u.job_id == j5 for u in labels["location"])


def test_verdict_labels_and_corrections(connection: sqlite3.Connection, config: Config) -> None:
    # Add jobs
    j10 = _add_job(connection, 10, "Senior Staff Engineer", "Google", "5+ years experience required")
    j11 = _add_job(connection, 11, "Associate Developer", "Microsoft", "Entry level role, 0 years")
    j12 = _add_job(connection, 12, "Backend Engineer", "Amazon", "5 years experience required")

    # j10 has verified senior verdict
    v10 = Verdict(level="senior", level_quote="Senior Staff Engineer", confidence="high")
    connection.execute(
        "INSERT INTO job_llm_verdicts (job_id, content_hash, rubric_version, model, source, raw_json, verified_json, cost_usd, created_at) "
        "VALUES (?, 'h10', 'v1', 'gemini-3.8-flash', 'gemini', '{}', ?, 0.001, '2026-10-25T00:00:00Z')",
        (j10, json.dumps(v10.as_json())),
    )

    # j11 has verified entry verdict
    v11 = Verdict(level="entry", level_quote="Entry level role", confidence="high")
    connection.execute(
        "INSERT INTO job_llm_verdicts (job_id, content_hash, rubric_version, model, source, raw_json, verified_json, cost_usd, created_at) "
        "VALUES (?, 'h11', 'v1', 'gemini-3.8-flash', 'gemini', '{}', ?, 0.001, '2026-10-25T00:00:00Z')",
        (j11, json.dumps(v11.as_json())),
    )

    # j12 has senior verdict but user marked human_label = 'wrong_years'
    v12 = Verdict(level="senior", required_years_min=5, years_quote="5 years experience", confidence="high")
    connection.execute(
        "INSERT INTO job_llm_verdicts (job_id, content_hash, rubric_version, model, source, raw_json, verified_json, cost_usd, created_at, human_label, human_reason) "
        "VALUES (?, 'h12', 'v1', 'gemini-3.8-flash', 'gemini', '{}', ?, 0.001, '2026-10-25T00:00:00Z', 'wrong_years', 'actually entry level')",
        (j12, json.dumps(v12.as_json())),
    )

    labels = build_unified_labels(connection, config, include_weak=False)

    # j10 is senior (weight 2.0, gemini)
    sen_j10 = next(u for u in labels["seniority"] if u.job_id == j10)
    assert sen_j10.label == 1.0 and sen_j10.weight == WEIGHT_GEMINI and sen_j10.source == "gemini_verdict"

    # j11 is not senior (0.0)
    sen_j11 = next(u for u in labels["seniority"] if u.job_id == j11)
    assert sen_j11.label == 0.0 and sen_j11.weight == WEIGHT_GEMINI and sen_j11.source == "gemini_verdict"

    # j12 is flipped to not senior (0.0) with weight 5.0 and source human_correction
    sen_j12 = next(u for u in labels["seniority"] if u.job_id == j12)
    assert sen_j12.label == 0.0 and sen_j12.weight == WEIGHT_HUMAN and sen_j12.source == "human_correction"


def test_eval_dataset_filters_out_weak_labels(connection: sqlite3.Connection, config: Config) -> None:
    j20 = _add_job(connection, 20, "Senior Architect", "Oracle", "10 years")
    labels = [
        UnifiedLabel(job_id=20, head="seniority", label=1.0, weight=5.0, source="human_decision", confidence=1.0, eval_usable=True),
        UnifiedLabel(job_id=21, head="seniority", label=1.0, weight=2.0, source="gemini_verdict", confidence=0.9, eval_usable=True),
        UnifiedLabel(job_id=22, head="seniority", label=1.0, weight=0.3, source="weak_rule", confidence=0.8, eval_usable=False),
        UnifiedLabel(job_id=23, head="seniority", label=1.0, weight=1.0, source="local_llm_verdict", confidence=0.8, eval_usable=False),
    ]
    eval_set = get_eval_dataset(labels)
    assert len(eval_set) == 2
    assert {u.job_id for u in eval_set} == {20, 21}
    assert all(u.eval_usable for u in eval_set)


def test_weak_parser_functions() -> None:
    assert parse_weak_senior_title("Senior Full Stack Developer")
    assert parse_weak_senior_title("Staff Machine Learning Engineer")
    assert not parse_weak_senior_title("Junior Python Engineer")
    assert not parse_weak_senior_title("Software Engineer Intern")

    assert parse_weak_years_ge_2("Minimum 3-5 years of experience in Python.")
    assert not parse_weak_years_ge_2("0-1 years of experience or fresh graduate.")

    assert parse_weak_non_india("This position is open to US citizens only.")
    assert parse_weak_non_india("Must reside in the US.")
    assert not parse_weak_non_india("Remote position open worldwide including India.")
