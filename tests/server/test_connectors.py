"""Connector interface, catalogue, secrets, migration of an existing database, and the connector route of JobRunner
against the in-memory connectors (no network service needed)."""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.connectors import imap as cimap
from app.connectors import registry
from app.connectors.base import IMPORT_HEADER, MAIL, ConnectorError, stamp_import_id, subject_of
from app.server.imap import Mailbox
from tests.server import connector_fakes as fakes
from tests.server import fake_engine
from tests.server.fake_engine import make_fake_pst

SCHEMA_100 = Path(__file__).resolve().parents[1] / "fixtures" / "schema-1.0.0.sql"
SRV = {"name": "Prod", "imap_host": "imap.example.test", "imap_port": 993, "imap_security": "ssl", "verify_tls": True,
       "dav_enabled": False, "workers": 2, "tuning": "manual", "notes": ""}
IMAP_CONFIG = {"host": "src.example.test", "login": "me@example.test"}


@pytest.fixture
def mem():
    fakes.register()
    yield fakes
    fakes.unregister()


def make_account(client, email="me@example.test", **extra):
    sid = client.post("/api/servers", json=SRV).json()["id"]
    r = client.post("/api/accounts", json={"server_id": sid, "email": email, "password": "pw", **extra})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def memory_account(client, folders=None):
    fakes.SOURCES["s"] = fakes.MemoryMailbox(folders or {"INBOX": 8, "Projects": 4})
    fakes.TARGETS["t"] = fakes.MemoryStore()
    aid = make_account(client)
    r = client.put(f"/api/accounts/{aid}/source", json={"kind": "memory", "config": {"name": "s"},
                                                        "secrets": {"token": "ok"}})
    assert r.status_code == 200, r.text
    r = client.put(f"/api/accounts/{aid}/target", json={"kind": "memory", "config": {"name": "t"}})
    assert r.status_code == 200, r.text
    return aid, fakes.SOURCES["s"], fakes.TARGETS["t"]


def start(client, aid):
    r = client.post("/api/runs", json={"account_ids": [aid]})
    assert r.status_code == 200, r.text
    return r.json()[0]["id"]


def run_job(client, jid):
    from app.server import runner
    runner.run_job(jid, "test:inproc")
    return client.get(f"/api/jobs/{jid}").json()


def log_of(client, jid):
    return "\n".join(x["msg"] for x in client.get(f"/api/jobs/{jid}/log").json()["lines"])


# ------------------------------------------------------------------ interface
def test_error_kinds():
    assert ConnectorError("x", ConnectorError.TRANSIENT).retryable
    assert ConnectorError("x", ConnectorError.UNREACHABLE).retryable
    for k in (ConnectorError.AUTH, ConnectorError.CONFIG, ConnectorError.REJECTED, ConnectorError.QUOTA):
        assert not ConnectorError("x", k).retryable
    assert ConnectorError("x", ConnectorError.TRANSIENT).result_kind == "temp"
    assert ConnectorError("x", ConnectorError.REJECTED).result_kind == "perm"
    assert ConnectorError("x", ConnectorError.QUOTA).result_kind == "fatal"
    assert ConnectorError("x", ConnectorError.AUTH).result_kind == "auth"


def test_import_header_is_the_runner_one():
    from app.server import builders, runner
    assert IMPORT_HEADER == runner.IMPORT_HEADER == builders.IMPORT_HEADER


def test_stamp_import_id():
    crlf = b"Subject: x\r\n\r\nbody\r\n"
    out = stamp_import_id(crlf, "<ns-1>")
    assert out == b"X-PST-Import-Id: <ns-1>\r\n" + crlf
    assert stamp_import_id(out, "<ns-1>") == out
    assert stamp_import_id(b"Subject: x\n\nbody\n", "<ns-1>").startswith(b"X-PST-Import-Id: <ns-1>\nSubject")
    again = stamp_import_id(out, "<ns-2>")  # imported earlier from another source: both stay
    assert again.startswith(b"X-PST-Import-Id: <ns-2>\r\nX-PST-Import-Id: <ns-1>\r\n")


