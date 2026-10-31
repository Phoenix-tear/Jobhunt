#!/usr/bin/env bash
# scripts/start.sh - Start Jobhunt local server on macOS & Linux
# Picks a free port (8000, then 8001-8010) and never touches an app that is already running.
# Usage: ./scripts/start.sh [--no-browser]
set -euo pipefail

PY=".venv/bin/python"
if [ ! -f "$PY" ]; then
    PY="python3"
fi

exec "$PY" -m jobhunt.platform.launch "$@"
