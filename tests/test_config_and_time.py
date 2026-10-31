from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from jobhunt.config import Config, load_config
from jobhunt.errors import ConfigError
from jobhunt.timeutil import coerce_timestamp, parse_iso, to_iso, to_utc_iso


def test_real_config_loads_and_exposes_targets(config: Config) -> None:
    assert config.targets.graduation_year == 2027
    assert "greenhouse" in config.sources and config.source_enabled("greenhouse")
    assert "intern" in config.prefilter_include_keywords          # from the explicit extras
    assert "machine learning" in config.prefilter_include_keywords  # from targets.fields
    assert config.discovery.common_crawl.patterns[0].ats == "greenhouse"
    assert config.migrations_directory.name == "migrations"


def test_user_agent_only_includes_configured_contact() -> None:
    assert "@" not in Config().user_agent  # no contact configured -> nothing personal in the header
    with_contact = Config(user_agent_contact="https://example.com/me")
    assert with_contact.user_agent.endswith("; https://example.com/me)")


def test_unknown_config_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("lifecycle:\n  missed_runs_to_clos: 3\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(path)


def test_missing_and_malformed_config(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(bad)


def test_source_options_are_collected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("sources:\n  adzuna: {enabled: false, countries: [in, us]}\n", encoding="utf-8")
    loaded = load_config(path)
    assert loaded.sources["adzuna"].enabled is False
    assert loaded.sources["adzuna"].options == {"countries": ["in", "us"]}
    assert loaded.project_root == tmp_path.resolve()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-05T16:05:18-04:00", "2026-10-05T20:05:18Z"),
        ("2026-10-05T16:05:18Z", "2026-10-05T16:05:18Z"),
        ("2026-10-05 16:05:18 UTC", "2026-10-05T16:05:18Z"),
        ("2026-10-05", "2026-10-05T00:00:00Z"),
        (1788199939703, "2026-08-31T18:12:19Z"),   # epoch milliseconds (Lever createdAt)
        (1788199939, "2026-08-31T18:12:19Z"),      # epoch seconds
        ("1788199939703", "2026-08-31T18:12:19Z"),
    ],
)
def test_timestamps_are_normalized_to_utc_iso(raw: object, expected: str) -> None:
    assert to_utc_iso(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "not a date", True, 0, -5, float("nan"), {}, []])
def test_unparseable_timestamps_return_none(raw: object) -> None:
    assert coerce_timestamp(raw) is None


def test_iso_round_trip() -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert parse_iso(to_iso(moment)) == moment
    with pytest.raises(ValueError):
        parse_iso("garbage")
