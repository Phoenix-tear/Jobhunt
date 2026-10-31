"""Tests for v12 Settings, Setup Wizard, Local Server Security, Secrets, and Demo Mode."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from jobhunt.api.app import create_app
from jobhunt.api.security import generate_csrf_token
from jobhunt.config import Config, load_config
from jobhunt.settings.demo import DemoGeminiStub, setup_demo_workspace
from jobhunt.settings.secrets import (
    SecretMaskingFilter,
    get_gemini_key,
    get_secret_info,
    install_secret_masking,
    set_gemini_key,
)
from jobhunt.settings.setup_state import (
    check_and_autodone_existing_install,
    get_setup_state,
    set_setup_state,
)
from jobhunt.resumes.validator import validate_resume_text
from jobhunt.settings.storage import (
    load_merged_settings,
    read_settings_file,
    write_settings_file,
)
from jobhunt.tailoring.latex import compile_sandboxed
from jobhunt.tailoring.texdoc import ResumeDoc, parse


# ---------------------------------------------------------------------------
# 1. Settings storage & precedence
# ---------------------------------------------------------------------------

class TestSettingsPrecedence:
    def test_precedence_defaults_local_and_ui(self, tmp_path: Path) -> None:
        """Verify config.yaml < config.local.yaml < data/settings.yaml precedence."""
        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text("sources:\n  lever: true\n  ashby: true\n", encoding="utf-8")

        local_yaml = tmp_path / "config.local.yaml"
        local_yaml.write_text("sources:\n  ashby: false\n", encoding="utf-8")

        settings_yaml = tmp_path / "data" / "settings.yaml"
        settings_yaml.parent.mkdir(parents=True, exist_ok=True)
        settings_yaml.write_text("budget:\n  preset: free\n  daily_pipeline_calls: 15\n", encoding="utf-8")

        # Load merged settings
        merged = load_merged_settings(tmp_path)
        assert merged.get("sources", {}).get("lever") is True
        assert merged.get("sources", {}).get("ashby") is False
        assert merged.get("budget", {}).get("preset") == "free"
        assert merged.get("budget", {}).get("daily_pipeline_calls") == 15

        # Saving UI settings never alters config.yaml or config.local.yaml
        write_settings_file({"budget": {"preset": "paid", "daily_pipeline_calls": 50}}, tmp_path)
        assert "ashby: false" in local_yaml.read_text(encoding="utf-8")
        assert "lever: true" in config_yaml.read_text(encoding="utf-8")

        # Reloading shows updated UI settings
        updated = read_settings_file(tmp_path)
        assert updated["budget"]["preset"] == "paid"

    def test_forbidden_keys_stripped_from_settings_yaml(self, tmp_path: Path) -> None:
        """Secrets like gemini_api_key must never be written to data/settings.yaml."""
        data_to_save = {
            "gemini_api_key": "AIzaSySecretShouldNeverBeInYaml",
            "api_key": "secret",
            "budget": {"preset": "free"},
        }
        write_settings_file(data_to_save, tmp_path)
        raw_text = (tmp_path / "data" / "settings.yaml").read_text(encoding="utf-8")
        assert "AIzaSy" not in raw_text
        assert "api_key" not in raw_text
        assert "gemini_api_key" not in raw_text


# ---------------------------------------------------------------------------
# 2. Secrets & Log Masking
# ---------------------------------------------------------------------------

class TestSecretsAndMasking:
    def test_secret_storage_and_permissions(self, tmp_path: Path) -> None:
        secret = "AIzaSySecretKeyForTesting1234"
        set_gemini_key(secret, tmp_path)
        key = get_gemini_key(tmp_path)
        assert key == secret

        info = get_secret_info(key)
        assert info["present"] is True
        assert info["last4"] == "…1234"
        assert "AIzaSySecret" not in json.dumps(info)

        # File exists in .secrets
        secret_file = tmp_path / ".secrets" / "gemini_api_key"
        assert secret_file.is_file()

    def test_log_filter_masks_secrets(self, tmp_path: Path) -> None:
        secret = "AIzaSySecretStringToMask9999"
        set_gemini_key(secret, tmp_path)

        masking_filter = install_secret_masking(tmp_path)
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="Calling Gemini with key %s for job analysis",
            args=(secret,),
            exc_info=None,
        )
        masking_filter.filter(record)
        rendered = record.getMessage()
        assert secret not in rendered
        assert "…9999" in rendered or "[REDACTED_API_KEY]" in rendered


# ---------------------------------------------------------------------------
# 3. Local Server Security & CSRF
# ---------------------------------------------------------------------------

class TestLocalServerSecurity:
    def test_missing_csrf_token_rejected_on_post(self, api_config: Config, db: sqlite3.Connection) -> None:
        app = create_app(api_config, extra_allowed_hosts=("testserver",))
        client = TestClient(app)
        res = client.post("/api/settings/budget", json={"preset": "free", "daily_pipeline_calls": 10})
        assert res.status_code == 403
        assert "invalid or missing" in res.json().get("detail", "").lower()

    def test_invalid_csrf_token_rejected(self, api_config: Config, db: sqlite3.Connection) -> None:
        app = create_app(api_config, extra_allowed_hosts=("testserver",))
        client = TestClient(app)
        res = client.post(
            "/api/settings/budget",
            json={"preset": "free", "daily_pipeline_calls": 10},
            headers={"X-Jobhunt-Token": "completely_wrong_token"},
        )
        assert res.status_code == 403

    def test_valid_csrf_token_accepted(self, api_config: Config, db: sqlite3.Connection, tmp_path: Path) -> None:
        import dataclasses
        cfg = dataclasses.replace(api_config, project_root=tmp_path)
        app = create_app(cfg, extra_allowed_hosts=("testserver",))
        token = getattr(app.state, "csrf_token", "")
        client = TestClient(app)
        res = client.post(
            "/api/settings/budget",
            json={"preset": "free", "daily_pipeline_calls": 10, "daily_interactive_calls": 5, "monthly_budget_usd": 0.0},
            headers={"X-Jobhunt-Token": token},
        )
        assert res.status_code == 200

    def test_dns_rebinding_rejection_on_invalid_host(self, api_config: Config, db: sqlite3.Connection) -> None:
        app = create_app(api_config)
        client = TestClient(app)
        res = client.get("/api/settings/gemini", headers={"Host": "evil-rebinding.com"})
        assert res.status_code == 400

    def test_cross_origin_rejected_when_origin_foreign(self, api_config: Config, db: sqlite3.Connection) -> None:
        app = create_app(api_config, extra_allowed_hosts=("testserver",))
        token = getattr(app.state, "csrf_token", "")
        client = TestClient(app)
        res = client.post(
            "/api/settings/budget",
            json={"preset": "free"},
            headers={"X-Jobhunt-Token": token, "Origin": "http://malicious-website.com"},
        )
        assert res.status_code == 403
        assert "cross-origin" in res.json().get("detail", "").lower()


# ---------------------------------------------------------------------------
# 4. Resume Upload Validation & Sandboxing
# ---------------------------------------------------------------------------

class TestResumeUploadAndSandboxing:
    def test_upload_size_limit_and_tex_extension(self, api_config: Config, db: sqlite3.Connection) -> None:
        app = create_app(api_config, extra_allowed_hosts=("testserver",))
        token = getattr(app.state, "csrf_token", "")
        client = TestClient(app)

        # 1. Non-tex file rejected
        res = client.post(
            "/api/resumes/upload",
            data={"base_id": "test"},
            files={"file": ("malicious.exe", b"binary content", "application/octet-stream")},
            headers={"X-Jobhunt-Token": token},
        )
        assert res.status_code == 400

        # 2. File > 2MB rejected
        oversized = b"\\documentclass{article}\n" + (b"% comment padding\n" * 150000)
        assert len(oversized) > 2 * 1024 * 1024
        res = client.post(
            "/api/resumes/upload",
            data={"base_id": "test"},
            files={"file": ("huge.tex", oversized, "text/plain")},
            headers={"X-Jobhunt-Token": token},
        )
        assert res.status_code == 400
        assert "exceeds" in res.json().get("detail", "").lower()

    def test_sandboxed_tectonic_flags(self, tmp_path: Path) -> None:
        sample_tex = tmp_path / "sample.tex"
        sample_tex.write_text("\\documentclass{article}\\begin{document}Hello\\end{document}", encoding="utf-8")

        with patch("jobhunt.tailoring.latex.find_engine", return_value=("tectonic", "tectonic")), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"Compiled successfully", stderr=b"")
            dummy_pdf = tmp_path / "sample.pdf"
            dummy_pdf.write_bytes(b"%PDF-1.4 dummy")

            compile_sandboxed(sample_tex, tmp_path, timeout=10.0)

            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            assert "--untrusted" in called_cmd
            assert "-C" not in called_cmd  # -C / --only-cached removed to allow package download on fresh setups
            assert "--outdir" in called_cmd


# ---------------------------------------------------------------------------
# 5. Robust Resume Parser (Jake's template, Plain itemize, Demo persona)
# ---------------------------------------------------------------------------

class TestResumeParserVariants:
    def test_parse_demo_persona(self) -> None:
        demo_resume = Path("tests/fixtures/persona_demo/resume_data.tex")
        assert demo_resume.is_file()
        content = demo_resume.read_text(encoding="utf-8")
        parsed = parse(content)

        assert parsed.education != ""
        assert len(parsed.experience) > 0
        assert len(parsed.projects) > 0
        assert len(parsed.skills) > 0

    def test_parse_jakes_resume_template(self) -> None:
        jakes_tex = r"""
