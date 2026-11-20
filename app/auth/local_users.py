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


# ── Account management (shared by the CLI and the admin Users page) ──────────
#
# Each function reads .env, changes LOCAL_USERS / ADMIN_USERS and writes it
# back through envconfig (which keeps every other line intact). They don't
# reload the running settings — the web routes do that, the CLI tells the
# operator to restart.

MIN_PASSWORD_LEN = 8


class UserError(ValueError):
    """A request the account store refuses; the message is shown as-is."""


class UserExists(UserError):
    pass


class NoSuchUser(UserError):
    pass


def _load(path=None) -> tuple[dict[str, str], list[str]]:
    from app import envconfig
    values = envconfig.read_values(path)
    users = parse(values.get("LOCAL_USERS", ""))
    admins = [a.strip() for a in values.get("ADMIN_USERS", "").split(",") if a.strip()]
    return users, admins


def _save(users: dict[str, str], admins: list[str], path=None) -> None:
    from app import envconfig
    envconfig.apply_changes({"LOCAL_USERS": render(users), "ADMIN_USERS": ",".join(admins)}, path)


def clean_username(raw: str) -> str:
    name = (raw or "").strip().lower()
    if not _NAME_RE.match(name):
        raise UserError("Usernames may contain letters, digits, '.', '_' and '-' only (max 64).")
    return name


def check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LEN:
        raise UserError(f"Use at least {MIN_PASSWORD_LEN} characters for the password.")


def list_users(path=None) -> list[dict]:
    users, admins = _load(path)
    admin_set = {a.lower() for a in admins}
    return [{"username": n, "is_admin": n in admin_set} for n in sorted(users)]


def add_user(username: str, password: str, admin: bool = False, path=None) -> str:
    """Create a new account. Refuses a name that already exists — changing an
    existing user's password is reset_password(), so a typo in the username
    can't silently overwrite someone else's account."""
    name = clean_username(username)
    check_password(password)
    users, admins = _load(path)
    if name in users:
        raise UserExists(f"User '{name}' already exists. Use reset password to change it.")
    users[name] = hash_password(password)
    if admin and name not in {a.lower() for a in admins}:
        admins.append(name)
    _save(users, admins, path)
    return name


def reset_password(username: str, password: str, path=None) -> str:
    name = clean_username(username)
    check_password(password)
    users, admins = _load(path)
    if name not in users:
        raise NoSuchUser(f"No local user named '{name}'.")
    users[name] = hash_password(password)
    _save(users, admins, path)
    return name


def set_admin(username: str, admin: bool, path=None) -> str:
    name = clean_username(username)
    users, admins = _load(path)
    if name not in users:
        raise NoSuchUser(f"No local user named '{name}'.")
    admins = [a for a in admins if a.lower() != name]
    if admin:
        admins.append(name)
    _save(users, admins, path)
    return name


def remove_user(username: str, path=None) -> str:
    name = clean_username(username)
    users, admins = _load(path)
    if name not in users:
        raise NoSuchUser(f"No local user named '{name}'.")
    del users[name]
    admins = [a for a in admins if a.lower() != name]
    _save(users, admins, path)
    return name


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main(argv: list[str] | None = None) -> int:
    import argparse
    import getpass

    ap = argparse.ArgumentParser(prog="python -m app.auth.local_users",
                                 description="Manage local dashboard accounts in .env")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="add a new user")
    add.add_argument("username")
    add.add_argument("--admin", action="store_true", help="also add the user to ADMIN_USERS")
    rp = sub.add_parser("reset", help="reset an existing user's password")
    rp.add_argument("username")
    rm = sub.add_parser("remove", help="remove a user (and their admin entry)")
    rm.add_argument("username")
    sub.add_parser("list", help="list local users")
    args = ap.parse_args(argv)

    restart = "Restart the service for it to take effect."
    try:
        if args.cmd == "list":
            rows = list_users()
            if not rows:
                print("No local users yet. Add an admin with the 'add <name> --admin' command.")
            for r in rows:
                print(f"{r['username']}{'  (admin)' if r['is_admin'] else ''}")
            return 0

        name = clean_username(args.username)
        if args.cmd == "remove":
            remove_user(name)
            print(f"Removed {name}. {restart}")
            return 0

        # Check before asking for a password, so nobody types one for nothing.
        exists = name in {r["username"] for r in list_users()}
        if args.cmd == "add" and exists:
            print(f"User '{name}' is already registered. To change the password, use: reset {name}")
            return 3
        if args.cmd == "reset" and not exists:
            print(f"No local user named '{name}'. To create it, use: add {name}")
            return 1

        password = getpass.getpass(f"{'New password' if args.cmd == 'reset' else 'Password'} for {name}: ")
        check_password(password)
        if getpass.getpass("Repeat password: ") != password:
            print("Passwords did not match.")
            return 2
        if args.cmd == "reset":
            reset_password(name, password)
            print(f"Password for {name} changed. {restart}")
        else:
            add_user(name, password, admin=args.admin)
            print(f"Registered {name}{' as an admin' if args.admin else ''}. {restart}")
        return 0
    except UserExists as exc:
        print(exc)
        return 3
    except NoSuchUser as exc:
        print(exc)
        return 1
    except UserError as exc:
        print(exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
