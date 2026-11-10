"""Source connections (one credential reached by many accounts, e.g. a Microsoft 365 tenant): schema, registry
scope and wiring."""
import json
import sqlite3
from pathlib import Path

import pytest

SCHEMA_100 = Path(__file__).resolve().parents[1] / "fixtures" / "schema-1.0.0.sql"
CONNECTION_COLS = {"id", "name", "kind", "config", "secret_enc", "notes", "created_at", "last_test",
                   "last_test_result", "provisioned"}


# ------------------------------------------------------------------ schema
def test_fresh_schema_has_source_connections(env):
    from app.server import db
    assert CONNECTION_COLS <= {r[1] for r in db.q("PRAGMA table_info(source_connections)")}
    assert "source_connection_id" in {r[1] for r in db.q("PRAGMA table_info(accounts)")}


def test_old_database_gains_source_connections(env):
    from app.server import db
    env.db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(env.db_path)
    c.executescript(SCHEMA_100.read_text())
    c.execute("INSERT INTO servers(id,name,imap_host,created_at) VALUES (1,'s','h',1)")
    c.execute("INSERT INTO accounts(id,server_id,email,login,created_at) VALUES (1,1,'a@example.test','',1)")
    c.commit()
    c.close()
    db.reset()
    assert CONNECTION_COLS <= {r[1] for r in db.q("PRAGMA table_info(source_connections)")}
    assert db.q1("SELECT source_connection_id FROM accounts WHERE id=1")["source_connection_id"] is None
    db.reset()  # second start: idempotent
    assert db.q1("SELECT COUNT(*) n FROM source_connections")["n"] == 0


def test_migrate_before_schema_leaves_missing_tables_to_the_schema(env):
    """_migrate on a 1.0.0 database (no source_connections yet) must not fail: the table comes from SCHEMA."""
    from app.server import db
    env.db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(env.db_path, isolation_level=None)
    c.executescript(SCHEMA_100.read_text())
    db._migrate(c)
    assert not c.execute("PRAGMA table_info(source_connections)").fetchall()
    c.executescript(db.SCHEMA)
    assert CONNECTION_COLS <= {r[1] for r in c.execute("PRAGMA table_info(source_connections)")}
    c.close()


def test_source_connections_without_provisioned_gains_it(env):
    """A database created before "Connect with Microsoft": source_connections without the provisioned column."""
    from app.server import db
    env.db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(env.db_path)
    c.executescript(SCHEMA_100.read_text())
    c.execute("CREATE TABLE source_connections (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
              "kind TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}', secret_enc TEXT, notes TEXT NOT NULL DEFAULT '', "
              "created_at REAL NOT NULL, last_test REAL, last_test_result TEXT)")
    c.execute("INSERT INTO source_connections(name, kind, created_at) VALUES ('Contoso', 'm365_graph', 1)")
    c.commit()
    c.close()
    db.reset()
    assert CONNECTION_COLS <= {r[1] for r in db.q("PRAGMA table_info(source_connections)")}
    assert db.q1("SELECT provisioned FROM source_connections")["provisioned"] is None


def test_source_connection_row_roundtrip(env):
    from app.server import db
    cid = db.ex("INSERT INTO source_connections(name, kind, config, secret_enc, created_at) VALUES (?,?,?,?,?)",
                ("Contoso", "m365_graph", '{"tenant_id": "t"}', None, 1.0)).lastrowid
    r = db.q1("SELECT * FROM source_connections WHERE id=?", (cid,))
    assert r["notes"] == "" and r["last_test"] is None and r["last_test_result"] is None


# ------------------------------------------------------------------ registry scope
def test_field_scope_and_directory_in_the_catalogue():
    from app.connectors import registry
    f = registry.Field("x", "X")
    assert f.scope == "mailbox" and f.view()["scope"] == "mailbox"
    assert registry.Field("t", "T", scope="connection").view()["scope"] == "connection"
    spec = registry.ConnectorSpec(kind="k", label="K")
    assert spec.directory is None and spec.view()["directory"] is False
    assert registry.ConnectorSpec(kind="k", label="K", directory=object()).view()["directory"] is True


def test_m365_connection_scoped_fields():
    from app.connectors import m365  # noqa: F401  (registers m365_graph)
    from app.connectors import registry
    for role in ("source", "target"):
        r = registry.role_of("m365_graph", role)
        conn = {f.name for f in registry.split_fields(r, "connection")}
        assert conn == {"tenant_id", "client_id", "client_secret", "cloud"}
        mailbox = {f.name for f in registry.split_fields(r, "mailbox")}
        assert "mailbox" in mailbox and not conn & mailbox
        assert conn | mailbox == {f.name for f in r.fields}
    entry = next(c for c in registry.catalogue() if c["kind"] == "m365_graph")
    assert {f["name"]: f["scope"] for f in entry["source"]["fields"]}["tenant_id"] == "connection"


# ------------------------------------------------------------------ wiring
TENANT = "11111111-2222-3333-4444-555555555555"
CONN_CONFIG = {"tenant_id": TENANT, "client_id": "app-1", "cloud": "global"}


