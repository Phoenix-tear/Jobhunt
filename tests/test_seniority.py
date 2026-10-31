"""Rule-based seniority: ordered title rules, level codes, and the gate's junior_only mode."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from jobhunt.config import Config
from jobhunt.matching.gate import GateJob, RelevanceGate
from jobhunt.matching.seniority import (
    EXECUTIVE, JUNIOR, MID, SENIOR, UNSPECIFIED, classify_seniority, compile_level_code_rules,
)

TODAY = date(2026, 10, 14)

CASES = [
    # --- executive / senior compounds first -------------------------------------------------
    ("Assistant Vice President - Software Engineer", EXECUTIVE),
    ("Vice President, ML Engineering", EXECUTIVE),
    ("AVP, Mobile Backend Developer", EXECUTIVE),
    ("Data Analyst-AVP", EXECUTIVE),
    ("Infrastructure Design Engineer (AVP)", EXECUTIVE),
    ("SVP Engineering", EXECUTIVE),
    ("Associate Vice President, Data Platforms", EXECUTIVE),
    ("Managing Director - Technology", EXECUTIVE),
    ("Director of Engineering", EXECUTIVE),
    ("Head of Machine Learning", EXECUTIVE),
    ("Chief Data Officer", EXECUTIVE),
    ("Distinguished Engineer", EXECUTIVE),
    ("Associate Director, Data", SENIOR),
    ("Associate Principal Engineer", SENIOR),
    ("Associate Manager - Software Engineering", SENIOR),
    ("Principal Engineer", SENIOR),
    ("Principal Data Scientist", SENIOR),
    ("Senior Software Engineer", SENIOR),
    ("Sr. Software Engineer", SENIOR),
    ("Sr Data Engineer", SENIOR),
    ("Staff Software Engineer", SENIOR),
    ("Lead Data Scientist", SENIOR),
    ("Lead Engineer", SENIOR),
    ("Tech Lead - Platform", SENIOR),
    ("Team Lead, Backend", SENIOR),
    ("Engineering Manager", SENIOR),
    ("Solutions Architect", SENIOR),
    ("Software Engineer, Senior", SENIOR),
    # --- mid: level numbers ------------------------------------------------------------------
    ("Software Engineer II", MID),
    ("Software Engineer III - Equities", MID),
    ("SDE II", MID),
    ("SDE 3", MID),
    ("Engineer 2", MID),
    ("Full-stack Engineer 4", MID),
    ("AI Engineer 5 (Gen AI Platform)", MID),
    ("Data Engineer IV", MID),
    ("Level 3 Software Developer", MID),
    ("Software Engineer L5", MID),
    ("Intermediate Software Engineer - Java Full Stack", MID),
    ("Mid-Level Data Analyst", MID),
    # --- junior ------------------------------------------------------------------------------
    ("Associate Software Engineer", JUNIOR),
    ("Associate (AI) Solution Consultant", JUNIOR),
    ("Programmer Analyst", JUNIOR),
    ("Graduate Analyst", JUNIOR),
    ("SDE 1", JUNIOR),
    ("SDE-I", JUNIOR),
    ("Software Engineer I", JUNIOR),
    ("Data Scientist I", JUNIOR),
    ("Level 1 Support Engineer", JUNIOR),
    ("Software Engineering Intern", JUNIOR),
    ("2027 Technology Developer Summer Internship Programme", JUNIOR),
    ("Quantitative Analytics Associate Off Cycle Internship", JUNIOR),
    ("Graduate Software Engineer", JUNIOR),
    ("Software Engineer New Grad", JUNIOR),
    ("Graduate Engineer Trainee", JUNIOR),
    ("Management Trainee - Technology", JUNIOR),
    ("Young Apprentice", JUNIOR),
    ("Early Careers - Software Developer", JUNIOR),
    ("Campus Hire Engineer", JUNIOR),
    ("Junior Full Stack Developer", JUNIOR),
    ("Jr. Python Developer", JUNIOR),
    ("Entry Level Machine Learning Engineer", JUNIOR),
    ("Fresher - Backend Developer", JUNIOR),
    ("Data Engineer - Analyst", JUNIOR),
    ("Custom Software Engineering Analyst", JUNIOR),
    ("Intern - Lead Generation Tools", JUNIOR),
    # --- unspecified -------------------------------------------------------------------------
    ("Software Engineer", UNSPECIFIED),
    ("Machine Learning Engineer", UNSPECIFIED),
    ("Full Stack Developer", UNSPECIFIED),
    ("AI Engineer", UNSPECIFIED),
    ("Backend Engineer - Payments", UNSPECIFIED),
    ("Software Engineer 2027", UNSPECIFIED),
    ("Lead Generation Specialist", "caution"),
    ("Python Developer - Remote", UNSPECIFIED),
]


@pytest.mark.parametrize("title,expected", CASES)
def test_title_levels(title: str, expected: str) -> None:
    verdict = classify_seniority(title)
    assert verdict.level == expected, (title, verdict)
    assert verdict.evidence


def test_there_are_at_least_sixty_cases() -> None:
    assert len(CASES) >= 60


def test_associate_with_a_senior_word_is_never_read_as_junior() -> None:
    for title in ("Associate Director", "Associate Vice President", "Associate Principal Scientist"):
        assert classify_seniority(title).level in {SENIOR, EXECUTIVE}


def test_level_code_rules_use_title_or_description_head() -> None:
    rules = compile_level_code_rules([
        {"name": "Citi C11+", "pattern": r"\bC1[1-9]\b", "level": "senior", "scope": "any"},
        {"name": "Officer", "pattern": r"(?:,\s*officer\b|\bofficer\s*$)", "level": "senior", "scope": "title"},
        {"name": "bad", "pattern": "x", "level": "wizard"},          # unknown level: ignored
    ])
    assert len(rules) == 2
    assert classify_seniority("Java Developer", description_head="Apps Dev (C11) role", level_code_rules=rules).level == SENIOR
    assert classify_seniority("Data Platform Engineer, Officer", level_code_rules=rules).level == SENIOR
    assert classify_seniority("Developer", description_head="officer of the day", level_code_rules=rules).level == UNSPECIFIED
    # a strong junior marker is not overridden by a code rule
    assert classify_seniority("Graduate Developer", description_head="C12", level_code_rules=rules).level == JUNIOR


@pytest.fixture(scope="module")
def gate(config: Config) -> RelevanceGate:
    return RelevanceGate(config.relevance)


def _decide(gate: RelevanceGate, title: str, description: str = "Build Python services.", **extra):  # noqa: ANN001, ANN202
    return gate.evaluate(GateJob(job_id=1, title=title, description=description, **extra), TODAY)


@pytest.mark.parametrize("title", [
    "Assistant Vice President - Software Engineer", "Vice President, ML Engineering", "Software Engineer II",
    "Sr. Software Engineer", "Lead Data Scientist", "Principal Engineer", "Associate Director, Data",
    "Software Engineer III", "Applications Development Programmer Analyst - C12",
])
def test_gate_excludes_non_junior_titles_with_evidence(gate: RelevanceGate, title: str) -> None:
    decision = _decide(gate, title)
    assert decision.status == "excluded" and decision.rule == "seniority_title"
    assert decision.seniority_level in {MID, SENIOR, EXECUTIVE} and decision.seniority_evidence


@pytest.mark.parametrize("title,level", [
    ("Associate Software Engineer", JUNIOR), ("SDE 1", JUNIOR), ("Graduate Analyst Software Developer", JUNIOR),
    ("Machine Learning Engineer", UNSPECIFIED),
])
def test_gate_keeps_junior_and_unspecified(gate: RelevanceGate, title: str, level: str) -> None:
    decision = _decide(gate, title)
    assert decision.status != "excluded" and decision.seniority_level == level


def test_off_mode_ignores_title_seniority(config: Config) -> None:
    relaxed = dataclasses.replace(config.relevance, seniority=dataclasses.replace(config.relevance.seniority, mode="off"))
    decision = _decide(RelevanceGate(relaxed), "Software Engineer II")
    assert decision.rule != "seniority_title"
    assert decision.seniority_level == MID          # still recorded for display


def test_internships_are_not_excluded_by_a_rank_word(gate: RelevanceGate) -> None:
    decision = _decide(gate, "Software Engineering Intern", employment_type="internship")
    assert decision.status != "excluded" and decision.seniority_level == JUNIOR


def test_junior_boost_adds_points_to_the_final_score(connection, config: Config) -> None:  # noqa: ANN001
    from jobhunt.matching.scoring import compute_final_scores
    from tests.test_scoring import add_job, seed

    class _Clock:
        def __call__(self):  # noqa: ANN204
            from datetime import datetime, timezone
            return datetime(2026, 10, 14, tzinfo=timezone.utc)

    cfg = dataclasses.replace(config, scoring=dataclasses.replace(config.scoring, recent_days=3650))
    ids = seed(connection, cfg, _Clock(), [("Associate Machine Learning Engineer", "Python.", {}),
                                           ("Machine Learning Engineer", "Python.", {})])
    for job_id in ids:
        connection.execute("UPDATE job_relevance SET similarity_ai = 0.7, similarity_fullstack = 0.6, "
                           "similarity_percentile = 0.5 WHERE job_id = ?", (job_id,))
    compute_final_scores(connection, cfg, clock=_Clock())
    rows = {r["job_id"]: r["final_score"] for r in connection.execute("SELECT job_id, final_score FROM job_relevance")}
    assert rows[ids[0]] - rows[ids[1]] == pytest.approx(config.scoring.junior_boost, abs=0.01)
    assert add_job is not None


@pytest.mark.parametrize("title,expected", [
    ("Software Engineering Fellow", UNSPECIFIED),
    ("Fellow Intern", JUNIOR),
    ("Technical Fellow", EXECUTIVE),
    ("Member of Technical Staff - ML Infra", UNSPECIFIED),
    ("Staff Software Engineer", SENIOR),
    ("Software Development Engineer I, L4", JUNIOR),
    ("Software Development Engineer L5", MID),
])
def test_fixes_found_in_real_titles(title: str, expected: str) -> None:
    assert classify_seniority(title).level == expected


def test_officer_suffix_rule_only_hits_bank_style_titles(config: Config) -> None:
    rules = compile_level_code_rules(config.relevance.seniority.level_code_rules)
    assert classify_seniority("Java Developer - Officer", level_code_rules=rules).level == SENIOR
    assert classify_seniority("Data Platform Engineer officer", level_code_rules=rules).level == SENIOR
    assert classify_seniority("Medical Data Officer", level_code_rules=rules).level == UNSPECIFIED


def test_officer_rule_sees_the_comma_in_the_raw_title(config: Config) -> None:
    rules = compile_level_code_rules(config.relevance.seniority.level_code_rules)
    assert classify_seniority("Artificial Intelligence & Machine Learning, Officer", level_code_rules=rules).level == SENIOR
