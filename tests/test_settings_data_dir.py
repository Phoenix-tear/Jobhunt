"""UI settings are written under the configured data dir, never under project_root/data."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from fastapi.testclient import TestClient

from jobhunt.api.app import create_app
from jobhunt.config import Config
from jobhunt.db import apply_migrations, connect


def test_budget_and_schedule_settings_land_in_the_data_dir(config: Config, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    data_dir = tmp_path / "custom-data"
    custom = dataclasses.replace(
        config, project_root=project, data_dir=data_dir, migrations_dir=str(config.migrations_directory), database_path="data/jobhunt.sqlite3"
    )
    database = custom.database_file
    database.parent.mkdir(parents=True)
    connection = connect(database)
    apply_migrations(connection, custom.migrations_directory)
    connection.close()

    app = create_app(custom, extra_allowed_hosts=("testserver",))
    client = TestClient(app)
    client.headers["x-jobhunt-token"] = app.state.csrf_token

    assert client.post("/api/settings/budget", json={"preset": "free", "monthly_budget_usd": 1.0}).status_code == 200
    assert client.post("/api/settings/schedule", json={"backend": "in_app"}).status_code == 200

    assert (data_dir / "settings.yaml").is_file()
    assert not (project / "data").exists()
