"""Tests for Secret/PII Scanner (Brief v14 Section 5).

Verifies that:
1. `scripts/scan_pii.py` source code contains ZERO hardcoded personal names, universities, or fest names.
2. The scanner dynamically loads terms from profile and catches a planted leak in a tracked file.
3. The real repository passes with 0 findings.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import pytest
import yaml

from scripts.scan_pii import load_profile_patterns, scan_repo


def test_scanner_source_contains_no_personal_terms() -> None:
    project_root = Path(__file__).resolve().parent.parent
    scanner_path = project_root / "scripts" / "scan_pii.py"
    scanner_source = scanner_path.read_text(encoding="utf-8")

    # Load personal values from profile files if present
    terms_file = project_root / "data" / "profile" / "terms.yaml"
    profile_file = project_root / "data" / "profile" / "profile.yaml"

    personal_values: list[str] = []
    if terms_file.is_file():
        data = yaml.safe_load(terms_file.read_text(encoding="utf-8")) or {}
        for val in data.values():
            if isinstance(val, str) and len(val.strip()) >= 3:
                personal_values.append(val.strip())
                personal_values.append(val.replace("_", " ").strip())

    if profile_file.is_file():
        data = yaml.safe_load(profile_file.read_text(encoding="utf-8")) or {}
        person = data.get("person", {})
        if isinstance(person, dict):
            for k in ("name", "full_name", "institution", "university", "city"):
                val = person.get(k)
                if isinstance(val, str) and len(val.strip()) >= 3 and val.strip().lower() not in {"the candidate", "student"}:
                    personal_values.append(val.strip())

    from scripts.scan_pii import is_specific_term

    # Generic placeholder values (e.g. a default institution) are stop-listed on purpose and appear in the scanner source.
    personal_values = [value for value in personal_values if is_specific_term(value)]
    if not personal_values:
        pytest.skip("no git-ignored personal profile in this checkout; nothing to compare the scanner source against")

    for val in personal_values:
        assert val.lower() not in scanner_source.lower(), (
            f"Personal term '{val}' must NOT appear in scripts/scan_pii.py source code"
        )


def test_scanner_catches_planted_leak(tmp_path: Path) -> None:
    # 1. Initialize a dummy git repo in tmp_path
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.test"], cwd=str(tmp_path), check=True)

    # 2. Add profile with a secret term
    profile_dir = tmp_path / "data" / "profile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "terms.yaml").write_text("event_name: SecretFest2026\ndownload_name: SpecialName\n", encoding="utf-8")

    # 3. Add clean file
    clean_file = tmp_path / "clean.txt"
    clean_file.write_text("This is clean public text.\n", encoding="utf-8")
    subprocess.run(["git", "add", "clean.txt"], cwd=str(tmp_path), check=True)

    findings_clean = scan_repo(tmp_path)
    assert len(findings_clean) == 0

    # 4. Add file with planted personal term and fake secret
    fake_gemini_key = "AIzaSy" + ("A" * 33)
    leaky_file = tmp_path / "leaky.txt"
    leaky_file.write_text(f"Found at SecretFest2026 with key {fake_gemini_key}.\n", encoding="utf-8")
    subprocess.run(["git", "add", "leaky.txt"], cwd=str(tmp_path), check=True)

    findings = scan_repo(tmp_path)
    assert len(findings) >= 2
    rule_names = [f[2] for f in findings]
    assert any("Profile personal term" in r for r in rule_names)
    assert any("Google / Gemini API Key" in r for r in rule_names)


def test_scanner_detects_planted_term_per_category(tmp_path: Path) -> None:
    """Verify that the scanner detects planted terms for every required category:
    name, university, city, event_names, handles_projects.
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.test"], cwd=str(repo_dir), check=True)

    # 1. Populate data/profile/pii_terms.txt with terms in each category
    profile_dir = repo_dir / "data" / "profile"
    profile_dir.mkdir(parents=True)
    pii_terms_content = """# PII Terms test fixture
# [name]
PlantedSecretName

# [university]
PlantedSecretUniversity

# [city]
PlantedSecretCity

# [event_names]
PlantedSecretFest

# [handles_projects]
PlantedSecretHandle
"""
    (profile_dir / "pii_terms.txt").write_text(pii_terms_content, encoding="utf-8")

    # 2. Plant files referencing each category
    planted_files = {
        "candidate.txt": "Author: PlantedSecretName\n",
        "education.txt": "Attended PlantedSecretUniversity in engineering.\n",
        "location.txt": "Relocating from PlantedSecretCity.\n",
        "events.txt": "Won first prize at PlantedSecretFest.\n",
        "projects.txt": "Code available at PlantedSecretHandle repository.\n",
    }
    for fname, content in planted_files.items():
        (repo_dir / fname).write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", fname], cwd=str(repo_dir), check=True)

    findings = scan_repo(repo_dir)
    assert len(findings) == 5, f"Expected 5 findings (one per category), got: {findings}"

    detected_rules = {f[2] for f in findings}
    assert "Profile personal term (name)" in detected_rules
    assert "Profile personal term (university)" in detected_rules
    assert "Profile personal term (city)" in detected_rules
    assert "Profile personal term (event_names)" in detected_rules
    assert "Profile personal term (handles_projects)" in detected_rules


