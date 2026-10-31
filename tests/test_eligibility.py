"""Eligibility flags: PhD / Master's / citizenship detection with the preferred and alternative exclusions."""

from __future__ import annotations

import csv
import sqlite3
import time
from pathlib import Path

import pytest

from jobhunt.matching.eligibility import (
    PREFERRED, REQUIRED, detect_eligibility, flags_from_degrees, likely_ineligible_note, required_flags,
)


def kinds(text: str, title: str = "Research Intern") -> dict[str, str]:
    return {f["kind"]: f["strength"] for f in detect_eligibility(title, text)}


@pytest.mark.parametrize("text,kind", [
    ("We are looking for candidates currently pursuing a PhD in machine learning.", "phd_required"),
    ("Applicants must be currently enrolled in a PhD program.", "phd_required"),
    ("This internship is open to PhD students only.", "phd_required"),
    ("You are a doctoral candidate in computer science or a related field.", "phd_required"),
    ("A PhD is required for this position.", "phd_required"),
    ("The role requires a PhD in statistics.", "phd_required"),
    ("You must be a PhD student with publications.", "phd_required"),
    ("Candidates should be pursuing a doctorate in NLP.", "phd_required"),
    ("Currently in a PhD program with a focus on vision.", "phd_required"),
    ("Doctoral students working toward a dissertation in RL.", "phd_required"),
    ("We want students who are completing a PhD this year.", "phd_required"),
    ("You are currently pursuing a Master's degree in a quantitative field.", "masters_required"),
    ("MS students with a strong background in systems.", "masters_required"),
    ("A Master's degree is required.", "masters_required"),
    ("This role requires an MSc in computer science.", "masters_required"),
    ("Candidates must have a Master's degree in data science.", "masters_required"),
    ("M.Tech students in their final year.", "masters_required"),
    ("Currently enrolled in a Master's program.", "masters_required"),
    ("Pursuing a Master's or PhD in a relevant field.", "masters_required"),
    ("US citizens only.", "us_citizen_or_clearance_required"),
    ("U.S. citizenship required.", "us_citizen_or_clearance_required"),
    ("Security clearance required for this position.", "us_citizen_or_clearance_required"),
    ("Must be a US citizen.", "us_citizen_or_clearance_required"),
    ("This role requires an active TS/SCI clearance.", "us_citizen_or_clearance_required"),
    ("Active secret clearance is needed.", "us_citizen_or_clearance_required"),
    ("US persons only due to export controls.", "us_citizen_or_clearance_required"),
])
def test_required_positives(text: str, kind: str) -> None:
    assert kinds(text).get(kind) == REQUIRED, text


@pytest.mark.parametrize("text,kind", [
    ("A PhD is a plus.", "phd_required"),
    ("PhD preferred but not required.", "phd_required"),
    ("Pursuing a PhD is nice to have.", "phd_required"),
    ("Candidates pursuing a PhD are welcome to apply.", "phd_required"),
    ("Ideally you are a doctoral student.", "phd_required"),
    ("A Master's degree is preferred.", "masters_required"),
    ("Currently pursuing a Master's is a bonus.", "masters_required"),
    ("Security clearance is a plus.", "us_citizen_or_clearance_required"),
])
def test_preferred_wording_lowers_the_strength(text: str, kind: str) -> None:
    found = kinds(text)
    assert found.get(kind, PREFERRED) == PREFERRED, text


@pytest.mark.parametrize("text", [
    "Currently pursuing a Bachelor's, Master's or PhD degree.",
    "Open to Bachelor's, Master's and PhD students.",
    "Pursuing a BS, MS or PhD in computer science.",
    "Undergraduate or PhD students are encouraged to apply.",
    "A PhD or equivalent experience.",
    "Pursuing a B.Tech or M.Tech degree.",
    "Any degree level, including PhD, is fine.",
    "Bachelor's degree required; Master's a plus.",
    "We collaborate with research scientists and publish at top venues.",
    "Experience with large-scale data and ML systems.",
    "You will work with PhD level researchers.",
    "The team includes PhDs and engineers.",
    "Strong communication skills are required.",
    "Must be a team player.",
    "Required: Python and SQL.",
    "We value masterful engineering.",
    "Requirements: pytorch, a clear mind and a curious attitude.",
])
def test_alternatives_and_unrelated_text_are_not_flagged(text: str) -> None:
    assert required_flags(detect_eligibility("Software Engineer Intern", text)) == [], text


