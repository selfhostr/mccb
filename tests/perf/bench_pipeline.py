"""End-to-end benchmark: real web, worker, engine and GreenMail. Writes JSON: items/s and MB/s per phase, peak RSS.

    # in the test image, with the services of docker-compose.test.yml (see docs/tuning.md for the full commands)
    python tests/perf/bench_pipeline.py --fixtures enron.pst --mailboxes 1,4 --output /tmp/bench.json
    python tests/perf/bench_pipeline.py --db-rows 1000000 --output /tmp/bench-db.json   # costs on a huge items table

For each number of mailboxes the file is uploaded once (upload MB/s, analysis seconds), then one account per mailbox is
migrated at once. Phases are timed by polling the API every 50 ms and read from the job logs; "resume" is a second
full run with everything already imported. PST sizes are small (tests/fixtures): this measures the pipeline with its
own costs in front, not a target server under load, and an auto-tuned job of a few seconds hardly ramps up.
Works unchanged on an older checkout: only the public API and the database file are used. Not collected by pytest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MB = 1_000_000


# ------------------------------------------------------------------ memory
def _tree_rss(root_pids: list[int]) -> tuple[int, int]:
    """(RSS of the process trees, largest single process), bytes. Read from /proc: children of the roots included."""
    parents: dict[int, int] = {}
    rss: dict[int, int] = {}
    page = os.sysconf("SC_PAGE_SIZE")
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat", encoding="ascii", errors="replace") as fh:
                st = fh.read()
        except OSError:
            continue
        fields = st[st.rindex(")") + 2:].split()
        parents[int(d)] = int(fields[1])
        rss[int(d)] = int(fields[21]) * page
    total = largest = 0
    for pid, value in rss.items():
        p = pid
        while p and p not in root_pids:
            p = parents.get(p, 0)
        if p:
            total += value
            largest = max(largest, value)
    return total, largest


class RssSampler(threading.Thread):
    def __init__(self, pids: list[int]):
        super().__init__(daemon=True)
        self.pids, self.peak, self.peak_single, self._stop_ev = pids, 0, 0, threading.Event()

    def run(self) -> None:
        while not self._stop_ev.wait(0.25):
            total, largest = _tree_rss(self.pids)
            self.peak, self.peak_single = max(self.peak, total), max(self.peak_single, largest)

    def stop(self) -> dict:
        self._stop_ev.set()
        self.join(2)
        return {"peak_rss_mb": round(self.peak / MB, 1), "peak_single_process_rss_mb": round(self.peak_single / MB, 1)}


# ------------------------------------------------------------------ one run of N jobs
def _per_second(n: float, seconds: float) -> float | None:
    return round(n / seconds, 2) if seconds > 0 else None


def _item_sizes(db_path: Path, account_ids: list[int], kind: str) -> tuple[int, int]:
    c = sqlite3.connect(db_path, timeout=60)
    try:
        marks = ",".join("?" * len(account_ids))
        n, total = c.execute(f"SELECT COUNT(*), COALESCE(SUM(size), 0) FROM items WHERE kind=? AND status='ok' "
                             f"AND account_id IN ({marks})", (kind, *account_ids)).fetchone()
        return n, total
    finally:
        c.close()


def _run_jobs(stack, jobs: list[int], timeout: float, account_ids: list[int]) -> dict:
    """Waits for the jobs, polling their phase: {phase: wall seconds while at least one job was in it}."""
    from tests.e2e.helpers import TERMINAL
    t0 = time.perf_counter()
    last = t0
    spent: dict[str, float] = {}
    n_done_before = {k: _item_sizes(stack.data / "app.db", account_ids, k) for k in ("mail", "event", "contact")}
    finals = {}
    while len(finals) < len(jobs):
        assert time.perf_counter() - t0 < timeout, f"jobs stuck: {finals}\n{stack.tail('worker')}"
        now = time.perf_counter()
        phases = set()
        for jid in jobs:
            if jid in finals:
                continue
            j = stack.client.get(f"/api/jobs/{jid}").json()
            if j["status"] in TERMINAL:
                finals[jid] = j
            elif j["status"] == "queued":
                phases.add("queued")
            else:
                phases.add(j["phase"])
        for p in phases:
            spent[p] = spent.get(p, 0.0) + now - last
        last = now
        time.sleep(0.05)
    wall = time.perf_counter() - t0
    out = {"wall_seconds": round(wall, 3), "statuses": sorted({j["status"] for j in finals.values()}),
           "phase_wall_seconds": {k: round(v, 3) for k, v in spent.items()}, "phases": {}}
    for phase, kind in (("mail", "mail"), ("calendar", "event"), ("contacts", "contact")):
        n, size = _item_sizes(stack.data / "app.db", account_ids, kind)
        n0, s0 = n_done_before[kind]
        sec = spent.get(phase, 0.0)
        out["phases"][phase] = {"items": n - n0, "mb": round((size - s0) / MB, 2), "seconds": round(sec, 3),
                                "items_per_s": _per_second(n - n0, sec), "mb_per_s": _per_second((size - s0) / MB, sec)}
    indexed = [(int(a.replace(",", "")), float(b)) for jid in jobs for a, b in
               re.findall(r"Indexed ([\d,]+) item\(s\) in ([\d.]+) s", _full_log(stack, jid))]
    if indexed:
        out["indexing"] = {"items": sum(a for a, _ in indexed), "seconds_sum": round(sum(b for _, b in indexed), 3),
                           "items_per_s": _per_second(sum(a for a, _ in indexed), sum(b for _, b in indexed))}
    out["tuning_lines"] = sum("Auto-tune:" in _full_log(stack, jid) for jid in jobs)
    out["last_tuning"] = [finals[jid].get("tuning") for jid in jobs][:4]
    return out


def _full_log(stack, jid: int) -> str:
    lines, after = [], 0
    while True:
        r = stack.client.get(f"/api/jobs/{jid}/log", params={"after": after, "limit": 10000}).json()
        if not r["lines"]:
            return "\n".join(lines)
        lines += [x["msg"] for x in r["lines"]]
        after = r["next"]


def _upload(stack, path: Path) -> tuple[dict, dict]:
    """One PUT after the other, as fast as this client goes (server cost of the chunks, no browser hashing)."""
    data = path.read_bytes()
    from tests.e2e.helpers import CHUNK
    shas = [hashlib.sha256(data[i * CHUNK:(i + 1) * CHUNK]).hexdigest() for i in range(-(-len(data) // CHUNK))]
    up = stack.api("POST", "/api/uploads", json={"filename": path.name, "size": len(data), "last_modified": 1}).json()
    t0 = time.perf_counter()
    for i, sha in enumerate(shas):
        stack.api("PUT", f"/api/uploads/{up['id']}/chunks/{i}", content=data[i * CHUNK:(i + 1) * CHUNK],
                  headers={"X-Chunk-Sha256": sha})
    upload_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    stack.api("POST", f"/api/uploads/{up['id']}/complete")
    while True:
        p = stack.api("GET", f"/api/psts/{up['id']}").json()
        if p["status"] in ("valid", "invalid"):
            break
        assert time.perf_counter() - t0 < 300, p
        time.sleep(0.02)
    assert p["status"] == "valid", p
    return p, {"mb": round(len(data) / MB, 2), "upload_seconds": round(upload_s, 3),
               "upload_mb_per_s": _per_second(len(data) / MB, upload_s),
               "analysis_seconds": round(time.perf_counter() - t0, 3)}


def bench_stack(fixture: str, mailboxes: int, env: dict, timeout: float) -> dict:
    from tests.e2e.helpers import FIXTURES, Stack, create_user
    with tempfile.TemporaryDirectory(prefix="mccb-bench-") as base:
        stack = Stack(Path(base), **env).start()
        rss = RssSampler([stack.web.pid, stack.worker.pid])
        rss.start()
        try:
            srv = stack.create_server(name="bench", tuning="auto")
            pst, up = _upload(stack, FIXTURES / fixture)
            accounts = []
            for _ in range(mailboxes):
                a = stack.create_account(srv["id"], create_user(prefix="bench"), "pw")
                stack.api("PUT", f"/api/accounts/{a['id']}/pst", json={"pst_id": pst["id"]})
                accounts.append(a["id"])
            jobs = [j["id"] for j in stack.api("POST", "/api/runs", json={"account_ids": accounts}).json()]
            first = _run_jobs(stack, jobs, timeout, accounts)
            for jid in jobs:
                stack.api("POST", f"/api/jobs/{jid}/retry")
            resume = _run_jobs(stack, jobs, timeout, accounts)
            events = sqlite3.connect(stack.data / "app.db").execute("SELECT COUNT(*) FROM events").fetchone()[0]
            logs = sqlite3.connect(stack.data / "app.db").execute("SELECT COUNT(*) FROM job_logs").fetchone()[0]
            return {"fixture": fixture, "mailboxes": mailboxes, "upload": up, "run": first, "resume": resume,
                    "events_rows": events, "log_rows": logs, **rss.stop()}
        finally:
            rss.stop()
            stack.close()


# ------------------------------------------------------------------ huge items table, without any server
def bench_db(rows: int, env: dict) -> dict:
    """What a job pays per second on a mailbox of `rows` items: progress recount, average size, item writes."""
    for k, v in env.items():
        os.environ[k] = v
    with tempfile.TemporaryDirectory(prefix="mccb-bench-db-") as base:
        os.environ["DATA_DIR"] = base
        from app.server import db
        from app.server.config import settings
        settings.reload()
        db.reset()
        c = db.conn()
        now = time.time()
        c.execute("INSERT INTO servers(id, name, imap_host, created_at) VALUES (1, 's', 'h', ?)", (now,))
        c.execute("INSERT INTO accounts(id, server_id, email, created_at) VALUES (1, 1, 'a@b', ?)", (now,))
        c.execute("BEGIN")
        c.executemany(
            "INSERT INTO items(account_id, pst_id, pst_key, kind, item_key, folder, target, status, subject, hash, size, "
            "job_id, updated_at) VALUES (1, 1, 'abcdef0123456789', 'mail', ?, ?, ?, 'ok', 'a subject of usual length', "
            "'0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef', 50000, 1, 1.0)",
            ((f"{i % 1000}-{i}", f"Folder {i % 1000}", f"Folder.{i % 1000}") for i in range(rows)))
        c.execute("COMMIT")

        def timed(sql: str, params: tuple = ()) -> float:
            t0 = time.perf_counter()
            c.execute(sql, params).fetchall()
            return round(time.perf_counter() - t0, 4)

        key = (1, "abcdef0123456789")
        recount = [timed("SELECT status, COUNT(*) n FROM items WHERE account_id=? AND pst_key=? AND kind=? GROUP BY status",
                         (*key, "mail")) for _ in range(3)]
        avg = [timed("SELECT COUNT(size) n, SUM(size) s FROM items WHERE account_id=? AND pst_key=? AND kind='mail' "
                     "AND status='ok'", key) for _ in range(3)]
        per_target = timed("SELECT COUNT(*) n FROM items WHERE account_id=? AND pst_key=? AND kind='mail' AND target=? "
                           "AND status='ok'", (*key, "Folder.1"))
        group = timed("SELECT target, MIN(folder) folder, COUNT(*) n FROM items WHERE account_id=? AND pst_key=? "
                      "AND kind='mail' AND status='ok' GROUP BY target", key)
        one_folder = timed("SELECT id, item_key, status, target, hash, error FROM items WHERE account_id=? AND pst_key=? "
                           "AND kind='mail' AND folder IN (?, ?)", (*key, "Folder 1", "Folder 1"))
        ids = list(range(1, min(rows, 20000) + 1))
        t0 = time.perf_counter()
        for i in ids:
            c.execute("UPDATE items SET status='ok', error=NULL, job_id=1, updated_at=? WHERE id=?", (now, i))
        single = time.perf_counter() - t0
        t0 = time.perf_counter()
        for j in range(0, len(ids), 64):
            c.execute("BEGIN IMMEDIATE")
            c.executemany("UPDATE items SET status='ok', error=NULL, job_id=1, updated_at=? WHERE id=?",
                          [(now, i) for i in ids[j:j + 64]])
            c.execute("COMMIT")
        batched = time.perf_counter() - t0
        db.reset()
        size = os.path.getsize(os.path.join(base, "app.db"))
        return {"rows": rows, "db_mb": round(size / MB, 1), "recount_seconds": statistics.median(recount),
                "mail_avg_query_seconds": statistics.median(avg), "count_of_one_target_seconds": per_target,
                "count_of_all_targets_seconds": group, "rows_of_one_folder_seconds": one_folder,
                "updates_per_s_autocommit": int(len(ids) / single), "updates_per_s_batch_64": int(len(ids) / batched)}


# ------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixtures", default="enron.pst", help="comma-separated files of tests/fixtures")
    ap.add_argument("--mailboxes", default="1,4", help="comma-separated numbers of mailboxes migrated at once")
    ap.add_argument("--repeat", type=int, default=1, help="runs of each case")
    ap.add_argument("--timeout", type=float, default=900, help="seconds allowed for one run")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="for web and worker, repeatable")
    ap.add_argument("--db-rows", type=int, default=0, help="also measure the database costs on a table of this size")
    ap.add_argument("--no-stack", action="store_true", help="database costs only (no GreenMail needed)")
    ap.add_argument("--output", required=True)
    a = ap.parse_args()
    env = {"WORKER_CONCURRENCY": "0"}  # helpers.Stack pins 2: the auto mode is what is measured
    env.update(x.split("=", 1) for x in a.env)
    out: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "cpus": os.cpu_count(), "python": platform.python_version(),
                 "env": env, "runs": []}
    if a.db_rows:
        out["db"] = bench_db(a.db_rows, {k: v for k, v in env.items() if k != "WORKER_CONCURRENCY"})
        print(json.dumps(out["db"], indent=2))
    if not a.no_stack:
        for fixture in a.fixtures.split(","):
            for n in (int(x) for x in a.mailboxes.split(",")):
                for _ in range(a.repeat):
                    r = bench_stack(fixture.strip(), n, env, a.timeout)
                    out["runs"].append(r)
                    print(f"{fixture} x{n}: mail {r['run']['phases']['mail']['items_per_s']} items/s, "
                          f"{r['run']['phases']['mail']['mb_per_s']} MB/s, wall {r['run']['wall_seconds']} s, "
                          f"resume {r['resume']['wall_seconds']} s, peak RSS {r['peak_rss_mb']} MB")
    Path(a.output).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"written: {a.output}")


if __name__ == "__main__":
    main()