def _server():
    from app.server import db
    return db.ex("INSERT INTO servers(name, imap_host, created_at) VALUES ('s', 'h', 1)").lastrowid


def _connection(config=CONN_CONFIG, secrets=None, kind="m365_graph"):
    from app.connectors import wiring
    from app.server import db
    return db.ex("INSERT INTO source_connections(name, kind, config, secret_enc, created_at) VALUES (?,?,?,?,1)",
                 ("Contoso", kind, json.dumps(config),
                  wiring.dump_secrets({"client_secret": "tenant-sécret"} if secrets is None else secrets))).lastrowid


def _account(sid, email, kind, config, secrets=None, connection_id=None):
    from app.connectors import wiring
    from app.server import db
    aid = db.ex("INSERT INTO accounts(server_id, email, created_at, source_kind, source_config, source_secret_enc, "
                "source_connection_id) VALUES (?,?,1,?,?,?,?)",
                (sid, email, kind, json.dumps(config), wiring.dump_secrets(secrets or {}), connection_id)).lastrowid
    return db.q1("SELECT * FROM accounts WHERE id=?", (aid,))


def test_linked_account_has_the_namespace_of_the_same_mailbox_configured_alone(env):
    from app.connectors import m365  # noqa: F401
    from app.connectors import wiring
    sid = _server()
    alone = _account(sid, "a@x.test", "m365_graph",
                     {**CONN_CONFIG, "tenant_id": TENANT.upper(), "mailbox": "a@x.test", "include_tasks": True},
                     {"client_secret": "own"})
    linked = _account(sid, "b@x.test", "m365_graph", {"mailbox": "a@x.test", "include_tasks": True},
                      connection_id=_connection())
    assert wiring.items_key_of_account(linked) == wiring.items_key_of_account(alone) is not None
    assert wiring.source_problem(linked) is None
    other = _account(sid, "c@x.test", "m365_graph", {"mailbox": "c@x.test"}, connection_id=linked["source_connection_id"])
    assert wiring.items_key_of_account(other) != wiring.items_key_of_account(linked)


def test_linked_account_config_and_secrets_come_from_the_connection(env):
    from app.connectors import wiring
    sid = _server()
    cid = _connection()
    acc = _account(sid, "a@x.test", "m365_graph",
                   {"mailbox": "oid-1", "include_tasks": False, "tenant_id": "stale", "client_id": "stale"},
                   {"client_secret": "stale"}, connection_id=cid)
    assert wiring.connection_of(acc)["id"] == cid
    assert wiring.source_config(acc) == {**CONN_CONFIG, "mailbox": "oid-1", "include_tasks": False}
    assert wiring.source_secrets(acc) == {"client_secret": "tenant-sécret"}


def test_source_of_account_is_built_from_the_merged_config(env):
    from app.connectors import registry, wiring
    seen = {}

    def factory(config, secrets, ctx):
        seen.update(config=config, secrets=secrets)
        return object()

    fields = (registry.Field("host", "Host", scope="connection"), registry.Field("pw", "Pw", secret=True,
                                                                                 scope="connection"),
              registry.Field("box", "Box"))
    registry.register(registry.ConnectorSpec(kind="conn_test", label="T", source=registry.Role(
        factory, ("mail",), fields=fields, identity=lambda c: f"{c.get('host')}:{c.get('box')}")))
    try:
        sid = _server()
        cid = _connection({"host": "h1"}, {"pw": "p1"}, kind="conn_test")
        acc = _account(sid, "a@x.test", "conn_test", {"box": "b"}, connection_id=cid)
        wiring.source_of_account(acc)
        assert seen == {"config": {"host": "h1", "box": "b"}, "secrets": {"pw": "p1"}}
    finally:
        registry.unregister("conn_test")


def test_dangling_connection_is_a_source_problem(env):
    from app.connectors import m365  # noqa: F401
    from app.connectors import wiring
    from app.server import db
    sid = _server()
    cid = _connection()
    acc = _account(sid, "a@x.test", "m365_graph", {"mailbox": "oid-1"}, connection_id=cid)
    db.ex("DELETE FROM source_connections WHERE id=?", (cid,))
    assert wiring.connection_of(acc) is None
    assert wiring.source_problem(acc) == "source connection missing"
    assert wiring.items_key_of_account(acc) is None
    assert wiring.source_secrets(acc) == {}


def test_unlinked_account_is_unchanged(env):
    from app.connectors import m365  # noqa: F401
    from app.connectors import registry, wiring
    sid = _server()
    cfg = {**CONN_CONFIG, "mailbox": "a@x.test"}
    acc = _account(sid, "a@x.test", "m365_graph", cfg, {"client_secret": "own"})
    assert wiring.connection_of(acc) is None
    assert wiring.source_config(acc) == cfg
    assert wiring.source_secrets(acc) == {"client_secret": "own"}
    assert wiring.items_key_of_account(acc) == registry.namespace("m365_graph", "source", cfg)
    assert wiring.source_problem(acc) is None


