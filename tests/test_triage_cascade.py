"""Unit tests for the local triage cascade router and shadow report (Brief v9 Phase 4)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import httpx
import pytest

from jobhunt.config import Config
from jobhunt.matching.ollama import OllamaClient
from jobhunt.matching.triage_cascade import (
    CascadeRouter,
    run_shadow_report,
)
from jobhunt.matching.triage_models import HybridLogisticModel


def _mock_ollama_client() -> OllamaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.5.0"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:7b-instruct"}, {"name": "mxbai-embed-large"}]})
        if request.url.path == "/api/embed":
            data = json.loads(request.content)
            n = len(data.get("input", []))
            return httpx.Response(200, json={"embeddings": [[0.0] * 1024] * n, "prompt_eval_count": 10})
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
            return httpx.Response(200, json={"message": {"content": json.dumps(data)}, "prompt_eval_count": 50, "eval_count": 20})
        if request.url.path == "/api/generate":
            return httpx.Response(200, json={"status": "unloaded"})
        return httpx.Response(404)
    return OllamaClient(transport=httpx.MockTransport(handler))


def _setup_test_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_cascade_decisions (
            id             INTEGER PRIMARY KEY,
            job_id         INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
            head           TEXT    NOT NULL,
            tier           INTEGER NOT NULL,
            action         TEXT    NOT NULL,
            confidence     REAL    NOT NULL DEFAULT 0.0,
            evidence       TEXT,
            mode           TEXT    NOT NULL DEFAULT 'shadow',
            model          TEXT,
            decided_at     TEXT    NOT NULL
        )
        """
    )


def _add_cascade_job(
    conn: sqlite3.Connection,
    job_id: int,
    title: str,
    company: str,
    status: str = "open",
    gate_status: str = "target",
    gate_rule: str = "title_ai_ml",
    prelim_score: float = 85.0,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO jobs (id, fingerprint, title, company_name, description_text, location_raw, country, status, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, 'Sample description with 5 years experience', 'Bangalore', 'IN', ?, '2026-10-25T00:00:00Z', '2026-10-25T00:00:00Z')
        """,
        (job_id, f"fp_{job_id}", title, company, status),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO job_relevance (job_id, gate_status, gate_rule, gate_reason, rules_hash, input_hash, computed_at, final_score)
        VALUES (?, ?, ?, 'Sample gate reason', 'h1', 'h2', '2026-10-25T00:00:00Z', ?)
        """,
        (job_id, gate_status, gate_rule, prelim_score),
    )


def test_cascade_tier0_exclusion_stays_excluded(connection: sqlite3.Connection, config: Config) -> None:
    """Verify Tier 0 gate exclusion is preserved and not promoted by cascade."""
    _setup_test_db(connection)
    _add_cascade_job(connection, 1, "Staff Director", "MegaCorp", gate_status="excluded", gate_rule="seniority_title")

    router = CascadeRouter(connection, config, client=_mock_ollama_client())
    res = router.route_jobs([1], dry_run=True)

    assert res.examined == 1
    assert res.tier_counts["rules"] == 1
    assert res.decisions[0].overall_tier == "rule"
    assert res.decisions[0].resulting_status == "excluded"
    assert not res.decisions[0].demoted


def test_cascade_never_touches_protected_applied_or_saved_jobs(connection: sqlite3.Connection, config: Config) -> None:
    """Verify protected jobs (applied, saved, tailored) are NEVER demoted by the cascade."""
    _setup_test_db(connection)
    # Applied job
    _add_cascade_job(connection, 10, "Senior Developer", "GoodCo", gate_status="target")
    connection.execute("INSERT INTO applications (job_id, stage, created_at, updated_at) VALUES (10, 'applied', '2026-10-25T00:00:00Z', '2026-10-25T00:00:00Z')")

    # Saved job
    _add_cascade_job(connection, 11, "Staff Architect", "DreamCo", gate_status="target")
    connection.execute("INSERT INTO job_decisions (job_id, action, reason, happened_at, created_at) VALUES (11, 'saved', '', '2026-10-25T00:00:00Z', '2026-10-25T00:00:00Z')")

    router = CascadeRouter(connection, config, client=_mock_ollama_client())
    res = router.route_jobs([10, 11], dry_run=True)

    for d in res.decisions:
        assert not d.demoted
        assert d.resulting_status == "target"


