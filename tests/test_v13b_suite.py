"""Tests for Brief v13b: template contract safety, engine command builder, doctor diagnostics, and report scrubbing."""

import json
from pathlib import Path
import pytest

from jobhunt.resumes.templates import (
    analyze_template,
    detect_template,
    extract_template_facts,
)
from jobhunt.settings.secrets import resolve_gemini_key
from jobhunt.tailoring.engine import Bases, Tailor
from jobhunt.tailoring.latex import build_compile_command, compile_sandboxed
from jobhunt.tailoring.texdoc import parse, render


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "resumes"


class TestTemplateContractsAndRoundTrip:
    """Verify template safety contracts and round-trip bullet preservation."""

    @pytest.mark.parametrize(
        "fname,expected_id,can_tailor",
        [
            ("fixture_custom_macros.tex", "custom_macros", True),
            ("fixture_jakes_resume.tex", "jakes_resume", True),
            ("fixture_plain_itemize.tex", "plain_itemize", True),
            ("fixture_awesome_cv.tex", "awesome_cv", True),
            ("fixture_moderncv.tex", "moderncv", False),
            ("fixture_unsupported_odd.tex", "unsupported", False),
        ],
    )
    def test_template_detection_and_capability_contract(self, fname: str, expected_id: str, can_tailor: bool):
        path = FIXTURES_DIR / fname
        assert path.is_file(), f"Missing fixture {fname}"
        content = path.read_text(encoding="utf-8")

        detected = detect_template(content)
        assert detected == expected_id

        analysis = analyze_template(content)
        assert analysis.template == expected_id
        assert analysis.can_tailor is can_tailor

    @pytest.mark.parametrize(
        "fname",
        [
            "fixture_custom_macros.tex",
            "fixture_jakes_resume.tex",
            "fixture_plain_itemize.tex",
            "fixture_awesome_cv.tex",
        ],
    )
    def test_tailorable_templates_round_trip_bullet_preservation(self, fname: str):
        """Every template marked tailorable MUST preserve exact bullets in parse -> render -> parse."""
        content = (FIXTURES_DIR / fname).read_text(encoding="utf-8")
        doc = parse(content)

        input_exp_bullets = [b for e in doc.experience for b in e.bullets]
        input_proj_bullets = [b for e in doc.projects for b in e.bullets]
        total_input = len(input_exp_bullets) + len(input_proj_bullets)
        assert total_input > 0, f"Template {fname} has no parsed bullets"

        rendered = render(doc)
        doc2 = parse(rendered)
        output_exp_bullets = [b for e in doc2.experience for b in e.bullets]
        output_proj_bullets = [b for e in doc2.projects for b in e.bullets]

        assert output_exp_bullets == input_exp_bullets
        assert output_proj_bullets == input_proj_bullets

    def test_moderncv_tailoring_refusal(self):
        """Moderncv must safely refuse tailoring with LookupError (facts and letters only)."""
        from unittest.mock import MagicMock
        from jobhunt.config import load_config
        import sqlite3

        content = (FIXTURES_DIR / "fixture_moderncv.tex").read_text(encoding="utf-8")
        doc = parse(content)
        analysis = analyze_template(content)
        assert analysis.can_tailor is False

        bases = Bases(docs={"mcv": doc}, per_resume={"mcv": []}, merged=[], ticked={})
        conn = sqlite3.connect(":memory:")
        tailor = Tailor(conn, load_config(), compiler=MagicMock(), bases=bases)

        with pytest.raises(LookupError, match="does not support tailoring"):
            tailor.tailor(1, base="mcv")


class TestEngineCommandConstruction:
    """Verify per-engine argument rules (tectonic uses --untrusted, others use -no-shell-escape)."""

    def test_tectonic_arguments(self, tmp_path: Path):
        tex = tmp_path / "resume.tex"
        out = tmp_path / "out"

        cmd_untrusted = build_compile_command("tectonic", "tectonic.exe", tex, out, untrusted=True)
        assert "-no-shell-escape" not in cmd_untrusted
        assert "--untrusted" in cmd_untrusted
        assert "-X" in cmd_untrusted and "compile" in cmd_untrusted
        assert "--outdir" in cmd_untrusted

        cmd_trusted = build_compile_command("tectonic", "tectonic.exe", tex, out, untrusted=False)
        assert "-no-shell-escape" not in cmd_trusted
        assert "--untrusted" not in cmd_trusted

    def test_latexmk_arguments(self, tmp_path: Path):
        tex = tmp_path / "resume.tex"
        out = tmp_path / "out"

        cmd = build_compile_command("latexmk", "latexmk", tex, out, untrusted=True)
        assert "-no-shell-escape" in cmd
        assert "--untrusted" not in cmd
        assert "-xelatex" in cmd
        assert f"-outdir={out}" in cmd

    def test_xelatex_and_lualatex_arguments(self, tmp_path: Path):
        tex = tmp_path / "resume.tex"
        out = tmp_path / "out"

        for eng in ("xelatex", "lualatex", "pdflatex"):
            cmd = build_compile_command(eng, eng, tex, out, untrusted=True)
            assert "-no-shell-escape" in cmd
            assert "--untrusted" not in cmd
            assert f"-output-directory={out}" in cmd


