"""Code review: IMAP idempotence (exact id, APPEND not blindly replayed), refused folders, pool wait,
items attached to the PST fingerprint, counting only our own messages, folder keys, fallback keys."""
import hashlib
import sqlite3
import threading
import time

import pytest

from tests.server import fake_engine
from tests.server.fake_engine import FolderInfo
from tests.svcusers import create_user
from tests.server.test_runner import (DEFAULT_FOLDERS, IMAP_HOST, IMAP_PORT, N_MAIL, _items, _log, imap_conn, run,
                                      server_import_ids, setup_account)

needs_imap = pytest.mark.skipif(not IMAP_HOST, reason="TEST_IMAP_HOST not set")


@pytest.fixture(autouse=True)
def reset_fake():
    fake_engine.CONFIG.update(folders=list(DEFAULT_FOLDERS), delay=0.0, fail_keys=set(), reject_keys=set(),
                              unreadable=set())
    yield
    fake_engine.CONFIG.update(folders=list(DEFAULT_FOLDERS), delay=0.0, fail_keys=set(), reject_keys=set(),
                              unreadable=set())


def run_inproc(client, jid, mode=None):
    """Run the job in this process (allows monkeypatch), after any restart."""
    from app.server import runner
    if mode:
        assert client.post(f"/api/jobs/{jid}/{mode}").status_code == 200
    runner.run_job(jid, "test:inproc")
    return client.get(f"/api/jobs/{jid}").json()


def pst_sha_of(aid):
    from app.server import db
    return db.q1("SELECT p.pst_sha FROM psts p JOIN accounts a ON a.pst_id=p.id WHERE a.id=?", (aid,))["pst_sha"]


def raw_msg(import_id, subject="x"):
    return (f"From: a@example.test\r\nTo: b@example.test\r\nSubject: {subject}\r\n"
            f"X-PST-Import-Id: {import_id}\r\n\r\ncorps\r\n").encode()


# ------------------------------------------------------------------ 1. correspondance exacte de X-PST-Import-Id
@needs_imap
def test_find_import_id_exact_not_substring(env):
    email = create_user()
    c = imap_conn(email)
    try:
        for v in ("<s-f-12>", "s-f-123", "<s-f-1234>", "<s-f-12>x"):
            c.append("INBOX", raw_msg(v), [], None)
        one = c.find_import_id("INBOX", "X-PST-Import-Id", "<s-f-12>")
        assert len(one) == 1 and len(c.search_header("INBOX", "X-PST-Import-Id", "s-f-12")) == 4
        legacy = c.find_import_id("INBOX", "X-PST-Import-Id", "<s-f-123>")  # old undelimited value
        assert len(legacy) == 1 and legacy != one
        assert c.find_import_id("INBOX", "X-PST-Import-Id", "<s-f-1>") == []
        assert c.count_import_prefix("INBOX", "X-PST-Import-Id", "s-f-") == 4
    finally:
        c.close()


@needs_imap
def test_inflight_not_marked_ok_by_prefix_match(client, env):
    """An `inflight` never sent whose id is a prefix of another message must not become `ok`."""
    from app.server import db
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    assert run_inproc(client, jid)["status"] == "done"
    sha = pst_sha_of(aid)
    lost = db.q("SELECT * FROM items WHERE account_id=? AND kind='mail' AND status='ok' ORDER BY id LIMIT 1", (aid,))[0]
    wanted = f"<{sha}-{lost['item_key']}>"
    c = imap_conn(email)
    uids = c.find_import_id(lost["target"], "X-PST-Import-Id", wanted)
    assert len(uids) == 1
    c.select(lost["target"], readonly=False)
    c.M.uid("STORE", uids[0], "+FLAGS", "(\\Deleted)")
    c.M.expunge()
    c.append(lost["target"], raw_msg(f"{sha}-{lost['item_key']}9", "leurre"), [], None)  # superstring of the id
    c.close()
    db.ex("UPDATE items SET status='inflight' WHERE id=?", (lost["id"],))
    j = run_inproc(client, jid, "retry")
    assert j["status"] == "done", _log(client, jid)
    ids = server_import_ids(email)
    assert ids.get(wanted) == 1 and sum(v.startswith("<") for v in ids) == N_MAIL


