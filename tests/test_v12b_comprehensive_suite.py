"""Comprehensive test suite for Brief v12b:
Budget presets, Facts hashing & counting, Base resumes API, Custom role families,
First ingest edge cases, Danger zone security bounds, and Database schema integrity.
"""

import json
import os
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from jobhunt.api.app import create_app
from jobhunt.config import Config, load_config
from jobhunt.profile.presets import ROLE_FAMILY_PRESETS
from jobhunt.settings.first_ingest import FirstIngestManager, FirstIngestProgress


@pytest.fixture
def test_suite_env(tmp_path):
    """Isolated environment with database, profiles, and configuration."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    secrets_dir = tmp_path / ".secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "gemini_api_key").write_text("test-fake-key", encoding="utf-8")

    db_path = data_dir / "jobhunt.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, company_name TEXT, "
        "description TEXT, description_hash TEXT, recommended TEXT, score REAL)"
    )
    conn.execute(
        "INSERT INTO jobs VALUES (1, 'Backend Engineer', 'ScaleCo', 'Distributed systems in Go', 'h1', 'ai', 90.0)"
    )
    conn.execute(
        "CREATE TABLE tailored_resumes (id INTEGER PRIMARY KEY, job_id INTEGER, base TEXT, mode TEXT, "
        "key TEXT, status TEXT, version INTEGER, cost_usd REAL, approved INTEGER, approved_at TEXT, "
        "created_at TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, message TEXT, "
        "latex_warnings TEXT, change_summary TEXT, pdf_path TEXT, tex_path TEXT, resume_number INTEGER)"
    )
    conn.execute(
        "CREATE TABLE profiles_v2 (name TEXT PRIMARY KEY, label TEXT, degree TEXT, "
        "graduation_year TEXT, target_level TEXT, max_level TEXT, remote_preference TEXT, "
        "work_auth TEXT, sponsorship TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO profiles_v2 VALUES ('ai', 'AI / ML', 'BS CS', '2026', 'entry', 'mid', 'any', 'yes', 'no', '2026-10-30T00:00:00')"
    )
    conn.execute(
        "INSERT INTO profiles_v2 VALUES ('fullstack', 'Full Stack', 'BS CS', '2026', 'entry', 'mid', 'any', 'yes', 'no', '2026-10-30T00:00:00')"
    )
    conn.execute(
        "CREATE TABLE job_base_similarity_v2 (job_id INTEGER, base TEXT, similarity REAL, PRIMARY KEY (job_id, base))"
    )
    conn.execute("INSERT INTO job_base_similarity_v2 VALUES (1, 'ai', 0.88)")
    conn.execute("INSERT INTO job_base_similarity_v2 VALUES (1, 'fullstack', 0.74)")
    conn.execute(
        "CREATE TABLE setup_state (id INTEGER PRIMARY KEY, completed INTEGER NOT NULL DEFAULT 0, "
        "step TEXT NOT NULL DEFAULT 'welcome', completed_at TEXT, schema_version INTEGER NOT NULL DEFAULT 1)"
    )
    conn.execute("INSERT INTO setup_state VALUES (1, 0, 'welcome', NULL, 1)")
    conn.commit()
    conn.close()

    profiles_dir = data_dir / "profile"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    facts_tex = profiles_dir / "facts_tex.md"
    facts_tex.write_text(
        "# Facts\n"
        "- [x] **T-aaaa1111** `project` Distributed Engine\n"
        "- [x] **T-bbbb2222** `project_bullet` Scaled throughput by 40%\n"
        "- [ ] **T-cccc3333** `experience_bullet` Managed team of 4 interns\n",
        encoding="utf-8",
    )

    resumes_dir = data_dir / "resumes"
    resumes_dir.mkdir(parents=True, exist_ok=True)
    (resumes_dir / "resume_ai.tex").write_text(
        r"\documentclass{article}\begin{document}\section{Experience}\begin{itemize}\item Engine\end{itemize}\end{document}",
        encoding="utf-8",
    )
    (resumes_dir / "resume_fullstack.tex").write_text(
        r"\documentclass{article}\begin{document}\section{Experience}\begin{itemize}\item Fullstack\end{itemize}\end{document}",
        encoding="utf-8",
    )

    settings_file = data_dir / "settings.yaml"
    settings_file.write_text("budget:\n  daily_calls: 20\n  interactive_daily_calls: 15\n  monthly_usd: 1.0\n", encoding="utf-8")

    config = load_config(data_dir=data_dir)
    app = create_app(config, extra_allowed_hosts=("testserver",))
    client = TestClient(app)
    token = getattr(app.state, "csrf_token", "")
    client.headers["x-jobhunt-token"] = token

    return {
        "tmp_path": tmp_path,
        "data_dir": data_dir,
        "secrets_dir": secrets_dir,
        "config": config,
        "client": client,
        "db_path": db_path,
        "token": token,
    }


class TestBudgetPresetsAndSettings:
    """Free (20/15/$1.00) and Paid (60/40/$5.00) presets."""

    def test_save_free_budget_preset(self, test_suite_env):
        client = test_suite_env["client"]
        res = client.post(
            "/api/settings/budget",
            json={
                "preset": "free",
                "daily_pipeline_calls": 20,
                "daily_interactive_calls": 15,
                "monthly_budget_usd": 1.0,
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"

    def test_save_paid_budget_preset(self, test_suite_env):
        client = test_suite_env["client"]
        res = client.post(
            "/api/settings/budget",
            json={
                "preset": "paid",
                "daily_pipeline_calls": 60,
                "daily_interactive_calls": 40,
                "monthly_budget_usd": 5.0,
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"

    def test_get_budget_settings(self, test_suite_env):
        client = test_suite_env["client"]
        res = client.get("/api/settings/budget")
        assert res.status_code == 200
        data = res.json()
        assert "budget" in data
        assert "usage" in data
        assert data["budget"]["daily_calls"] == 20


class TestFactsHashingAndCounting:
    """Facts review endpoint providing facts_hash, used_count, and unused_count."""

    def test_facts_hash_and_counts_returned(self, test_suite_env):
        client = test_suite_env["client"]
        res = client.get("/api/facts")
        assert res.status_code == 200
        data = res.json()
        assert "facts_hash" in data
        assert len(data["facts_hash"]) >= 8
        assert "used_count" in data
        assert "unused_count" in data
        assert data["used_count"] == 2
        assert data["unused_count"] == 1

    def test_facts_content_structure(self, test_suite_env):
        client = test_suite_env["client"]
        res = client.get("/api/facts")
        assert res.status_code == 200
        facts = res.json().get("facts", [])
        assert len(facts) >= 3
        first = facts[0]
        assert "id" in first
        assert "text" in first
        assert "ticked" in first


class TestBaseResumesCapabilitiesAPI:
    """Base resumes listing with detected template and tailoring capabilities."""

    def test_list_base_resumes_with_template_capabilities(self, test_suite_env):
        client = test_suite_env["client"]
        res = client.get("/api/resumes/bases")
        assert res.status_code == 200
        bases = res.json().get("bases", [])
        assert len(bases) >= 2
        for b in bases:
            assert "id" in b
            assert "template" in b
            assert "can_tailor" in b
            assert "compiles" in b

    def test_reject_non_tex_resume_upload(self, test_suite_env):
        client = test_suite_env["client"]
        files = {"file": ("resume.pdf", b"%PDF-dummy", "application/pdf")}
        data = {"base_id": "test_base"}
        res = client.post("/api/resumes/upload", files=files, data=data)
        assert res.status_code in {400, 422}


class TestCustomRoleFamilies:
    """Custom role family creation and preset catalog."""

    def test_all_ten_presets_exist(self):
        expected = [
            "genai", "ai_ml", "fullstack", "backend", "data",
            "devops_cloud", "mobile", "security", "qa", "salesforce",
        ]
        for key in expected:
            assert key in ROLE_FAMILY_PRESETS, f"Preset {key} missing from catalog"

    def test_salesforce_preset_structure(self):
        sf = ROLE_FAMILY_PRESETS["salesforce"]
        assert sf["id"] == "salesforce"
        assert "apex" in sf["keywords"] or "salesforce" in sf["keywords"]
        assert any("salesforce" in p for p in sf["title_patterns"])

    def test_genai_preset_structure(self):
        genai = ROLE_FAMILY_PRESETS["genai"]
        assert genai["id"] == "genai"
        assert "rag" in genai["keywords"] or "llm" in genai["keywords"]


class TestSetupRestartAndNavigation:
    """Non-destructive wizard restart and step management."""

    def test_wizard_restart_endpoint(self, test_suite_env):
        client = test_suite_env["client"]
        res = client.post("/api/setup/restart")
        assert res.status_code == 200
        data = res.json()
        assert data["completed"] is False
        assert data["step"] == "welcome"

    def test_run_setup_again_endpoint(self, test_suite_env):
        client = test_suite_env["client"]
        res = client.post("/api/settings/run-setup-again")
        assert res.status_code == 200
        assert "welcome" in res.json()["message"]

    def test_complete_setup_endpoint(self, test_suite_env):
        client = test_suite_env["client"]
        res = client.post("/api/setup/complete")
        assert res.status_code == 200
        data = res.json()
        assert data["completed"] is True
        assert data["step"] == "done"


class TestDangerZoneSafetyInvariants:
    """Danger zone bounds checking, exact phrase confirmation, and secret protection."""

    def test_empty_confirmation_phrase_rejected(self, test_suite_env):
        client = test_suite_env["client"]
        res = client.post("/api/settings/danger/move-aside", json={"confirmation_phrase": ""})
        assert res.status_code == 400

    def test_case_sensitive_confirmation_phrase(self, test_suite_env):
        client = test_suite_env["client"]
        res = client.post("/api/settings/danger/move-aside", json={"confirmation_phrase": "move data aside"})
        assert res.status_code == 400

    def test_path_traversal_backslash_rejected(self, test_suite_env):
        client = test_suite_env["client"]
        res = client.post("/api/settings/danger/restore-aside", json={"timestamp": "..\\..\\sensitive"})
        assert res.status_code in {400, 422}

    def test_nonexistent_trash_timestamp_returns_404(self, test_suite_env):
        client = test_suite_env["client"]
        res = client.post("/api/settings/danger/restore-aside", json={"timestamp": "99999999_999999"})
        assert res.status_code == 404

    def test_reset_learned_models_moves_to_trash(self, test_suite_env):
        client = test_suite_env["client"]
        data_dir = test_suite_env["data_dir"]
        models_dir = data_dir / "models"
        models_dir.mkdir(parents=True, exist_ok=True)
        dummy_model = models_dir / "triage.joblib"
        dummy_model.write_text("model-bytes", encoding="utf-8")

        res = client.post("/api/settings/danger/reset-models")
        assert res.status_code == 200
        assert not dummy_model.exists()


class TestDatabaseSchemaIntegrity:
    """Verification of profiles_v2, job_base_similarity_v2, and setup_state."""

    def test_profiles_v2_columns(self, test_suite_env):
        conn = sqlite3.connect(test_suite_env["db_path"])
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(profiles_v2)")
        col_names = {row[1] for row in cur.fetchall()}
        expected_cols = {
            "name", "label", "degree", "graduation_year",
            "target_level", "max_level", "remote_preference",
            "work_auth", "sponsorship", "updated_at",
        }
        assert expected_cols.issubset(col_names)
        conn.close()

    def test_job_base_similarity_composite_pk(self, test_suite_env):
        conn = sqlite3.connect(test_suite_env["db_path"])
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(job_base_similarity_v2)")
        rows = cur.fetchall()
        pk_cols = [r[1] for r in rows if r[5] > 0]
        assert "job_id" in pk_cols
        assert "base" in pk_cols
        conn.close()

    def test_job_base_similarity_query(self, test_suite_env):
        conn = sqlite3.connect(test_suite_env["db_path"])
        cur = conn.cursor()
        cur.execute("SELECT base, similarity FROM job_base_similarity_v2 WHERE job_id = 1")
        sims = {r[0]: r[1] for r in cur.fetchall()}
        assert sims["ai"] == 0.88
        assert sims["fullstack"] == 0.74
        conn.close()
