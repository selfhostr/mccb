"""An IMAP source on a server saved with CalDAV/CardDAV on brings its calendars, tasks and contacts in the same job
(connectors/composite.py). Mail keys and namespace stay those of the plain IMAP source; a DAV that cannot be read
does not stop the mail; calendars or contacts wanted that the source cannot bring are said, never skipped in silence.

End to end against GreenMail + Radicale (docker-compose.test.yml), unit tests without them."""
import os
from datetime import datetime, timezone
from urllib.parse import urljoin

import pytest

from app.connectors.base import (CALENDAR, CONTACTS, MAIL, TASKS, ConnectorError, Listing, Source, SourceFolder,
                                 SourceItem)
from app.connectors.composite import ImapDavSource

IMAP_HOST = os.environ.get("TEST_IMAP_HOST")
IMAP_PORT = int(os.environ.get("TEST_IMAP_PORT", "3143"))
DAV_URL = os.environ.get("TEST_DAV_URL")
needs_services = pytest.mark.skipif(not (IMAP_HOST and DAV_URL), reason="TEST_IMAP_HOST / TEST_DAV_URL not set")
WHEN = datetime(2024, 5, 17, 8, 30, tzinfo=timezone.utc)


# ------------------------------------------------------------------ unit: the composite
class Part(Source):
    def __init__(self, folders, fail=None):
        self._folders, self.fail, self.calls = folders, fail, []

    def open(self):
        self.calls.append("open")

    def close(self):
        self.calls.append("close")

    def folders(self):
        if self.fail:
            raise ConnectorError(self.fail, ConnectorError.AUTH)
        return list(self._folders)

    def iter_items(self, folder):
        self.calls.append(("iter", folder.kind))
        yield SourceItem(key=f"k-{folder.kind}")

    def fetch(self, folder, item, import_id):
        self.calls.append(("fetch", folder.kind))
        return None

    def mirror_listing(self, folder):
        return Listing([])

    def key_epoch(self, key):
        return "uv"


def test_composite_dispatches_by_kind_and_keeps_the_mail_part_whole():
    mail = Part([SourceFolder(["INBOX"], MAIL), SourceFolder(["Calendar"], MAIL)])
    cal = Part([SourceFolder(["Calendar"], CALENDAR, ref="c"), SourceFolder(["Calendar"], TASKS, ref="c"),
                SourceFolder(["odd"], CONTACTS)])  # a kind that is not CalDAV's is never taken from it
    card = Part([SourceFolder(["Book"], CONTACTS, ref="b")])
    src = ImapDavSource(mail, {"caldav": cal, "carddav": card}, "src")
    src.open()
    folders = src.folders()
    assert [(f.path, f.kind) for f in folders] == [
        (["INBOX"], MAIL), (["Calendar"], MAIL), (["Calendar"], CALENDAR), (["Calendar"], TASKS), (["Book"], CONTACTS)]
    for f in folders:
        list(src.iter_items(f))
        src.fetch(f, SourceItem(key="x"), "id")
    assert [c for c in mail.calls if c != "open"] == [("iter", MAIL), ("fetch", MAIL)] * 2
    assert ("iter", TASKS) in cal.calls and ("iter", CONTACTS) in card.calls and ("iter", CONTACTS) not in cal.calls
    # delta: the mail keeps its cursor, DAV is listed in full
    ch = src.changed_since(folders[2], '{"uv": 1, "uid": 3}')
    assert ch.reset and ch.cursor is None
    # mirror: the epoch of a DAV key is none, the one of a mail key is IMAP's
    assert src.key_epoch("0123abcd-" + "a" * 32) is None and src.key_epoch("0123abcd-7-12") == "uv"
    src.close()
    assert mail.calls[-1] == "close" and cal.calls[-1] == "close" and card.calls[-1] == "close"


def test_composite_dav_failure_leaves_the_mail():
    mail = Part([SourceFolder(["INBOX"], MAIL)])
    src = ImapDavSource(mail, {"caldav": Part([], fail="401 Unauthorized"), "carddav": Part([SourceFolder(["B"], CONTACTS)])},
                        "src")
    folders = src.folders()
    assert [f.kind for f in folders] == [MAIL, CONTACTS]
    assert src.failed == {CALENDAR, TASKS} and "CALDAV" in src.problems[0] and "401" in src.problems[0]
    mail.fail = "down"
    with pytest.raises(ConnectorError):  # an IMAP error stays the job's
        src.folders()