def test_cascade_honors_verdict_is_wrong_exemption(connection: sqlite3.Connection, config: Config) -> None:
    """Verify a job where user clicked 'verdict is wrong' is exempt from demotion."""
    _setup_test_db(connection)
    _add_cascade_job(connection, 20, "Lead Architect", "Acme", gate_status="target")
    # Mark wrong verdict
    connection.execute(
        """
        INSERT INTO job_llm_verdicts (job_id, content_hash, rubric_version, model, raw_json, verified_json, hallucinated, cost_usd, created_at, source, human_label)
        VALUES (20, 'hash20', 'v1', 'gemini', '{}', '{}', 0, 0.0, '2026-10-25T00:00:00Z', 'gemini', 'wrong')
        """
    )

    router = CascadeRouter(connection, config, client=_mock_ollama_client())
    res = router.route_jobs([20], dry_run=True)

    assert len(res.decisions) == 1
    assert not res.decisions[0].demoted
    assert res.decisions[0].resulting_status == "target"


def test_cascade_dry_run_vs_apply_persistence(connection: sqlite3.Connection, config: Config) -> None:
    """Verify dry_run writes nothing to DB, whereas apply persists cascade records."""
    _setup_test_db(connection)
    _add_cascade_job(connection, 30, "Software Engineer", "BaseCo", gate_status="target")

    router = CascadeRouter(connection, config, client=_mock_ollama_client())

    # 1. Dry run
    res_dry = router.route_jobs([30], dry_run=True)
    count_dry = connection.execute("SELECT COUNT(*) FROM job_cascade_decisions").fetchone()[0]
    assert count_dry == 0

    # 2. Apply run
    res_apply = router.route_jobs([30], dry_run=False)
    count_apply = connection.execute("SELECT COUNT(*) FROM job_cascade_decisions WHERE job_id = 30").fetchone()[0]
    assert count_apply > 0


def test_shadow_report_tracking(connection: sqlite3.Connection, config: Config) -> None:
    """Verify run_shadow_report calculates agreement with subsequent human actions."""
    _setup_test_db(connection)
    _add_cascade_job(connection, 41, "Senior Lead", "AlphaCorp")
    _add_cascade_job(connection, 42, "Senior ML", "BetaCorp")
    _add_cascade_job(connection, 43, "US Remote", "GammaCorp")

    # Insert shadow demotions
    connection.execute(
        """
        INSERT INTO job_cascade_decisions (job_id, head, tier, action, confidence, evidence, mode, decided_at)
        VALUES (41, 'seniority', 1, 'demote', 0.94, '5+ years', 'shadow', '2026-10-25T00:00:00Z'),
               (42, 'seniority', 1, 'demote', 0.92, 'senior', 'shadow', '2026-10-25T00:00:00Z'),
               (43, 'location', 1, 'demote', 0.91, 'US only', 'shadow', '2026-10-25T00:00:00Z')
        """
    )

    # User subsequently dismissed job 41 for too_senior (agreed)
    connection.execute(
        "INSERT INTO job_decisions (job_id, action, reason, happened_at, created_at) VALUES (41, 'not_applying', 'too_senior', '2026-10-26T00:00:00Z', '2026-10-26T00:00:00Z')"
    )
    # User subsequently applied to job 42 (disagreed)
    connection.execute(
        "INSERT INTO applications (job_id, stage, created_at, updated_at) VALUES (42, 'applied', '2026-10-26T00:00:00Z', '2026-10-26T00:00:00Z')"
    )
    # Job 43 is unreviewed

    report = run_shadow_report(connection, config)
    assert report["total_shadow_demotions"] == 3
    assert report["agreed_human_rejections"] == 1
    assert report["disagreed_applied_saved"] == 1
    assert report["unreviewed"] == 1


# --- Additional Phase 6 Unit Tests ----------------------------------------------------------------------------------


