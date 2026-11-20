#!/usr/bin/env bash
# manage-users.sh - add, remove and list the dashboard's local sign-in
# accounts (LOCAL_USERS / ADMIN_USERS in .env), using the project's venv
# Python so the app's packages are always available.
#
#   ./setup/linux/manage-users.sh                    # interactive menu
#   ./setup/linux/manage-users.sh add admin --admin  # add an admin
#   ./setup/linux/manage-users.sh add kishore        # add a normal user
#   ./setup/linux/manage-users.sh reset kishore      # reset an existing user's password
#   ./setup/linux/manage-users.sh remove kishore
#   ./setup/linux/manage-users.sh list
#
# Passwords are typed at a hidden prompt; only a PBKDF2 hash goes into .env.
# Restart the service afterwards for changes to take effect.
set -euo pipefail

# This script lives in setup/<os>/ -- the venv and .env live at the repo root.
cd "$(dirname "$0")/../.."

PY="venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "No venv found at $(pwd)/venv -- run the setup script for this OS first." >&2
    exit 1
fi
if [ ! -f ".env" ]; then
    echo ".env not found -- run the setup script (or copy .env.example to .env) first." >&2
    exit 1
fi

cli() { "$PY" -m app.auth.local_users "$@"; }

if [ $# -gt 0 ]; then
    exec "$PY" -m app.auth.local_users "$@"
fi

read_name() {
    local name
    while true; do
        read -r -p "Username: " name
        if [[ "$name" =~ ^[A-Za-z0-9._-]{1,64}$ ]]; then echo "$name"; return; fi
        echo "Usernames may contain letters, digits, '.', '_' and '-' only (max 64)." >&2
    done
}

changed=0
while true; do
    echo
    echo "Dashboard users"
    echo "  1) List users"
    echo "  2) Add a normal user (dashboard only)"
    echo "  3) Add an admin (dashboard + Home + Configuration)"
    echo "  4) Reset a user's password"
    echo "  5) Remove a user"
    echo "  q) Quit"
    read -r -p "Choose: " choice
    case "$choice" in
        1) cli list || true ;;
        2) cli add "$(read_name)" && changed=1 || true ;;
        4) cli reset "$(read_name)" && changed=1 || true ;;
        3) cli add "$(read_name)" --admin && changed=1 || true ;;
        5) name="$(read_name)"
           read -r -p "Remove '$name'? (y/N) " ok
           if [ "$ok" = "y" ] || [ "$ok" = "Y" ]; then cli remove "$name" && changed=1 || true; fi ;;
        q|Q|quit|exit|"")
           if [ "$changed" = 1 ]; then
               echo
               echo "Changes saved to .env. Restart the service for them to take effect."
           fi
           exit 0 ;;
        *) echo "Unknown choice '$choice'." ;;
    esac
done
