"""
tests/test_auth.py — only an authenticated Active Directory user may read
review reports.

The reports are the sensitive artefact here: every finding quotes real source
lines out of private repositories, with file paths, author names and the
security issues found in them. Before this the dashboard and its APIs were
open to anyone who could reach the port.

What's guarded below:

  * every reporting/control endpoint refuses an anonymous caller;
  * the dashboard *page* bounces to /login rather than 401ing, because a
    human typed that URL — but it carries no data, so bouncing is safe;
  * the GitLab webhook and /health stay open, because neither caller can hold
    a session and the webhook has a shared secret of its own;
  * a session cookie can't be forged or replayed past its TTL;
  * an empty password never reaches AD, which would accept it as an
    anonymous bind and report success.
"""

import time
import pytest
from unittest.mock import AsyncMock, patch

from app.auth import (AuthUser, AuthUnavailable, InvalidCredentials,
                      NotAuthorized)
from app.auth import deps, session as session_mod
from app.auth.ldap_auth import _bind_identity, _cn_of, _sam_name, _split_host_port, authenticate
from app.settings import get_settings
from tests.conftest import TEST_USER

# Every endpoint that serves review data or acts on a review. If a new one is
# added without a require_user dependency, it belongs here and this list is
# where that gets noticed.
GATED = [
    ("GET",  "/api/reports"),
    ("GET",  "/api/reports/review_local_20260101_000000.json"),
    ("GET",  "/review/active"),
    ("GET",  "/review/status/primary/group%2Frepo/7"),
    ("POST", "/review"),
    ("POST", "/review/primary/group%2Frepo/7/cancel"),
    ("POST", "/review/primary/group%2Frepo/7/clear-stale-note"),
    ("GET",  "/api/me"),
]


@pytest.mark.parametrize("method, path", GATED)
def test_every_reporting_endpoint_refuses_an_anonymous_caller(anonymous_client, method, path):
    res = anonymous_client.request(method, path, json={})

    assert res.status_code == 401, (
        f"{method} {path} answered {res.status_code} without a session — review "
        f"findings quote private source code and must not be readable anonymously."
    )


@pytest.mark.parametrize("method, path", GATED)
def test_a_signed_in_user_is_not_refused_by_the_gate(signed_in_client, method, path):
    res = signed_in_client.request(method, path, json={})

    # 404/409/400 are fine — they mean the handler ran. 401/403 would mean the
    # gate rejected a valid session, which is the failure this guards.
    assert res.status_code not in (401, 403), (
        f"{method} {path} rejected a valid session with {res.status_code}"
    )


def test_the_dashboard_page_redirects_to_login_instead_of_401ing(anonymous_client):
    res = anonymous_client.get("/dashboard", follow_redirects=False)

    assert res.status_code == 303
    assert res.headers["location"] == "/login?next=/dashboard"


def test_the_dashboard_page_is_served_to_a_signed_in_user(signed_in_client):
    res = signed_in_client.get("/dashboard")

    assert res.status_code == 200
    assert "Code Review Agent" in res.text


def test_the_login_page_is_reachable_without_a_session(anonymous_client):
    res = anonymous_client.get("/login")

    assert res.status_code == 200
    assert "Sign in" in res.text


def test_health_stays_open_because_probes_cannot_sign_in(anonymous_client):
    with patch("app.server.get_llm_client") as get_client:
        get_client.return_value.health_check = AsyncMock(
            return_value={"status": "ok", "provider": "ollama", "model": "qwen"}
        )
        res = anonymous_client.get("/health")

    assert res.status_code == 200


def test_the_gitlab_webhook_stays_open_because_gitlab_cannot_sign_in(anonymous_client):
    """It authenticates with the per-instance webhook secret instead — proven
    here by the 401 coming from *that* check, not from the session gate."""
    with patch("app.server.get_gitlab_client") as get_gitlab:
        get_gitlab.return_value.verify_webhook.return_value = False
        res = anonymous_client.post(
            "/webhook/gitlab/primary",
            json={"object_attributes": {}},
            headers={"X-Gitlab-Token": "wrong", "X-Gitlab-Event": "Merge Request Hook"},
        )

    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid webhook token"


