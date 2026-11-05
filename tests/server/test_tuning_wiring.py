"""Auto-tuning wired into the server: servers.tuning/builders (migration, API), manual mode, BUILD_PROCESSES
override, resizable AppendPool / BuilderPool (graceful drain), decisions applied and logged, auto worker
concurrency, upgrade of a 1.0.0 database."""
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from tests.server import fake_engine
from tests.server.test_idempotence import pst_sha_of, run_inproc
from tests.server.test_ordering import _ref, expected_order
from tests.server.test_runner import (DEFAULT_FOLDERS, IMAP_HOST, _items, _log, run, server_import_ids,
                                      setup_account)

needs_imap = pytest.mark.skipif(not IMAP_HOST, reason="TEST_IMAP_HOST not set")
SCHEMA_100 = Path(__file__).resolve().parents[1] / "fixtures" / "schema-1.0.0.sql"
INBOX = (["Boîte de réception"], "mail", "IPF.Note", "inbox", 40)
SUB = (["Boîte de réception", "Projets"], "mail", "IPF.Note", None, 20)
T0 = 1_700_000_000
SRV = {"name": "Prod", "imap_host": "imap.example.test", "imap_port": 993, "imap_security": "ssl",
       "verify_tls": True, "root_folder": None, "caldav_url": None, "carddav_url": None,
       "dav_enabled": False, "workers": 4, "notes": ""}


@pytest.fixture(autouse=True)
def reset_fake():
    fake_engine.CONFIG.update(folders=list(DEFAULT_FOLDERS), delay=0.0, fail_keys=set(), reject_keys=set(),
                              unreadable=set(), dates={})
    yield
    fake_engine.CONFIG.update(folders=list(DEFAULT_FOLDERS), delay=0.0, fail_keys=set(), reject_keys=set(),
                              unreadable=set(), dates={})


def _dates(*folders):
    """Distinct dates interleaving the folders (newest-first order across folders is meaningful)."""
    out = {}
    for n, fo in enumerate(folders):
        for i in range(fo[4]):
            out[_ref(fo, i)] = T0 + (i * len(folders) + n) * 60
    return out