\documentclass[letterpaper,11pt]{article}
\begin{document}
\section{Education}
  \resumeSubHeadingListStart
    \resumeSubheading
      {State University}{City, ST}
      {Bachelor of Science in Computer Science}{Aug. 2020 -- May 2024}
  \resumeSubHeadingListEnd

\section{Experience}
  \resumeSubHeadingListStart
    \resumeSubheading
      {Software Engineer Intern}{May 2023 -- Aug. 2023}
      {Tech Corp}{Remote}
      \resumeItemListStart
        \resumeItem{Developed distributed caching layer with Redis reducing latency by 40\%.}
        \resumeItem{Automated CI/CD pipeline using GitHub Actions.}
      \resumeItemListEnd
  \resumeSubHeadingListEnd

\section{Projects}
  \resumeSubHeadingListStart
    \resumeProjectHeading
      {\textbf{Search Engine} $|$ \emph{Python, Elasticsearch, Flask}}{Jan. 2024 -- Present}
      \resumeItemListStart
        \resumeItem{Indexed 1M documents with custom BM25 ranking algorithm.}
      \resumeItemListEnd
  \resumeSubHeadingListEnd

\section{Technical Skills}
 \begin{itemize}[leftmargin=0.15in, label={}]
    \small{\item{
     \textbf{Languages}{: Python, Java, C++, SQL} \\
     \textbf{Frameworks}{: FastAPI, React, PyTorch}
    }}
 \end{itemize}