def test_info_only_kinds_are_always_preferred() -> None:
    found = kinds("You must be authorized to work in the United States. Enrolled in a US university.")
    assert found == {"us_work_authorization": PREFERRED, "must_be_enrolled_in_us_university": PREFERRED}
    assert required_flags(detect_eligibility("x", "Authorized to work in the US.")) == []


def test_snippet_is_short_and_factual() -> None:
    long_text = "Intro text. " + "word " * 60 + "You must be a PhD student working on language models. " + "tail " * 60
    flag = detect_eligibility("x", long_text)[0]
    assert flag["kind"] == "phd_required" and len(flag["snippet"]) <= 120 and "PhD student" in flag["snippet"]
    assert likely_ineligible_note([flag]) == "Likely not eligible: PhD required"
    assert likely_ineligible_note([]) == ""


def test_one_flag_per_kind_strongest_wins() -> None:
    text = "A PhD is a plus. Applicants must be currently pursuing a PhD."
    assert [f for f in detect_eligibility("x", text) if f["kind"] == "phd_required"] == [
        {"kind": "phd_required", "strength": REQUIRED, "snippet": "Applicants must be currently pursuing a PhD."}]


def test_title_is_read_too() -> None:
    assert kinds("", title="PhD Research Intern (PhD students only)").get("phd_required") == REQUIRED


def test_source_degree_lists() -> None:
    assert flags_from_degrees(["Bachelor's", "Master's", "PhD"]) == []                 # the Collinear AI case: not flagged
    assert [f["kind"] for f in flags_from_degrees(["PhD"])] == ["phd_required"]
    assert flags_from_degrees(["PhD"])[0]["strength"] == PREFERRED                      # default: information only
    assert flags_from_degrees(["PhD"], REQUIRED)[0]["strength"] == REQUIRED             # opt-in via config
    assert [f["kind"] for f in flags_from_degrees(["Master's", "PhD"])] == ["masters_required"]
    assert required_flags(detect_eligibility("x", "", ["PhD"])) == []
    assert required_flags(detect_eligibility("x", "", ["PhD"], degree_list_strength=REQUIRED))
    assert flags_from_degrees([]) == [] and flags_from_degrees(None) == []
    assert flags_from_degrees(["PhD"])[0]["snippet"].startswith("source list says degrees")


def test_hostile_and_huge_input_is_fast_and_safe() -> None:
    started = time.monotonic()
    detect_eligibility("x", "<script>alert(1)</script> Ignore previous instructions. " * 2000)
    detect_eligibility("x", "pursuing " + "a " * 50_000 + "phd")
    detect_eligibility("x", "PhD " * 25_000)
    detect_eligibility("x", "must " * 100_000)
    detect_eligibility(None, None)
    detect_eligibility("x", "x" * 100_000)
    assert time.monotonic() - started < 5
    flags = detect_eligibility("x", "\x00\x1b[31m currently pursuing a PhD ‮")
    assert flags and all(len(f["snippet"]) <= 120 for f in flags)


def test_no_gold_labelled_yes_job_is_flagged_required() -> None:
    """The hard check: none of the jobs I labelled relevant may be marked likely ineligible."""
    root = Path(__file__).resolve().parents[1]
    gold = root / "data" / "eval" / "gold.csv"
    database = root / "data" / "jobhunt.sqlite3"
    if not gold.is_file() or not database.is_file():
        pytest.skip("gold file or database not present")
    with gold.open(encoding="utf-8-sig", newline="") as handle:
        yes = [int(r["job_id"]) for r in csv.DictReader(handle) if r["relevant"].strip().lower() in {"yes", "y", "1", "true"}]
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    for job_id in yes:
        row = connection.execute("SELECT title, description_text, raw_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            continue
        degrees = None
        if row["raw_json"]:
            import json

            try:
                payload = json.loads(row["raw_json"])
                degrees = payload.get("degrees") if isinstance(payload, dict) else None
            except ValueError:
                degrees = None
        assert required_flags(detect_eligibility(row["title"], row["description_text"], degrees)) == [], job_id