def test_subject_of():
    assert subject_of(b"Subject: =?utf-8?b?w6l0w6k=?=\r\n\r\n") == "été"
    assert subject_of(b"\r\nno header") == ""


def test_namespace_follows_the_mailbox_not_the_password():
    a = registry.namespace("imap", "source", {"host": "H.example", "port": 993, "login": "x"})
    assert a == registry.namespace("imap", "source", {"host": "h.example", "port": 993, "login": "x",
                                                      "verify_tls": False})
    assert a != registry.namespace("imap", "source", {"host": "h.example", "port": 993, "login": "y"})
    assert a.startswith("src-")
    assert not cimap.ImapSource({"host": "h"}, {"password": "pw"}).native_code


def test_catalogue_declares_every_builtin():
    cat = {c["kind"]: c for c in registry.catalogue()}
    assert {"imap", "pst", "caldav", "carddav"} <= set(cat)
    assert cat["imap"]["source"]["items"] == ["mail"] and cat["imap"]["target"]["items"] == ["mail"]
    assert "incremental" in cat["imap"]["source"]["capabilities"]
    assert cat["caldav"]["source"] is None and cat["pst"]["target"] is None
    pw = [f for f in cat["imap"]["source"]["fields"] if f["name"] == "password"][0]
    assert pw["secret"] is True and pw["type"] == "password"
    assert [f for f in cat["imap"]["source"]["fields"] if f["name"] == "security"][0]["options"]


def test_normalize_config():
    role = registry.role_of("imap", "source")
    out = registry.normalize_config(role, {"host": " h ", "login": "l", "port": "143", "verify_tls": False})
    assert out == {"host": "h", "login": "l", "port": 143, "security": "ssl", "verify_tls": False}
    for bad in ({"login": "l"}, {"host": "h", "login": "l", "nope": 1}, {"host": "h", "login": "l", "password": "x"},
                {"host": "h", "login": "l", "security": "rot13"}, {"host": "h", "login": "l", "port": "abc"}):
        with pytest.raises(ConnectorError) as e:
            registry.normalize_config(role, bad)
        assert e.value.kind == ConnectorError.CONFIG


def test_merge_secrets():
    role = registry.role_of("imap", "source")
    assert registry.merge_secrets(role, {"password": "a"}, None) == {"password": "a"}
    assert registry.merge_secrets(role, {"password": "a"}, {"password": "b"}) == {"password": "b"}
    with pytest.raises(ConnectorError):
        registry.merge_secrets(role, {"password": "a"}, {})  # required by this connector
    with pytest.raises(ConnectorError):
        registry.merge_secrets(role, {}, None)
    with pytest.raises(ConnectorError):
        registry.merge_secrets(role, {"password": "a"}, {"other": "b"})


# ------------------------------------------------------------------ IMAP source, without a server
def test_parse_fetch_attribute_orders():
    data = [
        (b'1 (UID 7 FLAGS (\\Seen \\Recent) INTERNALDATE " 1-Mar-2024 12:00:00 +0100" RFC822.SIZE 42 '
         b'BODY[HEADER.FIELDS (MESSAGE-ID)] {27}', b"Message-ID: <a@b>\r\n\r\n"), b")",
        (b'2 (FLAGS () BODY[HEADER.FIELDS (MESSAGE-ID)] {2}', b"\r\n"), b' UID 9 RFC822.SIZE 5)',
        b'3 (UID 11 FLAGS (\\Deleted) BODY[HEADER.FIELDS (MESSAGE-ID)] "")',
        b"4 (FLAGS (\\Seen))",  # unsolicited: no UID
        None,
    ]
    recs = cimap.parse_fetch(data)
    assert [r["uid"] for r in recs] == [7, 9, 11]
    assert recs[0]["message_id"] == "<a@b>" and recs[0]["size"] == 42
    assert recs[0]["date"] == datetime(2024, 3, 1, 11, 0, tzinfo=timezone.utc)
    assert recs[1]["message_id"] is None and recs[1]["size"] == 5 and recs[1]["date"] is None
    assert recs[2]["flags"] == ["\\Deleted"]