# ------------------------------------------------------------------ 2. no APPEND blindly replayed
@needs_imap
def test_append_timeout_after_literal_not_duplicated(env, monkeypatch):
    """The APPEND succeeds server-side but the response is lost (timeout): the resume looks up the exact
    id before resending → a single copy."""
    from app.server import runner
    from app.server.imap import ImapConn, ImapError, ImapParams
    email = create_user()
    real = ImapConn.append
    calls = []

    def flaky(self, name, raw, flags, dt):
        calls.append(name)
        uid = real(self, name, raw, flags, dt)
        if len(calls) == 1:
            raise ImapError("IMAP connection: timed out", "temp")
        return uid

    monkeypatch.setattr(ImapConn, "append", flaky)
    params = ImapParams(IMAP_HOST, IMAP_PORT, "none", False, email, "pw")
    pool = runner.AppendPool(params, 1, lambda c, t: c.create(t), lambda: False, base_delay=0.05)
    try:
        iid = "<abc-1-2>"
        res = pool._do(ImapConn(params), runner.AppendTask(1, "INBOX", raw_msg(iid), [], None, 10, iid))
    finally:
        pool.close()
    assert res.ok and len(calls) == 1
    assert server_import_ids(email) == {iid: 1}


@needs_imap
def test_retry_errors_searches_before_resend(client, env):
    """Item in error « [temp] » but actually arrived on the server: retry-errors does not resend it."""
    from app.server import db
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    assert run_inproc(client, jid)["status"] == "done"
    it = db.q("SELECT * FROM items WHERE account_id=? AND kind='mail' AND status='ok' ORDER BY id LIMIT 1", (aid,))[0]
    db.ex("UPDATE items SET status='error', error='[temp] IMAP connection: timed out' WHERE id=?", (it["id"],))
    j = run_inproc(client, jid, "retry-errors")
    assert j["status"] == "done", _log(client, jid)
    assert db.q1("SELECT status FROM items WHERE id=?", (it["id"],))["status"] == "ok"
    ids = server_import_ids(email)
    assert len(ids) == N_MAIL and set(ids.values()) == {1}


# ------------------------------------------------------------------ 3. CREATE refused for a folder
@needs_imap
def test_refused_create_fails_only_that_folder(client, env, monkeypatch):
    from app.server.imap import ImapConn, ImapError, utf7_decode
    real = ImapConn.create

    def create(self, name, special_flag=None):
        if "Été" in utf7_decode(name):
            raise ImapError(f"CREATE {name} : NO refused by server policy", "perm")
        return real(self, name, special_flag)

    monkeypatch.setattr(ImapConn, "create", create)
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    j = run_inproc(client, jid)
    assert j["status"] == "done_with_errors", _log(client, jid)
    items = _items(aid)
    errs = [r for r in items.values() if r["status"] == "error"]
    assert len(errs) == 3 and all("not created" in r["error"] for r in errs)
    assert sum(r["status"] == "ok" for r in items.values()) == N_MAIL - 3
    assert j["progress"]["contacts"]["done"] == 2 and j["progress"]["calendar"]["done"] == 3


# ------------------------------------------------------------------ 4. pool wait / never « done » with inflight items
class _SlowConn:
    appended: list = []
    lock = threading.Lock()

    def __init__(self, params):
        self.M = None

    def connect(self):
        self.M = object()
        return self

    def close(self):
        self.M = None

    def append(self, name, raw, flags, dt):
        time.sleep(0.3)
        with self.lock:
            _SlowConn.appended.append((time.time(), raw))
        return "1"


def test_pool_stop_cancels_queued_and_joins_threads(env, monkeypatch):
    from app.server import runner
    from app.server.imap import ImapParams
    _SlowConn.appended = []
    monkeypatch.setattr(runner, "ImapConn", _SlowConn)
    stop = threading.Event()
    pool = runner.AppendPool(ImapParams("h", 1, "none", False, "u", "p"), 2, None, stop.is_set)
    feeder = threading.Thread(target=lambda: [pool.submit(runner.AppendTask(i, "INBOX", b"m%d" % i, [], None, 2, ""))
                                              for i in range(12)])
    feeder.start()
    time.sleep(0.4)
    stop.set()
    feeder.join(10)
    results = []
    while pool.pending > 0:
        results += pool.drain(block=True, timeout=0.5)
    pool.close()
    closed_at = time.time()
    assert not any(t.is_alive() for t in pool.threads)
    assert len(results) == 12
    cancelled = [r for r in results if r.kind == "cancel"]
    assert cancelled and len([r for r in results if r.ok]) == len(_SlowConn.appended) == 12 - len(cancelled)
    time.sleep(0.5)
    assert all(ts <= closed_at for ts, _ in _SlowConn.appended)  # no APPEND after close()


