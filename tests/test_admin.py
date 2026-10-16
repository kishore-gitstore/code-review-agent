"""Admin surface: configuration page, service switch, and the MR-note delete guards."""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import envconfig, server
from app.auth import AuthUser
from app.gitlab.client import GitLabClient, PROGRESS_NOTE_MARKER
from app.service_state import service
from app.settings import get_settings
from tests.conftest import TEST_USER, sign_in

ADMIN = AuthUser(username="boss", display_name="Boss")


@pytest.fixture(autouse=True)
def _admins(monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_users", "Boss")
    monkeypatch.setattr(get_settings(), "admin_group", "")
    service.set(True, "")
    yield
    service.set(True, "")


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text(
        "# header\r\nLLM_PROVIDER=ollama\r\nOTHER_KEY=keep-me\r\nANTHROPIC_API_KEY=sk-secret\r\n"
        "GITLAB_INSTANCES='{\r\n  \"a\": {\"url\": \"https://g\", \"token\": \"glpat-real\"}\r\n}'\r\n"
        "AFTER=1\r\n", encoding="utf-8", newline="")
    monkeypatch.setattr(envconfig, "ENV_PATH", p)
    return p


def admin_client():
    server._reviews.clear(); server._tasks.clear()
    return sign_in(TestClient(server.create_app()), ADMIN)


# ── envconfig ────────────────────────────────────────────────────────────────

def test_secrets_are_masked(env_file):
    vals = envconfig.masked_values(env_file)
    assert vals["ANTHROPIC_API_KEY"] == envconfig.SECRET_MASK
    assert "glpat-real" not in vals["GITLAB_INSTANCES"]
    assert vals["LLM_PROVIDER"] == "ollama"


def test_edit_preserves_everything_else(env_file):
    changed = envconfig.apply_changes({"LLM_PROVIDER": "claude",
                                       "ANTHROPIC_API_KEY": envconfig.SECRET_MASK}, env_file)
    assert changed == ["LLM_PROVIDER"]
    text = env_file.read_bytes().decode("utf-8")
    assert "LLM_PROVIDER=claude" in text and "OTHER_KEY=keep-me" in text
    assert "ANTHROPIC_API_KEY=sk-secret" in text and "# header" in text and "AFTER=1" in text
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")


def test_masked_gitlab_token_is_restored_from_saved_value(env_file):
    masked = json.loads(envconfig.masked_values(env_file)["GITLAB_INSTANCES"])
    masked["a"]["url"] = "https://new"
    envconfig.apply_changes({"GITLAB_INSTANCES": json.dumps(masked)}, env_file)
    saved = envconfig.read_values(env_file)["GITLAB_INSTANCES"]
    assert json.loads(saved)["a"] == {"url": "https://new", "token": "glpat-real"}
    assert "AFTER=1" in env_file.read_text(encoding="utf-8")


def test_new_key_is_appended_and_unknown_key_rejected(env_file):
    envconfig.apply_changes({"MAX_DIFF_LINES": "500"}, env_file)
    assert "MAX_DIFF_LINES=500" in env_file.read_text(encoding="utf-8")
    with pytest.raises(envconfig.ConfigError):
        envconfig.apply_changes({"PATH": "/evil"}, env_file)
    with pytest.raises(envconfig.ConfigError):
        envconfig.apply_changes({"MAX_DIFF_LINES": "lots"}, env_file)


def test_values_needing_quotes_round_trip(env_file):
    f = "(&(objectClass=user)(sAMAccountName={username}))"
    envconfig.apply_changes({"LDAP_USER_FILTER": f}, env_file)
    assert envconfig.read_values(env_file)["LDAP_USER_FILTER"] == f


# ── access control ───────────────────────────────────────────────────────────

def test_non_admin_is_refused(env_file):
    c = sign_in(TestClient(server.create_app()), TEST_USER)
    assert c.get("/api/config").status_code == 403
    assert c.put("/api/config", json={"values": {}}).status_code == 403
    assert c.post("/api/service/stop").status_code == 403
    assert c.get("/config", follow_redirects=False).status_code == 303
    assert c.get("/api/me").json()["is_admin"] is False
    assert c.get("/api/service").json()["can_control"] is False


def test_no_admins_configured_means_nobody_is_admin(env_file, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_users", "")
    assert admin_client().get("/api/config").status_code == 403


def test_anonymous_is_sent_to_login(anonymous_client):
    assert anonymous_client.get("/config", follow_redirects=False).headers["location"].startswith("/login")
    assert anonymous_client.get("/", follow_redirects=False).status_code == 303
    assert anonymous_client.get("/api/config").status_code == 401


def test_config_get_never_returns_secrets(env_file):
    body = admin_client().get("/api/config").text
    assert "sk-secret" not in body and "glpat-real" not in body


def test_config_put_saves_to_disk(env_file, monkeypatch):
    monkeypatch.setattr(server, "_apply_config", AsyncMock(return_value={
        "applied": True, "restart_required": [], "message": "ok"}))
    r = admin_client().put("/api/config", json={"values": {"LLM_PROVIDER": "openai"}})
    assert r.status_code == 200 and r.json()["changed"] == ["LLM_PROVIDER"]
    assert "LLM_PROVIDER=openai" in env_file.read_text(encoding="utf-8")
    bad = admin_client().put("/api/config", json={"values": {"MAX_DIFF_LINES": "x"}})
    assert bad.status_code == 422


# ── service switch ───────────────────────────────────────────────────────────

def test_stopped_service_refuses_manual_trigger_and_ignores_webhooks():
    c = admin_client()
    assert c.post("/api/service/stop").json()["state"] == "stopped"
    r = c.post("/review", json={"project_id": "g/r", "mr_iid": 1})
    assert r.status_code == 503
    assert c.post("/api/service/start").json()["state"] == "running"


# ── MR deletion guards ───────────────────────────────────────────────────────

def _client():
    g = GitLabClient.__new__(GitLabClient)
    g.base_url = "https://gitlab.example.com/api/v4"
    g.headers = {}
    return g


@pytest.mark.asyncio
@pytest.mark.parametrize("mr,note", [(7, None), (7, 0), (7, "42/../.."), (None, 5), (7, True), (0, 5), ("", 5)])
async def test_delete_mr_note_refuses_ids_that_could_reach_the_mr(mr, note):
    with pytest.raises(ValueError):
        await _client().delete_mr_note("g/r", mr, note)


@pytest.mark.asyncio
async def test_stale_cleanup_leaves_other_peoples_notes_alone():
    g = _client()
    g._own_name = "review-bot"
    g.list_mr_notes = AsyncMock(return_value=[
        {"id": 1, "body": PROGRESS_NOTE_MARKER, "author": {"username": "review-bot"}},
        {"id": 2, "body": PROGRESS_NOTE_MARKER + " quoted", "author": {"username": "alice"}},
        {"id": 3, "body": PROGRESS_NOTE_MARKER, "author": {"username": "review-bot"}, "system": True},
    ])
    g.delete_mr_note = AsyncMock()
    assert await g.delete_stale_progress_notes(1, 7) == 1
    assert [c.args[2] for c in g.delete_mr_note.await_args_list] == [1]


@pytest.mark.asyncio
async def test_stale_cleanup_deletes_nothing_if_identity_unknown():
    g = _client()
    g._own_name = None
    g._own_username = AsyncMock(side_effect=Exception("401"))
    g.list_mr_notes = AsyncMock(return_value=[
        {"id": 1, "body": PROGRESS_NOTE_MARKER, "author": {"username": "review-bot"}}])
    g.delete_mr_note = AsyncMock()
    assert await g.delete_stale_progress_notes(1, 7) == 0
    g.delete_mr_note.assert_not_called()
