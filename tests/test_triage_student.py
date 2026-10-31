"""v8 B4: rules and the logistic classifier mined from verdicts, the retrain guards, and how the student acts in the gate."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jobhunt.config import BudgetSettings, Config
from jobhunt.matching import triage_student as ts
from jobhunt.matching.gate import run_relevance
from jobhunt.matching.location import run_location
from jobhunt.matching.triage import Verdict, build_evidence
from tests.test_triage import add_job

COMPANIES = ["Acme", "Globex", "Initech", "Umbrella", "Hooli", "Stark"]


@pytest.fixture()
def cfg(config: Config, tmp_path: Path) -> Config:
    relevance = dataclasses.replace(config.relevance, seniority=dataclasses.replace(config.relevance.seniority, mode="off"))
    return dataclasses.replace(config, relevance=relevance, project_root=tmp_path, budget=BudgetSettings(monthly_usd=5.0))


def store(connection: sqlite3.Connection, cfg: Config, job_id: int, verdict: Verdict, label: str | None = None) -> None:
    connection.execute(
        "INSERT INTO job_llm_verdicts (job_id, content_hash, rubric_version, model, raw_json, verified_json, created_at, human_label) "
        "VALUES (?, ?, ?, 'm', '{}', ?, '2026-10-12T00:00:00Z', ?)",
        (job_id, f"h{job_id}", cfg.triage.rubric_version, json.dumps(verdict.as_json()), label))


SENIOR = Verdict(level="senior", confidence="high", level_quote="staff level")
ABROAD = Verdict(india_eligible="no", india_quote="US only", location_scope="us_only", confidence="high")
ENTRY = Verdict(level="entry", confidence="high", level_quote="new grad")


def judged_world(connection: sqlite3.Connection, cfg: Config, per_kind: int = 40) -> dict[str, list[int]]:
    """per_kind senior jobs ('staff' / 'principal' titles over six companies), per_kind abroad jobs ('Berlin'), per_kind entry jobs."""
    ids: dict[str, list[int]] = {"senior": [], "abroad": [], "entry": []}
    n = 0
    for i in range(per_kind):
        n += 1
        title = ("Staff Software Engineer" if i % 2 else "Principal Backend Engineer")
        job = add_job(connection, n, title, f"You will own the architecture. Requires 8+ years of experience. Team {i}.",
                      company_name=COMPANIES[i % 6])
        store(connection, cfg, job, SENIOR)
        ids["senior"].append(job)
        n += 1
        job = add_job(connection, n, "Python Developer", f"Join our team in Berlin, Germany. Hybrid office. Project {i}.",
                      company_name=COMPANIES[i % 6], location_raw="Remote")
        store(connection, cfg, job, ABROAD)
        ids["abroad"].append(job)
        n += 1
        job = add_job(connection, n, "Junior Python Developer", f"Great place for new grads to learn. Project {i}.",
                      company_name=COMPANIES[i % 6])
        store(connection, cfg, job, ENTRY)
        ids["entry"].append(job)
    return ids


# --- features and numeric helpers ---------------------------------------------------------------------------------


def test_feature_indexes_are_deterministic_and_field_tagged() -> None:
    first = build_evidence("Staff Engineer", "Acme", "Berlin", "remote", None, "Own the platform.")
    again = build_evidence("Staff Engineer", "Acme", "Berlin", "remote", None, "Own the platform.")
    assert ts.feature_indexes(first) == ts.feature_indexes(again) and all(0 <= i < ts.DIM for i in ts.feature_indexes(first))
    assert "staff engineer" in ts.field_tokens(first)["t"] and "berlin" in ts.field_tokens(first)["l"]


def test_logistic_regression_separates_a_clean_signal_and_honours_weights() -> None:
    rng = np.random.default_rng(1)
    x = np.zeros((120, 16), dtype=np.float32)
    y = np.zeros(120)
    for row in range(120):
        positive = row % 2 == 0
        x[row, 0 if positive else 1] = 1.0
        x[row, 2 + int(rng.integers(0, 10))] = 1.0
        y[row] = 1.0 if positive else 0.0
    w, b = ts.fit_logistic(x, y, np.ones(120))
    scores = ts._sigmoid(x @ w + b)
    assert scores[y == 1].min() > 0.7 and scores[y == 0].max() < 0.3
    assert ts.average_precision(y, scores) == pytest.approx(1.0)


def test_threshold_for_picks_the_lowest_threshold_with_enough_precision_and_support() -> None:
    labels = np.array([1] * 8 + [0] * 8, dtype=float)
    scores = np.array([0.99, 0.97, 0.95, 0.93, 0.91, 0.9, 0.7, 0.6, 0.85, 0.5, 0.4, 0.3, 0.2, 0.1, 0.05, 0.01])
    chosen = ts.threshold_for(labels, scores, 0.9, 5)
    assert chosen is not None and chosen["precision"] >= 0.9 and chosen["predicted"] >= 5
    assert ts.threshold_for(labels, scores, 1.0, 12) is None


# --- rules ----------------------------------------------------------------------------------------------------------


def rows_for(texts: list[tuple[str, str, bool]]) -> list[ts.Row]:
    return [ts.Row(i, build_evidence(title, company, "Remote", None, None, "Text."), senior, False, 1.0)
            for i, (title, company, senior) in enumerate(texts)]


def test_a_rule_needs_precision_support_and_several_companies() -> None:
    data = [("Staff Engineer", c, True) for c in COMPANIES[:5]] + [("Junior Engineer", c, False) for c in COMPANIES[:5]]
    rules = ts.mine_rules(rows_for(data), {}, precision=0.9, min_support=5)
    assert [r.token for r in rules if r.target == "senior" and r.field == "t" and r.token == "staff"] == ["staff"]
    one_company = [("Staff Engineer", "Acme", True)] * 6 + [("Junior Engineer", "Globex", False)] * 6
    assert not [r for r in ts.mine_rules(rows_for(one_company), {}, precision=0.9, min_support=5) if r.field == "t"]
    thin = [("Staff Engineer", c, True) for c in COMPANIES[:3]] + [("Junior Engineer", c, False) for c in COMPANIES[:5]]
    assert not [r for r in ts.mine_rules(rows_for(thin), {}, precision=0.9, min_support=5) if r.token == "staff"]


def test_location_rules_never_use_title_words() -> None:
    texts = [ts.Row(i, build_evidence("Security Engineer", COMPANIES[i % 6], "Remote", None, None, "Text."), False, True, 1.0)
             for i in range(12)]
    rules = ts.mine_rules(texts, {}, precision=0.9, min_support=5)
    assert [r for r in rules if r.target == "not_india" and r.field == "t"] == []
    assert any(r.target == "not_india" and r.field == "l" for r in rules)          # the location word "remote" is allowed


def test_a_rule_that_matches_a_protected_job_is_only_proposed() -> None:
    data = [("Staff Engineer", c, True) for c in COMPANIES[:5]] + [("Junior Engineer", c, False) for c in COMPANIES[:5]]
    protected = {999: build_evidence("Staff Engineer", "Acme", "Pune", None, None, "I applied here.")}
    rule = next(r for r in ts.mine_rules(rows_for(data), protected, precision=0.9, min_support=5) if r.token == "staff")
    assert rule.status == "proposed" and rule.blocked_by == [999]


# --- training and the retrain guards -------------------------------------------------------------------------------


def test_too_little_data_trains_nothing(connection, cfg) -> None:  # noqa: ANN001
    judged_world(connection, cfg, per_kind=5)
    model = ts.train(connection, cfg)
    assert model["trained"] is False and "need" in model["reason"]
    assert ts.retrain_if_needed(connection, cfg)["action"] == "skipped"
    assert not ts.model_path(cfg).exists()


def test_training_learns_both_targets_and_reports_cross_validated_precision(connection, cfg) -> None:  # noqa: ANN001
    judged_world(connection, cfg)
    model = ts.train(connection, cfg)
    assert model["trained"] and model["rows"] == 120
    for target in ts.TARGETS:
        metrics = model["metrics"][target]
        assert metrics["trusted"] and metrics["chosen"]["precision"] >= cfg.triage.student_precision and metrics["folds"] == 4
    applied = {(r["target"], r["field"], r["token"]) for r in model["rules"] if r["status"] == "applied"}
    assert ("senior", "t", "staff") in applied


def test_my_verdict_is_wrong_label_flips_the_teachers_answer_with_triple_weight(connection, cfg) -> None:  # noqa: ANN001
    world = judged_world(connection, cfg, per_kind=2)
    store_label = connection.execute("UPDATE job_llm_verdicts SET human_label = 'wrong_level' WHERE job_id = ?", (world["senior"][0],))
    assert store_label.rowcount == 1
    by_id = {row.job_id: row for row in ts.build_rows(connection, cfg)}
    flipped = by_id[world["senior"][0]]
    assert flipped.senior is False and flipped.weight == ts.HUMAN_WEIGHT and by_id[world["senior"][1]].senior is True
    connection.execute("UPDATE job_llm_verdicts SET human_label = 'other' WHERE job_id = ?", (world["entry"][0],))
    assert world["entry"][0] not in {row.job_id for row in ts.build_rows(connection, cfg)}      # direction unknown: skipped


def test_retrain_needs_fifty_new_rows_and_keeps_the_old_model_when_the_new_one_is_worse(connection, cfg) -> None:  # noqa: ANN001
    judged_world(connection, cfg)
    first = ts.retrain_if_needed(connection, cfg)
    assert first["action"] == "retrained" and ts.model_path(cfg).is_file()
    again = ts.retrain_if_needed(connection, cfg)
    assert again["action"] == "skipped" and "need 50" in again["reason"]
    saved = json.loads(ts.model_path(cfg).read_text(encoding="utf-8"))
    saved["metrics"]["senior"]["average_precision"] = 2.0       # an (artificially) better old model: this synthetic data is already perfect
    saved["metrics"]["not_india"]["average_precision"] = 2.0
    saved["rows"] = 10                                      # pretend the old model was trained on far fewer rows
    ts.model_path(cfg).write_text(json.dumps(saved), encoding="utf-8")
    worse = ts.retrain_if_needed(connection, cfg)
    assert worse["action"] == "kept_previous" and "below" in worse["reason"]
    assert json.loads(ts.model_path(cfg).read_text(encoding="utf-8"))["rows"] == 10
    forced = ts.retrain_if_needed(connection, cfg, force=True)
    assert forced["action"] == "kept_previous"             # force skips the row count, never the quality guard
    assert "triage-student:" in ts.describe_decision(worse)


def test_the_student_flags_the_patterns_it_learned(connection, cfg) -> None:  # noqa: ANN001
    judged_world(connection, cfg)
    ts.retrain_if_needed(connection, cfg)
    student = ts.load_student(cfg)
    assert student is not None
    senior = student.opinion(build_evidence("Staff Platform Engineer", "Acme", "Pune", None, None, "Own the architecture, 8+ years."))
    entry = student.opinion(build_evidence("Junior Python Developer", "Acme", "Pune", None, None, "Great place for new grads."))
    assert senior.p_senior > entry.p_senior and any(r.target == "senior" for r in senior.rules) and not entry.senior_demote


# --- in the gate ------------------------------------------------------------------------------------------------------


def status_of(connection, job_id: int) -> tuple[str, str]:  # noqa: ANN001
    row = connection.execute("SELECT gate_status, gate_rule FROM job_relevance WHERE job_id = ?", (job_id,)).fetchone()
    return row["gate_status"], row["gate_rule"]


def test_a_mined_senior_rule_excludes_an_unjudged_job_and_leaves_judged_and_protected_ones(connection, cfg) -> None:  # noqa: ANN001
    judged_world(connection, cfg)
    ts.retrain_if_needed(connection, cfg)
    fresh = add_job(connection, 900, "Staff Software Engineer", "Build APIs. Fine team.", company_name="Wayne")
    protected = add_job(connection, 901, "Staff Software Engineer", "Build APIs. Fine team.", company_name="Wayne")
    connection.execute("INSERT INTO job_decisions (job_id, action, happened_at, created_at) VALUES (?, 'saved', 'n', 'n')", (protected,))
    run_relevance(connection, cfg)
    assert status_of(connection, fresh) == ("excluded", "student_rule")
    assert status_of(connection, protected)[1] != "student_rule"
    reason = connection.execute("SELECT gate_reason, triage_json FROM job_relevance WHERE job_id = ?", (fresh,)).fetchone()
    assert "Mined rule" in reason["gate_reason"] and json.loads(reason["triage_json"])["demoted_by"] == "student_rule"


def test_a_job_with_an_llm_verdict_is_decided_by_the_verdict_not_the_student(connection, cfg) -> None:  # noqa: ANN001
    world = judged_world(connection, cfg)
    ts.retrain_if_needed(connection, cfg)
    entry_like = world["entry"][0]
    connection.execute("UPDATE jobs SET title = 'Staff Software Engineer' WHERE id = ?", (entry_like,))
    run_relevance(connection, cfg, force=True)
    assert status_of(connection, entry_like)[1] != "student_rule"      # its (entry) verdict, not the title rule, is used
    assert connection.execute("SELECT triage_json FROM job_relevance WHERE job_id = ?", (entry_like,)).fetchone()[0] is None


def test_the_classifier_only_demotes_a_target_to_maybe_and_never_excludes(connection, cfg) -> None:  # noqa: ANN001
    judged_world(connection, cfg)
    model = ts.train(connection, cfg)
    model["rules"] = []                                         # classifier only
    model["version"] = "student-classifier-test"
    ts.model_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    ts.model_path(cfg).write_text(json.dumps(model), encoding="utf-8")
    fresh = add_job(connection, 910, "Principal Backend Software Engineer", "You will own the architecture of the platform.", company_name="Wayne")
    run_relevance(connection, cfg)
    status, rule = status_of(connection, fresh)
    assert status == "maybe" and rule == "student_classifier"
    row = connection.execute("SELECT rank_adjust, triage_json FROM job_relevance WHERE job_id = ?", (fresh,)).fetchone()
    assert row["rank_adjust"] < 0 and json.loads(row["triage_json"])["p_senior"] >= 0.5


def test_a_mined_location_rule_hides_a_vaguely_located_unjudged_job_only(connection, cfg) -> None:  # noqa: ANN001
    judged_world(connection, cfg)
    ts.retrain_if_needed(connection, cfg)
    model = json.loads(ts.model_path(cfg).read_text(encoding="utf-8"))
    rule = {"target": "not_india", "field": "l", "token": "berlin", "support": 40, "precision": 1.0, "status": "applied", "blocked_by": []}
    model["rules"] = [rule]
    model["version"] = "student-location-test"
    ts.model_path(cfg).write_text(json.dumps(model), encoding="utf-8")
    vague = add_job(connection, 920, "Python Developer", "Team in Germany.", company_name="Wayne", location_raw="Berlin or remote",
                    workplace_type="remote", country=None)
    india = add_job(connection, 921, "Python Developer", "Team in India.", company_name="Wayne", location_raw="Berlin, Pune, India",
                    workplace_type="onsite")
    run_relevance(connection, cfg)
    run_location(connection, cfg)
    fits = {row["job_id"]: (row["location_fit"], row["location_reason"]) for row in connection.execute(
        "SELECT job_id, location_fit, location_reason FROM job_relevance WHERE job_id IN (?, ?)", (vague, india))}
    assert fits[vague][0] == "wrong_location" and fits[vague][1].startswith("student_rule")
    assert fits[india][0] == "india"


def test_student_disabled_or_missing_changes_nothing(connection, cfg) -> None:  # noqa: ANN001
    judged_world(connection, cfg)
    ts.retrain_if_needed(connection, cfg)
    off = dataclasses.replace(cfg, triage=dataclasses.replace(cfg.triage, student_enabled=False))
    fresh = add_job(connection, 930, "Staff Software Engineer", "Build APIs. Fine team.", company_name="Wayne")
    run_relevance(connection, off)
    assert status_of(connection, fresh)[1] != "student_rule"