def test_parse_internaldate():
    assert cimap.parse_internaldate("17-Jul-1996 02:44:25 -0700") == datetime(
        1996, 7, 17, 2, 44, 25, tzinfo=timezone(-timedelta(hours=7)))
    assert cimap.parse_internaldate("31-Foo-2024 00:00:00 +0000") is None
    assert cimap.parse_internaldate("") is None


def test_keep_flags():
    assert cimap.keep_flags(["\\Seen", "\\Recent", "\\Deleted", "$Forwarded", "bad(kw", "\\Flagged"]) == [
        "\\Seen", "$Forwarded", "\\Flagged"]


def test_item_keys():
    recs = [{"uid": 3, "message_id": "<a>", "date": None, "size": 1}, {"uid": 4, "message_id": "<a>", "date": None, "size": 1},
            {"uid": 5, "message_id": None, "date": None, "size": 9}]
    fh = cimap.folder_hash(["INBOX"])
    assert cimap.item_keys(["INBOX"], 77, recs) == [f"{fh}-77-3", f"{fh}-77-4", f"{fh}-77-5"]
    other = cimap.item_keys(["Other"], 77, recs)
    assert other[0] != f"{fh}-77-3"  # same UIDVALIDITY and UID in two mailboxes
    no_uv = cimap.item_keys(["INBOX"], 0, recs)
    assert len(set(no_uv)) == 3 and no_uv[1] == no_uv[0] + "-2"  # Message-ID fallback, identical ones told apart
    assert no_uv == cimap.item_keys(["INBOX"], 0, recs)  # stable
    assert cimap.alt_key_of(recs[0]).startswith("m:") and cimap.alt_key_of(recs[2]).startswith("d:")


def box(name, *flags, delim="/"):
    return Mailbox(name=name, flags=list(flags), delim=delim)


def test_folders_with_special_use():
    out = {tuple(f.path): f for f in cimap.folders_of(
        [box("INBOX"), box("Envoy"), box("Archive/2024"), box("Sent Mail", "\\Sent"), box("Sent"), box("All", "\\All"),
         box("[Gmail]", "\\Noselect"), box("Bin", "\\Trash"), box("Spam", "\\Junk")], "/", "")}
    assert out[("INBOX",)].special == "inbox"
    assert out[("Sent Mail",)].special == "sent"
    assert out[("Sent",)].special is None  # the flag already gave this role
    assert out[("Bin",)].special == "trash" and out[("Spam",)].special == "junk"
    assert out[("Archive", "2024")].special is None
    assert ("All",) not in out and ("[Gmail]",) not in out  # virtual, unselectable


def test_folders_without_special_use_and_with_prefix():
    out = {tuple(f.path): f for f in cimap.folders_of(
        [box("INBOX", delim="."), box("INBOX.Sent", delim="."), box("INBOX.Corbeille", delim="."),
         box("INBOX.Junk", delim="."), box("INBOX.Work.Sent", delim=".")], ".", "INBOX.")}
    assert out[("INBOX",)].special == "inbox"
    assert out[("Sent",)].special == "sent" and out[("Corbeille",)].special == "trash"
    assert out[("Junk",)].special == "junk"
    assert out[("Work", "Sent")].special is None  # nested: an ordinary folder
    assert all(f.kind == MAIL for f in out.values())
    assert out[("Sent",)].ref == "INBOX.Sent"  # the real mailbox name


# ------------------------------------------------------------------ API
def test_api_catalogue(client):
    r = client.get("/api/connectors")
    assert r.status_code == 200
    assert "imap" in {c["kind"] for c in r.json()}
    client.post("/api/logout")
    assert client.get("/api/connectors").status_code == 401


