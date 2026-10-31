"""Reproducible public export: tracked files -> a NEW folder -> one clean commit, scanned and tested.

    python scripts/export_public.py --out ..\\jobhunt-public-v15 [--terms-from <private repo>]
    python scripts/export_public.py --onto <clone of the public repo> [--terms-from <private repo>]

What it does (exits non-zero on any failure, never prints personal values: path + line + kind only):

 a. builds the export from `git ls-files` into a NEW empty folder (refuses a folder that already has `.git`);
 b. `git init -b main` and ONE commit with the noreply identity (or, with --onto, replaces the tree of an
    existing clone and makes a normal follow-up commit: no history rewrite);
 c. runs the PII/secret scanner with the private terms (the term count must be > 0 and findings 0) on the
    tree AND on the commit, and checks the commit metadata is noreply-only;
 d. in a scratch copy: temp venv, install from the lock, the CI pytest marker set, frontend `npm ci`, `npm test`,
    `tsc --noEmit`, `npm run build`, the clean-folder acceptance test, and the lock wheel check, with REAL durations;
 e. prints a one-screen summary.

No network call other than package downloads (pip / npm / Tectonic). It never pushes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
import scan_pii  # noqa: E402  (sibling script; the private copy is always used)

PUBLIC_NAME = "Phoenix-tear"
PUBLIC_EMAIL = "Phoenix-tear@users.noreply.github.com"
COMMIT_MESSAGE = "Jobhunt: public release"
FOLLOW_UP_MESSAGE = "Jobhunt: update public release"
CI_MARKER_EXPRESSION = "not live and not live_gemini and not gpu and not ollama"
MIN_PYTHON = (3, 12)
INCLUDE_RUNTIME_TERMS = True  # git e-mail / OS login of this machine count as personal terms


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    seconds: float | None = None
    failures: list[str] = field(default_factory=list)  # failing test ids (never values)


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", seconds: float | None = None, failures: list[str] | None = None) -> bool:
        self.checks.append(Check(name, ok, detail, seconds, failures or []))
        return ok

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


def run(command: list[str], cwd: Path, *, env: dict[str, str] | None = None, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )


def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return run(["git", "-c", "commit.gpgsign=false", *args], cwd)


def tracked_files(source: Path) -> list[str]:
    result = git(["ls-files", "-z"], source)
    if result.returncode != 0:
        raise SystemExit(f"git ls-files failed in {source}")
    return [name for name in result.stdout.split("\0") if name]


def working_tree_is_clean(source: Path) -> bool:
    return git(["status", "--porcelain", "--untracked-files=no"], source).stdout.strip() == ""


EXCLUDED_PREFIXES = ("data/", ".secrets/", "scratch/")  # private reports, profile examples and scratch files stay private


def publishable(files: list[str]) -> list[str]:
    return [name for name in files if not name.startswith(EXCLUDED_PREFIXES)]


def copy_tracked(source: Path, destination: Path, files: list[str]) -> int:
    copied = 0
    for relative in files:
        origin = source / relative
        if not origin.is_file():  # deleted in the working tree or a submodule entry
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, target)
        copied += 1
    return copied


def require_new_folder(path: Path) -> None:
    if (path / ".git").exists():
        raise SystemExit(f"Refusing {path}: it already has a .git folder. Use a NEW empty folder.")
    if path.exists() and any(path.iterdir()):
        raise SystemExit(f"Refusing {path}: the folder is not empty. Use a NEW empty folder.")


def commit_all(repo: Path, message: str) -> str:
    identity = {
        "GIT_AUTHOR_NAME": PUBLIC_NAME, "GIT_AUTHOR_EMAIL": PUBLIC_EMAIL,
        "GIT_COMMITTER_NAME": PUBLIC_NAME, "GIT_COMMITTER_EMAIL": PUBLIC_EMAIL,
    }
    env = {**os.environ, **identity}
    subprocess.run(["git", "add", "-A"], cwd=str(repo), env=env, check=True, capture_output=True)
    done = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "user.useConfigOnly=false", "commit", "-q", "-m", message],
        cwd=str(repo), env=env, capture_output=True, text=True,
    )
    if done.returncode != 0:
        raise SystemExit(f"git commit failed: {done.stderr.strip()[:300]}")
    return git(["rev-parse", "HEAD"], repo).stdout.strip()


def commit_metadata_is_noreply(repo: Path, revision: str = "HEAD") -> tuple[bool, str]:
    out = git(["show", "-s", "--format=%ae%n%ce", revision], repo).stdout.split()
    bad = [email for email in out if "noreply" not in email.lower()]
    return (not bad and len(out) == 2), f"{len(out)} identities, {len(bad)} not noreply"


def summarize_findings(findings: list[tuple[str, int, str, str]]) -> str:
    # path + line + kind only; the matched text is never printed.
    return "; ".join(f"{path}:{line} [{rule}]" for path, line, rule, _ in findings[:20])


def scan_tree(root: Path, terms_from: Path) -> list[tuple[str, int, str, str]]:
    return scan_pii.scan_repo(root, terms_dir=terms_from, include_runtime=INCLUDE_RUNTIME_TERMS)


def scan_commit(repo: Path, terms_from: Path, revision: str = "HEAD") -> list[tuple[str, int, str, str]]:
    """Extract the commit's tree with `git archive` and scan that (what a friend would receive)."""
    archive = subprocess.run(["git", "archive", "--format=tar", revision], cwd=str(repo), capture_output=True)
    if archive.returncode != 0:
        raise SystemExit("git archive failed")
    with tempfile.TemporaryDirectory(prefix="export-commit-scan-") as tmp:
        tar_path = Path(tmp) / "tree.tar"
        tar_path.write_bytes(archive.stdout)
        extracted = Path(tmp) / "tree"
        extracted.mkdir()
        with tarfile.open(tar_path) as tar:
            tar.extractall(extracted)  # our own archive of a repo we just built
        return scan_pii.scan_repo(extracted, terms_dir=terms_from, include_runtime=INCLUDE_RUNTIME_TERMS)


