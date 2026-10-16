"""
app/auth/ldap_auth.py — verify a username/password against Active Directory.

Authentication is a *bind* as the user: if AD accepts the credentials the
password is correct, and no service account is needed to check it. A second,
read-only search on the same connection then pulls displayName / mail /
memberOf so the UI can show a real name and the optional group restriction
can be applied.

ldap3 is synchronous, so every call here is wrapped in asyncio.to_thread by
authenticate() — a bind against a busy DC can take seconds, and blocking the
event loop would stall every in-flight review.
"""

from __future__ import annotations
import asyncio
import structlog
from ldap3 import Server, Connection, ALL, NTLM, SIMPLE
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from app.settings import get_settings
from . import (AuthUser, AuthConfigError, AuthUnavailable, InvalidCredentials,
               NotAuthorized)

log = structlog.get_logger(__name__)

# OID for AD's LDAP_MATCHING_RULE_IN_CHAIN — makes a memberOf filter follow
# nested groups, so a user in "Dev Team" which is itself a member of
# "CRA-Dashboard-Users" still matches. Only usable when LDAP_REQUIRED_GROUP is
# given as a full DN; a bare CN can't be resolved without a directory lookup.
_IN_CHAIN = "1.2.840.113556.1.4.1941"

_ATTRS = ["displayName", "mail", "sAMAccountName", "memberOf"]


def _split_host_port(raw: str, default_port: int) -> tuple[str, int, bool | None]:
    """
    Parse LDAP_SERVER into (host, port, ssl_override).

    Accepts a bare host ("dc01.corp.local"), a host:port, or a URL
    ("ldaps://dc01.corp.local:636"). A URL scheme wins over LDAP_USE_SSL —
    writing ldaps:// and getting a plaintext 389 connection would be a
    security surprise, not a convenience.
    """
    host, ssl_override = raw.strip().rstrip("/"), None
    for scheme, is_ssl in (("ldaps://", True), ("ldap://", False)):
        if host.lower().startswith(scheme):
            host, ssl_override = host[len(scheme):], is_ssl
            break
    port = default_port
    # Only treat a trailing ":1234" as a port — an IPv6 literal or a stray
    # colon must not be mistaken for one.
    head, sep, tail = host.rpartition(":")
    if sep and head and tail.isdigit():
        host, port = head, int(tail)
    return host, port, ssl_override


def _build_server() -> Server:
    cfg = get_settings()
    if not cfg.ldap_server:
        raise AuthConfigError(
            "LDAP_SERVER is not set — set it in .env, or set AUTH_ENABLED=false "
            "to run the dashboard without a login."
        )
    use_ssl = cfg.ldap_use_ssl
    host, port, ssl_override = _split_host_port(
        cfg.ldap_server, cfg.ldap_port or (636 if use_ssl else 389)
    )
    if ssl_override is not None:
        use_ssl = ssl_override
        if not cfg.ldap_port:
            port = 636 if use_ssl else 389
    return Server(host, port=port, use_ssl=use_ssl, get_info=ALL,
                  connect_timeout=cfg.ldap_timeout)


def _bind_identity(username: str) -> tuple[str, str]:
    r"""
    (bind_principal, ldap3_auth_mechanism) for the login name as typed.

    A name the user already qualified ("me@corp.local", "CORP\me") is passed
    through as-is — re-qualifying it would produce "CORP\me@corp.local" and
    fail a bind that would otherwise have worked.
    """
    cfg = get_settings()
    if "@" in username or "\\" in username:
        return username, NTLM if "\\" in username else SIMPLE
    if cfg.ldap_bind_style == "ntlm":
        netbios = cfg.ldap_netbios_domain or cfg.ldap_domain.split(".")[0]
        if not netbios:
            raise AuthConfigError(
                "LDAP_BIND_STYLE=ntlm needs LDAP_NETBIOS_DOMAIN (or LDAP_DOMAIN) in .env"
            )
        return f"{netbios}\\{username}", NTLM
    if not cfg.ldap_domain:
        raise AuthConfigError(
            "LDAP_DOMAIN is not set — needed to turn a bare login name into "
            "user@domain for the AD bind (or set LDAP_BIND_STYLE=ntlm)."
        )
    return f"{username}@{cfg.ldap_domain}", SIMPLE


def _sam_name(username: str) -> str:
    """The bare sAMAccountName from whatever form the user typed."""
    return username.split("@")[0].split("\\")[-1]


