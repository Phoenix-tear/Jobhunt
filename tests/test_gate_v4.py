"""v4 gate rules: games / low-code titles and non-English-only titles move a target to maybe, never to excluded."""

from __future__ import annotations

from datetime import date

import pytest

from jobhunt.config import Config
from jobhunt.matching.gate import EXCLUDED, MAYBE, TARGET, GateJob, RelevanceGate

TODAY = date(2026, 10, 14)


@pytest.fixture(scope="module")
def gate(config: Config) -> RelevanceGate:
    return RelevanceGate(config.relevance)


def judge(gate: RelevanceGate, title: str, *, level: str = "unknown", employment: str = "full_time"):
    job = GateJob(job_id=1, title=title, description="Build things in Python.", experience_level=level,
                  employment_type=employment)
    return gate.evaluate(job, TODAY)


@pytest.mark.parametrize("title", [
    "Gameplay Software Engineer Co-op",
    "C++ CGF Unreal Developer Intern",
    "Unity Game Developer",
    "Junior Game Developer",
    "Software Engineer Intern - Game Development",
])
def test_game_titles_become_maybe(gate: RelevanceGate, title: str) -> None:
    decision = judge(gate, title)
    assert decision.status == MAYBE and decision.rule == "off_domain_title"
    assert decision.family_detail == "game_dev"


@pytest.mark.parametrize("title", [
    "Junior Power Platform Developer",
    "MS Power Apps Developer",
    "D365 Developer",
    "Software Engineer Intern - Low Code",
])
def test_low_code_titles_become_maybe(gate: RelevanceGate, title: str) -> None:
    decision = judge(gate, title)
    assert decision.status == MAYBE and decision.rule == "off_domain_title"
    assert decision.family_detail == "low_code"


@pytest.mark.parametrize("title", [
    "Software Engineer Intern",
    "Backend Engineer",
    "Machine Learning Engineer",
    "Full Stack Developer",
    "AI Engineer - Game Analytics Platform Python",   # contains "game" but no off-domain phrase
    "Software Engineer, Games Infrastructure",        # "games" alone is not a rule
    "Python Developer",
])
def test_ordinary_titles_are_unchanged(gate: RelevanceGate, title: str) -> None:
    assert judge(gate, title).status == TARGET


def test_non_english_title_without_role_word_is_maybe(gate: RelevanceGate) -> None:
    decision = judge(gate, "计算机视觉工程师 (AI架构)")
    assert decision.status == MAYBE and decision.rule == "cjk_title_no_english_role"


def test_cjk_title_with_english_role_word_is_left_alone(gate: RelevanceGate) -> None:
    assert judge(gate, "AI 全栈工程师 Software Engineer").status == TARGET


def test_new_rules_never_promote_or_exclude(gate: RelevanceGate) -> None:
    # an excluded title stays excluded even when it names a game engine
    assert judge(gate, "Senior Unreal Engineer").status == EXCLUDED
    assert judge(gate, "Java Developer, Game Development").status == EXCLUDED
    # a non-software game title stays out of target (no promotion to maybe from excluded)
    assert judge(gate, "Game Artist").status != TARGET
