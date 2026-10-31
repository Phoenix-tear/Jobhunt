"""Profile loader and legacy importer (Brief v11 Phase 2).

Provides:
- load_profile(path): loads and validates Profile from YAML, caching the immutable instance
- get_profile(): single accessor for the loaded Profile
- profile_import_legacy(): generates data/profile/profile.yaml after backing up the database
- ensure_profile(): auto-creates profile.yaml on first run if database exists
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
import yaml

from jobhunt.config import Config
from jobhunt.profile.legacy import build_legacy_profile
from jobhunt.profile.model import (
    BaseResume,
    LevelPolicy,
    LocationPolicy,
    Person,
    Profile,
    ProfilePaths,
    RoleFamily,
    ScoringPolicy,
)

logger = logging.getLogger(__name__)

PROFILE_PATH = Path("data/profile/profile.yaml")
EXAMPLE_PATH = Path("data/profile/profile.example.yaml")

_ACTIVE_PROFILE: Profile | None = None


def save_profile(profile: Profile, path: Path) -> None:
    """Save Profile to YAML file in canonical structure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = profile.model_dump(mode="json")
    yaml_text = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    path.write_text(yaml_text, encoding="utf-8")


def load_profile(path: Path | str | None = None) -> Profile:
    """Load, validate, and return immutable Profile from YAML."""
    global _ACTIVE_PROFILE
    file_path = Path(path) if path is not None else PROFILE_PATH
    is_default = (path is None or Path(path) == PROFILE_PATH)
    if not file_path.is_file():
        # Fall back to legacy profile if file not yet on disk
        legacy = build_legacy_profile()
        if is_default:
            _ACTIVE_PROFILE = legacy
        return legacy

    raw = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}

    # Parse submodels
    person = Person(**raw.get("person", {}))
    level_policy = LevelPolicy(**raw.get("level_policy", {}))
    role_families = tuple(RoleFamily(**rf) for rf in raw.get("role_families", []))
    bases = tuple(BaseResume(**b) for b in raw.get("bases", []))
    location_policy = LocationPolicy(**raw.get("location_policy", {}))
    scoring = ScoringPolicy(**raw.get("scoring", {}))
    paths = ProfilePaths(**raw.get("paths", {}))
    candidate_blurb = raw.get("candidate_blurb", "")

    profile = Profile(
        person=person,
        level_policy=level_policy,
        role_families=role_families,
        bases=bases,
        location_policy=location_policy,
        scoring=scoring,
        paths=paths,
        candidate_blurb=candidate_blurb,
    )
    if is_default:
        _ACTIVE_PROFILE = profile
    return profile


def get_profile(config: Any | None = None) -> Profile:
    """Single global accessor for the current Profile."""
    global _ACTIVE_PROFILE
    if _ACTIVE_PROFILE is None:
        _ACTIVE_PROFILE = load_profile()
    return _ACTIVE_PROFILE


def set_active_profile(profile: Profile) -> None:
    """Override the active profile (used in tests/fixtures)."""
    global _ACTIVE_PROFILE
    _ACTIVE_PROFILE = profile


def profile_import_legacy(config: Config) -> Path:
    """Generate data/profile/profile.yaml from current hardcoded parameters.

    Backs up the database first if jobs exist.
    """
    from jobhunt.db import backup_database

    db_path = Path(config.database_file)
    profile_path = config.resolve_path(str(PROFILE_PATH))  # honours a custom data dir; same file by default
    example_path = config.resolve_path(str(EXAMPLE_PATH))
    if db_path.is_file():
        backup_path = backup_database(db_path, config.backup_directory, label="pre-profile-import")
        logger.info("Backed up database to %s before importing legacy profile", backup_path)

    profile = build_legacy_profile()
    save_profile(profile, profile_path)

    # Also save example profile
    generic_person = Person(
        display_name="the candidate",
        degree="B.S.",
        field="Computer Science",
        institution="University of Technology",
        graduation_year=2026,
        career_stage="student",
    )
    generic_profile = Profile(
        person=generic_person,
        level_policy=profile.level_policy,
        role_families=profile.role_families,
        bases=profile.bases,
        location_policy=profile.location_policy,
        scoring=profile.scoring,
        paths=profile.paths,
        candidate_blurb="",
    )
    save_profile(generic_profile, example_path)

    logger.info("Saved legacy profile to %s and example to %s", profile_path, example_path)
    return profile_path


def ensure_profile(config: Config) -> Profile:
    """Auto-creates profile.yaml on first start after backing up DB if DB exists and profile is absent."""
    if not PROFILE_PATH.is_file():
        db_path = Path(config.database_file)
        if db_path.is_file():
            profile_import_legacy(config)
    return load_profile()


__all__ = [
    "EXAMPLE_PATH",
    "PROFILE_PATH",
    "ensure_profile",
    "get_profile",
    "load_profile",
    "profile_import_legacy",
    "save_profile",
    "set_active_profile",
]
