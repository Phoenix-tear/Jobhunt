"""v6 title additions: each new pattern has positive and negative cases; the years policy at the gate."""

from __future__ import annotations

from datetime import date

import pytest

from jobhunt.config import Config
from jobhunt.matching.gate import EXCLUDED, MAYBE, TARGET, GateJob, RelevanceGate
from jobhunt.matching.seniority import CAUTION, EXECUTIVE, JUNIOR, MID, SENIOR, UNSPECIFIED, classify_seniority

TODAY = date(2026, 10, 21)

NEW_CASES = [
    # hyphenated / abbreviated vice presidents
    ("Vice-President, Technology", EXECUTIVE),
    ("Assistant Vice-President - Java Developer", EXECUTIVE),
    ("Asst. Vice President, Engineering", EXECUTIVE),
    ("Associate Vice-President, Data", EXECUTIVE),
    ("Vice Principal Engineer", SENIOR),                      # contains "principal"; not read as a vice president
    # snr
    ("Snr Software Engineer", SENIOR),
    ("Snr. Data Scientist", SENIOR),
    ("Sneaker Software Engineer", UNSPECIFIED),
    # team leader
    ("Team Leader - Platform Engineering", SENIOR),
    ("Technical Leader, ML", SENIOR),
    ("Leaders Program Software Engineer Intern", JUNIOR),
    ("Leadership Development Programme Graduate", JUNIOR),
    # member of technical staff levels
    ("Member of Technical Staff 2", MID),
    ("Member of Technical Staff III", MID),
    ("Member of Technical Staff", UNSPECIFIED),
    ("Member of Technical Staff Intern", JUNIOR),
    # specialist / L4 are cautions, not exclusions
    ("Software Specialist", CAUTION),
    ("Data Specialist - ML", CAUTION),
    ("Associate Specialist Developer", JUNIOR),
    ("Senior Specialist", SENIOR),
    ("Software Engineer L4", CAUTION),
    ("Software Engineer L5", MID),
    ("Software Engineer L40", UNSPECIFIED),
    ("SDE I L4", JUNIOR),
    # bare "Associate" is not excluded by itself
    ("Associate", UNSPECIFIED),
    ("Associate Software Engineer", JUNIOR),
    ("Associate Vice President", EXECUTIVE),
    # numeric / roman levels already covered, kept as regression
    ("Software Engineer 3", MID),
    ("Engineer 3", MID),
    ("SDE-2", MID),
    ("SDET II", MID),
    ("Software Engineer I", JUNIOR),
    ("Software Engineer I/II", JUNIOR),
]


@pytest.mark.parametrize(("title", "expected"), NEW_CASES)
def test_new_title_patterns(title: str, expected: str) -> None:
    assert classify_seniority(title).level == expected, (title, classify_seniority(title))


@pytest.fixture(scope="module")
def gate(config: Config) -> RelevanceGate:
    return RelevanceGate(config.relevance)


def _judge(gate: RelevanceGate, title: str, description: str = "Build Python services.", **extra):  # noqa: ANN001, ANN202
    return gate.evaluate(GateJob(job_id=1, title=title, description=description, **extra), TODAY)


@pytest.mark.parametrize("title", ["Vice-President, Software Engineering", "Snr Software Engineer",
                                   "Team Leader - Software Engineering", "Software Engineer, Member of Technical Staff 3"])
def test_gate_excludes_the_new_senior_titles(gate: RelevanceGate, title: str) -> None:
    assert _judge(gate, title).status == EXCLUDED


def test_specialist_is_capped_at_maybe_not_excluded(gate: RelevanceGate) -> None:
    decision = _judge(gate, "Software Engineer Specialist")
    assert decision.status == MAYBE and decision.rule == "seniority_caution"
    assert decision.seniority_level == CAUTION
    assert _judge(gate, "Software Engineer L4").status == MAYBE


def test_bare_associate_is_not_excluded_and_years_decide(gate: RelevanceGate) -> None:
    assert _judge(gate, "Associate Software Engineer", "0-2 years of experience").status == TARGET
    decision = _judge(gate, "Associate Software Engineer", "2-4 years of experience in Python development")
    assert decision.status == EXCLUDED and decision.rule == "required_years" and decision.required_years == 2
    assert _judge(gate, "Associate Vice President, Software Engineering").status == EXCLUDED


@pytest.mark.parametrize(("text", "status"), [
    ("2+ years of experience", EXCLUDED),
    ("3+ years of experience", EXCLUDED),
    ("2-4 years of experience", EXCLUDED),
    ("minimum 2 years of experience", EXCLUDED),
    ("1-3 years of experience", MAYBE),
    ("1+ years of experience", MAYBE),
    ("0-1 years of experience", TARGET),
    ("0-2 years of experience", TARGET),
    ("fresher welcome", TARGET),
    ("within 2 years of graduating", TARGET),
    ("2 years of coursework", TARGET),
    ("preferred: 3 years of experience", TARGET),
])
def test_years_policy_at_the_gate(gate: RelevanceGate, text: str, status: str) -> None:
    assert _judge(gate, "Software Engineer", f"Build Python services. {text}.").status == status


def test_explicit_internship_still_skips_the_years_rule(gate: RelevanceGate) -> None:
    decision = _judge(gate, "Software Engineer Intern", "Ideally 3+ years of coding", employment_type="internship",
                      experience_level="intern")
    assert decision.status == TARGET


@pytest.mark.parametrize("title", [
    "2027 Technology Developer Degree Apprenticeship Programme", "Graduate Software Engineer", "Software Engineer Trainee",
    "Early Careers Software Engineer", "Software Engineering Intern",
])
def test_early_career_titles_skip_the_years_rule(gate: RelevanceGate, title: str) -> None:
    decision = _judge(gate, title, "Requirements - 3+ years of experience in software engineering. 3 year residence requirement.")
    assert decision.status != EXCLUDED, (title, decision)


def test_a_senior_word_still_wins_over_an_early_career_word(gate: RelevanceGate) -> None:
    assert _judge(gate, "Senior Graduate Programme Lead Software Engineer").status == EXCLUDED
