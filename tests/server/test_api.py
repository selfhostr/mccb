import time


def test_auth_required(env):
    from fastapi.testclient import TestClient
    from app.server.web import app
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/servers").status_code == 401
        assert c.get("/api/me").status_code == 401
        assert c.post("/api/login", json={"password": "faux"}).status_code == 401
        assert c.post("/api/login", json={"password": "test-admin-é"}).status_code == 200
        assert c.get("/api/me").json()["authenticated"] is True
        c.post("/api/logout")
        assert c.get("/api/me").status_code == 401
        c.cookies.set("mccb_session", "forge.xxx")
        assert c.get("/api/me").status_code == 401


SRV = {"name": "Prod", "imap_host": "imap.example.test", "imap_port": 993, "imap_security": "ssl",
       "verify_tls": True, "root_folder": None, "caldav_url": None, "carddav_url": "https://dav/{email}/",
       "dav_enabled": True, "workers": 4, "notes": "x"}


def test_servers_crud(client):
    r = client.post("/api/servers", json=SRV)
    assert r.status_code == 201
    s = r.json()
    assert set(s) == {"id", "name", "imap_host", "imap_port", "imap_security", "verify_tls", "root_folder",
                      "caldav_url", "carddav_url", "dav_enabled", "workers", "notes", "tuning", "builders",
                      "last_test", "last_test_result"}
    assert s["last_test"] is None and s["last_test_result"] is None
    assert s["carddav_url"] == "https://dav/{email}/"
    sid = s["id"]
    assert client.get(f"/api/servers/{sid}").json()["name"] == "Prod"
    upd = dict(SRV, name="Prod 2", root_folder="PST Import", workers=2)
    assert client.put(f"/api/servers/{sid}", json=upd).json()["root_folder"] == "PST Import"
    assert len(client.get("/api/servers").json()) == 1
    assert client.post("/api/servers", json=dict(SRV, imap_security="bad")).status_code == 422
    assert client.delete(f"/api/servers/{sid}").status_code == 200
    assert client.get(f"/api/servers/{sid}").status_code == 404


def test_server_test_is_stored_and_cleared_by_a_connection_change(client, monkeypatch):
    import imaplib

    def refuse(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(imaplib, "IMAP4_SSL", refuse)
    sid = client.post("/api/servers", json={**SRV, "dav_enabled": False}).json()["id"]
    res = client.post(f"/api/servers/{sid}/test").json()
    assert res["imap"]["ok"] is False and res["caldav"]["ok"] is True
    s = client.get(f"/api/servers/{sid}").json()
    assert s["last_test"] and s["last_test_result"] == res
    assert client.get("/api/servers").json()[0]["last_test_result"] == res
    # Notes only: the stored verdict still applies
    assert client.put(f"/api/servers/{sid}", json={**SRV, "dav_enabled": False, "notes": "y"}).json()["last_test_result"] == res
    # Another host: it no longer does
    s = client.put(f"/api/servers/{sid}", json={**SRV, "dav_enabled": False, "imap_host": "other.example.test"}).json()
    assert s["last_test"] is None and s["last_test_result"] is None


def test_accounts_crud_and_password_encrypted(client, env):
    sid = client.post("/api/servers", json=SRV).json()["id"]
    r = client.post("/api/accounts", json={"server_id": sid, "email": "jean@example.test", "password": "sécret"})
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["has_password"] is True and a["status"] == "new" and a["job"] is None and a["pst_id"] is None
    assert a["options"] == {"mail": True, "calendar": True, "contacts": True}
    assert a["login"] == "jean@example.test"
    from app.server import db
    from app.server.security import decrypt
    row = db.q1("SELECT password_enc FROM accounts WHERE id=?", (a["id"],))
    assert "sécret" not in row["password_enc"] and decrypt(row["password_enc"]) == "sécret"
    assert client.post("/api/accounts", json={"server_id": sid, "email": "jean@example.test", "password": "x"}).status_code == 409
    r = client.put(f"/api/accounts/{a['id']}", json={"options": {"calendar": False}, "login": "jean"})
    assert r.json()["options"]["calendar"] is False and r.json()["login"] == "jean"
    assert len(client.get(f"/api/accounts?server_id={sid}").json()) == 1
    assert client.get("/api/accounts?server_id=999").json() == []
    assert client.delete(f"/api/accounts/{a['id']}").status_code == 200
    assert client.get("/api/accounts").json() == []


def test_runs_require_valid_pst(client):
    sid = client.post("/api/servers", json=SRV).json()["id"]
    aid = client.post("/api/accounts", json={"server_id": sid, "email": "a@example.test", "password": "p"}).json()["id"]
    r = client.post("/api/runs", json={"account_ids": [aid, 9999]})
    assert r.status_code == 422 and len(r.json()["detail"]["errors"]) == 2


def test_spa_fallback_and_api_404(client, env, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>SPA</html>")
    (web / "app.js").write_text("console.log(1)")
    env.web_dir = web
    assert "SPA" in client.get("/").text
    assert "SPA" in client.get("/comptes/12").text
    assert client.get("/app.js").text == "console.log(1)"
    assert "SPA" in client.get("/../../etc/passwd").text
    assert client.get("/api/nexistepas").status_code == 404


def test_job_controls_state_machine(client):
    from app.server import db
    sid = client.post("/api/servers", json=SRV).json()["id"]
    aid = client.post("/api/accounts", json={"server_id": sid, "email": "b@example.test", "password": "p"}).json()["id"]
    jid = db.ex("INSERT INTO jobs(account_id,status,created_at) VALUES (?, 'queued', ?)", (aid, time.time())).lastrowid
    assert client.post(f"/api/jobs/{jid}/pause").json()["status"] == "paused"
    assert client.post(f"/api/jobs/{jid}/resume").json()["status"] == "queued"
    assert client.post(f"/api/jobs/{jid}/stop").json()["status"] == "stopped"
    assert client.post(f"/api/jobs/{jid}/pause").status_code == 409
    j = client.post(f"/api/jobs/{jid}/retry").json()
    assert j["status"] == "queued" and j["mode"] == "full"
    assert client.get("/api/jobs?active=1").json()[0]["id"] == jid
    keys = set(client.get(f"/api/jobs/{jid}").json())
    assert {"id", "account_id", "status", "phase", "progress", "current_folder", "rate", "eta",
            "started_at", "finished_at", "error"} <= keys
    lg = client.get(f"/api/jobs/{jid}/log").json()
    assert set(lg["lines"][0]) == {"n", "ts", "level", "msg"} and lg["next"] == lg["lines"][-1]["n"]
    assert client.get(f"/api/jobs/{jid}/log?after={lg['next']}").json() == {"lines": [], "next": lg["next"]}
    assert client.get(f"/api/jobs/{jid}/errors").json() == []


def test_sse_emits_job_events(client):
    import threading
    from app.server import db
    sid = client.post("/api/servers", json=SRV).json()["id"]
    aid = client.post("/api/accounts", json={"server_id": sid, "email": "c@example.test", "password": "p"}).json()["id"]

    def later():
        time.sleep(0.8)
        db.ex("INSERT INTO jobs(account_id,status,created_at) VALUES (?, 'queued', ?)", (aid, time.time()))

    threading.Thread(target=later).start()
    got = []
    with client.stream("GET", "/api/events?max_seconds=3") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        ev = None
        for line in r.iter_lines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: ") and ev:
                got.append(ev)
                if ev == "job":
                    break
    assert "job" in got
