"""Local account management: the shared functions, the CLI, and the admin-only
Users page API (register, duplicate warning, password reset, admin toggle)."""

import pytest
from fastapi.testclient import TestClient

from app import envconfig, server
from app.auth import AuthUser, local_users
from app.settings import get_settings
from tests.conftest import TEST_USER, sign_in

ADMIN = AuthUser(username="boss", display_name="Boss")


@pytest.fixture(autouse=True)
def fast_hashes(monkeypatch):
    """600k PBKDF2 rounds per hash would make this file take seconds."""
    real = local_users.hash_password
    monkeypatch.setattr(local_users, "hash_password", lambda pw, **kw: real(pw, iterations=1000))


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("# header\r\nOTHER_KEY=keep-me\r\nADMIN_USERS=boss\r\n", encoding="utf-8", newline="")
    monkeypatch.setattr(envconfig, "ENV_PATH", p)
    monkeypatch.setattr(get_settings(), "admin_users", "boss")

    def fake_reload():
        # The real reload_settings() re-reads ./.env; point it at the temp file
        # instead, so a sign-in right after a change sees exactly what was saved.
        vals = envconfig.read_values(p)
        monkeypatch.setattr(get_settings(), "local_users", vals["LOCAL_USERS"])
        monkeypatch.setattr(get_settings(), "admin_users", vals["ADMIN_USERS"])
        return get_settings()

    monkeypatch.setattr(server, "reload_settings", fake_reload)
    return p


def admin_client():
    server._reviews.clear(); server._tasks.clear()
    return sign_in(TestClient(server.create_app()), ADMIN)


def login(username, password):
    return TestClient(server.create_app()).post(
        "/api/login", json={"username": username, "password": password})


# ── shared functions ─────────────────────────────────────────────────────────

def test_add_refuses_an_existing_user_and_keeps_their_password(env_file):
    local_users.add_user("alice", "first-pass-1")
    with pytest.raises(local_users.UserExists, match="already exists"):
        local_users.add_user("ALICE", "second-pass-2")
    stored = local_users.parse(envconfig.read_values(env_file)["LOCAL_USERS"])["alice"]
    assert local_users.verify_password("first-pass-1", stored)
    assert "OTHER_KEY=keep-me" in env_file.read_text(encoding="utf-8")


def test_reset_set_admin_and_remove(env_file):
    local_users.add_user("alice", "first-pass-1")
    local_users.reset_password("alice", "second-pass-2")
    stored = local_users.parse(envconfig.read_values(env_file)["LOCAL_USERS"])["alice"]
    assert local_users.verify_password("second-pass-2", stored)

    local_users.set_admin("alice", True)
    assert {"username": "alice", "is_admin": True} in local_users.list_users()
    local_users.set_admin("alice", False)
    assert {"username": "alice", "is_admin": False} in local_users.list_users()

    local_users.remove_user("alice")
    assert local_users.list_users() == []
    for fn, args in ((local_users.reset_password, ("ghost", "whatever-1")),
                     (local_users.set_admin, ("ghost", True)),
                     (local_users.remove_user, ("ghost",))):
        with pytest.raises(local_users.NoSuchUser):
            fn(*args)


def test_short_password_and_bad_name_are_refused(env_file):
    with pytest.raises(local_users.UserError, match="at least 8"):
        local_users.add_user("alice", "short")
    with pytest.raises(local_users.UserError, match="letters, digits"):
        local_users.add_user("bad name", "long-enough-1")


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_add_existing_user_says_so_without_asking_for_a_password(env_file, monkeypatch, capsys):
    local_users.add_user("alice", "first-pass-1")

    def no_prompt(*_a, **_k):
        raise AssertionError("must not prompt for a password")
    monkeypatch.setattr("getpass.getpass", no_prompt)

    assert local_users._main(["add", "alice"]) == 3
    out = capsys.readouterr().out
    assert "already registered" in out and "reset alice" in out


def test_cli_reset_changes_the_password(env_file, monkeypatch, capsys):
    local_users.add_user("alice", "first-pass-1")
    monkeypatch.setattr("getpass.getpass", lambda *_a, **_k: "second-pass-2")
    assert local_users._main(["reset", "alice"]) == 0
    stored = local_users.parse(envconfig.read_values(env_file)["LOCAL_USERS"])["alice"]
    assert local_users.verify_password("second-pass-2", stored)
    assert local_users._main(["reset", "ghost"]) == 1
    assert "add ghost" in capsys.readouterr().out


