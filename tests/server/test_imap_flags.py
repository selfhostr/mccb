"""Read/unread state IMAP to IMAP against GreenMail (docker-compose.test.yml): the flags of the source arrive on the
target, a target that drops the flags of APPEND is repaired, so is a copy found on the target instead of being sent
again, and a rerun carries what was read or starred at the source since. The source never changes, and nothing but
our own copies is ever touched on the target."""
import email
import imaplib
import re
from datetime import datetime, timezone

import pytest

from app.server.imap import ImapConn
from tests.server.test_idempotence import run_inproc
from tests.server.test_runner import (DAV_URL, IMAP_HOST, _log, imap_conn, run,  # noqa: F401 - wait_services: autouse
                                      wait_services)
from tests.server.test_connectors_imap import setup_route

pytestmark = pytest.mark.skipif(not IMAP_HOST or not DAV_URL, reason="TEST_IMAP_HOST / TEST_DAV_URL not set")
WHEN = datetime(2024, 3, 4, 9, 15, tzinfo=timezone.utc)
# subject -> flags at the source (an archive folder read long ago, an inbox with a bit of everything)
INBOX = {"seen": ["\\Seen"], "new": [], "starred": ["\\Flagged"], "replied": ["\\Seen", "\\Answered"],
         "keyword": ["\\Seen", "NonJunk"]}
ARCHIVE = {f"invoice {i}": ["\\Seen"] for i in range(6)}


def msg(subject: str) -> bytes:
    return (f"From: a@example.test\r\nTo: b@example.test\r\nSubject: {subject}\r\n"
            f"Message-ID: <{subject.replace(' ', '.')}@flags.test>\r\nDate: Mon, 04 Mar 2024 09:15:00 +0000\r\n"
            f"\r\nbody {subject}\r\n").encode()


def seed(src: str) -> int:
    c = imap_conn(src)
    try:
        c.create("Archives")
        for subject, flags in INBOX.items():
            c.append("INBOX", msg(subject), flags, WHEN)
        for subject, flags in ARCHIVE.items():
            c.append("Archives", msg(subject), flags, WHEN)
    finally:
        c.close()
    return len(INBOX) + len(ARCHIVE)