def test_mirror_wrapper_reaches_every_read_method_of_the_composite():
    from app.connectors.base import READ_ONLY_SOURCE_METHODS
    from app.server.mirror import ReadOnlySource
    ro = ReadOnlySource(ImapDavSource(Part([]), {}))
    for name in READ_ONLY_SOURCE_METHODS:
        assert callable(getattr(ro, name))
    assert ro.native_code is False


def server(client, name, dav, caldav_url=None, host=None, port=None):
    r = client.post("/api/servers", json={
        "name": name, "imap_host": host or IMAP_HOST or "imap.example.test", "imap_port": port or IMAP_PORT,
        "imap_security": "none", "verify_tls": False, "dav_enabled": dav, "caldav_url": caldav_url,
        "carddav_url": caldav_url, "workers": 1, "tuning": "manual", "notes": ""})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def imap_account(client, sid, src, dst, host=None, port=None):
    aid = client.post("/api/accounts", json={"server_id": sid, "email": dst, "password": "pw"}).json()["id"]
    r = client.put(f"/api/accounts/{aid}/source", json={
        "kind": "imap", "config": {"host": host or IMAP_HOST or "imap.example.test", "port": port or IMAP_PORT,
                                   "security": "none", "verify_tls": False, "login": src},
        "secrets": {"password": "pw"}})
    assert r.status_code == 200, r.text
    return aid


def test_account_says_whether_its_imap_source_brings_calendars(client):
    from app.connectors import wiring
    from app.server import db
    dst_srv = server(client, "dst", True, host="dst.example.test", port=993)
    aid = imap_account(client, dst_srv, "src@example.test", "dst@example.test", "Mail.Old.Example", 993)
    acc = client.get(f"/api/accounts/{aid}").json()
    assert acc["source_carries"] == {"calendar": False, "contacts": False}
    assert acc["source_dav"] == {"server_id": None, "problem": "no_server"}
    old = server(client, "old", False, host="mail.old.example", port=993)  # matched without case
    acc = client.get(f"/api/accounts/{aid}").json()
    assert acc["source_dav"] == {"server_id": old, "problem": "dav_off"}
    row = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
    assert "off on the source server 'old'" in wiring.source_gaps(row)[CALENDAR]
    before = wiring.items_key_of_account(row)
    db.ex("UPDATE servers SET dav_enabled=1 WHERE id=?", (old,))
    acc = client.get(f"/api/accounts/{aid}").json()
    assert acc["source_carries"] == {"calendar": True, "contacts": True}
    assert acc["source_dav"] == {"server_id": old, "problem": None}
    row = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
    assert wiring.items_key_of_account(row) == before  # the namespace of the mail never moves
    assert wiring.source_gaps(row) == {}
    src = wiring.source_of_account(row)
    assert isinstance(src, ImapDavSource) and set(src.dav) == {"caldav", "carddav"}
    assert src.dav["caldav"].config["login"] == "src@example.test" and src.dav["caldav"].password == "pw"
    # only the kinds wanted are read
    assert client.put(f"/api/accounts/{aid}", json={"options": {"contacts": False}}).status_code == 200
    assert set(wiring.source_of_account(db.q1("SELECT * FROM accounts WHERE id=?", (aid,))).dav) == {"caldav"}
    # a second server of the same address with other DAV settings: not guessed
    server(client, "old-bis", False, host="mail.old.example", port=993)
    acc = client.get(f"/api/accounts/{aid}").json()
    assert acc["source_dav"]["problem"] == "ambiguous" and acc["source_carries"]["calendar"] is False
    # OAuth2 IMAP: mail only, said as such
    r = client.put(f"/api/accounts/{aid}/source", json={
        "kind": "imap_oauth2", "config": {"host": "mail.old.example", "login": "src@example.test",
                                          "token_url": "https://t.example.test/token", "client_id": "c"}})
    assert r.status_code == 200, r.text
    acc = client.get(f"/api/accounts/{aid}").json()
    assert acc["source_carries"] == {"calendar": False, "contacts": False} and acc["source_dav"] is None
    assert "has no CalDAV" in wiring.source_gaps(db.q1("SELECT * FROM accounts WHERE id=?", (aid,)))[CALENDAR]


def test_the_destination_mailbox_is_never_its_own_dav_source(client):
    sid = server(client, "both", True, host="same.example.test", port=993)
    aid = imap_account(client, sid, "me@example.test", "me@example.test", "same.example.test", 993)
    assert client.get(f"/api/accounts/{aid}").json()["source_dav"] == {"server_id": sid, "problem": "same_mailbox"}
    other = imap_account(client, sid, "old@example.test", "new@example.test", "same.example.test", 993)
    assert client.get(f"/api/accounts/{other}").json()["source_dav"] == {"server_id": sid, "problem": None}


