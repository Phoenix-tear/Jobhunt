"""Tests for Brief v12b resume template contracts, detection, fact extraction, and tailoring safety."""

from pathlib import Path
import pytest

from jobhunt.resumes.templates import (
    detect_template,
    analyze_template,
    extract_template_facts,
    clean_bullet_text,
    TemplateAnalysis,
)
from jobhunt.resumes.validator import validate_resume_text
from jobhunt.tailoring.texdoc import parse, render, ResumeDoc

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "resumes"


def read_fixture(name: str) -> str:
    path = FIXTURES_DIR / name
    assert path.is_file(), f"Fixture {name} not found at {path}"
    return path.read_text(encoding="utf-8")


class TestTemplateDetection:
    """Detection across all 7 synthetic fixtures."""

    def test_detect_custom_macros(self):
        content = read_fixture("fixture_custom_macros.tex")
        assert detect_template(content) == "custom_macros"

    def test_detect_jakes_resume(self):
        content = read_fixture("fixture_jakes_resume.tex")
        assert detect_template(content) == "jakes_resume"

    def test_detect_plain_itemize(self):
        content = read_fixture("fixture_plain_itemize.tex")
        assert detect_template(content) == "plain_itemize"

    def test_detect_awesome_cv(self):
        content = read_fixture("fixture_awesome_cv.tex")
        assert detect_template(content) == "awesome_cv"

    def test_detect_moderncv(self):
        content = read_fixture("fixture_moderncv.tex")
        assert detect_template(content) == "moderncv"

    def test_detect_unsupported_odd(self):
        content = read_fixture("fixture_unsupported_odd.tex")
        assert detect_template(content) == "unsupported"

    def test_detect_malformed_nested(self):
        content = read_fixture("fixture_malformed_nested.tex")
        detected = detect_template(content)
        assert detected in {"custom_macros", "plain_itemize", "unsupported"}


class TestTemplateCapabilities:
    """Template capability analysis and flags."""

    def test_custom_macros_capabilities(self):
        content = read_fixture("fixture_custom_macros.tex")
        res = analyze_template(content)
        assert res.template == "custom_macros"
        assert res.compiles is True
        assert res.parses_facts is True
        assert res.can_tailor is True

    def test_jakes_resume_capabilities(self):
        content = read_fixture("fixture_jakes_resume.tex")
        res = analyze_template(content)
        assert res.template == "jakes_resume"
        assert res.compiles is True
        assert res.parses_facts is True
        assert res.can_tailor is True

    def test_plain_itemize_capabilities(self):
        content = read_fixture("fixture_plain_itemize.tex")
        res = analyze_template(content)
        assert res.template == "plain_itemize"
        assert res.compiles is True
        assert res.parses_facts is True
        assert res.can_tailor is True

    def test_awesome_cv_capabilities(self):
        content = read_fixture("fixture_awesome_cv.tex")
        res = analyze_template(content)
        assert res.template == "awesome_cv"
        assert res.compiles is True
        assert res.parses_facts is True
        assert res.can_tailor is True

    def test_moderncv_capabilities(self):
        content = read_fixture("fixture_moderncv.tex")
        res = analyze_template(content)
        assert res.template == "moderncv"
        assert res.compiles is True
        assert res.parses_facts is True
        assert res.can_tailor is False

    def test_unsupported_odd_capabilities(self):
        content = read_fixture("fixture_unsupported_odd.tex")
        res = analyze_template(content)
        assert res.template == "unsupported"
        assert res.can_tailor is False
        assert len(res.reasons) > 0


class TestFactExtractionAndCleaning:
    """Fact extraction, ID stability, and LaTeX cleaning."""

    def test_extract_custom_macros_facts(self):
        content = read_fixture("fixture_custom_macros.tex")
        doc = parse(content)
        facts = extract_template_facts("ai", doc)
        assert len(facts) >= 4
        kinds = {f.kind for f in facts}
        assert "experience" in kinds or "project" in kinds

    def test_extract_jakes_facts(self):
        content = read_fixture("fixture_jakes_resume.tex")
        doc = parse(content)
        facts = extract_template_facts("cv", doc)
        assert len(facts) >= 4
        assert all(f.id.startswith("T-") for f in facts)
        assert all("cv" in f.sources for f in facts)

    def test_extract_plain_itemize_facts(self):
        content = read_fixture("fixture_plain_itemize.tex")
        doc = parse(content)
        facts = extract_template_facts("plain", doc)
        assert len(facts) >= 4

    def test_extract_awesome_cv_facts(self):
        content = read_fixture("fixture_awesome_cv.tex")
        doc = parse(content)
        facts = extract_template_facts("acv", doc)
        assert len(facts) >= 2

    def test_extract_moderncv_facts(self):
        content = read_fixture("fixture_moderncv.tex")
        doc = parse(content)
        facts = extract_template_facts("mcv", doc)
        assert len(facts) >= 2

    def test_stable_fact_ids_across_reparse(self):
        content = read_fixture("fixture_jakes_resume.tex")
        doc1 = parse(content)
        doc2 = parse(content)
        facts1 = extract_template_facts("cv", doc1)
        facts2 = extract_template_facts("cv", doc2)
        assert [f.id for f in facts1] == [f.id for f in facts2]
        assert [f.text for f in facts1] == [f.text for f in facts2]

    def test_clean_bullet_text_strips_latex(self):
        raw = r"\textbf{Developed} an \emph{AI system} with \href{https://example.com}{link} % comment"
        cleaned = clean_bullet_text(raw)
        assert r"\textbf" not in cleaned
        assert r"\emph" not in cleaned
        assert r"\href" not in cleaned
        assert "comment" not in cleaned
        assert "Developed an AI system with link" in cleaned

    def test_clean_bullet_text_excludes_urls_and_emails(self):
        raw = "Contact test.user@example.com or visit https://github.com/testuser for code."
        cleaned = clean_bullet_text(raw)
        assert "test.user@example.com" not in cleaned
        assert "https://github.com/testuser" not in cleaned


class TestTemplateWarnings:
    """Warnings for line numbers and actionable hints."""

    def test_malformed_nested_warnings(self):
        content = read_fixture("fixture_malformed_nested.tex")
        res = analyze_template(content)
        assert len(res.warnings) > 0
        w = res.warnings[0]
        assert w.get("line") > 0
        assert len(w.get("message")) > 0
        assert len(w.get("hint")) > 0


class TestTailoringRefusal:
    """Tailoring engine safety guard when can_tailor=False."""

    def test_refuse_tailoring_unsupported(self, tmp_path):
        import sqlite3
        from unittest.mock import MagicMock
        from jobhunt.config import load_config
        from jobhunt.tailoring.engine import Tailor, Bases

        config = load_config()
        unsupported_tex = read_fixture("fixture_unsupported_odd.tex")
        doc = parse(unsupported_tex)
        bases = Bases(
            docs={"odd": doc},
            per_resume={"odd": []},
            merged=[],
            ticked={},
        )

        conn = sqlite3.connect(":memory:")
        tailor = Tailor(conn, config, compiler=MagicMock(), bases=bases)
        with pytest.raises(LookupError, match="does not support tailoring"):
            tailor.tailor(1, base="odd")


class TestValidatorIntegration:
    """Validator integration with templates module."""

    def test_validator_returns_template_metadata(self):
        content = read_fixture("fixture_custom_macros.tex")
        val = validate_resume_text(content, run_compile=False)
        assert val["template"]["template"] == "custom_macros"
        assert val.get("can_tailor") is True
        assert val.get("parses_facts") is True
