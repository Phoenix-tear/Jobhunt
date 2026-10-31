"""The relevance gate: deterministic verdicts, reasons, dates (fake clock) and retroactive recompute."""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import date, datetime, timezone
from typing import Any

import pytest

from jobhunt.config import Config
from jobhunt.matching.gate import (
    EXCLUDED,
    MAYBE,
    TARGET,
    GateJob,
    RelevanceGate,
    graduation_years,
    internship_term_ends,
    required_years,
    run_relevance,
)

TODAY = date(2026, 10, 14)


@pytest.fixture(scope="module")
def gate(config: Config) -> RelevanceGate:
    return RelevanceGate(config.relevance)


def judge(gate: RelevanceGate, title: str, description: str | None = "Build things in Python.", *,
          level: str = "unknown", employment: str = "full_time", today: date = TODAY, **extra: Any):
    job = GateJob(job_id=1, title=title, description=description, experience_level=level,
                  employment_type=employment, **extra)
    return gate.evaluate(job, today)


# --- titles -----------------------------------------------------------------------------------


@pytest.mark.parametrize("title,rule", [
    ("Java Developer", "hard_exclude_title"),
    ("Senior Java Engineer", "seniority_title"),
    ("Java Full Stack Developer", "hard_exclude_title"),
    ("Backend Engineer - Java", "primary_stack_title"),
    (".NET Developer", "hard_exclude_title"),
    ("C# Software Engineer", "hard_exclude_title"),
    ("PHP Developer", "hard_exclude_title"),
    ("SAP ABAP Consultant", "hard_exclude_title"),
    ("Mainframe COBOL Programmer", "hard_exclude_title"),
    ("Manual QA Engineer", "hard_exclude_title"),
    ("IT Support Specialist", "hard_exclude_title"),
    ("Embedded Firmware Engineer", "hard_exclude_title"),
    ("Sales Engineer", "hard_exclude_title"),
    ("Technical Recruiter", "hard_exclude_title"),
    ("Marketing Intern", "hard_exclude_title"),
    ("Electrical Engineer", "hard_exclude_title"),
])
def test_hard_excludes(gate: RelevanceGate, title: str, rule: str) -> None:
    decision = judge(gate, title)
    assert decision.status == EXCLUDED and decision.rule == rule
    assert decision.reason  # every exclusion explains itself


def test_java_a_plus_is_not_excluded(gate: RelevanceGate) -> None:
    decision = judge(gate, "Python Backend Engineer (Java a plus)")
    assert decision.status == TARGET and decision.role_family == "fullstack"
    soft = judge(gate, "Backend Engineer (Java a plus)")
    assert soft.status != EXCLUDED


def test_python_or_java_is_kept(gate: RelevanceGate) -> None:
    assert judge(gate, "Java / Python Engineer").status != EXCLUDED
    assert judge(gate, "Software Engineer", "Requirements: Python or Java. Java Spring a plus.").status == TARGET


@pytest.mark.parametrize("title,family", [
    ("Machine Learning Engineer", "ai_ml"),
    ("AI/ML Engineer", "ai_ml"),
    ("Applied AI Engineer", "ai_ml"),
    ("Generative AI Engineer", "ai_ml"),
    ("LLM Engineer", "ai_ml"),
    ("NLP Engineer", "ai_ml"),
    ("Data Scientist", "ai_ml"),
    ("Forward Deployed Engineer", "ai_ml"),
    ("Applied Scientist", "ai_ml"),
    ("AI Agent Engineer", "ai_ml"),
    ("Full Stack Engineer", "fullstack"),
    ("Software Engineer", "fullstack"),
    ("Software Engineering Intern", "fullstack"),
    ("SDE-1", "fullstack"),
    ("Backend Engineer (Python/FastAPI)", "fullstack"),
    ("Frontend Engineer, React", "fullstack"),
    ("Web Developer", "fullstack"),
    ("Python Developer", "fullstack"),
])
def test_target_families(gate: RelevanceGate, title: str, family: str) -> None:
    decision = judge(gate, title)
    assert decision.status == TARGET and decision.role_family == family, decision


def test_ai_wins_when_a_title_matches_both(gate: RelevanceGate) -> None:
    assert judge(gate, "AI Software Engineer").role_family == "ai_ml"


@pytest.mark.parametrize("title,detail", [
    ("DevOps Engineer", "devops"),
    ("Site Reliability Engineer", "devops"),
    ("Data Engineer", "data_engineering"),
    ("Android Developer", "mobile"),
    ("Salesforce Administrator", "salesforce"),
    ("Salesforce CRM Developer", "salesforce"),
])
def test_allowed_but_ranked_lower(gate: RelevanceGate, title: str, detail: str) -> None:
    decision = judge(gate, title)
    assert decision.status == MAYBE and decision.family_detail == detail
    assert decision.rank_adjust < 0


