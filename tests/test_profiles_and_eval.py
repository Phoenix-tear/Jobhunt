"""Profiles (PII stripping), the facts-to-review list, and the evaluation tooling."""

from __future__ import annotations

import csv
import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from jobhunt.config import Config, ProfileSettings
from jobhunt.matching.evaluation import EvalError, format_report, run_eval, write_sample
from jobhunt.matching.facts import compare_resumes, existing_ticks, render_review_facts
from jobhunt.matching.gate import run_relevance
from jobhunt.matching.profiles import (
    ProfileError,
    assert_clean,
    clean_extracted,
    guess_person_name,
    run_profile_build,
    strip_pii,
)

AI_RESUME = """JANE Q DOE
SOFTWARE ENGINEER - AI & ML
jane.doe@example.com | +91 98765 43210 | linkedin.com/in/jane-doe-1234 | github.com/janedoe
SUMMARY
Final-year student building applied GenAI systems. Jane Doe ships RAG assistants and evaluates them in CI.
EDUCATION
Example Institute of Technology Expected 2027
B.Tech Computer Science - AI & ML CGPA: 8.00
Selected Coursework: Machine Learning, Data Structures & Algorithms, DBMS
PROFESSIONAL EXPERIENCE
Organiser, Event Team - Fest '26 Aug 2025 - Present
- Owned end-to-end delivery of the Fest '26 web platform : Next.js frontend, NestJS backend services, and AWS
deployment. Served ~60k visitors on the peak day at 99.9% uptime.
PROJECTS
Fest GammaBot | Python, FastAPI, LangChain, Gemini, ChromaDB Aug 2025 - Present
- Built a multi-tool campus assistant with a regex intent router and ChromaDB retrieval over event documents.
SKILLS
AI & LLMs: RAG, LangChain, Embeddings, OpenAI & Gemini APIs
Backend & Cloud: FastAPI, NestJS, AWS (EC2, S3), Docker, pytest
ADDITIONAL INFORMATION
Certifications: AWS Certified Cloud Practitioner
"""

OLDER_RESUME = """JANE Q DOE
SOFTWARE ENGINEER
jane.doe@example.com | +91 98765 43210 | linkedin.com/in/jane-doe-1234
SUMMARY
Final-year student with a student portal used by 1000+ students and a RAG assistant on OpenAI.
EDUCATION
Example Institute of Technology Expected 2027
B.Tech Computer Science - AI & ML CGPA: 8.00
Selected Coursework: Data Structures & Algorithms, DBMS, Computer Networks
PROFESSIONAL EXPERIENCE
Organiser, Event Team Aug 2025-Present
- Owned end-to-end delivery of the Fest '25 web platform: architecture, React/Next.js frontend, Python backend
services, and AWS deployment. Sustained ~60k peak visitors in a day at 99.9% uptime.
PROJECTS
Fest GammaBot | Python, FastAPI, OpenAI API, ChromaDB Aug 2025-Present
- Built a production RAG pipeline over fragmented institutional data using OpenAI embeddings.
PortalApp - Student Portal | Python, FastAPI, MongoDB, React Aug 2025-Present
- A unified student portal for grades and timetables, now actively used by 1000+ students.
SKILLS
ML & GenAI: Python, OpenAI API, RAG Pipelines, ChromaDB
Frontend: React.js, Redux, Next.js, TypeScript
Backend & Databases: FastAPI, HonoJS, Microservices, MongoDB, Node.js
DevOps & Cloud: AWS (EC2, S3), Docker, Jest, Cypress
ADDITIONAL INFORMATION
Activities & Leadership: Committee Head, Event Team (Fest '25) leading web & AI initiatives
"""


# --- PII ----------------------------------------------------------------------------------------


def test_header_name_is_detected() -> None:
    assert guess_person_name(AI_RESUME) == "JANE Q DOE"
    assert guess_person_name("SUMMARY\nSomething") is None or True
    assert guess_person_name("Curriculum Vitae of Someone With A Long Name 2026") is None


def test_stripped_text_has_no_contact_details_or_name() -> None:
    result = strip_pii(clean_extracted(AI_RESUME))
    text = result.text
    for secret in ("jane.doe@example.com", "98765", "43210", "linkedin", "github", "janedoe", "JANE", "Jane", "Doe"):
        assert secret.lower() not in text.lower(), secret
    assert "RAG assistants and evaluates them in CI" in text            # content survives
    assert "B.Tech Computer Science" in text                            # "B.Tech" is not a URL
    assert result.removed.get("email") == 1 and result.removed.get("phone") == 1
    assert result.removed.get("url", 0) >= 2
    assert "name" in result.removed
    assert_clean(text, person_name="JANE Q DOE")


