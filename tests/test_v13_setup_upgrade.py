"""Tests for one-command setup, upgrade workflow, and Tectonic installer (Brief v13)."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import sqlite3
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jobhunt.cli import _command_upgrade
from jobhunt.config import Config
from jobhunt.db import connect
from jobhunt.platform.tectonic_install import install_tectonic, verify_tectonic_runs


class TestUpgradeCommand:
    @pytest.fixture
    def upgrade_env(self, tmp_path: Path) -> tuple[Config, Path]:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = data_dir / "jobhunt.sqlite3"
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()

        # Create a sample migration
        mig1 = migrations_dir / "001_init.sql"
        mig1.write_text("CREATE TABLE test_items (id INTEGER PRIMARY KEY, name TEXT);", encoding="utf-8")

        # Initialize DB with version 1
        mig1_hash = hashlib.sha256(mig1.read_bytes()).hexdigest()
        conn = connect(db_path)
        conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, checksum TEXT, applied_at TEXT);")
        conn.execute("INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (1, 'init', ?, '2026-01-01T00:00:00Z');", (mig1_hash,))
        conn.execute("CREATE TABLE test_items (id INTEGER PRIMARY KEY, name TEXT);")
        conn.execute("INSERT INTO test_items (id, name) VALUES (1, 'demo');")
        conn.commit()
        conn.close()

        # Add a new pending migration 002
        mig2 = migrations_dir / "002_add_flag.sql"
        mig2.write_text("ALTER TABLE test_items ADD COLUMN active INTEGER DEFAULT 1;", encoding="utf-8")

        config = Config(
            project_root=tmp_path,
            data_dir=data_dir,
            database_path=str(db_path),
            migrations_dir=str(migrations_dir),
        )
        return config, tmp_path

    def test_upgrade_backs_up_db_before_migration(self, upgrade_env: tuple[Config, Path]) -> None:
        config, tmp_path = upgrade_env
        args = argparse.Namespace()

        with patch("subprocess.run") as mock_subproc:
            ret = _command_upgrade(args, config)
            assert ret == 0

        # Check that backup was created
        backups = list((config.data_dir / "backups").glob("*.sqlite3"))
        assert len(backups) >= 1
        assert "upgrade" in backups[0].name or "before_migration" in backups[0].name

        # Verify migration 002 was applied
        conn = connect(config.database_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(test_items)").fetchall()]
        conn.close()
        assert "active" in cols

    def test_upgrade_detects_lock_change(self, upgrade_env: tuple[Config, Path]) -> None:
        config, tmp_path = upgrade_env
        lock_file = tmp_path / "requirements.lock"
        lock_file.write_text("fake-dep==1.0.0\n", encoding="utf-8")

        mock_proc = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_proc) as mock_subproc:
            ret = _command_upgrade(argparse.Namespace(), config)
            assert ret == 0
            # Pip install should have been called
            called_cmds = [call.args[0] for call in mock_subproc.call_args_list if call.args]
            pip_calls = [c for c in called_cmds if isinstance(c, list) and "pip" in c]
            assert len(pip_calls) == 1

        # Re-running without changing requirements.lock should NOT invoke pip
        with patch("subprocess.run", return_value=mock_proc) as mock_subproc:
            ret = _command_upgrade(argparse.Namespace(), config)
            assert ret == 0
            called_cmds = [call.args[0] for call in mock_subproc.call_args_list if call.args]
            pip_calls = [c for c in called_cmds if isinstance(c, list) and "pip" in c]
            assert len(pip_calls) == 0


class TestTectonicInstall:
    def test_verify_tectonic_runs_false_for_missing(self, tmp_path: Path) -> None:
        assert verify_tectonic_runs(tmp_path / "nonexistent.exe") is False

    def test_install_tectonic_already_working_idempotent(self, tmp_path: Path) -> None:
        target_dir = tmp_path / "tools" / "tectonic" / "windows-x86_64"
        target_dir.mkdir(parents=True)
        exe_file = target_dir / ("tectonic.exe" if os.name == "nt" else "tectonic")
        exe_file.touch()

        with patch("jobhunt.platform.tectonic_install.verify_tectonic_runs", return_value=True):
            installed = install_tectonic(target_dir=target_dir, os_arch="windows-x86_64")
            assert installed == exe_file

    def test_install_tectonic_from_zip(self, tmp_path: Path) -> None:
        target_dir = tmp_path / "tectonic_bin"
        # Create an in-memory zip containing fake tectonic executable
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("tectonic.exe", "fake-binary-content")
        zip_bytes = buf.getvalue()

        mock_resp = MagicMock()
        mock_resp.read.return_value = zip_bytes
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp), \
             patch("jobhunt.platform.tectonic_install.verify_tectonic_runs", return_value=True):
            installed = install_tectonic(target_dir=target_dir, os_arch="windows-x86_64")
            assert installed.is_file()
            assert installed.name == ("tectonic.exe" if os.name == "nt" else "tectonic")
            assert installed.read_text(encoding="utf-8") == "fake-binary-content"