def test_scanner_terms_from_option(tmp_path: Path) -> None:
    """Verify scanning a clean export repo using --terms-from pointing to an external dir."""
    repo_dir = tmp_path / "export_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.test"], cwd=str(repo_dir), check=True)

    # External profile dir
    ext_dir = tmp_path / "original_data" / "profile"
    ext_dir.mkdir(parents=True)
    (ext_dir / "pii_terms.txt").write_text("# [name]\nExternalSecretPersona\n", encoding="utf-8")

    # Leaky file in export_repo
    (repo_dir / "notes.txt").write_text("Hello ExternalSecretPersona!\n", encoding="utf-8")
    subprocess.run(["git", "add", "notes.txt"], cwd=str(repo_dir), check=True)

    # Scanning repo alone without terms_dir sees 0 findings because repo has no local profile
    assert len(scan_repo(repo_dir)) == 0

    # Scanning with terms_dir pointing to external profile finds the leak
    findings = scan_repo(repo_dir, terms_dir=tmp_path / "original_data")
    assert len(findings) == 1
    assert "Profile personal term (name)" in findings[0][2]


def _synthetic_terms_dir(tmp_path: Path) -> Path:
    """Explicit temp terms directory: tests never read the checkout's own data/profile."""
    terms_dir = tmp_path / "terms" / "profile"
    terms_dir.mkdir(parents=True)
    (terms_dir / "pii_terms.txt").write_text(
        "# [name]\nZyxwvuSyntheticPerson\n# [handles_projects]\nqrstuvw-synthetic-handle\n", encoding="utf-8"
    )
    return tmp_path / "terms"


def test_real_repo_clean(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parent.parent
    findings = scan_repo(project_root, terms_dir=_synthetic_terms_dir(tmp_path))
    assert findings == [], f"Expected 0 findings in tracked files, got: {findings}"


@pytest.mark.parametrize(
    "phrase",
    [
        "Institute of Science and Technology",
        "Example Institute of Science and Technology",
        "University",
        "The Candidate",
        "Computer Science and Engineering",
        "ab",
    ],
)
def test_generic_phrases_are_never_personal_terms(phrase: str) -> None:
    from scripts.scan_pii import is_specific_term

    assert not is_specific_term(phrase)


def test_distinctive_word_is_a_specific_term() -> None:
    from scripts.scan_pii import is_specific_term

    assert is_specific_term("Zyxwvu Polytechnic")
    assert is_specific_term("Qrstuville")


def test_placeholder_profile_yields_no_terms(tmp_path: Path) -> None:
    from scripts.scan_pii import count_loaded_terms

    profile_dir = tmp_path / "data" / "profile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "profile.yaml").write_text(
        "person:\n  display_name: the candidate\n  institution: Institute of Science and Technology\n  city: India\n",
        encoding="utf-8",
    )
    assert count_loaded_terms(tmp_path, tmp_path) == 0


def test_profile_contact_fields_become_terms(tmp_path: Path) -> None:
    from scripts.scan_pii import load_profile_terms

    profile_dir = tmp_path / "data" / "profile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "profile.yaml").write_text(
        "person:\n  display_name: Zyxwvu Qrstuvwx\n  institution: Zyxwvu Polytechnic\n  city: Qrstuville\n"
        "contact:\n  email: zyxwvu.person@mail.example\n  phone: '+91 98765 43210'\n"
        "  github: https://github.com/zyxwvu-handle\n",
        encoding="utf-8",
    )
    counts, terms = load_profile_terms(tmp_path, tmp_path)
    assert counts["profile.yaml"] > 0
    assert {"Zyxwvu Qrstuvwx", "Zyxwvu", "Qrstuvwx"} <= terms["name"]
    assert "Zyxwvu Polytechnic" in terms["university"]
    assert "Qrstuville" in terms["city"]
    assert {"zyxwvu.person@mail.example", "zyxwvu.person", "919876543210"} <= terms["contact"]
    assert "zyxwvu-handle" in terms["handles_projects"]


def test_phone_with_separators_is_detected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "terms" / "profile").mkdir(parents=True)
    (tmp_path / "terms" / "profile" / "profile.yaml").write_text("contact:\n  phone: '+91 98765 43210'\n", encoding="utf-8")
    (repo / "note.txt").write_text("call 91-98765-43210 now\n", encoding="utf-8")
    findings = scan_repo(repo, terms_dir=tmp_path / "terms")
    assert [f[0] for f in findings] == ["note.txt"]
