"""Deterministic language-quality checks (no model): at least 30 bad and good sentences, plus the voice-file stripping."""

from __future__ import annotations

import pytest

from jobhunt.writing.quality import check_quality, split_sentences
from jobhunt.writing.voice import MAX_SAMPLE_CHARS, clean_samples, ensure_template, load_voice


def rules(text: str, **kwargs) -> set[str]:  # noqa: ANN003
    return check_quality(text, **kwargs).rules


GOOD = [
    "I built a tool-using agent that answers portfolio questions across four tools.",
    "My campus assistant raised correct answers from 65% to 85% after I tuned its retrieval.",
    "The role needs someone who can test an API, and I have written those tests in pytest.",
    "Students use the portal every day, and caching keeps it quick during exams.",
    "I study Computer Science at the University and graduate in 2027.",
    "A short project can still show careful engineering.",
    "I would welcome a conversation about the role.",
    "Thank you for your time.",
    "At HeatBeasts I shipped React components with the core team.",
    "We reviewed each other's code before every release.",
    "Evidence matters more to me than claims.",
    "The faculty advisor reviewed my project, and it passed.",
    "The agent was evaluated against a 60-case suite.",
    "Pvt. Ltd. companies often ask for this, so I prepared an example.",
    "I use Docker to ship the service the same way each time.",
]


@pytest.mark.parametrize("sentence", GOOD)
def test_good_sentences_raise_no_warning(sentence: str) -> None:
    report = check_quality(sentence)
    assert report.ok, [(w.rule, w.detail) for w in report.warnings]


BAD = [
    ("I am writing in response to your advertisement for this position.", "filler_opener"),
    ("I am passionate about building agents for your team.", "cliche"),
    ("I am excited to leverage my skills in a dynamic environment.", "cliche"),
    ("You work in a fast-paced setting and I thrive there.", "cliche"),
    ("I am a team player with a proven track record.", "cliche"),
    ("With great enthusiasm I submit my application for review.", "filler_opener"),
    ("I built the agent and then I tested the agent with the agent tests and the agent was fast and the agent was safe "
     "and the agent was documented and the agent was deployed and the agent was monitored every day.", "sentence_too_long"),
    ("Portfolio questions across four tools and a market classifier.", "sentence_without_verb"),
    ("A fast and tidy campus portal with many small parts.", "sentence_without_verb"),
    ("I built a portal with caching and", "unfinished_sentence"),
    ("I built a portal for students, ", "unfinished_sentence"),
    ("I built a portal that uses the", "unfinished_sentence"),
    ("I built a portal.  It was used daily.", "double_space"),
    ("I would like to work with [Company Name] on this role.", "placeholder_left_in"),
    ("Dear {name}, I am applying for the role.", "placeholder_left_in"),
    ("AlphaAgent - AI Portfolio Agent: Built a tool-using LLM agent that routes across tools.", "pasted_resume_bullet"),
    ("BetaPortal - Student Portal: Designed the MongoDB schema and RESTful API layer.", "pasted_resume_bullet"),
    ("TBD", "placeholder_left_in"),
]


@pytest.mark.parametrize("sentence, rule", BAD)
def test_bad_sentences_raise_their_warning(sentence: str, rule: str) -> None:
    assert rule in rules(sentence), [(w.rule, w.detail) for w in check_quality(sentence).warnings]


def test_repeated_openers_words_and_phrases_are_found() -> None:
    openers = "I built a thing. I tested a thing. I shipped a thing. I wrote about the thing. Then it was done."
    assert "repeated_sentence_starter" in rules(openers)
    run = "I built a portal. I tested it well. I shipped it fast. Students used it daily."
    assert "repeated_sentence_starter" in rules(run)
    varied = "I built a portal. Testing came next. Then the release shipped. Students used it daily."
    assert "repeated_sentence_starter" not in rules(varied)
    word = "The deployment worked. Our deployment was safe. A deployment log helps. Each deployment was checked."
    assert "repeated_word" in rules(word)
    phrase = "The team ships features weekly. The team ships features again. The team ships features often."
    assert "repeated_phrase" in rules(phrase)


def test_length_rules_and_paragraph_endings() -> None:
    long = " ".join(["This sentence has exactly ten simple words in it today."] * 3)
    assert "average_sentence_too_long" not in rules(long)
    wordy = ("The portal that I built for students at my university during the second year of my degree was used by "
             "many people who needed grades and timetables and attendance records in one place every single day.")
    assert {"sentence_too_long", "average_sentence_too_long"} <= rules(wordy)
    assert "too_short" in rules("I built it.", min_words=150)
    assert "too_long" in rules("word " * 30 + ".", max_words=20)
    assert "unfinished_sentence" in rules("I built a portal.\n\nIt was used daily")
    assert rules("") == {"empty_text"}


def test_sentence_splitter_keeps_abbreviations_and_decimals_together() -> None:
    parts = split_sentences("I worked at HeatBeasts Esports Pvt. Ltd. on a B.Tech project. CGPA is 8.5 now.")
    assert len(parts) == 2 and parts[0].endswith("project.")
    assert len(split_sentences("One idea.\n\nTwo ideas. Three!")) == 3


def test_the_stats_are_reported() -> None:
    report = check_quality("I built a portal. Students use it daily.")
    assert report.stats["sentences"] == 2 and report.stats["words"] == 8 and report.stats["average_sentence_words"] == 4.0


# --- voice samples -----------------------------------------------------------------------------------------------------


RAW = """<!-- instructions that are never sent -->
I like small, honest projects and I say so plainly. Write to me at jordan.taylor@example.test or on +91 98765 43210.
See https://jordan.example.test/portfolio and www.jordan.dev and linkedin.com/in/jordantaylor, @jordan_taylor.
---
Thanks for reading,
Jordan Taylor
"""


def test_voice_samples_lose_contact_details_the_name_and_comments() -> None:
    samples = clean_samples(RAW, person_name="Jordan Taylor")
    assert samples.present and "small, honest projects" in samples.text
    for leaked in ("jordan.taylor@example.test", "98765", "jordan.example.test", "www.jordan.dev", "linkedin.com", "@jordan_taylor",
                   "Jordan", "Taylor", "instructions that are never sent"):
        assert leaked not in samples.text, leaked
    assert samples.removed.get("email") == 1 and samples.removed.get("phone") == 1 and samples.removed.get("url", 0) >= 2


def test_voice_samples_are_capped_and_an_empty_file_is_not_present() -> None:
    assert len(clean_samples("word " * 2000).text) <= MAX_SAMPLE_CHARS
    assert clean_samples("<!-- only a comment -->\n").present is False


def test_the_template_is_created_once_and_never_overwritten(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "data" / "profile" / "writing_samples.md"
    assert ensure_template(path) is True and path.is_file()
    path.write_text("my own words go here and stay here", encoding="utf-8")
    assert ensure_template(path) is False and "stay here" in path.read_text(encoding="utf-8")
    assert load_voice(path).present
