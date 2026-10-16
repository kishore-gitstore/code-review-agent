"""
app/auth/deps.py — the FastAPI pieces the routes use.

`require_user` is the single gate in front of every reporting and control
endpoint. It resolves, in order:

  1. AUTH_ENABLED=false      → a synthetic local user (dev boxes, CI).
  2. X-API-Token header      → the machine identity, for scripts that trigger
                               reviews without a browser session.
  3. the session cookie      → a real AD user who logged in at /login.

and 401s if none of those apply.
"""

from __future__ import annotations
import secrets
import structlog
from fastapi import HTTPException, Request, Response

from app.settings import get_settings
from . import AuthUser
from . import session as session_mod

log = structlog.get_logger(__name__)

# Used when AUTH_ENABLED=false, so downstream code (the MR cancellation note,
# the audit log) always has a name to attribute an action to.
ANONYMOUS = AuthUser(username="anonymous", display_name="Anonymous (auth disabled)")

# Identity for a caller presenting DASHBOARD_API_TOKEN rather than logging in.
API_CLIENT = AuthUser(username="api-token", display_name="API token")

API_TOKEN_HEADER = "X-API-Token"


def auth_enabled() -> bool:
    return get_settings().auth_enabled


def cookie_name() -> str:
    return get_settings().session_cookie_name


def _api_token_ok(request: Request) -> bool:
    expected = (get_settings().dashboard_api_token or "").strip()
    if not expected:
        return False
    presented = request.headers.get(API_TOKEN_HEADER, "")
    # Constant-time: a short-circuiting == on a secret is a timing oracle.
    return bool(presented) and secrets.compare_digest(presented, expected)


def current_user(request: Request) -> AuthUser | None:
    """The signed-in user, or None. Never raises — use it where being signed
    out is a normal outcome (the /login page, GET /api/me)."""
    if not auth_enabled():
        return ANONYMOUS
    if _api_token_ok(request):
        return API_CLIENT
    return session_mod.load(request.cookies.get(cookie_name()))


def require_user(request: Request) -> AuthUser:
    """FastAPI dependency: 401 unless the request carries a valid session."""
    user = current_user(request)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Sign in with your Active Directory account to view reports.",
        )
    # Lets the dashboard show who's signed in without a second round trip.
    request.state.user = user
    return user


def is_admin(user: AuthUser) -> bool:
    """May this user configure the service and start/stop it?

    Fails closed: with auth on and neither ADMIN_USERS nor ADMIN_GROUP set,
    nobody is an admin. The API-token identity is never an admin — a token
    that lives in CI scripts shouldn't be able to rewrite the configuration.
    """
    cfg = get_settings()
    if not cfg.auth_enabled:
        return True
    if user is API_CLIENT:
        return False
    names = {n.strip().lower() for n in cfg.admin_users.split(",") if n.strip()}
    if user.username.lower() in names:
        return True
    group = cfg.admin_group.strip().lower()
    return bool(group) and any(g.lower() == group for g in user.groups)


def require_admin(request: Request) -> AuthUser:
    user = require_user(request)
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="This action needs an administrator.")
    return user


def set_session_cookie(response: Response, user: AuthUser) -> None:
    cfg = get_settings()
    response.set_cookie(
        key=cfg.session_cookie_name,
        value=session_mod.issue(user),
        max_age=max(1, cfg.session_ttl_hours) * 3600,
        httponly=True,          # not readable from JS — an XSS can't lift the session
        samesite="lax",         # survives a normal top-level navigation, blocks cross-site POSTs
        secure=cfg.session_cookie_secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=get_settings().session_cookie_name, path="/")


def safe_next(raw: str | None, default: str = "/") -> str:
    """
    Sanitize a ?next= target. Only same-origin absolute paths are allowed:
    "//evil.example" and "https://evil.example" are browser-valid redirect
    targets, so echoing the parameter back unchecked would turn the login
    page into an open redirect.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return default
    return raw
