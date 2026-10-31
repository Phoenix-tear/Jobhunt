"""Demo workspace initialization and isolation (Brief v12).

Provides an isolated environment using the demo persona:
- SQLite database under `data/demo/demo.sqlite3`
- Pre-seeded jobs from `tests/fixtures/persona_demo/jobs.json`
- Pre-configured demo resumes and profile
- Deterministic stub Gemini client: zero live API calls and no network
- Leaving demo mode leaves real data completely untouched.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from jobhunt.config import Config
from jobhunt.db import apply_migrations, connect
from jobhunt.matching.profiles import PiiResult
from jobhunt.timeutil import to_iso, utc_now

logger = logging.getLogger(__name__)


class DemoGeminiStub:
    """Deterministic stub replacing the live Gemini client in demo mode."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def list_models(self) -> list[str]:
        return [
            "gemini-3.8-flash",
            "gemini-3.5-flash-lite",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-1.5-pro",
            "gemini-1.5-flash",
        ]

    def generate_json(self, model: str, *, system_instruction: str, user_text: str, response_schema: dict[str, Any], **kwargs: Any) -> Any:
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class StubResult:
            text: str
            input_tokens: int = 120
            output_tokens: int = 80
            thinking_tokens: int = 0
            finish_reason: str = "stop"
            model_version: str = "demo-stub"

        # Deterministic responses based on task
        if "score" in system_instruction.lower():
            mock_data = {
                "fit_score": 88,
                "seniority_fit": "junior",
                "role_fit": "high",
                "rationale": "Strong alignment with candidate's ML and backend background.",
            }
        elif "classify" in system_instruction.lower():
            mock_data = {
                "level": "junior",
                "employment_type": "full_time",
                "workplace_type": "hybrid",
            }
        elif "recipe" in system_instruction.lower() or "tailor" in system_instruction.lower():
            mock_data = {
                "recipe": "ai_ml",
                "summary_variant": "S1",
                "projects": ["Distributed Cache", "FinVoice Agent"],
                "missing": [],
            }
        elif "role" in (system_instruction + " " + user_text).lower():
            mock_data = {
                "role_families": [
                    {
                        "id": "applied_ai_engineer",
                        "label": "Applied AI Engineer",
                        "keywords": ["rag", "llm", "agents", "langchain", "embeddings", "python"],
                        "title_patterns": ["ai engineer", "llm engineer", "generative ai"],
                        "negative_keywords": ["salesforce", "hardware", "php"],
                        "embedding_query": "applied ai engineer with llm and python experience",
                    }
                ]
            }
        else:
            mock_data = {"status": "ok", "message": "Demo mode simulated response"}

        return StubResult(text=json.dumps(mock_data))

    def close(self) -> None:
        pass


def setup_demo_workspace(config: Config) -> Config:
    """Initialize or load demo workspace at data/demo/."""
    demo_dir = config.data_directory / "demo"
    demo_dir.mkdir(parents=True, exist_ok=True)
    demo_db_path = demo_dir / "demo.sqlite3"
    fixtures_dir = config.project_root / "tests" / "fixtures" / "persona_demo"

    # Set up directory structure
    (demo_dir / "profile").mkdir(parents=True, exist_ok=True)
    (demo_dir / "resumes").mkdir(parents=True, exist_ok=True)

    # Copy demo profile and resumes if needed
    demo_profile = demo_dir / "profile" / "profile.yaml"
    if not demo_profile.is_file() and (fixtures_dir / "profile.yaml").is_file():
        shutil.copy2(fixtures_dir / "profile.yaml", demo_profile)

    demo_answers = demo_dir / "profile" / "application_answers.yaml"
    if not demo_answers.is_file() and (fixtures_dir / "application_answers.yaml").is_file():
        shutil.copy2(fixtures_dir / "application_answers.yaml", demo_answers)

    for tex_file in fixtures_dir.glob("resume_*.tex"):
        dest_tex = demo_dir / "resumes" / tex_file.name
        if not dest_tex.is_file():
            shutil.copy2(tex_file, dest_tex)

    # Initialize demo database
    connection = connect(demo_db_path, check_same_thread=False)
    try:
        apply_migrations(connection, config.migrations_directory)
        _seed_demo_jobs_if_empty(connection, fixtures_dir / "jobs.json")
    finally:
        connection.close()

    # Create demo configuration pointing to demo paths
    profiles_cfg = dataclasses.replace(
        config.profiles,
        resume_dir=str(demo_dir / "resumes"),
        profile_dir=str(demo_dir / "profile"),
        files={"ai": "resume_systems.tex", "fullstack": "resume_data.tex"},
    )
    demo_config = dataclasses.replace(
        config,
        database_path=str(demo_db_path),
        profiles=profiles_cfg,
    )
    return demo_config


def _seed_demo_jobs_if_empty(connection: sqlite3.Connection, jobs_json_path: Path) -> None:
    count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    if count > 0 or not jobs_json_path.is_file():
        return

    jobs = json.loads(jobs_json_path.read_text(encoding="utf-8"))
    now = to_iso(utc_now())

    for idx, j in enumerate(jobs):
        fingerprint = f"demo_fp_{j.get('id', idx)}"
        cur = connection.execute(
            """
            INSERT INTO jobs (
                fingerprint, company_name, title, location_raw, country, city, description_text,
                first_seen_at, last_seen_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (
                fingerprint,
                j.get("company_name", "Demo Company"),
                j.get("title", "Software Engineer"),
                j.get("location_raw", "Bengaluru, India"),
                j.get("country", "IN"),
                j.get("city", "Bengaluru"),
                j.get("description_text", "Sample description for demo persona."),
                now,
                now,
            ),
        )
        job_id = cur.lastrowid
        connection.execute(
            """
            INSERT INTO job_sightings (job_id, source, source_job_id, source_url, first_seen_at, last_seen_at)
            VALUES (?, 'simplify', ?, ?, ?, ?)
            """,
            (job_id, f"demo_{job_id}", f"https://example.com/jobs/{job_id}", now, now),
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO job_relevance (
                job_id, computed_at, rules_hash, input_hash, gate_status, gate_rule, gate_reason, role_family, final_score
            ) VALUES (?, ?, 'demo_rules', 'demo_input', 'target', 'demo_persona', 'Matches demo criteria', 'ai_ml', 85.0)
            """,
            (job_id, now),
        )
