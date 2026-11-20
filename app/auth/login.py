"""
app/auth/login.py — one entry point for "is this username/password valid?".

Sign-in is always required; there is no switch to turn it off. Two sources:

  * local accounts (LOCAL_USERS in .env) — checked first, and a local name
    is answered by its local password only; it never falls through to AD,
    so a local "admin" can't be impersonated by an AD account of that name.
  * Active Directory (LDAP_SERVER) — every other name.
"""

from __future__ import annotations

from app.settings import get_settings
from . import AuthConfigError, AuthUser, InvalidCredentials
from . import ldap_auth, local_users


async def authenticate(username: str, password: str) -> AuthUser:
    username = (username or "").strip()
    if not username or not password:
        # AD accepts a bind with an empty password as an *unauthenticated*
        # bind and reports success; reject it before any backend sees it.
        raise InvalidCredentials("Username and password are both required.")

    if local_users.is_local(username):
        return await local_users.authenticate_local(username, password)

    if get_settings().ldap_server.strip():
        return await ldap_auth.authenticate(username, password)

    if local_users.configured():
        raise InvalidCredentials("Incorrect username or password.")
    raise AuthConfigError(
        "No sign-in method is configured. On the server, run "
        "`python -m app.auth.local_users add admin --admin`, or set LDAP_SERVER "
        "and LDAP_DOMAIN for Active Directory."
    )