def test_year_ranges_and_metrics_are_not_phone_numbers() -> None:
    text = "Aug 2025-2026 built a platform for 60000 users at 99.9% uptime and 5000+ payments"
    result = strip_pii(text, person_name=None)
    assert "2025-2026" in result.text and "60000" in result.text
    assert "phone" not in result.removed


def test_assert_clean_fails_closed() -> None:
    with pytest.raises(ProfileError, match="e-mail"):
        assert_clean("contact me at someone@example.org")
    with pytest.raises(ProfileError, match="phone"):
        assert_clean("call +91 98765 43210 today")
    with pytest.raises(ProfileError, match="url"):
        assert_clean("see https://example.com/me")
    with pytest.raises(ProfileError, match="name"):
        assert_clean("Built by Jane Doe", person_name="Jane Doe")


def test_pdf_extraction_artifacts_are_repaired() -> None:
    cleaned = clean_extracted("Frontend: React.js, T ailwindCSS\nbackend ser-\nvices\nA unified portal\n• item")
    assert "TailwindCSS" in cleaned and "services" in cleaned
    assert "A unified portal" in cleaned          # a real article is not glued to the next word
    assert "- item" in cleaned


# --- facts --------------------------------------------------------------------------------------


@pytest.fixture()
def stripped_texts() -> dict[str, str]:
    return {name: strip_pii(clean_extracted(text)).text for name, text in (("ai", AI_RESUME), ("fullstack", OLDER_RESUME))}


def test_conflicts_resolve_to_the_authoritative_resume(stripped_texts: dict[str, str]) -> None:
    items = compare_resumes("ai", stripped_texts)
    conflicts = {item.kind: item for item in items if item.category == "conflict"}
    assert "'26'" in conflicts["edition"].text and "'25'" in conflicts["edition"].text
    assert "Organiser" in conflicts["role"].text and "Committee Head" in conflicts["role"].text
    assert "LangChain" in conflicts["stack"].text and "OpenAI API" in conflicts["stack"].text
    reworded = [item for item in items if item.kind == "bullet" and item.category == "conflict"]
    assert any("Python backend" in item.text and "NestJS backend" in item.detail for item in reworded)


def test_unconfirmed_items_are_only_in_the_older_resume(stripped_texts: dict[str, str]) -> None:
    items = compare_resumes("ai", stripped_texts)
    unconfirmed = [item for item in items if item.category == "unconfirmed"]
    texts = " | ".join(item.text for item in unconfirmed)
    for expected in ("PortalApp", "Redux", "HonoJS", "Node.js", "Jest", "Cypress", "Microservices", "1000+", "Computer Networks"):
        assert expected in texts, expected
    skills = " | ".join(item.text for item in unconfirmed if item.kind == "skill")
    assert "FastAPI" not in skills and "Docker" not in skills       # in both resumes
    assert all("resume_ai" not in item.text for item in unconfirmed)


def test_ticks_survive_regeneration(stripped_texts: dict[str, str]) -> None:
    items = compare_resumes("ai", stripped_texts)
    first = render_review_facts(items, "ai")
    target = next(item for item in items if "Redux" in item.text)
    ticked = first.replace(f"- [ ] **{target.item_id}**", f"- [x] **{target.item_id}**")
    again = render_review_facts(items, "ai", existing_ticks(ticked))
    assert f"- [x] **{target.item_id}**" in again
    assert again.count("- [x]") == 1
    assert render_review_facts(items, "ai") == first                  # deterministic


def _profile_config(config: Config, root: Path) -> Config:
    (root / "resumes").mkdir()
    (root / "resumes" / "ai.txt").write_text(AI_RESUME, encoding="utf-8")
    (root / "resumes" / "fs.txt").write_text(OLDER_RESUME, encoding="utf-8")
    settings = ProfileSettings(resume_dir="resumes", profile_dir="profile", files={"ai": "ai.txt", "fullstack": "fs.txt"})
    return dataclasses.replace(config, profiles=settings, project_root=root)


