#!/usr/bin/env bash
# setup.sh — one-shot bootstrap for Ubuntu/Debian Linux.
# Installs system prerequisites (git, Python 3.12, Ollama), creates the venv,
# installs Python deps, and bootstraps .env from .env.example.
#
# Usage (run from anywhere, e.g. ./setup/linux/setup.sh from the repo root):
#   setup/linux/setup.sh                       # full bootstrap
#   setup/linux/setup.sh --no-system            # skip OS package installs (git/python/ollama)
#   setup/linux/setup.sh --no-ollama            # skip Ollama install/pull even if LLM_PROVIDER=ollama
#   setup/linux/setup.sh --model deepseek-r1:7b # Ollama model to pull (default: qwen3-coder:latest)
#   setup/linux/setup.sh --static-langs js,python   # also install linters for these languages
#                                                     # (python|js|dotnet|rust|cpp|java)
set -euo pipefail
# This script lives in setup/linux/ -- everything it operates on (venv, .env,
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

if [ "$(uname -s)" != "Linux" ]; then
    echo "This script is for Ubuntu/Debian Linux. Use setup/macos/setup.sh or setup/windows/setup.ps1 instead." >&2
    exit 1
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

pkg_install() {
    if ! command -v apt-get >/dev/null 2>&1; then
        echo "This script only supports apt-get (Ubuntu/Debian)." >&2
        echo "Install manually: $*" >&2
        return 1
    fi
    $SUDO apt-get update -qq
    $SUDO apt-get install -y "$@"
}

# ── System prerequisites ────────────────────────────────────
if [ "$SKIP_SYSTEM" = false ]; then
    log "Checking system prerequisites (git, Python 3.12)..."

    if ! command -v git >/dev/null 2>&1; then
        log "Installing git..."
        pkg_install git
    fi

    if ! command -v python3.12 >/dev/null 2>&1; then
        log "Installing Python 3.12..."
        pkg_install python3.12 python3.12-venv python3-pip || {
            echo "python3.12 isn't in this distro's default apt repos." >&2
            echo "Add the deadsnakes PPA first:" >&2
            echo "  sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt-get update" >&2
            echo "then re-run this script" >&2
            exit 1
        }
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
        curl -fsSL https://ollama.com/install.sh | sh
    fi
    if command -v ollama >/dev/null 2>&1; then
        log "Pulling Ollama model: $OLLAMA_MODEL (this can take a while)..."
        ollama pull "$OLLAMA_MODEL" || log "Model pull failed/skipped — make sure the Ollama service is running, then run: ollama pull $OLLAMA_MODEL"
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
                pkg_install clang-tidy
                ;;
            java)
                log "Installing SpotBugs..."
                pkg_install spotbugs
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
echo "  2. Start the server: setup/linux/run.sh"
echo "  3. Point a GitLab webhook at http://<this-host>:8000/webhook/gitlab/<instance-name>"
