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
    box — e.g. a dev .env carrying real LOCAL_USERS or an API token would
    change who the gate lets in.

    SESSION_SECRET is pinned for a different reason: the serializer otherwise
    generates a fresh random secret per process, which is fine at runtime but
    makes "the gate rejected me" indistinguishable from "the two halves of
    this test disagreed about the key".
    """
    monkeypatch.setattr(get_settings(), "dashboard_api_token", "")
    monkeypatch.setattr(get_settings(), "local_users", "")
    monkeypatch.setattr(get_settings(), "ldap_server", "")
    monkeypatch.setattr(get_settings(), "admin_users", "")
    monkeypatch.setattr(get_settings(), "admin_group", "")
    monkeypatch.setattr(get_settings(), "session_secret", "test-session-secret-not-a-real-one")
    monkeypatch.setattr(get_settings(), "session_ttl_hours", 8)
    session_mod.reset_serializer()
    yield
    session_mod.reset_serializer()


def sign_in(client: TestClient, user: AuthUser = TEST_USER) -> TestClient:
    """Attach a valid session cookie to an existing TestClient."""
    client.cookies.set(get_settings().session_cookie_name, session_mod.issue(user))
    return client


@pytest.fixture(autouse=True)
def _no_real_model_loading(monkeypatch):
    """_run_review loads the LLM before its agents run and unloads it after.
    Against the real client that's an HTTP call to whatever OLLAMA_BASE_URL
    the developer's .env names; every test gets a stand-in instead, which a
    test can inspect as `server.get_llm_client()`."""
    from unittest.mock import AsyncMock, MagicMock
    llm = MagicMock()
    llm.load = AsyncMock()
    llm.unload = AsyncMock()
    monkeypatch.setattr(server, "get_llm_client", lambda: llm)
    monkeypatch.setattr(server, "_model_leases", 0)
    yield llm


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