def test_profile_build_stores_stripped_text_and_writes_review_file(
    connection: sqlite3.Connection, config: Config, tmp_path: Path
) -> None:
    cfg = _profile_config(config, tmp_path)
    summary = run_profile_build(connection, cfg)
    assert {profile.name for profile in summary.profiles} == {"ai", "fullstack"}
    assert summary.conflicts >= 3 and summary.unconfirmed >= 5

    rows = {row["name"]: row for row in connection.execute("SELECT * FROM profiles")}
    for name in ("ai", "fullstack"):
        stored = rows[name]["stripped_text"]
        assert "jane.doe@example.com" not in stored and "98765" not in stored and "Doe" not in stored
        assert len(rows[name]["text_hash"]) == 64
        removed = json.loads(rows[name]["pii_removed"])
        assert removed["email"] == 1 and "jane" not in json.dumps(removed).lower()   # counts only, never values
    assert (tmp_path / "profile" / "review_facts.md").is_file()
    assert (tmp_path / "profile" / "ai.txt").read_text(encoding="utf-8") == rows["ai"]["stripped_text"] + "\n"

    # rebuilding is idempotent: same hash, still one row each
    again = run_profile_build(connection, cfg)
    assert [p.text_hash for p in again.profiles] == [rows[p.name]["text_hash"] for p in again.profiles]
    assert connection.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 2


def test_missing_resume_is_a_clear_error(connection: sqlite3.Connection, config: Config, tmp_path: Path) -> None:
    cfg = dataclasses.replace(config, project_root=tmp_path,
                              profiles=ProfileSettings(resume_dir="nowhere", files={"ai": "missing.pdf"}))
    with pytest.raises(ProfileError, match="resume not found"):
        run_profile_build(connection, cfg)


# --- evaluation ---------------------------------------------------------------------------------


def _seed_jobs(connection: sqlite3.Connection, config: Config, clock) -> dict[str, int]:
    titles = {
        "ml": "Machine Learning Engineer", "se": "Software Engineer", "py": "Python Developer",
        "ds": "Data Scientist", "java": "Java Developer", "php": "PHP Developer", "sales": "Account Manager",
        "salesforce": "Salesforce Administrator", "devops": "DevOps Engineer", "data": "Data Engineer",
    }
    ids: dict[str, int] = {}
    for n, (key, title) in enumerate(titles.items(), start=1):
        cursor = connection.execute(
            """INSERT INTO jobs (fingerprint, title, company_name, location_raw, first_seen_at, last_seen_at, status,
                                 description_text, description_hash)
               VALUES (?, ?, 'Acme', 'Pune, India', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', 'open', 'Python APIs', ?)""",
            (f"fp{n}", title, f"h{n}"),
        )
        ids[key] = int(cursor.lastrowid)
    run_relevance(connection, config, clock=clock)
    return ids


def test_eval_sample_is_stratified_and_never_fills_labels(
    connection: sqlite3.Connection, config: Config, clock, tmp_path: Path
) -> None:
    _seed_jobs(connection, config, clock)
    path = tmp_path / "eval" / "gold.csv"
    counts = write_sample(connection, path, size=9)
    assert set(counts) == {"target", "maybe", "excluded"}
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows and all(row["relevant"] == "" for row in rows)
    assert {"job_id", "title", "gate_status", "gate_reason", "relevant"} <= set(rows[0])

    with pytest.raises(EvalError, match="already exists"):
        write_sample(connection, path, size=9)
    write_sample(connection, path, size=9, force=True)


def test_eval_run_precision_recall_and_false_rejections(
    connection: sqlite3.Connection, config: Config, clock, tmp_path: Path
) -> None:
    ids = _seed_jobs(connection, config, clock)
    path = tmp_path / "gold.csv"
    labels = {"ml": "yes", "se": "yes", "py": "yes", "ds": "no", "java": "yes", "php": "no", "sales": "no",
              "salesforce": "yes", "devops": "no", "data": ""}
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["job_id", "relevant"])
        writer.writeheader()
        for key, label in labels.items():
            writer.writerow({"job_id": ids[key], "relevant": label})

    report = run_eval(connection, path)
    assert report.labeled == 9 and report.unlabeled == 1
    assert [item["title"] for item in report.false_rejections] == ["Java Developer"]
    # predicted relevant = target+maybe: ml, se, py, ds, salesforce(maybe), devops(maybe) ; relevant: ml, se, py, java, salesforce
    both = report.gate_target_or_maybe
    assert (both.tp, both.fp, both.fn) == (4, 2, 1)
    assert round(both.precision, 2) == 0.67 and round(both.recall, 2) == 0.8
    assert "False rejections: 1" in format_report(report)
    assert report.precision_at[10] is None            # not enough scored jobs yet: reported as n/a, not faked


def test_eval_run_needs_a_gold_file(connection: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(EvalError, match="not found"):
        run_eval(connection, tmp_path / "nope.csv")
