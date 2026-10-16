"""
app/auth/session.py — signed, expiring session cookies.

Stateless by design: the cookie *is* the session. There's no server-side
store, so a service restart (NSSM recycles it on every deploy) doesn't sign
everyone out and two processes behind a load balancer need nothing shared but
SESSION_SECRET.

The cookie carries only identity (username, display name, email, group CNs) —
never the password, and never anything the browser is trusted to assert: it's
HMAC-signed with SESSION_SECRET, so a tampered payload fails to load rather
than logging someone in as someone else.
"""

from __future__ import annotations
import secrets
import structlog
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.settings import get_settings
from . import AuthUser

log = structlog.get_logger(__name__)

# Namespaces the signature. Changing it invalidates every issued cookie.
_SALT = "cra-dashboard-session-v1"

_serializer: URLSafeTimedSerializer | None = None
_secret_is_ephemeral = False


def _get_serializer() -> URLSafeTimedSerializer:
    global _serializer, _secret_is_ephemeral
    if _serializer is None:
        secret = (get_settings().session_secret or "").strip()
        if not secret:
            # Refusing to start would be worse than this: the dashboard would
            # be unreachable after an upgrade until someone edits .env. A
            # random per-process secret is still cryptographically sound —
            # its only cost is that restarts log everyone out, which the
            # warning explains.
            secret = secrets.token_urlsafe(48)
            _secret_is_ephemeral = True
            log.warning(
                "auth.session_secret_generated",
                detail="SESSION_SECRET is not set in .env — generated a random one for "
                       "this process. Logins will not survive a service restart, and "
                       "will not work across multiple instances. Set SESSION_SECRET to fix.",
            )
        _serializer = URLSafeTimedSerializer(secret, salt=_SALT)
    return _serializer


def reset_serializer() -> None:
    """Drop the cached serializer — for tests that swap settings between cases."""
    global _serializer, _secret_is_ephemeral
    _serializer, _secret_is_ephemeral = None, False


def secret_is_ephemeral() -> bool:
    """True when running on a generated SESSION_SECRET (see _get_serializer)."""
    _get_serializer()
    return _secret_is_ephemeral


def issue(user: AuthUser) -> str:
    """Serialize a verified user into the signed cookie value."""
    return _get_serializer().dumps({
        "u": user.username,
        "n": user.display_name,
        "e": user.email,
        "g": list(user.groups),
    })


def load(token: str | None) -> AuthUser | None:
    """
    Verify a cookie value and return the user it names, or None if it's
    missing, expired, tampered with, or signed by a previous SESSION_SECRET.
    Never raises — every failure means "not signed in".
    """
    if not token:
        return None
    max_age = max(1, get_settings().session_ttl_hours) * 3600
    try:
        data = _get_serializer().loads(token, max_age=max_age)
    except SignatureExpired:
        return None
    except BadSignature:
        log.warning("auth.session_bad_signature")
        return None
    except Exception as exc:  # malformed payload — treat as signed out
        log.warning("auth.session_unreadable", error=str(exc))
        return None
    if not isinstance(data, dict) or not data.get("u"):
        return None
    return AuthUser(
        username=str(data.get("u", "")),
        display_name=str(data.get("n", "") or ""),
        email=str(data.get("e", "") or ""),
        groups=tuple(str(g) for g in (data.get("g") or [])),
    )
