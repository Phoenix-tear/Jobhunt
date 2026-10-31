"""System diagnostics and health verification (Brief v13 `jobhunt doctor`)."""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jobhunt.config import Config
from jobhunt.platform.versions import MIN_NODE, MIN_PYTHON, format_version, node_is_supported
from jobhunt.platform.paths import (
    find_ollama_executable,
    get_os_arch,
    get_python_executable,
    get_tectonic_search_paths,
)
from jobhunt.settings.secrets import get_gemini_key, get_secrets_dir, resolve_gemini_key
from jobhunt.tailoring.latex import compile_sandboxed, find_engine
from jobhunt.timeutil import utc_now

logger = logging.getLogger(__name__)


@dataclass
class DoctorCheck:
    id: str
    name: str
    status: str       # "pass" | "warn" | "fail"
    message: str
    hint: str | None = None
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SystemDoctor:
    def __init__(self, config: Config, *, online: bool = False) -> None:
        self.config = config
        self.online = online

    def run_all_checks(self) -> list[DoctorCheck]:
        return [
            self.check_os_arch(),
            self.check_python_version(),
            self.check_venv(),
            self.check_node_version(),
            self.check_disk_space(),
            self.check_port(),
            self.check_write_access(),
            self.check_tectonic(),
            self.check_ollama(),
            self.check_gpu(),
            self.check_gemini_key(),
            self.check_sqlite_fts5(),
            self.check_timezone(),
            self.check_gmail(),
        ]

    def check_os_arch(self) -> DoctorCheck:
        os_arch = get_os_arch()
        sys_desc = f"{platform.system()} {platform.release()} ({os_arch})"
        return DoctorCheck(
            id="os_arch",
            name="OS & Architecture",
            status="pass",
            message=sys_desc,
        )

    def check_python_version(self) -> DoctorCheck:
        v = sys.version_info
        ver_str = f"{v.major}.{v.minor}.{v.micro}"
        if v >= MIN_PYTHON:
            return DoctorCheck(
                id="python_version",
                name="Python Version",
                status="pass",
                message=f"Python {ver_str} (>= {format_version(MIN_PYTHON)} required)",
            )
        return DoctorCheck(
            id="python_version",
            name="Python Version",
            status="fail",
            message=f"Python {ver_str} is unsupported",
            hint=f"Install Python {format_version(MIN_PYTHON)} or newer (https://www.python.org/downloads/)",
        )

    def check_venv(self) -> DoctorCheck:
        in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
        if in_venv:
            return DoctorCheck(
                id="venv",
                name="Virtual Environment",
                status="pass",
                message=f"Active in virtualenv: {sys.prefix}",
            )
        expected_py = get_python_executable(self.config.project_root)
        return DoctorCheck(
            id="venv",
            name="Virtual Environment",
            status="warn",
            message=f"Running outside repo virtualenv with {sys.executable}",
            hint=f"Activate .venv or execute commands with {expected_py}",
        )

    def check_node_version(self) -> DoctorCheck:
        node_exe = shutil.which("node")
        if not node_exe:
            return DoctorCheck(
                id="node_version",
                name="Node.js (for web app)",
                status="warn",
                message="Node.js not found on PATH",
                hint=f"Install Node.js >= {format_version(MIN_NODE)} (22 LTS) to build the frontend (https://nodejs.org/)",
            )
        try:
            res = subprocess.run([node_exe, "-v"], capture_output=True, text=True, timeout=5, check=False)
            ver = res.stdout.strip()
            if node_is_supported(ver):
                return DoctorCheck(
                    id="node_version",
                    name="Node.js (for web app)",
                    status="pass",
                    message=f"Node.js {ver} (>= {format_version(MIN_NODE)} required)",
                )
            return DoctorCheck(
                id="node_version",
                name="Node.js (for web app)",
                status="warn",
                message=f"Node.js {ver} is older than v{format_version(MIN_NODE)}",
                hint=f"Upgrade to Node.js >= {format_version(MIN_NODE)} (22 LTS) for building web assets (https://nodejs.org/)",
            )
        except Exception as exc:
            return DoctorCheck(
                id="node_version",
                name="Node.js (for web app)",
                status="warn",
                message=f"Failed checking node version: {exc}",
                hint="Verify Node.js installation (https://nodejs.org/)",
            )

    def check_disk_space(self) -> DoctorCheck:
        try:
            target_dir = self.config.data_directory
            target_dir.mkdir(parents=True, exist_ok=True)
            usage = shutil.disk_usage(target_dir)
            free_gb = usage.free / (1024 ** 3)
            if free_gb >= 1.0:
                return DoctorCheck(
                    id="disk_space",
                    name="Disk Space",
                    status="pass",
                    message=f"{free_gb:.1f} GB free on {target_dir}",
                )
            return DoctorCheck(
                id="disk_space",
                name="Disk Space",
                status="warn",
                message=f"Low disk space: {free_gb:.2f} GB free on {target_dir}",
                hint="Free at least 1 GB of disk space on the drive holding data/",
            )
        except Exception as exc:
            return DoctorCheck(
                id="disk_space",
                name="Disk Space",
                status="warn",
                message=f"Could not determine disk space: {exc}",
            )

    def check_port(self, port: int = 8000) -> DoctorCheck:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                res = s.connect_ex(("127.0.0.1", port))
                if res != 0:
                    # Port is free
                    return DoctorCheck(
                        id="port_free",
                        name="Local Port 8000",
                        status="pass",
                        message=f"Port {port} is available",
                    )
                # Port is open / in use
                return DoctorCheck(
                    id="port_free",
                    name="Local Port 8000",
                    status="warn",
                    message=f"Port {port} is currently in use",
                    hint="Another process is listening on port 8000. 'jobhunt serve' will automatically pick an available port.",
                )
        except Exception as exc:
            return DoctorCheck(
                id="port_free",
                name="Local Port 8000",
                status="pass",
                message=f"Port check passed ({exc})",
            )

    def check_write_access(self) -> DoctorCheck:
        errors = []
        data_dir = self.config.data_directory
        secrets_dir = get_secrets_dir(self.config.project_root)

        for d, name in [(data_dir, "data/"), (secrets_dir, ".secrets/")]:
            try:
                d.mkdir(parents=True, exist_ok=True)
                test_file = d / f".doctor_probe_{os.getpid()}"
                test_file.write_text("probe", encoding="utf-8")
                test_file.unlink(missing_ok=True)
            except Exception as exc:
                errors.append(f"{name}: {exc}")

        if not errors:
            return DoctorCheck(
                id="write_access",
                name="Storage Permissions",
                status="pass",
                message="Write access verified for data/ and .secrets/",
            )
        return DoctorCheck(
            id="write_access",
            name="Storage Permissions",
            status="fail",
            message="Permission check failed: " + "; ".join(errors),
            hint="Ensure the current user has write access to data/ and .secrets/ directories",
        )

    def check_tectonic(self) -> DoctorCheck:
        found = find_engine(self.config.project_root)
        if not found:
            return DoctorCheck(
                id="tectonic",
                name="LaTeX / Tectonic Engine",
                status="warn",
                message="No LaTeX engine found; PDF generation disabled",
                hint="Download Tectonic into tools/tectonic/ or install via: brew install tectonic (macOS) / choco install tectonic (Windows)",
            )
        engine, exe = found
        # Run 1-line compile test
        with tempfile.TemporaryDirectory() as tmp_d:
            tmp_path = Path(tmp_d)
            test_tex = tmp_path / "test.tex"
            test_tex.write_text(r"\documentclass{article}\begin{document}OK\end{document}", encoding="utf-8")
            first_run = tectonic_cache_is_empty()
            timeout = TECTONIC_FIRST_RUN_TIMEOUT_SECONDS if first_run else TECTONIC_WARM_TIMEOUT_SECONDS
            if first_run:
                print("  (LaTeX engine: the first compile downloads packages, this can take a few minutes)", flush=True)
            res = compile_sandboxed(test_tex, tmp_path / "out", project_root=self.config.project_root, timeout=timeout)
            if res.success and res.pdf_path and res.pdf_path.is_file():
                return DoctorCheck(
                    id="tectonic",
                    name="LaTeX / Tectonic Engine",
                    status="pass",
                    message=f"{engine.title()} ready ({exe})",
                )
            if "timed out" in res.log:
                return DoctorCheck(
                    id="tectonic",
                    name="LaTeX / Tectonic Engine",
                    status="warn",
                    message=(
                        f"{engine.title()} compile test timed out after {timeout} s. This is a warning, not a failure: "
                        "the first compile downloads LaTeX packages, which can take a few minutes on a slow connection."
                    ),
                    hint="Run the doctor again once the download has finished (PDF generation works after the first successful compile).",
                )
            return DoctorCheck(
                id="tectonic",
                name="LaTeX / Tectonic Engine",
                status="warn",
                message=f"{engine.title()} found but compile test failed: {res.log[-200:].strip()}",
                hint="Run 'tectonic -X compile --untrusted <file>' to diagnose package download or font issues",
            )

    def check_ollama(self) -> DoctorCheck:
        import httpx
        url = self.config.schedule.ollama_url or "http://127.0.0.1:11434"
        try:
            resp = httpx.get(f"{url.rstrip('/')}/api/tags", timeout=1.5)
            if resp.status_code == 200:
                data = resp.json()
                models = [m.get("name", "") for m in data.get("models", [])]
                shown = ", ".join(models[:5])
                extra_count = len(models) - 5
                msg_models = f"{shown} (+{extra_count} more)" if extra_count > 0 else (shown or "none installed")
                return DoctorCheck(
                    id="ollama",
                    name="Ollama (Local Models)",
                    status="pass",
                    message=f"Ollama running with {len(models)} model(s): {msg_models}",
                )
        except Exception:
            pass

        # If Ollama is not answering
        ollama_exe = find_ollama_executable()
        embed_provider = getattr(self.config.embedding, "provider", "local")
        if embed_provider == "ollama" or embed_provider == "local":
            return DoctorCheck(
                id="ollama",
                name="Ollama (Local Models)",
                status="warn",
                message="Ollama is not running (configured for local embeddings)",
                hint="Start Ollama ('ollama serve') or switch embeddings.provider to 'gemini' or 'none'",
            )
        return DoctorCheck(
            id="ollama",
            name="Ollama (Local Models)",
            status="pass",
            message=f"Ollama not running (optional; binary: {'found' if ollama_exe else 'not found'})",
        )

    def check_gpu(self) -> DoctorCheck:
        # Check Apple Silicon Metal
        if sys.platform == "darwin":
            try:
                proc = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=2)
                brand = proc.stdout.strip()
                if "Apple" in brand:
                    return DoctorCheck(
                        id="gpu",
                        name="GPU Acceleration",
                        status="pass",
                        message=f"{brand} (Apple Metal GPU acceleration enabled)",
                    )
            except Exception:
                pass

        # Check NVIDIA GPU via nvidia-smi
        nvidia_smi = shutil.which("nvidia-smi")
        if nvidia_smi:
            try:
                proc = subprocess.run(
                    [nvidia_smi, "--query-gpu=name,memory.total", "--format=csv,noheader"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    gpu_info = proc.stdout.strip().replace("\n", "; ")
                    return DoctorCheck(
                        id="gpu",
                        name="GPU Acceleration",
                        status="pass",
                        message=f"NVIDIA GPU detected: {gpu_info}",
                    )
            except Exception:
                pass

        return DoctorCheck(
            id="gpu",
            name="GPU Acceleration",
            status="pass",
            message="CPU mode (no dedicated GPU detected; local LLM optional)",
        )

    def check_gemini_key(self) -> DoctorCheck:
        resolved = resolve_gemini_key(self.config.project_root)
        if not resolved.key:
            return DoctorCheck(
                id="gemini_key",
                name="Gemini API Key",
                status="warn",
                message="No Gemini key found (checked .secrets/, .env, environment)",
                hint="Free tier scoring and letters require a free key from https://aistudio.google.com",
            )

        if not resolved.is_safe:
            return DoctorCheck(
                id="gemini_key",
                name="Gemini API Key",
                status="warn",
                message=f"Gemini key found in unsafe location: {resolved.source}",
                hint=resolved.warning or "Move your key to .secrets/gemini_api_key or .env to avoid accidental git commit",
            )

        last4 = resolved.key[-4:] if len(resolved.key) >= 4 else "…"
        if self.online:
            import httpx
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models?key={resolved.key}"
                resp = httpx.get(url, timeout=5)
                if resp.status_code == 200:
                    return DoctorCheck(
                        id="gemini_key",
                        name="Gemini API Key",
                        status="pass",
                        message=f"Found in {resolved.source} (verified online, ...{last4})",
                    )
                return DoctorCheck(
                    id="gemini_key",
                    name="Gemini API Key",
                    status="warn",
                    message=f"Found in {resolved.source} but API returned status {resp.status_code}",
                    hint="Verify your key at https://aistudio.google.com",
                )
            except Exception as exc:
                return DoctorCheck(
                    id="gemini_key",
                    name="Gemini API Key",
                    status="warn",
                    message=f"Found in {resolved.source} but could not verify online: {exc}",
                )
        return DoctorCheck(
            id="gemini_key",
            name="Gemini API Key",
            status="pass",
            message=f"Found in {resolved.source} (key ends in ...{last4})",
        )

    def check_sqlite_fts5(self) -> DoctorCheck:
        try:
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE VIRTUAL TABLE _test_fts USING fts5(title, body);")
            conn.execute("INSERT INTO _test_fts VALUES ('Software Engineer', 'Python and Rust');")
            res = conn.execute("SELECT * FROM _test_fts WHERE _test_fts MATCH 'python';").fetchall()
            conn.close()
            if res:
                return DoctorCheck(
                    id="sqlite_fts5",
                    name="SQLite FTS5 Full-Text Search",
                    status="pass",
                    message="SQLite FTS5 module available and working",
                )
            return DoctorCheck(
                id="sqlite_fts5",
                name="SQLite FTS5 Full-Text Search",
                status="fail",
                message="FTS5 query did not return expected match",
            )
        except Exception as exc:
            return DoctorCheck(
                id="sqlite_fts5",
                name="SQLite FTS5 Full-Text Search",
                status="fail",
                message=f"SQLite FTS5 unavailable: {exc}",
                hint="Your Python build lacks SQLite FTS5 support. Install a standard Python 3.12+ distribution.",
            )

    def check_timezone(self) -> DoctorCheck:
        tz_name = self.config.schedule.timezone or "UTC"
        try:
            from jobhunt.schedule.definitions import zone
            tz = zone(tz_name)
            now = utc_now().astimezone(tz)
            return DoctorCheck(
                id="timezone",
                name="Time Zone & Clock",
                status="pass",
                message=f"Configured timezone: {tz_name} (local time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')})",
            )
        except Exception as exc:
            return DoctorCheck(
                id="timezone",
                name="Time Zone & Clock",
                status="warn",
                message=f"Invalid timezone '{tz_name}': {exc}",
                hint="Install 'tzdata' (pip install tzdata) or set schedule.timezone in config.yaml to UTC / IST",
            )

    def check_gmail(self) -> DoctorCheck:
        configured, reason = check_gmail_configured(self.config)
        if configured:
            return DoctorCheck(
                id="gmail",
                name="Gmail Integration",
                status="pass",
                message="Gmail credentials and token configured",
            )
        creds_file = self.config.resolve_path(self.config.email.credentials_file)
        token_file = self.config.resolve_path(self.config.email.token_file)
        if not creds_file.is_file() and not token_file.is_file():
            return DoctorCheck(
                id="gmail",
                name="Gmail Integration",
                status="pass",
                message="Optional (not configured)",
                hint="To ingest jobs from email alerts, follow SETUP.md to configure credentials.json and run jobhunt gmail-auth",
            )
        return DoctorCheck(
            id="gmail",
            name="Gmail Integration",
            status="warn",
            message=f"Gmail configured but unusable: {reason}",
            hint="Run `jobhunt gmail-auth` to re-authorize Gmail access",
        )


TECTONIC_WARM_TIMEOUT_SECONDS = 30
TECTONIC_FIRST_RUN_TIMEOUT_SECONDS = 180


def tectonic_cache_dir() -> Path:
    """Where Tectonic stores downloaded LaTeX packages on this OS."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "TectonicProject" / "Tectonic"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "Tectonic"
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "Tectonic"


def tectonic_cache_is_empty(cache_dir: Path | None = None) -> bool:
    cache = cache_dir or tectonic_cache_dir()
    try:
        return not cache.is_dir() or not any(cache.iterdir())
    except OSError:
        return True


def check_gmail_configured(config: Config) -> tuple[bool, str]:
    """Check if Gmail is configured with credentials and a loadable token."""
    creds_file = config.resolve_path(config.email.credentials_file)
    token_file = config.resolve_path(config.email.token_file)

    if not creds_file.is_file():
        return False, f"credentials file not found: {creds_file.name}"
    if not token_file.is_file():
        return False, f"token file not found: {token_file.name}"

    try:
        import json
        data = json.loads(token_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"token file is not valid JSON: {exc}"

    if not isinstance(data, dict):
        return False, "token file content is not a JSON object"

    from jobhunt.alerts.gmail import READONLY_SCOPE
    scopes = set(data.get("scopes") or [])
    if READONLY_SCOPE not in scopes:
        return False, f"token missing {READONLY_SCOPE} scope"

    try:
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(str(token_file), [READONLY_SCOPE])
        if not creds.valid:
            if not creds.refresh_token:
                return False, "token expired and has no refresh token"
            from jobhunt.schedule.runner import gmail_token_status
            usable, reason = gmail_token_status(config)
            if not usable:
                return False, f"token unusable: {reason}"
    except Exception as exc:
        return False, f"token load error: {exc}"

    return True, "Gmail credentials and token valid"



def scrub_text(text: str, custom_pii: set[str] | None = None) -> str:
    """Scrub secrets, API keys, email addresses, phone numbers, and candidate PII from text."""
    scrubbed = text

    # 1. Google / Gemini API keys: AIzaSy...
    scrubbed = re.sub(r"AIzaSy[A-Za-z0-9_-]{33}", "[REDACTED_API_KEY]", scrubbed)

    # 2. Bearer tokens or generic 16+ char secrets following headers/flags
    scrubbed = re.sub(
        r"(?i)\b(key|secret|token|password|auth|bearer)[:=\s]+['\"]?[A-Za-z0-9_\-\.]{16,}['\"]?",
        r"\1: [REDACTED_SECRET]",
        scrubbed,
    )

    # 3. Emails
    scrubbed = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "[REDACTED_EMAIL]", scrubbed)

    # 4. Phone numbers (standard formats)
    scrubbed = re.sub(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", "[REDACTED_PHONE]", scrubbed)

    # 5. Custom PII terms (e.g. candidate name, university) if supplied
    if custom_pii:
        for term in sorted(custom_pii, key=len, reverse=True):
            if term and len(term.strip()) >= 3:
                pattern = re.compile(re.escape(term), re.IGNORECASE)
                scrubbed = pattern.sub("[REDACTED_PII]", scrubbed)

    return scrubbed


def generate_doctor_report(config: Config, out_path: Path | None = None, online: bool = False) -> Path:
    """Generate a shareable, scrubbed diagnostic report doctor_report.txt."""
    doc = SystemDoctor(config, online=online)
    checks = doc.run_all_checks()

    # Discover any personal PII terms from local git-ignored profile if present to ensure 100% scrubber safety
    custom_pii: set[str] = set()
    profile_path = config.data_directory / "profile" / "profile.yaml"
    if profile_path.is_file():
        try:
            import yaml
            pdata = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
            if isinstance(pdata, dict):
                person = pdata.get("person", {})
                for k in ("display_name", "institution", "field"):
                    v = person.get(k)
                    if v and isinstance(v, str) and len(v) > 3 and v != "the candidate":
                        custom_pii.add(v)
        except Exception:
            pass

    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("JOBHUNT SYSTEM DOCTOR REPORT")
    lines.append(f"Generated: {utc_now().isoformat()} UTC")
    lines.append(f"Operating System: {platform.system()} {platform.release()} ({get_os_arch()})")
    lines.append(f"Python: {sys.version.split()[0]} ({get_python_executable(config.project_root)})")
    lines.append("=" * 70)
    lines.append("")
    lines.append("--- SYSTEM CHECKS ---")
    for c in checks:
        sym = "[OK]  " if c.status == "pass" else ("[WARN]" if c.status == "warn" else "[FAIL]")
        lines.append(f"{sym} {c.name:<30} {scrub_text(c.message, custom_pii)}")
        if c.hint:
            lines.append(f"       -> Hint: {scrub_text(c.hint, custom_pii)}")
    lines.append("")

    # Resume template detection
    lines.append("--- RESUME TEMPLATES ---")
    resumes_dir = config.data_directory / "resumes"
    if not resumes_dir.is_dir():
        resumes_dir = config.project_root / "data" / "resumes"
    found_resumes = list(resumes_dir.glob("*.tex")) if resumes_dir.is_dir() else []
    if found_resumes:
        from jobhunt.resumes.templates import analyze_template
        for r_file in found_resumes:
            try:
                raw_tex = r_file.read_text(encoding="utf-8", errors="replace")
                cap = analyze_template(raw_tex)
                lines.append(f"Resume: {r_file.name} -> Template: {cap.template_id} ({cap.template_name}), Tailorable: {cap.can_tailor}")
            except Exception as exc:
                lines.append(f"Resume: {r_file.name} -> Error analyzing template: {exc}")
    else:
        lines.append("No .tex resume files found in data/resumes/")
    lines.append("")

    # Last 50 log lines
    lines.append("--- RECENT DIAGNOSTIC LOGS (SCRUBBED, LAST 50 LINES) ---")
    log_file = config.data_directory / "logs" / "jobhunt.log"
    if not log_file.is_file():
        log_file = config.project_root / "data" / "logs" / "jobhunt.log"
    if log_file.is_file():
        try:
            log_lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            recent_logs = log_lines[-50:]
            for log_line in recent_logs:
                lines.append(scrub_text(log_line, custom_pii))
        except Exception as exc:
            lines.append(f"Could not read log file: {exc}")
    else:
        lines.append("No log file found at data/logs/jobhunt.log")

    lines.append("")
    lines.append("=" * 70)
    lines.append("END OF REPORT (Safe to share with issue reports)")
    lines.append("=" * 70)

    target_path = out_path or (config.project_root / "doctor_report.txt")
    target_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target_path
