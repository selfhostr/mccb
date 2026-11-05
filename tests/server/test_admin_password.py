"""Auto-generated admin password (security.ensure_admin_password) and POST /api/password."""
import json
import os
import stat

import pytest


@pytest.fixture
def sec(env, monkeypatch, tmp_path):
    """security module with no ADMIN_PASSWORD and an empty data dir."""
    from app.server import security
    monkeypatch.setattr(env, "admin_password", "")
    monkeypatch.setattr(env, "data_dir", tmp_path / "admindata")
    security.reset_cache()
    yield security
    security.reset_cache()


def test_generates_once_and_checks(sec, env):
    pw = sec.ensure_admin_password()
    assert pw and len(pw) >= 16
    assert sec.ensure_admin_password() is None  # second call: already exists, nothing to show
    assert sec.check_password(pw)
    assert not sec.check_password(pw + "x")
    assert not sec.check_password("")


def test_file_mode_and_no_plaintext(sec, env):
    pw = sec.ensure_admin_password()
    p = env.data_dir / "admin.json"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    raw = p.read_text()
    assert pw not in raw
    rec = json.loads(raw)
    assert {"salt", "hash"} <= set(rec)


def test_set_password_invalidates_sessions(sec):
    pw = sec.ensure_admin_password()
    tok = sec.make_session()
    assert sec.check_session(tok)
    gen = sec._generation()
    sec.set_admin_password("brand-new-password")
    assert sec._generation() != gen
    assert not sec.check_session(tok)
    assert sec.check_session(sec.make_session())
    assert sec.check_password("brand-new-password") and not sec.check_password(pw)


def test_env_var_wins(sec, env, monkeypatch):
    monkeypatch.setattr(env, "admin_password", "from-env-é")
    assert sec.password_from_env()
    assert sec.ensure_admin_password() is None
    assert not (env.data_dir / "admin.json").exists()
    assert sec.check_password("from-env-é") and not sec.check_password("other")


def test_api_password_wrong_current_403(sec, env):
    from fastapi.testclient import TestClient
    from app.server.web import app
    pw = sec.ensure_admin_password()
    with TestClient(app) as c:
        assert c.post("/api/login", json={"password": pw}).status_code == 200
        r = c.post("/api/password", json={"current": "wrong-current", "new": "another-password"})
        assert r.status_code == 403
        r = c.post("/api/password", json={"current": pw, "new": "another-password"})
        assert r.status_code == 200
        assert c.get("/api/me").status_code == 200  # fresh cookie issued
        c.cookies.clear()
        assert c.post("/api/login", json={"password": pw}).status_code == 401
        assert c.post("/api/login", json={"password": "another-password"}).status_code == 200


def test_api_password_env_set_409(client):
    r = client.post("/api/password", json={"current": "test-admin-é", "new": "another-password"})
    assert r.status_code == 409