@needs_imap
def test_unfinished_items_never_reported_done(client, env, monkeypatch):
    """An item left `pending` (result lost / cancelled): the job cannot be `done`."""
    from app.server import runner
    real = runner.AppendPool._do
    seen = []

    def lossy(self, conn, t):
        seen.append(t.item_id)
        if len(seen) == 1:
            return runner.AppendResult(t.item_id, False, "cancel", "cancelled")
        return real(self, conn, t)

    monkeypatch.setattr(runner.AppendPool, "_do", lossy)
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    j = run_inproc(client, jid)
    assert j["status"] == "done_with_errors" and "unfinished" in (j["error"] or ""), j
    rep = client.get(f"/api/jobs/{jid}/report").json()
    assert rep["totals"]["mail"]["pending"] == 1
    monkeypatch.setattr(runner.AppendPool, "_do", real)
    j = run_inproc(client, jid, "retry")
    assert j["status"] == "done"
    ids = server_import_ids(email)
    assert len(ids) == N_MAIL and set(ids.values()) == {1}


# ------------------------------------------------------------------ 6. items attached to the PST fingerprint
def _upload(client, env, data, name):
    from app.server.uploads import validate
    up = client.post("/api/uploads", json={"filename": name, "size": len(data), "last_modified": 1}).json()
    cs = up["chunk_size"]
    for n in range(up["chunks"]):
        c = data[n * cs:(n + 1) * cs]
        assert client.put(f"/api/uploads/{up['id']}/chunks/{n}", content=c,
                          headers={"X-Chunk-Sha256": hashlib.sha256(c).hexdigest()}).status_code == 200
    client.post(f"/api/uploads/{up['id']}/complete")
    validate(up["id"])
    return up["id"]


@needs_imap
def test_reupload_same_pst_does_not_reimport(client, env):
    from app.server import db
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    assert run_inproc(client, jid)["status"] == "done"
    old_pid = client.get(f"/api/accounts/{aid}").json()["pst_id"]
    n_items = db.q1("SELECT COUNT(*) n FROM items WHERE account_id=?", (aid,))["n"]
    sha = pst_sha_of(aid)
    data = (env.data_dir / "src.pst").read_bytes()
    assert client.delete(f"/api/psts/{old_pid}").status_code == 200
    pid = _upload(client, env, data, f"{email}-bis.pst")
    assert pid != old_pid
    client.put(f"/api/accounts/{aid}/pst", json={"pst_id": pid})
    assert pst_sha_of(aid) == sha
    j = run_inproc(client, jid, "retry")
    assert j["status"] == "done", _log(client, jid)
    ids = server_import_ids(email)
    assert len(ids) == N_MAIL and set(ids.values()) == {1}
    assert db.q1("SELECT COUNT(*) n FROM items WHERE account_id=?", (aid,))["n"] == n_items
    assert client.get(f"/api/jobs/{jid}/errors").json() == []


def test_items_migration_rekeys_on_sha(tmp_path, monkeypatch):
    """Earlier database (items keyed by pst_id): migrated to pst_key = fingerprint, crash_skips renamed."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from app.server import db
    from app.server.config import settings
    settings.reload()
    db.reset()
    settings.ensure_dirs()
    c = sqlite3.connect(str(settings.db_path))
    old = db.SCHEMA.replace(db.ITEMS_DDL.replace("{name}", "items"), """
CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL REFERENCES accounts(id)
 ON DELETE CASCADE, pst_id INTEGER NOT NULL, kind TEXT NOT NULL, item_key TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '',
 target TEXT, status TEXT NOT NULL DEFAULT 'pending', error TEXT, warnings TEXT, subject TEXT, hash TEXT, size INTEGER,
 job_id INTEGER, updated_at REAL NOT NULL, UNIQUE (account_id, pst_id, kind, item_key));
