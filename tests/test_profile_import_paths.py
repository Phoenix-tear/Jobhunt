"""profile_import_legacy must write under the configured data directory, never the current directory's data/."""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest

from jobhunt.config import Config
from jobhunt.profile.loader import profile_import_legacy


def test_profile_import_writes_under_the_configured_data_dir(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    data_dir = tmp_path / "custom-data"
    custom = dataclasses.replace(config, data_dir=data_dir)
    database = custom.database_file
    database.parent.mkdir(parents=True)
    sqlite3.connect(database).execute("CREATE TABLE t (x)").connection.close()

    written = profile_import_legacy(custom)

    assert written == data_dir / "profile" / "profile.yaml"
    assert written.is_file() and (data_dir / "profile" / "profile.example.yaml").is_file()
    assert list((data_dir / "backups").glob("*pre-profile-import*"))
    assert not (elsewhere / "data").exists()
