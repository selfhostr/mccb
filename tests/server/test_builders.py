"""Parallel builder processes: results, crash isolation (respawn), pause/resume without duplicates,
byte rate, aggregated cid warnings, in-process mode (BUILD_PROCESSES=0)."""
import time

import pytest

from tests.server import fake_engine
from tests.server.test_idempotence import run_inproc
from tests.server.test_runner import (DEFAULT_FOLDERS, IMAP_HOST, WorkerThread, _items, _log, run, server_import_ids,
                                      setup_account, wait_job)

needs_imap = pytest.mark.skipif(not IMAP_HOST, reason="TEST_IMAP_HOST not set")
INBOX = (["Boîte de réception"], "mail", "IPF.Note", "inbox", 24)
SUB = (["Boîte de réception", "Projets"], "mail", "IPF.Note", None, 9)


@pytest.fixture(autouse=True)
def reset_fake():
    fake_engine.CONFIG.update(folders=list(DEFAULT_FOLDERS), delay=0.0, fail_keys=set(), reject_keys=set(),
                              unreadable=set(), warnings={})
    yield
    fake_engine.CONFIG.update(folders=list(DEFAULT_FOLDERS), delay=0.0, fail_keys=set(), reject_keys=set(),
                              unreadable=set(), warnings={})


# ------------------------------------------------------------------ BuilderPool unit
def _pool_tasks(path):
    from app.server.builders import BuildTask
    pst = fake_engine.PstFile(path)
    out = []
    for fi, f in enumerate(pst.folders()):
        if f.kind == "mail":
            out += [BuildTask(fi, list(f.path), i, k, f"<x-{k}>") for i, k, h in pst.iter_items(f)]
    return out


def test_builder_pool_results_and_crash_respawn(env, monkeypatch, tmp_path):
    from app.server.builders import BuilderPool
    path = str(tmp_path / "f.pst")
    fake_engine.make_fake_pst(path, 1000)
    fake_engine.CONFIG.update(folders=[INBOX], fail_keys={fake_engine.item_key(INBOX[0], 2)},
                              reject_keys={fake_engine.item_key(INBOX[0], 3)})
    bad = fake_engine.item_key(INBOX[0], 5)
    monkeypatch.setenv("PSTMIG_TEST_CRASH", bad)
    tasks = _pool_tasks(path)
    pool = BuilderPool(path, 3, in_child=True, engine_module="tests.server.fake_engine")
    got = {}
    try:
        todo = list(tasks)
        while todo or pool.busy():
            while todo and pool.can_dispatch():
                pool.dispatch(todo.pop(0))
            for ev in pool.wait(1.0):
                got[ev.task.key] = ev
    finally:
        pool.close()
    assert set(got) == {t.key for t in tasks}
    assert got[bad].crashed == -11 and got[bad].result is None  # SIGSEGV, attributed to exactly this item
    st = {k: (e.result or {}).get("status") for k, e in got.items() if k != bad}
    assert st[fake_engine.item_key(INBOX[0], 2)] == "error" and st[fake_engine.item_key(INBOX[0], 3)] == "rejected"
    assert sum(v == "ok" for v in st.values()) == len(tasks) - 3  # the respawned builder kept going
    ok = got[fake_engine.item_key(INBOX[0], 0)].result
    assert b"X-PST-Import-Id: <x-" in ok["raw"] and len(ok["hash"]) == 64


def test_builder_pool_mismatched_key_is_an_item_error(env, tmp_path):
    from app.server.builders import BuilderPool, BuildTask
    path = str(tmp_path / "f.pst")
    fake_engine.make_fake_pst(path, 1000)
    fake_engine.CONFIG.update(folders=[INBOX])
    pool = BuilderPool(path, 1, in_child=False, engine_module="tests.server.fake_engine")
    try:
        pool.dispatch(BuildTask(0, list(INBOX[0]), 1, "not-the-key", "<x>"))
        evs = []
        while not evs:
            evs = pool.wait(1.0)
    finally:
        pool.close()
    assert evs[0].result["status"] == "error" and "mismatch" in evs[0].result["error"]


def test_default_processes(monkeypatch):
    from app.server import builders
    from app.server import tuner
    monkeypatch.delenv("BUILD_PROCESSES", raising=False)
    cpus = {"n": 12}
    monkeypatch.setattr(tuner, "probe_machine", lambda *a, **k: tuner.MachineProbe(cpus["n"], 64 << 30))
    assert builders.default_processes() == 11
    cpus["n"] = 4
    assert builders.default_processes() == 3
    cpus["n"] = 1
    assert builders.default_processes() == 1
    monkeypatch.setenv("BUILD_PROCESSES", "0")
    assert builders.default_processes() == 0