def test_api_source_secret_never_round_trips(client, env):
    from app.server import db
    aid = make_account(client)
    a = client.get(f"/api/accounts/{aid}").json()
    assert a["source"] == {"kind": "pst", "config": {"pst_id": None}, "has_secret": False}
    assert a["target"]["kind"] is None
    r = client.put(f"/api/accounts/{aid}/source", json={"kind": "imap", "config": IMAP_CONFIG,
                                                        "secrets": {"password": "sécret-src"}})
    assert r.status_code == 200, r.text
    src = r.json()["source"]
    assert src["kind"] == "imap" and src["has_secret"] is True and src["config"]["host"] == "src.example.test"
    assert "sécret-src" not in r.text
    assert "sécret-src" not in client.get(f"/api/accounts/{aid}/source").text
    row = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
    assert "secret" not in row["source_secret_enc"] and "password" not in (row["source_config"] or "")
    from app.connectors import wiring
    assert wiring.load_secrets(row["source_secret_enc"]) == {"password": "sécret-src"}
    # omitted on PUT: kept
    r = client.put(f"/api/accounts/{aid}/source", json={"kind": "imap", "config": {**IMAP_CONFIG, "port": 143}})
    assert r.status_code == 200 and r.json()["source"]["has_secret"] is True
    assert r.json()["source"]["config"]["port"] == 143
    row = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
    assert wiring.load_secrets(row["source_secret_enc"]) == {"password": "sécret-src"}
    # replaced
    assert client.put(f"/api/accounts/{aid}/source", json={"kind": "imap", "config": IMAP_CONFIG,
                                                           "secrets": {"password": "new"}}).status_code == 200
    row = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
    assert wiring.load_secrets(row["source_secret_enc"]) == {"password": "new"}
    # back to a PST: secret and config gone
    r = client.put(f"/api/accounts/{aid}/source", json={"kind": "pst"})
    assert r.json()["source"]["kind"] == "pst"
    row = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
    assert row["source_secret_enc"] is None and row["source_config"] is None


def test_api_source_refusals(client):
    aid = make_account(client)
    url = f"/api/accounts/{aid}/source"
    assert client.put(url, json={"kind": "nope", "config": {}}).status_code == 422
    assert client.put(url, json={"kind": "caldav", "config": {}}).status_code == 422  # target only
    assert client.put(url, json={"kind": "imap", "config": IMAP_CONFIG}).status_code == 422  # password required
    assert client.put(url, json={"kind": "imap", "config": {**IMAP_CONFIG, "password": "x"},
                                 "secrets": {"password": "x"}}).status_code == 422  # secret in clear
    assert client.put(url, json={"kind": "pst", "config": {"pst_id": 999}}).status_code == 404
    assert client.get("/api/accounts/999/source").status_code == 404
    assert client.post("/api/connectors/nope/test", json={}).status_code == 404


def test_api_secret_of_another_kind_is_not_reused(client, mem):
    aid = make_account(client)
    url = f"/api/accounts/{aid}/source"
    fakes.SOURCES["s"] = fakes.MemoryMailbox({})
    assert client.put(url, json={"kind": "imap", "config": IMAP_CONFIG, "secrets": {"password": "x"}}).status_code == 200
    assert client.put(url, json={"kind": "memory", "config": {"name": "s"}}).status_code == 422  # token required


def test_api_refuses_changes_while_a_job_is_active(client, mem):
    aid, _s, _t = memory_account(client)
    start(client, aid)
    r = client.put(f"/api/accounts/{aid}/source", json={"kind": "pst"})
    assert r.status_code == 409
    assert client.put(f"/api/accounts/{aid}/target", json={"kind": None}).status_code == 409


