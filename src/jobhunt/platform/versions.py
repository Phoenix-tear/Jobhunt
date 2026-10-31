"""Single source of truth for the supported runtime versions.

CI, the setup/start scripts, `jobhunt doctor` and the docs must agree with these values
(tests/test_supported_versions.py enforces it).
"""

from __future__ import annotations

MIN_PYTHON: tuple[int, int] = (3, 12)
MIN_NODE: tuple[int, int] = (22, 12)
CI_PYTHON_VERSIONS: tuple[str, ...] = ("3.12", "3.13")
CI_NODE_MAJOR: int = 22


def format_version(parts: tuple[int, int]) -> str:
    return f"{parts[0]}.{parts[1]}"


def node_is_supported(version: str) -> bool:
    """True when a `node -v` style string (e.g. v22.12.0) meets MIN_NODE."""
    pieces = version.strip().lstrip("v").split(".")
    try:
        major, minor = int(pieces[0]), int(pieces[1]) if len(pieces) > 1 else 0
    except ValueError:
        return False
    return (major, minor) >= MIN_NODE
