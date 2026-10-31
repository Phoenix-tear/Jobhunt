"""Settings storage and precedence layer (Brief v12).

Precedence hierarchy:
1. `config.yaml`       - Committed application defaults (lowest precedence).
2. `config.local.yaml` - Hand-written local developer overrides (git-ignored).
3. `data/settings.yaml`- Settings modified and persisted via the web UI (highest precedence).

The web UI writes ONLY to `data/settings.yaml` and never rewrites `config.yaml`
or `config.local.yaml`. Secrets are strictly forbidden in `data/settings.yaml`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from jobhunt.errors import JobHuntError

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_FILE = "config.yaml"
LOCAL_CONFIG_FILE = "config.local.yaml"
SETTINGS_FILE = "data/settings.yaml"

FORBIDDEN_SETTINGS_KEYS = {
    "gemini_api_key",
    "api_key",
    "key",
    "secret",
    "secrets",
    "oauth_token",
    "gmail_token",
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def get_settings_file_path(project_root: Path | None = None, data_dir: Path | None = None) -> Path:
    if data_dir is not None:
        return Path(data_dir) / "settings.yaml"
    import os
    env_data_dir = os.environ.get("JOBHUNT_DATA_DIR")
    if env_data_dir:
        return Path(env_data_dir).resolve() / "settings.yaml"
    root = project_root or Path.cwd()
    return root / SETTINGS_FILE


def read_settings_file(project_root: Path | None = None, data_dir: Path | None = None) -> dict[str, Any]:
    """Read the current content of data/settings.yaml."""
    settings_path = get_settings_file_path(project_root, data_dir=data_dir)
    if not settings_path.is_file():
        return {}
    try:
        data = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError as error:
        logger.warning("Could not parse %s: %s", settings_path, error)
        return {}


def strip_forbidden_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively strip any keys that might contain sensitive secrets."""
    cleaned: dict[str, Any] = {}
    for k, v in data.items():
        if str(k).lower() in FORBIDDEN_SETTINGS_KEYS:
            continue
        if isinstance(v, dict):
            cleaned[k] = strip_forbidden_keys(v)
        else:
            cleaned[k] = v
    return cleaned


def write_settings_file(updates: dict[str, Any], project_root: Path | None = None, data_dir: Path | None = None) -> dict[str, Any]:
    """Write updates to data/settings.yaml by merging with existing settings.
    
    Never touches config.yaml or config.local.yaml. Strips secrets before persisting.
    """
    settings_path = get_settings_file_path(project_root, data_dir=data_dir)
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    cleaned_updates = strip_forbidden_keys(updates)
    existing = read_settings_file(project_root, data_dir=data_dir)
    merged = _deep_merge(existing, cleaned_updates)

    # Atomic write to avoid partial writes
    tmp_path = settings_path.with_suffix(".tmp")
    tmp_path.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")
    tmp_path.replace(settings_path)
    return merged


def load_merged_settings(project_root: Path | None = None, data_dir: Path | None = None) -> dict[str, Any]:
    """Load configuration dictionaries with precedence:
    config.yaml < config.local.yaml < data/settings.yaml
    """
    root = project_root or Path.cwd()
    base_file = root / DEFAULT_CONFIG_FILE
    local_file = root / LOCAL_CONFIG_FILE
    settings_file = get_settings_file_path(root, data_dir=data_dir)

    merged: dict[str, Any] = {}
    if base_file.is_file():
        try:
            base_data = yaml.safe_load(base_file.read_text(encoding="utf-8")) or {}
            if isinstance(base_data, dict):
                merged = base_data
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load %s: %s", base_file, exc)

    if local_file.is_file():
        try:
            local_data = yaml.safe_load(local_file.read_text(encoding="utf-8")) or {}
            if isinstance(local_data, dict):
                merged = _deep_merge(merged, local_data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load %s: %s", local_file, exc)

    if settings_file.is_file():
        try:
            settings_data = yaml.safe_load(settings_file.read_text(encoding="utf-8")) or {}
            if isinstance(settings_data, dict):
                merged = _deep_merge(merged, settings_data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load %s: %s", settings_file, exc)

    return merged