# ── Users page / API ─────────────────────────────────────────────────────────

def test_users_page_and_api_are_admin_only(env_file, anonymous_client):
    r = anonymous_client.get("/users", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")

    viewer = sign_in(TestClient(server.create_app()), TEST_USER)
    assert viewer.get("/users", follow_redirects=False).headers["location"] == "/dashboard"
    assert viewer.get("/api/users").status_code == 403
    assert viewer.post("/api/users", json={"username": "x", "password": "long-enough-1"}).status_code == 403
    assert viewer.post("/api/users/boss/password", json={"password": "long-enough-1"}).status_code == 403

    assert admin_client().get("/users").status_code == 200


def test_admin_registers_a_user_who_can_then_sign_in(env_file):
    c = admin_client()
    r = c.post("/api/users", json={"username": "Alice", "password": "alice-pass-1", "is_admin": False})
    assert r.status_code == 201 and r.json()["username"] == "alice"
    assert {"username": "alice", "is_admin": False} in r.json()["users"]

    ok = login("alice", "alice-pass-1")
    assert ok.status_code == 200 and ok.json()["is_admin"] is False

    dup = c.post("/api/users", json={"username": "alice", "password": "other-pass-2"})
    assert dup.status_code == 409 and "already exists" in dup.json()["detail"]
    assert login("alice", "alice-pass-1").status_code == 200   # duplicate didn't overwrite


def test_admin_resets_a_password(env_file):
    c = admin_client()
    c.post("/api/users", json={"username": "alice", "password": "alice-pass-1"})
    r = c.post("/api/users/alice/password", json={"password": "alice-pass-2"})
    assert r.status_code == 200
    assert login("alice", "alice-pass-1").status_code == 401
    assert login("alice", "alice-pass-2").status_code == 200

    assert c.post("/api/users/alice/password", json={"password": "short"}).status_code == 422
    assert c.post("/api/users/ghost/password", json={"password": "long-enough-1"}).status_code == 404


def test_new_user_can_sign_in_when_launched_with_env_from_run_script(tmp_path, monkeypatch):
    """run.ps1 / run.sh / docker-compose copy .env into the process
    environment, and environment variables outrank .env in pydantic-settings.
    A user registered on the Users page must still be able to sign in
    straight away — no fake reload here, the real one."""
    from app import settings as settings_mod
    real = local_users.hash_password
    admin_entry = "boss:" + real("boss-pass-1", iterations=1000)
    env = tmp_path / ".env"
    env.write_text(f"LOCAL_USERS='{admin_entry}'\nADMIN_USERS=boss\n"
                   f"SESSION_SECRET={get_settings().session_secret}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)                              # Settings() reads ./.env
    monkeypatch.setattr(envconfig, "ENV_PATH", env)
    monkeypatch.setattr(settings_mod, "_settings", get_settings())   # restored after the test
    monkeypatch.setenv("LOCAL_USERS", admin_entry)           # what run.ps1 exported at startup
    monkeypatch.setenv("ADMIN_USERS", "boss")
    monkeypatch.setattr(get_settings(), "local_users", admin_entry)
    monkeypatch.setattr(get_settings(), "admin_users", "boss")

    c = admin_client()
    assert c.post("/api/users", json={"username": "viewer", "password": "viewer-pass-1"}).status_code == 201
    r = login("viewer", "viewer-pass-1")
    assert r.status_code == 200, r.text
    assert r.json()["is_admin"] is False
    assert login("boss", "boss-pass-1").json()["is_admin"] is True


def test_admin_toggle_and_remove_but_never_on_themselves(env_file):
    c = admin_client()
    c.post("/api/users", json={"username": "boss", "password": "boss-pass-1", "is_admin": True})
    c.post("/api/users", json={"username": "alice", "password": "alice-pass-1"})

    assert c.post("/api/users/alice/admin", json={"is_admin": True}).status_code == 200
    assert login("alice", "alice-pass-1").json()["is_admin"] is True

    assert c.post("/api/users/boss/admin", json={"is_admin": False}).status_code == 400
    assert c.post("/api/users/boss/remove").status_code == 400

    assert c.post("/api/users/alice/remove").status_code == 200
    assert login("alice", "alice-pass-1").status_code == 401
    assert [u["username"] for u in c.get("/api/users").json()["users"]] == ["boss"]