def test_api_test_endpoint(client, mem):
    fakes.SOURCES["s"] = fakes.MemoryMailbox({"INBOX": 3})
    r = client.post("/api/connectors/memory/test", json={"config": {"name": "s"}, "secrets": {"token": "ok"}}).json()
    assert r["ok"] is True and r["folders"] == [{"path": ["INBOX"], "kind": "mail", "special": "inbox", "count": 3}]
    r = client.post("/api/connectors/memory/test", json={"config": {"name": "s"}, "secrets": {"token": "no"}}).json()
    assert r["ok"] is False and r["error_kind"] == "auth"
    # stored secret reused, never returned
    aid, _s, _t = memory_account(client)
    r = client.post("/api/connectors/memory/test", json={"config": {"name": "s"}, "account_id": aid})
    assert r.json()["ok"] is True and "token" not in r.text
    fakes.TARGETS["t"] = fakes.MemoryStore()
    r = client.post("/api/connectors/memory/test", json={"role": "target", "config": {"name": "t"}}).json()
    assert r["ok"] is True


def test_runs_refuses_an_account_without_a_usable_source(client, env):
    from app.server import db
    aid = make_account(client)
    r = client.post("/api/runs", json={"account_ids": [aid]})
    assert r.status_code == 422 and "no valid PST linked" in r.text
    db.ex("UPDATE accounts SET source_kind='gone', source_config='{}' WHERE id=?", (aid,))
    r = client.post("/api/runs", json={"account_ids": [aid]})
    assert r.status_code == 422 and "unknown source connector" in r.text


# ------------------------------------------------------------------ migration
def test_migration_of_an_existing_database(env):
    """A 1.0.0 database: its accounts become PST sources, nothing else changes."""
    from app.connectors import wiring
    from app.server import db, repo
    env.db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(env.db_path)
    c.executescript(SCHEMA_100.read_text())
    c.execute("INSERT INTO servers(id,name,imap_host,created_at) VALUES (1,'s','h',1)")
    c.execute("INSERT INTO psts(id,filename,size,chunk_size,status,path,pst_sha,created_at,updated_at) "
              "VALUES (5,'a.pst',1,1,'valid','/x','cafe',1,1)")
    c.execute("INSERT INTO accounts(id,server_id,email,login,password_enc,pst_id,created_at) "
              "VALUES (1,1,'a@example.test','','tok',5,1)")
    c.commit()
    c.close()
    db.reset()
    cols = {r[1] for r in db.q("PRAGMA table_info(accounts)")}
    assert {"source_kind", "source_config", "source_secret_enc", "target_kind", "target_config",
            "target_secret_enc"} <= cols
    assert "source_ref" in {r[1] for r in db.q("PRAGMA table_info(items)")}
    r = db.q1("SELECT * FROM accounts WHERE id=1")
    assert r["pst_id"] == 5 and r["password_enc"] == "tok" and r["source_kind"] is None
    assert repo.source_view(r) == {"kind": "pst", "config": {"pst_id": 5}, "has_secret": False}
    assert wiring.uses_connector_route(r) is False
    assert wiring.items_key_of_account(r) == "cafe"
    assert wiring.source_problem(r) is None
    db.reset()  # second start: nothing to do, nothing lost
    assert db.q1("SELECT pst_id FROM accounts WHERE id=1")["pst_id"] == 5


def test_fresh_schema_has_the_same_columns_as_a_migrated_one(env):
    from app.server import db
    assert "source_kind" in {r[1] for r in db.q("PRAGMA table_info(accounts)")}
    assert "source_ref" in {r[1] for r in db.q("PRAGMA table_info(items)")}


