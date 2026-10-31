import pytest
from pydantic import ValidationError
from jobhunt.profile import (
    Person,
    LevelPolicy,
    RoleFamily,
    BaseResume,
    LocationPolicy,
    ScoringPolicy,
    Profile,
    get_profile,
    load_profile,
    build_legacy_profile,
)

def test_legacy_profile_structure():
    profile = build_legacy_profile()
    assert profile.person.graduation_year == 2027
    assert profile.person.career_stage == "student"
    assert profile.level_policy.max_required_years == 2
    assert profile.level_policy.junior_boost == 6.0
    assert len(profile.role_families) == 7
    assert len(profile.bases) == 3
    assert profile.authoritative_base == "ai"
    assert profile.location_policy.home_country == "India"
    assert profile.scoring.weight_fit == 0.55
    assert profile.content_hash is not None

def test_profile_immutability():
    profile = build_legacy_profile()
    with pytest.raises(ValidationError):
        profile.person.graduation_year = 2028  # type: ignore

def test_profile_loader_and_accessor():
    profile = get_profile()
    assert profile is not None
    assert profile.person.display_name == "the candidate"
    assert profile.render_scoring_blurb() != ""
    assert profile.render_classify_blurb() != ""
    assert profile.render_triage_blurb() != ""

def test_content_hash_stable():
    p1 = build_legacy_profile()
    p2 = build_legacy_profile()
    assert p1.content_hash == p2.content_hash