def test_salesforce_is_allowed_and_agentforce_is_ai_side(gate: RelevanceGate) -> None:
    ai_side = judge(gate, "Salesforce Agentforce Engineer")
    assert ai_side.status == TARGET and ai_side.role_family == "ai_ml" and ai_side.rank_adjust > 0
    assert ai_side.family_detail == "salesforce_ai"
    plain = judge(gate, "Salesforce Developer")
    assert plain.status == MAYBE and plain.rank_adjust < 0


def test_unrelated_titles_are_excluded_with_a_reason(gate: RelevanceGate) -> None:
    decision = judge(gate, "Office Coordinator")
    assert decision.status == EXCLUDED and decision.rule == "no_target_title"


def test_generic_title_is_decided_by_description_signals(gate: RelevanceGate) -> None:
    with_signals = judge(gate, "Associate Engineer", "You will write Python services and REST APIs on AWS.")
    assert with_signals.status == MAYBE and with_signals.confidence == "medium"
    without = judge(gate, "Associate Engineer", "You will operate the lathe and maintain tooling.")
    assert without.status == EXCLUDED and without.rule == "no_target_title"


# --- email-only jobs --------------------------------------------------------------------------


def test_email_only_job_is_gated_on_title_with_low_confidence(gate: RelevanceGate) -> None:
    target = judge(gate, "Machine Learning Engineer", None)
    assert target.status == TARGET and target.confidence == "low"
    generic = judge(gate, "Engineer", None)
    assert generic.status == MAYBE and generic.confidence == "low" and generic.rule == "generic_title_no_description"
    assert judge(gate, "Java Developer", "").confidence == "low"
    # description-only rules cannot fire without a description
    assert judge(gate, "Software Engineer", None).required_years is None


# --- years of experience ----------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("3+ years of experience in backend development", 3),
    ("Minimum of 5 years of professional experience", 5),
    ("2-4 years of experience with Python", 2),
    ("at least three years of hands-on experience", 3),
    ("5 to 7 years experience", 5),
    ("1 year of experience", 1),
    ("0-2 years of experience", 0),          # a range counts by its lower bound
    ("We were founded 10 years ago and have 20 years in business", None),
    ("Over the past 8 years we grew", None),
    ("No experience needed", None),
    ("1+ years of Python experience and 4+ years of overall industry experience", 4),
])
def test_required_years_parsing(text: str, expected: int | None) -> None:
    years, _ = required_years(text)
    assert years == expected


def test_two_years_excludes_and_one_is_a_stretch(gate: RelevanceGate) -> None:
    excluded = judge(gate, "Software Engineer", "Requires 3+ years of experience building APIs in Python.")
    assert excluded.status == EXCLUDED and excluded.rule == "required_years" and excluded.required_years == 3
    two = judge(gate, "Software Engineer", "Requires 2 years of professional experience in Python.")
    assert two.status == EXCLUDED and two.rule == "required_years" and two.required_years == 2
    assert judge(gate, "Software Engineer", "2-4 years of experience in Python.").status == EXCLUDED
    stretch = judge(gate, "Software Engineer", "Requires 1 year of professional experience in Python, 1-3 years total.")
    assert stretch.status == MAYBE and stretch.rule == "required_years_borderline"
    for fine_text in ("0-1 years of experience, Python.", "0-2 years of experience, Python.", "Freshers welcome."):
        assert judge(gate, "Software Engineer", fine_text).status == TARGET


def test_new_grad_and_intern_postings_skip_the_years_rule(gate: RelevanceGate) -> None:
    intern = judge(gate, "Software Engineer Intern", "Ideally 3+ years of coding experience (school projects count).",
                   level="intern", employment="internship")
    assert intern.status == TARGET
    grad = judge(gate, "Software Engineer, New Grad", "Typically 3 years of experience, or a degree.", level="new_grad")
    assert grad.status == TARGET


def test_mid_level_title_is_a_maybe(gate: RelevanceGate) -> None:
    decision = judge(gate, "Software Engineer", "Build things in Python.", level="mid")
    assert decision.status == MAYBE and decision.rule == "mid_level_title"
    assert judge(gate, "Software Engineer II", "Build things in Python.").rule == "seniority_title"


def test_senior_level_is_excluded(gate: RelevanceGate) -> None:
    decision = judge(gate, "Software Engineer", level="senior")
    assert decision.status == EXCLUDED and decision.rule == "seniority_level"


# --- primary stack from the description -------------------------------------------------------


