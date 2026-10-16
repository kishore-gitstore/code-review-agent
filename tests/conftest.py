"""
tests/conftest.py — shared fixtures.

`signed_in_client` exists because every reporting endpoint now sits behind an
Active Directory session (see app/auth/). A test that wants to assert
something about *reports* shouldn't have to re-derive how logging in works,
and a test that forgets to log in should fail loudly on its own assertion
rather than on a confusing 401 — so the cookie is minted directly from the
session serializer instead of by posting credentials at a real domain
controller.
"""

import pytest
from fastapi.testclient import TestClient

from app import server
from app.auth import AuthUser
from app.auth import session as session_mod
from app.settings import get_settings

TEST_USER = AuthUser(
    username="testuser",
    display_name="Test User",
    email="testuser@corp.local",
    groups=("CRA-Dashboard-Users",),
)


@pytest.fixture(autouse=True)
def _pinned_auth_settings(monkeypatch):
    """Pin the auth settings the suite assumes, independent of the local .env.

    Settings are read from whatever .env sits in the working directory, so
    without this the whole suite's meaning depends on the developer's own
    box: a dev .env carrying AUTH_ENABLED=false turns every "this endpoint
    refuses an anonymous caller" test green while asserting nothing. Pinned
    to the shipped default (on) — the one test that covers the off switch
    monkeypatches it back itself.

    SESSION_SECRET is pinned for a different reason: the serializer otherwise
    generates a fresh random secret per process, which is fine at runtime but
    makes "the gate rejected me" indistinguishable from "the two halves of
    this test disagreed about the key".
    """
    monkeypatch.setattr(get_settings(), "auth_enabled", True)
    monkeypatch.setattr(get_settings(), "dashboard_api_token", "")
    monkeypatch.setattr(get_settings(), "session_secret", "test-session-secret-not-a-real-one")
    monkeypatch.setattr(get_settings(), "session_ttl_hours", 8)
    session_mod.reset_serializer()
    yield
    session_mod.reset_serializer()


def sign_in(client: TestClient, user: AuthUser = TEST_USER) -> TestClient:
    """Attach a valid session cookie to an existing TestClient."""
    client.cookies.set(get_settings().session_cookie_name, session_mod.issue(user))
    return client


@pytest.fixture
def signed_in_client():
    """TestClient carrying a valid AD session, with a clean review tracker."""
    server._reviews.clear()
    server._tasks.clear()
    yield sign_in(TestClient(server.create_app()))
    server._reviews.clear()
    server._tasks.clear()


@pytest.fixture
def anonymous_client():
    """TestClient with no session — for asserting the gate actually closes."""
    server._reviews.clear()
    server._tasks.clear()
    yield TestClient(server.create_app())
    server._reviews.clear()
    server._tasks.clear()
