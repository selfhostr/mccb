"""Costs that must not grow with the size of a migration, and what they must not change: no chunk list per PUT, no
recount per second, batched item writes, one prefix search on resume, pressure shared between connections and jobs."""
import threading
import time

import pytest

from tests.server import fake_engine
from tests.server.test_runner import DEFAULT_FOLDERS

SHA = "abcd1234"


def _runner(env, status="running", n_jobs=1):
    """A JobRunner on minimal rows (no IMAP): account 1, server 1, PST 1, job 1."""
    from app.server import db, runner
    now = time.time()
    db.ex("INSERT INTO servers(name, imap_host, dav_enabled, created_at) VALUES ('s', 'h', 0, ?)", (now,))
    db.ex("INSERT INTO psts(id, filename, size, chunk_size, status, path, pst_sha, created_at, updated_at) "
          "VALUES (1, 'f', 1, 1, 'valid', '/nowhere', ?, ?, ?)", (SHA, now, now))
    db.ex("INSERT INTO accounts(server_id, email, password_enc, pst_id, created_at) VALUES (1, 'a@b', NULL, 1, ?)", (now,))
    for _ in range(n_jobs):
        db.ex("INSERT INTO jobs(account_id, status, progress, created_at) VALUES (1, ?, '{}', ?)", (status, now))
    return runner.JobRunner(1, "w")


def _items(r, n, status="inflight", folder="F", target="T", kind="mail"):
    return [r.upsert_item(kind, f"k{i}", folder, status, target=target, hash="h", size=10) for i in range(n)]


def _status_counts():
    from app.server import db
    return {x["status"]: x["n"] for x in db.q("SELECT status, COUNT(*) n FROM items GROUP BY status")}


# ================================================================== progress
def test_recount_is_spaced_by_its_cost(env, monkeypatch):
    from app.server import runner
    r = _runner(env)
    calls = []
    real = runner.JobRunner.recompute_progress

    def slow(self):
        calls.append(1)
        real(self)
        time.sleep(0.02)  # 2 % of the time: not before 1 s

    monkeypatch.setattr(runner.JobRunner, "recompute_progress", slow)
    r.flush(force=True)
    assert len(calls) == 1 and r._next_recount >= time.time() + 0.5
    r._last_flush = 0.0
    r.flush()
    assert len(calls) == 1  # too soon, nothing recounted
    r.current_folder = "Other"
    r._last_flush = 0.0
    r.flush()
    from app.server import db
    assert len(calls) == 1 and db.q1("SELECT current_folder c FROM jobs")["c"] == "Other"  # the folder is still shown
    r._next_recount = 0.0
    r._last_flush = 0.0
    r.flush()
    assert len(calls) == 2
    r.flush(force=True)
    assert len(calls) == 3


def test_mail_average_size_reads_the_table_once(env, monkeypatch):
    from app.server import db
    r = _runner(env)
    ids = _items(r, 4)
    db.ex(f"UPDATE items SET status='ok' WHERE id IN ({ids[0]}, {ids[1]})")
    assert r._mail_avg_size() == 10
    queries = []
    conn = db.conn()
    conn.set_trace_callback(queries.append)
    try:
        r._ok_items, r._bytes_done = 2, 50  # this run: 2 mails, 50 bytes
        assert r._mail_avg_size() == (20 + 50) / 4
    finally:
        conn.set_trace_callback(None)
    assert not queries


# ================================================================== item writes
def test_results_are_written_in_batches_and_never_lost(env, monkeypatch):
    from app.server import db, runner
    r = _runner(env)
    ids = _items(r, 6)
    r._ok_buf_t = time.monotonic()  # a batch has just been written
    r._handle_results([runner.AppendResult(i, True, size=10) for i in ids[:3]])
    assert _status_counts() == {"inflight": 6}  # kept in memory: on a crash they are searched on the server
    r.recompute_progress()  # every reader of the table writes them first
    assert _status_counts() == {"ok": 3, "inflight": 3}
    assert r.progress["mail"]["done"] == 3 and r._ok_items == 3
    r._ok_buf_t = time.monotonic()
    r._handle_results([runner.AppendResult(ids[3], False, "cancel", "x")])
    r._flush_items(force=True)
    row = db.q1("SELECT status, error, job_id FROM items WHERE id=?", (ids[3],))
    assert (row["status"], row["error"], row["job_id"]) == ("pending", None, 1)