# ------------------------------------------------------------------ API (api_sources.py)
import io  # noqa: E402

import httpx  # noqa: E402

from tests.server import fake_exchange as fx  # noqa: E402
from tests.server.test_connectors_exchange import GRAPH_CONFIG, collect, seed_graph  # noqa: E402,F401

SECRET = fx.FakeGraph.SECRET
ROLES = ["User.Read.All", "Mail.Read", "Calendars.Read", "Contacts.Read", "MailboxSettings.Read", "Tasks.Read.All"]
SRV = {"name": "Yeti", "imap_host": "imap.yeti.test", "imap_port": 993, "imap_security": "ssl", "dav_enabled": True,
       "caldav_url": "https://dav.yeti.test/cal/", "carddav_url": "https://dav.yeti.test/card/"}
BODY = {"name": "Contoso", "kind": "m365_graph", "config": {"tenant_id": "contoso", "client_id": "app-1"},
        "secrets": {"client_secret": SECRET}, "notes": "client A"}


def graph_user(uid, mail, **kw):
    return {"id": uid, "displayName": uid.title(), "mail": mail, "userPrincipalName": mail, "proxyAddresses": [],
            "accountEnabled": True, "assignedLicenses": [{"skuId": "x"}], "userType": "Member", **kw}


@pytest.fixture
def graph(monkeypatch):
    from app.connectors import m365, oauth
    from app.server import api_sources
    monkeypatch.setattr(m365, "_sleep", lambda s: None)
    api_sources._cache.clear()
    f = fx.FakeGraph()
    f.roles = list(ROLES)
    f.directory = [graph_user("oid-me", "me@contoso.test"), graph_user("oid-ann", "ann@contoso.test"),
                   graph_user("oid-bob", "bob@contoso.test",
                              proxyAddresses=["SMTP:bob@contoso.test", "smtp:robert@contoso.test"]),
                   graph_user("oid-zed", "zed@contoso.test")]
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(f))
    return f


def no_secret(r):
    assert SECRET not in r.text
    return r


def new_conn(client, **over):
    r = no_secret(client.post("/api/source-connections", json={**BODY, **over}))
    assert r.status_code == 201, r.text
    return r.json()


def yeti(client, emails=("me@contoso.test", "ann@contoso.test", "robert@contoso.test", "only@yeti.test")):
    sid = client.post("/api/servers", json=SRV).json()["id"]
    data = ("email;password\n" + "".join(f"{e};pw\n" for e in emails)).encode()
    r = client.post("/api/accounts/import-csv", data={"server_id": str(sid)},
                    files={"file": ("a.csv", io.BytesIO(data), "text/csv")})
    assert r.status_code == 200 and r.json()["created"] == len(emails), r.text
    ids = {a["email"]: a["id"] for a in client.get(f"/api/accounts?server_id={sid}").json()}
    return sid, ids


def test_connection_crud_never_returns_the_secret(client):
    c = new_conn(client)
    assert c["config"] == {"tenant_id": "contoso", "client_id": "app-1", "cloud": "global"}
    assert c["has_secret"] is True and c["accounts"] == 0 and c["notes"] == "client A" and c["last_test"] is None
    assert set(c) == {"id", "name", "kind", "config", "has_secret", "notes", "last_test", "last_test_result",
                      "accounts", "created_at", "provisioned", "secret_names"}
    assert c["secret_names"] == ["client_secret"]
    for r in (client.get("/api/source-connections"), client.get(f"/api/source-connections/{c['id']}")):
        assert r.status_code == 200 and SECRET not in r.text
    r = no_secret(client.put(f"/api/source-connections/{c['id']}", json={"name": "Contoso Ltd"}))
    assert r.status_code == 200 and r.json()["name"] == "Contoso Ltd" and r.json()["has_secret"] is True
    assert client.delete(f"/api/source-connections/{c['id']}").json() == {"ok": True}
    assert client.get(f"/api/source-connections/{c['id']}").status_code == 404


@pytest.mark.parametrize("config,secrets", [
    ({"tenant_id": "t", "client_id": "c", "mailbox": "a@x.test"}, {"client_secret": "s"}),   # mailbox field
    ({"tenant_id": "t", "client_id": "c", "client_secret": "s"}, {}),                         # secret in config
    ({"tenant_id": "t", "client_id": "c", "bogus": 1}, {"client_secret": "s"}),               # unknown
    ({"tenant_id": "t"}, {"client_secret": "s"}),                                             # required missing
    ({"tenant_id": "t", "client_id": "c"}, {}),                                               # secret missing
])
def test_connection_validation(client, config, secrets):
    r = client.post("/api/source-connections", json={**BODY, "config": config, "secrets": secrets})
    assert r.status_code == 422, r.text


SHARED = {"name": "Old server", "kind": "imap", "config": {"host": "imap.old.test"}, "secrets": {}}


