import httpx
import pytest

from app.server import db, imap
from app.server.dav import DavClient, DavError


# ---------------------------------------------------------------- A. redirections DAV
def _client(handler):
    return DavClient("u", "p", transport=httpx.MockTransport(handler))


def test_dav_redirect_keeps_method_body_auth():
    seen = []

    def h(req):
        seen.append(req)
        if req.url.path == "/.well-known/caldav":
            return httpx.Response(301, headers={"Location": "/dav/"})
        return httpx.Response(207, text="<x/>")

    with _client(h) as c:
        r = c._request("PROPFIND", "https://s.test/.well-known/caldav", ok=(207,),
                       content=b"<body/>", headers={"Depth": "0"})
    assert r.status_code == 207 and len(seen) == 2
    assert seen[1].method == "PROPFIND" and seen[1].content == b"<body/>"
    assert seen[1].headers["Depth"] == "0" and "authorization" in seen[1].headers


def test_dav_redirect_cross_host_drops_auth_and_loop_limited():
    seen = []

    def h(req):
        seen.append(req)
        if req.url.host == "a.test":
            return httpx.Response(302, headers={"Location": "https://b.test/x"})
        return httpx.Response(207, text="<x/>")

    with _client(h) as c:
        c._request("PROPFIND", "https://a.test/", ok=(207,), content=b"x")
    assert "authorization" in seen[0].headers and "authorization" not in seen[1].headers
    assert seen[1].method == "PROPFIND"

    with _client(lambda req: httpx.Response(302, headers={"Location": "/again"})) as c:
        with pytest.raises(DavError):
            c._request("PROPFIND", "https://a.test/")


# ---------------------------------------------------------------- B. update_account purge
def test_update_account_purges_items(client):
    srv = {"name": "P", "imap_host": "h", "imap_port": 993, "imap_security": "ssl", "verify_tls": True,
           "root_folder": None, "caldav_url": None, "carddav_url": None, "dav_enabled": False,
           "workers": 1, "notes": ""}
    sid = client.post("/api/servers", json=srv).json()["id"]
    aid = client.post("/api/accounts", json={"server_id": sid, "email": "a@example.test",
                                             "password": "x"}).json()["id"]

    def add():
        db.ex("INSERT INTO items(account_id,pst_id,kind,item_key,updated_at) VALUES (?,?,?,?,?)", (aid, 1, "mail", "k", 0))

    def n():
        return db.q1("SELECT COUNT(*) c FROM items WHERE account_id=?", (aid,))["c"]
    add()
    client.put(f"/api/accounts/{aid}", json={"options": {"mail": True}})
    assert n() == 1  # modif sans effet sur la cible
    for body in ({"email": "b@example.test"}, {"login": "other"}):
        db.ex("INSERT OR IGNORE INTO items(account_id,pst_id,kind,item_key,updated_at) VALUES (?,?,?,?,?)", (aid, 1, "mail", "k", 0))
        assert client.put(f"/api/accounts/{aid}", json=body).status_code == 200
        assert n() == 0
    sid2 = client.post("/api/servers", json={**srv, "name": "Q"}).json()["id"]
    db.ex("INSERT OR IGNORE INTO items(account_id,pst_id,kind,item_key,updated_at) VALUES (?,?,?,?,?)", (aid, 1, "mail", "k", 0))
    client.put(f"/api/accounts/{aid}", json={"server_id": sid2})
    assert n() == 0


# ---------------------------------------------------------------- C. sécurité web
def test_docs_disabled(client):
    for p in ("/api/docs", "/api/redoc", "/api/openapi.json"):
        assert client.get(p).status_code in (401, 404)
    from fastapi.testclient import TestClient
    from app.server.web import app
    with TestClient(app) as c:
        assert c.get("/api/openapi.json").status_code in (401, 404)


def test_login_rate_limit(env):
    from fastapi.testclient import TestClient
    from app.server import web
    web._LOGIN_FAILS.clear()
    with TestClient(web.app) as c:
        for _ in range(5):
            assert c.post("/api/login", json={"password": "no"}).status_code == 401
        assert c.post("/api/login", json={"password": "no"}).status_code == 429
        assert c.post("/api/login", json={"password": "test-admin-é"}).status_code == 429
    web._LOGIN_FAILS.clear()


def test_session_invalidated_by_password_change(env):
    from app.server.security import check_session, make_session
    tok = make_session()
    assert check_session(tok)
    old = env.admin_password
    env.admin_password = "autre"
    try:
        assert not check_session(tok)
    finally:
        env.admin_password = old
    assert check_session(tok)


# ---------------------------------------------------------------- D. IMAP login
class FakeM:
    def __init__(self, caps, typ="OK"):
        self.capabilities = caps
        self._encoding = "ascii"
        self.state = "NONAUTH"
        self.typ = typ
        self.cmd = None
        self.blob = None

    def _simple_command(self, name, *args):
        self.cmd = (name, args, self._encoding)
        return self.typ, [b"denied"]

    def authenticate(self, mech, fn):
        self.blob = (mech, fn(b""))


def test_imap_login_plain_utf8():
    m = FakeM(("IMAP4REV1", "AUTH=PLAIN"))
    imap._login(m, "jé an", 'pä"ss\\')
    assert m.blob == ("PLAIN", "\0jé an\0pä\"ss\\".encode("utf-8"))


def test_imap_login_fallback_quoted():
    m = FakeM(("IMAP4REV1",))
    imap._login(m, "jé an", 'p"s\\')
    assert m.cmd == ("LOGIN", ('"jé an"', '"p\\"s\\\\"'), "ascii") or m.cmd[1] == ('"jé an"', '"p\\"s\\\\"')
    assert m._encoding == "ascii" and m.state == "AUTH"
    import imaplib
    with pytest.raises(imaplib.IMAP4.error):
        imap._login(FakeM(("IMAP4REV1",), typ="NO"), "a", "b")


def test_dav_get_sends_auth():
    """Régression (e2e Radicale avec htpasswd) : GET/exists envoyait la requête sans Authorization → 401."""
    def h(req):
        if "authorization" not in req.headers:
            return httpx.Response(401)
        return httpx.Response(200, text="BEGIN:VCARD") if req.url.path.endswith("/a.vcf") else httpx.Response(404)

    with _client(h) as c:
        assert c.get("https://s.test/col/", "a.vcf") == "BEGIN:VCARD"
        assert c.exists("https://s.test/col/", "a.vcf") and not c.exists("https://s.test/col/", "b.vcf")


def test_test_item_delay_hook_only_in_child(monkeypatch):
    """Crochet PSTMIG_TEST_ITEM_DELAY (tests e2e) : sans effet hors sous-processus, borné et tolérant."""
    import time as _t

    from app.server import crash
    monkeypatch.setenv(crash.TEST_DELAY_ENV, "0.2")
    monkeypatch.setattr(crash, "IN_CHILD", False)
    t0 = _t.monotonic()
    crash.maybe_delay()
    assert _t.monotonic() - t0 < 0.1
    monkeypatch.setattr(crash, "IN_CHILD", True)
    crash.maybe_delay()
    assert _t.monotonic() - t0 >= 0.2
    monkeypatch.setenv(crash.TEST_DELAY_ENV, "abc")
    crash.maybe_delay()  # valeur invalide ignorée
