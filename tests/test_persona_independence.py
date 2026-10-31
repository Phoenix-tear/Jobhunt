"""Dual-profile persona independence and zero-leakage tests (Brief v11 Phase 5 / v13b).

Verifies that:
1. The engine functions person-agnostically via the Profile model.
2. Alex Chen (University of Waterloo, 2026, Systems & Data Infrastructure, Canada) produces
   distinct gate outcomes, rankings, tailored resumes, and prompts matching their profile.
3. Primary profile results on the same engine remain completely unaffected (zero bleed).
4. No PII, candidate blurbs, university names, or contact info leak between personas.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from jobhunt.config import Config
from jobhunt.matching.gate import run_relevance
from jobhunt.matching.location import LocationClassifier, INDIA, WRONG, run_location
from jobhunt.matching.profiles import build_profile
from jobhunt.matching.prompts import (
    build_score_user_text,
    build_classify_user_text,
    render_score_system,
    render_classify_system,
    JobForPrompt,
)
from jobhunt.matching.scoring import compute_final_scores
from jobhunt.matching.triage import render_triage_system
from jobhunt.profile import load_profile
from jobhunt.tailoring.engine import Tailor, load_bases, parse, ADVICE_SYSTEM, read_variants
from tests.conftest import FakeClock

DEMO_DIR = Path(__file__).parent / "fixtures" / "persona_demo"
JOBS_PATH = DEMO_DIR / "jobs.json"
PROFILE_PATH = DEMO_DIR / "profile.yaml"
INDIA_PROFILE_PATH = DEMO_DIR / "profile_india.yaml"


@pytest.fixture()
def demo_profile():
    return load_profile(PROFILE_PATH)


@pytest.fixture()
def primary_profile():
    """Synthetic second persona (India, class of 2027); never the owner's git-ignored profile."""
    return load_profile(INDIA_PROFILE_PATH)