# ------------------------------------------------------------------ the route, in memory
def test_full_run(client, mem):
    aid, box_, store = memory_account(client)
    jid = start(client, aid)
    j = run_job(client, jid)
    assert j["status"] == "done", log_of(client, jid)
    assert j["progress"]["mail"] == {"done": 12, "total": 12, "errors": 0}
    copies = store.copies()
    assert len(copies) == 12 and set(copies.values()) == {1}
    assert all(k.startswith("<src-") and k.endswith(">") for k in copies)
    first = store.items[0]
    assert first[2].startswith(f"{IMPORT_HEADER}: {first[1]}\r\n".encode())
    assert first[4].tzinfo is not None
    assert {i[0] for i in store.items} == {"INBOX", "Projects"}
    rep = client.get(f"/api/jobs/{jid}/report").json()
    assert rep["status"] == "done" and rep["totals"]["mail"]["sent"] == 12 and rep["mail"]["present"] == 12
    assert rep["source"]["kind"] == "memory"
    assert rep["verify"]["sample"]["problems"] == []
    assert j["tuning"]["builders"] == 0
    assert client.get(f"/api/jobs/{jid}/errors").json() == []


def test_newest_first_across_folders(client, mem):
    from app.server import db
    aid, _b, store = memory_account(client)
    db.ex("UPDATE servers SET workers=1")  # one connection: the order of the writes is strict
    assert run_job(client, start(client, aid))["status"] == "done"
    dates = [i[4] for i in store.items]
    assert len(dates) == 12 and dates == sorted(dates, reverse=True)


def test_rerun_sends_nothing_again(client, mem):
    aid, box_, store = memory_account(client)
    jid = start(client, aid)
    assert run_job(client, jid)["status"] == "done"
    n = len(box_.fetches)
    assert client.post(f"/api/jobs/{jid}/retry").status_code == 200
    assert run_job(client, jid)["status"] == "done"
    assert len(box_.fetches) == n and len(store.items) == 12


def test_pause_then_resume_without_duplicates(client, mem):
    from app.server import db
    aid, box_, store = memory_account(client, {"INBOX": 30})
    jid = start(client, aid)
    calls = []

    def pause_after_five(item):
        calls.append(item.import_id)
        if len(calls) == 5:
            db.ex("UPDATE jobs SET status='paused' WHERE id=?", (jid,))

    store.on_put = pause_after_five
    j = run_job(client, jid)
    assert j["status"] == "paused", log_of(client, jid)
    assert 5 <= len(store.items) < 30
    store.on_put = None
    assert client.post(f"/api/jobs/{jid}/resume").status_code == 200
    j = run_job(client, jid)
    assert j["status"] == "done", log_of(client, jid)
    copies = store.copies()
    assert len(copies) == 30 and set(copies.values()) == {1}


def test_stop_then_retry_without_duplicates(client, mem):
    from app.server import db
    aid, _b, store = memory_account(client, {"INBOX": 20})
    jid = start(client, aid)
    store.on_put = lambda item: db.ex("UPDATE jobs SET status='stopping' WHERE id=?", (jid,))
    assert run_job(client, jid)["status"] == "stopped"
    store.on_put = None
    assert client.post(f"/api/jobs/{jid}/retry").status_code == 200
    assert run_job(client, jid)["status"] == "done"
    copies = store.copies()
    assert len(copies) == 20 and set(copies.values()) == {1}


def test_inflight_item_found_on_the_target_is_not_sent_again(client, mem):
    """A worker killed after the write but before the answer: the row stays `inflight`."""
    from app.server import db
    aid, _b, store = memory_account(client)
    jid = start(client, aid)
    assert run_job(client, jid)["status"] == "done"
    row = db.q("SELECT * FROM items WHERE account_id=? ORDER BY id LIMIT 2", (aid,))
    db.ex("UPDATE items SET status='inflight' WHERE id=?", (row[0]["id"],))  # written
    db.ex("UPDATE items SET status='inflight' WHERE id=?", (row[1]["id"],))  # not written after all
    lost = f"<{row[1]['pst_key']}-{row[1]['item_key']}>"
    with store.lock:
        store.items = [i for i in store.items if i[1] != lost]
    assert client.post(f"/api/jobs/{jid}/retry").status_code == 200
    assert run_job(client, jid)["status"] == "done", log_of(client, jid)
    copies = store.copies()
    assert len(copies) == 12 and set(copies.values()) == {1}


