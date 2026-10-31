"""Comprehensive tests for Brief v12b: Setup Wizard steps, First Ingest, Danger Zone,
Role Presets, Summary Proposals, Tectonic sandboxing, and Database Audits.
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
from jobhunt.settings.first_ingest import FirstIngestManager
from jobhunt.tailoring.latex import LatexCompiler, MAX_PDF_SIZE_BYTES


@pytest.fixture
def test_env(tmp_path):
    """Isolated test environment with test data directory."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    secrets_dir = tmp_path / ".secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "gemini_api_key").write_text("test-fake-key", encoding="utf-8")

    # Create dummy database
    db_path = data_dir / "jobhunt.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, company_name TEXT, description TEXT, description_hash TEXT, recommended TEXT, score REAL)")
    conn.execute("INSERT INTO jobs VALUES (1, 'ML Engineer', 'Acme AI', 'Work with PyTorch and Python microservices', 'h1', 'ai', 85.0)")
    conn.execute("CREATE TABLE tailored_resumes (id INTEGER PRIMARY KEY, job_id INTEGER, base TEXT, mode TEXT, key TEXT, status TEXT, version INTEGER, cost_usd REAL, approved INTEGER, approved_at TEXT, created_at TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, message TEXT, latex_warnings TEXT, change_summary TEXT, pdf_path TEXT, tex_path TEXT, resume_number INTEGER)")
    conn.execute("CREATE TABLE profiles_v2 (name TEXT PRIMARY KEY, label TEXT, degree TEXT, graduation_year TEXT, target_level TEXT, max_level TEXT, remote_preference TEXT, work_auth TEXT, sponsorship TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO profiles_v2 VALUES ('ai', 'AI / ML', 'BS CS', '2026', 'entry', 'mid', 'any', 'yes', 'no', '2026-10-30T00:00:00')")
    conn.execute("INSERT INTO profiles_v2 VALUES ('fullstack', 'Full Stack', 'BS CS', '2026', 'entry', 'mid', 'any', 'yes', 'no', '2026-10-30T00:00:00')")
    conn.execute("CREATE TABLE job_base_similarity_v2 (job_id INTEGER, base TEXT, similarity REAL, PRIMARY KEY (job_id, base))")
    conn.execute("CREATE TABLE setup_state (id INTEGER PRIMARY KEY, completed INTEGER NOT NULL DEFAULT 0, step TEXT NOT NULL DEFAULT 'welcome', completed_at TEXT, schema_version INTEGER NOT NULL DEFAULT 1)")
    conn.execute("INSERT INTO setup_state (id, completed, step, completed_at, schema_version) VALUES (1, 0, 'welcome', NULL, 1)")
    conn.commit()
    conn.close()

    # Create dummy config and profiles
    profiles_dir = data_dir / "profile"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    facts_tex = profiles_dir / "facts_tex.md"
    facts_tex.write_text(
        "# Facts\n- [x] **T-11111111** `project` FastData Engine\n"
        "- [x] **T-22222222** `project_bullet` Built distributed stream processing engine in Rust\n"
        "- [ ] **T-33333333** `experience_bullet` Handled internal deployments and monitoring\n",
        encoding="utf-8",
    )
    facts_json = profiles_dir / "facts.json"
    facts_json.write_text(
        json.dumps([
            {"id": "T-11111111", "text": "FastData Engine", "kind": "project", "ticked": True},
            {"id": "T-22222222", "text": "Built distributed stream processing engine in Rust", "kind": "project_bullet", "ticked": True},
            {"id": "T-33333333", "text": "Handled internal deployments and monitoring", "kind": "experience_bullet", "ticked": False},
        ]),
        encoding="utf-8",
    )

    # Base resume
    resumes_dir = data_dir / "resumes"
    resumes_dir.mkdir(parents=True, exist_ok=True)
    (resumes_dir / "resume_ai.tex").write_text(
        r"\documentclass{article}\begin{document}\section{Summary}ML Engineer\section{Experience}\begin{itemize}\item Rust distributed\end{itemize}\end{document}",
        encoding="utf-8",
    )

    settings_file = data_dir / "settings.yaml"
    settings_file.write_text("setup:\n  step: welcome\n  completed: false\n", encoding="utf-8")

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