def test_description_primary_stack(gate: RelevanceGate) -> None:
    java_only = judge(gate, "Backend Engineer",
                      "Requirements: 2 years of Java. Strong Java and Spring Boot. Experience with Java 17 microservices.")
    assert java_only.status == EXCLUDED and java_only.rule == "primary_stack_description"
    both = judge(gate, "Backend Engineer", "Requirements: Python or Java. Java experience with Spring.")
    assert both.status == TARGET
    plus = judge(gate, "Backend Engineer", "You will build APIs. Experience with Go. Java is a plus. Java/Kotlin a plus.")
    assert plus.status == TARGET
    incidental = judge(gate, "Backend Engineer", "You will build APIs in Go and integrate with SAP once.")
    assert incidental.status == TARGET


# --- dates (fake clock) -----------------------------------------------------------------------


def test_graduation_year_extraction() -> None:
    assert graduation_years("Class of 2026 and 2027") == {2026, 2027}
    assert graduation_years("2028 New Grad Program") == {2028}
    assert graduation_years("Graduating between December 2026 and June 2027") == {2026, 2027}
    assert graduation_years("Founded in 2015, we ship fast") == set()


@pytest.mark.parametrize("title,description,excluded", [
    ("Software Engineer, New Grad (Class of 2026)", "Join the class of 2026.", True),
    ("Software Engineer, New Grad", "For students graduating in 2028.", True),
    ("Software Engineer, New Grad", "For students graduating in 2027.", False),
    ("Software Engineer, New Grad", "Class of 2026 and 2027 graduates welcome.", False),
    ("Software Engineer, New Grad", "No class year stated.", False),
])
def test_new_grad_class_year(gate: RelevanceGate, title: str, description: str, excluded: bool) -> None:
    decision = judge(gate, title, description, level="new_grad")
    assert (decision.status == EXCLUDED) is excluded
    if excluded:
        assert decision.rule == "graduation_year"


def test_internship_terms_end_dates() -> None:
    assert internship_term_ends("Summer 2026 Intern") == [date(2026, 8, 31)]
    assert internship_term_ends("Fall 2026 internship") == [date(2026, 12, 31)]
    assert internship_term_ends("Winter 2026 Intern") == [date(2027, 2, 28)]
    assert internship_term_ends("Summer '27 SWE Intern") == [date(2027, 8, 31)]
    assert internship_term_ends("2027 Summer Internship") == [date(2027, 8, 31)]
    assert internship_term_ends("2026 internship program") == [date(2026, 12, 31)]
    assert internship_term_ends("Software Engineer Intern") == []


@pytest.mark.parametrize("title,today,excluded", [
    ("Software Engineering Intern - Summer 2026", date(2026, 10, 14), True),    # summer 2026 is over
    ("Software Engineering Intern - Summer 2027", date(2026, 10, 14), False),
    ("Software Engineering Intern - Fall 2026", date(2026, 10, 14), False),
    ("Software Engineering Intern - Winter 2026", date(2026, 10, 14), False),
    ("Software Engineering Intern (Off-Season)", date(2026, 10, 14), False),
    ("Software Engineering Intern - Summer 2026 or Fall 2026", date(2026, 10, 14), False),
    ("Software Engineering Intern - Fall 2026", date(2027, 1, 10), True),       # clock moved: fall ended
    ("Software Engineering Intern - Summer 2027", date(2027, 1, 10), False),
])
def test_internships_must_be_current_or_future(gate: RelevanceGate, title: str, today: date, excluded: bool) -> None:
    decision = judge(gate, title, "Python projects.", level="intern", employment="internship", today=today)
    assert (decision.status == EXCLUDED) is excluded, decision
    if excluded:
        assert decision.rule == "internship_season_over"


# --- persistence, incremental runs and retroactive config changes -----------------------------


def _insert(connection: sqlite3.Connection, n: int, title: str, **values: Any) -> int:
    row: dict[str, Any] = {
        "fingerprint": f"fp{n}", "title": title, "company_name": "Acme", "location_raw": "Pune, India",
        "first_seen_at": "2026-10-01T00:00:00Z", "last_seen_at": "2026-10-01T00:00:00Z", "status": "open",
        "description_text": "Python and APIs.", "description_hash": f"h{n}", "experience_level": "unknown",
        "employment_type": "full_time",
    }
    row.update(values)
    cursor = connection.execute(
        f"INSERT INTO jobs ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", list(row.values())
    )
    return int(cursor.lastrowid)


