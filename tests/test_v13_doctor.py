"""Tests for jobhunt doctor checks (Brief v13)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jobhunt.config import Config
from jobhunt.doctor import DoctorCheck, SystemDoctor


@pytest.fixture
def test_config(tmp_path: Path) -> Config:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    secrets_dir = tmp_path / ".secrets"
    secrets_dir.mkdir()
    return Config(
        project_root=tmp_path,
        data_dir=data_dir,
        database_path=str(data_dir / "jobhunt.sqlite3"),
    )


def test_check_os_arch(test_config: Config) -> None:
    doc = SystemDoctor(test_config)
    res = doc.check_os_arch()
    assert res.status == "pass"
    assert "Architecture" in res.name


def test_check_python_version(test_config: Config) -> None:
    doc = SystemDoctor(test_config)
    res = doc.check_python_version()
    assert res.status == "pass"
    assert "Python" in res.name


def test_check_venv(test_config: Config) -> None:
    doc = SystemDoctor(test_config)
    res = doc.check_venv()
    assert res.status in ("pass", "warn")


def test_check_node_version_missing(test_config: Config) -> None:
    doc = SystemDoctor(test_config)
    with patch("shutil.which", return_value=None):
        res = doc.check_node_version()
        assert res.status == "warn"
        assert "not found" in res.message


def test_check_node_version_present(test_config: Config) -> None:
    doc = SystemDoctor(test_config)
    mock_proc = MagicMock(stdout="v22.14.0\n", stderr="", returncode=0)
    with patch("shutil.which", return_value="node"), \
         patch("subprocess.run", return_value=mock_proc):
        res = doc.check_node_version()
        assert res.status == "pass"
        assert "22.14.0" in res.message


def test_check_node_version_too_old(test_config: Config) -> None:
    doc = SystemDoctor(test_config)
    mock_proc = MagicMock(stdout="v18.15.0\n", stderr="", returncode=0)
    with patch("shutil.which", return_value="node"), \
         patch("subprocess.run", return_value=mock_proc):
        res = doc.check_node_version()
        assert res.status == "warn"
        assert "v18.15.0 is older than v22.12" in res.message


def test_check_node_version_22_11_is_too_old(test_config: Config) -> None:
    doc = SystemDoctor(test_config)
    mock_proc = MagicMock(stdout="v22.11.0\n", stderr="", returncode=0)
    with patch("shutil.which", return_value="node"), \
         patch("subprocess.run", return_value=mock_proc):
        assert doc.check_node_version().status == "warn"


def test_check_disk_space(test_config: Config) -> None:
    doc = SystemDoctor(test_config)
    res = doc.check_disk_space()
    assert res.status in ("pass", "warn")


def test_check_port(test_config: Config) -> None:
    doc = SystemDoctor(test_config)
    res = doc.check_port(8000)
    assert res.status in ("pass", "warn")


def test_check_write_access(test_config: Config) -> None:
    doc = SystemDoctor(test_config)
    res = doc.check_write_access()
    assert res.status == "pass"


def test_check_sqlite_fts5(test_config: Config) -> None:
    doc = SystemDoctor(test_config)
    res = doc.check_sqlite_fts5()
    assert res.status == "pass"
    assert "FTS5" in res.message


def test_check_timezone(test_config: Config) -> None:
    doc = SystemDoctor(test_config)
    res = doc.check_timezone()
    assert res.status == "pass"


def test_check_gemini_key_present(test_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyTestKey123456789")
    doc = SystemDoctor(test_config)
    res = doc.check_gemini_key()
    assert res.status == "pass"
    assert "ends in" in res.message


def test_check_gemini_key_missing(test_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    doc = SystemDoctor(test_config)
    res = doc.check_gemini_key()
    assert res.status == "warn"


def test_check_tectonic_missing(test_config: Config) -> None:
    doc = SystemDoctor(test_config)
    with patch("jobhunt.doctor.find_engine", return_value=None):
        res = doc.check_tectonic()
        assert res.status == "warn"
        assert "No LaTeX engine found" in res.message


def test_run_all_checks_offline(test_config: Config) -> None:
    doc = SystemDoctor(test_config, online=False)
    checks = doc.run_all_checks()
    assert len(checks) >= 12
    ids = [c.id for c in checks]
    assert "os_arch" in ids
    assert "python_version" in ids
    assert "sqlite_fts5" in ids


def test_tectonic_cache_empty_detection(tmp_path: Path) -> None:
    from jobhunt.doctor import tectonic_cache_is_empty

    assert tectonic_cache_is_empty(tmp_path / "missing")
    assert tectonic_cache_is_empty(tmp_path)
    (tmp_path / "file").write_text("x", encoding="utf-8")
    assert not tectonic_cache_is_empty(tmp_path)


def test_tectonic_first_run_uses_long_timeout_and_timeout_is_a_warning(test_config: Config) -> None:
    from jobhunt.doctor import TECTONIC_FIRST_RUN_TIMEOUT_SECONDS
    from jobhunt.tailoring.latex import CompileResult

    seen: dict[str, int] = {}

    def fake_compile(*_args, timeout: int, **_kwargs):  # noqa: ANN002, ANN003
        seen["timeout"] = timeout
        return CompileResult(False, None, 0, "tectonic", f"Compilation timed out after {timeout} seconds")

    doc = SystemDoctor(test_config)
    with patch("jobhunt.doctor.find_engine", return_value=("tectonic", "tectonic.exe")), \
         patch("jobhunt.doctor.tectonic_cache_is_empty", return_value=True), \
         patch("jobhunt.doctor.compile_sandboxed", side_effect=fake_compile):
        res = doc.check_tectonic()
    assert seen["timeout"] == TECTONIC_FIRST_RUN_TIMEOUT_SECONDS == 180
    assert res.status == "warn"
    assert "first compile downloads" in res.message
    assert "180" in res.message


def test_tectonic_warm_cache_uses_short_timeout(test_config: Config) -> None:
    from jobhunt.doctor import TECTONIC_WARM_TIMEOUT_SECONDS
    from jobhunt.tailoring.latex import CompileResult

    seen: dict[str, int] = {}

    def fake_compile(*_args, timeout: int, **_kwargs):  # noqa: ANN002, ANN003
        seen["timeout"] = timeout
        return CompileResult(False, None, 0, "tectonic", "boom")

    doc = SystemDoctor(test_config)
    with patch("jobhunt.doctor.find_engine", return_value=("tectonic", "tectonic.exe")), \
         patch("jobhunt.doctor.tectonic_cache_is_empty", return_value=False), \
         patch("jobhunt.doctor.compile_sandboxed", side_effect=fake_compile):
        res = doc.check_tectonic()
    assert seen["timeout"] == TECTONIC_WARM_TIMEOUT_SECONDS
    assert res.status == "warn"


def test_doctor_report_hides_the_home_folder_and_login_name(test_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses

    from jobhunt.doctor import generate_doctor_report, scrub_paths

    fake_home = tmp_path / "Users" / "zyxwvulogin"
    project = fake_home / "projects" / "jobhunt"
    project.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    config = dataclasses.replace(test_config, project_root=project, data_dir=None)
    monkeypatch.setattr("jobhunt.doctor.get_python_executable", lambda root: project / ".venv" / "Scripts" / "python.exe")
    report = generate_doctor_report(config, out_path=tmp_path / "report.txt").read_text(encoding="utf-8")
    assert "zyxwvulogin" not in report.lower()
    assert "<project>" in report

    forward = str(project).replace("\\", "/")
    assert scrub_paths(f"see {forward}/data and {project}\data", {str(project): "<project>"}) == "see <project>/data and <project>\data"
