"""Tests verifying v6b follow-up requirements: Greenhouse 90-day window and Barclays company rules (Brief v14 Section 3)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jobhunt.config import load_config
from jobhunt.freshness import classify_freshness, stale_threshold
from jobhunt.matching.company_rules import CompanyRuleSet, load_company_rules
from jobhunt.timeutil import utc_now


def test_greenhouse_90_day_stale_threshold() -> None:
    config = load_config()
    now = utc_now()

    # (a) Verify Greenhouse threshold is 90 days vs default 45 days
    gh_threshold = stale_threshold(config.freshness, ["greenhouse"], early_career=False)
    lever_threshold = stale_threshold(config.freshness, ["lever"], early_career=False)

    assert gh_threshold == 90
    assert lever_threshold == 45

    # 60 days old: Greenhouse is aging (not stale), Lever is stale
    posted_60d = (now - timedelta(days=60)).isoformat()
    gh_freshness = classify_freshness(
        posted_at=posted_60d,
        first_seen_at=posted_60d,
        sources=["greenhouse"],
        settings=config.freshness,
        now=now,
    )
    lever_freshness = classify_freshness(
        posted_at=posted_60d,
        first_seen_at=posted_60d,
        sources=["lever"],
        settings=config.freshness,
        now=now,
    )

    assert gh_freshness.band == "aging"
    assert gh_freshness.stale_after_days == 90

    assert lever_freshness.band == "stale"
    assert lever_freshness.stale_after_days == 45


SYNTHETIC_COMPANY_RULES = Path(__file__).parent / "fixtures" / "company_rules_synthetic.yaml"


def test_barclays_company_rules_with_deny_text_patterns_synthetic_fixture() -> None:
    """Runs everywhere: uses the committed synthetic rules, not the git-ignored data/ file."""
    _check_barclays_rules(SYNTHETIC_COMPANY_RULES)


def test_barclays_company_rules_with_deny_text_patterns() -> None:
    rules_path = load_config().resolve_path("data/profile/company_rules.yaml")
    if not rules_path.is_file():
        pytest.skip("data/profile/company_rules.yaml is git-ignored and absent in this checkout")
    _check_barclays_rules(rules_path)


def _check_barclays_rules(rules_path: Path) -> None:
    rules = load_company_rules(rules_path)
    barclays = next((r for r in rules if r.company.lower() == "barclays"), None)
    assert barclays is not None, "Barclays rule must exist in company_rules.yaml"

    # Verify BA4 allow
    assert "BA4" in barclays.allow_level_codes

    # Verify deny title patterns
    assert "Assistant Vice President" in barclays.deny_title_patterns
    assert "Vice President" in barclays.deny_title_patterns
    assert "Director" in barclays.deny_title_patterns

    # Verify deny text patterns
    assert "Assistant Vice President Expectations" in barclays.deny_text_patterns
    assert "Vice President Expectations" in barclays.deny_text_patterns

    rule_set = CompanyRuleSet([barclays])

    # 1. Generic title without AVP/VP markers -> unverified (maybe)
    v1 = rule_set.judge("Barclays", "Software Developer", "General software engineering role")
    assert v1.outcome == "unverified"

    # 2. Generic title with AVP Expectations in text -> deny (excluded)
    v2 = rule_set.judge("Barclays", "Software Developer", "Assistant Vice President Expectations: leadership skills")
    assert v2.outcome == "deny"
    assert v2.matched == "Assistant Vice President Expectations"

    # 3. Generic title with VP Expectations in text -> deny (excluded)
    v3 = rule_set.judge("Barclays", "Software Developer", "Vice President Expectations: manage team")
    assert v3.outcome == "deny"
    assert v3.matched == "Vice President Expectations"

    # 4. Title with Director -> deny (excluded)
    v4 = rule_set.judge("Barclays", "Director - Technology", "Manage engineering")
    assert v4.outcome == "deny"

    # 5. Title with Analyst or BA4 code -> allowed
    v5 = rule_set.judge("Barclays", "Analyst - Software Developer", "Build web tools")
    assert v5.outcome == "allowed"

    v6 = rule_set.judge("Barclays", "Technology Developer (BA4)", "Build microservices")
    assert v6.outcome == "allowed"