def test_full_buffer_is_written_at_once(env):
    from app.server import runner
    r = _runner(env)
    ids = _items(r, runner.OK_BUFFER_ITEMS)
    r._ok_buf_t = time.monotonic()
    r._handle_results([runner.AppendResult(i, True, size=1) for i in ids])
    assert _status_counts() == {"ok": runner.OK_BUFFER_ITEMS}


def test_fatal_result_writes_the_others_first(env):
    from app.server import runner
    r = _runner(env)
    ids = _items(r, 2)
    r._ok_buf_t = time.monotonic()
    with pytest.raises(runner.JobFailed):
        r._handle_results([runner.AppendResult(ids[0], True, size=1),
                           runner.AppendResult(ids[1], False, "fatal", "over quota")])
    assert _status_counts() == {"ok": 1, "error": 1}


def test_row_committed_before_the_task_is_submitted(env, monkeypatch):
    """The rule of the whole import: no APPEND without its committed `inflight` row. Several rows, one transaction."""
    from app.server import db, runner
    r = _runner(env)
    seen = []

    class Pool:
        pending = 0

    def submit(self, pool, task):
        # read from another connection: sees only what is committed
        got = []
        t = threading.Thread(target=lambda: got.append(db.q1("SELECT status FROM items WHERE id=?", (task.item_id,))["status"]))
        t.start()
        t.join()
        seen.append((task.import_id, got[0]))

    monkeypatch.setattr(runner.JobRunner, "_submit", submit)
    res = {"status": "ok", "raw": b"x", "flags": [], "internaldate": None, "hash": "h", "subject": "s", "warnings": []}
    r._defer_inflight = True
    for i in range(3):
        r._record_built("F", i, f"k{i}", "T", Pool(), f"<id{i}>", dict(res))
    assert not seen and db.q1("SELECT COUNT(*) n FROM items")["n"] == 0
    r._defer_inflight = False
    r._commit_inflight(Pool())
    assert seen == [(f"<id{i}>", "inflight") for i in range(3)]  # in order


def test_item_log_lines_are_bounded(env, monkeypatch):
    from app.server import db, runner
    monkeypatch.setattr(runner, "ITEM_LOG_LIMIT", 5)
    r = _runner(env)
    for i in range(20):
        r._item_log(f"line {i}", "warning")
    lines = [x["msg"] for x in db.q("SELECT msg FROM job_logs ORDER BY id")]
    assert len(lines) == 5 and "only in the report" in lines[-1]


# ================================================================== resume
def test_item_rows_share_one_row_for_finished_items(env):
    from app.server import db, runner
    r = _runner(env)
    ids = _items(r, 4)
    db.ex(f"UPDATE items SET status='ok' WHERE id IN ({ids[0]}, {ids[1]})")
    db.ex(f"UPDATE items SET status='skipped' WHERE id={ids[2]}")
    rows = r.item_rows("mail", "F")
    assert rows["k0"] is rows["k1"] is rows["k2"] is runner.DONE_ROW
    assert rows["k3"]["id"] == ids[3] and rows["k3"]["status"] == "inflight"
    assert not r.wants(rows["k0"])
    r.mode = "errors"
    assert not r.wants(rows["k0"])
    assert r.fallback_rows(rows) == {}


class _BulkConn:
    def __init__(self, present):
        self.present, self.bulk, self.single = present, [], []

    def import_ids_with_prefix(self, name, header, prefix):
        self.bulk.append((name, prefix))
        return {k: ["1"] for k in self.present}

    def find_import_id(self, name, header, import_id):
        self.single.append(import_id)
        return ["1"] if import_id[1:-1] in self.present else []


