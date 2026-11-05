"""Auto-tuning end to end (real engine, real web + worker, GreenMail + Radicale), and upgrade of a database
written by mccb 1.0.0 in the middle of a migration (ok + inflight items): resume without duplicates."""
from __future__ import annotations

import sqlite3
import time

import pytest

from tests.e2e.helpers import (FIXTURES, IMAP_HOST, IMAP_PORT, ImapView, Stack, create_user, source_model,
                               verify_on_servers)

SCHEMA_100 = FIXTURES / "schema-1.0.0.sql"
FIXTURE = FIXTURES / "enron.pst"


def _run_and_check(stack, srv: dict, path, expect_mode: str):
    user = create_user(prefix="tune")
    acc = stack.create_account(srv["id"], user, "pw")
    pst = stack.upload(path, bad_chunk=None)
    assert pst["status"] == "valid", pst
    model = source_model(path, pst["pst_sha"])
    stack.api("PUT", f"/api/accounts/{acc['id']}/pst", json={"pst_id": pst["id"]})
    return user, acc, pst, model


def test_auto_tuned_job_exactly_once(stack_factory):
    stack = stack_factory(AUTOTUNE_INTERVAL="0.5")
    srv = stack.create_server(name="auto", tuning="auto")
    assert srv["tuning"] == "auto"
    user, acc, pst, model = _run_and_check(stack, srv, FIXTURE, "auto")
    jid = stack.api("POST", "/api/runs", json={"account_ids": [acc["id"]]}).json()[0]["id"]
    j = stack.wait_job(jid, timeout=300)
    final = "done_with_errors" if model["errors"] else "done"
    assert j["status"] == final, stack.job_log(jid)
    rep = stack.api("GET", f"/api/jobs/{jid}/report").json()
    got = verify_on_servers(stack, user, pst, model, rep)  # every item exactly once, content verified
    assert got["mail"] == sum(len(v) for v in model["mail"].values())
    t = j["tuning"]
    assert t["mode"] == "auto" and t["builders"] >= 1 and t["connections"] >= 1, t
    log = stack.job_log(jid)
    assert "Auto-tune: probe " in log and " CPUs, " in log and "GiB free, bench " in log, log


# ====================================================================== upgrade from 1.0.0
def key_100(f, i: int) -> str:
    """item_key exactly as mccb 1.0.0 derived it (app/engine/pst.py iter_items at tag v1.0.0)."""
    try:
        fnid = f._folder.identifier
    except Exception:  # noqa: BLE001
        fnid = 0
    try:
        return "%d-%d" % (fnid, f._folder.get_sub_message(i).identifier)
    except Exception:  # noqa: BLE001
        return "%d-x%d" % (fnid, i)


def folder_key_100(path: list[str]) -> str:
    """folder key exactly as mccb 1.0.0 derived it (app/server/runner.py folder_key at tag v1.0.0)."""
    return "/".join(p.replace("\\", "\\\\").replace("/", "\\/") for p in path)


def import_id_100(pst_sha: str, item_key: str) -> str:
    return f"<{pst_sha}-{item_key}>"


def test_keys_identical_to_1_0_0():
    from app.engine import PstFile
    from app.server.runner import folder_key, import_id_of
    for path in (["A/B"], ["a\\b", "c"], ["Boîte de réception", "Été"], []):
        assert folder_key(path) == folder_key_100(path)
    assert import_id_of("sha", "1-2") == import_id_100("sha", "1-2")
    n = 0
    for fx in ("enron.pst", "aspose-test-part0.pst"):  # the latter has an unreadable item (fallback key)
        with PstFile(str(FIXTURES / fx)) as p:
            for f in p.folders():
                for idx, key, _h in p.iter_items(f):
                    assert key == key_100(f, idx), (fx, f.path, idx)
                    n += 1
    assert n > 100