\end{document}
"""
        val = validate_resume_text(jakes_tex, run_compile=False)
        assert val["valid"] is True
        assert val["sections_recognized"]["education"] is True
        assert val["sections_recognized"]["experience"] >= 1
        assert val["sections_recognized"]["projects"] >= 1
        assert val["sections_recognized"]["skills"] >= 1

        doc = parse(jakes_tex)
        assert len(doc.experience) >= 1
        assert "Redis" in doc.experience[0].bullets[0]
        assert len(doc.projects) >= 1
        assert "BM25" in doc.projects[0].bullets[0]

    def test_parse_plain_itemize_template(self) -> None:
        plain_tex = r"""
\documentclass{article}
\begin{document}
\section{Experience}
\textbf{Data Analyst} at Finance Inc (2022 - 2024)
\begin{itemize}
    \item Built automated reporting dashboard in Tableau and SQL.
    \item Optimized SQL queries cutting load time from 5 minutes to 15 seconds.
\end{itemize}
\end{document}
"""
        val = validate_resume_text(plain_tex, run_compile=False)
        assert val["valid"] is True
        assert val["sections_recognized"]["experience"] == 1
        doc = parse(plain_tex)
        assert len(doc.experience) == 1
        assert len(doc.experience[0].bullets) == 2

    def test_unparseable_lines_produce_guidance_without_crash(self) -> None:
        weird_tex = r"""
