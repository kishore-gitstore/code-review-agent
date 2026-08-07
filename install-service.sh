#!/usr/bin/env bash
# install-service.sh — installs code-review-agent.service as a systemd
# service: starts on boot, restarts automatically if it ever stops
# unexpectedly. Fills in the User/WorkingDirectory placeholders in
# code-review-agent.service from this machine, so nothing needs manual
# editing. Run with sudo: sudo ./install-service.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this with sudo: sudo ./install-service.sh" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="${SUDO_USER:-$(logname 2>/dev/null || echo "$USER")}"
UNIT_NAME="code-review-agent"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}.service"

if [ ! -d "$SCRIPT_DIR/venv" ]; then
    echo "No venv found at $SCRIPT_DIR/venv — run ./run.sh once by hand first" >&2
    echo "to create it and confirm the app actually starts, then Ctrl+C and re-run this." >&2
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "No .env found at $SCRIPT_DIR/.env — copy .env.example to .env and configure it first." >&2
    exit 1
fi

sed \
    -e "s|__SERVICE_USER__|$SERVICE_USER|g" \
    -e "s|__WORKDIR__|$SCRIPT_DIR|g" \
    "$SCRIPT_DIR/code-review-agent.service" > "$UNIT_PATH"

echo "Installed $UNIT_PATH"
echo "  User/Group      : $SERVICE_USER"
echo "  WorkingDirectory : $SCRIPT_DIR"
echo

systemctl daemon-reload
systemctl enable --now "$UNIT_NAME"

echo
systemctl status "$UNIT_NAME" --no-pager || true
echo
echo "Logs: journalctl -u $UNIT_NAME -f"
echo "Restart after a code/config change: sudo systemctl restart $UNIT_NAME"
