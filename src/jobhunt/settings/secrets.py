"""Secure storage and masking for secrets (Brief v12).

Secrets (Gemini key, Gmail tokens/OAuth JSON) live under `.secrets/` with owner-only
permissions (Windows ACL via icacls, chmod 600 on POSIX).
Secrets are never written to data/settings.yaml, the database, logs, or browser storage.
The API only ever returns `{present: true, last4: "…1234"}`.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

GEMINI_KEY_FILENAME = "gemini_api_key"
_API_KEY_REGEX = re.compile(r"AIza[0-9A-Za-z\-_]{35}")


def get_secrets_dir(project_root: Path | None = None) -> Path:
    root = project_root or Path.cwd()
    return root / ".secrets"


def secure_permissions(path: Path) -> None:
    """Enforce owner-only permissions on a directory or file where supported."""
    try:
        if os.name == "nt":
            username = os.environ.get("USERNAME")
            if username:
                perm = f"{username}:(OI)(CI)F" if path.is_dir() else f"{username}:F"
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", perm],
                    capture_output=True,
                    check=False,
                )
        else:
            if path.is_dir():
                os.chmod(path, 0o700)
            elif path.is_file():
                os.chmod(path, 0o600)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not set strict file permissions on %s: %s", path, exc)


from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedSecret:
    key: str
    source: str
    source_path: Path | None = None
    is_safe: bool = True
    warning: str | None = None


def resolve_gemini_key(project_root: Path | None = None) -> ResolvedSecret:
    """Resolve Gemini API key across all supported locations in precedence order.

    Resolution order:
    1. Runtime environment variable: GEMINI_API_KEY
    2. Dedicated owner-only file: .secrets/gemini_api_key
    3. Project environment file: .env (GEMINI_API_KEY=...)
    4. Local configuration: config.local.yaml (gemini.api_key)
    5. Tracked configuration: config.yaml (gemini.api_key) -> flags unsafe warning!

    Returns a ResolvedSecret containing the key, the source description (path only,
    never the value), and an is_safe flag.
    """
    root = project_root or Path.cwd()

    # 1. Environment variable
    env_val = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_val:
        return ResolvedSecret(key=env_val, source="environment variable GEMINI_API_KEY", source_path=None, is_safe=True)

    # 2. Dedicated secret file under .secrets/
    secrets_dir = get_secrets_dir(root)
    key_file = secrets_dir / GEMINI_KEY_FILENAME
    if key_file.is_file():
        try:
            content = key_file.read_text(encoding="utf-8").strip()
            if content:
                return ResolvedSecret(key=content, source=".secrets/gemini_api_key", source_path=key_file, is_safe=True)
        except OSError:
            pass

    # 3. .env file
    env_path = root / ".env"
    if env_path.is_file():
        try:
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                k, sep, v = line.partition("=")
                if k.strip() == "GEMINI_API_KEY":
                    val = v.strip()
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                        val = val[1:-1]
                    else:
                        val = val.split(" #", 1)[0].rstrip()
                    if val:
                        return ResolvedSecret(key=val, source=".env", source_path=env_path, is_safe=True)
        except OSError:
            pass

    # 4. config.local.yaml
    local_cfg = root / "config.local.yaml"
    if local_cfg.is_file():
        try:
            import yaml
            parsed = yaml.safe_load(local_cfg.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                gem_sec = parsed.get("gemini", {})
                if isinstance(gem_sec, dict) and gem_sec.get("api_key"):
                    return ResolvedSecret(
                        key=str(gem_sec["api_key"]).strip(),
                        source="config.local.yaml",
                        source_path=local_cfg,
                        is_safe=True,
                    )
        except Exception:
            pass

    # 5. config.yaml (tracked file - unsafe!)
    tracked_cfg = root / "config.yaml"
    if tracked_cfg.is_file():
        try:
            import yaml
            parsed = yaml.safe_load(tracked_cfg.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                gem_sec = parsed.get("gemini", {})
                if isinstance(gem_sec, dict) and gem_sec.get("api_key"):
                    val = str(gem_sec["api_key"]).strip()
                    if val and not val.startswith("${"):
                        return ResolvedSecret(
                            key=val,
                            source="config.yaml",
                            source_path=tracked_cfg,
                            is_safe=False,
                            warning="Gemini API key found in tracked file config.yaml! Move it to .secrets/ or .env.",
                        )
        except Exception:
            pass

    return ResolvedSecret(key="", source="none", source_path=None, is_safe=True)


def get_gemini_key(project_root: Path | None = None) -> str:
    """Read Gemini API key from the single canonical resolution hierarchy."""
    return resolve_gemini_key(project_root).key


def set_gemini_key(key: str, project_root: Path | None = None) -> None:
    """Write Gemini API key to .secrets/gemini_api_key with owner-only permissions."""
    secrets_dir = get_secrets_dir(project_root)
    secrets_dir.mkdir(parents=True, exist_ok=True)
    secure_permissions(secrets_dir)

    key_file = secrets_dir / GEMINI_KEY_FILENAME
    stripped = key.strip()
    if not stripped:
        if key_file.is_file():
            key_file.unlink()
        return

    key_file.write_text(stripped, encoding="utf-8")
    secure_permissions(key_file)
    register_secret(stripped)


def get_secret_info(key: str | None) -> dict[str, Any]:
    """Return safe public representation of a secret: {present: bool, last4: str}."""
    if not key or not key.strip():
        return {"present": False, "last4": ""}
    k = key.strip()
    last4 = f"…{k[-4:]}" if len(k) >= 4 else "…"
    return {"present": True, "last4": last4}


_KNOWN_SECRETS: set[str] = set()


def register_secret(secret: str) -> None:
    """Register a secret for log masking."""
    s = secret.strip()
    if s and len(s) >= 4:
        _KNOWN_SECRETS.add(s)


def mask_secret_text(text: str) -> str:
    """Replace known secrets and Google API key patterns with masked representations."""
    if not text:
        return text
    # Mask registered secrets
    for secret in sorted(_KNOWN_SECRETS, key=len, reverse=True):
        if secret in text:
            last4 = f"…{secret[-4:]}" if len(secret) >= 4 else "…"
            text = text.replace(secret, last4)
    # Mask any Google/Gemini API key patterns
    text = _API_KEY_REGEX.sub("[REDACTED_API_KEY]", text)
    return text


class SecretMaskingFilter(logging.Filter):
    """Logging filter that ensures API keys never leak into log output."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = mask_secret_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: mask_secret_text(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    mask_secret_text(v) if isinstance(v, str) else v
                    for v in record.args
                )
        return True


_installed_filter: SecretMaskingFilter | None = None


def install_secret_masking(
    secrets: list[str] | Path | None = None,
    project_root: Path | None = None,
) -> SecretMaskingFilter:
    """Install SecretMaskingFilter on root logger and all existing handlers."""
    global _installed_filter
    root_path = project_root
    if isinstance(secrets, Path):
        root_path = secrets
        secrets = None
    elif secrets:
        for s in secrets:
            register_secret(s)
    # Also register current Gemini key if present
    current_key = get_gemini_key(root_path)
    if current_key:
        register_secret(current_key)

    if _installed_filter is None:
        _installed_filter = SecretMaskingFilter()
        root = logging.getLogger()
        root.addFilter(_installed_filter)
        for handler in root.handlers:
            handler.addFilter(_installed_filter)
    return _installed_filter
