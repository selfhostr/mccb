"""Folders another mailbox shares with the source account (SmarterMail lists them under its other users' namespace,
e.g. "Dossiers partagés/<owner>/Inbox", in the same LIST as the account's own): not copied into this mailbox, where
they would be the owner's mails, unread for this one. Copies an earlier version made are said, never deleted."""
from datetime import datetime, timezone

import pytest

from app.connectors import imap as cimap
from app.server.imap import Mailbox, parse_namespace, utf7_encode
from tests.server.test_idempotence import run_inproc
from tests.server.test_runner import DAV_URL, IMAP_HOST, _log, imap_conn, run  # noqa: F401
from tests.server.test_runner import wait_services  # noqa: F401 - autouse
from tests.server.test_connectors_imap import setup_route

WHEN = datetime(2024, 3, 4, 9, 15, tzinfo=timezone.utc)
SHARED_ROOT = "Dossiers partagés"


def test_parse_namespace_of_smartermail_and_others():
    assert parse_namespace('(("" "/")) (("Dossiers partag&AOk-s/" "/")) (("Dossiers publics/" "/"))') == (
        [""], ["Dossiers partag&AOk-s/"], ["Dossiers publics/"])
    assert parse_namespace('(("INBOX." ".")) NIL NIL') == (["INBOX."], [], [])
    assert parse_namespace('(("" "/")("#mh/" "/" "X-PARAM" ("FLAG1" "FLAG2"))) (("#user/" "/")) NIL') == (
        ["", "#mh/"], ["#user/"], [])
    assert parse_namespace("") == ([], [], [])


def box(name, *flags, delim="/"):
    return Mailbox(name=name, flags=list(flags), delim=delim)


def test_folders_of_leaves_out_the_shared_and_public_namespaces():
    foreign = cimap.foreign_namespaces(["Dossiers partag&AOk-s/"], ["Dossiers publics/"])
    out = {tuple(f.path): f for f in cimap.folders_of(
        [box("INBOX"), box("Dossiers partag&AOk-s", "\\Noselect"),
         box("Dossiers partag&AOk-s/alice/Inbox"), box("Dossiers partag&AOk-s/bob/Sent Items"),
         box("Dossiers publics/example.test [Public]"), box("Sent Items")], "/", "", foreign)}
    assert out[("INBOX",)].skip is None and out[("Sent Items",)].special == "sent"
    shared = out[("Dossiers partagés", "alice", "Inbox")]
    assert shared.skip and "another mailbox" in shared.skip and shared.special is None
    assert out[("Dossiers partagés", "bob", "Sent Items")].special is None  # never the sent folder of this one
    assert "public" in out[("Dossiers publics", "example.test [Public]")].skip


@pytest.mark.skipif(not IMAP_HOST or not DAV_URL, reason="TEST_IMAP_HOST / TEST_DAV_URL not set")
def test_shared_folders_of_another_mailbox_are_not_copied(client, env, monkeypatch):
    from app.server import db
    from app.server.config import settings
    aid, src, dst = setup_route(client)
    c = imap_conn(src)
    try:
        d = c.delimiter()
        shared = utf7_encode(f"{SHARED_ROOT}{d}alice{d}Inbox")
        c.create(utf7_encode(SHARED_ROOT))
        c.create(utf7_encode(f"{SHARED_ROOT}{d}alice"))
        c.create(shared)
        for i in range(3):  # the owner's mails, unread: hers, not this mailbox's
            c.append(shared, f"Subject: shared {i}\r\nMessage-ID: <s{i}@x>\r\n\r\nb\r\n".encode(), [], WHEN)
        c.append("INBOX", b"Subject: own\r\nMessage-ID: <own@x>\r\n\r\nb\r\n", ["\\Seen"], WHEN)
    finally:
        c.close()
    real = cimap.namespaces
    monkeypatch.setattr(cimap, "namespaces", lambda conn: (
        ([""], [utf7_encode(SHARED_ROOT) + d], []) if conn.p.login == src else real(conn)))

    monkeypatch.setattr(settings, "imap_include_shared", True)  # as earlier versions did
    jid = run(client, aid)
    assert run_inproc(client, jid)["status"] == "done", _log(client, jid)
    dc = imap_conn(dst)
    try:
        assert dc.select(shared, readonly=True) == 3
    finally:
        dc.close()

    monkeypatch.setattr(settings, "imap_include_shared", False)
    for _ in range(2):  # said at every pass, the copies stay where they are
        assert client.post(f"/api/jobs/{jid}/retry").status_code == 200
        j = run_inproc(client, jid)
        assert j["status"] == "done", _log(client, jid)
        log = _log(client, jid)
        assert "not migrated: folder of another mailbox shared with this one" in log
        assert "an earlier run copied 3 of its mail(s)" in log
        rep = client.get(f"/api/jobs/{jid}/report").json()
        assert rep["mail_left_on_target"] == [
            {"folder": f"{SHARED_ROOT}/alice/Inbox", "target": shared, "items": 3, "reason": rep[
                "mail_left_on_target"][0]["reason"]}]
        assert rep["flags"]["checked"] == 1  # the own mail only
    dc = imap_conn(dst)
    try:
        assert dc.select(shared, readonly=True) == 3  # never deleted by mccb
    finally:
        dc.close()
    n = db.q1("SELECT COUNT(*) n FROM items WHERE account_id=? AND status='ok'", (aid,))["n"]
    assert n == 1

    monkeypatch.setattr(settings, "imap_include_shared", True)  # asked for again: they are ours again, not doubled
    assert client.post(f"/api/jobs/{jid}/retry").status_code == 200
    assert run_inproc(client, jid)["status"] == "done", _log(client, jid)
    dc = imap_conn(dst)
    try:
        assert dc.select(shared, readonly=True) == 3
    finally:
        dc.close()