def test_a_kind_without_directory_is_a_shared_server(client):
    r = client.post("/api/source-connections", json=SHARED)
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["config"]["host"] == "imap.old.test" and c["has_secret"] is False and c["secret_names"] == []
    # It is checked with each mailbox: the routes of a directory refuse it.
    assert client.post(f"/api/source-connections/{c['id']}/test").status_code == 422
    assert client.post("/api/source-connections/test", json={"kind": "imap", "config": {"host": "h"}}).status_code == 422
    assert client.get(f"/api/source-connections/{c['id']}/mailboxes").status_code == 422
    # A field the kind does not have, and files, are refused.
    assert client.post("/api/source-connections", json={**SHARED, "secrets": {"client_secret": "x"}}).status_code == 422
    assert client.post("/api/source-connections", json={**SHARED, "kind": "pst", "config": {}}).status_code == 422


def test_a_mailbox_reads_through_a_shared_server_with_its_own_login(client):
    _, ids = yeti(client, ("ann@contoso.test",))
    cid = client.post("/api/source-connections", json=SHARED).json()["id"]
    url = f"/api/accounts/{ids['ann@contoso.test']}/source"
    # The server gives the address, the mailbox its login and password: the whole is validated.
    assert client.put(url, json={"connection_id": cid, "config": {}, "secrets": {"password": SECRET}}).status_code == 422
    assert client.put(url, json={"connection_id": cid, "config": {"login": "ann"}}).status_code == 422
    r = no_secret(client.put(url, json={"connection_id": cid, "config": {"login": "ann"}, "secrets": {"password": SECRET}}))
    assert r.status_code == 200, r.text
    src = r.json()["source"]
    assert src["kind"] == "imap" and src["connection"]["id"] == cid and src["has_secret"] is True
    assert client.get(f"/api/source-connections/{cid}").json()["accounts"] == 1
    # Another address would make it another mailbox: refused while it is linked.
    assert client.put(f"/api/source-connections/{cid}", json={"config": {"host": "imap.other.test"}}).status_code == 409
    assert client.put(f"/api/source-connections/{cid}", json={"name": "Renamed"}).status_code == 200


def test_put_secrets_semantics_and_test_cleared(client, graph):
    from app.connectors import wiring
    from app.server import db
    c = new_conn(client)
    assert client.post(f"/api/source-connections/{c['id']}/test").json()["ok"] is True
    url = f"/api/source-connections/{c['id']}"
    assert client.put(url, json={"notes": "x"}).json()["last_test"] is not None  # notes only: the test stays
    secret = lambda: wiring.load_secrets(db.q1("SELECT secret_enc FROM source_connections WHERE id=?",  # noqa: E731
                                               (c["id"],))["secret_enc"])
    assert client.put(url, json={"config": BODY["config"]}).json()["has_secret"] is True  # omitted: kept
    assert secret() == {"client_secret": SECRET}
    r = no_secret(client.put(url, json={"secrets": {"client_secret": "new-value"}}))
    assert r.json()["last_test"] is None and r.json()["last_test_result"] is None and "new-value" not in r.text
    assert secret() == {"client_secret": "new-value"}
    assert client.put(url, json={"secrets": {}}).status_code == 422  # cleared, but required
    r = client.put(url, json={"config": {**BODY["config"], "client_id": "app-2"}})
    assert r.json()["config"]["client_id"] == "app-2"


def test_tenant_change_with_linked_accounts_is_409_and_delete_too(client, graph):
    c = new_conn(client)
    sid, ids = yeti(client)
    r = client.post(f"/api/source-connections/{c['id']}/link",
                    json={"server_id": sid, "links": [{"account_id": ids["ann@contoso.test"], "mailbox_id": "oid-ann"}]})
    assert r.status_code == 200 and r.json()["linked"] == [ids["ann@contoso.test"]], r.text
    url = f"/api/source-connections/{c['id']}"
    assert client.get(url).json()["accounts"] == 1
    assert client.put(url, json={"config": {**BODY["config"], "tenant_id": "fabrikam"}}).status_code == 409
    assert client.put(url, json={"config": {**BODY["config"], "cloud": "usgov"}}).status_code == 409
    assert client.put(url, json={"config": {**BODY["config"], "tenant_id": "CONTOSO"}}).status_code == 200
    assert client.put(url, json={"config": {**BODY["config"], "client_id": "app-2"}}).status_code == 200
    r = client.delete(url)
    assert r.status_code == 409 and r.json()["detail"]["accounts"] == 1
    # detached (PUT source without connection_id): the connection can go
    client.put(f"/api/accounts/{ids['ann@contoso.test']}/source", json={"kind": "pst"})
    assert client.delete(url).status_code == 200


def test_test_unsaved_and_stored(client, graph):
    r = no_secret(client.post("/api/source-connections/test",
                              json={"kind": "m365_graph", "config": BODY["config"], "secrets": {"client_secret": "bad"}}))
    assert r.status_code == 200 and r.json()["ok"] is False and "bad" not in r.text
    c = new_conn(client)
    r = no_secret(client.post("/api/source-connections/test", json={"kind": "m365_graph", "config": BODY["config"],
                                                                    "id": c["id"]}))
    assert r.json()["ok"] is True  # secret filled from the stored connection
    assert client.get(f"/api/source-connections/{c['id']}").json()["last_test"] is None  # unsaved: nothing stored
    graph.roles = [x for x in ROLES if x != "Tasks.Read.All"]
    r = no_secret(client.post(f"/api/source-connections/{c['id']}/test"))
    res = r.json()
    assert res["ok"] is True and {x["key"]: x["ok"] for x in res["checks"]}["tasks"] is False
    stored = client.get(f"/api/source-connections/{c['id']}").json()
    assert stored["last_test"] and stored["last_test_result"] == res


