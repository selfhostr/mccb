"""IMAP to IMAP against GreenMail (docker-compose.test.yml): two users, through the API and the real runner."""
import email
import email.policy
import re
from datetime import datetime, timezone

import pytest

from app.connectors.imap import ImapSource
from app.server.imap import ImapConn, ImapError, utf7_encode
from tests.server.test_idempotence import run_inproc
from tests.server.test_runner import (DAV_URL, IMAP_HOST, IMAP_PORT, _log, imap_conn, run, server_import_ids,
                                      wait_services)  # noqa: F401 - wait_services: autouse fixture of this module
from tests.svcusers import create_user

pytestmark = pytest.mark.skipif(not IMAP_HOST or not DAV_URL, reason="TEST_IMAP_HOST / TEST_DAV_URL not set")


def message(i: int, folder: str, mid: bool = True) -> bytes:
    head = f"From: Hélène <h@example.test>\r\nTo: b@example.test\r\nSubject: {folder} {i}\r\n"
    if mid:
        head += f"Message-ID: <{folder}.{i}@example.test>\r\n"
    return (head + "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            + f"Corps accentué {i}\r\n").encode("utf-8")


def seed(email_addr: str, n_inbox: int = 6) -> int:
    """INBOX, a nested folder, a Sent folder with no SPECIAL-USE, and a deleted message that must not be copied."""
    c = imap_conn(email_addr)
    try:
        d = c.delimiter()
        nested = f"Projets{d}Été"
        names = {"INBOX": "INBOX", "Sent": "Sent", nested: utf7_encode(nested)}
        c.create("Projets")
        c.create(names[nested])
        c.create("Sent")
        when = datetime(2023, 5, 17, 8, 30, 0, tzinfo=timezone.utc)
        for i in range(n_inbox):
            c.append("INBOX", message(i, "INBOX"), ["\\Seen"] if i % 2 else [], when)
        for i in range(2):
            c.append(names[nested], message(i, "ete", mid=i == 0), ["\\Flagged"], when)
        c.append("Sent", message(0, "sent"), ["\\Seen"], when)
        return n_inbox + 3
    finally:
        c.close()


def setup_route(client, workers: int = 1, root_folder=None):
    src, dst = create_user(prefix="src"), create_user(prefix="dst")
    sid = client.post("/api/servers", json={
        "name": "dst", "imap_host": IMAP_HOST, "imap_port": IMAP_PORT, "imap_security": "none", "verify_tls": False,
        "root_folder": root_folder, "dav_enabled": False, "workers": workers, "tuning": "manual", "notes": ""}).json()["id"]
    aid = client.post("/api/accounts", json={"server_id": sid, "email": dst, "password": "pw"}).json()["id"]
    r = client.put(f"/api/accounts/{aid}/source", json={
        "kind": "imap",
        "config": {"host": IMAP_HOST, "port": IMAP_PORT, "security": "none", "verify_tls": False, "login": src},
        "secrets": {"password": "pw"}})
    assert r.status_code == 200, r.text
    return aid, src, dst


def messages_of(email_addr: str) -> dict[str, list[dict]]:
    """{folder: [{subject, flags, date, import_id, body}]}."""
    c = imap_conn(email_addr)
    out: dict[str, list[dict]] = {}
    try:
        for b in c.list():
            if "\\Noselect" in b.flags or not c.select(b.name):
                continue
            typ, data = c.M.fetch("1:*", "(FLAGS INTERNALDATE BODY.PEEK[])")
            for i, d in enumerate(data):
                if not isinstance(d, tuple):
                    continue
                # attributes may follow the literal; \\Recent depends on who selected the folder before
                tail = data[i + 1] if i + 1 < len(data) and isinstance(data[i + 1], bytes) else b""
                meta = (d[0] + b" " + tail).decode()
                m = email.message_from_bytes(d[1], policy=email.policy.default)
                out.setdefault(b.decoded, []).append({
                    "subject": str(m["Subject"]), "import_id": m.get("X-PST-Import-Id"),
                    "flags": sorted(set(re.findall(r"\\\w+", meta)) - {"\\Recent"}),
                    "date": re.search(r'INTERNALDATE "([^"]+)"', meta).group(1), "body": m.get_content().strip()})
    finally:
        c.close()
    return out


def test_imap_to_imap(client, env):
    aid, src, dst = setup_route(client)
    n = seed(src)
    before = messages_of(src)
    jid = run(client, aid)
    j = run_inproc(client, jid)
    assert j["status"] == "done", _log(client, jid)
    assert j["progress"]["mail"] == {"done": n, "total": n, "errors": 0}
    ids = server_import_ids(dst)
    assert len(ids) == n and set(ids.values()) == {1}
    got = messages_of(dst)
    assert len(got["INBOX"]) == 6
    assert {m["subject"] for m in got["INBOX"]} == {f"INBOX {i}" for i in range(6)}
    assert any("ete 0" == m["subject"] for f, ms in got.items() if f.endswith("Été") for m in ms)
    assert len(got["Sent"]) == 1
    assert got["INBOX"][0]["body"].startswith("Corps accentué")
    seen = {m["subject"]: m["flags"] for m in got["INBOX"]}
    assert "\\Seen" in seen["INBOX 1"] and "\\Seen" not in seen["INBOX 0"]
    assert all("17-May-2023" in m["date"] for m in got["INBOX"])  # INTERNALDATE kept
    assert messages_of(src) == before  # flags and content of the source untouched
    rep = client.get(f"/api/jobs/{jid}/report").json()
    assert rep["verify"]["problems"] == [] and rep["verify"]["sample"]["ok"] == rep["verify"]["sample"]["checked"] > 0
    assert rep["totals"]["mail"]["present"] == n


def test_root_folder(client, env):
    aid, src, dst = setup_route(client, root_folder="Old")
    n = seed(src)
    jid = run(client, aid)
    assert run_inproc(client, jid)["status"] == "done", _log(client, jid)
    got = messages_of(dst)
    assert sum(len(v) for k, v in got.items() if k.startswith("Old")) == n and "INBOX" not in got


def test_pause_resume_without_duplicates(client, env, monkeypatch):
    from app.server import db
    aid, src, dst = setup_route(client)
    n = seed(src, 14)
    jid = run(client, aid)
    real, calls = ImapConn.append, []

    def pausing(self, name, raw, flags, dt):
        uid = real(self, name, raw, flags, dt)
        calls.append(name)
        if len(calls) == 4:
            db.ex("UPDATE jobs SET status='paused' WHERE id=?", (jid,))
        return uid

    monkeypatch.setattr(ImapConn, "append", pausing)
    j = run_inproc(client, jid)
    assert j["status"] == "paused", _log(client, jid)
    partial = server_import_ids(dst)
    assert 4 <= len(partial) < n
    monkeypatch.setattr(ImapConn, "append", real)
    assert client.post(f"/api/jobs/{jid}/resume").status_code == 200
    j = run_inproc(client, jid)
    assert j["status"] == "done", _log(client, jid)
    ids = server_import_ids(dst)
    assert len(ids) == n and set(ids.values()) == {1}


def test_inflight_after_a_kill_is_checked_on_the_target(client, env):
    from app.server import db
    aid, src, dst = setup_route(client)
    n = seed(src)
    jid = run(client, aid)
    assert run_inproc(client, jid)["status"] == "done"
    db.ex("UPDATE items SET status='inflight' WHERE account_id=?", (aid,))  # every row: as after a brutal stop
    assert client.post(f"/api/jobs/{jid}/retry").status_code == 200
    assert run_inproc(client, jid)["status"] == "done", _log(client, jid)
    ids = server_import_ids(dst)
    assert len(ids) == n and set(ids.values()) == {1}


def test_answer_lost_after_append_is_not_duplicated(client, env, monkeypatch):
    from app.server import connector_runner
    aid, src, dst = setup_route(client)
    n = seed(src, 3)
    real, calls = ImapConn.append, []

    def flaky(self, name, raw, flags, dt):
        uid = real(self, name, raw, flags, dt)
        calls.append(name)
        if len(calls) == 2:
            raise ImapError("IMAP connection: timed out", "temp")
        return uid

    monkeypatch.setattr(ImapConn, "append", flaky)
    init = connector_runner.TargetPool.__init__
    monkeypatch.setattr(connector_runner.TargetPool, "__init__",
                        lambda self, target, k, stop: init(self, target, k, stop, base_delay=0.05))
    jid = run(client, aid)
    assert run_inproc(client, jid)["status"] == "done", _log(client, jid)
    ids = server_import_ids(dst)
    assert len(ids) == n and set(ids.values()) == {1}


def test_wrong_source_password_fails_the_job(client, env):
    aid, src, dst = setup_route(client)
    seed(src, 1)
    assert client.put(f"/api/accounts/{aid}/source", json={
        "kind": "imap",
        "config": {"host": IMAP_HOST, "port": IMAP_PORT, "security": "none", "verify_tls": False, "login": src},
        "secrets": {"password": "wrong"}}).status_code == 200
    jid = run(client, aid)
    j = run_inproc(client, jid)
    assert j["status"] == "failed" and "Source (imap)" in j["error"]
    assert server_import_ids(dst) == {}
    r = client.post("/api/connectors/imap/test", json={
        "config": {"host": IMAP_HOST, "port": IMAP_PORT, "security": "none", "verify_tls": False, "login": src},
        "secrets": {"password": "wrong"}}).json()
    assert r["ok"] is False and r["error_kind"] == "auth"


def test_source_listing_keys_and_incremental(env):
    src = create_user(prefix="src")
    seed(src, 2)
    cfg = {"host": IMAP_HOST, "port": IMAP_PORT, "security": "none", "verify_tls": False, "login": src}
    with ImapSource(cfg, {"password": "pw"}) as s:
        folders = {f.ref: f for f in s.folders()}
        assert folders["INBOX"].special == "inbox" and folders["Sent"].special == "sent"
        inbox = folders["INBOX"]
        items = list(s.iter_items(inbox))
        assert [it.key for it in items] == [it.key for it in s.iter_items(inbox)]  # stable
        assert len({it.key for it in items}) == 2 and all(it.alt_key.startswith("m:") for it in items)
        assert all(it.date and it.size for it in items)
        got = s.fetch(inbox, items[0], "<unused>")
        assert got.subject.startswith("INBOX") and b"Corps accentu" in got.raw
        first = s.changed_since(inbox, None)
        assert first.reset and len(first.items) == 2
        none = s.changed_since(inbox, first.cursor)
        assert not none.reset and none.items == []
        c = imap_conn(src)
        c.append("INBOX", message(9, "INBOX"), [], None)
        c.close()
        s.close()
        s.open()
        new = s.changed_since(inbox, first.cursor)
        assert not new.reset and [i.date is not None for i in new.items] == [True]
        stale = s.changed_since(inbox, '{"uv": 1, "uid": 1}')
        assert stale.reset and len(stale.items) == 3
