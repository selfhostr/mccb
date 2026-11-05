"""Worker : exécute la file de jobs (DB) et la validation des PST. Lancer : python -m app.server.worker

Chaque job et chaque analyse de PST tourne dans un sous-processus (`python -m app.server.child`) : un crash
natif de libpff ne tue que ce sous-processus ; le worker (superviseur) applique la reprise (voir crash.py)."""
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

from . import crash, db, repo
from .config import settings

log = logging.getLogger("worker")
HEARTBEAT_FILE = "worker.heartbeat"
ROOT = Path(__file__).resolve().parents[2]  # dossier contenant le paquet `app`
TERMINAL = ("done", "done_with_errors", "failed", "stopped")


class Worker:
    def __init__(self, concurrency: int | None = None):
        self.host = socket.gethostname()
        self.id = f"{self.host}:{os.getpid()}:{int(time.time())}"
        self.concurrency = concurrency or settings.worker_concurrency
        self.threads: dict[int, threading.Thread] = {}
        self.stop_event = threading.Event()
        self.validating: dict[int, threading.Thread] = {}
        self.procs: dict[tuple[str, int], subprocess.Popen] = {}
        self.lock = threading.Lock()

    # -------------------------------------------------- reprise après crash
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
                    repo.log(r["id"], "Arrêt finalisé après interruption du worker", "warning")
                else:
                    db.ex("UPDATE jobs SET status='queued', worker_id=NULL WHERE id=? AND status='running'", (r["id"],))
                    repo.log(r["id"], "Job orphelin (worker interrompu) : remis en file, reprise sans doublon", "warning")
        # paused jobs encore attachés à un worker mort
        db.ex("UPDATE jobs SET worker_id=NULL WHERE status='paused' AND worker_id IS NOT NULL AND COALESCE(heartbeat,0) < ?",
              (stale,))

    # -------------------------------------------------- prise de job
    def claim(self) -> int | None:
        now = time.time()
        with db.tx() as c:
            r = c.execute(
                "SELECT j.id FROM jobs j WHERE j.status='queued' AND NOT EXISTS ("
                " SELECT 1 FROM jobs k WHERE k.account_id=j.account_id AND k.id<>j.id AND k.status IN ('running','stopping'))"
                " ORDER BY j.id LIMIT 1").fetchone()
            if not r:
                return None
            c.execute("UPDATE jobs SET status='running', worker_id=?, heartbeat=?, started_at=COALESCE(started_at, ?), "
                      "finished_at=NULL, error=NULL WHERE id=? AND status='queued'", (self.id, now, now, r["id"]))
            return r["id"]

    # -------------------------------------------------- sous-processus isolés
    def _spawn(self, args: list[str]) -> subprocess.Popen:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        env[crash.PARENT_PID_ENV] = str(os.getpid())
        return subprocess.Popen([sys.executable, "-m", "app.server.child", *args], cwd=str(ROOT), env=env)

    def _supervise(self, key: tuple[str, int], args: list[str]) -> int | None:
        """Lance le sous-processus et attend sa fin. None = arrêt du worker (pas un crash)."""
        try:
            p = self._spawn(args)
        except OSError as e:
            log.error("lancement du sous-processus %s impossible : %s", key, e)
            raise
        with self.lock:
            self.procs[key] = p
        try:
            rc = p.wait()
        finally:
            with self.lock:
                self.procs.pop(key, None)
        if self.stop_event.is_set() and rc != 0:
            return None  # terminé par l'arrêt du worker : le job est remis en file par run_forever
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
            log.warning("job %s : sous-processus mort (%s) → %s", job_id, crash.describe_rc(rc), st)
        except Exception as e:  # ne jamais tuer le worker
            log.exception("job %s", job_id)
            db.ex("UPDATE jobs SET status='failed', error=?, worker_id=NULL, finished_at=? WHERE id=?",
                  (f"{type(e).__name__}: {e}", time.time(), job_id))

    def _validate(self, pid: int) -> None:
        try:
            rc = self._supervise(("validate", pid), ["validate", str(pid)])
            if rc:
                log.warning("validation %s : sous-processus mort (%s)", pid, crash.describe_rc(rc))
                crash.handle_validate_crash(pid, rc)
        except Exception as e:
            log.exception("validation %s", pid)
            db.ex("UPDATE psts SET status='invalid', error=? WHERE id=? AND status='validating'", (str(e), pid))

    def terminate_children(self) -> None:
        """SIGTERM aux sous-processus, SIGKILL après CHILD_TERM_GRACE secondes."""
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
                log.warning("sous-processus %s : SIGKILL", p.pid)
                p.kill()
                p.wait()
        for t in list(self.threads.values()) + list(self.validating.values()):
            t.join(5)

    def tick(self) -> None:
        for jid, t in list(self.threads.items()):
            if not t.is_alive():
                del self.threads[jid]
        for pid, t in list(self.validating.items()):
            if not t.is_alive():
                del self.validating[pid]
        # validations PST en attente
        for r in db.q("SELECT id FROM psts WHERE status='validating'"):
            if r["id"] not in self.validating and len(self.validating) < 2:
                t = threading.Thread(target=self._validate, args=(r["id"],), daemon=True, name=f"validate-{r['id']}")
                self.validating[r["id"]] = t
                t.start()
        # jobs
        while len(self.threads) < self.concurrency and not self.stop_event.is_set():
            jid = self.claim()
            if jid is None:
                break
            t = threading.Thread(target=self._run_job, args=(jid,), daemon=True, name=f"job-{jid}")
            self.threads[jid] = t
            t.start()
        # heartbeat des jobs (y compris pendant une longue opération réseau)
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
        last_recover = time.time()
        log.info("worker %s démarré (concurrence %d)", self.id, self.concurrency)
        while not self.stop_event.is_set():
            try:
                self.tick()
                if time.time() - last_recover > 15:
                    self.recover()
                    last_recover = time.time()
            except Exception:
                log.exception("boucle worker")
            self.stop_event.wait(poll)
        # arrêt propre : sous-processus terminés, puis jobs en cours remis en file (reprise idempotente)
        self.terminate_children()
        for jid in list(self.threads):
            cur = db.ex("UPDATE jobs SET status='queued', worker_id=NULL WHERE id=? AND status='running' AND worker_id=?",
                        (jid, self.id))
            if cur.rowcount:  # pas pour un job qui venait de se terminer
                repo.log(jid, "Worker arrêté : job remis en file", "warning")


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
    w = Worker()

    def _stop(*_):
        w.stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    w.run_forever()


if __name__ == "__main__":
    main()