# ------------------------------------------------------------------ job level
@needs_imap
def test_parallel_import_pause_resume_no_duplicates(client, env, monkeypatch):
    monkeypatch.setenv("BUILD_PROCESSES", "4")
    fake_engine.CONFIG.update(folders=[INBOX, SUB], delay=0.2)
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    with WorkerThread():
        wait_job(client, jid, ("running",), cond=lambda j: j["progress"]["mail"]["done"] >= 6)
        assert client.post(f"/api/jobs/{jid}/pause").status_code == 200
        j = wait_job(client, jid, ("paused",), cond=lambda j: j["rate"] is None, timeout=30)
        n_paused = j["progress"]["mail"]["done"]
        assert n_paused < 33
        # nothing half-recorded: every item row is ok, or pending/inflight to be resolved on resume
        assert all(r["status"] in ("ok", "pending", "inflight") for r in _items(aid).values())
        fake_engine.CONFIG["delay"] = 0.0
        assert client.post(f"/api/jobs/{jid}/resume").status_code == 200
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"), cond=lambda j: j["finished_at"])
    assert j["status"] == "done", _log(client, jid)
    assert j["progress"]["mail"] == {"done": 33, "total": 33, "errors": 0}
    assert "4 builder process(es)" in _log(client, jid)
    assert j["rate_bytes"] and j["rate_bytes"] > 0
    ids = server_import_ids(email)
    assert len(ids) == 33 and set(ids.values()) == {1}


@needs_imap
def test_builder_crash_inprocess_respawns_and_continues(client, env, monkeypatch):
    """Builder SIGSEGV with the job running in this process: no job-process death, item marked
    "libpff crash", builder respawned, the rest imported once."""
    from app.server import crash
    monkeypatch.setenv("BUILD_PROCESSES", "2")  # builders required: the hook must never fire in this process
    monkeypatch.setattr(crash, "IN_CHILD", True)  # hooks active in the builders (in_child flag)
    fake_engine.CONFIG.update(folders=[INBOX])
    bad = {fake_engine.item_key(INBOX[0], i) for i in (4, 11)}
    monkeypatch.setenv("PSTMIG_TEST_CRASH", ",".join(sorted(bad)))
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    from app.server import db
    db.ex("UPDATE jobs SET status='running' WHERE id=?", (jid,))
    j = run_inproc(client, jid)
    assert j["status"] == "done_with_errors", _log(client, jid)
    items = _items(aid)
    assert {k for k, r in items.items() if r["status"] == "error"} == bad
    assert all(items[k]["error"].startswith("libpff crash") for k in bad)
    log = _log(client, jid)
    assert log.count("Subprocess crash (signal SIGSEGV") == 2 and "builder process restarted" in log
    ids = server_import_ids(email)
    assert len(ids) == 22 and set(ids.values()) == {1}
    assert crash.get_crashes(jid) == 0


@needs_imap
def test_inprocess_mode_and_cid_warnings_aggregated(client, env, monkeypatch):
    monkeypatch.setenv("BUILD_PROCESSES", "0")
    k = [fake_engine.item_key(INBOX[0], i) for i in range(4)]
    fake_engine.CONFIG.update(folders=[INBOX], warnings={
        k[0]: ["cid-unresolved:image001.png@01D9"], k[1]: ["embedded:cid-unresolved:image002.png@01D9"],
        k[2]: ["rtf-converted", "cid-unresolved:image003.png@01D9"], k[3]: ["rtf-converted"]})
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    with WorkerThread():
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"), cond=lambda j: j["finished_at"])
    assert j["status"] == "done", _log(client, jid)
    log = _log(client, jid)
    assert "builder process" not in log and "1 indexing process(es)" in log  # dates read out of process
    assert "Boîte de réception: 3 message(s) reference images missing from the PST (quoted replies) — harmless" in log
    assert "cid-unresolved" not in log and log.count("rtf-converted") == 2
    rep = client.get(f"/api/jobs/{jid}/report").json()
    warned = {w["item_key"]: w["warnings"] for w in rep["item_warnings"]}
    assert warned[k[0]] == ["cid-unresolved:image001.png@01D9"] and len(warned) == 4
    assert len(server_import_ids(email)) == 24


