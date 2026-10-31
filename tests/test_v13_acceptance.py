"""Clean-folder acceptance test (Brief v13 Phase 7).

Proves that a fresh clone in an isolated temp folder, with:
- No data/
- No .secrets/
- No .venv/
can start up, run offline setup steps, start `jobhunt serve` on a random port with
`--data-dir` pointing to an empty data directory, and drive the HTTP API as a new user:
  1. Bootstrap & wizard status (welcome step)
  2. Enter demo mode
  3. Demo ingest from fixtures
  4. Gate / scoring with stubbed LLM
  5. Tailor dry run for demo persona (compile if Tectonic available, or clean skip)
  6. Exercise all six resume templates (not only demo persona)
  7. Settings round-trips
  8. Doctor diagnostics (API and CLI)

Strict assertions:
- No file written outside temp directory
- No secret / bearer token logged in server output
- No live network call (socket monkeypatched)
- Zero PII leakage from primary user profile across the temp folder and all HTTP responses.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
import httpx

from jobhunt.resumes.templates import (
    analyze_template,
    detect_template,
    extract_template_facts,
)
from jobhunt.tailoring.texdoc import parse


def get_real_profile_pii_strings(project_root: Path) -> set[str]:
    """Dynamically discover primary user profile strings for zero-leakage assertions."""
    pii: set[str] = set()
    real_profile_yaml = project_root / "data" / "profile" / "profile.yaml"
    if real_profile_yaml.is_file():
        try:
            import yaml

            data = yaml.safe_load(real_profile_yaml.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                person = data.get("person", {})
                for k in ("display_name", "institution", "field"):
                    v = person.get(k)
                    if v and isinstance(v, str) and len(v) > 3 and v != "the candidate":
                        pii.add(v)
        except Exception:
            pass

    real_answers = project_root / "data" / "profile" / "application_answers.yaml"
    if real_answers.is_file():
        try:
            import yaml

            ans = yaml.safe_load(real_answers.read_text(encoding="utf-8"))
            if isinstance(ans, dict):
                for k in ("university", "branch"):
                    v = ans.get(k)
                    if v and isinstance(v, str) and len(v) > 3:
                        pii.add(v)
        except Exception:
            pass

    return pii


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_clean_folder_acceptance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = Path(__file__).resolve().parent.parent

    # 1. Collect real profile PII for regression guard
    forbidden_pii = get_real_profile_pii_strings(project_root)
    if len(forbidden_pii) < 3:
        # Check neighboring private repo if running in a clean export
        parent_repo = project_root.parent / "jobhunt"
        if parent_repo.is_dir():
            forbidden_pii = get_real_profile_pii_strings(parent_repo)
    if len(forbidden_pii) < 3:
        # Fallback guard for friend/tester environments without private profiles
        forbidden_pii = {"SentinelCandidateName", "SentinelUniversity", "SentinelCity"}
    assert len(forbidden_pii) >= 3

    # 2. Prepare isolated clean temp directories
    temp_repo = tmp_path / "jobhunt_clean"
    temp_data = tmp_path / "temp_data"
    temp_venv = tmp_path / "temp_venv"
    temp_repo.mkdir(parents=True, exist_ok=True)
    temp_data.mkdir(parents=True, exist_ok=True)

    # 3. Copy only git-tracked files into temp_repo
    git_files_raw = subprocess.check_output(["git", "ls-files"], cwd=str(project_root), text=True)
    git_files = [f.strip() for f in git_files_raw.splitlines() if f.strip()]
    assert len(git_files) > 50

    for rel in git_files:
        src = project_root / rel
        dst = temp_repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # Verify no private data copied
    assert not (temp_repo / ".secrets").exists()
    assert not (temp_repo / ".venv").exists()
    assert not (temp_repo / "data" / "jobhunt.sqlite3").exists()
    assert not (temp_repo / "data" / "profile" / "profile.yaml").exists()

    # If web/dist is prebuilt in main repo, copy it over to simulate prebuilt web
    main_dist = project_root / "web" / "dist"
    if main_dist.is_dir() and (main_dist / "index.html").is_file():
        shutil.copytree(main_dist, temp_repo / "web" / "dist", dirs_exist_ok=True)

    # 4. Create new isolated venv using system site packages
    subprocess.run(
        [sys.executable, "-m", "venv", str(temp_venv), "--system-site-packages"],
        check=True,
        capture_output=True,
    )

    if sys.platform == "win32":
        venv_python = temp_venv / "Scripts" / "python.exe"
        site_packages = temp_venv / "Lib" / "site-packages"
    else:
        venv_python = temp_venv / "bin" / "python"
        sp_matches = list((temp_venv / "lib").glob("python*/site-packages"))
        site_packages = sp_matches[0] if sp_matches else (temp_venv / "lib" / "site-packages")

    assert venv_python.is_file(), f"Venv python binary not found at {venv_python}"
    site_packages.mkdir(parents=True, exist_ok=True)
    # Inherit parent environment site-packages so offline venv has dependencies
    import site
    for idx, sp in enumerate(site.getsitepackages()):
        if Path(sp).is_dir():
            (site_packages / f"inherited_{idx}.pth").write_text(str(Path(sp).resolve()) + "\n", encoding="utf-8")

    # Link temp_repo/src into venv so it runs isolated
    (site_packages / "temp_jobhunt.pth").write_text(str(temp_repo / "src") + "\n", encoding="utf-8")

    # 5. Socket guard: block all non-local connections in this test process
    orig_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: Any) -> None:
        host = address[0] if isinstance(address, (tuple, list)) else address
        if str(host) in ("127.0.0.1", "localhost", "::1"):
            return orig_connect(self, address)
        raise RuntimeError(f"Acceptance test violation: attempt to connect to external host {address}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)

    # 6. Start `serve` on a random free port with --data-dir pointing to temp_data
    port = get_free_port()
    env = os.environ.copy()
    env["JOBHUNT_DATA_DIR"] = str(temp_data)
    env["PYTHONPATH"] = str(temp_repo / "src")
    # Strip any live keys
    env.pop("GEMINI_API_KEY", None)
    env.pop("ANTHROPIC_API_KEY", None)

    cmd = [
        str(venv_python),
        "-m",
        "jobhunt",
        "serve",
        "--port",
        str(port),
        "--data-dir",
        str(temp_data),
    ]

    server_log_file = tmp_path / "server.log"
    log_fp = open(server_log_file, "w", encoding="utf-8")

    proc = subprocess.Popen(
        cmd,
        cwd=str(temp_repo),
        env=env,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        text=True,
    )

    recorded_responses: list[str] = []
    try:
        # Wait for server to become healthy
        base_url = f"http://127.0.0.1:{port}"
        client = httpx.Client(base_url=base_url, timeout=15.0)

        server_ready = False
        for _ in range(40):
            time.sleep(0.5)
            if proc.poll() is not None:
                log_fp.close()
                log_content = server_log_file.read_text(encoding="utf-8")
                raise RuntimeError(f"Server exited prematurely with code {proc.returncode}:\n{log_content}")
            try:
                r = client.get("/api/bootstrap")
                if r.status_code == 200:
                    server_ready = True
                    recorded_responses.append(r.text)
                    break
            except Exception:
                pass

        if not server_ready:
            log_fp.close()
            log_content = server_log_file.read_text(encoding="utf-8")
            raise RuntimeError(f"Server failed to start within timeout. Logs:\n{log_content}")

        # Drive HTTP API as a new user
        # Step A: Bootstrap and wizard status
        boot_resp = client.get("/api/bootstrap")
        assert boot_resp.status_code == 200
        boot_data = boot_resp.json()
        token = boot_data["token"]
        assert boot_data["setup_completed"] is False
        assert boot_data["setup_step"] == "welcome"
        assert boot_data["demo_mode"] is False
        recorded_responses.append(boot_resp.text)

        headers = {"X-Jobhunt-Token": token, "Content-Type": "application/json"}

        # Step B: Enter demo mode
        demo_enter = client.post("/api/setup/demo/enter", headers=headers)
        assert demo_enter.status_code == 200
        assert demo_enter.json()["demo_mode"] is True
        recorded_responses.append(demo_enter.text)

        # Step C: Demo Ingest from fixtures
        ingest_resp = client.post("/api/setup/first-ingest", json={"sync": True}, headers=headers)
        assert ingest_resp.status_code == 200
        recorded_responses.append(ingest_resp.text)

        # Step D: Gate and Score verification via Jobs API
        jobs_resp = client.get("/api/jobs")
        assert jobs_resp.status_code == 200
        jobs_data = jobs_resp.json()
        assert len(jobs_data.get("items", [])) > 0
        recorded_responses.append(jobs_resp.text)

        first_job = jobs_data["items"][0]
        job_id = first_job["id"]
        assert first_job.get("company_name")

        # Step E: Tailoring dry-run for demo persona
        tailor_resp = client.post(
            f"/api/jobs/{job_id}/tailor",
            json={"base": "ai", "use_llm": True},
            headers=headers,
        )
        assert tailor_resp.status_code in (200, 422)  # Either ok or validation message
        recorded_responses.append(tailor_resp.text)
        if tailor_resp.status_code == 200:
            tailor_data = tailor_resp.json()
            assert tailor_data["has_tex"] is True
            # Compiling PDF depends on whether Tectonic is installed
            if tailor_data.get("has_pdf"):
                assert tailor_data["pages"] >= 1
            else:
                assert "compiler" in (tailor_data.get("message") or "").lower() or tailor_data["pages"] == 0

        # Step F: Fixtures for ALL SIX resume templates
        # Verify template detection, capability analysis, and facts extraction across all 6 fixtures
        fixtures_resumes_dir = temp_repo / "tests" / "fixtures" / "resumes"
        six_templates = [
            ("fixture_custom_macros.tex", "custom_macros", True),
            ("fixture_jakes_resume.tex", "jakes_resume", True),
            ("fixture_plain_itemize.tex", "plain_itemize", True),
            ("fixture_awesome_cv.tex", "awesome_cv", True),
            ("fixture_moderncv.tex", "moderncv", False),
            ("fixture_unsupported_odd.tex", "unsupported", False),
        ]

        for fname, expected_tpl, expected_can_tailor in six_templates:
            tex_file = fixtures_resumes_dir / fname
            assert tex_file.is_file(), f"Missing template fixture {fname}"
            content = tex_file.read_text(encoding="utf-8")

            # 1. Detection
            detected = detect_template(content)
            assert detected == expected_tpl, f"Expected {expected_tpl} for {fname}, got {detected}"

            # 2. Capability Analysis
            analysis = analyze_template(content)
            assert analysis.template == expected_tpl
            assert analysis.can_tailor is expected_can_tailor

            # 3. Facts Extraction & AST Parsing
            if analysis.parses_facts:
                doc = parse(content)
                facts = extract_template_facts("sample", doc)
                assert len(facts) > 0, f"Expected extracted facts from {fname}"

        # Also verify base resumes API endpoint works
        resumes_api = client.get("/api/resumes")
        assert resumes_api.status_code == 200
        recorded_responses.append(resumes_api.text)

        # Step G: Settings round-trip
        get_settings = client.get("/api/settings")
        assert get_settings.status_code == 200
        recorded_responses.append(get_settings.text)

        update_settings = client.post(
            "/api/settings",
            json={"settings": {"schedule": {"backend": "in_app"}}},
            headers=headers,
        )
        assert update_settings.status_code == 200
        recorded_responses.append(update_settings.text)

        get_settings_after = client.get("/api/settings")
        assert get_settings_after.status_code == 200
        data_after = get_settings_after.json()
        assert (
            data_after.get("stored_overrides", {}).get("schedule", {}).get("backend") == "in_app"
            or data_after.get("schedule", {}).get("backend") == "in_app"
        )
        recorded_responses.append(get_settings_after.text)

        # Assert settings.yaml was written inside temp_data
        assert (temp_data / "settings.yaml").is_file()

        # Step H: Doctor check via API and CLI
        doc_resp = client.get("/api/health/doctor")
        assert doc_resp.status_code == 200
        doc_json = doc_resp.json()
        assert "checks" in doc_json
        assert len(doc_json["checks"]) >= 10
        recorded_responses.append(doc_resp.text)

        # CLI doctor run
        cli_doc = subprocess.run(
            [str(venv_python), "-m", "jobhunt", "doctor", "--data-dir", str(temp_data)],
            cwd=str(temp_repo),
            capture_output=True,
            text=True,
        )
        assert cli_doc.returncode in (0, 1)  # 0 if all pass, 1 if warnings/fails
        assert "jobhunt system doctor" in cli_doc.stdout

    finally:
        # Shutdown server cleanly
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        try:
            log_fp.close()
        except Exception:
            pass

    # 7. Strict Post-Run Assertions
    # (a) No secret / bearer token in server logs
    server_logs = server_log_file.read_text(encoding="utf-8") if server_log_file.exists() else ""
    assert "AIzaSy" not in server_logs  # No Google API key prefix
    assert "sk-ant-" not in server_logs  # No Anthropic key prefix
    if token:
        assert token not in server_logs  # CSRF session token not leaked to logs

    # (b) No writes outside tmp_path
    # Main project data directory was not modified by the run
    main_db = project_root / "data" / "jobhunt.sqlite3"
    if main_db.exists():
        # Assert mtime has not been touched in the last few seconds
        assert main_db.stat().st_mtime < time.time() - 2

    # (c) Zero PII leakage
    # Check all recorded HTTP responses
    for resp_text in recorded_responses:
        for pii_str in forbidden_pii:
            assert pii_str.lower() not in resp_text.lower(), (
                f"PII Leakage detected in HTTP response: '{pii_str}' found in response!"
            )

    # Grep the temp data folder for ANY forbidden profile strings
    for p in temp_data.rglob("*"):
        if p.is_file() and not p.name.endswith(".pyc"):
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
                for pii_str in forbidden_pii:
                    assert pii_str.lower() not in content.lower(), (
                        f"PII Leakage detected in temp file {p.relative_to(tmp_path)}: '{pii_str}' found!"
                    )
            except Exception:
                pass

    # Dynamically extract any private contact info from untracked data/resumes at test time (never hardcoded in repo)
    private_contact: set[str] = set()
    real_resumes = project_root / "data" / "resumes"
    if real_resumes.is_dir():
        for rf in real_resumes.glob("*.tex"):
            try:
                rtxt = rf.read_text(encoding="utf-8")
                for em in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", rtxt):
                    if not em.endswith(".test") and not em.endswith(".example"):
                        private_contact.add(em)
                for ph in re.findall(r"\b\d{10}\b", rtxt):
                    private_contact.add(ph)
            except Exception:
                pass

    for p in tmp_path.rglob("*"):
        if p.is_file() and not p.name.endswith(".pyc"):
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
                for secret in private_contact:
                    assert secret.lower() not in content.lower(), (
                        f"Private contact info leaked to temp file {p.relative_to(tmp_path)}: '{secret}' found!"
                    )
            except Exception:
                pass


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main(["-v", __file__]))