def flags_of(addr: str) -> dict[str, dict[str, set[str]]]:
    """{folder: {subject: flags}} read with EXAMINE + BODY.PEEK (nothing changes), \\Recent left out."""
    c = imap_conn(addr)
    out: dict[str, dict[str, set[str]]] = {}
    try:
        for b in c.list():
            if "\\Noselect" in b.flags:
                continue
            n = c.select(b.name, readonly=True)
            out[b.decoded] = {}
            if not n:
                continue
            typ, data = c.M.fetch("1:*", "(FLAGS BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
            for i, d in enumerate(data):
                if not isinstance(d, tuple):
                    continue
                tail = data[i + 1] if i + 1 < len(data) and isinstance(data[i + 1], bytes) else b""
                flags = {f.decode() for f in imaplib.ParseFlags(d[0] + b" " + tail) if f.lower() != b"\\recent"}
                out[b.decoded][str(email.message_from_bytes(d[1])["Subject"])] = flags
    finally:
        c.close()
    return out


def store(addr: str, folder: str, subject: str, op: str, flags: str) -> None:
    """What the owner of the mailbox does in a mail client: changes the flags of one message (SEARCH SUBJECT is a
    substring search: no subject of these tests contains another)."""
    c = imap_conn(addr)
    try:
        c.select(folder, readonly=False)
        typ, data = c.M.uid("SEARCH", "SUBJECT", f'"{subject}"')
        uids = data[0].decode().split()
        assert uids, (folder, subject)
        c.M.uid("STORE", ",".join(uids), f"{op}FLAGS", f"({flags})")
    finally:
        c.close()


def strip_all(addr: str, folder: str) -> None:
    """As left by a server that kept none of the flags of APPEND."""
    c = imap_conn(addr)
    try:
        c.select(folder, readonly=False)
        c.M.uid("STORE", "1:*", "-FLAGS.SILENT", "(\\Seen \\Flagged \\Answered NonJunk)")
    finally:
        c.close()


def expected() -> dict[str, dict[str, set[str]]]:
    return {"INBOX": {k: set(v) for k, v in INBOX.items()}, "Archives": {k: set(v) for k, v in ARCHIVE.items()}}


def assert_flags(dst: str, want: dict, keywords: bool = True) -> None:
    got = flags_of(dst)
    for folder, subjects in want.items():
        for subject, flags in subjects.items():
            have = got[folder][subject]
            if not keywords:  # keywords are not repaired: the system flags only
                have, flags = {f for f in have if f.startswith("\\")}, {f for f in flags if f.startswith("\\")}
            assert have == flags, (folder, subject, have, flags)


MUTATING = {"STORE", "COPY", "MOVE", "EXPUNGE", "APPEND", "CREATE", "DELETE", "RENAME", "SUBSCRIBE"}


@pytest.fixture
def guarded_source(monkeypatch):
    """Every command the job sends to the source: read-only (EXAMINE), no write, no FETCH that sets \\Seen."""
    from app.connectors import imap as cimap
    real_open = cimap.ImapSource.open

    def check_fetch(args):
        items = " ".join(str(a) for a in args[1:]).upper()
        bare = re.sub(r"BODY\.PEEK\[|RFC822\.SIZE|RFC822\.HEADER", "", items)
        assert "BODY[" not in bare and "RFC822" not in bare, f"FETCH marking the source read: {items}"

    class Guard:
        def __init__(self, m):
            self._m = m

        def __getattr__(self, name):
            assert name.upper() not in MUTATING, f"source written: {name}"
            attr = getattr(self._m, name)
            if name == "uid":
                def uid(cmd, *a):
                    assert cmd.upper() not in MUTATING, f"source written: UID {cmd}"
                    if cmd.upper() == "FETCH":
                        check_fetch(a)
                    return attr(cmd, *a)
                return uid
            if name == "fetch":
                def fetch(*a):
                    check_fetch(a)
                    return attr(*a)
                return fetch
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


@pytest.fixture
def dropping_append(monkeypatch):
    """A target that accepts APPEND but keeps none of its flags (as some servers do); the source is never written."""
    real = ImapConn.append
    dst_logins = set()

    def append(self, name, raw, flags, dt):
        return real(self, name, raw, [] if self.p.login in dst_logins else flags, dt)

    monkeypatch.setattr(ImapConn, "append", append)
    return dst_logins


def test_flags_of_the_source_arrive_and_the_source_is_untouched(client, env, guarded_source):
    aid, src, dst = setup_route(client)
    n = seed(src)
    before = flags_of(src)
    j = run_inproc(client, run(client, aid))
    assert j["status"] == "done" and j["progress"]["mail"]["done"] == n, j
    assert_flags(dst, expected())
    assert flags_of(src) == before
    assert "\\Seen" not in before["INBOX"]["new"] and "\\Seen" not in before["INBOX"]["starred"]


def test_target_dropping_append_flags_is_repaired(client, env, dropping_append, guarded_source):
    aid, src, dst = setup_route(client)
    seed(src)
    c = imap_conn(dst)
    try:  # a mail of the owner already on the target, unread: never touched (it is not ours)
        c.append("INBOX", msg("native unread"), [], WHEN)
    finally:
        c.close()
    before = flags_of(src)
    dropping_append.add(dst)
    jid = run(client, aid)
    j = run_inproc(client, jid)
    assert j["status"] == "done", _log(client, jid)
    assert_flags(dst, expected(), keywords=False)
    assert flags_of(dst)["INBOX"]["native unread"] == set()
    assert flags_of(src) == before
    assert "flags of the source added" in _log(client, jid)
    rep = client.get(f"/api/jobs/{jid}/report").json()
    # every copy whose source has a system flag: seen, starred, replied, keyword and the 6 archived invoices
    assert rep["flags"]["fixed"] == 10


def test_inflight_found_on_the_target_gets_its_flags(client, env):
    from app.server import db
    aid, src, dst = setup_route(client)
    seed(src)
    jid = run(client, aid)
    assert run_inproc(client, jid)["status"] == "done"
    # copies written without their flags by an earlier pass, then a brutal stop: rows left inflight
    strip_all(dst, "INBOX")
    strip_all(dst, "Archives")
    db.ex("UPDATE items SET status='inflight' WHERE account_id=?", (aid,))
    before = flags_of(src)
    assert client.post(f"/api/jobs/{jid}/retry").status_code == 200
    j = run_inproc(client, jid)
    assert j["status"] == "done", _log(client, jid)
    assert_flags(dst, expected(), keywords=False)
    assert flags_of(src) == before


@pytest.mark.parametrize("mode", ["full", "delta"])
def test_rerun_carries_what_was_read_at_the_source(client, env, mode, guarded_source):
    aid, src, dst = setup_route(client)
    seed(src)
    if mode == "delta":
        jid = client.post("/api/runs", json={"account_ids": [aid], "mode": "delta"}).json()[0]["id"]
    else:
        jid = run(client, aid)
    assert run_inproc(client, jid)["status"] == "done"
    # since the first pass, at the source: one read, one starred, one marked unread again
    store(src, "INBOX", "new", "+", "\\Seen")
    store(src, "Archives", "invoice 0", "+", "\\Flagged")
    store(src, "INBOX", "seen", "-", "\\Seen")
    before = flags_of(src)
    assert client.post(f"/api/jobs/{jid}/{'delta' if mode == 'delta' else 'retry'}").status_code == 200
    j = run_inproc(client, jid)
    assert j["status"] == "done", _log(client, jid)
    got = flags_of(dst)
    assert got["INBOX"]["new"] == {"\\Seen"}
    assert got["Archives"]["invoice 0"] == {"\\Seen", "\\Flagged"}
    assert got["INBOX"]["seen"] == {"\\Seen"}  # never removed outside the mirror
    assert flags_of(src) == before
    if mode == "delta":
        assert "changed since the previous pass" in _log(client, jid)


def test_mirror_adds_of_a_dropping_target_are_repaired(client, env, dropping_append):
    aid, src, dst = setup_route(client)
    seed(src)
    assert client.put(f"/api/accounts/{aid}", json={"mirror": True}).status_code == 200
    jid = run(client, aid)
    assert run_inproc(client, jid)["status"] == "done"
    c = imap_conn(src)
    try:
        c.append("INBOX", msg("new read"), ["\\Seen"], WHEN)
    finally:
        c.close()
    before = flags_of(src)
    dropping_append.add(dst)
    r = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror", "dry_run": False}).json()
    mj = r["jobs"][0]["id"]
    j = run_inproc(client, mj)
    assert j["status"] == "done", _log(client, mj)
    assert flags_of(dst)["INBOX"]["new read"] == {"\\Seen"}
    assert flags_of(src) == before


def test_parse_fetch_merges_a_message_split_over_two_responses():
    from app.connectors.imap import parse_fetch
    data = [(b'1 (UID 7 RFC822.SIZE 10 BODY[HEADER.FIELDS (MESSAGE-ID)] {20}', b"Message-ID: <a@b>\r\n\r\n"), b")",
            b"1 (UID 7 FLAGS (\\Seen))", b"2 (FLAGS (\\Seen \\Recent) UID 8 RFC822.SIZE 3)"]
    recs = parse_fetch(data)
    assert [(r["uid"], r["flags"], r["size"], r["message_id"]) for r in recs] == [
        (7, ["\\Seen"], 10, "<a@b>"), (8, ["\\Seen", "\\Recent"], 3, None)]


def test_flags_to_add_only_adds_system_flags():
    from app.server.imap import flags_to_add
    assert flags_to_add(["\\Seen", "\\Flagged", "NonJunk", "\\Recent", "\\Deleted"], ["\\SEEN"]) == ["\\Flagged"]
    assert flags_to_add(["\\answered"], []) == ["\\Answered"]
    assert flags_to_add([], ["\\Seen"]) == []


def seen_count(addr: str) -> tuple[int, int]:
    """(messages, messages flagged \\Seen) across the mailbox."""
    got = flags_of(addr)
    return (sum(len(v) for v in got.values()),
            sum("\\Seen" in fl for v in got.values() for fl in v.values()))


def test_pst_to_a_server_dropping_append_flags_is_repaired(client, env, dropping_append):
    """The historical PST route shares the check: the flags each mail was written with are given again."""
    from app.server import db
    from tests.server.test_runner import N_MAIL, setup_account
    aid, addr = setup_account(client, env, workers=1)
    dropping_append.add(addr)
    jid = run(client, aid)
    j = run_inproc(client, jid)
    assert j["status"] == "done", _log(client, jid)
    want = db.q1("SELECT COUNT(*) n FROM items WHERE account_id=? AND kind='mail' AND status='ok' AND "
                 "version LIKE '%\\Seen%'", (aid,))["n"]
    assert 0 < want < N_MAIL
    assert seen_count(addr) == (N_MAIL, want)
    assert client.get(f"/api/jobs/{jid}/report").json()["flags"]["fixed"] == want


def test_a_target_that_keeps_no_flag_at_all_is_reported(client, env, dropping_append, monkeypatch):
    aid, src, dst = setup_route(client)
    seed(src)
    dropping_append.add(dst)
    real = ImapConn.uid_store
    monkeypatch.setattr(ImapConn, "uid_store", lambda self, name, uids, op, flags: (
        None if self.p.login == dst else real(self, name, uids, op, flags)))
    jid = run(client, aid)
    j = run_inproc(client, jid)
    assert j["status"] == "done", _log(client, jid)
    rep = client.get(f"/api/jobs/{jid}/report").json()
    assert rep["flags"] == {"checked": 10, "fixed": 10, "not_kept": 10, "not_found": 0, "folders": 2}
    assert any("did not keep the flags" in w for w in rep["warnings"])


def test_a_target_whose_header_search_finds_nothing_is_repaired(client, env, dropping_append, monkeypatch):
    """A server that does not index X-PST-Import-Id (SEARCH HEADER: no hit): every header of the folder is read and
    compared here instead, and the copies still get their flags; a mail of the owner is still never touched."""
    aid, src, dst = setup_route(client)
    seed(src)
    c = imap_conn(dst)
    try:
        c.append("INBOX", msg("native unread"), [], WHEN)
    finally:
        c.close()
    dropping_append.add(dst)
    real_search, real_add = ImapConn.search_header, ImapConn.add_missing_flags

    def add(self, *a):
        self.blind = self.p.login == dst
        try:
            return real_add(self, *a)
        finally:
            self.blind = False
    monkeypatch.setattr(ImapConn, "add_missing_flags", add)
    monkeypatch.setattr(ImapConn, "search_header", lambda self, name, header, value: (
        [] if getattr(self, "blind", False) else real_search(self, name, header, value)))
    jid = run(client, aid)
    j = run_inproc(client, jid)
    assert j["status"] == "done", _log(client, jid)
    assert_flags(dst, expected(), keywords=False)
    assert flags_of(dst)["INBOX"]["native unread"] == set()
    rep = client.get(f"/api/jobs/{jid}/report").json()
    assert rep["flags"]["fixed"] == 10 and rep["flags"]["not_found"] == 0
    assert "Read, answered and starred state: 10 of our mail(s) checked" in _log(client, jid)


def test_a_mail_read_at_the_source_during_the_pass_arrives_read(client, env, monkeypatch, guarded_source):
    """The listing is taken when the pass starts; a long pass reads the flags of the source again before the
    repair: a mail read at the source meanwhile arrives read."""
    from app.server import connector_runner as cr
    aid, src, dst = setup_route(client)
    seed(src)
    monkeypatch.setattr(cr, "FLAGS_FRESH_SECONDS", -1)  # every listing counts as old
    real = cr.ConnectorJobRunner._repair_flags

    def repair(self):
        store(src, "INBOX", "new", "+", "\\Seen")  # read in the webmail of the source while the job ran
        return real(self)
    monkeypatch.setattr(cr.ConnectorJobRunner, "_repair_flags", repair)
    jid = run(client, aid)
    j = run_inproc(client, jid)
    assert j["status"] == "done", _log(client, jid)
    assert flags_of(dst)["INBOX"]["new"] == {"\\Seen"}


def test_rerun_logs_the_flag_check_even_when_nothing_is_missing(client, env):
    aid, src, dst = setup_route(client)
    seed(src)
    jid = run(client, aid)
    assert run_inproc(client, jid)["status"] == "done"
    assert client.post(f"/api/jobs/{jid}/retry").status_code == 200
    j = run_inproc(client, jid)
    assert j["status"] == "done", _log(client, jid)
    rep = client.get(f"/api/jobs/{jid}/report").json()
    assert rep["flags"] == {"checked": 10, "fixed": 0, "not_kept": 0, "not_found": 0, "folders": 2}
    assert "10 of our mail(s) checked against the source in 2 target folder(s), 0 given" in _log(client, jid)