def test_mailboxes_cache_and_refresh(client, graph):
    c = new_conn(client)
    url = f"/api/source-connections/{c['id']}/mailboxes"
    r = no_secret(client.get(url))
    assert r.status_code == 200 and {m["id"] for m in r.json()["mailboxes"]} == {"oid-me", "oid-ann", "oid-bob", "oid-zed"}
    users = lambda: sum(1 for q in graph.requests if q.url.path.endswith("/users"))  # noqa: E731
    n = users()
    assert client.get(url).json() == r.json() and users() == n  # cached
    graph.directory.append(graph_user("oid-new", "new@contoso.test"))
    assert len(client.get(url + "?refresh=1").json()["mailboxes"]) == 5 and users() > n


def test_mailboxes_error_is_502_without_the_secret(client, graph):
    c = new_conn(client, secrets={"client_secret": "wrong-sécret"})
    r = client.get(f"/api/source-connections/{c['id']}/mailboxes")
    assert r.status_code == 502 and "wrong-sécret" not in r.text


def test_match_rows_for_a_server_filled_by_csv(client, graph):
    c = new_conn(client)
    sid, ids = yeti(client)
    r = no_secret(client.get(f"/api/source-connections/{c['id']}/match?server_id={sid}"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connection"] == {"id": c["id"], "name": "Contoso"}
    assert body["server"] == {"id": sid, "name": "Yeti", "dav_enabled": True, "caldav_ok": None, "carddav_ok": None}
    assert body["purpose_checked"] is True and body["hidden"] == {"guests": 0, "no_mailbox": 0} and body["fetched_at"]
    states = {(row["mailbox"] or {}).get("id") or row["account"]["email"]: row["state"] for row in body["rows"]}
    assert states == {"oid-me": "ready", "oid-ann": "ready", "oid-bob": "ready", "oid-zed": "no_target",
                      "only@yeti.test": "no_source"}
    bob = next(x for x in body["rows"] if (x["mailbox"] or {}).get("id") == "oid-bob")
    assert bob["matched_by"] == "alias" and bob["matched_address"] == "robert@contoso.test"
    assert set(bob) == {"state", "mailbox", "account", "matched_by", "matched_address", "coverage", "last_job"}
    assert set(bob["account"]) == {"id", "email", "status", "last_test", "last_test_result", "source_kind",
                                   "source_connection_id", "free"}
    assert bob["account"]["free"] is True
    assert bob["coverage"] == {"mail": "on", "calendar": "unknown", "contacts": "unknown", "tasks": "unknown"}
    assert all(set(x["coverage"]) == {"mail", "calendar", "contacts", "tasks"} for x in body["rows"])
    assert body["counts"]["ready"] == 3 and body["counts"]["no_target"] == 1 and "all" not in body["counts"]
    # without server_id: the server with the most matches
    client.post("/api/servers", json={**SRV, "name": "Empty"})
    assert client.get(f"/api/source-connections/{c['id']}/match").json()["server"]["id"] == sid
    assert client.get(f"/api/source-connections/{c['id']}/match?server_id=999").status_code == 404


def test_link_start_creates_a_named_batch_and_an_active_job_is_an_error(client, graph):
    from app.server import db
    c = new_conn(client)
    sid, ids = yeti(client)
    me, ann = ids["me@contoso.test"], ids["ann@contoso.test"]
    db.ex("INSERT INTO jobs(account_id, status, phase, progress, mode, created_at) VALUES (?, 'running', 'mail', '{}', "
          "'full', 1)", (ann,))
    r = no_secret(client.post(f"/api/source-connections/{c['id']}/link", json={
        "server_id": sid, "links": [{"account_id": me, "mailbox_id": "oid-me"}, {"account_id": ann, "mailbox_id": "oid-ann"}],
        "include_tasks": True, "start": True, "batch_name": "Contoso to Yeti"}))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["linked"] == [me] and out["errors"] == [{"account_id": ann, "msg": "a job is active for this mailbox"}]
    assert len(out["jobs"]) == 1 and out["jobs"][0]["account_id"] == me and out["jobs"][0]["batch_id"] == out["batch_id"]
    assert db.q1("SELECT name FROM batches WHERE id=?", (out["batch_id"],))["name"] == "Contoso to Yeti"
    acc = client.get(f"/api/accounts/{me}").json()
    assert acc["source"] == {"kind": "m365_graph", "config": {"mailbox": "oid-me", "include_tasks": True},
                             "has_secret": True, "connection": {"id": c["id"], "name": "Contoso"}}
    row = db.q1("SELECT * FROM accounts WHERE id=?", (me,))
    assert row["source_secret_enc"] is None and row["source_connection_id"] == c["id"]
    rows = client.get(f"/api/source-connections/{c['id']}/match?server_id={sid}").json()["rows"]
    linked = next(x for x in rows if x["account"] and x["account"]["id"] == me)
    assert linked["state"] == "linked" and linked["last_job"] == {"id": out["jobs"][0]["id"], "status": "queued"}
    # nothing linkable at all: 409
    r = client.post(f"/api/source-connections/{c['id']}/link",
                    json={"server_id": sid, "links": [{"account_id": ann, "mailbox_id": "oid-ann"}]})
    assert r.status_code == 409 and r.json()["detail"]["errors"][0]["account_id"] == ann


def test_link_refuses_another_source_and_another_server(client, graph):
    c = new_conn(client)
    sid, ids = yeti(client)
    ann = ids["ann@contoso.test"]
    client.put(f"/api/accounts/{ann}/source", json={"kind": "m365_graph", "config": {
        "tenant_id": "fabrikam", "client_id": "x", "mailbox": "ann@fabrikam.test"}, "secrets": {"client_secret": "s"}})
    other_sid = client.post("/api/servers", json={**SRV, "name": "Other"}).json()["id"]
    r = client.post(f"/api/source-connections/{c['id']}/link", json={"server_id": other_sid, "links": [
        {"account_id": ids["me@contoso.test"], "mailbox_id": "oid-me"}]})
    assert r.status_code == 409 and r.json()["detail"]["errors"][0]["msg"] == "not a mailbox of this server"
    r = client.post(f"/api/source-connections/{c['id']}/link", json={"server_id": sid, "links": [
        {"account_id": ann, "mailbox_id": "oid-ann"}, {"account_id": ids["me@contoso.test"], "mailbox_id": "oid-me"}]})
    assert r.status_code == 200
    assert r.json()["errors"] == [{"account_id": ann, "msg": "the mailbox has another source"}]
    assert client.get(f"/api/accounts/{ann}").json()["source"]["config"]["tenant_id"] == "fabrikam"


def test_link_is_idempotent_and_keeps_the_namespace_of_a_same_tenant_account(client, graph):
    from app.connectors import wiring
    from app.server import db
    c = new_conn(client)
    sid, ids = yeti(client)
    me = ids["me@contoso.test"]
    client.put(f"/api/accounts/{me}/source", json={"kind": "m365_graph", "config": {
        "tenant_id": "CONTOSO", "client_id": "old-app", "mailbox": "me@contoso.test", "include_tasks": False},
        "secrets": {"client_secret": "own"}})
    acc = lambda: db.q1("SELECT * FROM accounts WHERE id=?", (me,))  # noqa: E731
    before = wiring.items_key_of_account(acc())
    db.ex("INSERT INTO items(account_id, pst_id, pst_key, kind, item_key, status, updated_at) VALUES (?,0,?,?,?,?,1)",
          (me, before, "mail", "m1", "ok"))
    rows = client.get(f"/api/source-connections/{c['id']}/match?server_id={sid}").json()["rows"]
    assert next(x for x in rows if x["account"] and x["account"]["id"] == me)["state"] == "ready"
    link = {"server_id": sid, "links": [{"account_id": me, "mailbox_id": "oid-me"}]}
    for _ in range(2):
        r = client.post(f"/api/source-connections/{c['id']}/link", json=link)
        assert r.status_code == 200 and r.json()["linked"] == [me], r.text
        assert db.jload(acc()["source_config"]) == {"mailbox": "me@contoso.test", "include_tasks": False}
        assert wiring.items_key_of_account(acc()) == before
    assert db.q1("SELECT COUNT(*) n FROM items WHERE account_id=?", (me,))["n"] == 1


def test_link_include_tasks_default_follows_the_last_test(client, graph):
    from app.server import db
    graph.roles = [x for x in ROLES if x != "Tasks.Read.All"]
    c = new_conn(client)
    client.post(f"/api/source-connections/{c['id']}/test")
    sid, ids = yeti(client)
    client.post(f"/api/source-connections/{c['id']}/link",
                json={"server_id": sid, "links": [{"account_id": ids["ann@contoso.test"], "mailbox_id": "oid-ann"}]})
    cfg = db.jload(db.q1("SELECT source_config FROM accounts WHERE id=?", (ids["ann@contoso.test"],))["source_config"])
    assert cfg == {"mailbox": "oid-ann", "include_tasks": False}


def test_linked_account_migrates_every_kind(client, graph, collect):  # noqa: F811
    from tests.server.test_connectors import log_of, run_job
    seed_graph(graph)
    c = new_conn(client)
    sid, ids = yeti(client, ("me@contoso.test",))
    me = ids["me@contoso.test"]
    assert client.put(f"/api/accounts/{me}/target", json={"kind": "collect"}).status_code == 200
    r = client.post(f"/api/source-connections/{c['id']}/link", json={
        "server_id": sid, "links": [{"account_id": me, "mailbox_id": "oid-me"}], "include_tasks": True, "start": True})
    jid = r.json()["jobs"][0]["id"]
    j = run_job(client, jid)
    assert j["status"] == "done", log_of(client, jid)
    assert {i.kind for _f, i in collect} == {"mail", "calendar", "contacts", "tasks"}
    assert SECRET not in log_of(client, jid)


def test_manual_destination_lasts_and_goes_back_to_automatic(client, graph):
    from app.connectors import wiring
    from app.server import db
    c = new_conn(client)
    sid, ids = yeti(client)
    url = f"/api/source-connections/{c['id']}"
    rows = lambda: {(x["mailbox"] or {}).get("id") or x["account"]["email"]: x  # noqa: E731
                    for x in client.get(f"{url}/match?server_id={sid}").json()["rows"]}
    test = client.post("/api/accounts", json={"server_id": sid, "email": "test@yeti.test", "password": "pw"}).json()
    assert rows()["test@yeti.test"]["state"] == "no_source"
    # zed has no box of its own on the server: test@yeti.test is chosen by hand
    r = client.post(f"{url}/link", json={"server_id": sid, "links": [
        {"account_id": test["id"], "mailbox_id": "oid-zed", "manual": True}]})
    assert r.status_code == 200 and r.json()["linked"] == [test["id"]], r.text
    zed = rows()["oid-zed"]
    assert (zed["state"], zed["matched_by"], zed["account"]["email"]) == ("ready", "manual", "test@yeti.test")
    assert "test@yeti.test" not in rows()
    refreshed = client.get(f"{url}/match?server_id={sid}&refresh=1").json()
    assert next(x for x in refreshed["rows"] if (x["mailbox"] or {}).get("id") == "oid-zed")[
        "matched_by"] == "manual"
    # the job reads zed and writes into test@yeti.test (its own server and password)
    acc = db.q1("SELECT * FROM accounts WHERE id=?", (test["id"],))
    assert wiring.source_config(acc)["mailbox"] == "oid-zed" and acc["email"] == "test@yeti.test"
    # one box, one mailbox: test@yeti.test cannot also take ann
    r = client.post(f"{url}/link", json={"server_id": sid, "links": [
        {"account_id": test["id"], "mailbox_id": "oid-ann", "manual": True}]})
    assert r.status_code == 409
    assert r.json()["detail"]["errors"][0]["msg"].startswith("already the destination of zed@contoso.test")
    assert rows()["oid-ann"]["account"]["email"] == "ann@contoso.test"
    # ann linked to her own box, then moved by hand to only@yeti.test: ann lets it go
    me, ann, only = ids["me@contoso.test"], ids["ann@contoso.test"], ids["only@yeti.test"]
    assert client.post(f"{url}/link", json={"server_id": sid, "links": [{"account_id": ann, "mailbox_id": "oid-ann"}]
                                            }).status_code == 200
    assert rows()["oid-ann"]["state"] == "linked"
    db.ex("INSERT INTO jobs(account_id, status, phase, progress, mode, created_at) VALUES (?, 'running', 'mail', '{}', "
          "'full', 1)", (ann,))
    r = client.post(f"{url}/link", json={"server_id": sid, "links": [
        {"account_id": only, "mailbox_id": "oid-ann", "manual": True}]})
    assert r.status_code == 409 and "a job is active for ann@contoso.test" in r.json()["detail"]["errors"][0]["msg"]
    db.ex("UPDATE jobs SET status='done' WHERE account_id=?", (ann,))
    r = client.post(f"{url}/link", json={"server_id": sid, "links": [
        {"account_id": only, "mailbox_id": "oid-ann", "manual": True}]})
    assert r.status_code == 200, r.text
    got = rows()
    assert got["oid-ann"]["account"]["id"] == only and got["oid-ann"]["matched_by"] == "manual"
    assert got["ann@contoso.test"]["state"] == "no_source" and got["ann@contoso.test"]["account"]["free"] is True
    assert client.get(f"/api/accounts/{ann}").json()["source"]["kind"] == "pst"
    # back to automatic: the account lets the mailbox go, the address match comes back
    assert client.put(f"/api/accounts/{only}/source", json={}).status_code == 200
    got = rows()
    assert got["oid-ann"]["account"]["id"] == ann and got["oid-ann"]["state"] == "ready"
    assert got["oid-ann"]["matched_by"] == "mail" and got["only@yeti.test"]["state"] == "no_source"
    assert got["oid-me"]["account"]["id"] == me


def test_manual_link_starts_a_job_into_the_chosen_box(client, graph):
    c = new_conn(client)
    sid, ids = yeti(client, ("only@yeti.test",))
    only = ids["only@yeti.test"]
    r = client.post(f"/api/source-connections/{c['id']}/link", json={"server_id": sid, "links": [
        {"account_id": only, "mailbox_id": "oid-me", "manual": True}], "start": True})
    assert r.status_code == 200 and r.json()["jobs"][0]["account_id"] == only, r.text
    row = next(x for x in client.get(f"/api/source-connections/{c['id']}/match?server_id={sid}").json()["rows"]
               if (x["mailbox"] or {}).get("id") == "oid-me")
    assert (row["state"], row["matched_by"], row["account"]["id"]) == ("linked", "manual", only)


# ------------------------------------------------------------------ existing endpoints (additive)
def test_put_source_attach_and_detach(client, graph):
    from app.server import db
    c = new_conn(client)
    sid, ids = yeti(client, ("me@contoso.test",))
    me = ids["me@contoso.test"]
    url = f"/api/accounts/{me}/source"
    for bad in ({"tenant_id": "x", "mailbox": "oid-me"}, {"bogus": 1}):
        assert client.put(url, json={"connection_id": c["id"], "config": bad}).status_code == 422
    assert client.put(url, json={"connection_id": c["id"], "config": {"mailbox": "oid-me"},
                                 "secrets": {"client_secret": "x"}}).status_code == 422
    assert client.put(url, json={"connection_id": 999, "config": {"mailbox": "oid-me"}}).status_code == 404
    r = no_secret(client.put(url, json={"connection_id": c["id"], "config": {"mailbox": "oid-me"}}))
    assert r.status_code == 200, r.text
    assert r.json()["source"] == {"kind": "m365_graph", "config": {"mailbox": "oid-me", "include_tasks": True},
                                  "has_secret": True, "connection": {"id": c["id"], "name": "Contoso"}}
    # without connection_id: the account's own settings, the link is dropped
    r = client.put(url, json={"kind": "m365_graph", "config": GRAPH_CONFIG, "secrets": {"client_secret": "own"}})
    assert r.status_code == 200 and r.json()["source"]["connection"] is None
    assert db.q1("SELECT source_connection_id FROM accounts WHERE id=?", (me,))["source_connection_id"] is None
    assert client.get(f"/api/source-connections/{c['id']}").json()["accounts"] == 0


def test_csv_export_of_a_linked_account_and_reimport(client, graph):
    from app.server import csvaccounts, db
    c = new_conn(client)
    sid, ids = yeti(client, ("me@contoso.test",))
    me = ids["me@contoso.test"]
    client.put(f"/api/accounts/{me}/source", json={"connection_id": c["id"], "config": {"mailbox": "oid-me"}})
    text = csvaccounts.export(sid)
    assert SECRET not in text
    head, line = text.strip().split("\r\n")
    row = dict(zip(head.split(";"), line.split(";")))
    assert row["source_kind"] == "m365_graph" and row["source_tenant_id"] == "contoso"
    assert row["source_mailbox"] == "oid-me" and row["source_client_id"] == "app-1"
    # the exported file read back: the link stays
    r = client.post("/api/accounts/import-csv", data={"server_id": str(sid)},
                    files={"file": ("a.csv", io.BytesIO(text.encode()), "text/csv")})
    assert r.status_code == 200 and r.json()["errors"] == [], r.text
    assert db.q1("SELECT source_connection_id FROM accounts WHERE id=?", (me,))["source_connection_id"] == c["id"]
    # another tenant in the file: it replaces the link
    other = text.replace(";contoso;", ";fabrikam;").replace("source_cloud", "source_client_secret") \
        .replace(";global", ";s")
    r = client.post("/api/accounts/import-csv", data={"server_id": str(sid)},
                    files={"file": ("a.csv", io.BytesIO(other.encode()), "text/csv")})
    assert r.json()["errors"] == [], r.text
    a = db.q1("SELECT * FROM accounts WHERE id=?", (me,))
    assert a["source_connection_id"] is None and db.jload(a["source_config"])["tenant_id"] == "fabrikam"


def test_m365_target_keeps_its_per_mailbox_settings(client, graph):
    """tenant_id, client_id, client_secret and cloud are connection-scoped, yet a target keeps them per account."""
    from app.connectors import registry
    from app.server import db
    entry = next(x for x in client.get("/api/connectors").json() if x["kind"] == "m365_graph")
    names = {f["name"] for f in entry["target"]["fields"]}
    assert {"tenant_id", "client_id", "client_secret", "cloud", "mailbox"} <= names
    sid, ids = yeti(client, ("me@contoso.test",))
    me = ids["me@contoso.test"]
    r = no_secret(client.put(f"/api/accounts/{me}/target", json={
        "kind": "m365_graph", "config": {**GRAPH_CONFIG, "cloud": "global"}, "secrets": {"client_secret": SECRET}}))
    assert r.status_code == 200, r.text
    assert r.json()["target"]["config"]["tenant_id"] == "contoso" and r.json()["target"]["has_secret"] is True
    from app.connectors import wiring
    acc = db.q1("SELECT * FROM accounts WHERE id=?", (me,))
    target = registry.make_target("m365_graph", db.jload(acc["target_config"]), wiring.load_secrets(acc["target_secret_enc"]))
    session = target.session()
    session.connect()  # the per-account tenant and secret reach Graph
    session.close()
    assert graph.token_requests and graph.token_requests[-1]["client_secret"] == SECRET