@pytest.fixture()
def demo_jobs():
    return json.loads(JOBS_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def populated_db(connection: sqlite3.Connection, demo_jobs):
    """Seed the 40 synthetic jobs into the test database."""
    for j in demo_jobs:
        connection.execute(
            """INSERT INTO jobs (id, fingerprint, title, company_name, location_raw, country,
                               workplace_type, description_text, status, first_seen_at, last_seen_at, experience_level)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', '2026-10-25T00:00:00Z', '2026-10-25T00:00:00Z', ?)""",
            (
                j["id"],
                f"fp_{j['id']}",
                j["title"],
                j["company_name"],
                j["location_raw"],
                j.get("country"),
                j["workplace_type"],
                j["description_text"],
                j.get("experience_level", "entry"),
            ),
        )
    connection.commit()
    return connection


# --- 1. Profile Model Verification -----------------------------------------------------------------


def test_demo_profile_loads_distinct_fields(demo_profile, primary_profile):
    assert demo_profile.person.display_name == "Alex Chen"
    assert demo_profile.person.institution == "University of Waterloo"
    assert demo_profile.person.graduation_year == 2026
    assert demo_profile.location_policy.home_country == "Canada"

    # Primary profile verification
    assert primary_profile.person.graduation_year == 2027
    assert primary_profile.location_policy.home_country == "India"
    assert bool(primary_profile.person.institution)

    # Different content hashes
    assert demo_profile.content_hash != primary_profile.content_hash


# --- 2. Relevance Gate Independence ---------------------------------------------------------------


def test_relevance_gate_distinguishes_personas(populated_db, config, demo_profile, primary_profile, demo_jobs):
    clock = FakeClock()

    # Run relevance & location for Alex Chen
    run_relevance(populated_db, config, clock=clock, profile=demo_profile)
    run_location(populated_db, config, profile=demo_profile)
    alex_rows = {
        row["job_id"]: row
        for row in populated_db.execute("SELECT job_id, gate_status, gate_rule, gate_reason, location_fit FROM job_relevance")
    }

    # Run relevance & location for primary profile
    run_relevance(populated_db, config, clock=clock, profile=primary_profile)
    run_location(populated_db, config, profile=primary_profile)
    primary_rows = {
        row["job_id"]: row
        for row in populated_db.execute("SELECT job_id, gate_status, gate_rule, gate_reason, location_fit FROM job_relevance")
    }

    # Verify Canadian jobs (1001-1005): Home for Alex, wrong/unclear for primary profile
    for jid in (1001, 1002, 1003, 1004, 1005):
        assert alex_rows[jid]["location_fit"] == INDIA, f"Alex location failed for job {jid}"
    for jid in (1001, 1002, 1003, 1004):
        assert primary_rows[jid]["location_fit"] == WRONG, f"Primary location failed for job {jid}"
    assert primary_rows[1005]["location_fit"] == "remote_unclear"

    # Verify Indian jobs (1011-1015): Home for primary, wrong location for Alex
    for jid in (1011, 1012, 1013, 1014, 1015):
        assert primary_rows[jid]["location_fit"] == INDIA, f"Primary Indian job {jid} not marked home"
        assert alex_rows[jid]["location_fit"] == WRONG, f"Alex Chen should not match Indian job {jid}"

    # Verify Senior/Staff roles (1021-1030) are excluded for both students
    for jid in range(1021, 1031):
        assert alex_rows[jid]["gate_status"] == "excluded"
        assert primary_rows[jid]["gate_status"] == "excluded"

    # Verify graduation year gating independence:
    # Job 1006 specifies "2026 grads" -> Alex (2026) is target; primary (2027) is excluded by graduation_year
    assert alex_rows[1006]["gate_status"] == "target"
    assert primary_rows[1006]["gate_status"] == "excluded"
    assert primary_rows[1006]["gate_rule"] == "graduation_year"

    # Job 1011 specifies "2027 batch" -> primary (2027) is target; Alex (2026) is excluded by graduation_year
    assert primary_rows[1011]["gate_status"] == "target"
    assert alex_rows[1011]["gate_status"] == "excluded"
    assert alex_rows[1011]["gate_rule"] == "graduation_year"


# --- 3. Location Classifier Independence ----------------------------------------------------------


def test_location_classifier_respects_profile_home(config, demo_profile, primary_profile):
    alex_classifier = LocationClassifier(config, profile=demo_profile)
    primary_classifier = LocationClassifier(config, profile=primary_profile)

    # Waterloo, ON
    v_alex_waterloo = alex_classifier.classify(
        country="CA", location_raw="Waterloo, ON, Canada", workplace="onsite",
        eligible=None, scope=None, regions=[], work_auth=False,
    )
    v_primary_waterloo = primary_classifier.classify(
        country="CA", location_raw="Waterloo, ON, Canada", workplace="onsite",
        eligible=None, scope=None, regions=[], work_auth=False,
    )
    assert v_alex_waterloo.fit != WRONG  # Accepted in Canada for Alex
    assert v_primary_waterloo.fit == WRONG  # Rejected abroad for primary profile

    # Bengaluru, India
    v_alex_blr = alex_classifier.classify(
        country="IN", location_raw="Bengaluru, Karnataka, India", workplace="onsite",
        eligible=None, scope=None, regions=[], work_auth=False,
    )
    v_primary_blr = primary_classifier.classify(
        country="IN", location_raw="Bengaluru, Karnataka, India", workplace="onsite",
        eligible=None, scope=None, regions=[], work_auth=False,
    )
    assert v_alex_blr.fit == WRONG  # Rejected in India for Alex
    assert v_primary_blr.fit == INDIA  # Home in India for primary profile


# --- 4. Scoring Independence & Bleed Guard --------------------------------------------------------


def test_scoring_weights_and_penalties_from_profile(populated_db, config, demo_profile, primary_profile):
    # Set job 1001 as target with similarity
    populated_db.execute(
        """INSERT OR REPLACE INTO job_relevance
           (job_id, gate_status, gate_rule, gate_reason, role_family, rank_adjust, confidence,
            similarity_ai, similarity_percentile, required_years, computed_at, rules_hash, input_hash)
           VALUES (1001, 'target', 'title_ai_ml', 'AI/ML title', 'ai_ml', 0.0, 'high',
                   0.85, 0.90, 2, '2026-10-30T00:00:00Z', 'gate7_test', 'input_test')"""
    )
    populated_db.commit()

    # Compute for Alex Chen
    compute_final_scores(populated_db, config, profile=demo_profile)
    alex_row = populated_db.execute("SELECT final_score, score_components FROM job_relevance WHERE job_id = 1001").fetchone()
    alex_comps = json.loads(alex_row["score_components"])

    # Compute for primary profile
    compute_final_scores(populated_db, config, profile=primary_profile)
    primary_row = populated_db.execute("SELECT final_score, score_components FROM job_relevance WHERE job_id = 1001").fetchone()
    primary_comps = json.loads(primary_row["score_components"])

    # Alex gets penalized for 2 YOE (over 1 year max by 1 year -> penalty)
    assert alex_comps.get("years_points") == -10.0
    assert primary_comps.get("years_points") == -12.0
    # Weights reflect demo_profile scoring
    alex_w_sum = demo_profile.scoring.weight_fit + demo_profile.scoring.weight_similarity + demo_profile.scoring.weight_location
    primary_w_sum = primary_profile.scoring.weight_fit + primary_profile.scoring.weight_similarity + primary_profile.scoring.weight_location
    assert pytest.approx(alex_comps["weights"]["fit"], 0.001) == demo_profile.scoring.weight_fit / alex_w_sum
    assert pytest.approx(primary_comps["weights"]["fit"], 0.001) == primary_profile.scoring.weight_fit / primary_w_sum
    assert alex_row["final_score"] != primary_row["final_score"]


# --- 5. Tailoring Dry-Run & Non-Leakage ------------------------------------------------------------


def test_tailoring_renders_persona_identity_without_leakage(populated_db, config, demo_profile, primary_profile):
    # Build demo persona bases
    systems_tex = DEMO_DIR / "resume_systems.tex"
    data_tex = DEMO_DIR / "resume_data.tex"
    assert systems_tex.is_file() and data_tex.is_file()

    doc_systems = parse(systems_tex.read_text(encoding="utf-8"))
    doc_data = parse(data_tex.read_text(encoding="utf-8"))

    # Tailor job 1001 (CanCloud Systems) for Alex Chen
    scratch_dir = DEMO_DIR / "_test_scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    try:
        from jobhunt.tailoring.facts import extract, merge
        from jobhunt.tailoring.engine import Bases

        per = {
            "systems": extract("systems", doc_systems),
            "data_infra": extract("data_infra", doc_data),
        }
        merged = merge(per)
        bases = Bases(
            docs={"systems": doc_systems, "data_infra": doc_data},
            per_resume=per,
            merged=merged,
            ticked={f.id: True for f in merged},
        )

        tailor = Tailor(populated_db, config, compiler=None, bases=bases)
        res = tailor.tailor(1001, base="systems", dry_run_dir=scratch_dir)

        # Assertions on Alex's tailored resume
        assert res.tex, "Tailored LaTeX output should not be empty"
        assert "ALEX CHEN" in res.tex
        assert "University of Waterloo" in res.tex
        assert "Systems Design Engineering" in res.tex

        # Critical Non-Leakage: Primary user details must NEVER appear in Alex's resume
        if primary_profile.person.display_name and primary_profile.person.display_name != "the candidate":
            assert primary_profile.person.display_name not in res.tex
        if primary_profile.person.institution and primary_profile.person.institution != "University":
            assert primary_profile.person.institution not in res.tex
        assert "Fest" not in res.tex
    finally:
        import shutil
        shutil.rmtree(scratch_dir, ignore_errors=True)


# --- 6. Prompt Rendering Independence & Non-Leakage ----------------------------------------------


def test_prompts_render_cleanly_per_persona(demo_profile, primary_profile):
    prompt_job = JobForPrompt(
        title="Distributed Systems Engineer",
        company="CanCloud Systems",
        location="Waterloo, ON",
        workplace="onsite",
        remote_open_to_india="no",
        description="Build Raft consensus in Go.",
    )

    # Alex Chen prompt
    alex_system = render_score_system(profile=demo_profile)
    assert "University of Waterloo" in alex_system
    assert "Systems Design Engineering" in alex_system
    assert "2026" in alex_system
    # Non-leakage: Primary user's info not in Alex's prompt
    if primary_profile.person.display_name and primary_profile.person.display_name != "the candidate":
        assert primary_profile.person.display_name not in alex_system
    if primary_profile.person.institution and primary_profile.person.institution != "University":
        assert primary_profile.person.institution not in alex_system
    assert "2027" not in alex_system

    # Primary prompt
    primary_system = render_score_system(profile=primary_profile)
    assert "India" in primary_system
    assert "2027" in primary_system
    # Non-leakage: Alex's info not in primary prompt
    assert "Alex" not in primary_system
    assert "Waterloo" not in primary_system
    assert "2026" not in primary_system
    assert "Canada" not in primary_system

    # Triage system prompt
    alex_triage = render_triage_system(profile=demo_profile)
    primary_triage = render_triage_system(profile=primary_profile)
    assert "Canada" in alex_triage and "India" not in alex_triage
    assert "2026" in alex_triage and "2027" not in alex_triage
    assert "India" in primary_triage and "Canada" not in primary_triage
    assert "2027" in primary_triage and "2026" not in primary_triage
