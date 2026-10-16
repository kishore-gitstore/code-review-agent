#!/usr/bin/env bash
# uninstall-service.sh — stops and removes the launchd daemon installed by
# install-service.sh. Only touches the plist — the venv, .env, logs, and repo
# files are left alone. Run with sudo: sudo ./setup/macos/uninstall-service.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this with sudo: sudo ./setup/macos/uninstall-service.sh" >&2
    exit 1
fi

LABEL="com.codereviewagent.service"
PLIST_PATH="/Library/LaunchDaemons/${LABEL}.plist"

if [ ! -f "$PLIST_PATH" ]; then
    echo "$PLIST_PATH not found — nothing to uninstall."
    exit 0
fi

launchctl bootout "system/${LABEL}" 2>/dev/null || true
rm -f "$PLIST_PATH"

echo "Removed $PLIST_PATH and stopped ${LABEL}."
echo "The venv, .env, logs, and repo files were left untouched."