# ================================================================== migration
def _db_100(path: Path, servers: list[int]) -> None:
    """A database in the 1.0.0 layout (tests/fixtures/schema-1.0.0.sql) with servers of the given `workers`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.executescript(SCHEMA_100.read_text())
    for i, w in enumerate(servers):
        c.execute("INSERT INTO servers(name, imap_host, workers, created_at) VALUES (?,?,?,?)", (f"s{i}", "h", w, 1.0))
    c.commit()
    c.close()


def test_schema_100_fixture_is_the_old_layout():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA_100.read_text())
    assert {"tuning", "builders"}.isdisjoint({r[1] for r in c.execute("PRAGMA table_info(servers)")})
    assert {"tuning", "rate_bytes"}.isdisjoint({r[1] for r in c.execute("PRAGMA table_info(jobs)")})


def test_migration_tuning_manual_kept_for_non_default_workers(env):
    from app.server import db
    _db_100(env.db_path, [4, 2, 8, 4])
    db.reset()
    rows = db.q("SELECT name, workers, tuning, builders FROM servers ORDER BY id")
    assert [(r["workers"], r["tuning"], r["builders"]) for r in rows] == [
        (4, "auto", None), (2, "manual", None), (8, "manual", None), (4, "auto", None)]
    cols = {r[1] for r in db.q("PRAGMA table_info(jobs)")}
    assert {"tuning", "rate_bytes"} <= cols
    # idempotent: a second migration changes nothing (a server switched to auto stays auto)
    db.ex("UPDATE servers SET tuning='auto' WHERE name='s1'")
    c = sqlite3.connect(env.db_path, isolation_level=None)
    db._migrate(c)
    c.close()
    assert db.q1("SELECT tuning FROM servers WHERE name='s1'")["tuning"] == "auto"
    # new servers (fresh schema): default auto
    db.ex("INSERT INTO servers(name, imap_host, workers, created_at) VALUES ('n', 'h', 2, 1)")
    assert db.q1("SELECT tuning FROM servers WHERE name='n'")["tuning"] == "auto"


def test_migration_tuning_two_processes_race(env):
    from app.server import db
    _db_100(env.db_path, [4, 3])
    barrier = threading.Barrier(4)
    errors = []

    def mig():
        try:
            c = sqlite3.connect(env.db_path, timeout=60, isolation_level=None)
            c.execute("PRAGMA busy_timeout=60000")
            barrier.wait()
            db._migrate(c)
            c.close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=mig) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert not errors, errors
    c = sqlite3.connect(env.db_path)
    assert c.execute("SELECT workers, tuning FROM servers ORDER BY id").fetchall() == [(4, "auto"), (3, "manual")]
    cols = [r[1] for r in c.execute("PRAGMA table_info(servers)")]
    assert cols.count("tuning") == 1 and cols.count("builders") == 1
    c.close()


# ================================================================== API
def test_api_tuning_round_trip(client):
    s = client.post("/api/servers", json=dict(SRV, tuning="manual", workers=3, builders=5)).json()
    assert (s["tuning"], s["workers"], s["builders"]) == ("manual", 3, 5)
    assert client.get(f"/api/servers/{s['id']}").json()["builders"] == 5
    # update without the tuning fields: kept
    u = client.put(f"/api/servers/{s['id']}", json=dict(SRV, workers=6)).json()
    assert (u["tuning"], u["workers"], u["builders"]) == ("manual", 6, 5)
    u = client.put(f"/api/servers/{s['id']}", json=dict(SRV, tuning="auto", builders=None)).json()
    assert (u["tuning"], u["builders"]) == ("auto", None)
    assert [x["tuning"] for x in client.get("/api/servers").json()] == ["auto"]
    # creation without `tuning`: auto, except a non-default `workers` (clients of earlier versions)
    assert client.post("/api/servers", json=SRV).json()["tuning"] == "auto"
    assert client.post("/api/servers", json=dict(SRV, workers=2)).json()["tuning"] == "manual"
    assert client.post("/api/servers", json={k: v for k, v in SRV.items() if k != "workers"}).json()["tuning"] == "auto"
    assert client.post("/api/servers", json=dict(SRV, tuning="turbo")).status_code == 422
    assert client.post("/api/servers", json=dict(SRV, builders=-1)).status_code == 422


# ================================================================== manual / auto choices
def test_initial_tuning_manual_and_build_processes_override(monkeypatch):
    from app.server import runner, tuner
    monkeypatch.delenv("BUILD_PROCESSES", raising=False)
    manual = {"tuning": "manual", "workers": 3, "builders": 5}
    t = runner.initial_tuning(manual)
    assert (t["mode"], t["builders"], t["connections"], t["forced"]) == ("manual", 5, 3, False)
    t = runner.initial_tuning(dict(manual, builders=None))
    from app.server.builders import machine_default
    assert t["builders"] == machine_default()
    probe = tuner.MachineProbe(cpus=9, mem_available_bytes=64 << 30)
    t = runner.initial_tuning({"tuning": "auto", "workers": 7}, running_jobs=1, probe=probe)
    assert (t["mode"], t["builders"], t["connections"], t["max_builders"]) == ("auto", 8, runner.AUTO_START_CONNECTIONS, 8)
    t = runner.initial_tuning({"tuning": "auto", "workers": 7}, running_jobs=2, probe=probe)
    assert (t["builders"], t["max_builders"]) == (4, 8)  # cores split between the running jobs
    monkeypatch.setenv("BUILD_PROCESSES", "3")
    assert runner.initial_tuning(manual)["builders"] == 3 and runner.initial_tuning(manual)["forced"]
    t = runner.initial_tuning({"tuning": "auto"}, running_jobs=2, probe=probe)
    assert (t["builders"], t["forced"]) == (3, True)
    monkeypatch.setenv("BUILD_PROCESSES", "0")
    assert runner.initial_tuning(manual)["builders"] == 0


def test_consecutive_temp_threshold_follows_current_connections():
    from app.server import runner
    r = runner.JobRunner.__new__(runner.JobRunner)
    r._inflight_bytes, r.consecutive_temp, r._bytes_done = {}, 0, 0
    r.set_item = lambda *a, **k: None
    r.log = lambda *a, **k: None
    r._should_stop = lambda: False
    temp = [runner.AppendResult(i, False, "temp", "timed out") for i in range(6)]
    r.connections = 4  # threshold 12
    r._handle_results(temp)
    r.consecutive_temp = 0
    r.connections = 2  # threshold max(5, 6) = 6
    with pytest.raises(runner.JobFailed):
        r._handle_results(temp)


def test_worker_auto_concurrency(env, monkeypatch):
    from app.server import tuner, worker
    monkeypatch.delenv("WORKER_CONCURRENCY", raising=False)
    env.reload()
    w = worker.Worker()
    assert w.auto_concurrency
    monkeypatch.setattr(tuner, "probe_machine", lambda *a, **k: tuner.MachineProbe(cpus=9, mem_available_bytes=64 << 30))
    w._probe = None
    assert w.auto_limit() == 1  # nothing queued
    from app.server import db
    db.ex("INSERT INTO servers(name, imap_host, created_at) VALUES ('s','h',1)")
    db.ex("INSERT INTO accounts(server_id, email, created_at) VALUES (1,'a@b',1)")
    for _ in range(6):
        db.ex("INSERT INTO jobs(account_id, status, created_at) VALUES (1,'queued',1)")
    assert w.auto_limit() == tuner.choose_worker_concurrency(9, 6, 64 << 30) == 4
    assert worker.Worker(concurrency=2).auto_concurrency is False
    monkeypatch.setenv("WORKER_CONCURRENCY", "3")
    env.reload()
    assert worker.Worker().concurrency == 3 and not worker.Worker().auto_concurrency


# ================================================================== AppendPool resize (fake IMAP)
class _FakeConn:
    """Records every APPEND (import id) of all connections; optional per-APPEND delay."""
    appended: list = []
    lock = threading.Lock()
    delay = 0.002
    opened = 0

    def __init__(self, params):
        self.M = None

    def connect(self):
        self.M = object()
        with _FakeConn.lock:
            _FakeConn.opened += 1
        return self

    def close(self):
        self.M = None

    def append(self, name, raw, flags, dt):
        time.sleep(_FakeConn.delay)
        with _FakeConn.lock:
            _FakeConn.appended.append(raw)
            return str(len(_FakeConn.appended))

    def find_import_id(self, *a):
        return []


@pytest.fixture
def fake_imap(monkeypatch):
    from app.server import runner
    _FakeConn.appended, _FakeConn.delay, _FakeConn.opened = [], 0.002, 0
    monkeypatch.setattr(runner, "ImapConn", _FakeConn)
    return _FakeConn


def _params():
    from app.server.imap import ImapParams
    return ImapParams("h", 1, "none", False, "u", "p")


def test_append_pool_resize_mid_run_exactly_once(env, fake_imap):
    from app.server import runner
    pool = runner.AppendPool(_params(), 3, None, lambda: False)
    n = 400
    results = []
    plan = [(50, 1), (100, 6), (150, 2), (200, 5), (250, 1), (300, 4)]  # (results seen, new size)

    def feeder():
        for i in range(n):
            pool.submit(runner.AppendTask(i, "INBOX", b"m%d" % i, [], None, 3, f"<x-{i}>"))

    ft = threading.Thread(target=feeder)
    ft.start()
    while len(results) < n:
        results += pool.drain(block=True, timeout=0.5)
        while plan and len(results) >= plan[0][0]:
            pool.resize(plan.pop(0)[1])  # shrink / grow while tasks are queued and in flight
    ft.join(10)
    assert not plan
    assert pool.pending == 0
    end = time.time() + 5
    while pool.alive() != pool.n and time.time() < end:
        time.sleep(0.02)
    assert pool.alive() == pool.n
    pool.close()
    assert not any(t.is_alive() for t in pool.threads)
    ids = sorted(r.item_id for r in results)
    assert ids == list(range(n)) and all(r.ok for r in results)
    assert sorted(fake_imap.appended) == sorted(b"m%d" % i for i in range(n))  # each APPENDed exactly once


def test_append_pool_shrink_waits_for_current_task(env, fake_imap):
    """Shrink while every connection is mid-APPEND: each in-flight APPEND finishes and is reported, queued
    tasks stay queued and are served by the remaining connection."""
    from app.server import runner
    fake_imap.delay = 0.3
    pool = runner.AppendPool(_params(), 4, None, lambda: False)
    for i in range(10):
        pool.submit(runner.AppendTask(i, "INBOX", b"m%d" % i, [], None, 3, f"<x-{i}>"))
    end = time.time() + 5
    while pool.busy() < 4 and time.time() < end:
        time.sleep(0.01)
    assert pool.busy() == 4
    pool.resize(1)
    assert pool.alive() == 4  # nobody leaves with a task in hand
    results = []
    while len(results) < 10:
        results += pool.drain(block=True, timeout=1.0)
    assert pool.alive() == 1 and pool.queued() == 0
    pool.resize(3)  # grow again
    assert pool.alive() == 3
    pool.close()
    assert sorted(r.item_id for r in results) == list(range(10)) and all(r.ok for r in results)
    assert len(fake_imap.appended) == 10


def test_append_pool_records_transient_errors_and_latency(env, fake_imap, monkeypatch):
    from app.server import runner
    from app.server.imap import ImapError
    calls = []

    def flaky(self, name, raw, flags, dt):
        calls.append(raw)
        if len(calls) == 1:
            raise ImapError("NO [UNAVAILABLE] server busy", "temp")
        return "1"

    monkeypatch.setattr(_FakeConn, "append", flaky)
    pool = runner.AppendPool(_params(), 1, None, lambda: False, base_delay=0.01)
    pool.submit(runner.AppendTask(1, "INBOX", b"m", [], None, 1, "<x-1>"))
    res = pool.drain(block=True, timeout=5)
    lat, errs = pool.take_stats()
    pool.close()
    assert res[0].ok and len(errs) == 1 and "[UNAVAILABLE]" in errs[0] and lat is not None
    assert pool.take_stats() == (None, [])


# ================================================================== BuilderPool resize (fake engine)
def test_builder_pool_resize_mid_run_exactly_once(env, tmp_path):
    from app.server.builders import BuilderPool, BuildTask
    path = str(tmp_path / "f.pst")
    fake_engine.make_fake_pst(path, 1000)
    fake_engine.CONFIG.update(folders=[INBOX], delay=0.01)
    pst = fake_engine.PstFile(path)
    f = pst.folders()[0]
    tasks = [BuildTask(0, list(f.path), i, k, f"<x-{k}>") for i, k, h in pst.iter_items(f)]
    pool = BuilderPool(path, 2, in_child=False, engine_module="tests.server.fake_engine")
    got: dict = {}
    sizes = {5: 1, 12: 4, 20: 2, 28: 3, 33: 1}
    try:
        todo = list(tasks)
        while todo or pool.busy():
            while todo and pool.can_dispatch():
                pool.dispatch(todo.pop(0))
            for ev in pool.wait(1.0):
                assert ev.task.key not in got
                got[ev.task.key] = ev
                if len(got) in sizes:
                    pool.resize(sizes[len(got)])
                    assert pool.size == sizes[len(got)]
        assert pool.take_utilisation() is not None
    finally:
        pool.close()
    assert set(got) == {t.key for t in tasks}
    assert all(e.crashed is None and e.result["status"] == "ok" and e.result["hash"] for e in got.values())
    assert not pool._leaving


# ================================================================== decisions applied (runner + GreenMail)
class ScriptedTuner:
    """Stands in for AutoTuner: one scripted (builders, connections) decision per sample."""
    script = [(1, 1), (3, 4), (2, 2), (1, 3), (3, 1), (2, 4)]
    last = None

    def __init__(self, builders, max_builders, connections=4, **kw):
        self.builders, self.connections, self.max_builders = builders, connections, max_builders
        self.fallback_builders = 2
        self.samples, self.i = [], 0
        ScriptedTuner.last = self

    def feed(self, s):
        from app.server import tuner
        self.samples.append(s)
        b, c = self.script[self.i % len(self.script)]
        self.i += 1
        d = tuner.Decision(b, c, self.builders, self.connections, "scripted",
                           f"Auto-tune: scripted builders {self.builders}→{b}, IMAP connections {self.connections}→{c}")
        self.builders, self.connections = b, c
        return d


@needs_imap
def test_decisions_resize_pools_mid_run_exactly_once_in_order(client, env, monkeypatch):
    from app.server import builders, db, runner
    monkeypatch.delenv("BUILD_PROCESSES", raising=False)  # builder count free to move
    monkeypatch.setenv("AUTOTUNE_INTERVAL", "0.05")
    monkeypatch.setattr(runner.tuner, "AutoTuner", ScriptedTuner)
    resized = {"append": [], "build": []}
    real_a, real_b = runner.AppendPool.resize, builders.BuilderPool.resize

    def a_resize(self, n):
        resized["append"].append(n)
        return real_a(self, n)

    def b_resize(self, n):
        resized["build"].append(n)
        return real_b(self, n)

    monkeypatch.setattr(runner.AppendPool, "resize", a_resize)
    monkeypatch.setattr(builders.BuilderPool, "resize", b_resize)
    submitted = []
    real_submit = runner.JobRunner._submit

    def submit(self, pool, task):
        submitted.append(task.import_id)
        return real_submit(self, pool, task)

    monkeypatch.setattr(runner.JobRunner, "_submit", submit)
    fake_engine.CONFIG.update(folders=[INBOX, SUB], dates=_dates(INBOX, SUB), delay=0.03)
    aid, email = setup_account(client, env, tuning="auto")
    jid = run(client, aid)
    j = run_inproc(client, jid)
    log = _log(client, jid)
    assert j["status"] == "done", log
    n = INBOX[4] + SUB[4]
    # every item APPENDed exactly once, verified (hash recorded, post-folder check OK)
    ids = server_import_ids(email)
    assert len(ids) == n and set(ids.values()) == {1}, ids
    items = _items(aid)
    assert len(items) == n and all(r["status"] == "ok" and r["hash"] for r in items.values())
    rep = client.get(f"/api/jobs/{jid}/report").json()
    assert all(f["ok"] for f in rep["folders"]), rep["folders"]
    # submission order = newest first across folders, whatever the resizes
    sha = pst_sha_of(aid)
    assert [i[len(sha) + 2:-1] for i in submitted] == expected_order([INBOX, SUB])
    # decisions applied (both pools resized, shrink and grow) and logged
    t = ScriptedTuner.last
    assert t.i >= 3, log
    assert len(set(resized["append"][1:])) >= 2 and len(resized["build"]) >= 2, resized
    assert "Auto-tune: probe " in log and "Auto-tune: scripted builders" in log
    assert all(s.duration_s > 0 and s.queue_state in ("backlog", "starved", "balanced") for s in t.samples)
    tun = j["tuning"]
    assert tun["mode"] == "auto" and (tun["builders"], tun["connections"]) == (t.builders, t.connections)
    assert db.jload(db.q1("SELECT tuning FROM jobs WHERE id=?", (jid,))["tuning"]) == tun


@needs_imap
def test_repeated_errors_fall_back_and_log(client, env, monkeypatch):
    from app.server import runner
    monkeypatch.setenv("AUTOTUNE_INTERVAL", "0.05")
    real = runner.AppendPool.take_stats

    def noisy(self):
        lat, errs = real(self)
        return lat, errs + ["NO [UNAVAILABLE] too many connections"]

    monkeypatch.setattr(runner.AppendPool, "take_stats", noisy)
    fake_engine.CONFIG.update(folders=[INBOX], dates=_dates(INBOX), delay=0.05)
    aid, email = setup_account(client, env, tuning="auto")
    jid = run(client, aid)
    j = run_inproc(client, jid)
    lines = client.get(f"/api/jobs/{jid}/log").json()["lines"]
    log = "\n".join(l["msg"] for l in lines)
    assert j["status"] == "done", log
    assert "Auto-tune: IMAP connections 4→2 (server said [UNAVAILABLE])" in log
    fb = [l for l in lines if "falling back to" in l["msg"]]
    assert fb and fb[0]["level"] == "warning", log
    assert "repeated errors, falling back to 2 builders, 2 IMAP connections" in fb[0]["msg"]
    assert j["tuning"] == {"mode": "auto", "builders": 2, "connections": 2, "fallback": True}
    ids = server_import_ids(email)
    assert len(ids) == INBOX[4] and set(ids.values()) == {1}


@needs_imap
@pytest.mark.parametrize("forced", [None, "3"])
def test_manual_mode_honoured(client, env, monkeypatch, forced):
    from app.server import builders, runner
    if forced is None:
        monkeypatch.delenv("BUILD_PROCESSES", raising=False)
    else:
        monkeypatch.setenv("BUILD_PROCESSES", forced)
    sizes = {}
    real_a, real_b = runner.AppendPool.__init__, builders.BuilderPool.__init__

    def a_init(self, params, n, *a, **k):
        sizes["append"] = n
        return real_a(self, params, n, *a, **k)

    def b_init(self, path, n, *a, **k):
        sizes.setdefault("build", n)
        return real_b(self, path, n, *a, **k)

    monkeypatch.setattr(runner.AppendPool, "__init__", a_init)
    monkeypatch.setattr(builders.BuilderPool, "__init__", b_init)
    probe = []
    monkeypatch.setattr(runner.tuner, "probe_machine", lambda *a, **k: probe.append(1))
    fake_engine.CONFIG.update(folders=[INBOX], dates=_dates(INBOX))
    aid, email = setup_account(client, env, tuning="manual", workers=2, builders=1)
    jid = run(client, aid)
    j = run_inproc(client, jid)
    log = _log(client, jid)
    assert j["status"] == "done", log
    b = 1 if forced is None else 3
    assert sizes == {"append": 2, "build": b}
    assert j["tuning"] == {"mode": "manual", "builders": b, "connections": 2, "fallback": False}
    assert f"Tuning: manual — {b} builder(s)" in log and "IMAP connections 2" in log and not probe
    assert len(server_import_ids(email)) == INBOX[4]


@needs_imap
def test_auto_mode_build_processes_forces_builders(client, env, monkeypatch):
    monkeypatch.setenv("BUILD_PROCESSES", "1")
    fake_engine.CONFIG.update(folders=[INBOX], dates=_dates(INBOX))
    aid, email = setup_account(client, env, tuning="auto")
    jid = run(client, aid)
    j = run_inproc(client, jid)
    log = _log(client, jid)
    assert j["status"] == "done", log
    assert "→ 1 builders (BUILD_PROCESSES), IMAP connections 4" in log
    assert j["tuning"]["mode"] == "auto" and j["tuning"]["builders"] == 1