CREATE INDEX IF NOT EXISTS items_status ON items(account_id, pst_id, kind, status);""")
    c.executescript(old)
    c.execute("INSERT INTO servers(id,name,imap_host,created_at) VALUES (1,'s','h',0)")
    c.execute("INSERT INTO accounts(id,server_id,email,created_at) VALUES (1,1,'a@x',0)")
    for pid in (1, 2):  # same file sent twice
        c.execute("INSERT INTO psts(id,filename,size,chunk_size,pst_sha,created_at,updated_at) VALUES (?,?,1,1,'cafe',0,0)",
                  (pid, f"f{pid}"))
    c.execute("INSERT INTO items(account_id,pst_id,kind,item_key,status,updated_at) VALUES (1,1,'mail','k','error',0)")
    c.execute("INSERT INTO items(account_id,pst_id,kind,item_key,status,updated_at) VALUES (1,2,'mail','k','ok',0)")
    c.execute("INSERT INTO items(account_id,pst_id,kind,item_key,status,updated_at) VALUES (1,9,'mail','z','ok',0)")
    c.execute("INSERT INTO kv(key,value) VALUES ('pst:1:crash_skips','[[\"mail\",\"F\",3]]')")
    c.commit()
    c.close()
    rows = {r["item_key"]: dict(r) for r in db.q("SELECT * FROM items")}
    assert rows["k"]["pst_key"] == "cafe" and rows["k"]["status"] == "ok" and len(rows) == 2
    assert rows["z"]["pst_key"] == "id:9"
    from app.server import crash
    assert crash.get_skips("cafe") == {("mail", "F", 3)} and db.kv_get("pst:1:crash_skips") is None
    db.reset()


# ------------------------------------------------------------------ 10. « present ≥ sent » check on our own messages only
@needs_imap
def test_folder_check_counts_only_our_messages(client, env):
    aid, email = setup_account(client, env)
    c = imap_conn(email)
    for i in range(4):
        c.append("INBOX", raw_msg(f"<autre-{i}>", f"préexistant {i}"), [], None)
    c.close()
    jid = run(client, aid)
    assert run_inproc(client, jid)["status"] == "done"
    rep = client.get(f"/api/jobs/{jid}/report").json()
    inbox = next(f for f in rep["folders"] if f["target"] == "INBOX")
    assert inbox["sent"] == 6 and inbox["present"] == 6


# ------------------------------------------------------------------ 15. folder keys and unique IMAP names
def test_folder_key_unambiguous_and_targets_unique():
    from app.server.imap import Mailbox
    from app.server.runner import folder_key, plan_folders
    assert folder_key(["A/B"]) != folder_key(["A", "B"])
    assert folder_key(["A\\", "B"]) != folder_key(["A\\/B"])
    assert folder_key(["Boîte", "Sous"]) == "Boîte/Sous"  # compatible with the old key
    fs = [FolderInfo(["A.B"], "mail", "IPF.Note", None, 1), FolderInfo(["A_B"], "mail", "IPF.Note", None, 1),
          FolderInfo(["A.B", "C"], "mail", "IPF.Note", None, 1), FolderInfo(["A_B", "C"], "mail", "IPF.Note", None, 1),
          FolderInfo(["a_b"], "mail", "IPF.Note", None, 1), FolderInfo(["Inbox2"], "mail", "IPF.Note", None, 1)]
    boxes = [Mailbox("INBOX", [], ".")]
    p = plan_folders(fs, boxes, ".", None, "")
    names = [v[0] for v in p.values()]
    assert len({n.casefold() for n in names}) == len(names)
    # the intact name keeps its form; « a_b » (case) then « A.B » (sanitized) get a suffix
    assert p["A_B"][0] == "A_B" and p["a_b"][0] == "a_b (2)" and p["A.B"][0] == "A_B (3)"
    assert p["A.B/C"][0] == "A_B (3).C" and p["A_B/C"][0] == "A_B.C"
    assert plan_folders(list(reversed(fs)), boxes, ".", None, "") == p  # deterministic


# ------------------------------------------------------------------ 16. fallback key « -x<index> » resolved on restart
@needs_imap
def test_fallback_key_resolved_on_retry(client, env):
    inbox = (["Boîte de réception"], "mail", "IPF.Note", "inbox", 4)
    fake_engine.CONFIG["folders"] = [inbox]
    fake_engine.CONFIG["unreadable"] = {"Boîte de réception#1"}
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    j = run_inproc(client, jid)
    assert j["status"] == "done_with_errors"
    items = _items(aid)
    assert [k for k, r in items.items() if r["status"] == "error"][0].endswith("-x1")
    fake_engine.CONFIG["unreadable"] = set()
    j = run_inproc(client, jid, "retry-errors")
    assert j["status"] == "done", _log(client, jid)
    items = _items(aid)
    assert len(items) == 4 and all(r["status"] == "ok" for r in items.values())
    assert not any(k.endswith("-x1") for k in items)
    ids = server_import_ids(email)
    assert len(ids) == 4 and set(ids.values()) == {1}
