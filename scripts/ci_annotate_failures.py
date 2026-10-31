"""Turn a pytest log into GitHub Actions error annotations (visible on the run page without logging in).

Usage in CI (only when the pytest step failed):  python scripts/ci_annotate_failures.py pytest.log
Prints test ids and the first message line of each failure; never anything else from the log.
"""

from __future__ import annotations

import sys
from pathlib import Path

MAX_ANNOTATIONS = 40


def failure_lines(log_text: str) -> list[str]:
    lines = [line.strip() for line in log_text.splitlines()]
    picked = [line for line in lines if line.startswith(("FAILED ", "ERROR "))]
    summary = [line for line in lines if " passed" in line or " failed" in line or " error" in line]
    return picked + summary[-1:]


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path("pytest.log")
    if not path.is_file():
        print("::error title=pytest::no pytest.log was produced (the run died before or while collecting tests)")
        return 0
    for line in failure_lines(path.read_text(encoding="utf-8", errors="replace"))[:MAX_ANNOTATIONS]:
        print(f"::error title=pytest::{line[:400].replace('%', '%25')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