def _cn_of(dn: str) -> str:
    """CN=CRA Users,OU=Groups,DC=corp,DC=local -> CRA Users."""
    first = dn.split(",")[0]
    return first[3:] if first[:3].upper() == "CN=" else first


def _check_group(conn: Connection, user_dn: str, groups: tuple[str, ...]) -> None:
    """
    Enforce LDAP_REQUIRED_GROUP. No-op when it's unset (any account that can
    bind may see the dashboard).
    """
    required = get_settings().ldap_required_group.strip()
    if not required:
        return

    # Direct membership first — one string compare, no extra round trip.
    wanted = required.lower()
    if any(g.lower() == wanted or _cn_of(g).lower() == wanted for g in groups):
        return

    # Nested membership, only resolvable when the group was given as a DN.
    if "=" in required and user_dn:
        try:
            conn.search(
                search_base=user_dn,
                search_filter=f"(memberOf:{_IN_CHAIN}:={escape_filter_chars(required)})",
                attributes=["sAMAccountName"],
            )
            if conn.entries:
                return
        except LDAPException as exc:
            log.warning("auth.nested_group_check_failed", error=str(exc))

    raise NotAuthorized(
        f"Your account is not a member of {required} — ask your administrator "
        "for access to the code review dashboard."
    )


def _authenticate_sync(username: str, password: str) -> AuthUser:
    cfg = get_settings()
    server = _build_server()
    principal, mechanism = _bind_identity(username)

    conn = Connection(
        server, user=principal, password=password, authentication=mechanism,
        receive_timeout=cfg.ldap_timeout, raise_exceptions=False, read_only=True,
    )
    try:
        bound = conn.bind()
    except LDAPException as exc:
        # Socket/TLS/DNS level — the directory is unreachable, which is our
        # problem, not the user's. Telling them "wrong password" here would
        # send the whole team chasing their own credentials during an outage.
        log.warning("auth.ldap_unreachable", server=cfg.ldap_server, error=str(exc))
        raise AuthUnavailable(
            "Could not reach the directory server. Try again shortly, or "
            "contact the service owner if it persists."
        ) from exc

    if not bound:
        log.info("auth.bind_rejected", user=_sam_name(username),
                 result=(conn.result or {}).get("description"))
        raise InvalidCredentials("Incorrect username or password.")

    try:
        sam = _sam_name(username)
        user_dn, display_name, email, groups = conn.user or "", "", "", ()
        if cfg.ldap_base_dn:
            try:
                conn.search(
                    search_base=cfg.ldap_base_dn,
                    search_filter=cfg.ldap_user_filter.format(username=escape_filter_chars(sam)),
                    attributes=_ATTRS,
                )
                if conn.entries:
                    e = conn.entries[0]
                    user_dn      = str(e.entry_dn)
                    display_name = str(e.displayName) if "displayName" in e else ""
                    email        = str(e.mail) if "mail" in e else ""
                    groups       = tuple(str(g) for g in (e.memberOf if "memberOf" in e else []))
                else:
                    log.warning("auth.user_lookup_empty", user=sam, base_dn=cfg.ldap_base_dn)
            except LDAPException as exc:
                # The password is already proven by the bind above. A failed
                # profile lookup costs us the display name and group data, so
                # it must not silently downgrade a required-group check into
                # "no groups found, therefore denied" — hence the re-raise
                # when a restriction is actually configured.
                log.warning("auth.user_lookup_failed", user=sam, error=str(exc))
                if cfg.ldap_required_group.strip():
                    raise AuthUnavailable(
                        "Signed in, but your group membership could not be verified. "
                        "Try again shortly."
                    ) from exc

        _check_group(conn, user_dn, groups)
        user = AuthUser(
            username=sam,
            display_name=display_name,
            email=email,
            groups=tuple(_cn_of(g) for g in groups),
        )
        log.info("auth.login_ok", user=user.username, groups=len(user.groups))
        return user
    finally:
        try:
            conn.unbind()
        except LDAPException:
            pass


async def authenticate(username: str, password: str) -> AuthUser:
    """
    Verify `username`/`password` against AD and return the signed-in user.

    Raises InvalidCredentials / NotAuthorized / AuthUnavailable /
    AuthConfigError — see app/auth/__init__.py for which HTTP status each
    maps to.
    """
    username = (username or "").strip()
    if not username or not password:
        # AD accepts a bind with an empty password as an *unauthenticated*
        # bind and reports success, which would let anyone in by leaving the
        # password box blank. Reject it before it ever reaches the server.
        raise InvalidCredentials("Username and password are both required.")
    return await asyncio.to_thread(_authenticate_sync, username, password)
