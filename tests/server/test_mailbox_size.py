"""Size of the mail at the source (accounts.source_size_*) and bytes imported (items.size)."""
import re
import time

import pytest

from app.connectors.imap import ImapSource
from tests.server.test_connectors_imap import message, seed, setup_route
from tests.server.test_idempotence import run_inproc
from tests.server.test_runner import DAV_URL, IMAP_HOST, IMAP_PORT, _log, imap_conn, run
from tests.svcusers import create_user

services = pytest.mark.skipif(not IMAP_HOST or not DAV_URL, reason="TEST_IMAP_HOST / TEST_DAV_URL not set")


def _cfg(login: str) -> dict:
    return {"host": IMAP_HOST, "port": IMAP_PORT, "security": "none", "verify_tls": False, "login": login}


def _listed(login: str) -> tuple[int, int]:
    """(bytes, messages) of the mail as the job's listing sees it."""
    with ImapSource(_cfg(login), {"password": "pw"}) as s:
        items = [it for f in s.folders() for it in s.iter_items(f)]
    return sum(it.size for it in items), len(items)


def _copied_bytes(login: str) -> int:
    """Bytes of every message on the target, read in full (GreenMail's RFC822.SIZE of a copy is not reliable)."""
    with ImapSource(_cfg(login), {"password": "pw"}) as s:
        return sum(len(s.fetch(f, it, "").raw) for f in s.folders() for it in list(s.iter_items(f)))


@services
def test_imap_size_is_the_sum_of_rfc822_size(env):
    src = create_user(prefix="src")
    n = seed(src)
    c = imap_conn(src)
    c.append("INBOX", message(99, "gone"), ["\\Deleted"], None)  # not listed by the job: not counted either
    c.close()
    want, count = _listed(src)
    assert count == n
    with ImapSource(_cfg(src), {"password": "pw"}) as s:
        folders = s.folders()
        size = s.mailbox_size(folders, time.monotonic() + 30)
        assert (size.bytes, size.count, size.approx) == (want, n, False)
        late = s.mailbox_size(folders, time.monotonic() - 1)  # no time left: the quota estimate, or unknown
        assert late is None or (late.approx and late.count is None)


@services
def test_size_measured_when_the_source_is_set_then_imported_bytes(client, env):
    from app.server import db, mailbox_size
    aid, src, dst = setup_route(client)  # source saved before the seed: measured empty
    mailbox_size.wait_idle()
    n = seed(src)
    want, _ = _listed(src)
    assert client.post(f"/api/accounts/{aid}/size").json()["source_size"]["computing"] is True
    mailbox_size.wait_idle()
    got = client.get(f"/api/accounts/{aid}").json()["source_size"]
    assert got["bytes"] == want and got["count"] == n and got["approx"] is False and got["at"]
    assert got["computing"] is False and got["imported"] == 0
    probe = client.post("/api/connectors/imap/test", json={"role": "source", "config": _cfg(src),
                                                           "secrets": {"password": "pw"}}).json()
    assert probe["size"] == {"bytes": want, "count": n, "approx": False}

    db.ex("UPDATE accounts SET source_size_bytes=1, source_size_count=1 WHERE id=?", (aid,))
    jid = run(client, aid)
    assert run_inproc(client, jid)["status"] == "done", _log(client, jid)
    row = [a for a in client.get("/api/accounts").json() if a["id"] == aid][0]["source_size"]
    assert (row["bytes"], row["count"]) == (want, n)  # refreshed from the job's listing
    copied = db.q1("SELECT SUM(size) s FROM items WHERE account_id=? AND kind='mail' AND status='ok'", (aid,))["s"]
    assert row["imported"] == copied == _copied_bytes(dst) > want  # + the import header of each copy
    assert client.get(f"/api/accounts/{aid}").json()["source_size"]["imported"] == copied


def test_unknown_stays_null(client, env):
    from app.server import mailbox_size
    sid = client.post("/api/servers", json={"name": "s", "imap_host": "127.0.0.1", "imap_port": 1,
                                            "imap_security": "none", "verify_tls": False, "dav_enabled": False,
                                            "notes": ""}).json()["id"]
    aid = client.post("/api/accounts", json={"server_id": sid, "email": "a@example.test", "password": "pw"}).json()["id"]
    size = client.get(f"/api/accounts/{aid}").json()["source_size"]
    assert size == {"bytes": None, "count": None, "at": None, "approx": False, "basis": None,
                    "computing": False, "measurable": False, "imported": 0}
    assert client.post(f"/api/accounts/{aid}/size").status_code == 422  # a PST: its job's listing tells
    r = client.put(f"/api/accounts/{aid}/source", json={
        "kind": "imap", "config": {"host": "127.0.0.1", "port": 1, "security": "none", "login": "a"},
        "secrets": {"password": "pw"}})
    assert r.status_code == 200, r.text
    mailbox_size.wait_idle()
    size = client.get(f"/api/accounts/{aid}").json()["source_size"]
    assert size["bytes"] is None and size["count"] is None and size["computing"] is False  # unreachable: unknown
    assert [a["source_size"]["bytes"] for a in client.get("/api/accounts").json()] == [None]


def test_size_columns_added_to_an_existing_database(env):
    from app.server import db
    db.ex("INSERT INTO servers(name, imap_host, created_at) VALUES ('s', 'h', 0)")
    db.ex("INSERT INTO accounts(server_id, email, created_at) VALUES (1, 'a@example.test', 0)")
    cols = ("source_size_bytes", "source_size_count", "source_size_at", "source_size_approx", "source_size_key",
            "source_size_pending")
    db.ex("DROP INDEX items_bytes")
    for c in cols:
        db.ex(f"ALTER TABLE accounts DROP COLUMN {c}")
    db.reset()  # as a process of the previous version left it
    assert set(cols) <= {r[1] for r in db.q("PRAGMA table_info(accounts)")}
    assert db.q1("SELECT 1 FROM sqlite_master WHERE type='index' AND name='items_bytes'")
    from app.server import repo
    view = repo.get_account(1)["source_size"]
    assert view["bytes"] is None and view["computing"] is False and view["imported"] == 0
