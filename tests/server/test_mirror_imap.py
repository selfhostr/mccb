"""Mirror mode IMAP to IMAP against GreenMail (docker-compose.test.yml or TEST_IMAP_HOST): deletions to the trash of
the target, flags, moves, new mail, mail arrived on the target, UIDVALIDITY, and a source that refuses any write."""
import email
import imaplib
import os
from datetime import datetime, timezone

import pytest

from app.connectors.base import MIRROR_DELETED_FLAG
from app.server.imap import ImapConn, ImapParams
from tests.svcusers import create_user

IMAP_HOST = os.environ.get("TEST_IMAP_HOST")
IMAP_PORT = int(os.environ.get("TEST_IMAP_PORT", "3143"))
pytestmark = pytest.mark.skipif(not IMAP_HOST, reason="TEST_IMAP_HOST not set")
WHEN = datetime(2024, 5, 17, 8, 30, tzinfo=timezone.utc)
MUTATING = {"STORE", "COPY", "MOVE", "EXPUNGE", "APPEND", "CREATE", "DELETE", "RENAME", "SUBSCRIBE"}


def conn(addr):
    return ImapConn(ImapParams(IMAP_HOST, IMAP_PORT, "none", False, addr, "pw")).connect()


def msg(mid, subject):
    return (f"From: a@example.test\r\nTo: b@example.test\r\nSubject: {subject}\r\nMessage-ID: <{mid}>\r\n"
            f"Date: Fri, 17 May 2024 08:30:00 +0000\r\n\r\nbody {subject}\r\n").encode()


def state(addr):
    """{folder: {message-id: sorted flags}} of a mailbox (\\Deleted ones included, with the flag)."""
    c = conn(addr)
    out = {}
    try:
        for b in c.list():
            n = c.select(b.name, readonly=True)
            out[b.name] = {}
            if not n:
                continue
            typ, data = c.M.fetch("1:*", "(FLAGS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID X-PST-IMPORT-ID)])")
            for d in data:
                if isinstance(d, tuple):
                    h = email.message_from_bytes(d[1])
                    mid = (h.get("Message-ID") or "").strip("<>")
                    flags = sorted(f.decode() for f in imaplib.ParseFlags(d[0]) if f.lower() != b"\\recent")
                    out[b.name][mid] = flags
    finally:
        c.close()
    return out


@pytest.fixture
def route(client):
    src, dst = create_user(prefix="msrc"), create_user(prefix="mdst")
    c = conn(src)
    try:
        c.create("Projects")
        for i in range(10):
            c.append("INBOX", msg(f"in{i}@x", f"in {i}"), ["\\Seen"] if i % 2 else [], WHEN)
        for i in range(3):
            c.append("Projects", msg(f"pr{i}@x", f"pr {i}"), [], WHEN)
    finally:
        c.close()
    c = conn(dst)
    try:
        c.create("Trash")
    finally:
        c.close()
    sid = client.post("/api/servers", json={
        "name": "dst", "imap_host": IMAP_HOST, "imap_port": IMAP_PORT, "imap_security": "none", "verify_tls": False,
        "dav_enabled": False, "workers": 1, "tuning": "manual", "notes": ""}).json()["id"]
    aid = client.post("/api/accounts", json={"server_id": sid, "email": dst, "password": "pw"}).json()["id"]
    r = client.put(f"/api/accounts/{aid}/source", json={
        "kind": "imap", "config": {"host": IMAP_HOST, "port": IMAP_PORT, "security": "none", "verify_tls": False,
                                   "login": src}, "secrets": {"password": "pw"}})
    assert r.status_code == 200, r.text
    assert client.put(f"/api/accounts/{aid}", json={"mirror": True}).status_code == 200
    r = client.post("/api/runs", json={"account_ids": [aid]})
    job = run(client, r.json()[0]["id"])
    assert job["status"] == "done", client.get(f"/api/jobs/{job['id']}/log").json()
    return aid, src, dst


def run(client, jid):
    from app.server import runner
    runner.run_job(jid, "test:inproc")
    return client.get(f"/api/jobs/{jid}").json()


def mirror(client, aid, **kw):
    r = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror", "dry_run": False, **kw})
    assert r.status_code == 200, r.text
    jobs = r.json()["jobs"]
    assert jobs, r.json()
    job = run(client, jobs[0]["id"])
    return job, client.get(f"/api/jobs/{job['id']}/report").json()


def preview(client, aid, **kw):
    r = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror", **kw})
    assert r.status_code == 200, r.text
    return r.json()["accounts"][0]


