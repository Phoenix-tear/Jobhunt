"""Required-years parser: lower bounds, ranges, number words, and the non-work uses it must ignore."""

from __future__ import annotations

import pytest

from jobhunt.matching.years import required_years

# (sentence, expected lower bound; None = no requirement stated, 0 = explicitly none)
REQUIRED = [
    ("0-2 years of experience in software development", 0),
    ("0 - 1 years of experience", 0),
    ("1-3 years of relevant experience", 1),
    ("2-4 years of experience building web apps", 2),
    ("2+ years of experience with Python", 2),
    ("2 plus years of professional experience", 2),
    ("3+ years of hands-on experience with React", 3),
    ("3 yrs experience in backend development", 3),
    ("5+yrs experience", 5),
    ("minimum 2 years of experience", 2),
    ("Minimum of two years of experience in a similar role", 2),
    ("at least two years of experience", 2),
    ("At least 3 years' experience with distributed systems", 3),
    ("two years of experience", 2),
    ("three years of professional experience", 3),
    ("2 to 3 years of experience", 2),
    ("two to three years of experience", 2),
    ("1 to 2 years of industry experience", 1),
    ("3-5 yrs of experience", 3),
    ("3 – 5 years of experience", 3),
    ("Requires 4 years of experience in software engineering", 4),
    ("You have 2+ years experience shipping production services", 2),
    ("Experience: 3 years or more in backend roles", 3),
    ("3 or more years of experience", 3),
    ("10+ years of experience leading teams", 10),
    ("three (3) years of experience", 3),
    ("24 months of experience in a related field", 2),
    ("one year of experience with Java", 1),
    ("a year of relevant experience is required", 1),
    ("1+ year of experience in a professional setting", 1),
    ("Proven track record of 5 years in the industry", 5),
    ("Bachelor's degree and 2 years of work experience", 2),
    ("Looking for someone with 4+ years of experience", 4),
    ("Strong background (3+ years) in machine learning", 3),
    ("minimum 3 years in software development", 3),
    ("experience of 6 years in cloud engineering", 6),
    ("The role needs 2.5 years of experience", 2),
    ("Must have at least five years of hands-on experience", 5),
    # upper bounds and fresher wording: nothing required
    ("up to 2 years of experience", 0),
    ("Less than 2 years of experience is fine", 0),
    ("0-6 months experience", None),
    ("no more than 1 year of experience", 0),
    ("Freshers welcome, no experience needed", None),
    # not a work requirement
    ("Recent graduates within 2 years of graduating", None),
    ("You graduated within the last 2 years", None),
    ("2 years of coursework in computer science", None),
    ("a 2-year degree in engineering", None),
    ("Python for 2 years", None),
    ("We have been in business for 10 years", None),
    ("Founded 12 years ago", None),
    ("This is a 2 year contract", None),
    ("a two year fixed term experience programme", None),
    ("over the past 3 years the team has grown", None),
    ("Must be 18 years old", None),
    ("4 years of university education", None),
    ("The program runs for 2 years with rotations", None),
    ("In the next 5 years we will expand, experience of growth", None),
    # softened: preferred, not required
    ("3+ years of experience preferred", None),
    ("5 years of experience is a plus", None),
    ("Ideally 4 years of experience", None),
    ("Nice to have: 3 years of experience with Go", None),
    # no experience wording
    ("2 years", None),
    ("Our office is 3 years old", None),
    ("", None),
]


@pytest.mark.parametrize(("text", "expected"), REQUIRED)
def test_required_years_sentences(text: str, expected: int | None) -> None:
    years, phrase = required_years(text)
    assert years == expected, (text, years, phrase)
    assert (phrase is None) == (years is None)


def test_there_are_at_least_sixty_sentences() -> None:
    assert len(REQUIRED) >= 60


def test_largest_lower_bound_wins_across_a_description() -> None:
    text = "Requirements\n- 2+ years of experience with Python\n- 4+ years of experience with SQL\n- Docker is a plus"
    assert required_years(text)[0] == 4


def test_soft_wording_only_silences_its_own_bullet() -> None:
    text = "- 3+ years of experience preferred\n- Minimum 2 years of experience with Java"
    assert required_years(text)[0] == 2


def test_flattened_description_with_dash_bullets() -> None:
    text = ("Key skills - Strong expertise in Java - Hands-on experience with ReactJS - 3+ years of experience preferred "
            "- Minimum 1 year of experience in banking")
    assert required_years(text)[0] == 1


def test_title_with_years_counts() -> None:
    assert required_years("", title="Backend Engineer (3+ years)")[0] == 3
    assert required_years("", title="Backend Engineer, Payments")[0] is None


def test_abbreviation_period_does_not_split_the_phrase() -> None:
    assert required_years("2+ yrs. of experience in testing")[0] == 2


def test_description_none_or_hostile() -> None:
    assert required_years(None) == (None, None)
    assert required_years("experience " * 20000)[0] is None


def test_a_requirements_heading_makes_bullets_without_experience_wording_count() -> None:
    text = "About us\nWe have been around for 26 years.\nWhat you bring\n- 3+ years shipping production software\n- Python"
    assert required_years(text) == (3, "3+ years")
    flat = "About the role - Build things. What you'll bring - 4 to 7 years in Growth, BizOps, Product. Benefits - Gym"
    assert required_years(flat)[0] == 4


def test_the_same_bullet_outside_a_requirements_section_does_not_count() -> None:
    text = "About us\n- Twenty-six years of runway\n- 15+ years of history in banking\nBenefits\n- 3+ years of free lunch"
    assert required_years(text)[0] is None


def test_headings_close_the_requirements_section() -> None:
    text = "Requirements\n- 2+ years shipping software\nWhat we offer\n- 10+ years of pension plan\nAbout us\n- 30+ years in the business"
    assert required_years(text)[0] == 2


def test_prose_that_merely_uses_the_word_requirements_does_not_open_a_section() -> None:
    text = "You build features that meet the client's compliance requirements. We have served banks for 12+ years."
    assert required_years(text)[0] is None


def test_hyphenated_number_words_are_not_ranges() -> None:
    assert required_years("Requirements: twenty-six years of runway")[0] is None
    assert required_years("Minimum five years of experience")[0] == 5


@pytest.mark.parametrize(("text", "expected"), [
    ("PDT Partners has a 30+ year track record and a reputation for excellence", None),
    ("Our model is physician-led, drawing upon more than 15 years of crisis care expertise", None),
    ("Barclays' clients receive services from a provider for a minimum of ten years", None),
    ("You may be a good fit if you - Have 8+ years building secure IT systems", 8),
    ("10+ years of experience leading platform teams", 10),
    ("25+ years of experience in the industry", 25),
])
def test_company_age_is_not_a_requirement(text: str, expected: int | None) -> None:
    assert required_years(text)[0] == expected