# ------------------------------------------------------------------ end to end
def msg(mid, subject):
    return (f"From: a@example.test\r\nTo: b@example.test\r\nSubject: {subject}\r\nMessage-ID: <{mid}>\r\n"
            f"Date: Fri, 17 May 2024 08:30:00 +0000\r\n\r\nbody {subject}\r\n").encode()


def ics(uid, summary, comp="VEVENT"):
    when = "DTSTART:20240305T100000Z\r\nDTEND:20240305T110000Z\r\n" if comp == "VEVENT" else "DUE:20240305T100000Z\r\n"
    return (f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//t//EN\r\nBEGIN:{comp}\r\nUID:{uid}\r\n"
            f"DTSTAMP:20240301T000000Z\r\n{when}SUMMARY:{summary}\r\nEND:{comp}\r\nEND:VCALENDAR\r\n")


def vcf(uid, fn):
    return f"BEGIN:VCARD\r\nVERSION:3.0\r\nUID:{uid}\r\nFN:{fn}\r\nN:{fn};;;;\r\nEND:VCARD\r\n"


def imap(addr):
    from app.server.imap import ImapConn, ImapParams
    return ImapConn(ImapParams(IMAP_HOST, IMAP_PORT, "none", False, addr, "pw")).connect()


def mail_counts(addr):
    c = imap(addr)
    try:
        return {b.name: c.status_messages(b.name) for b in c.list()}
    finally:
        c.close()


def dav_objects(user, kind):
    """Sorted texts of every object of every collection of this kind."""
    from app.server.dav import DavClient, DavError
    c = DavClient(user, "pw", False)
    try:
        try:
            colls = c.collections(kind, urljoin(DAV_URL, f"/{user}/"), user, "")
        except DavError:
            return []  # nothing written there yet
        return sorted(c.get(url, n) or "" for url, _l, _c in colls for n in c.names(url))
    finally:
        c.close()


def run(client, aid, **kw):
    from app.server import runner
    r = client.post("/api/runs", json={"account_ids": [aid], **kw})
    assert r.status_code == 200, r.text
    jid = r.json()[0]["id"]
    runner.run_job(jid, "test:inproc")
    return (client.get(f"/api/jobs/{jid}").json(), client.get(f"/api/jobs/{jid}/report").json(),
            [line["msg"] for line in client.get(f"/api/jobs/{jid}/log").json()["lines"]])


def mail_rows(aid):
    from app.server import db
    return sorted((r["id"], r["item_key"], r["folder"], r["status"], r["pst_key"]) for r in db.q(
        "SELECT * FROM items WHERE account_id=? AND kind='mail'", (aid,)))


@pytest.fixture
def route(client):
    from app.server.dav import DavClient
    from tests.svcusers import create_user
    src, dst = create_user(prefix="idsrc"), create_user(prefix="iddst")
    c = imap(src)
    try:
        c.create("Calendar")  # a mail folder of the same name as the DAV calendar
        for i in range(3):
            c.append("INBOX", msg(f"in{i}@x", f"in {i}"), [], WHEN)
        c.append("Calendar", msg("cal0@x", "invitation"), [], WHEN)
    finally:
        c.close()
    d = DavClient(src, "pw", False)
    try:
        home = urljoin(DAV_URL, f"/{src}/")
        cal = d.ensure_collection("caldav", home + "cal/", "VEVENT", "Calendar")
        todo = d.ensure_collection("caldav", home + "todo/", "VTODO", "Todo")
        book = d.ensure_collection("carddav", home + "book/", "VEVENT", "Contacts")
        for i in range(3):
            d.put(cal, f"e{i}.ics", ics(f"ev{i}-{src}", f"Event {i}"), "caldav")
        d.put(todo, "t0.ics", ics(f"task0-{src}", "Task 0", "VTODO"), "caldav")
        for i in range(2):
            d.put(book, f"c{i}.vcf", vcf(f"card{i}-{src}", f"Contact {i}"), "carddav")
    finally:
        d.close()
    dst_srv = server(client, "yety", True, DAV_URL)
    src_srv = server(client, "old", False, DAV_URL)  # same GreenMail: told apart from the destination's own server
    aid = imap_account(client, dst_srv, src, dst)
    return aid, src, dst, src_srv, (cal, todo, book)


