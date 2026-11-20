"""
app/auth/ — Active Directory (LDAP) authentication for the dashboard.

The review pipeline itself (webhooks, the CLI) is unauthenticated by design —
GitLab authenticates its own webhooks with a shared secret. What this package
guards is the *reporting* surface: the dashboard page, the report APIs, and
the control endpoints that can start or cancel a review.

Three pieces, deliberately separate so each can be tested on its own:

  ldap_auth.py  — binds to AD as the user to verify the password, then reads
                  back displayName/mail/memberOf and applies the optional
                  group restriction.
  session.py    — turns a verified user into a signed, expiring cookie value
                  and back. No server-side session store, so restarting the
                  service doesn't log everyone out (as long as SESSION_SECRET
                  is set in .env rather than generated per-process).
  deps.py       — the FastAPI dependencies the routes actually use.
"""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuthUser:
    """A verified dashboard user. `groups` holds CNs, not full DNs."""
    username:     str
    display_name: str = ""
    email:        str = ""
    groups:       tuple[str, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        """What to show in the UI and write onto an MR note."""
        return self.display_name or self.username

    def as_dict(self) -> dict:
        return {
            "username":     self.username,
            "display_name": self.display_name,
            "email":        self.email,
            "groups":       list(self.groups),
        }


class AuthError(Exception):
    """Base for every authentication failure. `http_status` is what the route
    returns, so the reason a login failed survives all the way to the UI
    without each route having to re-classify it."""
    http_status = 401

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class InvalidCredentials(AuthError):
    """Wrong username/password, or the account is disabled/locked."""
    http_status = 401


class NotAuthorized(AuthError):
    """Credentials are valid, but the account isn't in LDAP_REQUIRED_GROUP."""
    http_status = 403


class AuthUnavailable(AuthError):
    """The directory couldn't be reached — a server-side fault, not the
    user's. Distinguished from InvalidCredentials so an AD outage doesn't
    tell every user their password is wrong."""
    http_status = 503


class AuthConfigError(AuthError):
    """No sign-in method (LOCAL_USERS or LDAP_SERVER) is configured, or the LDAP settings are incomplete."""
    http_status = 500


__all__ = [
    "AuthUser", "AuthError", "InvalidCredentials", "NotAuthorized",
    "AuthUnavailable", "AuthConfigError",
]
