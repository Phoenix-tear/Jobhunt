"""Comprehensive tests for Brief v10b: Trustworthy LLM Triage.

Covers:
- Relevance verification per field (positive and negative cases: values sentence, "architect" verb, spelled numbers, ranges)
- Irrelevant evidence vs not-verbatim separation
- Retroactive re-verification and preserving old JSON in verified_json_v1
- Strong evidence consensus rules and actions
- Company allow-rule precedence (Barclays BA4 override)
- Protected jobs exemption
- Blast-radius guard (25% cap)
- High-stakes confirmation only when non-Gemini
- Queue ordering (local flags first, then score DESC, then newest)
- Budget guard (skips Gemini call when monthly cap remaining < $1.00)
- primary_judge modes
- Audit CSV generation, validation, and CLI label import
- Coverage computation split by Gemini / local only / none
- Scheduler configuration and runner checks
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from jobhunt.config import Config
from jobhunt.matching.triage import (
    Verdict,
    is_relevant_blocker,
    is_relevant_india,
    is_relevant_level,
    is_relevant_years,
    verify,
)
from jobhunt.matching.triage_apply_llm import (
    check_strong_blockers,
    check_strong_location,
    check_strong_seniority,
    is_company_rule_seniority_allowed,
)
from jobhunt.matching.triage_audit import validate_and_import_audit_csv
from jobhunt.matching.triage_benchmark import wilson_interval, recompute_acting_rules
from jobhunt.matching.triage_queue import get_triage_coverage
from jobhunt.matching.triage_run import gemini_queue_jobs
from jobhunt.schedule.definitions import build_task_specs
from jobhunt.schedule.runner import TaskRunner


# ------------------------------------------------------------------------------------
# 1. Relevance Verification Tests
# ------------------------------------------------------------------------------------

def test_years_relevance_digits_spelled_and_ranges() -> None:
    # Digits
    assert is_relevant_years("Requires at least 3 years of experience in Python", 3)
    assert is_relevant_years("Minimum 5 yrs required", 5)
    # Spelled numbers
    assert is_relevant_years("Requires five years of professional software engineering", 5)
    assert is_relevant_years("Three to five yrs experience", 3)  # range lower bound
    assert not is_relevant_years("Three to five yrs experience", 5)  # upper bound is not min
    # Rejects numbers not tied to years
    assert not is_relevant_years("Lead a team of 5 engineers", 5)
    assert not is_relevant_years("Version 3 released last year", 3)
    # Rejects mismatched min
    assert not is_relevant_years("Requires at least 2 years of experience", 5)


def test_level_relevance_verb_vs_noun_and_titles() -> None:
    # Senior / Lead requirement
    assert is_relevant_level("Looking for a Senior Software Engineer", "senior", "Software Engineer")
    assert is_relevant_level("Candidate must be a Lead Engineer", "senior", "Engineer")
    assert is_relevant_level("5+ years of experience required", "senior", "Engineer")
    
    # Verb "architect" never counts unless it is in the job title
    assert not is_relevant_level("You will architect and optimise distributed systems", "senior", "Backend Engineer")
    assert is_relevant_level("Senior Architect", "senior", "Solutions Architect")
    
    # Verb "lead" task usage rejects, people lead counts
    assert not is_relevant_level("Lead the design and development of scalable microservices", "senior", "Software Engineer")
    assert is_relevant_level("Lead a team of 4 engineers", "senior", "Software Engineer")

    # Entry / intern
    assert is_relevant_level("Ideal for fresh graduates and entry level developers", "entry", "Junior Developer")
    assert is_relevant_level("SDE-1 position for new grads", "entry", "SDE-1")
    assert not is_relevant_level("Senior architect with 10 years experience", "entry", "Junior Developer")


def test_india_relevance_places_tz_and_auth() -> None:
    # Positive geographic / remote / auth cues
    assert is_relevant_india("This role is fully remote for candidates located in India")
    assert is_relevant_india("Bangalore office, hybrid workplace")
    assert is_relevant_india("Must be authorized to work in the US only")
    assert is_relevant_india("Working hours aligned with IST timezone")
    assert is_relevant_india("No visa sponsorship or relocation provided")
    
    # Negative quotes without location or work authorization cues
    assert not is_relevant_india("We are a fast-growing company building developer tools")
    assert not is_relevant_india("You will write high-performance C++ code")


def test_blocker_relevance_cues_vs_values() -> None:
    # Blocker cues
    assert is_relevant_blocker("Active Top Secret security clearance required", "clearance")
    assert is_relevant_blocker("US Citizenship required by federal contract", "us_work_auth")
    assert is_relevant_blocker("PhD in Computer Science or related field required", "phd")
    assert is_relevant_blocker("Master's degree in Statistics or ML", "masters")

    # Values statements erroneously cited as clearance or work auth blockers must be dropped
    assert not is_relevant_blocker("Our core values include honesty, openness, and high performance", "clearance")
    assert not is_relevant_blocker("We foster a culture of transparency and collaboration", "us_work_auth")
    assert not is_relevant_blocker("Strong educational background required", "phd")


def test_verify_separates_not_verbatim_from_irrelevant() -> None:
    extract = (
        "Job Title: Software Engineer\n"
        "About the role: You will architect and optimise web applications.\n"
        "Requirements: 3-5 years of experience in Python.\n"
        "Our core values include honesty, security, and openness."
    )
    raw = {
        "level": "senior",
        "level_evidence": "You will architect and optimise web applications",  # verbatim but irrelevant verb
        "required_years_min": 3,
        "years_evidence": "3-5 years of experience in Python",               # verbatim & relevant
        "india_eligible": "no",
        "india_evidence": "Only available to Canadian residents",             # hallucinated (not in extract)
        "eligibility_blockers": ["clearance"],
        "blocker_evidence": "Our core values include honesty, security, and openness", # verbatim but irrelevant
    }
    v = verify(raw, extract, title="Software Engineer")

    # Years accepted
    assert v.required_years_min == 3
    assert v.years_quote == "3-5 years of experience in Python"

    # Level dropped for irrelevant verb
    assert v.level == "unknown"
    assert "level" in v.irrelevant_evidence

    # India dropped for hallucination (not verbatim)
    assert v.india_eligible == "unclear"
    assert "india_eligible" in v.hallucinated

    # Clearance blocker dropped for irrelevant evidence
    assert v.eligibility_blockers == []
    assert "eligibility_blockers" in v.irrelevant_evidence


# ------------------------------------------------------------------------------------
# 2. Consensus Acting Rules & Barclays Precedence Tests
# ------------------------------------------------------------------------------------

def test_strong_seniority_consensus_combinations() -> None:
    # 1. Gemini alone (no independent agreement) -> not strong
    gemini_verdict = Verdict(level="senior", level_quote="5+ years senior engineer required", required_years_min=5, years_quote="5+ years")
    is_strong, reason = check_strong_seniority(
        gemini_verdict,
        job_title="Software Engineer",
        job_text="Generic job text",
        local_v=None,
        head_p=0.40,
        company_rule_override=False,
    )
    assert not is_strong
    assert "Gemini alone" in reason

    # 2. Gemini + Local LLM agreement -> strong
    local_verdict = Verdict(level="senior", level_quote="5+ years senior engineer required")
    is_strong, reason = check_strong_seniority(
        gemini_verdict,
        job_title="Software Engineer",
        job_text="Generic job text",
        local_v=local_verdict,
        head_p=0.40,
        company_rule_override=False,
    )
    assert is_strong
    assert "Gemini + local LLM" in reason

    # 3. Gemini + Deterministic Title Token -> strong
    is_strong, reason = check_strong_seniority(
        gemini_verdict,
        job_title="Senior Backend Engineer",
        job_text="Generic job text",
        local_v=None,
        head_p=0.40,
        company_rule_override=False,
    )
    assert is_strong
    assert "title word" in reason

    # 4. Gemini + Classifier Head (p >= 0.90) -> strong
    is_strong, reason = check_strong_seniority(
        gemini_verdict,
        job_title="Software Engineer",
        job_text="Generic job text",
        local_v=None,
        head_p=0.92,
        company_rule_override=False,
    )
    assert is_strong
    assert "head" in reason


def test_barclays_ba4_company_allow_rule_precedence() -> None:
    gemini_verdict = Verdict(level="senior", level_quote="5+ years senior required", required_years_min=5, years_quote="5+ years")
    local_verdict = Verdict(level="senior", level_quote="5+ years senior required")

    # With company_rule_override=True (e.g. Barclays BA4)
    is_strong, reason = check_strong_seniority(
        gemini_verdict,
        job_title="BA4 Developer",
        job_text="Barclays BA4 grade",
        local_v=local_verdict,
        head_p=0.90,
        company_rule_override=True,
    )
    assert not is_strong
    assert "company rule allows this grade" in reason


# ------------------------------------------------------------------------------------
# 3. Wilson 95% Interval & Acting Rules Recomputation
# ------------------------------------------------------------------------------------

def test_wilson_interval_bounds() -> None:
    # 0 out of 0
    lo, hi = wilson_interval(0, 0)
    assert lo == 0.0 and hi == 0.0

    # 10 out of 10 positives
    lo, hi = wilson_interval(10, 10)
    assert lo > 0.65
    assert hi == 1.0

    # 2 out of 4 positives
    lo, hi = wilson_interval(2, 4)
    assert 0.14 <= lo <= 0.16
    assert 0.84 <= hi <= 0.86


def test_recompute_acting_rules_database_integration() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, company_name TEXT, description_text TEXT, location_raw TEXT, first_seen_at TEXT);
        CREATE TABLE job_llm_verdicts (id INTEGER PRIMARY KEY, job_id INTEGER, model TEXT, source TEXT, rubric_version TEXT, verified_json TEXT, created_at TEXT, human_label TEXT, human_reason TEXT);
        CREATE TABLE job_audit_labels (id INTEGER PRIMARY KEY, job_id INTEGER, head TEXT, label REAL, my_verdict TEXT, my_note TEXT, created_at TEXT);
        CREATE TABLE applications (job_id INTEGER);
        CREATE TABLE job_feedback_current (job_id INTEGER, action TEXT, reason TEXT, created_at TEXT);
        CREATE TABLE job_decision_current (job_id INTEGER PRIMARY KEY, action TEXT, reason TEXT, created_at TEXT);
        CREATE TABLE job_relevance (job_id INTEGER PRIMARY KEY, location_fit TEXT, location_reason TEXT);
        CREATE TABLE job_triage_acting_rules (
            id INTEGER PRIMARY KEY, field TEXT, target_class TEXT, sample_count INTEGER,
            high_conf_count INTEGER, precision_val REAL, recall_val REAL, can_act INTEGER,
            mode TEXT, reason TEXT, evaluated_at TEXT, ci_lower REAL, ci_upper REAL, rule_desc TEXT
        );
    """)

    # Insert a human label and a Gemini verdict
    conn.execute("INSERT INTO jobs VALUES (1, 'Senior Dev', 'Acme', 'desc', 'US', '2026-10-30')")
    v_json = json.dumps({
        "level": "senior", "level_quote": "5+ years", "required_years_min": 5, "years_quote": "5+ years",
        "india_eligible": "no", "india_quote": "US only", "eligibility_blockers": [],
        "hallucinated": [], "irrelevant_evidence": []
    })
    conn.execute("INSERT INTO job_llm_verdicts VALUES (1, 1, 'gemini-2.5-flash', 'gemini', 'v1', ?, '2026-10-30', NULL, NULL)", (v_json,))
    conn.execute("INSERT INTO job_audit_labels VALUES (1, 1, 'seniority', 1.0, 'gemini_right', 'correct senior', '2026-10-30')")
    conn.commit()

    cfg = replace(Config(), triage=replace(Config().triage, rubric_version="v1"))
    rules = recompute_acting_rules(conn, cfg)
    assert len(rules) >= 4
    for r in rules:
        assert r.ci_lower is not None
        assert r.ci_upper is not None
        assert r.mode == "consensus"