def parse_pytest_counts(output: str) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    summary = [line for line in output.splitlines() if re.search(r"\b(passed|failed|error)", line) and " in " in line]
    if summary:
        for number, word in re.findall(r"(\d+) (passed|failed|skipped|error|errors)", summary[-1]):
            key = "errors" if word.startswith("error") else word
            counts[key] += int(number)
    return counts


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def timed(report: Report, name: str, command: list[str], cwd: Path, *, env: dict[str, str] | None = None, timeout: int = 1800, counts: bool = False) -> bool:
    started = time.perf_counter()
    try:
        done = run(command, cwd, env=env, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return report.add(name, False, f"{type(exc).__name__}", time.perf_counter() - started)
    elapsed = time.perf_counter() - started
    detail = f"exit {done.returncode}"
    if counts:
        parsed = parse_pytest_counts(done.stdout + done.stderr)
        detail = f"{parsed['passed']} passed, {parsed['failed']} failed, {parsed['errors']} errors, {parsed['skipped']} skipped"
        failed_ids = [line.split(" - ")[0].strip() for line in (done.stdout + done.stderr).splitlines() if line.startswith(("FAILED ", "ERROR "))]
        return report.add(name, done.returncode == 0 and parsed["failed"] == 0 and parsed["errors"] == 0, detail, elapsed, failed_ids[:30])
    if done.returncode != 0:
        tail = (done.stdout + done.stderr).strip().splitlines()[-3:]
        detail += " | " + " / ".join(line.strip()[:120] for line in tail)
    return report.add(name, done.returncode == 0, detail, elapsed)


def run_environment_checks(tree: Path, source: Path, report: Report) -> None:
    """Scratch copy of the export (so the export folder stays exactly the commit): venv + tests + frontend."""
    if sys.version_info < MIN_PYTHON:
        report.add("python version", False, f"need Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]} to run the export checks")
        return
    with tempfile.TemporaryDirectory(prefix="export-verify-") as tmp:
        work = Path(tmp) / "work"
        if (tree / ".git").is_dir():
            clone = run(["git", "clone", "-q", "--no-hardlinks", str(tree), str(work)], Path(tmp))  # exactly the committed files
            if clone.returncode != 0:
                report.add("scratch clone of the export", False, clone.stderr.strip()[:120])
                return
        else:  # --onto stage: tests (acceptance, upgrade) need a git repo, so commit the stage in the scratch copy
            shutil.copytree(tree, work)
            git(["init", "-q", "-b", "main"], work)
            commit_all(work, "scratch")
        report.add("fresh copy has no data/ folder", not (work / "data").exists(), "")
        venv = Path(tmp) / "venv"
        if not timed(report, "create venv", [sys.executable, "-m", "venv", str(venv)], work):
            return
        py = str(venv_python(venv))
        if not timed(report, "pip install requirements.lock", [py, "-m", "pip", "install", "-q", "-r", "requirements.lock", "-e", ".[api,gmail,matching,dev]"], work, timeout=1800):
            return
        base_env = {k: v for k, v in os.environ.items() if k not in {"JOBHUNT_DATA_DIR", "CI", "GITHUB_ACTIONS"}}
        timed(report, "pytest (CI marker set)", [py, "-m", "pytest", "-m", CI_MARKER_EXPRESSION, "-q", "-p", "no:cacheprovider"], work, env=base_env, counts=True)
        npm = shutil.which("npm")
        npx = shutil.which("npx")
        if npm and npx:
            web = work / "web"
            if timed(report, "npm ci", [npm, "ci", "--silent"], web, timeout=900):
                timed(report, "npm test", [npm, "test", "--silent"], web)
                timed(report, "tsc --noEmit", [npx, "tsc", "--noEmit"], web)
                timed(report, "npm run build", [npm, "run", "build", "--silent"], web)
        else:
            report.add("node/npm available", False, "npm or npx not found on PATH")
        accept_env = {**base_env, "CI": "true"}
        timed(report, "clean-folder acceptance test", [py, "-m", "pytest", "tests/test_v13_acceptance.py", "-q", "-p", "no:cacheprovider"], work, env=accept_env, counts=True)
    timed(report, "lock wheels (py3.12/3.13 x win/mac/linux)", [sys.executable, str(tree / "scripts" / "check_lock_wheels.py"), str(tree / "requirements.lock")], tree, timeout=1800)


def print_summary(report: Report, header: list[str]) -> None:
    print("=" * 78)
    for line in header:
        print(line)
    print("-" * 78)
    for check in report.checks:
        mark = "PASS" if check.ok else "FAIL"
        duration = f"{check.seconds:7.1f}s" if check.seconds is not None else "        "
        print(f"[{mark}] {check.name:<42} {duration}  {check.detail}"[:200])
        for failing in check.failures:
            print(f"        {failing}"[:200])
    print("-" * 78)
    print("RESULT:", "ALL CHECKS PASSED" if report.ok else "FAILED (see lines above)")
    print("=" * 78)


def apply_onto(clone: Path, stage: Path) -> str:
    """Replace the clone's tracked tree with the staged export and make a normal follow-up commit."""
    if not (clone / ".git").is_dir():
        raise SystemExit(f"--onto needs a git clone, but {clone} has no .git")
    for relative in tracked_files(clone):
        victim = clone / relative
        if victim.is_file():
            victim.unlink()
    for path in sorted(clone.rglob("*"), reverse=True):  # remove emptied folders (never .git)
        if ".git" in path.relative_to(clone).parts:
            continue
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    for origin in stage.rglob("*"):
        if origin.is_file():
            target = clone / origin.relative_to(stage)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target)
    if git(["status", "--porcelain"], clone).stdout.strip() == "":
        raise SystemExit("--onto: the clone already matches the export; nothing to commit")
    return commit_all(clone, FOLLOW_UP_MESSAGE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--out", type=Path, help="NEW empty folder for the export (one orphan commit)")
    target.add_argument("--onto", type=Path, help="existing clone of the public repo: replace its tree, add a follow-up commit")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="private repository (default: this one)")
    parser.add_argument("--terms-from", type=Path, default=None, help="directory holding the git-ignored profile terms (default: --source)")
    parser.add_argument("--allow-dirty", action="store_true", help="export even if tracked files have uncommitted changes (tests only)")
    parser.add_argument("--no-runtime-terms", action="store_true", help="ignore this machine's git e-mail / OS login as terms (tests only)")
    parser.add_argument("--skip-env-checks", action="store_true", help="skip venv/pytest/npm/acceptance/wheel checks (unit tests of this script only)")
    args = parser.parse_args(argv)
    global INCLUDE_RUNTIME_TERMS
    INCLUDE_RUNTIME_TERMS = not args.no_runtime_terms

    source = args.source.resolve()
    terms_from = (args.terms_from or source).resolve()
    report = Report()

    if not args.allow_dirty and not working_tree_is_clean(source):
        print("Refusing: the private repo has uncommitted changes to tracked files. Commit first.", file=sys.stderr)
        return 2

    if args.out:
        out = args.out.resolve()
        require_new_folder(out)
        out.mkdir(parents=True, exist_ok=True)
        tree = out
    else:
        clone = args.onto.resolve()
        stage_holder = tempfile.TemporaryDirectory(prefix="export-stage-")
        tree = Path(stage_holder.name) / "stage"
        tree.mkdir()

    files = publishable(tracked_files(source))
    copied = copy_tracked(source, tree, files)
    report.add("export built from git ls-files", copied > 0, f"{copied} files")
    forbidden = [f for f in files if f.startswith(("data/", ".secrets/")) or Path(f).name.startswith(".env") and Path(f).name != ".env.example"]
    report.add("no data/, .secrets/ or .env files tracked", not forbidden, f"{len(forbidden)} found")

    terms = scan_pii.count_loaded_terms(terms_from, terms_from, include_runtime=INCLUDE_RUNTIME_TERMS)
    report.add("personal terms loaded (must be > 0)", terms > 0, f"{terms} terms")
    findings = scan_tree(tree, terms_from) if terms > 0 else []
    report.add("scan: tree has zero findings", terms > 0 and not findings, f"{len(findings)} findings {summarize_findings(findings)}")

    commit_hash = ""
    if args.out:
        git(["init", "-q", "-b", "main"], tree)
        commit_hash = commit_all(tree, COMMIT_MESSAGE)
        report.add("single commit created", git(["rev-list", "--count", "HEAD"], tree).stdout.strip() == "1", commit_hash[:7])
        repo_for_commit = tree
    else:
        repo_for_commit = None

    if repo_for_commit is not None and terms > 0:
        commit_findings = scan_commit(repo_for_commit, terms_from)
        report.add("scan: commit tree has zero findings", not commit_findings, f"{len(commit_findings)} findings {summarize_findings(commit_findings)}")
        ok, detail = commit_metadata_is_noreply(repo_for_commit)
        report.add("commit author/committer are noreply", ok, detail)

    if not args.skip_env_checks:
        run_environment_checks(tree, source, report)

    if args.onto and report.ok:
        commit_hash = apply_onto(clone, tree)
        report.add("follow-up commit created on the clone", True, commit_hash[:7])
        if terms > 0:
            follow_findings = scan_commit(clone, terms_from)
            report.add("scan: follow-up commit tree has zero findings", not follow_findings, f"{len(follow_findings)} findings {summarize_findings(follow_findings)}")
            ok, detail = commit_metadata_is_noreply(clone)
            report.add("follow-up author/committer are noreply", ok, detail)
    if args.onto:
        stage_holder.cleanup()

    where = f"export: {tree}" if args.out else f"onto: {clone}"
    print_summary(report, [where, f"commit: {commit_hash[:12] or '(none)'}   terms loaded: {terms}"])
    summary_path = (tree.parent / f"{tree.name}.export-summary.json") if args.out else None
    if summary_path is not None:
        summary_path.write_text(
            json.dumps({"ok": report.ok, "commit": commit_hash, "terms": terms, "checks": [c.__dict__ for c in report.checks]}, indent=1),
            encoding="utf-8",
        )
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