def _to_100_layout(db_path) -> None:
    """Rewrites the database in the 1.0.0 layout (tests/fixtures/schema-1.0.0.sql), keeping every row."""
    new = db_path.with_name("app.db.100")
    new.unlink(missing_ok=True)
    c = sqlite3.connect(new, isolation_level=None)
    c.executescript(SCHEMA_100.read_text())
    c.execute("ATTACH DATABASE ? AS cur", (str(db_path),))
    c.execute("BEGIN")
    for (table,) in c.execute("SELECT name FROM main.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                              ).fetchall():
        if table == "events":
            continue  # change feed only (the copy's triggers fill it)
        old_cols = [r[1] for r in c.execute(f"PRAGMA main.table_info({table})")]
        cur_cols = {r[1] for r in c.execute(f"PRAGMA cur.table_info({table})")}
        cols = ",".join(x for x in old_cols if x in cur_cols)
        if cols:
            c.execute(f"INSERT INTO main.{table}({cols}) SELECT {cols} FROM cur.{table}")
    c.execute("COMMIT")
    c.execute("DETACH DATABASE cur")
    c.close()
    for suffix in ("", "-wal", "-shm"):
        db_path.with_name("app.db" + suffix).unlink(missing_ok=True)
    new.rename(db_path)


def test_upgrade_from_1_0_0_mid_migration_resumes_without_duplicates(tmp_path_factory):
    from app.engine import PstFile
    from app.server.builders import norm_hash
    from app.server.imap import ImapConn, ImapParams, personal_prefix
    from app.server.runner import plan_folders

    base = tmp_path_factory.mktemp("upgrade")
    stack = Stack(base).start()
    try:
        srv_manual = stack.create_server(name="old-manual", workers=3, tuning="manual")
        srv_auto = stack.create_server(name="old-default", workers=4, tuning="manual")  # becomes auto
        user, acc, pst, model = _run_and_check(stack, srv_manual, FIXTURE, "manual")
        stack.stop_worker()  # the job is created but not run by the current version
        jid = stack.api("POST", "/api/runs", json={"account_ids": [acc["id"]]}).json()[0]["id"]
    finally:
        stack.close()
    sha = pst["pst_sha"]

    # --- what 1.0.0 had done before it was interrupted: some mails APPENDed and `ok`, some `inflight`
    #     (APPEND done or not), the rest never seen. Keys / folder keys / import ids as 1.0.0 derived them.
    params = ImapParams(IMAP_HOST, IMAP_PORT, "none", False, user, "pw")
    conn = ImapConn(params).connect()
    rows = []
    with PstFile(str(FIXTURE)) as p:
        folders = p.folders()
        mail = [f for f in folders if f.kind == "mail"]
        f = max(mail, key=lambda x: int(x.count or 0))
        assert int(f.count or 0) >= 8, [(x.path, x.count) for x in mail]
        delim = conn.delimiter()
        prefix = personal_prefix(conn)
        if prefix and not prefix.endswith(delim):
            prefix = ""
        plan = plan_folders(mail, conn.list(), delim, None, prefix)
        fk = folder_key_100(f.path)
        target = plan[fk][0]
        if target.upper() != "INBOX":
            parts = target.split(delim)
            for i in range(1, len(parts) + 1):
                try:
                    conn.create(delim.join(parts[:i]))
                except Exception:  # noqa: BLE001 - already exists
                    pass
        states = ["ok", "ok", "ok", "inflight+sent", "inflight+sent", "inflight", "inflight"]
        for i, st in enumerate(states):
            key = key_100(f, i)
            built = p.build_message(next(p.iter_items(f, i))[2], import_id_100(sha, key))
            assert built is not None
            if st in ("ok", "inflight+sent"):
                conn.append(target, built.raw, list(built.flags or []), built.internaldate)
            rows.append((key, st.split("+")[0], built))
    conn.close()

    db_path = stack.data / "app.db"
    c = sqlite3.connect(db_path, isolation_level=None)
    for key, st, built in rows:
        c.execute("INSERT INTO items(account_id, pst_id, pst_key, kind, item_key, folder, target, status, subject, "
                  "hash, size, job_id, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (acc["id"], pst["id"], sha, "mail", key, fk, target, st, built.subject, norm_hash(built.raw),
                   len(built.raw), jid, 1234.5))
    # the 1.0.0 worker died mid-job: still `running`, stale heartbeat, dead worker
    c.execute("UPDATE jobs SET status='running', phase='mail', worker_id='old-host:1:1', heartbeat=1, "
              "started_at=1 WHERE id=?", (jid,))
    c.close()
    _to_100_layout(db_path)
    c = sqlite3.connect(db_path)
    assert "tuning" not in {r[1] for r in c.execute("PRAGMA table_info(servers)")}
    assert c.execute("SELECT COUNT(*) FROM items").fetchone()[0] == len(rows)
    c.close()

    # --- upgrade: current version on the same data directory
    stack2 = Stack(base).start()
    try:
        servers = {s["name"]: s for s in stack2.api("GET", "/api/servers").json()}
        assert (servers["old-manual"]["tuning"], servers["old-manual"]["workers"]) == ("manual", 3)
        assert servers["old-default"]["tuning"] == "auto"
        j = stack2.wait_job(jid, timeout=300, cond=lambda j: j["finished_at"])
        final = "done_with_errors" if model["errors"] else "done"
        assert j["status"] == final, stack2.job_log(jid)
        assert j["tuning"] == {"mode": "manual", "builders": j["tuning"]["builders"], "connections": 3,
                               "fallback": False}, j["tuning"]
        rep = stack2.api("GET", f"/api/jobs/{jid}/report").json()
        got = verify_on_servers(stack2, user, pst, model, rep)  # no duplicate, every mail exactly once
        assert got["mail"] == sum(len(v) for v in model["mail"].values())
        log = stack2.job_log(jid)
        assert "4 'inflight' item(s)" in log, log
        c = sqlite3.connect(db_path)
        try:
            st = {k: (s, u, n) for k, s, u, n in c.execute(
                "SELECT item_key, status, updated_at, job_id FROM items WHERE account_id=? AND kind='mail'",
                (acc["id"],))}
            # ok items of 1.0.0: skipped (row untouched), inflight items: resolved, all ok now
            for key, s, _b in rows:
                assert st[key][0] == "ok", (key, st[key])
                if s == "ok":
                    assert st[key][1] == 1234.5, (key, st[key])
            # same item_key space as 1.0.0: no second row for the same mail
            n_rows = c.execute("SELECT COUNT(*) FROM items WHERE account_id=? AND kind='mail' AND folder=?",
                               (acc["id"], fk)).fetchone()[0]
            assert n_rows == int(f.count or 0)
        finally:
            c.close()
        v = ImapView(user)
        try:
            ids, _ = v.ids()
        finally:
            v.close()
        for key, _s, _b in rows:
            assert len(ids[import_id_100(sha, key)]) == 1
    finally:
        stack2.close()


@pytest.fixture(autouse=True)
def _settle():
    yield
    time.sleep(0.2)