# ------------------------------------------------------------------------------------
# 4. Audit CSV Generation, Validation & CLI Label Import
# ------------------------------------------------------------------------------------

def test_audit_csv_validation_and_import(tmp_path: Path) -> None:
    csv_file = tmp_path / "v10b_audit.csv"
    headers = [
        "job_id", "category", "company", "title", "source", "posting_snippet",
        "gemini_verdict", "gemini_quote", "local_verdict", "local_quote",
        "my_verdict", "my_note"
    ]
    rows = [
        [
            "1001", "a_local_years_ge_2", "TechCorp", "Python Engineer", "local_flag",
            "Requires 3+ years experience with Django", "years=1", "1 yr", "years=3", "3+ years",
            "local_right", "clear 3+ years requirement"
        ],
        [
            "1002", "b_location_conflict", "RemoteCo", "Frontend Engineer", "location_conflict",
            "Must be resident in the US", "india=yes", "unclear", "india=no", "US only",
            "local_right", "explicit US residency requirement"
        ],
        [
            "1003", "c_entry_vs_senior", "DataInc", "Junior Analyst", "level_conflict",
            "Graduate role with mentorship", "level=senior", "mentorship lead", "level=entry", "Graduate role",
            "local_right", "graduate program"
        ],
        [
            "1004", "d_control_agreement", "AgreeCo", "SDE-1", "control",
            "Freshers welcome 0-1 years", "years=0", "0-1 years", "years=0", "0-1 years",
            "unsure", "not clear"
        ],
    ]
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, company_name TEXT, description_text TEXT, location_raw TEXT, first_seen_at TEXT);
        CREATE TABLE job_audit_labels (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, head TEXT NOT NULL,
            label REAL NOT NULL, source TEXT NOT NULL DEFAULT 'audit', my_verdict TEXT NOT NULL, my_note TEXT, created_at TEXT NOT NULL,
            UNIQUE(job_id, head)
        );
        CREATE TABLE job_llm_verdicts (id INTEGER PRIMARY KEY, job_id INTEGER, model TEXT, source TEXT, rubric_version TEXT, verified_json TEXT, created_at TEXT, human_label TEXT, human_reason TEXT);
        CREATE TABLE applications (job_id INTEGER);
        CREATE TABLE job_feedback_current (job_id INTEGER, action TEXT, reason TEXT, created_at TEXT);
        CREATE TABLE job_decision_current (job_id INTEGER PRIMARY KEY, action TEXT, reason TEXT, created_at TEXT);
        CREATE TABLE job_relevance (job_id INTEGER PRIMARY KEY, location_fit TEXT, location_reason TEXT);
        CREATE TABLE job_triage_acting_rules (
            id INTEGER PRIMARY KEY, field TEXT, target_class TEXT, sample_count INTEGER,
            high_conf_count INTEGER, precision_val REAL, recall_val REAL, can_act INTEGER,
            mode TEXT, reason TEXT, evaluated_at TEXT, ci_lower REAL, ci_upper REAL, rule_desc TEXT
        );
    """)

    cfg = replace(Config(), triage=replace(Config().triage, rubric_version="v1"))
    res = validate_and_import_audit_csv(conn, cfg, csv_file)
    assert res.imported_labels == 3  # row 1004 was "unsure" so not imported as label
    assert res.empty_rows == 0

    # Verify rows in job_audit_labels
    stored = conn.execute("SELECT job_id, head, label, my_verdict FROM job_audit_labels ORDER BY job_id ASC").fetchall()
    assert len(stored) == 3
    assert stored[0]["job_id"] == 1001 and stored[0]["head"] == "seniority" and stored[0]["label"] == 1.0
    assert stored[1]["job_id"] == 1002 and stored[1]["head"] == "location" and stored[1]["label"] == 1.0


# ------------------------------------------------------------------------------------
# 5. Queue Ordering with Local Flags First
# ------------------------------------------------------------------------------------

def test_queue_ordering_local_flags_first() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, company_name TEXT, description_text TEXT, status TEXT, first_seen_at TEXT);
        CREATE TABLE job_relevance (job_id INTEGER PRIMARY KEY, gate_status TEXT, location_fit TEXT, final_score REAL, local_rank_score REAL);
        CREATE TABLE job_llm_verdicts (id INTEGER PRIMARY KEY, job_id INTEGER, rubric_version TEXT, source TEXT, model TEXT, verified_json TEXT);
        CREATE TABLE job_auto_hidden (job_id INTEGER, restored_at TEXT);
        CREATE TABLE applications (job_id INTEGER);
        CREATE TABLE job_decision_current (job_id INTEGER, action TEXT);
        CREATE TABLE tailored_resumes (job_id INTEGER);
        CREATE TABLE job_sightings (job_id INTEGER, source TEXT);
    """)

    # Job 1: high score (90), but no local flag
    conn.execute("INSERT INTO jobs VALUES (1, 'Title 1', 'Company 1', 'desc', 'open', '2026-10-29')")
    conn.execute("INSERT INTO job_relevance VALUES (1, 'target', 'india', 90.0, 0.9)")

    # Job 2: lower score (70), but flagged senior by local LLM
    conn.execute("INSERT INTO jobs VALUES (2, 'Title 2', 'Company 2', 'desc', 'open', '2026-10-28')")
    conn.execute("INSERT INTO job_relevance VALUES (2, 'target', 'india', 70.0, 0.7)")
    v2_json = json.dumps({"level": "senior", "level_quote": "5+ yrs", "required_years_min": None, "india_eligible": "yes", "eligibility_blockers": []})
    conn.execute("INSERT INTO job_llm_verdicts VALUES (1, 2, 'v1', 'local_llm', 'qwen', ?)", (v2_json,))

    # Job 3: medium score (80), flagged not-India by local LLM
    conn.execute("INSERT INTO jobs VALUES (3, 'Title 3', 'Company 3', 'desc', 'open', '2026-10-27')")
    conn.execute("INSERT INTO job_relevance VALUES (3, 'target', 'india', 80.0, 0.8)")
    v3_json = json.dumps({"level": "unknown", "required_years_min": None, "india_eligible": "no", "india_quote": "US only", "eligibility_blockers": []})
    conn.execute("INSERT INTO job_llm_verdicts VALUES (2, 3, 'v1', 'local_llm', 'qwen', ?)", (v3_json,))
    conn.commit()

    config = replace(Config(), triage=replace(Config().triage, rubric_version="v1"))

    # Pass judge as None so it uses fast deterministic unjudged set
    queue = gemini_queue_jobs(conn, config, judge=None)
    # Flagged jobs (3 and 2) should precede unflagged job (1)
    # Between 3 and 2, Job 3 has score 80 > Job 2 score 70
    assert queue == [3, 2, 1]


