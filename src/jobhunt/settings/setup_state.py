"""Setup state management and automatic detection of existing installs (Brief v12).

An existing install (jobs in DB and a profile created by v11) is marked completed
automatically on first start, after a DB backup; the wizard is reachable later
via Settings -> "Run setup again" (non-destructive).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from jobhunt.config import Config
from jobhunt.db import backup_database
from jobhunt.timeutil import to_iso, utc_now

logger = logging.getLogger(__name__)


def get_setup_state(connection: sqlite3.Connection) -> dict[str, Any]:
    """Retrieve the current setup state."""
    try:
        row = connection.execute(
            "SELECT completed, step, completed_at, schema_version FROM setup_state WHERE id = 1"
        ).fetchone()
        if row:
            return {
                "completed": bool(row["completed"]),
                "step": str(row["step"]),
                "completed_at": row["completed_at"],
                "schema_version": int(row["schema_version"]),
            }
    except sqlite3.OperationalError:
        # Table might not exist yet if migrations haven't run
        pass
    return {
        "completed": False,
        "step": "welcome",
        "completed_at": None,
        "schema_version": 1,
    }


def set_setup_state(
    connection: sqlite3.Connection,
    completed: bool,
    step: str,
    *,
    completed_at: str | None = None,
    schema_version: int = 1,
) -> dict[str, Any]:
    """Insert or update setup_state row."""
    if completed and not completed_at:
        completed_at = to_iso(utc_now())

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS setup_state (
            id INTEGER PRIMARY KEY,
            completed INTEGER NOT NULL DEFAULT 0,
            step TEXT NOT NULL DEFAULT 'welcome',
            completed_at TEXT,
            schema_version INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    connection.execute(
        """
        INSERT INTO setup_state (id, completed, step, completed_at, schema_version)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            completed = excluded.completed,
            step = excluded.step,
            completed_at = excluded.completed_at,
            schema_version = excluded.schema_version
        """,
        (1 if completed else 0, step, completed_at, schema_version),
    )
    return {
        "completed": completed,
        "step": step,
        "completed_at": completed_at,
        "schema_version": schema_version,
    }


def check_and_autodone_existing_install(
    connection: sqlite3.Connection,
    config: Config,
) -> bool:
    """Check if this is an existing install.
    
    If jobs > 0 and profile exists, take a DB backup and mark setup complete automatically.
    """
    state = get_setup_state(connection)
    if state["completed"]:
        return False

    try:
        job_count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    except sqlite3.OperationalError:
        return False

    profile_dir = config.resolve_path(config.profiles.profile_dir)
    profile_file = profile_dir / "profile.yaml"
    facts_file = profile_dir / "facts_tex.md"
    root_profile_file = config.resolve_path("data/profile.yaml")
    has_profile = profile_file.is_file() or facts_file.is_file() or root_profile_file.is_file()

    if job_count > 0 and has_profile:
        # Take a database backup before marking auto-complete
        try:
            if config.database_file.is_file() and config.database_file.stat().st_size > 0:
                backup_path = backup_database(
                    config.database_file,
                    config.backup_directory,
                    label="before_autodone_setup",
                )
                logger.info("Auto-completed setup for existing install after backup to %s", backup_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not create DB backup prior to auto-completing setup: %s", exc)

        set_setup_state(connection, completed=True, step="done", completed_at=to_iso(utc_now()))
        return True

    return False