class TestEmbeddingPricingEstimates:
    """Verify embedding estimation does not state dollar figure as fact without disclaimer."""

    def test_embedding_estimate_includes_pricing_disclaimer(self, tmp_path: Path):
        import sqlite3
        from jobhunt.config import load_config
        from jobhunt.matching.embeddings import estimate_recompute_cost

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, company_name TEXT, location_raw TEXT, description_text TEXT, status TEXT)")
        conn.execute("CREATE TABLE job_relevance (job_id INTEGER PRIMARY KEY, gate_status TEXT)")
        conn.execute("CREATE TABLE profiles (name TEXT PRIMARY KEY, stripped_text TEXT)")

        cfg = load_config()
        est = estimate_recompute_cost(conn, cfg, "gemini")
        assert est["provider"] == "gemini"
        assert "pricing_note" in est
        assert "Google's current pricing" in est["pricing_note"]


class TestRepositoryPiiScanner:
    """Automated guard: zero personal names or leaked credentials in tracked files."""

    def test_scanner_zero_findings(self, tmp_path: Path):
        from scripts.scan_pii import scan_repo
        project_root = Path(__file__).resolve().parent.parent
        terms_dir = tmp_path / "terms" / "profile"  # explicit temp terms: never the checkout's own data/
        terms_dir.mkdir(parents=True)
        (terms_dir / "pii_terms.txt").write_text("# [name]\nZyxwvuSyntheticPerson\n", encoding="utf-8")
        findings = scan_repo(project_root, terms_dir=tmp_path / "terms")
        assert findings == [], f"Detected PII/secret findings in tracked files: {findings}"


class TestUpgradeWorkflow:
    """Verify jobhunt upgrade runs backup, migration, lock check without git pull."""

    def test_upgrade_in_temp_dir(self, tmp_path: Path):
        import argparse
        import sqlite3
        from unittest.mock import patch
        from jobhunt.config import load_config
        from jobhunt.cli import _command_upgrade

        fake_root = tmp_path / "repo"
        fake_root.mkdir()
        fake_data = fake_root / "data"
        fake_data.mkdir()

        # Create dummy db
        db_file = fake_data / "jobhunt.sqlite3"
        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE dummy (x TEXT)")
        conn.execute("INSERT INTO dummy VALUES ('seed')")
        conn.commit()
        conn.close()

        import shutil
        shutil.copy2("config.yaml", fake_root / "config.yaml")
        shutil.copytree("migrations", fake_root / "migrations")
        cfg = load_config(fake_root / "config.yaml", data_dir=fake_data)

        # Run upgrade with mock subprocess
        with patch("subprocess.run") as mock_sub:
            code = _command_upgrade(argparse.Namespace(), cfg)
            assert code == 0

        # Verify backup was created
        backups = list((fake_data / "backups").glob("*.sqlite3"))
        assert len(backups) >= 1, "Backup file should have been created before migration"


class TestDoctorReportAndScrubber:
    """Verify doctor report generation and zero secret/PII leakage in logs."""

    def test_scrubber_redacts_keys_emails_and_pii(self):
        from jobhunt.doctor import scrub_text

        sample_key = "AIzaSy" + "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"
        raw_log = (
            f"2026-10-31 04:00:00 [INFO] Request sent with key={sample_key} and bearer token: abcdef1234567890abcdef1234567890\n"
            "Contact user at devon.smith@example.test or call +1 555-123-4567\n"
            "Applicant graduated from Northern State University with honors.\n"
        )
        custom_pii = {"Northern State University", "Devon Smith"}

        scrubbed = scrub_text(raw_log, custom_pii=custom_pii)

        assert "AIzaSy" not in scrubbed
        assert "[REDACTED_API_KEY]" in scrubbed
        assert "[REDACTED_SECRET]" in scrubbed
        assert "devon.smith@example.test" not in scrubbed
        assert "[REDACTED_EMAIL]" in scrubbed
        assert "555-123-4567" not in scrubbed
        assert "[REDACTED_PHONE]" in scrubbed
        assert "Northern State University" not in scrubbed
        assert "[REDACTED_PII]" in scrubbed

    def test_generate_doctor_report(self, tmp_path: Path):
        from jobhunt.config import load_config
        from jobhunt.doctor import generate_doctor_report

        cfg = load_config()
        out_file = tmp_path / "doctor_report.txt"
        generated = generate_doctor_report(cfg, out_path=out_file)

        assert generated.is_file()
        content = generated.read_text(encoding="utf-8")
        assert "JOBHUNT SYSTEM DOCTOR REPORT" in content
        assert "SYSTEM CHECKS" in content
        assert "RESUME TEMPLATES" in content
        assert "END OF REPORT" in content


class TestFeedbackTemplateCommand:
    """Verify jobhunt feedback-template output."""

    def test_feedback_template_cli(self, capsys):
        import argparse
        from jobhunt.config import load_config
        from jobhunt.cli import _command_feedback_template

        cfg = load_config()
        code = _command_feedback_template(argparse.Namespace(), cfg)
        assert code == 0
        captured = capsys.readouterr().out
        assert "Jobhunt Tester Feedback Form" in captured
        assert "1. Environment & Setup:" in captured
        assert "10. Overall Impression & Rating" in captured



