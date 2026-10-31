"""Eligibility flags and learned rules in the gate and the score: reversible, never promoting, never deleting."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from jobhunt.config import Config
from jobhunt.matching import learned_rules
from jobhunt.matching.gate import EXCLUDED, MAYBE, TARGET, GateJob, RelevanceGate, run_relevance
from jobhunt.matching.scoring import compute_final_scores
from tests.api.conftest import add_job

TODAY = date(2026, 10, 19)
PHD = "Research Scientist Intern - Applied Scientist"


def judge(config: Config, title: str, description: str | None, **extra):  # noqa: ANN003, ANN201
    gate = RelevanceGate(config.relevance, extra.pop("learned", ()))
    return gate.evaluate(GateJob(job_id=1, title=title, description=description, experience_level="intern",
                                 employment_type="internship", **extra), TODAY)


def test_required_flag_turns_a_target_into_a_maybe_with_a_factual_note(config: Config) -> None:
    clean = judge(config, "Machine Learning Engineer Intern", "Build ML systems in Python.")
    assert clean.status == TARGET and not clean.eligibility_json
    flagged = judge(config, "Machine Learning Engineer Intern", "You are currently pursuing a PhD in machine learning.")
    assert flagged.status == MAYBE and flagged.rule == "likely_ineligible"
    assert flagged.reason.startswith("Likely not eligible: PhD required. Posting says:")
    assert json.loads(flagged.eligibility_json)[0]["kind"] == "phd_required"


def test_a_maybe_stays_maybe_and_an_excluded_job_is_never_promoted(config: Config) -> None:
    maybe = judge(config, "Business Analyst Intern", "Master's degree required. Python and SQL.")
    assert maybe.status != TARGET and maybe.status != "excluded" or maybe.status == EXCLUDED
    excluded = judge(config, "Java Developer", "Must be currently pursuing a PhD.")
    assert excluded.status == EXCLUDED and excluded.rule == "hard_exclude_title"
    assert json.loads(excluded.eligibility_json)                       # still noted, still excluded, nothing deleted


def test_preferred_flags_and_alternatives_have_no_effect(config: Config) -> None:
    for text in ("A PhD is a plus.", "Bachelor's, Master's or PhD students welcome.", "Authorized to work in the US."):
        decision = judge(config, "Machine Learning Engineer Intern", text)
        assert decision.status == TARGET, text


def test_source_degree_lists_are_information_unless_configured(config: Config) -> None:
    info = judge(config, "Machine Learning Engineer Intern", None, degrees=("PhD",))
    assert info.status == TARGET and json.loads(info.eligibility_json)[0]["strength"] == "preferred"
    strict = dataclasses.replace(config, relevance=dataclasses.replace(config.relevance, eligibility_source_lists_required=True))
    assert judge(strict, "Machine Learning Engineer Intern", None, degrees=("PhD",)).status == MAYBE


def seed_scored(db: sqlite3.Connection, config: Config, description: str) -> int:
    job_id = add_job(db, title="Machine Learning Engineer Intern", description_text=description,
                     experience_level="intern", employment_type="internship", raw_json=None)
    run_relevance(db, config)
    db.execute("""UPDATE job_relevance SET similarity_ai = 0.9, similarity_fullstack = 0.8, similarity_percentile = 0.99,
                  local_rank_score = 95, location_fit = 'india', seniority_level = 'junior' WHERE job_id = ?""", (job_id,))
    compute_final_scores(db, config)
    return job_id


def test_required_flag_caps_the_score_at_fifty_and_records_why(db: sqlite3.Connection, config: Config) -> None:
    ok = seed_scored(db, config, "Build ML systems in Python.")
    flagged = seed_scored(db, config, "Applicants must be currently enrolled in a PhD program.")
    ok_score = db.execute("SELECT final_score FROM job_relevance WHERE job_id = ?", (ok,)).fetchone()[0]
    row = db.execute("SELECT final_score, score_components FROM job_relevance WHERE job_id = ?", (flagged,)).fetchone()
    assert ok_score > 50 and row["final_score"] == 50.0
    assert json.loads(row["score_components"])["capped_by"] == ["eligibility"]
    assert db.execute("SELECT status FROM jobs WHERE id = ?", (flagged,)).fetchone()[0] == "open"     # never deleted


def test_learned_rules_demote_or_penalise_but_never_exclude(config: Config, tmp_path: Path) -> None:
    path = tmp_path / "learned.yaml"
    rule = learned_rules.add_rule(path, kind="title_phrase", phrase="machine learning engineer", evidence_count=6,
                                  job_ids=[1, 2, 3, 4, 5, 6], reason="wrong_role", created="2026-10-19",
                                  source_key="title_phrase:scientist", demote=True, penalty=8)
    decision = judge(config, "Machine Learning Engineer Intern", "Python.", learned=learned_rules.load_rules(path))
    assert decision.status == MAYBE and decision.rule == "learned_rule" and decision.learned_penalty == 8
    assert json.loads(decision.learned_json)[0]["id"] == rule.id and "6 of your decisions" in json.loads(decision.learned_json)[0]["tooltip"]
    unaffected = judge(config, "Backend Engineer Intern", "Python.", learned=learned_rules.load_rules(path))
    assert unaffected.status == TARGET and not unaffected.learned_json
    already = judge(config, "Java Developer", "x", learned=learned_rules.load_rules(path))
    assert already.status == EXCLUDED                                  # a learned rule cannot change an exclusion


def test_learned_rule_limits_are_enforced_on_write_and_on_load(tmp_path: Path) -> None:
    path = tmp_path / "learned.yaml"
    with pytest.raises(ValueError):                                    # fewer than 5 supporting jobs
        learned_rules.add_rule(path, kind="title_phrase", phrase="scientist", evidence_count=4, job_ids=[1, 2, 3, 4],
                               reason="x", created="d", source_key="k")
    with pytest.raises(ValueError):                                    # penalty above the cap
        learned_rules.add_rule(path, kind="title_phrase", phrase="scientist", evidence_count=9, job_ids=[1],
                               reason="x", created="d", source_key="k", penalty=50)
    with pytest.raises(ValueError):                                    # unknown kind (e.g. an "exclude" rule)
        learned_rules.add_rule(path, kind="exclude", phrase="scientist", evidence_count=9, job_ids=[1], reason="x",
                               created="d", source_key="k")
    path.write_text("rules:\n- {id: L-0001, kind: title_phrase, phrase: hand edited, penalty: 99, demote: true, "
                    "evidence: {count: 9, job_ids: [1]}}\n- {id: L-0002, kind: title_phrase, phrase: too few, "
                    "evidence: {count: 2}}\n", encoding="utf-8")
    assert learned_rules.load_rules(path) == []                         # a hand-edited rule over a limit is ignored


def test_adding_and_removing_a_rule_changes_the_gate_hash_and_removal_works(db: sqlite3.Connection, config: Config,
                                                                           tmp_path: Path) -> None:
    cfg = dataclasses.replace(config, project_root=tmp_path)
    job_id = add_job(db, title="Machine Learning Engineer Intern", description_text="Python.", experience_level="intern",
                     employment_type="internship")
    run_relevance(db, cfg)
    assert db.execute("SELECT gate_status FROM job_relevance WHERE job_id = ?", (job_id,)).fetchone()[0] == TARGET
    path = cfg.resolve_path(learned_rules.LEARNED_FILE)
    rule = learned_rules.add_rule(path, kind="title_phrase", phrase="machine learning engineer", evidence_count=5,
                                  job_ids=[1, 2, 3, 4, 5], reason="wrong_role", created="d", source_key="t:rs")
    run_relevance(db, cfg)                                              # the signature changed: verdicts recomputed
    assert db.execute("SELECT gate_status FROM job_relevance WHERE job_id = ?", (job_id,)).fetchone()[0] == MAYBE
    assert learned_rules.remove_rule(path, rule.id) is True and learned_rules.remove_rule(path, rule.id) is False
    run_relevance(db, cfg)
    assert db.execute("SELECT gate_status FROM job_relevance WHERE job_id = ?", (job_id,)).fetchone()[0] == TARGET