def change_source(src):
    c = conn(src)
    try:
        M = c.M
        M.select("INBOX")
        uids = {}
        typ, data = M.uid("FETCH", "1:*", "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        for i, d in enumerate(data):
            if isinstance(d, tuple):
                import re
                uid = re.search(rb"UID (\d+)", d[0]).group(1).decode()
                uids[email.message_from_bytes(d[1])["Message-ID"].strip("<>")] = uid
        M.uid("STORE", uids["in0@x"], "+FLAGS", "(\\Deleted)")      # deleted at the source
        M.expunge()
        M.uid("STORE", uids["in2@x"], "+FLAGS", "(\\Seen \\Flagged)")  # read and starred
        M.uid("STORE", uids["in3@x"], "-FLAGS", "(\\Seen)")            # read -> unread
        M.uid("COPY", uids["in4@x"], "Projects")                         # moved to Projects
        M.uid("STORE", uids["in4@x"], "+FLAGS", "(\\Deleted)")
        M.expunge()
        M.logout()
        c.M = None
        c.connect()
        c.append("INBOX", msg("new@x", "new"), [], WHEN)                 # new mail
    finally:
        c.close()


def test_imap_mirror_end_to_end(client, env, route):
    aid, src, dst = route
    change_source(src)
    c = conn(dst)
    try:
        c.append("INBOX", msg("native@x", "arrived on the target"), [], WHEN)
    finally:
        c.close()
    before_src = state(src)
    before_dst = state(dst)
    acc = preview(client, aid)
    assert acc["ready"] is True, acc
    assert acc["to_add"]["mail"] == 1 and acc["to_delete"]["mail"] == 1 and acc["to_update"]["mail"] == 3, acc
    assert state(dst) == before_dst and state(src) == before_src  # the dry run wrote nothing
    job, rep = mirror(client, aid)
    assert job["status"] == "done", client.get(f"/api/jobs/{job['id']}/log").json()
    assert rep["mirror"]["mail"] == {"added": 1, "updated": 3, "deleted": 1, "skipped_by_safety": 0, "errors": 0}
    after = state(dst)
    assert "in0@x" not in after["INBOX"] and MIRROR_DELETED_FLAG in after["Trash"]["in0@x"]
    assert after["INBOX"]["in2@x"] == ["\\Flagged", "\\Seen"]
    assert "\\Seen" not in after["INBOX"]["in3@x"]
    assert "in4@x" in after["Projects"] and "in4@x" not in after["INBOX"]
    assert "new@x" in after["INBOX"] and "native@x" in after["INBOX"]
    assert state(src) == before_src  # the source was only read
    # idempotent: nothing more to do, nothing written
    acc = preview(client, aid)
    assert acc["to_add"]["mail"] == acc["to_update"]["mail"] == acc["to_delete"]["mail"] == 0, acc
    job, rep = mirror(client, aid)
    assert rep["mirror"]["mail"] == {"added": 0, "updated": 0, "deleted": 0, "skipped_by_safety": 0, "errors": 0}
    assert state(dst) == after


def test_imap_source_refusing_writes(client, env, route, monkeypatch):
    """The imaplib object of the SOURCE raises on every mutating command: the mirror still works."""
    from app.connectors import imap as cimap
    aid, src, dst = route
    change_source(src)
    real_open = cimap.ImapSource.open

    class Guard:
        def __init__(self, m):
            self._m = m

        def __getattr__(self, name):
            if name.upper() in MUTATING:
                raise AssertionError(f"source written: {name}")
            attr = getattr(self._m, name)
            if name == "uid":
                def uid(cmd, *a):
                    assert cmd.upper() not in MUTATING, f"source written: UID {cmd}"
                    return attr(cmd, *a)
                return uid
            if name == "select":
                def select(mailbox="INBOX", readonly=False):
                    assert readonly, "source selected read-write"
                    return attr(mailbox, readonly)
                return select
            if name == "_simple_command":
                def simple(cmd, *a):
                    assert cmd.upper() not in MUTATING, f"source written: {cmd}"
                    return attr(cmd, *a)
                return simple
            return attr

    def guarded_open(self):
        real_open(self)
        self.conn.M = Guard(self.conn.M)
    monkeypatch.setattr(cimap.ImapSource, "open", guarded_open)
    job, rep = mirror(client, aid)
    assert job["status"] == "done", client.get(f"/api/jobs/{job['id']}/log").json()
    assert rep["mirror"]["mail"]["deleted"] == 1 and rep["mirror"]["mail"]["updated"] == 3


def imap_source(addr):
    from app.connectors.imap import ImapSource
    s = ImapSource({"host": IMAP_HOST, "port": IMAP_PORT, "security": "none", "verify_tls": False, "login": addr},
                   {"password": "pw"})
    s.open()
    return s


def test_imap_exists_answers_present_for_keys_it_cannot_check(client, env):
    """ImapSource.exists only says "gone" for a key of THIS folder (its folder hash) and of the current UIDVALIDITY:
    any other key cannot be checked there and is answered present (kept)."""
    from app.connectors.imap import folder_hash
    addr = create_user(prefix="mex")
    c = conn(addr)
    try:
        c.append("INBOX", msg("a@x", "a"), [], WHEN)
    finally:
        c.close()
    s = imap_source(addr)
    try:
        inbox = [f for f in s.folders() if f.path == ["INBOX"]][0]
        items = s.mirror_listing(inbox).items
        uv = int(s.key_epoch(items[0].key))
        fh = folder_hash(["INBOX"])
        here, gone = items[0].key, f"{fh}-{uv}-999999"
        other_uv, other_folder = f"{fh}-{uv + 1}-999999", f"{folder_hash(['Projects'])}-{uv}-999999"
        got = s.exists(inbox, [here, gone, other_uv, other_folder])
        assert got == {here: True, gone: False, other_uv: True, other_folder: True}
    finally:
        s.close()


def test_imap_uidvalidity_changed(client, env, route):
    aid, src, dst = route
    c = conn(src)
    try:  # Projects deleted and created again: new UIDVALIDITY, same messages
        c.M.delete("Projects")
        c.create("Projects")
        for i in range(3):
            c.append("Projects", msg(f"pr{i}@x", f"pr {i}"), [], WHEN)
    finally:
        c.close()
    acc = preview(client, aid)
    assert "uidvalidity_changed" in {b["code"] for b in acc["blocked"]}, acc
    before = state(dst)
    job, rep = mirror(client, aid)
    assert state(dst)["Projects"] == before["Projects"]
    assert rep["mirror"]["mail"]["added"] == 0 and rep["mirror"]["mail"]["deleted"] == 0