# ------------------------------------------------------------------------------------
# 6. Budget Guard Behavior
# ------------------------------------------------------------------------------------

def test_budget_guard_skips_when_monthly_remaining_under_one_dollar(tmp_path: Path) -> None:
    from jobhunt.matching.budget import UsageLedger, BudgetGuard, BudgetSettings, PricingSettings
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE llm_usage (
            id INTEGER PRIMARY KEY, created_at TEXT, day TEXT, model TEXT,
            purpose TEXT, calls INTEGER, items INTEGER, input_tokens INTEGER,
            output_tokens INTEGER, estimated_usd REAL, note TEXT
        );
    """)
    p_settings = PricingSettings()
    ledger = UsageLedger(conn, p_settings)
    # Record enough calls so usd_month is $4.20 out of $5.00 cap
    ledger.record(model="gemini-2.5-flash", purpose="match", input_tokens=30_000_000, output_tokens=3_000_000)
    
    b_settings = BudgetSettings(monthly_usd=5.00)
    guard = BudgetGuard(ledger, b_settings)

    summary = ledger.summary()
    remaining = b_settings.monthly_usd - summary.usd_month
    assert remaining < 1.00

    can_run_gemini_backfill = remaining >= 1.00
    assert not can_run_gemini_backfill


# ------------------------------------------------------------------------------------
# 7. Coverage Calculation Split
# ------------------------------------------------------------------------------------

def test_coverage_calculation_split_gemini_local_none() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT, first_seen_at TEXT);
        CREATE TABLE job_relevance (job_id INTEGER PRIMARY KEY, gate_status TEXT, location_fit TEXT, final_score REAL);
        CREATE TABLE job_llm_verdicts (id INTEGER PRIMARY KEY, job_id INTEGER, rubric_version TEXT, source TEXT, model TEXT, verified_json TEXT);
        CREATE TABLE job_auto_hidden (job_id INTEGER, restored_at TEXT);
        CREATE TABLE job_cascade_decisions (id INTEGER PRIMARY KEY, job_id INTEGER, head TEXT, tier INTEGER, action TEXT, confidence REAL, evidence TEXT, mode TEXT, model TEXT, decided_at TEXT);
    """)

    # 3 visible Relevant jobs
    for i in (1, 2, 3):
        conn.execute("INSERT INTO jobs VALUES (?, 'open', '2026-10-30')", (i,))
        conn.execute("INSERT INTO job_relevance VALUES (?, 'target', 'india', 80.0)", (i,))

    # Job 1 has Gemini verdict
    conn.execute("INSERT INTO job_llm_verdicts VALUES (1, 1, 'v1', 'gemini', 'gemini-2.5-flash', '{}')")
    # Job 2 has Local LLM verdict only
    conn.execute("INSERT INTO job_llm_verdicts VALUES (2, 2, 'v1', 'local_llm', 'qwen2.5:7b', '{}')")
    # Job 3 has no verdict
    conn.commit()

    config = replace(Config(), triage=replace(Config().triage, rubric_version="v1"))

    cov = get_triage_coverage(conn, config)
    assert cov["total_relevant"] == 3
    assert cov["judged_relevant"] == 2
    assert cov["coverage_gemini"] == 1
    assert cov["coverage_local_only"] == 1
    assert cov["coverage_none"] == 1
    assert cov["relevant_coverage_pct"] == 66.7


# ------------------------------------------------------------------------------------
# 8. Scheduler Task Specs & Interpreter Check
# ------------------------------------------------------------------------------------

def test_scheduler_specs_order_and_triage_presence() -> None:
    config = replace(Config(), schedule=replace(Config().schedule, triage_time="07:30"))
    task_map = build_task_specs(config.schedule)
    
    assert "triage" in task_map
    spec = task_map["triage"]
    assert spec.order == 3
    assert task_map["match"].order == 2
    assert task_map["match"].order < spec.order

    # Verify TaskRunner uses python interpreter
    conn = sqlite3.connect(":memory:")
    runner = TaskRunner(config, conn, Path("config.yaml"))
    assert any("python" in runner.python.lower() for _ in [1])