# ── session cookies ─────────────────────────────────────────────────────────

def test_a_tampered_cookie_is_not_a_session(anonymous_client):
    forged = session_mod.issue(AuthUser(username="attacker"))[:-4] + "AAAA"
    anonymous_client.cookies.set(get_settings().session_cookie_name, forged)

    assert anonymous_client.get("/api/reports").status_code == 401


def test_a_cookie_signed_with_another_secret_is_not_a_session(anonymous_client, monkeypatch):
    """What a rotated SESSION_SECRET must do: invalidate, not silently accept."""
    stolen = session_mod.issue(TEST_USER)
    monkeypatch.setattr(get_settings(), "session_secret", "a-completely-different-secret")
    session_mod.reset_serializer()
    anonymous_client.cookies.set(get_settings().session_cookie_name, stolen)

    assert anonymous_client.get("/api/reports").status_code == 401


def test_a_session_expires(anonymous_client, monkeypatch):
    token = session_mod.issue(TEST_USER)
    # itsdangerous stamps the signing time, so age is measured against the
    # clock — not against anything the holder of the cookie controls.
    monkeypatch.setattr(get_settings(), "session_ttl_hours", 1)
    monkeypatch.setattr(time, "time", lambda: time.time() + 2 * 3600)
    session_mod.reset_serializer()

    assert session_mod.load(token) is None


def test_logging_in_sets_a_cookie_that_is_not_reachable_from_javascript(anonymous_client):
    """httponly, so an XSS on the dashboard can't lift a colleague's session."""
    with patch("app.server.authenticate", AsyncMock(return_value=TEST_USER)):
        res = anonymous_client.post("/api/login", json={"username": "u", "password": "p"})

    assert res.status_code == 200
    cookie_header = res.headers["set-cookie"]
    assert "httponly" in cookie_header.lower()
    assert "samesite=lax" in cookie_header.lower()
    # And the session works for a subsequent gated call.
    assert anonymous_client.get("/review/active").status_code == 200


def test_logging_out_clears_the_session(anonymous_client):
    """Signs in through the real endpoint rather than injecting a cookie: the
    thing under test is the Set-Cookie that *removes* the session, and that
    only cancels out a cookie the same response flow installed."""
    with patch("app.server.authenticate", AsyncMock(return_value=TEST_USER)):
        anonymous_client.post("/api/login", json={"username": "u", "password": "p"})
    assert anonymous_client.get("/review/active").status_code == 200

    anonymous_client.post("/api/logout")

    assert anonymous_client.get("/review/active").status_code == 401


def test_api_me_reports_the_signed_in_identity(signed_in_client):
    body = signed_in_client.get("/api/me").json()

    assert body["user"]["username"] == "testuser"
    assert body["user"]["display_name"] == "Test User"
    assert body["auth_enabled"] is True


# ── how a failed login is reported ──────────────────────────────────────────

@pytest.mark.parametrize("error, expected_status", [
    (InvalidCredentials("Incorrect username or password."), 401),
    (NotAuthorized("Not in the group."), 403),
    (AuthUnavailable("Directory unreachable."), 503),
])
def test_a_directory_outage_is_not_reported_as_a_bad_password(anonymous_client, error, expected_status):
    """Each failure keeps its own status. Collapsing them to 401 would send a
    whole team resetting passwords during an AD outage."""
    with patch("app.server.authenticate", AsyncMock(side_effect=error)):
        res = anonymous_client.post("/api/login", json={"username": "u", "password": "p"})

    assert res.status_code == expected_status
    assert res.json()["detail"] == error.message


@pytest.mark.asyncio
@pytest.mark.parametrize("username, password", [("alice", ""), ("", "secret"), ("alice", None)])
async def test_an_empty_password_never_reaches_the_directory(username, password):
    """AD treats a bind with an empty password as an *unauthenticated* bind and
    returns success — so a blank password box would log anyone in as anyone."""
    with patch("app.auth.ldap_auth._authenticate_sync") as bind:
        with pytest.raises(InvalidCredentials):
            await authenticate(username, password)

    bind.assert_not_called()


