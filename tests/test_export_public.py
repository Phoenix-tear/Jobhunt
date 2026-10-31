"""scripts/export_public.py with temp folders only (no venv, no network, no real terms)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import export_public

FAKE_TERM = "Zyxwvu Syntheticperson"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=Tester", "-c", "user.email=tester@example.test", "-c", "commit.gpgsign=false", *args],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return done.stdout


@pytest.fixture()
def private_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "private"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (repo / ".gitignore").write_text("data/\nsecret.txt\n", encoding="utf-8")
    (repo / "data" / "profile").mkdir(parents=True)
    (repo / "data" / "profile" / "pii_terms.txt").write_text(f"# [name]\n{FAKE_TERM}\n", encoding="utf-8")
    (repo / "secret.txt").write_text("ignored file\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "private history")
    return repo


def _export(private_repo: Path, *extra: str) -> int:
    return export_public.main(["--source", str(private_repo), "--no-runtime-terms", "--skip-env-checks", *extra])


def test_export_creates_one_noreply_commit_with_only_tracked_files(private_repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "public"
    assert _export(private_repo, "--out", str(out)) == 0
    assert (out / "README.md").is_file() and (out / "src" / "app.py").is_file()
    assert not (out / "data").exists() and not (out / "secret.txt").exists()
    assert _git(out, "rev-list", "--count", "HEAD").strip() == "1"
    emails = _git(out, "show", "-s", "--format=%ae|%ce", "HEAD").strip().split("|")
    assert all("noreply" in e for e in emails)
    assert "private history" not in _git(out, "log", "--format=%s")


def test_refuses_a_folder_with_git_or_content(private_repo: Path, tmp_path: Path) -> None:
    with_git = tmp_path / "has_git"
    (with_git / ".git").mkdir(parents=True)
    with pytest.raises(SystemExit):
        _export(private_repo, "--out", str(with_git))
    not_empty = tmp_path / "not_empty"
    not_empty.mkdir()
    (not_empty / "x.txt").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit):
        _export(private_repo, "--out", str(not_empty))


def test_leak_fails_the_scan_without_printing_the_value(private_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (private_repo / "notes.md").write_text(f"written by {FAKE_TERM}\n", encoding="utf-8")
    _git(private_repo, "add", "notes.md")
    _git(private_repo, "commit", "-q", "-m", "add notes")
    assert _export(private_repo, "--out", str(tmp_path / "public")) == 1
    output = capsys.readouterr().out
    assert "notes.md:1" in output
    assert FAKE_TERM not in output and "Syntheticperson" not in output


def test_zero_terms_is_a_failure(private_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty_terms = tmp_path / "no_terms"
    empty_terms.mkdir()
    code = _export(private_repo, "--out", str(tmp_path / "public"), "--terms-from", str(empty_terms))
    assert code == 1
    assert "personal terms loaded" in capsys.readouterr().out


def test_dirty_tracked_tree_is_refused(private_repo: Path, tmp_path: Path) -> None:
    (private_repo / "README.md").write_text("changed\n", encoding="utf-8")
    assert _export(private_repo, "--out", str(tmp_path / "public")) == 2


def test_onto_makes_a_normal_follow_up_commit(private_repo: Path, tmp_path: Path) -> None:
    first = tmp_path / "public"
    assert _export(private_repo, "--out", str(first)) == 0
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(first), str(clone)], check=True, capture_output=True)
    first_hash = _git(clone, "rev-parse", "HEAD").strip()

    (private_repo / "src" / "app.py").write_text("print('v2')\n", encoding="utf-8")
    (private_repo / "src" / "new.py").write_text("x = 1\n", encoding="utf-8")
    _git(private_repo, "add", "-A")
    _git(private_repo, "commit", "-q", "-m", "second change")

    assert _export(private_repo, "--onto", str(clone)) == 0
    assert _git(clone, "rev-list", "--count", "HEAD").strip() == "2"
    assert _git(clone, "rev-parse", "HEAD~1").strip() == first_hash  # history kept, no rewrite
    assert (clone / "src" / "new.py").read_text(encoding="utf-8") == "x = 1\n"
    emails = _git(clone, "show", "-s", "--format=%ae|%ce", "HEAD").strip().split("|")
    assert all("noreply" in e for e in emails)


def test_parse_pytest_counts() -> None:
    counts = export_public.parse_pytest_counts("....\n3 failed, 2512 passed, 3 skipped, 12 deselected in 233.04s (0:03:53)\n")
    assert counts == {"passed": 2512, "failed": 3, "skipped": 3, "errors": 0}


def test_tracked_data_and_scratch_files_are_not_exported(private_repo: Path, tmp_path: Path) -> None:
    (private_repo / "data" / "eval").mkdir(parents=True)
    (private_repo / "data" / "eval" / "old_report.md").write_text("private report\n", encoding="utf-8")
    (private_repo / "scratch").mkdir()
    (private_repo / "scratch" / "notes.md").write_text("scratch\n", encoding="utf-8")
    _git(private_repo, "add", "-f", "data/eval/old_report.md", "scratch/notes.md")
    _git(private_repo, "commit", "-q", "-m", "track private files")
    out = tmp_path / "public"
    assert _export(private_repo, "--out", str(out)) == 0
    assert not (out / "data").exists() and not (out / "scratch").exists()
    assert (out / "README.md").is_file()
