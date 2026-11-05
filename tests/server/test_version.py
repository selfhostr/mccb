from app.server import version


def test_version_public_and_health(env):
    from fastapi.testclient import TestClient
    from app.server.web import app
    with TestClient(app) as c:  # not logged in
        r = c.get("/api/version")
        assert r.status_code == 200 and r.json()["version"] == version.get_version()
        h = c.get("/api/health").json()
        assert h["version"] == version.get_version() and "worker" in h and h["ok"]


def test_env_override(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "9.9.9")
    assert version.get_version() == "9.9.9"


def test_file_fallback(monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.setenv("GIT_SHA", "abc1234")
    base = (version.VERSION_FILE).read_text().strip()
    assert version.get_version() == f"{base}+abc1234"
    monkeypatch.delenv("GIT_SHA")
    monkeypatch.setattr(version, "_git_sha", lambda: "")
    assert version.get_version() == base
