"""Names of the user's own resume-specific entities (an event brand, its team) and the file-name prefix for downloads.

Code that checks "edition" labels and roles needs these names. The defaults are generic so the repository carries no
personal data; put the real ones in the git-ignored `data/profile/terms.yaml`:

    event_name: Your Event
    team_name: Your Team
    download_name: Your_Name

Set JOBHUNT_TERMS_FILE to another path (or to an empty string to disable the file, as the tests do).
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_EVENT = "Fest"
DEFAULT_TEAM = "Event Team"
DEFAULT_DOWNLOAD = "Resume"


def _load() -> tuple[str, str, str]:
    raw = os.environ.get("JOBHUNT_TERMS_FILE")
    path = Path("data/profile/terms.yaml") if raw is None else Path(raw) if raw else None
    values: dict[str, str] = {}
    try:
        if path is not None and path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                key, sep, value = line.partition(":")
                if sep and not line.lstrip().startswith("#"):
                    values[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    return (values.get("event_name") or DEFAULT_EVENT, values.get("team_name") or DEFAULT_TEAM,
            values.get("download_name") or DEFAULT_DOWNLOAD)


EVENT_NAME, TEAM_NAME, DOWNLOAD_NAME = _load()


def load_project_markers() -> dict[str, tuple[str, ...]]:
    """Optional git-ignored `data/profile/project_markers.txt`, one line per project: `heading fragment: marker | marker`."""
    raw = os.environ.get("JOBHUNT_TERMS_FILE")
    if raw == "":
        return {}
    path = Path("data/profile/project_markers.txt") if raw is None else Path(raw).with_name("project_markers.txt")
    markers: dict[str, tuple[str, ...]] = {}
    try:
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                key, sep, rest = line.partition(":")
                if sep and key.strip() and not line.lstrip().startswith("#"):
                    markers[key.strip().lower()] = tuple(m.strip().lower() for m in rest.split("|") if m.strip())
    except OSError:
        pass
    return markers