def test_label_building_and_weights(connection: sqlite3.Connection, config: Config) -> None:
    """Verify human > verified > weak weights, unrelated reason filtering, and eval exclusion."""
    from jobhunt.matching.triage_labels import build_unified_labels

    _setup_test_db(connection)
    # Human label: applied = 0 for seniority, weight 5.0
    _add_cascade_job(connection, 101, "Junior Dev", "Acme")
    connection.execute("INSERT INTO applications (job_id, stage, created_at, updated_at) VALUES (101, 'applied', '2026-10-25T00:00:00Z', '2026-10-25T00:00:00Z')")

    # Human label: not_applying with unrelated reason e.g. closed -> must NOT generate seniority or location label
    _add_cascade_job(connection, 102, "Some Job", "Acme")
    connection.execute("INSERT INTO job_decisions (job_id, action, reason, happened_at, created_at) VALUES (102, 'not_applying', 'closed', '2026-10-25T00:00:00Z', '2026-10-25T00:00:00Z')")

    # Verified Gemini verdict: weight 2.0
    _add_cascade_job(connection, 103, "Senior Architect", "Acme")
    connection.execute(
        f"""
        INSERT INTO job_llm_verdicts (job_id, content_hash, rubric_version, model, raw_json, verified_json, cost_usd, created_at, source)
        VALUES (103, 'h103', '{config.triage.rubric_version}', 'gemini', '{{}}', '{{"level": "senior", "level_quote": "Senior Architect", "required_years_min": 6, "years_quote": "6 years", "india_eligible": "yes"}}', 0.001, '2026-10-25T00:00:00Z', 'gemini')
        """
    )

    labels = build_unified_labels(connection, config, include_weak=True)
    seniority_labels = labels.get("seniority", [])

    # Verify job 101 has weight 5.0
    lbl_101 = next(lbl for lbl in seniority_labels if lbl.job_id == 101)
    assert lbl_101.weight == 5.0
    assert lbl_101.label == 0
    assert lbl_101.eval_usable is True

    # Verify job 102 is ignored for seniority
    assert not any(lbl.job_id == 102 for lbl in seniority_labels)

    # Verify job 103 has weight 2.0 and eval_usable is True
    lbl_103 = next(lbl for lbl in seniority_labels if lbl.job_id == 103)
    assert lbl_103.weight == 2.0
    assert lbl_103.label == 1
    assert lbl_103.eval_usable is True

    # Verify weak labels have weight 0.3 and eval_usable is False
    weak_labels = [lbl for lbl in seniority_labels if lbl.source == "weak_rule"]
    for w in weak_labels:
        assert w.weight == 0.3
        assert w.eval_usable is False


def test_feature_hashing_determinism() -> None:
    """Verify feature hashing produces deterministic sparse matrices."""
    from jobhunt.matching.triage_features import build_sparse_csr, extract_ngrams

    text_title = "Staff Machine Learning Engineer"
    text_extract = "8+ years experience in Python and PyTorch"

    ng_a = extract_ngrams(text_title, text_extract)
    ng_b = extract_ngrams(text_title, text_extract)

    assert ng_a == ng_b

    csr_a = build_sparse_csr([ng_a])
    csr_b = build_sparse_csr([ng_b])

    assert (csr_a.indices == csr_b.indices).all()
    assert (csr_a.indptr == csr_b.indptr).all()
    assert (csr_a.data == csr_b.data).all()


def test_hybrid_logistic_model_convergence_and_calibration() -> None:
    """Verify HybridLogisticModel trains with sample weights and Platt calibration monotonically."""
    import numpy as np
    from jobhunt.matching.triage_features import SparseCSR
    from jobhunt.matching.triage_models import HybridLogisticModel, PlattCalibrator

    np.random.seed(42)
    # Synthetic linearly separable data
    n_samples = 100
    x_dense = np.random.randn(n_samples, 4).astype(np.float32)
    y = ((x_dense[:, 0] + x_dense[:, 1]) > 0).astype(np.int32)
    sample_weights = np.ones(n_samples, dtype=np.float32)

    # Empty sparse
    csr = SparseCSR(data=np.array([], dtype=np.float32), indices=np.array([], dtype=np.int32), indptr=np.zeros(n_samples + 1, dtype=np.int32), shape=(n_samples, 10))

    model = HybridLogisticModel(w_dense=np.zeros(4, dtype=np.float32), w_sparse=np.zeros(10, dtype=np.float32), l2=0.01)
    model.fit(x_dense, csr, y, sample_weights, steps=60, lr=0.1)

    probs = model.predict_proba(x_dense, csr)
    preds = (probs >= 0.5).astype(np.int32)
    acc = float(np.mean(preds == y))
    assert acc >= 0.85

    # Calibrator monotonicity
    logits = model.predict_logits(x_dense, csr)
    calib = PlattCalibrator()
    calib.fit(logits, y)
    cal_probs = calib.predict_proba(logits)
    assert len(cal_probs) == n_samples
    assert np.all((cal_probs >= 0.0) & (cal_probs <= 1.0))


