#!/usr/bin/env bash
# scripts/setup.sh - Jobhunt one-command setup for macOS & Linux
set -euo pipefail

echo "=== Jobhunt Setup (macOS / Linux) ==="

# 1. Locate Python >= 3.12
PY_CMD=""
for cand in python3.14 python3.13 python3.12 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
            PY_CMD="$cand"
            break
        fi
    fi
done

if [ -z "$PY_CMD" ]; then
    echo "Error: Python >= 3.12 is required. Please install Python 3.12 or newer (e.g. via brew install python@3.12)" >&2
    exit 1
fi
echo "Using Python: $($PY_CMD --version)"

# 2. Create .venv if not exists
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment .venv..."
    "$PY_CMD" -m venv .venv
fi
VENV_PY=".venv/bin/python"

# 3. Install pinned dependencies
echo "Installing Python dependencies from requirements.lock..."
"$VENV_PY" -m pip install --upgrade pip --quiet
if [ -f "requirements.lock" ]; then
    "$VENV_PY" -m pip install -r requirements.lock -e ".[api,gmail,matching,dev]" --quiet
else
    "$VENV_PY" -m pip install -e ".[api,gmail,matching,dev]" --quiet
fi

# 4. Check Node >= 22.12
if command -v node >/dev/null 2>&1; then
    NODE_VER=$(node -v | tr -d 'v')
    NODE_MAJOR=$(echo "$NODE_VER" | cut -d. -f1)
    NODE_MINOR=$(echo "$NODE_VER" | cut -d. -f2)
    if [ "$NODE_MAJOR" -lt 22 ] || { [ "$NODE_MAJOR" -eq 22 ] && [ "$NODE_MINOR" -lt 12 ]; }; then
        echo "Warning: Node.js v$NODE_VER detected. Jobhunt requires Node.js >= 22.12 (22 LTS). Install from https://nodejs.org/ or brew install node@22" >&2
    else
        echo "Node.js v$NODE_VER detected (>= 22.12 OK)"
    fi
else
    echo "Warning: Node.js not found. To build web UI assets, please install Node.js >= 22.12 (22 LTS) from https://nodejs.org/ or brew install node@22" >&2
fi

# 5. Build frontend if npm is available
if command -v npm >/dev/null 2>&1 && [ -d "web" ]; then
    (
        cd web
        if [ ! -d "node_modules" ]; then
            echo "Installing frontend dependencies (npm ci)..."
            npm ci --quiet
        fi
        echo "Building frontend assets (npm run build)..."
        npm run build --silent
    )
fi

# 6. Ensure Tectonic is present
echo "Checking Tectonic LaTeX engine..."
"$VENV_PY" -m jobhunt.platform.tectonic_install

# 7. Create data/ and .secrets/
mkdir -p data .secrets

# 8. Run doctor
echo ""
echo "Running system doctor..."
"$VENV_PY" -m jobhunt doctor || true

# 9. Done. Setup never opens a browser or starts a server; start.sh does that on a free port.
echo ""
echo "=== Setup Complete! ==="
echo "Run: ./scripts/start.sh   (picks a free port; add --no-browser to skip opening the browser)"
echo "Or:  ./.venv/bin/python -m jobhunt serve"
