"""Minimal `.env` reader (no python-dotenv dependency).

Secrets live in `.env` / `.secrets/` (both gitignored). Values are only ever passed to adapters
that declare them in `required_secrets`; they are never logged or stored in the database.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def parse_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; `#` comments, blank lines, `export ` prefix and quotes are handled."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def load_secrets(project_root: Path, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """`.env` values, overridden by .secrets/ and real environment variables; empty values are dropped."""
    merged = parse_env_file(project_root / ".env")
    from jobhunt.settings.secrets import resolve_gemini_key

    resolved = resolve_gemini_key(project_root)
    if resolved.key:
        merged["GEMINI_API_KEY"] = resolved.key

    merged.update(os.environ if environ is None else environ)
    return {key: value for key, value in merged.items() if value}