@needs_services
def test_imap_source_brings_calendars_and_contacts_of_its_server(client, route):
    from app.server import db
    aid, src, dst, src_srv, (cal, _todo, _book) = route
    # 1. DAV off on the source server: the mail only, and the report says why the rest is not copied
    job, rep, log = run(client, aid)
    assert job["status"] == "done", log
    assert any("Calendars wanted but CalDAV/CardDAV is off on the source server 'old'" in w for w in rep["warnings"])
    assert any("Contacts wanted but" in line for line in log)
    assert dav_objects(dst, "caldav") == [] and dav_objects(dst, "carddav") == []
    first = mail_rows(aid)
    counts = mail_counts(dst)
    assert len(first) == 4 and counts["INBOX"] == 3 and counts["Calendar"] == 1
    namespace = first[0][4]
    # 2. DAV on: events, tasks and contacts arrive, no mail is copied again (same rows, same keys, same namespace)
    db.ex("UPDATE servers SET dav_enabled=1 WHERE id=?", (src_srv,))
    job, rep, log = run(client, aid)
    assert job["status"] == "done", log
    assert not any("wanted but" in w for w in rep["warnings"]), rep["warnings"]
    assert rep["source"]["namespace"] == namespace and mail_rows(aid) == first
    assert mail_counts(dst) == counts
    events, books = dav_objects(dst, "caldav"), dav_objects(dst, "carddav")
    assert sum("BEGIN:VEVENT" in t for t in events) == 3 and sum("BEGIN:VTODO" in t for t in events) == 1
    assert len(books) == 2
    assert job["progress"]["calendar"]["done"] == 3 and job["progress"]["contacts"]["done"] == 2
    # 3. again: nothing twice
    job, rep, log = run(client, aid)
    assert job["status"] == "done", log
    assert (dav_objects(dst, "caldav"), dav_objects(dst, "carddav")) == (events, books)
    assert mail_rows(aid) == first and mail_counts(dst) == counts
    # 4. delta pass: the mail stays incremental, DAV is listed in full
    job, rep, log = run(client, aid, mode="delta")
    assert job["status"] == "done", log
    assert (dav_objects(dst, "caldav"), dav_objects(dst, "carddav")) == (events, books)
    assert mail_counts(dst) == counts
    # 5. mirror dry run and pass on the composite
    assert client.put(f"/api/accounts/{aid}", json={"mirror": True}).status_code == 200
    from app.server.dav import DavClient
    d = DavClient(src, "pw", False)
    try:
        d.put(cal, "e9.ics", ics(f"ev9-{src}", "Event 9"), "caldav")
    finally:
        d.close()
    r = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror"})
    acc = r.json()["accounts"][0]
    assert acc["ready"] is True, acc
    assert acc["to_add"]["calendar"] == 1 and acc["to_add"]["mail"] == 0 and sum(acc["to_delete"].values()) == 0, acc
    r = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror", "dry_run": False})
    from app.server import runner
    jid = r.json()["jobs"][0]["id"]
    runner.run_job(jid, "test:inproc")
    job = client.get(f"/api/jobs/{jid}").json()
    assert job["status"] == "done", client.get(f"/api/jobs/{jid}/log").json()
    assert sum("Event 9" in t for t in dav_objects(dst, "caldav")) == 1


@needs_services
def test_a_dav_that_cannot_be_read_does_not_stop_the_mail(client, route):
    from app.server import db
    aid, src, dst, src_srv, _ = route
    db.ex("UPDATE servers SET dav_enabled=1, caldav_url=?, carddav_url=? WHERE id=?",
          (urljoin(DAV_URL, "/nobody-here/"), urljoin(DAV_URL, "/nobody-here/"), src_srv))
    job, rep, log = run(client, aid)
    assert job["status"] == "done_with_errors", log
    assert any("CALDAV of the source server 'old' could not be read" in w for w in rep["warnings"]), rep["warnings"]
    assert any("CARDDAV of the source server 'old' could not be read" in line for line in log)
    assert mail_counts(dst)["INBOX"] == 3 and mail_counts(dst)["Calendar"] == 1
    assert dav_objects(dst, "caldav") == []
    # the mirror dry run leaves calendars and contacts out, says why, and still mirrors the mail
    assert client.put(f"/api/accounts/{aid}", json={"mirror": True}).status_code == 200
    acc = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror"}).json()["accounts"][0]
    assert acc["ready"] is True, acc
    assert any(n["code"] == "source_dav_unreadable" for n in acc["notes"]), acc