def test_lost_answer_is_not_written_twice(client, mem, monkeypatch):
    from app.server import connector_runner
    aid, _b, store = memory_account(client, {"INBOX": 3})
    real = connector_runner.TargetPool.__init__
    monkeypatch.setattr(connector_runner.TargetPool, "__init__",
                        lambda self, target, n, stop: real(self, target, n, stop, base_delay=0.01))
    from app.connectors import wiring
    from app.server import db, runner
    acc = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
    ns = wiring.items_key_of_account(acc)
    key = fakes.SOURCES["s"].key("INBOX", 2)
    store.lose_response.add(runner.import_id_of(ns, key))
    jid = start(client, aid)
    assert run_job(client, jid)["status"] == "done", log_of(client, jid)
    copies = store.copies()
    assert len(copies) == 3 and set(copies.values()) == {1}


def test_uidvalidity_change_does_not_duplicate(client, mem):
    """Every key changes (new UIDVALIDITY): the alt keys recognize what was already migrated."""
    aid, box_, store = memory_account(client, {"INBOX": 6})
    jid = start(client, aid)
    assert run_job(client, jid)["status"] == "done"
    box_.generation = 2
    box_.folders["INBOX"] = 7  # and one new message
    assert client.post(f"/api/jobs/{jid}/retry").status_code == 200
    j = run_job(client, jid)
    assert j["status"] == "done", log_of(client, jid)
    assert len(store.items) == 7
    assert j["progress"]["mail"]["done"] == 7
    assert sum(b"Subject: INBOX 6\r\n" in i[2] for i in store.items) == 1


def test_fetch_error_is_per_item(client, mem):
    aid, box_, store = memory_account(client, {"INBOX": 4})
    box_.fetch_errors[box_.key("INBOX", 1)] = ConnectorError("too big", ConnectorError.REJECTED)
    jid = start(client, aid)
    j = run_job(client, jid)
    assert j["status"] == "done_with_errors" and len(store.items) == 3
    errs = client.get(f"/api/jobs/{jid}/errors").json()
    assert len(errs) == 1 and "too big" in errs[0]["error"]
    box_.fetch_errors.clear()
    assert client.post(f"/api/jobs/{jid}/retry-errors").status_code == 200
    assert run_job(client, jid)["status"] == "done"
    assert len(store.items) == 4 and set(store.copies().values()) == {1}


def test_source_down_fails_the_job_instead_of_erring_every_item(client, mem, monkeypatch):
    from app.server import connector_runner
    monkeypatch.setattr(connector_runner.settings, "max_retries", 0)
    aid, box_, store = memory_account(client, {"INBOX": 20})
    for i in range(20):
        box_.fetch_errors[box_.key("INBOX", i)] = ConnectorError("gone", ConnectorError.UNREACHABLE)
    jid = start(client, aid)
    j = run_job(client, jid)
    assert j["status"] == "failed" and "unavailable" in j["error"]
    assert len(box_.fetches) == connector_runner.MAX_SOURCE_ERRORS


def test_source_auth_failure_fails_the_job(client, mem):
    from app.connectors import wiring
    from app.server import db
    aid, _b, store = memory_account(client)
    db.ex("UPDATE accounts SET source_secret_enc=? WHERE id=?", (wiring.dump_secrets({"token": "no"}), aid))
    jid = start(client, aid)
    j = run_job(client, jid)
    assert j["status"] == "failed" and "bad token" in j["error"]
    assert store.items == []


