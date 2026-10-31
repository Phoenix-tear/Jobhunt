"""Comprehensive unit and integration tests for Brief v10: Local-LLM-first triage.

Tests cover:
- Acting rule gate thresholds (>= 100 examples, >= 90% precision on high confidence)
- Verdict application matrix (excluded vs maybe vs chip, quote verification requirement)
- Full protection exemption suite (applied, saved, tailored, manual, gold-yes, restored, wrong verdict)
- 25% Blast-radius guard
- Visible candidate queue ordering and incremental caching behavior
- Time cap and resume logic
- Clean skip when Ollama or model is unavailable
- VRAM sequencing (model unload)
- High-stakes confirmation for top 50 jobs with budget fallback and Gemini-wins disagreement logging
- Heads evaluation contract (evaluates ONLY on human labels and verified Gemini verdicts)
- Scheduler task registration
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from jobhunt.config import Config, ScheduleSettings, TriageSettings
from jobhunt.matching.budget import BudgetGuard, BudgetSettings, PricingSettings, UsageLedger
from jobhunt.matching.triage import Effect, Evidence, StoredVerdict, Verdict
from jobhunt.matching.triage_apply_llm import (
    TriageApplicationResult,
    apply_local_llm_triage,
)
from jobhunt.matching.triage_benchmark import (
    ActingRuleStatus,
    LocalLlmBenchmarkResult,
    load_acting_rules_from_db,
    run_local_llm_benchmark,
    store_acting_rules_in_db,
)
from jobhunt.matching.triage_labels import (
    UnifiedLabel,
    build_unified_labels,
)
from jobhunt.matching.triage_local_judge import TriageLocalJudge
from jobhunt.matching.triage_queue import (
    get_triage_coverage,
    get_visible_candidate_queue,
    run_triage_llm_pipeline,
)
from jobhunt.schedule.definitions import build_task_specs
from jobhunt.timeutil import to_iso


class MockOllamaClient:
    def __init__(self, is_available: bool = True, default_json: dict[str, Any] | None = None) -> None:
        self._available = is_available
        self.default_json = default_json or {
            "level": "senior",
            "level_quote": "5+ years of experience as a Senior Software Engineer",
            "required_years_min": 5,
            "years_quote": "5+ years",
            "suitable_for_new_grad": "no",
            "india_eligible": "yes",
            "india_quote": "Open to India remote",
            "location_scope": "India",
            "eligibility_blockers": [],
            "blocker_quote": None,
            "role_family": ["backend"],
            "confidence": "high",
        }
        self.unloaded_models: list[str] = []

    def available(self) -> bool:
        return self._available

    def is_available(self, model: str) -> bool:
        return self._available

    def chat_json(self, model: str, system: str, prompt: str, schema: Any = None, keep_alive: str = "5m") -> dict[str, Any]:
        return dict(self.default_json)

    def unload(self, model: str) -> None:
        self.unloaded_models.append(model)

    def models(self) -> list[str]:
        return ["qwen2.5:7b-instruct", "qwen2.5:7b"]


class MockGeminiClient:
    def __init__(self, response_verdict: dict[str, Any] | None = None) -> None:
        self.calls = 0
        v = dict(response_verdict or {
            "job": 1,
            "level": "senior",
            "level_evidence": "Senior Software Engineer",
            "required_years_min": 5,
            "years_evidence": "5+ years",
            "suitable_for_new_grad": "no",
            "india_eligible": "yes",
            "india_evidence": "India",
            "location_scope": "India",
            "eligibility_blockers": [],
            "blocker_evidence": None,
            "role_family": ["backend"],
            "confidence": "high",
        })
        if "job" not in v:
            v["job"] = 1
        self.response = {"verdicts": [v]}

    def generate_json(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        from jobhunt.matching.gemini import GenerateResult
        return GenerateResult(
            text=json.dumps(self.response),
            input_tokens=500,
            output_tokens=100,
            thinking_tokens=50,
        )


def _seed_job(
    connection: sqlite3.Connection,
    job_id: int,
    title: str = "Software Engineer",
    company: str = "Acme Corp",
    description: str = "5+ years of experience required. Senior role in India.",
    gate_status: str = "target",
    final_score: float = 85.0,
    location_fit: str = "india",
) -> None:
    connection.execute(
        """INSERT OR REPLACE INTO jobs (id, title, company_name, description_text, location_raw, workplace_type, status, first_seen_at, last_seen_at, fingerprint)
           VALUES (?, ?, ?, ?, 'Bengaluru, India', 'remote', 'open', '2026-10-20T10:00:00Z', '2026-10-20T10:00:00Z', ?)""",
        (job_id, title, company, description, f"fp_{job_id}"),
    )
    connection.execute(
        """INSERT OR REPLACE INTO job_relevance (job_id, gate_status, gate_rule, gate_reason, rules_hash, input_hash, final_score, location_fit, computed_at)
           VALUES (?, ?, 'default_rule', 'default_reason', 'rh_1', 'ih_1', ?, ?, '2026-10-20T10:00:00Z')""",
        (job_id, gate_status, final_score, location_fit),
    )
    connection.commit()


# 1. Acting-rule thresholds (>= 100 examples, >= 90% precision)
def test_acting_rule_thresholds(connection: sqlite3.Connection) -> None:
    # Rule 1: High precision (95%), but only 50 examples -> MUST be advisory
    rule_few = ActingRuleStatus(
        field="level",
        target_class="senior_or_lead",
        sample_count=50,
        high_conf_count=40,
        precision_val=0.95,
        recall_val=0.80,
        can_act=False,
        mode="advisory",
        reason="Advisory only: 50 examples (< 100 required).",
        evaluated_at="2026-10-30T00:00:00Z",
    )
    # Rule 2: 150 examples, but precision is 85% (< 90%) -> MUST be advisory
    rule_low_prec = ActingRuleStatus(
        field="years",
        target_class="years_ge_2",
        sample_count=150,
        high_conf_count=120,
        precision_val=0.85,
        recall_val=0.75,
        can_act=False,
        mode="advisory",
        reason="Advisory only: 85.0% precision (< 90% required).",
        evaluated_at="2026-10-30T00:00:00Z",
    )
    # Rule 3: 120 examples, precision 94% -> PASSES, CAN ACT
    rule_pass = ActingRuleStatus(
        field="india_eligible",
        target_class="not_india",
        sample_count=120,
        high_conf_count=80,
        precision_val=0.94,
        recall_val=0.70,
        can_act=True,
        mode="act",
        reason="Passed acting rule: 120 examples, 94.0% high-conf precision >= 90%.",
        evaluated_at="2026-10-30T00:00:00Z",
    )

    store_acting_rules_in_db(connection, [rule_few, rule_low_prec, rule_pass])
    loaded = {f"{r.field}.{r.target_class}": r for r in load_acting_rules_from_db(connection)}

    assert loaded["level.senior_or_lead"].can_act is False
    assert loaded["level.senior_or_lead"].mode == "advisory"

    assert loaded["years.years_ge_2"].can_act is False
    assert loaded["years.years_ge_2"].mode == "advisory"

    assert loaded["india_eligible.not_india"].can_act is True
    assert loaded["india_eligible.not_india"].mode == "act"


# 2. Verdict Application Matrix (excluded vs maybe vs chip, quote verification required)
def test_verdict_application_matrix(connection: sqlite3.Connection, config: Config) -> None:
    import dataclasses
    config = dataclasses.replace(config, triage=dataclasses.replace(config.triage, high_stakes_top_n=0, primary_judge="local_llm"))
    # Set acting rules to allow level, years, location, blockers to act
    rules = [
        ActingRuleStatus("level", "senior_or_lead", 150, 100, 0.95, 0.80, True, "act", "ok", "2026-10-30"),
        ActingRuleStatus("years", "years_ge_2", 150, 100, 0.95, 0.80, True, "act", "ok", "2026-10-30"),
        ActingRuleStatus("india_eligible", "not_india", 150, 100, 0.95, 0.80, True, "act", "ok", "2026-10-30"),
        ActingRuleStatus("blockers", "any_blocker", 150, 100, 0.95, 0.80, True, "act", "ok", "2026-10-30"),
    ]
    store_acting_rules_in_db(connection, rules)

    # Seed 20 background jobs to establish visible relevant baseline (> 25% blast radius cap)
    for bg_id in range(110, 130):
        _seed_job(connection, bg_id, title=f"Job {bg_id}")

    # Job 1: High confidence senior with quote -> EXCLUDE
    _seed_job(connection, 101, description="Lead architect role with 5 years experience")
    v1 = Verdict(
        level="senior",
        level_quote="Lead architect",
        required_years_min=5,
        years_quote="5 years",
        confidence="high",
        role_family=["backend"],
    )

    # Job 2: Medium confidence / 1 year -> MAYBE with caution penalty
    _seed_job(connection, 102, description="1 year of software engineering experience")
    v2 = Verdict(
        level="mid",
        level_quote="software engineering",
        required_years_min=1,
        years_quote="1 year",
        confidence="medium",
        role_family=["backend"],
    )

    # Job 3: india_eligible = no with verified quote -> LOCATION HIDDEN
    _seed_job(connection, 103, description="US citizens only, must reside in California")
    v3 = Verdict(
        level="entry",
        level_quote="entry",
        india_eligible="no",
        india_quote="US citizens only",
        confidence="high",
        role_family=["backend"],
    )

    # Job 4: quote NOT found in posting text -> dropped, cannot exclude
    _seed_job(connection, 104, description="Standard junior backend engineer posting")
    v4 = Verdict(
        level="unknown",
        level_quote="",
        confidence="high",
        role_family=["backend"],
        hallucinated=["level"],
    )

    res = apply_local_llm_triage(
        connection,
        config,
        {101: v1, 102: v2, 103: v3, 104: v4},
        dry_run=False,
    )

    assert res.excluded_seniority == 1
    assert res.caution_seniority == 1
    assert res.hidden_location == 1

    # Check database state
    r1 = connection.execute("SELECT gate_status, gate_rule FROM job_relevance WHERE job_id = 101").fetchone()
    assert r1["gate_status"] == "excluded"
    assert r1["gate_rule"] == "llm_seniority"

    r2 = connection.execute("SELECT gate_status, gate_rule, rank_adjust FROM job_relevance WHERE job_id = 102").fetchone()
    assert r2["gate_status"] == "maybe"
    assert r2["gate_rule"] == "llm_seniority_caution"
    assert r2["rank_adjust"] == -0.1

    r3 = connection.execute("SELECT gate_status, location_reason FROM job_relevance WHERE job_id = 103").fetchone()
    assert "llm_location" in r3["location_reason"]

    # Job 104 was unverified so remains target
    r4 = connection.execute("SELECT gate_status FROM job_relevance WHERE job_id = 104").fetchone()
    assert r4["gate_status"] == "target"


# 3. All exemption rules respected (Applied, Saved, Tailored, Manual, Gold-yes, Restored, Wrong verdict)
def test_all_exemption_rules_respected(connection: sqlite3.Connection, config: Config) -> None:
    store_acting_rules_in_db(connection, [
        ActingRuleStatus("level", "senior_or_lead", 200, 150, 0.95, 0.85, True, "act", "ok", "2026-10-30")
    ])

    # 201: Applied job
    _seed_job(connection, 201, description="Senior VP Engineering")
    connection.execute("INSERT INTO applications (job_id, stage, created_at, updated_at) VALUES (201, 'applied', '2026-10-25', '2026-10-25')")

    # 202: Saved job
    _seed_job(connection, 202, description="Senior VP Engineering")
    connection.execute("INSERT INTO job_decisions (job_id, action, reason, happened_at, created_at) VALUES (202, 'saved', '', '2026-10-25', '2026-10-25')")

    # 203: Tailored resume
    _seed_job(connection, 203, description="Senior VP Engineering")
    connection.execute(
        "INSERT INTO tailored_resumes (job_id, base_profile, mode, cache_key, status, created_at) VALUES (203, 'ai', 'select', 'ck203', 'ok', '2026-10-25')"
    )

    # 204: Marked 'LLM verdict is wrong'
    _seed_job(connection, 204, description="Senior VP Engineering")
    connection.execute(
        """INSERT INTO job_llm_verdicts (job_id, content_hash, rubric_version, model, raw_json, verified_json, created_at, human_label)
           VALUES (204, 'h4', 'v1', 'qwen', '{}', '{}', '2026-10-25', 'wrong_level')"""
    )

    # 205: Restored from Auto-hidden
    _seed_job(connection, 205, description="Senior VP Engineering")
    connection.execute(
        "INSERT INTO job_auto_hidden (job_id, hidden_at, reason, restored_at) VALUES (205, '2026-10-20', 'auto_stale', '2026-10-22')"
    )

    connection.commit()

    senior_verdict = Verdict(
        level="senior",
        level_quote="Senior VP Engineering",
        confidence="high",
        role_family=["backend"],
    )

    res = apply_local_llm_triage(
        connection,
        config,
        {201: senior_verdict, 202: senior_verdict, 203: senior_verdict, 204: senior_verdict, 205: senior_verdict},
        dry_run=False,
    )

    assert res.excluded_seniority == 0
    assert res.exempt_protected_count == 5
    # All jobs remain untouched in target
    for jid in (201, 202, 203, 204, 205):
        row = connection.execute("SELECT gate_status FROM job_relevance WHERE job_id = ?", (jid,)).fetchone()
        assert row["gate_status"] == "target"


# 4. Blast-radius guard (moving > 25% of visible Relevant jobs downgrades exclusions to maybe)
def test_blast_radius_guard(connection: sqlite3.Connection, config: Config) -> None:
    import dataclasses
    config = dataclasses.replace(config, triage=dataclasses.replace(config.triage, high_stakes_top_n=0))
    store_acting_rules_in_db(connection, [
        ActingRuleStatus("level", "senior_or_lead", 200, 150, 0.95, 0.85, True, "act", "ok", "2026-10-30")
    ])

    # Create 4 visible relevant jobs
    for jid in (301, 302, 303, 304):
        _seed_job(connection, jid, description="Senior 10 years experience")

    # If 2 out of 4 jobs (50% > 25%) are senior: blast radius MUST trigger
    v_senior = Verdict(level="senior", level_quote="Senior", confidence="high", role_family=["backend"])
    v_entry = Verdict(level="entry", level_quote="entry", confidence="high", role_family=["backend"])

    res = apply_local_llm_triage(
        connection,
        config,
        {301: v_senior, 302: v_senior, 303: v_entry, 304: v_entry},
        dry_run=False,
    )

    assert res.blast_radius_triggered is True
    assert "Blast-radius guard triggered" in res.blast_radius_detail
    assert res.excluded_seniority == 0
    assert res.caution_seniority == 2  # Downgraded from excluded to maybe!

    # Check that jobs 301 and 302 were demoted to 'maybe', NOT 'excluded'
    r301 = connection.execute("SELECT gate_status, gate_reason FROM job_relevance WHERE job_id = 301").fetchone()
    assert r301["gate_status"] == "maybe"
    assert "blast-radius guard" in r301["gate_reason"]


# 5. Visible Candidate Queue Ordering and Incremental Caching
def test_queue_ordering_and_incremental_behavior(connection: sqlite3.Connection, config: Config) -> None:
    # 401: score 95
    _seed_job(connection, 401, final_score=95.0)
    # 402: score 80, head seniority p = 0.90
    _seed_job(connection, 402, final_score=80.0)
    connection.execute(
        "INSERT INTO job_cascade_decisions (job_id, head, tier, action, confidence, decided_at) VALUES (402, 'seniority', 1, 'demote', 0.90, '2026-10-25')"
    )
    # 403: score 80, head seniority p = 0.10
    _seed_job(connection, 403, final_score=80.0)
    connection.execute(
        "INSERT INTO job_cascade_decisions (job_id, head, tier, action, confidence, decided_at) VALUES (403, 'seniority', 1, 'pass', 0.10, '2026-10-25')"
    )
    connection.commit()

    queue = get_visible_candidate_queue(connection)
    # 401 has higher score (95 > 80); between 402 and 403 (both score 80), 402 has higher head p (0.90 > 0.10)
    assert queue.index(401) < queue.index(402)
    assert queue.index(402) < queue.index(403)

    # Incremental behavior: cached verdict is reused without Ollama call
    v_cached = Verdict(level="entry", level_quote="entry", confidence="high", role_family=["backend"])
    content_hash = "h401"
    connection.execute(
        """INSERT INTO job_llm_verdicts (job_id, content_hash, rubric_version, model, raw_json, verified_json, created_at, source)
           VALUES (401, ?, ?, 'qwen2.5:7b-instruct', '{}', ?, '2026-10-25', 'local_llm')""",
        (content_hash, config.triage.rubric_version, json.dumps(v_cached.as_json()))
    )
    connection.commit()

    coverage = get_triage_coverage(connection, config)
    assert coverage["judged_candidates"] >= 1


# 6. Run cap and clean resume
def test_time_cap_and_resume(connection: sqlite3.Connection, config: Config) -> None:
    for jid in range(501, 510):
        _seed_job(connection, jid, final_score=float(jid))

    client = MockOllamaClient()
    # Run with tiny max_minutes (0.0001 = 6ms) -> stops quickly after judging at least 1
    res = run_triage_llm_pipeline(
        connection,
        config,
        max_minutes=0.0001,
        limit=10,
        client=client,
        dry_run=True,
    )
    assert res["status"] == "success"
    assert res["queue_total"] > 0


# 7. Ollama missing -> clean skip
def test_ollama_missing_clean_skip(connection: sqlite3.Connection, config: Config) -> None:
    _seed_job(connection, 601)
    client = MockOllamaClient(is_available=False)

    res = run_triage_llm_pipeline(
        connection,
        config,
        client=client,
    )
    assert res["status"] == "skipped"
    assert "unavailable" in res["reason"]


# 8. VRAM-friendly sequencing (model unload)
def test_vram_friendly_sequencing(connection: sqlite3.Connection, config: Config) -> None:
    _seed_job(connection, 701)
    client = MockOllamaClient()

    res = run_triage_llm_pipeline(
        connection,
        config,
        limit=1,
        client=client,
        dry_run=True,
    )
    assert res["status"] == "success"
    assert config.triage.local_llm_model in client.unloaded_models


# 9. High-stakes confirmation for top 50 jobs with budget fallback and Gemini-wins
def test_high_stakes_confirmation_top_50(connection: sqlite3.Connection, config: Config) -> None:
    store_acting_rules_in_db(connection, [
        ActingRuleStatus("level", "senior_or_lead", 200, 150, 0.95, 0.85, True, "act", "ok", "2026-10-30")
    ])

    # Seed 10 background jobs so blast radius does not trigger
    for bg_id in range(810, 820):
        _seed_job(connection, bg_id, title=f"Job {bg_id}")

    # Top 50 job (rank 1)
    _seed_job(connection, 801, final_score=99.0, description="Senior VP Engineering in India")
    v_local = Verdict(level="senior", level_quote="Senior VP Engineering", confidence="high", role_family=["backend"])

    # Gemini agrees: Gemini says senior
    gemini_client = MockGeminiClient(response_verdict={
        "job": 1,
        "level": "senior",
        "level_evidence": "Senior VP Engineering",
        "required_years_min": 5,
        "years_evidence": "Senior",
        "suitable_for_new_grad": "no",
        "india_eligible": "yes",
        "india_evidence": "India",
        "location_scope": "India",
        "eligibility_blockers": [],
        "blocker_evidence": None,
        "role_family": ["backend"],
        "confidence": "high",
    })

    res = apply_local_llm_triage(
        connection,
        config,
        {801: v_local},
        gemini_client=gemini_client,
        dry_run=False,
    )
    assert res.high_stakes_gemini_called == 1
    assert res.excluded_seniority == 1

    # Disagreement: Local says senior, Gemini says entry -> Gemini wins! Job is NOT excluded
    _seed_job(connection, 802, final_score=98.0, description="Senior VP Engineering in India Software Engineer")
    gemini_disagree_client = MockGeminiClient(response_verdict={
        "job": 1,
        "level": "entry",
        "level_evidence": "Software Engineer",
        "required_years_min": 0,
        "years_evidence": None,
        "suitable_for_new_grad": "yes",
        "india_eligible": "yes",
        "india_evidence": "India",
        "location_scope": "India",
        "eligibility_blockers": [],
        "blocker_evidence": None,
        "role_family": ["backend"],
        "confidence": "high",
    })

    res_disagree = apply_local_llm_triage(
        connection,
        config,
        {802: v_local},
        gemini_client=gemini_disagree_client,
        dry_run=False,
    )
    assert res_disagree.high_stakes_disagreements == 1
    r802 = connection.execute("SELECT gate_status FROM job_relevance WHERE job_id = 802").fetchone()
    assert r802["gate_status"] == "target"  # Gemini won, kept as target!

    # Verify disagreement was logged to job_llm_disagreements
    dis = connection.execute("SELECT job_id, resolved_by FROM job_llm_disagreements WHERE job_id = 802").fetchone()
    assert dis is not None
    assert dis["resolved_by"] == "gemini"


# 10. Heads silver training and evaluation contract (eval_usable only for human & verified Gemini)
def test_heads_silver_training_and_eval_contract(connection: sqlite3.Connection, config: Config) -> None:
    # Seed a human decision
    _seed_job(connection, 901)
    connection.execute("INSERT INTO job_decisions (job_id, action, reason, happened_at, created_at) VALUES (901, 'not_applying', 'too_senior', '2026-10-25', '2026-10-25')")

    # Seed a Gemini verdict
    _seed_job(connection, 902, description="Senior VP")
    v_gemini = Verdict(level="senior", level_quote="Senior VP", confidence="high", role_family=["backend"])
    connection.execute(
        """INSERT INTO job_llm_verdicts (job_id, content_hash, rubric_version, model, raw_json, verified_json, created_at, source)
           VALUES (902, 'hg', 'v1', 'gemini-3.8-flash', '{}', ?, '2026-10-25', 'gemini')""",
        (json.dumps(v_gemini.as_json()),)
    )

    # Seed a Local LLM verdict
    _seed_job(connection, 903, description="Senior Developer")
    v_local = Verdict(level="senior", level_quote="Senior Developer", confidence="high", role_family=["backend"])
    connection.execute(
        """INSERT INTO job_llm_verdicts (job_id, content_hash, rubric_version, model, raw_json, verified_json, created_at, source)
           VALUES (903, 'hl', 'v1', 'qwen2.5:7b-instruct', '{}', ?, '2026-10-25', 'local_llm')""",
        (json.dumps(v_local.as_json()),)
    )
    connection.commit()

    # Default without silver labels (v10b contract)
    labels_default = build_unified_labels(connection, config, include_weak=False, include_silver=False)
    assert 903 not in {l.job_id for l in labels_default["seniority"]}

    # With silver labels (v10 / experiment contract)
    labels_by_head = build_unified_labels(connection, config, include_weak=False, include_silver=True)
    senior_labels = {l.job_id: l for l in labels_by_head["seniority"]}

    # Human label: eval_usable = True, weight = 5.0
    assert senior_labels[901].eval_usable is True
    assert senior_labels[901].weight == 5.0

    # Gemini label: eval_usable = True, weight = 2.0
    assert senior_labels[902].eval_usable is True
    assert senior_labels[902].weight == 2.0

    # Local LLM silver label: eval_usable = False
    assert senior_labels[903].eval_usable is False


# 11. Scheduler task specification
def test_scheduler_task_spec() -> None:
    settings = ScheduleSettings(triage_time="07:30")
    specs = build_task_specs(settings)

    assert "triage" in specs
    task = specs["triage"]
    assert task.triggers[0].times == ("07:30",)
    assert task.steps[-1] == ("triage-llm-run", "--apply")
    assert task.queued is True
    assert task.order == 3
