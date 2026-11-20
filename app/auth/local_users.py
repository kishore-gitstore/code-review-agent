"""
app/auth/local_users.py — dashboard accounts kept in .env, for sites without
Active Directory (or for a break-glass admin that works when AD is down).

LOCAL_USERS is a comma-separated list of `username:password-hash` entries.
Passwords are never stored in clear: each hash is PBKDF2-SHA256 with its own
random salt, in the form

    pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>

Admin rights are NOT part of the entry — a local user is an admin when their
name is in ADMIN_USERS, exactly like an AD user.

Manage accounts on the server with:

    python -m app.auth.local_users add <username> [--admin]   # prompts for the password
    python -m app.auth.local_users remove <username>
    python -m app.auth.local_users list
"""

from __future__ import annotations
import asyncio
import hashlib
import hmac
import re
import secrets

import structlog

from app.settings import get_settings
from . import AuthUser, InvalidCredentials

log = structlog.get_logger(__name__)

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 600_000   # OWASP's 2023 recommendation for PBKDF2-SHA256
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def hash_password(password: str, *, iterations: int = _ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$")
        if algo != _ALGO:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def parse(raw: str) -> dict[str, str]:
    """LOCAL_USERS → {lower-cased username: hash}. Malformed entries are
    skipped with a warning rather than taking every other account down."""
    users: dict[str, str] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, stored = entry.partition(":")
        name = name.strip()
        if not sep or not _NAME_RE.match(name) or not stored.startswith(_ALGO + "$"):
            log.warning("auth.local_users.bad_entry", entry=name or "(blank)")
            continue
        users[name.lower()] = stored.strip()
    return users


def render(users: dict[str, str]) -> str:
    return ",".join(f"{name}:{stored}" for name, stored in users.items())


def configured() -> bool:
    return bool(parse(get_settings().local_users))


def is_local(username: str) -> bool:
    return username.strip().lower() in parse(get_settings().local_users)


async def authenticate_local(username: str, password: str) -> AuthUser:
    """Verify a local account. The caller has already checked is_local()."""
    name = username.strip().lower()
    stored = parse(get_settings().local_users).get(name, "")
    # PBKDF2 at 600k rounds is ~0.3s of CPU — off the event loop, like the
    # LDAP bind, so a login doesn't stall running reviews.
    ok = await asyncio.to_thread(verify_password, password, stored)
    if not ok:
        log.warning("auth.local_login_failed", user=name)
        raise InvalidCredentials("Incorrect username or password.")
    log.info("auth.login_ok", user=name, source="local")
    return AuthUser(username=name, display_name=name)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main(argv: list[str] | None = None) -> int:
    import argparse
    import getpass
    from app import envconfig

    ap = argparse.ArgumentParser(prog="python -m app.auth.local_users",
                                 description="Manage local dashboard accounts in .env")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="add a user, or reset an existing user's password")
    add.add_argument("username")
    add.add_argument("--admin", action="store_true", help="also add the user to ADMIN_USERS")
    rm = sub.add_parser("remove", help="remove a user (and their admin entry)")
    rm.add_argument("username")
    sub.add_parser("list", help="list local users")
    args = ap.parse_args(argv)

    values = envconfig.read_values()
    users = parse(values.get("LOCAL_USERS", ""))
    admins = [a.strip() for a in values.get("ADMIN_USERS", "").split(",") if a.strip()]

    if args.cmd == "list":
        if not users:
            print("No local users. Add one with: python -m app.auth.local_users add <name> --admin")
        admin_set = {a.lower() for a in admins}
        for name in users:
            print(f"{name}{'  (admin)' if name in admin_set else ''}")
        return 0

    name = args.username.strip().lower()
    if not _NAME_RE.match(name):
        print("Usernames may contain letters, digits, '.', '_' and '-' only (max 64).")
        return 2

    if args.cmd == "remove":
        if name not in users:
            print(f"No local user named {name!r}.")
            return 1
        del users[name]
        admins = [a for a in admins if a.lower() != name]
        envconfig.apply_changes({"LOCAL_USERS": render(users), "ADMIN_USERS": ",".join(admins)})
        print(f"Removed {name}. Restart the service (or press Apply on the configuration page).")
        return 0

    password = getpass.getpass(f"Password for {name}: ")
    if len(password) < 8:
        print("Use at least 8 characters.")
        return 2
    if getpass.getpass("Repeat password: ") != password:
        print("Passwords did not match.")
        return 2
    users[name] = hash_password(password)
    changes = {"LOCAL_USERS": render(users)}
    if args.admin and name not in {a.lower() for a in admins}:
        changes["ADMIN_USERS"] = ",".join(admins + [name])
    envconfig.apply_changes(changes)
    print(f"Saved {name}{' as an admin' if args.admin else ''} to .env. "
          "Restart the service for it to take effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