def test_state_is_per_source(client, mem):
    """Back to a source already migrated: nothing sent again. A new source starts afresh."""
    aid, box_, store = memory_account(client, {"INBOX": 3})
    jid = start(client, aid)
    assert run_job(client, jid)["status"] == "done"
    fakes.SOURCES["s2"] = fakes.MemoryMailbox({"INBOX": 2})
    assert client.put(f"/api/accounts/{aid}/source", json={"kind": "memory", "config": {"name": "s2"},
                                                           "secrets": {"token": "ok"}}).status_code == 200
    jid = start(client, aid)
    assert run_job(client, jid)["status"] == "done"
    assert len(store.items) == 5
    assert client.put(f"/api/accounts/{aid}/source", json={"kind": "memory", "config": {"name": "s"},
                                                           "secrets": {"token": "ok"}}).status_code == 200
    jid = start(client, aid)
    assert run_job(client, jid)["status"] == "done"
    assert len(store.items) == 5


def test_changing_the_target_sends_everything_again(client, mem):
    aid, _b, store = memory_account(client, {"INBOX": 3})
    assert run_job(client, start(client, aid))["status"] == "done"
    fakes.TARGETS["t2"] = fakes.MemoryStore()
    assert client.put(f"/api/accounts/{aid}/target", json={"kind": "memory", "config": {"name": "t2"}}).status_code == 200
    assert run_job(client, start(client, aid))["status"] == "done"
    assert len(fakes.TARGETS["t2"].items) == 3


def test_pst_as_a_source_to_another_target(client, env, mem):
    """The PST wrapper: same keys as the historical path, header embedded by the engine."""
    import hashlib

    from app.server.uploads import validate
    folders = [(["Inbox"], "mail", "IPF.Note", "inbox", 4), (["Sub"], "mail", "IPF.Note", None, 2),
               (["Calendar"], "calendar", "IPF.Appointment", None, 1)]
    old = list(fake_engine.CONFIG["folders"])
    fake_engine.CONFIG.update(folders=folders, fail_keys={fake_engine.item_key(["Inbox"], 1)}, reject_keys=set(),
                              unreadable=set(), delay=0.0, dates={})
    try:
        data = make_fake_pst(str(env.data_dir / "src.pst"), 200_000)
        up = client.post("/api/uploads", json={"filename": "a.pst", "size": len(data), "last_modified": 1}).json()
        cs = up["chunk_size"]
        for n in range(up["chunks"]):
            c = data[n * cs:(n + 1) * cs]
            assert client.put(f"/api/uploads/{up['id']}/chunks/{n}", content=c,
                              headers={"X-Chunk-Sha256": hashlib.sha256(c).hexdigest()}).status_code == 200
        client.post(f"/api/uploads/{up['id']}/complete")
        validate(up["id"])
        fakes.TARGETS["t"] = fakes.MemoryStore()
        aid = make_account(client, pst_id=up["id"])
        r = client.put(f"/api/accounts/{aid}/target", json={"kind": "memory", "config": {"name": "t"}})
        assert r.status_code == 200, r.text
        jid = start(client, aid)
        j = run_job(client, jid)
        assert j["status"] == "done_with_errors", log_of(client, jid)  # one corrupted item
        store = fakes.TARGETS["t"]
        assert len(store.items) == 5 and set(store.copies().values()) == {1}
        sha = client.get(f"/api/psts/{up['id']}").json()["pst_sha"]
        assert all(i[1].startswith(f"<{sha}-") for i in store.items)
        assert all(i[2].count(b"X-PST-Import-Id:") == 1 for i in store.items)
        assert j["progress"]["mail"]["errors"] == 1
        assert "no target for them" in log_of(client, jid)  # calendar: no DAV on this server
    finally:
        fake_engine.CONFIG.update(folders=old, fail_keys=set())


def test_pst_account_without_target_keeps_the_historical_runner(client, env, monkeypatch):
    from app.server import db, runner
    aid = make_account(client)
    cur = db.ex("INSERT INTO jobs(account_id, status, phase, progress, created_at) VALUES (?, 'queued', 'precheck', '{}', 1)",
                (aid,))
    seen = []
    monkeypatch.setattr(runner.JobRunner, "run", lambda self: seen.append(type(self).__name__))
    runner.run_job(cur.lastrowid, "t")
    assert seen == ["JobRunner"]