def test_resolve_on_server_many_items_one_search(env):
    from app.server import runner
    r = _runner(env)
    n = runner.BULK_RESOLVE_MIN
    _items(r, n)
    r.upsert_item("mail", "other", "G", "inflight", target="T2", hash="h")
    conn = _BulkConn({f"{SHA}-k{i}" for i in range(n) if i % 2} | {f"{SHA}-other"})
    rows = {**r.item_rows("mail", "F"), **r.item_rows("mail", "G")}
    r._resolve_on_server(conn, rows)
    assert conn.bulk == [("T", f"{SHA}-")] and conn.single == [f"<{SHA}-other>"]
    assert _status_counts() == {"ok": n // 2 + 1, "pending": n // 2}  # present: ok; absent: to send


def test_import_ids_with_prefix_is_exact():
    from app.server.imap import ImapConn, ImapParams
    c = ImapConn(ImapParams("h", 1, "none", False, "u", "p"))
    c.search_header = lambda n, h, p: ["1", "2", "3"]
    c.fetch_header_values = lambda n, u, h: {"1": ["<s-1>"], "2": ["s-2"], "3": ["<s-1>"]}
    assert c.import_ids_with_prefix("F", "X", "s-") == {"s-1": ["1", "3"], "s-2": ["2"]}


def test_sample_ids_are_distinct_and_bounded(env):
    from app.server import db
    from app.server.config import settings
    r = _runner(env)
    ids = _items(r, 200)
    db.ex("UPDATE items SET status='ok'")
    got = r._sample_ids()
    assert len(got) == min(settings.verify_sample, 200) == len(set(got)) and set(got) <= set(ids)


# ================================================================== IMAP
class _FakeM:
    def __init__(self):
        self.calls, self.literal, self.untagged_responses = [], None, {}

    def _simple_command(self, *a):
        self.calls.append(("command", a, self.literal))
        return "OK", [b"[APPENDUID 1 9] done"]

    def append(self, mailbox, flags, dt, message):
        self.calls.append(("append", (mailbox, flags, dt), message))
        return "OK", [b"done"]


def test_append_keeps_a_clean_literal_as_is():
    from app.server.imap import ImapConn, ImapParams
    c = ImapConn(ImapParams("h", 1, "none", False, "u", "p"))
    c.M = _FakeM()
    raw = b"Subject: x\r\n\r\nbody\r\n"
    assert c.append("Dossier", raw, ["\\Seen"], None) == "9"
    kind, args, lit = c.M.calls[0]
    assert kind == "command" and args == ("APPEND", '"Dossier"', "(\\Seen)", None) and lit is raw
    for dirty in (b"a\nb\r\n", b"a\rb\r\n"):  # imaplib normalises the line endings of the others, as ever
        c.M = _FakeM()
        c.append("D", dirty, [], None)
        assert c.M.calls[0][0] == "append"


@pytest.mark.parametrize("text,kind", [("NO Too many connections from your IP", "temp"),
                                       ("NO Maximum number of connections from user+IP exceeded", "temp"),
                                       ("NO [THROTTLED] slow down", "temp"), ("NO Rate limit reached", "temp"),
                                       ("NO too many recipients", "perm"), ("NO message too big", "perm")])
def test_overload_answers_are_temporary(text, kind):
    from app.server.imap import classify_response
    assert classify_response(text) == kind


class _PressureConn:
    fail = 1
    times: list = []

    def __init__(self, params):
        self.M = None

    def connect(self):
        self.M = object()
        return self

    def close(self):
        self.M = None

    def append(self, name, raw, flags, dt):
        from app.server.imap import ImapError
        _PressureConn.times.append(time.monotonic())
        if _PressureConn.fail > 0:
            _PressureConn.fail -= 1
            raise ImapError("NO [UNAVAILABLE] busy", "temp")
        return "1"

    def find_import_id(self, *a):
        return []


def test_a_server_error_holds_every_connection_of_the_pool(env, monkeypatch):
    from app.server import runner
    from app.server.imap import ImapParams
    monkeypatch.setattr(runner, "ImapConn", _PressureConn)
    _PressureConn.fail, _PressureConn.times = 1, []
    pool = runner.AppendPool(ImapParams("h", 1, "none", False, "u", "p"), 3, None, lambda: False, base_delay=0.3)
    pool.submit(runner.AppendTask(1, "INBOX", b"m", [], None, 1, "<1>"))
    deadline = time.monotonic() + 5
    while not pool._errors and time.monotonic() < deadline:
        time.sleep(0.005)
    t0 = time.monotonic()
    pool.submit(runner.AppendTask(2, "INBOX", b"m", [], None, 1, "<2>"))  # another connection is free, but must wait
    got = []
    while len(got) < 2 and time.monotonic() < deadline:
        got += pool.drain(block=True, timeout=0.5)
    pool.close()
    assert sorted(r.item_id for r in got) == [1, 2] and all(r.ok for r in got)
    assert _PressureConn.times[1] >= t0 + 0.1, _PressureConn.times
    assert pool._err_streak == 0  # reset by the success


# ================================================================== shared server budget
def test_server_share(env, monkeypatch):
    from app.server import db
    r = _runner(env, n_jobs=3)
    monkeypatch.setenv("AUTOTUNE_MAX_SERVER_CONNECTIONS", "20")
    db.ex("UPDATE jobs SET tuning='{\"connections\": 2}' WHERE id=2")
    db.ex("UPDATE jobs SET tuning='{\"connections\": 16}' WHERE id=3")
    limit, hold, running = r._server_share([])
    assert (limit, hold, running) == (6, False, 3)  # max(20 // 3, 20 - 18)
    db.ex("UPDATE jobs SET tuning='{\"connections\": 1}'")
    assert r._server_share([])[0] == 18  # 20 - 2 others: what they leave free
    # a job that sees an error tells the others
    assert r._server_share(["NO [LIMIT]"])[1] is True
    from app.server import runner
    other = runner.JobRunner(2, "w")
    assert other._server_share([])[1] is True
    db.kv_set("server:1:pressure", str(time.time() - runner.SERVER_PRESSURE_SECONDS - 1))
    assert other._server_share([])[1] is False


def test_memory_guard_logs_changes(env, monkeypatch):
    from app.server import db, tuner
    r = _runner(env)
    monkeypatch.setenv("MEM_GUARD_MB", "100")
    free = {"v": 50 << 20}
    monkeypatch.setattr(tuner, "mem_available", lambda *a: free["v"])
    assert r._memory_is_low() is True
    free["v"] = 1 << 30
    assert r._memory_is_low() is True  # read at most once per second
    r._mem_t = 0.0
    assert r._memory_is_low() is False
    r._mem_t = 0.0
    free["v"] = 0  # unknown: no guard
    assert r._memory_is_low() is False
    assert sum("Memory guard" in x["msg"] for x in db.q("SELECT msg FROM job_logs")) == 2


# ================================================================== DB events
def test_heartbeat_makes_no_event(env):
    from app.server import db
    _runner(env)
    n = db.q1("SELECT COUNT(*) n FROM events")["n"]
    db.ex("UPDATE jobs SET heartbeat=1 WHERE id=1")
    assert db.q1("SELECT COUNT(*) n FROM events")["n"] == n
    db.ex("UPDATE jobs SET phase='mail' WHERE id=1")
    assert db.q1("SELECT COUNT(*) n FROM events")["n"] == n + 1


def test_old_job_trigger_is_replaced(env):
    from app.server import db
    db.ex("DROP TRIGGER ev_job_u")
    db.ex("CREATE TRIGGER ev_job_u AFTER UPDATE ON jobs BEGIN INSERT INTO events(kind, ref_id) VALUES ('job', NEW.id); END")
    db._migrate(db.conn())
    db._migrate(db.conn())
    assert "UPDATE OF" in db.q1("SELECT sql FROM sqlite_master WHERE name='ev_job_u'")["sql"]


# ================================================================== worker
def test_worker_admits_more_jobs_while_cores_stay_idle(env, monkeypatch):
    from app.server import db, tuner, worker
    monkeypatch.delenv("WORKER_CONCURRENCY", raising=False)
    env.reload()
    w = worker.Worker()
    monkeypatch.setattr(tuner, "probe_machine", lambda *a, **k: tuner.MachineProbe(cpus=9, mem_available_bytes=64 << 30))
    monkeypatch.setattr(tuner, "io_pressure", lambda *a: 0.0)
    db.ex("INSERT INTO servers(name, imap_host, created_at) VALUES ('s','h',1)")
    db.ex("INSERT INTO accounts(server_id, email, created_at) VALUES (1,'a@b',1)")
    for _ in range(9):
        db.ex("INSERT INTO jobs(account_id, status, created_at) VALUES (1,'queued',1)")
    w.threads = {i: None for i in range(4)}  # the 4 of the static choice are running
    idle = {"v": 0.0}
    monkeypatch.setattr(tuner, "proc_stat", lambda *a: (idle["v"], idle["v"] * 2))  # half of the 9 cores idle
    assert w.auto_limit() == 4
    for expected in (5, 6):
        idle["v"] += 100
        w._extra_t = 0.0
        assert w.auto_limit() == expected
        w.threads[expected] = None
    # a target complained: no more
    db.kv_set("server:1:pressure", str(time.time()))
    idle["v"] += 100
    w._extra_t = 0.0
    w._probe_t = 0
    assert w.auto_limit() == 6
    # no idle core: one less for the next admissions
    monkeypatch.setattr(tuner, "proc_stat", lambda *a: (0.0, idle["v"]))
    w._extra_t = 0.0
    w.auto_limit()
    idle["v"] += 100
    w._extra_t = 0.0
    assert w.auto_limit() == 5


def test_worker_starts_nothing_more_when_disk_or_memory_is_short(env, monkeypatch):
    from app.server import db, tuner, worker
    monkeypatch.delenv("WORKER_CONCURRENCY", raising=False)
    env.reload()
    w = worker.Worker()
    probe = {"p": tuner.MachineProbe(cpus=33, mem_available_bytes=64 << 30)}
    monkeypatch.setattr(tuner, "probe_machine", lambda *a, **k: probe["p"])
    pressure = {"v": 80.0}
    monkeypatch.setattr(tuner, "io_pressure", lambda *a: pressure["v"])
    db.ex("INSERT INTO servers(name, imap_host, created_at) VALUES ('s','h',1)")
    db.ex("INSERT INTO accounts(server_id, email, created_at) VALUES (1,'a@b',1)")
    for _ in range(20):
        db.ex("INSERT INTO jobs(account_id, status, created_at) VALUES (1,'queued',1)")
    assert w.auto_limit() == 16  # nothing running: the first ones always start
    w.threads = {i: None for i in range(3)}
    assert w.auto_limit() == 3
    pressure["v"], w._probe_t = 0.0, 0
    assert w.auto_limit() == 16
    probe["p"], w._probe_t = tuner.MachineProbe(cpus=33, mem_available_bytes=512 << 20), 0
    assert w.auto_limit() == 3


# ================================================================== uploads
def test_chunk_reply_has_no_list_of_chunks(client, env):
    import hashlib
    data = b"!BDN" + b"x" * 200_000
    up = client.post("/api/uploads", json={"filename": "a.pst", "size": len(data), "last_modified": 1}).json()
    cs = up["chunk_size"]
    for n in (0, 1):
        c = data[n * cs:(n + 1) * cs]
        r = client.put(f"/api/uploads/{up['id']}/chunks/{n}", content=c,
                       headers={"X-Chunk-Sha256": hashlib.sha256(c).hexdigest(), "Content-Type": "application/octet-stream"})
        assert r.status_code == 200 and r.json() == {"n": n, "ok": True}
    assert client.get(f"/api/uploads/{up['id']}").json()["received"] == [0, 1]
    from app.server import repo
    assert repo.event_object("upload", up["id"]) == {"id": up["id"], "status": "uploading", "received_count": 2}


@pytest.fixture(autouse=True)
def reset_fake():
    fake_engine.CONFIG.update(folders=list(DEFAULT_FOLDERS), delay=0.0, fail_keys=set(), reject_keys=set(),
                              unreadable=set(), dates={})
    yield