\documentclass{article}
\begin{document}
\section{Experience}
\unknownMacro{random}{parameters}{not}{seen}{before}
Some plain text without any itemize environment or recognized macro
\end{document}
"""
        val = validate_resume_text(weird_tex, run_compile=False)
        assert len(val["unparsed"]) > 0
        # Warnings provide guidance
        assert any("guidance" in item for item in val["unparsed"])


# ---------------------------------------------------------------------------
# 6. Facts Review Round Trip (Tick / Edit / Hash)
# ---------------------------------------------------------------------------

class TestFactsReview:
    def test_facts_tick_edit_roundtrip(self, api_config: Config, db: sqlite3.Connection) -> None:
        # Write dummy facts_tex.md in profile_dir
        profile_dir = api_config.resolve_path(api_config.profiles.profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
        facts_file = profile_dir / "facts_tex.md"
        facts_file.write_text(
            "# Resume Facts\n\n"
            "- [x] **T-a1b2c3d4** `experience` Built distributed storage engine serving 10M QPS\n"
            "- [ ] **T-e5f6a7b8** `experience` Prototype experimental machine learning tool\n",
            encoding="utf-8",
        )

        app = create_app(api_config, extra_allowed_hosts=("testserver",))
        token = getattr(app.state, "csrf_token", "")
        client = TestClient(app)

        # GET facts
        res = client.get("/api/facts")
        assert res.status_code == 200
        data = res.json()
        assert data["total_count"] == 2
        assert data["ticked_count"] == 1

        # Toggle tick on the second fact
        res = client.post(
            "/api/facts/tick",
            json={"fact_ids": ["T-e5f6a7b8"], "ticked": True},
            headers={"X-Jobhunt-Token": token},
        )
        assert res.status_code == 200
        assert res.json()["updated_count"] == 1

        # Check updated facts list
        res_after = client.get("/api/facts")
        assert res_after.json()["ticked_count"] == 2

        # Edit the first fact
        res = client.post(
            "/api/facts/edit",
            json={
                "fact_id": "T-a1b2c3d4",
                "text": "Built globally distributed storage engine serving 20M QPS",
            },
            headers={"X-Jobhunt-Token": token},
        )
        assert res.status_code == 200
        new_id = res.json()["new_id"]
        assert new_id != "T-a1b2c3d4"

        # Verify edited fact has "(edited by you)" in stored file
        saved_text = facts_file.read_text(encoding="utf-8")
        assert "20M QPS" in saved_text
        assert "(edited by you)" in saved_text


# ---------------------------------------------------------------------------
# 7. Setup Wizard State Machine & Demo Mode
# ---------------------------------------------------------------------------

class TestSetupWizardAndDemo:
    def test_wizard_state_navigation(self, api_config: Config, db: sqlite3.Connection) -> None:
        app = create_app(api_config, extra_allowed_hosts=("testserver",))
        token = getattr(app.state, "csrf_token", "")
        client = TestClient(app)

        # Start wizard
        res = client.get("/api/setup/state")
        assert res.status_code == 200

        # Advance step
        res = client.post(
            "/api/setup/step",
            json={"step": "gemini"},
            headers={"X-Jobhunt-Token": token},
        )
        assert res.status_code == 200
        assert res.json()["step"] == "gemini"

        # Complete wizard
        res = client.post("/api/setup/complete", headers={"X-Jobhunt-Token": token})
        assert res.status_code == 200
        assert res.json()["completed"] is True

        # Restart wizard
        res = client.post("/api/setup/restart", headers={"X-Jobhunt-Token": token})
        assert res.status_code == 200
        assert res.json()["completed"] is False
        assert res.json()["step"] == "welcome"

    def test_demo_mode_isolation(self, api_config: Config, db: sqlite3.Connection) -> None:
        app = create_app(api_config, extra_allowed_hosts=("testserver",))
        token = getattr(app.state, "csrf_token", "")
        client = TestClient(app)

        # Enter demo mode via API
        res = client.post("/api/setup/demo/enter", headers={"X-Jobhunt-Token": token})
        assert res.status_code == 200
        assert res.json()["demo_mode"] is True

        # Demo database exists under data/demo/demo.sqlite3
        demo_db = api_config.project_root / "data" / "demo" / "demo.sqlite3"
        assert demo_db.is_file()

        # Deterministic stub client works
        stub = DemoGeminiStub()
        models = stub.list_models()
        assert len(models) >= 1
        assert "gemini" in models[0]
        gen = stub.generate_json("gemini-3.8-flash", system_instruction="score", user_text="job", response_schema={})
        assert gen.text != ""

        # Exit demo mode
        res_exit = client.post("/api/setup/demo/exit", headers={"X-Jobhunt-Token": token})
        assert res_exit.status_code == 200
        assert res_exit.json()["demo_mode"] is False

    def test_existing_install_autodone(self, api_config: Config) -> None:
        root = api_config.project_root
        from jobhunt.db import apply_migrations, connect

        # Insert a job into the existing DB
        conn = connect(api_config.database_file)
        apply_migrations(conn, api_config.migrations_directory)
        conn.execute(
            """
            INSERT INTO jobs (
                fingerprint, company_name, title, location_raw, country, city, description_text,
                first_seen_at, last_seen_at, status
            ) VALUES ('fp1', 'Acme', 'Software Engineer', 'Remote', 'US', 'Remote', 'Desc', '2026-10-01', '2026-10-01', 'open')
            """
        )
        conn.commit()

        # Create dummy profile.yaml in test profile dir
        profile_file = api_config.resolve_path(api_config.profiles.profile_dir) / "profile.yaml"
        profile_file.parent.mkdir(parents=True, exist_ok=True)
        profile_file.write_text("education:\n  stage: undergraduate\n", encoding="utf-8")

        # Run auto-done check
        res = check_and_autodone_existing_install(conn, api_config)
        conn.close()
        assert res is True

        # Setup state should now be completed
        verify_conn = connect(api_config.database_file)
        state = get_setup_state(verify_conn)
        verify_conn.close()
        assert state["completed"] is True
        assert state["step"] == "done"


# ---------------------------------------------------------------------------
# 8. Readiness Checklist on /api/health
# ---------------------------------------------------------------------------

class TestReadinessChecklist:
    def test_health_includes_readiness_checklist(self, api_config: Config, db: sqlite3.Connection) -> None:
        app = create_app(api_config, extra_allowed_hosts=("testserver",))
        client = TestClient(app)
        res = client.get("/api/health")
        assert res.status_code == 200
        data = res.json()
        assert "readiness" in data
        checklist = data["readiness"]
        assert "key_set" in checklist
        assert "resume_validated" in checklist
        assert "facts_ticked" in checklist
        assert "profile_complete" in checklist
        assert "sources_chosen" in checklist
        assert "first_ingest_done" in checklist
        assert "first_match_done" in checklist
        assert "ready" in checklist
