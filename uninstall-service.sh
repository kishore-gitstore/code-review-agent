#!/usr/bin/env bash
# uninstall-service.sh — stops and removes the code-review-agent systemd
# service installed by install-service.sh. Only touches the systemd unit —
# the venv, .env, and repo files are left alone. Run with sudo:
# sudo ./uninstall-service.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this with sudo: sudo ./uninstall-service.sh" >&2
    exit 1
fi

UNIT_NAME="code-review-agent"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}.service"

if [ ! -f "$UNIT_PATH" ]; then
    echo "$UNIT_PATH not found — nothing to uninstall."
    exit 0
fi

systemctl stop "$UNIT_NAME" 2>/dev/null || true
systemctl disable "$UNIT_NAME" 2>/dev/null || true
rm -f "$UNIT_PATH"
systemctl daemon-reload
systemctl reset-failed "$UNIT_NAME" 2>/dev/null || true

echo "Removed $UNIT_PATH and disabled/stopped $UNIT_NAME."
echo "The venv, .env, and repo files were left untouched."