# ── API token, for callers that have no browser ─────────────────────────────

def test_an_api_token_is_accepted_when_configured(anonymous_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "dashboard_api_token", "s3cret-ci-token")

    res = anonymous_client.get("/review/active", headers={"X-API-Token": "s3cret-ci-token"})

    assert res.status_code == 200


def test_a_wrong_api_token_is_still_anonymous(anonymous_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "dashboard_api_token", "s3cret-ci-token")

    res = anonymous_client.get("/review/active", headers={"X-API-Token": "guess"})

    assert res.status_code == 401


def test_an_unset_api_token_cannot_be_matched_by_an_empty_header(anonymous_client, monkeypatch):
    """The dangerous default: an empty configured token compared against an
    empty header would let every anonymous request through."""
    monkeypatch.setattr(get_settings(), "dashboard_api_token", "")

    res = anonymous_client.get("/review/active", headers={"X-API-Token": ""})

    assert res.status_code == 401


# ── the escape hatch ────────────────────────────────────────────────────────

def test_auth_can_be_turned_off_for_a_dev_box(anonymous_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_enabled", False)

    assert anonymous_client.get("/review/active").status_code == 200
    assert anonymous_client.get("/dashboard", follow_redirects=False).status_code == 200


# ── open-redirect guard on ?next= ───────────────────────────────────────────

@pytest.mark.parametrize("hostile", [
    "//evil.example.com",           # protocol-relative — a real redirect off-site
    "https://evil.example.com",
    "/\\evil.example.com",          # some browsers normalise the backslash to /
    "",
    None,
])
def test_next_cannot_be_pointed_off_site(hostile):
    assert deps.safe_next(hostile) == "/"


def test_next_still_works_for_a_same_origin_path():
    assert deps.safe_next("/dashboard?range=7#report=x") == "/dashboard?range=7#report=x"


# ── LDAP helpers ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("dc01.corp.local",              ("dc01.corp.local", 389, None)),
    ("dc01.corp.local:3268",         ("dc01.corp.local", 3268, None)),
    ("ldaps://dc01.corp.local",      ("dc01.corp.local", 389, True)),
    ("ldap://dc01.corp.local:389/",  ("dc01.corp.local", 389, False)),
])
def test_ldap_server_accepts_a_host_a_host_port_or_a_url(raw, expected):
    assert _split_host_port(raw, 389) == expected


def test_an_ldaps_url_is_not_downgraded_to_plaintext():
    """Writing ldaps:// and getting a cleartext bind would be a security
    surprise, so the scheme overrides LDAP_USE_SSL rather than the reverse."""
    _, _, ssl_override = _split_host_port("ldaps://dc01.corp.local", 389)

    assert ssl_override is True


def test_a_bare_login_name_is_qualified_with_the_domain(monkeypatch):
    monkeypatch.setattr(get_settings(), "ldap_bind_style", "upn")
    monkeypatch.setattr(get_settings(), "ldap_domain", "corp.local")

    principal, _ = _bind_identity("alice")

    assert principal == "alice@corp.local"


@pytest.mark.parametrize("typed", ["alice@corp.local", "CORP\\alice"])
def test_an_already_qualified_login_name_is_left_alone(monkeypatch, typed):
    """Re-qualifying would produce "CORP\\alice@corp.local" and fail a bind
    that would otherwise have worked."""
    monkeypatch.setattr(get_settings(), "ldap_domain", "corp.local")

    principal, _ = _bind_identity(typed)

    assert principal == typed


@pytest.mark.parametrize("typed", ["alice", "alice@corp.local", "CORP\\alice"])
def test_the_sam_account_name_is_recovered_from_any_login_form(typed):
    assert _sam_name(typed) == "alice"


def test_a_group_dn_reduces_to_its_common_name():
    assert _cn_of("CN=CRA-Dashboard-Users,OU=Groups,DC=corp,DC=local") == "CRA-Dashboard-Users"
