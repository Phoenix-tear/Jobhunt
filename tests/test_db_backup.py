"""Backup helper, migrate-with-backup and the config.local.yaml override."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from jobhunt.config import load_config
from jobhunt.db import backup_database, connect, migrate_with_backup, pending_migrations


def _migrations(tmp_path: Path, count: int) -> Path:
    folder = tmp_path / "migrations"
    folder.mkdir(exist_ok=True)
    for number in range(1, count + 1):
        (folder / f"{number:03d}_t{number}.sql").write_text(f"CREATE TABLE t{number} (id INTEGER);")
    return folder


def test_backup_is_verified_copy(tmp_path):
    database = tmp_path / "a.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE x (v INTEGER)")
    connection.execute("INSERT INTO x VALUES (7)")
    connection.commit()
    connection.close()
    copy = backup_database(database, tmp_path / "backups", label="t")
    assert copy.parent.name == "backups" and "_t_" in copy.name
    assert sqlite3.connect(copy).execute("SELECT v FROM x").fetchone()[0] == 7


def test_migration_run_backs_up_only_when_something_is_pending(tmp_path):
    folder = _migrations(tmp_path, 1)
    database = tmp_path / "jobs.sqlite3"
    connection = connect(database)
    assert migrate_with_backup(connection, database, folder, tmp_path / "backups") == [1]
    assert not (tmp_path / "backups").exists()  # brand-new database: nothing worth saving
    _migrations(tmp_path, 2)
    assert pending_migrations(connection, folder) == [2]
    assert migrate_with_backup(connection, database, folder, tmp_path / "backups") == [2]
    assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1
    assert migrate_with_backup(connection, database, folder, tmp_path / "backups") == []
    assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1


def test_local_config_overrides_nested_values(tmp_path):
    (tmp_path / "config.yaml").write_text('user_agent_contact: ""\nemail:\n  account: ""\n  lookback_days: 9\n')
    (tmp_path / "config.local.yaml").write_text('user_agent_contact: "me@example.com"\nemail:\n  account: me@example.com\n')
    config = load_config(tmp_path / "config.yaml")
    assert config.user_agent_contact == "me@example.com"
    assert config.email.account == "me@example.com" and config.email.lookback_days == 9