# ------------------------------------------------------------------ v1.1.0 regressions
class _BrokenConn:
    """Pipe end whose recv() fails with something else than EOF (corrupt / truncated pickle, OSError)."""

    def __init__(self, conn):
        self._c = conn

    def fileno(self):
        return self._c.fileno()

    def poll(self, *a):
        return True

    def recv(self):
        raise OSError("simulated recv failure")

    def send(self, obj):
        return self._c.send(obj)

    def close(self):
        return self._c.close()


def test_builder_pool_recv_error_kills_builder_and_reports_crash(env, tmp_path):
    """Regression: a non-EOF recv() error broke the service loop, leaving the builder alive with its task
    held forever (wait() returned at once, _mail_parallel spun)."""
    from app.server import builders
    from app.server.builders import BuilderPool
    path = str(tmp_path / "f.pst")
    fake_engine.make_fake_pst(path, 1000)
    fake_engine.CONFIG.update(folders=[INBOX])
    tasks = _pool_tasks(path)
    pool = BuilderPool(path, 1, in_child=False, engine_module="tests.server.fake_engine")
    try:
        pool.dispatch(tasks[0])
        slot = pool.slots[0]
        slot.conn = _BrokenConn(slot.conn)
        evs, deadline = [], time.time() + 30
        while not evs and time.time() < deadline:
            evs = pool.wait(1.0)
        assert len(evs) == 1 and evs[0].task is tasks[0]
        assert evs[0].crashed == -9 and "unreadable" in evs[0].note
        assert not builders.is_oom_kill(evs[0])  # killed by the pool: a crash, not an OOM retry
        assert not pool.busy() and pool.size == 1
        # the respawned builder keeps working
        pool.dispatch(tasks[1])
        got = []
        while not got and time.time() < deadline:
            got = pool.wait(1.0)
        assert got[0].result["status"] == "ok"
    finally:
        pool.close()


def test_is_oom_kill():
    from app.server.builders import BuildEvent, is_oom_kill
    assert is_oom_kill(BuildEvent(None, crashed=-9))
    assert not is_oom_kill(BuildEvent(None, crashed=-9, note="result unreadable"))
    assert not is_oom_kill(BuildEvent(None, crashed=-11)) and not is_oom_kill(BuildEvent(None, result={}))


def test_machine_default_honours_probe(monkeypatch):
    """Regression: manual mode with `builders` unset used os.cpu_count() - 1, ignoring the cgroup CPU quota,
    the affinity mask and the available memory."""
    from app.server import builders, tuner
    probe = {"p": tuner.MachineProbe(cpus=3, mem_available_bytes=64 << 30)}
    monkeypatch.setattr(tuner, "probe_machine", lambda *a, **k: probe["p"])
    monkeypatch.setattr(builders.os, "cpu_count", lambda: 64)
    assert builders.machine_default() == 2
    probe["p"] = tuner.MachineProbe(cpus=16, mem_available_bytes=600 << 20)  # 300 MiB budget -> 1 builder
    assert builders.machine_default() == 1
    monkeypatch.delenv("BUILD_PROCESSES", raising=False)
    assert builders.default_processes() == 1


@needs_imap
def test_builder_oom_kill_is_retried_once_with_fewer_builders(client, env, monkeypatch, tmp_path):
    """A builder SIGKILLed (OOM killer) is not a libpff crash: the item is rebuilt once with one builder
    fewer; killed again, it becomes an out-of-memory error (crash counter untouched)."""
    from app.server import crash
    monkeypatch.setenv("BUILD_PROCESSES", "3")
    monkeypatch.setattr(crash, "IN_CHILD", True)
    fake_engine.CONFIG.update(folders=[INBOX])
    once, always = fake_engine.item_key(INBOX[0], 4), fake_engine.item_key(INBOX[0], 11)
    monkeypatch.setenv("PSTMIG_TEST_OOM", f"{once}@{tmp_path / 'oom-flag'},{always}")
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    from app.server import db
    db.ex("UPDATE jobs SET status='running' WHERE id=?", (jid,))
    j = run_inproc(client, jid)
    log = _log(client, jid)
    assert j["status"] == "done_with_errors", log
    items = _items(aid)
    assert {k for k, r in items.items() if r["status"] == "error"} == {always}, log
    assert items[always]["error"].startswith(crash.OOM_MSG) and "libpff" not in items[always]["error"]
    assert items[once]["status"] == "ok"
    assert "probably out of memory: retrying it once with 2 builder(s)" in log
    assert "probably out of memory: retrying it once with 1 builder(s)" in log
    assert "Subprocess crash" not in log and "SIGKILL again" in log
    assert crash.get_crashes(jid) == 0
    ids = server_import_ids(email)
    assert len(ids) == 23 and set(ids.values()) == {1}
