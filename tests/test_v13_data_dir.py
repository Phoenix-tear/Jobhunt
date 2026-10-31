"""Tests for --data-dir CLI argument and JOBHUNT_DATA_DIR environment variable (Brief v13)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from jobhunt.cli import build_parser, main
from jobhunt.config import load_config
from jobhunt.settings.storage import get_settings_file_path, read_settings_file, write_settings_file


class TestDataDirOverride:
    def test_load_config_with_explicit_data_dir(self, tmp_path: Path) -> None:
        custom_data = tmp_path / "custom_data"
        custom_data.mkdir()

        cfg = load_config(data_dir=custom_data)
        assert cfg.data_dir == custom_data.resolve()
        assert cfg.data_directory == custom_data.resolve()
        assert cfg.database_file == (custom_data / "jobhunt.sqlite3").resolve()
        assert cfg.backup_directory == (custom_data / "backups").resolve()
        assert cfg.resolve_path("data/profile/facts_tex.md") == (custom_data / "profile" / "facts_tex.md").resolve()
        assert cfg.resolve_path("data/resumes/resume_ai.tex") == (custom_data / "resumes" / "resume_ai.tex").resolve()

    def test_load_config_with_env_var(self, tmp_path: Path) -> None:
        env_data = tmp_path / "env_data"
        env_data.mkdir()

        with patch.dict(os.environ, {"JOBHUNT_DATA_DIR": str(env_data)}):
            cfg = load_config()
            assert cfg.data_dir == env_data.resolve()
            assert cfg.data_directory == env_data.resolve()
            assert cfg.database_file == (env_data / "jobhunt.sqlite3").resolve()

    def test_explicit_arg_beats_env_var(self, tmp_path: Path) -> None:
        env_data = tmp_path / "env_data"
        explicit_data = tmp_path / "explicit_data"
        env_data.mkdir()
        explicit_data.mkdir()

        with patch.dict(os.environ, {"JOBHUNT_DATA_DIR": str(env_data)}):
            cfg = load_config(data_dir=explicit_data)
            assert cfg.data_dir == explicit_data.resolve()
            assert cfg.database_file == (explicit_data / "jobhunt.sqlite3").resolve()

    def test_settings_storage_respects_data_dir(self, tmp_path: Path) -> None:
        custom_data = tmp_path / "custom_data"
        custom_data.mkdir()

        settings_path = get_settings_file_path(data_dir=custom_data)
        assert settings_path == (custom_data / "settings.yaml").resolve()

        # Write setting into custom data dir
        write_settings_file({"budget": {"preset": "free"}}, data_dir=custom_data)
        assert settings_path.is_file()

        # Read back
        read_back = read_settings_file(data_dir=custom_data)
        assert read_back.get("budget", {}).get("preset") == "free"

    def test_cli_parser_accepts_data_dir(self, tmp_path: Path) -> None:
        custom_data = tmp_path / "cli_data"
        parser = build_parser()
        args = parser.parse_args(["--data-dir", str(custom_data), "init-db"])
        assert args.data_dir == str(custom_data)
        assert args.command == "init-db"

    def test_cli_init_db_in_custom_data_dir(self, tmp_path: Path) -> None:
        custom_data = tmp_path / "init_data"
        custom_data.mkdir()

        # Run init-db via CLI with --data-dir
        exit_code = main(["--data-dir", str(custom_data), "init-db"])
        assert exit_code == 0
        db_file = custom_data / "jobhunt.sqlite3"
        assert db_file.is_file()
        assert db_file.stat().st_size > 0