def _row(connection: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    return connection.execute("SELECT * FROM job_relevance WHERE job_id = ?", (job_id,)).fetchone()


def test_run_stores_reasons_and_is_incremental(connection: sqlite3.Connection, config: Config, clock) -> None:
    java = _insert(connection, 1, "Java Developer")
    ml = _insert(connection, 2, "Machine Learning Engineer")
    closed = _insert(connection, 3, "Software Engineer", status="closed")
    stats = run_relevance(connection, config, clock=clock)
    assert stats.computed == 2 and stats.by_status == {EXCLUDED: 1, TARGET: 1}

    assert _row(connection, java)["gate_status"] == EXCLUDED
    assert "excluded phrase" in _row(connection, java)["gate_reason"]
    assert _row(connection, ml)["role_family"] == "ai_ml"
    assert _row(connection, closed) is None            # closed jobs are not judged

    again = run_relevance(connection, config, clock=clock)
    assert again.computed == 0 and again.unchanged == 2

    connection.execute("UPDATE jobs SET description_hash = 'changed' WHERE id = ?", (ml,))
    changed = run_relevance(connection, config, clock=clock)
    assert changed.computed == 1


def test_config_change_recomputes_and_reopens_excluded_jobs(connection: sqlite3.Connection, config: Config, clock) -> None:
    php = _insert(connection, 1, "PHP Developer")
    run_relevance(connection, config, clock=clock)
    assert _row(connection, php)["gate_status"] == EXCLUDED

    relevance = config.relevance
    loosened = dataclasses.replace(
        config,
        relevance=dataclasses.replace(
            relevance,
            titles=dataclasses.replace(relevance.titles, exclude=tuple(p for p in relevance.titles.exclude if p != "php")),
            excluded_stacks={k: v for k, v in relevance.excluded_stacks.items() if k != "php"},
        ),
    )
    stats = run_relevance(connection, loosened, clock=clock)
    assert stats.computed == 1
    assert _row(connection, php)["gate_status"] == TARGET   # reopened, nothing was ever deleted
    assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_month_change_recomputes_internship_verdicts(connection: sqlite3.Connection, config: Config, clock) -> None:
    job = _insert(connection, 1, "Software Engineering Intern - Fall 2026",
                  experience_level="intern", employment_type="internship")
    run_relevance(connection, config, clock=clock)
    assert _row(connection, job)["gate_status"] == TARGET

    clock.current = datetime(2027, 1, 10, tzinfo=timezone.utc)
    run_relevance(connection, config, clock=clock)
    row = _row(connection, job)
    assert row["gate_status"] == EXCLUDED and row["gate_rule"] == "internship_season_over"


def test_downstream_columns_survive_a_gate_recompute(connection: sqlite3.Connection, config: Config, clock) -> None:
    job = _insert(connection, 1, "Machine Learning Engineer")
    run_relevance(connection, config, clock=clock)
    connection.execute("UPDATE job_relevance SET similarity_ai = 0.8, llm_score = 77 WHERE job_id = ?", (job,))
    run_relevance(connection, config, force=True, clock=clock)
    row = _row(connection, job)
    assert row["similarity_ai"] == 0.8 and row["llm_score"] == 77


# --- overnight fixes: pooled postings, language lists, generic security titles --------------------


def test_talent_pool_posting_is_not_judged_by_its_language_list(gate):
    description = ("Software Engineer Talent Pool (all grades). We're building a network of engineers for future "
                   "opportunities. One or more of: Industrial C#/.NET development, WPF desktop applications, "
                   "C#/.NET applications for inspection systems, .NET tooling.")
    verdict = judge(gate, "Software Engineer Talent Pool (all grades)", description)
    assert verdict.status != EXCLUDED


def test_language_inside_a_such_as_list_is_not_the_primary_stack(gate):
    description = ("Experience with object-oriented languages such as C#, Java or C++. "
                   "Familiarity with typed languages such as Java, C# or Go.")
    assert judge(gate, "Software Engineer", description).status != EXCLUDED


def test_a_required_language_outside_any_list_is_still_excluded(gate):
    description = "Required: strong C# development. You will write C# services daily. Deep .NET knowledge."
    verdict = judge(gate, "Software Engineer", description)
    assert verdict.status == EXCLUDED and verdict.rule == "primary_stack_description"


def test_security_engineer_building_tooling_is_a_maybe_not_excluded(gate):
    description = ("Build detection tooling and automation. Experience with Python, Go, C++, Rust. "
                   "Build ETL pipelines for logging. Manage services in a Kubernetes environment.")
    verdict = judge(gate, "Security Engineer (Detection Engineering)", description)
    assert verdict.status == MAYBE and verdict.rule == "generic_title_with_tech_signals"


def test_hardware_generic_title_with_one_script_mention_stays_excluded(gate):
    verdict = judge(gate, "Production Engineer (Electronics)",
                    "Own factory automation and test programming. Occasional Python scripts. Linux workstations.")
    assert verdict.status == EXCLUDED