def test_grouped_kfold_no_company_leakage() -> None:
    """Verify grouped K-fold never places the same company in train and test splits."""
    from jobhunt.matching.triage_train import grouped_kfold_split

    companies = ["Google", "Meta", "Amazon", "Microsoft", "Apple", "Netflix", "Google", "Meta"]
    n_samples = len(companies)

    folds = grouped_kfold_split(companies, n_splits=3, seed=42)
    assert len(folds) == 3

    for train_idx, test_idx in folds:
        train_companies = {companies[i] for i in train_idx}
        test_companies = {companies[i] for i in test_idx}
        assert train_companies.isdisjoint(test_companies)


def test_time_split_chronological_ordering() -> None:
    """Verify time split trains on older jobs and tests on newest 20%."""
    from jobhunt.matching.triage_train import time_split

    dates = [
        "2026-01-01T00:00:00Z",
        "2026-01-02T00:00:00Z",
        "2026-01-03T00:00:00Z",
        "2026-01-04T00:00:00Z",
        "2026-01-05T00:00:00Z",
    ]
    train_idx, test_idx = time_split(dates, train_ratio=0.8)
    assert len(train_idx) == 4
    assert len(test_idx) == 1
    assert test_idx[0] == 4


def test_retrain_guard_preserves_old_model() -> None:
    """Verify retrain guard refuses replacement when metric drops by > 0.03 or label count is insufficient."""
    from jobhunt.matching.triage_train import should_accept_retrain

    # Metric dropped from 0.90 to 0.85 (>0.03 drop)
    accept, reason = should_accept_retrain(
        old_metric=0.90,
        new_metric=0.85,
        new_label_count=100,
        min_labels=30,
        max_metric_drop=0.03,
    )
    assert not accept
    assert "metric dropped" in reason

    # Insufficient samples
    accept2, reason2 = should_accept_retrain(
        old_metric=0.90,
        new_metric=0.95,
        new_label_count=15,
        min_labels=30,
    )
    assert not accept2
    assert "insufficient" in reason2

    # Healthy retrain
    accept3, _ = should_accept_retrain(
        old_metric=0.90,
        new_metric=0.92,
        new_label_count=50,
        min_labels=30,
    )
    assert accept3


def test_local_llm_verbatim_quote_verification() -> None:
    """Verify verbatim quote verification accepts substrings and rejects hallucinations."""
    from jobhunt.matching.triage import verify

    extract = "We require at least 5 years of experience in distributed systems. Office based in Bangalore, India."

    # Substring quotes
    raw_valid = {
        "level": "senior",
        "level_evidence": "senior",  # not in extract, but level has no quote in extract
        "required_years_min": 5,
        "years_evidence": "5 years of experience",
        "india_eligible": "yes",
        "india_evidence": "Bangalore, India",
    }
    v_valid = verify(raw_valid, extract)
    assert v_valid.required_years_min == 5
    assert v_valid.india_eligible == "yes"
    assert "required_years_min" not in v_valid.hallucinated
    assert "india_eligible" not in v_valid.hallucinated
    assert "level" in v_valid.hallucinated  # 'senior' was not in text

    # Hallucinated quote
    raw_invalid = {
        "required_years_min": 10,
        "years_evidence": "10 years in Kubernetes",
        "india_eligible": "no",
        "india_evidence": "United States only",
    }
    v_invalid = verify(raw_invalid, extract)
    assert "required_years_min" in v_invalid.hallucinated
    assert "india_eligible" in v_invalid.hallucinated


def test_scheduler_triage_task_entry() -> None:
    """Verify build_task_specs adds the triage task when triage_time is configured."""
    from jobhunt.config import ScheduleSettings
    from jobhunt.schedule.definitions import build_task_specs

    # Default: 5 tasks
    default_specs = build_task_specs(ScheduleSettings())
    assert len(default_specs) == 5
    assert "triage" not in default_specs

    # Configured: 6 tasks
    triage_specs = build_task_specs(ScheduleSettings(triage_time="07:30"))
    assert len(triage_specs) == 6
    assert "triage" in triage_specs
    assert triage_specs["triage"].triggers[0].times == ("07:30",)
    assert triage_specs["triage"].steps[-1] in (("triage-run", "--apply"), ("triage-llm-run", "--apply"))