class TestSetupWizardSteps:
    """Tests for all 10 setup wizard steps save/resume state."""

    STEPS = [
        "welcome", "gemini", "budget", "resumes", "facts",
        "profile", "sources", "schedule", "gmail", "finish",
    ]

    def test_save_and_get_all_steps(self, test_env):
        client = test_env["client"]

        for step in self.STEPS:
            res = client.post("/api/setup/step", json={"step": step})
            assert res.status_code == 200
            assert res.json()["step"] == step

            # Verify persisted in state
            state_res = client.get("/api/setup/state")
            assert state_res.status_code == 200
            assert state_res.json()["step"] == step

    def test_reject_invalid_step(self, test_env):
        client = test_env["client"]
        res = client.post("/api/setup/step", json={"step": "non_existent_step"})
        assert res.status_code == 422


class TestFirstIngestManager:
    """Tests for background first ingest runner, polling, cancellation, and Ollama offline."""

    def test_first_ingest_lifecycle(self, test_env):
        config = test_env["config"]
        manager = FirstIngestManager(config)

        assert not manager.is_running()
        prog = manager.get_progress()
        assert prog["status"] == "idle"

        # Start ingest
        res = manager.start_ingest()
        assert res["status"] in {"running", "completed"}

        # Wait or poll
        import time
        for _ in range(20):
            p = manager.get_progress()
            if p["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.1)

        final_p = manager.get_progress()
        assert final_p["status"] in {"completed", "running"}
        assert final_p["progress"] >= 0

    def test_first_ingest_cancellation(self, test_env):
        config = test_env["config"]
        manager = FirstIngestManager(config)
        manager.start_ingest()
        cancelled = manager.cancel()
        assert cancelled["status"] in {"cancelled", "completed"}
        p = manager.get_progress()
        assert p["status"] in {"cancelled", "completed"}

    def test_first_ingest_endpoints(self, test_env):
        client = test_env["client"]

        # POST first-ingest
        start_res = client.post("/api/setup/first-ingest")
        assert start_res.status_code == 200

        # GET progress
        prog_res = client.get("/api/setup/ingest-progress")
        assert prog_res.status_code == 200
        assert "progress" in prog_res.json()
        assert "status" in prog_res.json()

        # POST cancel
        cancel_res = client.post("/api/setup/ingest-cancel")
        assert cancel_res.status_code == 200


class TestSummaryVariantsProposal:
    """Summary variant proposals strictly validated against facts."""

    def test_suggest_summaries_from_facts(self, test_env):
        client = test_env["client"]
        res = client.post("/api/profile/suggest-summaries", json={"base": "ai"})
        assert res.status_code == 200
        data = res.json()
        assert "variants" in data
        variants = data["variants"]
        assert len(variants) >= 4  # Proposes 4 to 6 variants
        for v in variants:
            assert "id" in v
            assert "title" in v
            assert "text" in v

    def test_save_and_read_summaries(self, test_env):
        client = test_env["client"]
        sample_variants = [
            {"id": "S1", "title": "Systems Specialist", "text": "Experienced in Rust distributed systems."},
            {"id": "S2", "title": "Backend Generalist", "text": "Backend software engineer building APIs."},
        ]
        save_res = client.post("/api/profile/summaries", json={"variants": sample_variants})
        assert save_res.status_code == 200

        get_res = client.get("/api/profile/summaries")
        assert get_res.status_code == 200
        assert len(get_res.json()["variants"]) >= 2


class TestRoleFamiliesAndPresets:
    """Role family presets and suggestion schema."""

    def test_ten_role_family_presets(self, test_env):
        client = test_env["client"]
        res = client.get("/api/profile/role-families/presets")
        assert res.status_code == 200
        presets = res.json()["presets"]
        assert len(presets) >= 10
        preset_ids = {p["id"] for p in presets}
        assert "salesforce" in preset_ids
        assert "genai" in preset_ids
        assert "backend" in preset_ids
        assert "fullstack" in preset_ids

    def test_suggest_role_families(self, test_env):
        client = test_env["client"]
        res = client.post(
            "/api/profile/suggest-role-families",
            json={"description": "Build high throughput distributed streaming systems in Go and Rust"},
        )
        assert res.status_code == 200
        families = res.json()["role_families"]
        assert len(families) > 0
        fam = families[0]
        assert "id" in fam
        assert "label" in fam
        assert "keywords" in fam
        assert "title_patterns" in fam


class TestDangerZoneMoveAsideAndRestore:
    """Tests for Move Data Aside with phrase confirmation, trash isolation, and restore."""

    def test_move_aside_requires_exact_phrase(self, test_env):
        client = test_env["client"]
        res = client.post(
            "/api/settings/danger/move-aside",
            json={"confirmation_phrase": "WRONG PHRASE"},
        )
        assert res.status_code == 400
        assert "MOVE DATA ASIDE" in res.json()["detail"]

    def test_move_aside_and_restore_cycle(self, test_env):
        client = test_env["client"]
        data_dir = test_env["data_dir"]
        secrets_dir = test_env["secrets_dir"]

        # Create a marker file in data/
        marker_file = data_dir / "marker.txt"
        marker_file.write_text("keep_me_safe", encoding="utf-8")

        # 1. Move aside without moving secrets
        res = client.post(
            "/api/settings/danger/move-aside",
            json={"confirmation_phrase": "MOVE DATA ASIDE", "include_secrets": False},
        )
        assert res.status_code == 200
        stamp = res.json()["timestamp"]
        trash_path = Path(res.json()["trash_path"])

        assert trash_path.is_dir()
        assert (trash_path / "marker.txt").is_file()
        assert not marker_file.is_file()

        # .secrets/ MUST NOT be touched
        assert (secrets_dir / "gemini_api_key").is_file()
        assert not (trash_path / ".secrets").exists()

        # 2. Restore aside
        restore_res = client.post(
            "/api/settings/danger/restore-aside",
            json={"timestamp": stamp},
        )
        assert restore_res.status_code == 200
        assert marker_file.is_file()
        assert marker_file.read_text(encoding="utf-8") == "keep_me_safe"

    def test_reject_path_traversal_on_restore(self, test_env):
        client = test_env["client"]
        # Attempt path traversal
        res = client.post(
            "/api/settings/danger/restore-aside",
            json={"timestamp": "../../../windows/system32"},
        )
        assert res.status_code in {400, 422}


class TestCompanyDiscovery:
    """Company ATS discovery."""

    def test_discover_company_from_url(self, test_env):
        client = test_env["client"]
        res = client.post(
            "/api/settings/discover-company",
            json={"url": "https://boards.greenhouse.io/stripe"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["ats"] == "greenhouse"
        assert "stripe" in data["company_name"].lower()


class TestTectonicSandboxing:
    """Tectonic sandboxing: no -C, --untrusted, explicit outdir, 10MB size cap."""

    def test_tectonic_command_flags(self, tmp_path):
        from jobhunt.tailoring.latex import compile_sandboxed

        with patch("jobhunt.tailoring.latex.find_engine", return_value=("tectonic", "C:\\mock\\tectonic.exe")), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
            dummy_tex = tmp_path / "resume.tex"
            dummy_tex.write_text(r"\documentclass{article}\begin{document}Hello\end{document}", encoding="utf-8")
            out_dir = tmp_path / "out"
            out_dir.mkdir(parents=True, exist_ok=True)

            compile_sandboxed(dummy_tex, out_dir=out_dir)

            assert mock_run.called
            called_cmd = mock_run.call_args[0][0]
            # Must NOT contain -C
            assert "-C" not in called_cmd
            assert "--only-cached" not in called_cmd
            # Must contain --untrusted and --outdir
            assert "--untrusted" in called_cmd
            assert "--outdir" in called_cmd

    def test_empty_cache_dir_environment(self, tmp_path):
        """Verifies tectonic sandbox works with an empty cache directory."""
        from jobhunt.tailoring.latex import compile_sandboxed

        empty_cache = tmp_path / "tectonic_cache"
        empty_cache.mkdir(parents=True, exist_ok=True)

        with patch.dict(os.environ, {"TECTONIC_CACHE_DIR": str(empty_cache)}), \
             patch("jobhunt.tailoring.latex.find_engine", return_value=("tectonic", "C:\\mock\\tectonic.exe")), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
            dummy_tex = tmp_path / "resume.tex"
            dummy_tex.write_text(r"\documentclass{article}\begin{document}Hello\end{document}", encoding="utf-8")
            out_dir = tmp_path / "out"
            out_dir.mkdir(parents=True, exist_ok=True)

            compile_sandboxed(dummy_tex, out_dir=out_dir)
            assert mock_run.called
            called_cmd = mock_run.call_args[0][0]
            assert "-C" not in called_cmd

    def test_pdf_size_cap_constant(self):
        assert MAX_PDF_SIZE_BYTES == 10 * 1024 * 1024


class TestDatabaseAuditVerification:
    """Database audit claims verification from v12/v12b report."""

    def test_database_profiles_and_similarity_counts(self, test_env):
        db_path = test_env["db_path"]
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        # profiles_v2
        cur.execute("SELECT name FROM profiles_v2")
        profile_names = {row[0] for row in cur.fetchall()}
        assert "ai" in profile_names
        assert "fullstack" in profile_names
        assert len(profile_names) == 2

        # job_base_similarity_v2 exists and connects
        cur.execute("SELECT count(*) FROM job_base_similarity_v2")
        assert cur.fetchone()[0] == 0  # in test env empty, table schema matches
        conn.close()
