#!/usr/bin/env bash
# setup.sh — one-shot bootstrap for macOS.
# Installs system prerequisites (git, Python 3.12, Ollama) via Homebrew,
# creates the venv, installs Python deps, and bootstraps .env from
# .env.example.
#
# Usage (run from anywhere, e.g. ./setup/macos/setup.sh from the repo root):
#   setup/macos/setup.sh                       # full bootstrap
#   setup/macos/setup.sh --no-system            # skip Homebrew installs (git/python/ollama)
#   setup/macos/setup.sh --no-ollama            # skip Ollama install/pull even if LLM_PROVIDER=ollama
#   setup/macos/setup.sh --model deepseek-r1:7b # Ollama model to pull (default: qwen3-coder:latest)
#   setup/macos/setup.sh --static-langs js,python   # also install linters for these languages
#                                                     # (python|js|dotnet|rust|cpp|java)
set -euo pipefail
# This script lives in setup/macos/ -- everything it operates on (venv, .env,
# requirements.txt, main.py) lives at the repo root, two directories up.
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

SKIP_SYSTEM=false
SKIP_OLLAMA=false
OLLAMA_MODEL="qwen3-coder:latest"
STATIC_LANGS=""

while [ $# -gt 0 ]; do
    case "$1" in
        --no-system) SKIP_SYSTEM=true ;;
        --no-ollama) SKIP_OLLAMA=true ;;
        --model) OLLAMA_MODEL="$2"; shift ;;
        --static-langs) STATIC_LANGS="$2"; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

log() { echo "==> $*"; }

if [ "$(uname -s)" != "Darwin" ]; then
    echo "This script is for macOS. Use setup/linux/setup.sh or setup/windows/setup.ps1 instead." >&2
    exit 1
fi

require_brew() {
    command -v brew >/dev/null 2>&1 || { echo "Homebrew required: https://brew.sh" >&2; exit 1; }
}

# ── System prerequisites ────────────────────────────────────
if [ "$SKIP_SYSTEM" = false ]; then
    log "Checking system prerequisites (git, Python 3.12)..."
    require_brew

    if ! command -v git >/dev/null 2>&1; then
        log "Installing git..."
        brew install git
    fi

    if ! command -v python3.12 >/dev/null 2>&1; then
        log "Installing Python 3.12..."
        brew install python@3.12
    fi
else
    log "Skipping system package installs (--no-system)."
fi

PYTHON_BIN="$(command -v python3.12 || command -v python3 || command -v python)"
if [ -z "$PYTHON_BIN" ]; then
    echo "No Python interpreter found on PATH." >&2
    exit 1
fi
log "Using Python: $PYTHON_BIN ($("$PYTHON_BIN" --version))"

# ── .env bootstrap (before deciding what to install, so the LLM_PROVIDER
#    choice already on disk — or the default — drives the Ollama step) ──
if [ ! -f ".env" ]; then
    log "Creating .env from .env.example — edit it (GITLAB_INSTANCES, provider keys) before running the server."
    cp .env.example .env
else
    log ".env already exists, leaving it as-is."
fi

LLM_PROVIDER="$(grep -E '^LLM_PROVIDER=' .env | tail -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
LLM_PROVIDER="${LLM_PROVIDER:-ollama}"

# ── Ollama (only if this config actually uses it) ───────────
if [ "$SKIP_OLLAMA" = false ] && [ "$LLM_PROVIDER" = "ollama" ]; then
    if ! command -v ollama >/dev/null 2>&1; then
        log "LLM_PROVIDER=ollama — installing Ollama..."
        require_brew
        brew install ollama
        log "Start it with 'brew services start ollama' or open the Ollama app."
    fi
    if command -v ollama >/dev/null 2>&1; then
        log "Pulling Ollama model: $OLLAMA_MODEL (this can take a while)..."
        ollama pull "$OLLAMA_MODEL" || log "Model pull failed/skipped — make sure the Ollama app/service is running, then run: ollama pull $OLLAMA_MODEL"
    fi
elif [ "$LLM_PROVIDER" != "ollama" ]; then
    log "LLM_PROVIDER=$LLM_PROVIDER — skipping Ollama install. Make sure the matching API key is set in .env."
else
    log "Skipping Ollama install (--no-ollama)."
fi

# ── Optional static-analysis tool installs ──────────────────
if [ -n "$STATIC_LANGS" ]; then
    IFS=',' read -ra LANGS <<< "$STATIC_LANGS"
    for lang in "${LANGS[@]}"; do
        case "$lang" in
            python)
                log "Installing Ruff..."
                "$PYTHON_BIN" -m pip install --quiet --user ruff
                ;;
            js)
                log "Installing ESLint (global npm install)..."
                command -v npm >/dev/null 2>&1 && npm install -g eslint || echo "npm not found — install Node.js first." >&2
                ;;
            dotnet)
                log "dotnet SDK: install from https://dotnet.microsoft.com/download (no unattended installer here)."
                ;;
            rust)
                log "Installing Rust + Clippy..."
                command -v rustup >/dev/null 2>&1 || curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
                command -v rustup >/dev/null 2>&1 && rustup component add clippy
                ;;
            cpp)
                log "Installing Clang-Tidy..."
                require_brew
                brew install llvm
                ;;
            java)
                log "Installing SpotBugs..."
                require_brew
                brew install spotbugs
                ;;
            *)
                echo "Unknown --static-langs entry: $lang (expected python|js|dotnet|rust|cpp|java)" >&2
                ;;
        esac
    done
fi

# ── Python venv + deps ───────────────────────────────────────
if [ ! -d "venv" ]; then
    log "Creating virtual environment..."
    "$PYTHON_BIN" -m venv venv
fi

log "Installing Python dependencies..."
venv/bin/python -m pip install -q --disable-pip-version-check --upgrade pip
venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt

log "Setup complete."
echo
echo "Next steps:"
echo "  1. Edit .env — set GITLAB_INSTANCES and the ${LLM_PROVIDER} provider key/config."
echo "  2. Start the server: setup/macos/run.sh"
echo "  3. Point a GitLab webhook at http://<this-host>:8000/webhook/gitlab/<instance-name>"
