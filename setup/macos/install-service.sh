#!/usr/bin/env bash
# install-service.sh — installs the Code Review Agent as a launchd daemon on
# macOS: starts at boot (no login needed), restarts automatically if it ever
# stops. Generates the plist from this machine's user/paths, so nothing needs
# manual editing. Run with sudo: sudo ./setup/macos/install-service.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this with sudo: sudo ./setup/macos/install-service.sh" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SERVICE_USER="${SUDO_USER:-$(logname 2>/dev/null || echo "$USER")}"
LABEL="com.codereviewagent.service"
PLIST_PATH="/Library/LaunchDaemons/${LABEL}.plist"
LOG_DIR="$REPO_ROOT/logs"

if [ ! -d "$REPO_ROOT/venv" ]; then
    echo "No venv found at $REPO_ROOT/venv — run setup/macos/run.sh once by hand first" >&2
    echo "to create it and confirm the app actually starts, then Ctrl+C and re-run this." >&2
    exit 1
fi

if [ ! -f "$REPO_ROOT/.env" ]; then
    echo "No .env found at $REPO_ROOT/.env — copy .env.example to .env and configure it first." >&2
    exit 1
fi

mkdir -p "$LOG_DIR"
chown "$SERVICE_USER" "$LOG_DIR"

# launchd has no EnvironmentFile equivalent, so a small bash wrapper exports
# .env (tolerating CRLF) and then exec's the venv's python. No UVICORN_RELOAD:
# a service must not re-exec itself on every file touch.
cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>UserName</key>
    <string>${SERVICE_USER}</string>
    <key>WorkingDirectory</key>
    <string>${REPO_ROOT}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>set -a; source &lt;(tr -d '\r' &lt; .env); set +a; exec venv/bin/python main.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>60</integer>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/service.out.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/service.err.log</string>
</dict>
</plist>
PLIST

chown root:wheel "$PLIST_PATH"
chmod 644 "$PLIST_PATH"

# Re-running is safe: remove any previously loaded copy first.
# bootout returns before the old process has fully exited, and bootstrap fails
# with "5: Input/output error" if the label is still registered — so wait for
# it to disappear (up to ~30s) before loading the new plist.
if launchctl print "system/${LABEL}" >/dev/null 2>&1; then
    echo "Stopping existing ${LABEL}..."
    launchctl bootout "system/${LABEL}" 2>/dev/null || true
    for _ in $(seq 1 30); do
        launchctl print "system/${LABEL}" >/dev/null 2>&1 || break
        sleep 1
    done
fi
launchctl bootstrap system "$PLIST_PATH" 2>/dev/null \
    || launchctl load -w "$PLIST_PATH"
launchctl enable "system/${LABEL}"
launchctl kickstart -k "system/${LABEL}"

echo "Installed $PLIST_PATH"
echo "  User             : $SERVICE_USER"
echo "  WorkingDirectory : $REPO_ROOT"
echo
echo "Status  : sudo launchctl print system/${LABEL}"
echo "Logs    : tail -f $LOG_DIR/service.out.log $LOG_DIR/service.err.log"
echo "Restart after a code/config change: sudo launchctl kickstart -k system/${LABEL}"
