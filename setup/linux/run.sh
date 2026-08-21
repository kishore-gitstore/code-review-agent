#!/usr/bin/env bash
# run.sh — set up (if needed) and start the Code Review Agent service directly,
# without Docker. Creates/reuses a venv, installs deps, loads .env, runs main.py.
set -euo pipefail
# This script lives in setup/linux/ -- everything it operates on (venv, .env,
# requirements.txt, main.py) lives at the repo root, two directories up.
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

VENV_DIR="venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment in $VENV_DIR..."
    python3 -m venv "$VENV_DIR" 2>/dev/null || python -m venv "$VENV_DIR"
fi

if [ -f "$VENV_DIR/bin/activate" ]; then
    source "$VENV_DIR/bin/activate"
elif [ -f "$VENV_DIR/Scripts/activate" ]; then
    source "$VENV_DIR/Scripts/activate"
else
    echo "Could not find an activate script under $VENV_DIR/" >&2
    exit 1
fi

pip install -q --disable-pip-version-check -r requirements.txt

if [ ! -f ".env" ]; then
    echo ".env not found — copy .env.example to .env and set GITLAB_INSTANCES first." >&2
    exit 1
fi

# Export .env into the shell so APP_HOST/APP_PORT (read via os.getenv in
# main.py) match what's configured there, not just main.py's fallback defaults.
set -a
source .env
set +a

# Interactive/dev usage — live-reload on file changes. The systemd unit
# (code-review-agent.service) runs main.py directly instead of this script,
# so it does NOT set this and stays in stable no-reload mode.
export UVICORN_RELOAD=true

exec python main.py
