"""SQLite connection helper and a tiny Flyway-style migration runner.

Spring Boot analogy: `apply_migrations` ~ Flyway.migrate(). Files named `NNN_name.sql` are
applied in version order, each inside one transaction, and recorded in `schema_migrations`
with a checksum. Editing an already-applied file is an error (like Flyway's validate).
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from jobhunt.errors import MigrationError
from jobhunt.timeutil import Clock, to_iso, utc_now

logger = logging.getLogger(__name__)

_MIGRATION_FILE = re.compile(r"^(?P<version>\d{3,})_(?P<name>[A-Za-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def connect(database_path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open the database with WAL, foreign keys and explicit transaction control.

    `isolation_level=None` puts the sqlite3 module in autocommit mode so that `transaction()`
    below is the single place where BEGIN/COMMIT happens (no implicit transactions).
    """
    target = str(database_path)
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        target, isolation_level=None, check_same_thread=check_same_thread, timeout=30
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    if target != ":memory:":
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside BEGIN IMMEDIATE ... COMMIT, rolling back on any exception.

    Nested use joins the outer transaction instead of failing.
    """
    if connection.in_transaction:
        yield connection
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def discover_migrations(migrations_dir: str | Path) -> list[Migration]:
    """List migration files in version order; rejects duplicate versions."""
    directory = Path(migrations_dir)
    if not directory.is_dir():
        raise MigrationError(f"migrations directory not found: {directory}")
    found: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_FILE.match(path.name)
        if not match:
            raise MigrationError(f"bad migration file name (want NNN_name.sql): {path.name}")
        version = int(match["version"])
        if version in found:
            raise MigrationError(f"duplicate migration version {version}: {path.name}")
        found[version] = Migration(version=version, name=match["name"], path=path)
    return [found[version] for version in sorted(found)]


def apply_migrations(
    connection: sqlite3.Connection,
    migrations_dir: str | Path,
    *,
    clock: Clock = utc_now,
) -> list[int]:
    """Apply every pending migration. Returns the versions applied by this call."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version    INTEGER PRIMARY KEY,
               name       TEXT NOT NULL,
               checksum   TEXT NOT NULL,
               applied_at TEXT NOT NULL
           )"""
    )
    applied = {
        row["version"]: row["checksum"]
        for row in connection.execute("SELECT version, checksum FROM schema_migrations")
    }
    migrations = discover_migrations(migrations_dir)

    for migration in migrations:  # validate before touching anything
        recorded = applied.get(migration.version)
        if recorded is not None and recorded != migration.checksum:
            raise MigrationError(
                f"migration {migration.path.name} was modified after being applied "
                f"(checksum mismatch); add a new migration instead of editing it"
            )

    newly_applied: list[int] = []
    for migration in migrations:
        if migration.version in applied:
            continue
        _run_one(connection, migration, to_iso(clock()))
        newly_applied.append(migration.version)
        logger.info("applied migration %03d_%s", migration.version, migration.name)
    return newly_applied


def current_version(connection: sqlite3.Connection) -> int:
    """Highest applied migration version (0 when none)."""
    try:
        row = connection.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["version"] or 0)


def _run_one(connection: sqlite3.Connection, migration: Migration, applied_at: str) -> None:
    # version/name are validated by the filename regex and checksum is hex, so inlining them
    # into the script is safe. executescript lets one transaction span many statements.
    bookkeeping = (
        "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES "
        f"({migration.version}, '{migration.name}', '{migration.checksum}', '{applied_at}');"
    )
    script = f"BEGIN;\n{migration.sql}\n{bookkeeping}\nCOMMIT;"
    try:
        connection.executescript(script)
    except sqlite3.Error as error:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise MigrationError(f"migration {migration.path.name} failed: {error}") from error


def pending_migrations(connection: sqlite3.Connection, migrations_dir: str | Path) -> list[int]:
    """Versions in the migrations folder that the database has not applied yet."""
    try:
        applied = {row["version"] for row in connection.execute("SELECT version FROM schema_migrations")}
    except sqlite3.OperationalError:  # brand-new database
        applied = set()
    return [m.version for m in discover_migrations(migrations_dir) if m.version not in applied]


def backup_database(database_path: str | Path, backup_dir: str | Path, *, label: str = "backup",
                    clock: Clock = utc_now) -> Path:
    """Copy a live database with SQLite's backup API (safe while WAL is active) and verify the copy.

    The file is named `<stem>_<label>_<UTC timestamp>.sqlite3`. Raises RuntimeError when the copy
    fails the integrity check (and removes the bad copy).
    """
    source_path = Path(database_path)
    destination_dir = Path(backup_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = clock().strftime("%Y%m%dT%H%M%SZ")
    destination = destination_dir / f"{source_path.stem}_{label}_{stamp}.sqlite3"
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        verdict = target.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        target.close()
        source.close()
    if verdict != "ok":
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"backup integrity check failed: {verdict}")
    return destination


def migrate_with_backup(connection: sqlite3.Connection, database_path: str | Path, migrations_dir: str | Path,
                        backup_dir: str | Path) -> list[int]:
    """apply_migrations, but take a verified backup first whenever something is pending on a non-empty DB."""
    pending = pending_migrations(connection, migrations_dir)
    if pending and Path(database_path).is_file() and Path(database_path).stat().st_size > 0             and current_version(connection) > 0:
        backup_database(database_path, backup_dir, label=f"before_migration_{pending[0]:03d}")
    return apply_migrations(connection, migrations_dir)
