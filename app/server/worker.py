"""Worker: runs the job queue (DB) and PST validation. Run: python -m app.server.worker

Each job and each PST analysis runs in a subprocess (`python -m app.server.child`): a native
libpff crash only kills that subprocess; the worker (supervisor) applies recovery (see crash.py)."""
from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time

from pathlib import Path

from . import crash, db, intake, notify, planning, repo, tuner
from .config import settings

log = logging.getLogger("worker")
HEARTBEAT_FILE = "worker.heartbeat"
ROOT = Path(__file__).resolve().parents[2]  # directory containing the `app` package
TERMINAL = ("done", "done_with_errors", "failed", "stopped")


class Worker:
    def __init__(self, concurrency: int | None = None):
        self.host = socket.gethostname()
        self.id = f"{self.host}:{os.getpid()}:{int(time.time())}"
        # auto (WORKER_CONCURRENCY unset or 0): recomputed on every tick from the machine probe and the queue;
        # the cores are then split between the running jobs by each job (tuner.choose_builders)
        self.auto_concurrency = not (concurrency or settings.worker_concurrency)
        self.concurrency = concurrency or settings.worker_concurrency or 1
        self._probe: tuner.MachineProbe | None = None
        self._probe_t = 0.0
        self._disk_busy = False
        self._disk_prev: tuple[float, int] | None = None
        self._extra = 0                                 # jobs above the static choice, while the machine stays idle
        self._extra_t = 0.0
        self._extra_cpu: tuple[float, float] | None = None
        self.threads: dict[int, threading.Thread] = {}
        self.stop_event = threading.Event()
        self.validating: dict[int, threading.Thread] = {}
        self.procs: dict[tuple[str, int], subprocess.Popen] = {}
        self.lock = threading.Lock()
        self.intake = intake.Intake(self.stop_event)   # drop folder, remote fetch, auto chain
        self.notifier = notify.Notifier()               # polls the DB; delivery on its own thread
        self.planner = planning.Jobs()                  # dry runs and comparisons asked through the API

    # -------------------------------------------------- crash recovery
    def recover(self, startup: bool = False) -> None:
        now = time.time()
        stale = now - settings.heartbeat_stale
        rows = db.q("SELECT id, status, worker_id, heartbeat FROM jobs WHERE status IN ('running','stopping')")
        for r in rows:
            wid = r["worker_id"] or ""
            if wid == self.id:
                continue
            same_host_dead = startup and wid.split(":")[0] == self.host
            if same_host_dead or not wid or (r["heartbeat"] or 0) < stale:
                if r["status"] == "stopping":
                    db.ex("UPDATE jobs SET status='stopped', worker_id=NULL, finished_at=? WHERE id=? AND status='stopping'",
                          (now, r["id"]))
                    repo.log(r["id"], "Stop finalized after worker interruption", "warning")
                else:
                    db.ex("UPDATE jobs SET status='queued', worker_id=NULL WHERE id=? AND status='running'", (r["id"],))
                    repo.log(r["id"], "Orphan job (worker interrupted): re-queued, resuming without duplicates", "warning")
        # paused jobs still attached to a dead worker
        db.ex("UPDATE jobs SET worker_id=NULL WHERE status='paused' AND worker_id IS NOT NULL AND COALESCE(heartbeat,0) < ?",
              (stale,))

    # -------------------------------------------------- job pickup
    def claim(self) -> int | None:
        now = time.time()
        with db.tx() as c:
            r = c.execute(
                "SELECT j.id FROM jobs j WHERE j.status='queued' AND NOT EXISTS ("
                " SELECT 1 FROM jobs k WHERE k.account_id=j.account_id AND k.id<>j.id AND k.status IN ('running','stopping'))"
                " ORDER BY j.priority DESC, j.id LIMIT 1").fetchone()
            if not r:
                return None
            c.execute("UPDATE jobs SET status='running', worker_id=?, heartbeat=?, started_at=COALESCE(started_at, ?), "
                      "finished_at=NULL, error=NULL WHERE id=? AND status='queued'", (self.id, now, now, r["id"]))
            return r["id"]

    # -------------------------------------------------- isolated subprocesses
    def _spawn(self, args: list[str]) -> subprocess.Popen:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        env[crash.PARENT_PID_ENV] = str(os.getpid())
        return subprocess.Popen([sys.executable, "-m", "app.server.child", *args], cwd=str(ROOT), env=env)

    def _supervise(self, key: tuple[str, int], args: list[str]) -> int | None:
        """Launches the subprocess and waits for its end. None = worker shutdown (not a crash)."""
        try:
            p = self._spawn(args)
        except OSError as e:
            log.error("failed to launch subprocess %s: %s", key, e)
            raise
        with self.lock:
            self.procs[key] = p
        try:
            rc = p.wait()
        finally:
            with self.lock:
                self.procs.pop(key, None)
        if self.stop_event.is_set() and rc != 0:
            return None  # terminated by worker shutdown: the job is re-queued by run_forever
        return rc

    def _run_job(self, job_id: int) -> None:
        try:
            rc = self._supervise(("job", job_id), ["job", str(job_id), self.id])
            if rc is None:
                return
            r = db.q1("SELECT status, worker_id FROM jobs WHERE id=?", (job_id,))
            if rc == 0 and not (r and r["status"] == "running" and r["worker_id"] == self.id):
                if r and r["status"] in TERMINAL:
                    crash.reset_crashes(job_id)
                return
            st = crash.handle_job_crash(job_id, rc)
            log.warning("job %s: subprocess died (%s) -> %s", job_id, crash.describe_rc(rc), st)
        except Exception as e:  # never kill the worker
            log.exception("job %s", job_id)
            db.ex("UPDATE jobs SET status='failed', error=?, worker_id=NULL, finished_at=? WHERE id=?",
                  (f"{type(e).__name__}: {e}", time.time(), job_id))

    def _validate(self, pid: int) -> None:
        try:
            rc = self._supervise(("validate", pid), ["validate", str(pid)])
            if rc:
                log.warning("validation %s: subprocess died (%s)", pid, crash.describe_rc(rc))
                crash.handle_validate_crash(pid, rc)
        except Exception as e:
            log.exception("validation %s", pid)
            db.ex("UPDATE psts SET status='invalid', error=? WHERE id=? AND status='validating'", (str(e), pid))

    def terminate_children(self) -> None:
        """SIGTERM to subprocesses, SIGKILL after CHILD_TERM_GRACE seconds."""
        with self.lock:
            procs = list(self.procs.values())
        for p in procs:
            try:
                p.terminate()
            except ProcessLookupError:
                pass
        deadline = time.time() + settings.child_term_grace
        for p in procs:
            try:
                p.wait(max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                log.warning("subprocess %s: SIGKILL", p.pid)
                p.kill()
                p.wait()
        for t in list(self.threads.values()) + list(self.validating.values()):
            t.join(5)

    def _server_pressure(self, now: float) -> bool:
        """A target server reported a limit or an error recently (see JobRunner._server_share)."""
        for r in db.q("SELECT value FROM kv WHERE key LIKE 'server:%:pressure'"):
            try:
                if now - float(r["value"]) < 60:
                    return True
            except ValueError:
                pass
        return False

    def _check_disk(self, now: float) -> None:
        """Is the disk the limit? More mailboxes would then only add seeks. PSI when the kernel has it (tasks waiting
        for I/O more than half of the time), else the busy time of the device holding the PSTs (> 95 %)."""
        busy = False
        pressure = tuner.io_pressure()
        if pressure is not None:
            busy = pressure > 50
        else:
            ticks = tuner.disk_io_ticks(str(settings.pst_dir))
            prev, self._disk_prev = self._disk_prev, (now, ticks) if ticks is not None else None
            if prev is not None and ticks is not None and now > prev[0]:
                busy = (ticks - prev[1]) / 1000 / (now - prev[0]) > 0.95
        if busy != self._disk_busy:
            self._disk_busy = busy
            log.info("concurrency (auto): disk %s", "saturated, no new job for now" if busy else "no longer saturated")

    def _tune_extra(self, now: float, base: int, running: int, queued: int) -> None:
        """Jobs often wait for their target, not for the CPU: the static choice then leaves the machine idle while
        mailboxes queue. Every 30 s: one job more if 2 cores stayed idle, memory and disk are fine and no target
        complained; one less (for the next admissions, nothing is stopped) if no core is idle any more."""
        if now - self._extra_t < 30:
            return
        self._extra_t = now
        cur = tuner.proc_stat()
        prev, self._extra_cpu = self._extra_cpu, cur
        if cur is None or prev is None or cur[1] <= prev[1] or self._probe is None:
            return
        idle = self._probe.cpus * (cur[0] - prev[0]) / (cur[1] - prev[1])
        if idle < 0.5:
            if self._extra:
                self._extra -= 1
                log.info("concurrency (auto): no idle core, %d job(s) above the static choice", self._extra)
            return
        if (idle >= 2.0 and queued > 0 and running >= base + self._extra
                and self._probe.mem_available_bytes >= tuner.JOB_ADMIT_MEM_BYTES
                and not self._disk_busy and not self._server_pressure(now)):
            self._extra += 1
            log.info("concurrency (auto): %.1f idle cores with jobs waiting, %d job(s) above the static choice",
                     idle, self._extra)

    def auto_limit(self) -> int:
        """Jobs to run in parallel in auto mode (no benchmark: cores, available memory, queue; refreshed every 10 s),
        plus the jobs that the idle cores allow (_tune_extra), none more while disk or memory are the limit."""
        now = time.time()
        if self._probe is None or now - self._probe_t > 10:
            self._probe, self._probe_t = tuner.probe_machine(), now
            self._check_disk(now)
        queued = db.q1("SELECT COUNT(*) n FROM jobs WHERE status='queued'")["n"]
        running = len(self.threads)
        free = self._probe.mem_available_bytes
        # memory already held by the running jobs is not "available" any more: counted back in
        mem = free + running * 512 * 1024 * 1024 if free else None
        base = tuner.choose_worker_concurrency(self._probe.cpus, pending=queued + running, mem_available_bytes=mem)
        self._tune_extra(now, base, running, queued)
        limit = min(base + self._extra, tuner.MAX_JOBS, max(1, queued + running))
        if running and (self._disk_busy or (free and free < tuner.JOB_ADMIT_MEM_BYTES)):
            limit = min(limit, running)  # nothing new starts until it comes back; nothing running is stopped
        return max(1, limit)

    def _validate_limit(self) -> int:
        """PST analyses in parallel: a folder walk per file, light for the CPU, read-only."""
        return max(2, min(8, (self._probe.cpus if self._probe else os.cpu_count() or 2) // 2))

    def tick(self) -> None:
        for jid, t in list(self.threads.items()):
            if not t.is_alive():
                del self.threads[jid]
        for pid, t in list(self.validating.items()):
            if not t.is_alive():
                del self.validating[pid]
        # pending PST validations
        for r in db.q("SELECT id FROM psts WHERE status='validating'"):
            if r["id"] not in self.validating and len(self.validating) < self._validate_limit():
                t = threading.Thread(target=self._validate, args=(r["id"],), daemon=True, name=f"validate-{r['id']}")
                self.validating[r["id"]] = t
                t.start()
        # server-side intake and notifications: isolated, they must never break the job loop
        for part in (self.intake, self.notifier):
            try:
                part.tick()
            except Exception:
                log.exception("%s", type(part).__name__)
        # dry runs, comparisons and the allowed hours of the servers: isolated like the parts above
        for part in (self.planner.tick, planning.window_tick):
            try:
                part()
            except Exception:
                log.exception("planning")
        # jobs
        if self.auto_concurrency:
            n = self.auto_limit()
            if n != self.concurrency:
                log.info("concurrency (auto): %d job(s) in parallel", n)
                self.concurrency = n
        while len(self.threads) < self.concurrency and not self.stop_event.is_set():
            jid = self.claim()
            if jid is None:
                break
            t = threading.Thread(target=self._run_job, args=(jid,), daemon=True, name=f"job-{jid}")
            self.threads[jid] = t
            t.start()
        # job heartbeat (including during a long network operation)
        if self.threads:
            ids = list(self.threads)
            db.ex(f"UPDATE jobs SET heartbeat=? WHERE worker_id=? AND id IN ({','.join('?' * len(ids))})",
                  [time.time(), self.id, *ids])
        db.kv_set("worker_heartbeat", str(time.time()))
        try:
            (settings.data_dir / HEARTBEAT_FILE).write_text(str(time.time()))
        except OSError:
            pass

    def run_forever(self, poll: float = 1.0) -> None:
        settings.ensure_dirs()
        db.conn()
        self.recover(startup=True)
        self.planner.recover()
        last_recover = time.time()
        log.info("worker %s started (concurrency %s)", self.id,
                 f"auto, {self.auto_limit()} now" if self.auto_concurrency else self.concurrency)
        while not self.stop_event.is_set():
            try:
                self.tick()
                if time.time() - last_recover > 15:
                    self.recover()
                    last_recover = time.time()
            except Exception:
                log.exception("worker loop")
            self.stop_event.wait(poll)
        # clean shutdown: subprocesses terminated, then running jobs re-queued (idempotent resume)
        self.terminate_children()
        self.intake.join(5)
        for jid in list(self.threads):
            cur = db.ex("UPDATE jobs SET status='queued', worker_id=NULL WHERE id=? AND status='running' AND worker_id=?",
                        (jid, self.id))
            if cur.rowcount:  # not for a job that had just finished
                repo.log(jid, "Worker stopped: job re-queued", "warning")


def healthcheck() -> int:
    try:
        ts = float((settings.data_dir / HEARTBEAT_FILE).read_text())
    except (OSError, ValueError):
        return 1
    return 0 if time.time() - ts < 60 else 1


def main() -> None:
    if "--healthcheck" in sys.argv:
        sys.exit(healthcheck())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("worker").info("mccb v%s starting (worker)", __import__("app.server.version", fromlist=["get_version"]).get_version())
    w = Worker()

    def _stop(*_):
        w.stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    w.run_forever()


if __name__ == "__main__":
    main()
