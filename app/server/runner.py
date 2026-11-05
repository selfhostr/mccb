"""Job execution (= one mailbox): precheck -> mail -> calendar -> contacts -> verify.

Idempotence: `items` table (account_id, pst_key = PST fingerprint, kind, item_key); a mail is marked
`inflight` (committed) BEFORE its APPEND, then `ok`/`error`. On resume, `inflight` items (and `error` items already tried)
are checked by `UID SEARCH HEADER X-PST-Import-Id` + exact re-read of the header (`<sha-key>`).
DAV: deterministic UIDs -> idempotent PUT.
"""
from __future__ import annotations

import hashlib
import json
import queue
import random
import re
import threading
import time
from dataclasses import dataclass, field

from . import crash, db, repo
from .config import settings
from .dav import DavClient, DavError
from .engine_loader import engine
from .imap import (SPECIAL_DEFAULT, SPECIAL_FLAGS, ImapConn, ImapError, ImapParams, find_special,
                   personal_prefix, utf7_decode, utf7_encode, with_retry, wrap_exc)
from .security import decrypt

IMPORT_HEADER = "X-PST-Import-Id"
KIND_OF_PHASE = {"mail": "mail", "calendar": "event", "contacts": "contact"}
PROGRESS_KEY = {"mail": "mail", "event": "calendar", "contact": "contacts"}
# engine fallback key for an item unreadable during iteration ("<fnid>-x<index>", see pst.iter_items)
FALLBACK_KEY_RE = re.compile(r"-x(\d+)$")


class PauseRequested(Exception):
    pass


class StopRequested(Exception):
    pass


class JobFailed(Exception):
    pass


def norm_hash(raw: bytes) -> str:
    """Line-ending-insensitive hash (the server stores CRLF)."""
    data = raw.replace(b"\r\n", b"\n").rstrip(b"\n")
    return hashlib.sha256(data).hexdigest()


def import_id_of(pst_sha: str, item_key: str) -> str:
    """X-PST-Import-Id value: delimited by <> so that no identifier is a prefix of another."""
    return f"<{pst_sha}-{item_key}>"


def folder_key(path: list[str]) -> str:
    """Unambiguous text key of a folder: "/" and "\\" in names are escaped ("A/B" != A -> B).
    Identical to the old key ("/".join) for names without "/" or "\\"."""
    return "/".join(p.replace("\\", "\\\\").replace("/", "\\/") for p in path)


def legacy_folder_key(path: list[str]) -> str:
    """Old (ambiguous) key, still present in the items table of earlier imports."""
    return "/".join(path)


def _clean_part(name: str, delim: str) -> str:
    name = (name or "").strip().replace(delim, "_")
    return name or "_"


def _needs_clean(name: str, delim: str) -> bool:
    return not name or name != name.strip() or delim in name


def plan_folders(mail_folders, boxes, delim: str, root: str | None, prefix: str = "") -> dict[str, tuple[str, str | None]]:
    """Computes the target IMAP name (modified UTF-7) for each mail folder of the PST.
    Returns {folder_key: (imap_name, special_flag_if_to_create_or_None)}.
    Target names are unique (case-insensitive comparison): two folders that cleaning would
    merge ("A.B" and "A_B" with the "." separator) are told apart by a suffix " (2)", " (3)"...
    The assignment is deterministic (depth, unmodified name first, key): a resume yields the same plan."""
    specials: dict[tuple, tuple[str, str | None]] = {}
    if not root:
        for f in mail_folders:
            sp = f.special
            if sp == "inbox":
                specials[tuple(f.path)] = ("INBOX", None)
            elif sp in SPECIAL_FLAGS:
                found = find_special(boxes, sp)
                if found:
                    specials[tuple(f.path)] = (found, None)
                else:
                    specials[tuple(f.path)] = (prefix + SPECIAL_DEFAULT[sp], SPECIAL_FLAGS[sp])
    base_root = (prefix + utf7_encode(_clean_part(root, delim))) if root else None
    paths: set[tuple] = set()
    for f in mail_folders:
        p = tuple(f.path)
        for i in range(1, len(p) + 1):
            paths.add(p[:i])
    used = {n.casefold() for n, _ in specials.values()}
    if base_root:
        used.add(base_root.casefold())
    names: dict[tuple, str] = {}
    for p in sorted(paths, key=lambda p: (len(p), _needs_clean(p[-1], delim), folder_key(list(p)))):
        if p in specials:
            names[p] = specials[p][0]
            continue
        parent = names[p[:-1]] if len(p) > 1 else base_root
        clean = _clean_part(p[-1], delim)

        def mk(part: str) -> str:
            return (parent + delim + part) if parent is not None else prefix + part

        cand, n = mk(utf7_encode(clean)), 2
        while cand.casefold() in used:
            cand, n = mk(utf7_encode(f"{clean} ({n})")), n + 1
        used.add(cand.casefold())
        names[p] = cand
    out: dict[str, tuple[str, str | None]] = {}
    for f in mail_folders:
        p = tuple(f.path)
        if not p:
            out[folder_key(f.path)] = (prefix + "_", None)
            continue
        out[folder_key(f.path)] = (names[p], specials[p][1] if p in specials else None)
    return out


@dataclass
class AppendTask:
    item_id: int
    target: str
    raw: bytes
    flags: list[str]
    internaldate: object
    size: int
    import_id: str = ""


@dataclass
class AppendResult:
    item_id: int
    ok: bool
    kind: str = ""
    msg: str = ""
    uid: str | None = None
    size: int = 0


class AppendPool:
    """N parallel IMAP connections, each in its own thread.

    - After a temporary error (timeout, dropped connection — possibly AFTER the literal was sent),
      the APPEND is never blindly replayed: reconnect then exact search for X-PST-Import-Id;
      resend only if the message is absent.
    - Pause/stop: tasks not yet started are cancelled ("cancel" result -> item `pending`),
      those in progress run to completion; close() waits for ALL threads to finish (no APPEND after the
      job ends)."""

    def __init__(self, params: ImapParams, n: int, create_folder, should_stop, base_delay: float = 1.0):
        self.params = params
        self.n = max(1, n)
        self.tasks: queue.Queue = queue.Queue(maxsize=self.n * 2)
        self.results: queue.Queue = queue.Queue()
        self.create_folder = create_folder
        self.should_stop = should_stop
        self.base_delay = base_delay
        self.abort = threading.Event()
        self.pending = 0
        self.lock = threading.Lock()
        self.threads = [threading.Thread(target=self._run, daemon=True, name=f"append-{i}") for i in range(self.n)]
        for t in self.threads:
            t.start()

    def _stopping(self) -> bool:
        if self.abort.is_set():
            return True
        try:
            if self.should_stop():
                self.abort.set()
                return True
        except Exception:  # pragma: no cover - DB unavailable: continue
            return False
        return False

    def _run(self) -> None:
        conn = ImapConn(self.params)
        try:
            while True:
                t = self.tasks.get()
                if t is None:
                    return
                if self._stopping():
                    self.results.put(AppendResult(t.item_id, False, "cancel", "cancelled before sending (pause/stop)"))
                    continue
                try:
                    r = self._do(conn, t)
                except Exception as e:  # pragma: no cover - safety
                    r = AppendResult(t.item_id, False, "temp", f"{type(e).__name__}: {e}")
                self.results.put(r)
        finally:
            conn.close()

    def _do(self, conn: ImapConn, t: AppendTask) -> AppendResult:
        created = False
        check = False  # an APPEND may have succeeded: search before resending
        attempt = 0
        while True:
            try:
                if conn.M is None:
                    conn.connect()
                if check and t.import_id:
                    try:
                        uids = conn.find_import_id(t.target, IMPORT_HEADER, t.import_id)
                    except ImapError as se:
                        if se.kind == "temp":
                            raise
                        uids = []  # folder missing / unreadable: the message is not there
                    if uids:
                        return AppendResult(t.item_id, True, uid=uids[0], size=t.size, msg="already present")
                uid = conn.append(t.target, t.raw, t.flags, t.internaldate)
                return AppendResult(t.item_id, True, uid=uid, size=t.size)
            except Exception as e:
                err = wrap_exc(e)
                if err.kind == "trycreate" and not created:
                    created = True
                    try:
                        self.create_folder(conn, t.target)
                        continue
                    except Exception as ce:
                        cerr = wrap_exc(ce)
                        return AppendResult(t.item_id, False, cerr.kind, str(cerr))
                if err.kind == "temp" and attempt < settings.max_retries and not self._stopping():
                    attempt += 1
                    check = True
                    conn.close()
                    end = time.time() + min(60.0, self.base_delay * (2 ** (attempt - 1)))
                    while time.time() < end and not self._stopping():
                        time.sleep(0.1)
                    continue
                return AppendResult(t.item_id, False, err.kind, str(err))

    def submit(self, t: AppendTask) -> None:
        with self.lock:
            self.pending += 1
        self.tasks.put(t)

    def drain(self, block: bool = False, timeout: float = 0.2) -> list[AppendResult]:
        out = []
        while True:
            try:
                r = self.results.get(block=block and not out, timeout=timeout if block and not out else None)
            except queue.Empty:
                break
            with self.lock:
                self.pending -= 1
            out.append(r)
            block = False
        return out

    def close(self) -> None:
        """Cancels the remaining tasks and waits for all threads to finish (no limit: an APPEND in progress
        is bounded by the socket timeout and the retries)."""
        self.abort.set()
        for _ in self.threads:
            self.tasks.put(None)
        for t in self.threads:
            t.join()


class JobRunner:
    def __init__(self, job_id: int, worker_id: str):
        self.job_id = job_id
        self.worker_id = worker_id
        self.job = db.q1("SELECT * FROM jobs WHERE id=?", (job_id,))
        self.mode = self.job["mode"]
        self.acc = db.q1("SELECT * FROM accounts WHERE id=?", (self.job["account_id"],))
        self.srv = db.q1("SELECT * FROM servers WHERE id=?", (self.acc["server_id"],)) if self.acc else None
        self.pst_row = db.q1("SELECT * FROM psts WHERE id=?", (self.acc["pst_id"],)) if self.acc and self.acc["pst_id"] else None
        self.account_id = self.acc["id"] if self.acc else None
        self.pst_id = self.pst_row["id"] if self.pst_row else None
        # items are attached to the PST fingerprint (not its id): the same re-uploaded file recovers its state
        self.pst_key = db.pst_key_of(self.pst_row)
        self.folder_errors: dict[str, str] = {}
        self.progress = repo.empty_progress()
        self.totals = {"mail": 0, "event": 0, "contact": 0}
        self.current_folder: str | None = None
        self._last_flush = 0.0
        self._last_check = 0.0
        self._rate_window: list[tuple[float, int]] = []
        # average rate over the import phases of this run (first / last sample): displayed at the end of the job
        self._rate_first: tuple[float, int] | None = None
        self._rate_last: tuple[float, int] | None = None
        self._item_phase = False
        self.report: dict = {"folders": [], "warnings": [], "verify": {}, "dav": {}, "precheck": {}}
        self.consecutive_temp = 0
        self.dav_targets: dict[str, str] = {}
        # native crashes already suffered by this job (subprocess) and unreadable indexes known for the PST
        self.crashes = crash.get_crashes(job_id)
        self.skips = crash.get_skips(self.pst_key) if self.pst_key else set()

    # ------------------------------------------------------------ utilities
    def log(self, msg: str, level: str = "info") -> None:
        repo.log(self.job_id, msg, level)

    # ------------------------------------------------------------ "current item" marker (libpff crash)
    def _mark(self, kind: str | None = None, fkey: str | None = None, idx: int | None = None,
              key: str | None = None, phase: str | None = None) -> None:
        """Records (committed) what the process is about to read with libpff: if the
        subprocess dies, the parent worker marks this item "libpff crash" and the resume continues after it."""
        if kind is None and phase is None:
            crash.set_marker(self.job_id, None)
            return
        crash.set_marker(self.job_id, {"a": self.account_id, "p": self.pst_id, "s": self.pst_key, "kind": kind, "folder": fkey,
                                       "idx": idx, "key": key, "phase": phase})
        if key is not None:
            crash.maybe_delay()
            crash.maybe_crash(key, "*")
        elif idx is not None:
            crash.maybe_crash(f"fetch:{idx}")
        elif phase:
            crash.maybe_crash(phase)

    def _iter(self, f, kind: str, fkey: str):
        """pst.iter_items with a marker before each read and skipping of indexes known to crash libpff."""
        fkeys = {fkey, legacy_folder_key(f.path)}
        skips = {i for k, fo, i in self.skips if k == kind and fo in fkeys}
        count = int(f.count or 0)
        start = 0
        while start < count:
            if start in skips:
                start += 1
                continue
            self._mark(kind, fkey, start)
            nxt = None
            gen = self.pst.iter_items(f, start)
            try:
                for idx, key, handle in gen:
                    yield idx, key, handle
                    if idx + 1 in skips:
                        nxt = idx + 1
                        break
                    if idx + 1 < count:
                        self._mark(kind, fkey, idx + 1)
            finally:
                gen.close()
            if nxt is None:
                break
            start = nxt
        self._mark()

    def check(self) -> None:
        """Checked between each item: pause / stop requested via the API."""
        r = db.q1("SELECT status FROM jobs WHERE id=?", (self.job_id,))
        st = r["status"] if r else "stopping"
        if st == "paused":
            raise PauseRequested()
        if st in ("stopping", "stopped"):
            raise StopRequested()
        self.flush()

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.check()
            time.sleep(min(0.5, max(0.0, end - time.time())))

    def _should_stop(self) -> bool:
        r = db.q1("SELECT status FROM jobs WHERE id=?", (self.job_id,))
        return (r is None) or r["status"] in ("paused", "stopping", "stopped")

    def set_phase(self, phase: str) -> None:
        now = time.time()
        db.ex("UPDATE jobs SET phase=?, heartbeat=? WHERE id=?", (phase, now, self.job_id))
        self.log(f"Phase: {phase}")
        self._item_phase = phase in ("mail", "calendar", "contacts")
        if self._item_phase and self.account_id and self.pst_key:
            # rate window seeded at the start of the phase: a short job has a rate from the 1st flush
            self.recompute_progress()
            done = sum(p["done"] for p in self.progress.values())
            self._rate_window = [(now, done)]
            if self._rate_first is None:
                self._rate_first = (now, done)

    def _avg_rate(self) -> float | None:
        if not (self._rate_first and self._rate_last):
            return None
        (t0, d0), (t1, d1) = self._rate_first, self._rate_last
        return round((d1 - d0) / (t1 - t0), 2) if t1 - t0 > 0 and d1 >= d0 else None

    def recompute_progress(self) -> None:
        for kind, pk in PROGRESS_KEY.items():
            rows = db.q("SELECT status, COUNT(*) n FROM items WHERE account_id=? AND pst_key=? AND kind=? GROUP BY status",
                        (self.account_id, self.pst_key, kind))
            c = {r["status"]: r["n"] for r in rows}
            self.progress[pk] = {"done": c.get("ok", 0) + c.get("skipped", 0), "total": self.totals[kind],
                                 "errors": c.get("error", 0)}

    def flush(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_flush < 1.0:
            return
        self._last_flush = now
        self.recompute_progress()
        done = sum(p["done"] for p in self.progress.values())
        total = sum(p["total"] for p in self.progress.values())
        self._rate_window.append((now, done))
        # sliding average over 60 s, otherwise since the start of the phase (first sample kept)
        self._rate_window = [x for x in self._rate_window if now - x[0] <= 60]
        t0, d0 = self._rate_window[0]
        rate = (done - d0) / (now - t0) if now - t0 >= 0.2 else None
        if self._item_phase:
            self._rate_last = (now, done)
        eta = (max(0, total - done) / rate) if rate is not None and rate > 0 else (0 if total and done >= total else None)
        db.ex("UPDATE jobs SET progress=?, current_folder=?, rate=?, eta=?, heartbeat=? WHERE id=?",
              (db.jdump(self.progress), self.current_folder, round(rate, 2) if rate is not None else None,
               round(eta) if eta is not None else None, now, self.job_id))

    def item_rows(self, kind: str, fkey: str, path: list[str] | None = None) -> dict[str, dict]:
        legacy = legacy_folder_key(path) if path is not None else fkey
        rows = db.q("SELECT id, item_key, status, target, hash, error FROM items WHERE account_id=? AND pst_key=? AND kind=? "
                    "AND folder IN (?, ?)", (self.account_id, self.pst_key, kind, fkey, legacy))
        return {r["item_key"]: dict(r) for r in rows}

    @staticmethod
    def fallback_rows(rows: dict[str, dict]) -> dict[int, dict]:
        """Items in error recorded under the fallback key "<fnid>-x<index>" (unreadable during iteration):
        {index: row}. If the item becomes readable (rerun), it is processed under its real key and the fallback
        row is deleted."""
        out = {}
        for k, r in rows.items():
            m = FALLBACK_KEY_RE.search(k)
            if m and r["status"] == "error":
                out[int(m.group(1))] = r
        return out

    def _resolve_key(self, rows: dict, fallback: dict, idx: int, key: str) -> tuple[dict | None, dict | None]:
        """(row to use for wants(), fallback row to delete once the real key is processed)."""
        r = rows.get(key)
        fb = fallback.get(idx)
        if fb is None or fb["item_key"] == key:
            return r, None
        return (r if r is not None else fb), fb

    def _drop_fallback(self, fb: dict | None) -> None:
        if fb is not None:
            db.ex("DELETE FROM items WHERE id=? AND status='error'", (fb["id"],))

    def upsert_item(self, kind: str, key: str, fkey: str, status: str, **kw) -> int:
        now = time.time()
        cols = {"target": kw.get("target"), "error": kw.get("error"), "warnings": kw.get("warnings"),
                "subject": kw.get("subject"), "hash": kw.get("hash"), "size": kw.get("size")}
        cur = db.ex(
            "INSERT INTO items(account_id,pst_id,pst_key,kind,item_key,folder,status,target,error,warnings,subject,hash,size,job_id,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id,pst_key,kind,item_key) DO UPDATE SET pst_id=excluded.pst_id, folder=excluded.folder, status=excluded.status, "
            "target=COALESCE(excluded.target, items.target), error=excluded.error, warnings=excluded.warnings, "
            "subject=COALESCE(excluded.subject, items.subject), hash=COALESCE(excluded.hash, items.hash), "
            "size=COALESCE(excluded.size, items.size), job_id=excluded.job_id, updated_at=excluded.updated_at "
            "RETURNING id",
            (self.account_id, self.pst_id, self.pst_key, kind, key, fkey, status, cols["target"], cols["error"], cols["warnings"],
             cols["subject"], cols["hash"], cols["size"], self.job_id, now))
        return cur.fetchone()[0]

    def set_item(self, item_id: int, status: str, error: str | None = None) -> None:
        db.ex("UPDATE items SET status=?, error=?, job_id=?, updated_at=? WHERE id=?",
              (status, error, self.job_id, time.time(), item_id))

    def wants(self, r: dict | None) -> bool:
        st = r["status"] if r else None
        if st == "error" and (r.get("error") or "").startswith(crash.CRASH_MSG):
            # item that crashed libpff: ignored, except on "retry errors" as long as this run has not crashed
            return self.mode == "errors" and self.crashes == 0
        if self.mode == "errors":
            return st in ("error", "inflight", "pending")
        return st not in ("ok", "skipped")

    def imap_params(self) -> ImapParams:
        return ImapParams(host=self.srv["imap_host"], port=self.srv["imap_port"], security=self.srv["imap_security"],
                          verify_tls=bool(self.srv["verify_tls"]), login=self.acc["login"] or self.acc["email"],
                          password=decrypt(self.acc["password_enc"]))

    # ------------------------------------------------------------ execution
    def run(self) -> None:
        if not self.acc or not self.srv:
            self._finish("failed", "account or server deleted")
            return
        started = time.time()
        self.report.update({"job_id": self.job_id, "account": self.acc["email"], "server": self.srv["name"],
                            "mode": self.mode, "started_at": started})
        self.log(f"Job {self.job_id} started for {self.acc['email']} (mode {self.mode})")
        self._mark()  # stale marker from a killed process (abrupt container stop): not a crash
        try:
            if not self.pst_row:
                raise JobFailed("no PST linked to the account")
            if self.pst_row["status"] != "valid":
                raise JobFailed(f"PST not valid (status {self.pst_row['status']})")
            eng = engine()
            self._mark(phase="open")
            with eng.PstFile(self.pst_row["path"]) as pst:
                self.pst = pst
                self.eng = eng
                self.folders = pst.folders()
                self._mark()
                for f in self.folders:
                    k = {"mail": "mail", "calendar": "event", "contacts": "contact"}.get(f.kind)
                    if k and self._phase_enabled(k):
                        self.totals[k] += int(f.count or 0)
                self.flush(force=True)
                self.precheck()
                if self.acc["opt_mail"]:
                    self.phase_mail()
                if self.srv["dav_enabled"] and self.acc["opt_calendar"]:
                    self.phase_dav("calendar")
                if self.srv["dav_enabled"] and self.acc["opt_contacts"]:
                    self.phase_dav("contacts")
                self.phase_verify()
        except PauseRequested:
            self.flush(force=True)
            db.ex("UPDATE jobs SET worker_id=NULL, rate=NULL, eta=NULL WHERE id=?", (self.job_id,))
            self.log("Job paused (resume possible)", "warning")
            return
        except StopRequested:
            self.log("Job stopped on request", "warning")
            self._finish("stopped", None)
            return
        except JobFailed as e:
            self.log(f"Failure: {e}", "error")
            self._finish("failed", str(e))
            return
        except Exception as e:
            self.log(f"Unexpected error: {type(e).__name__}: {e}", "error")
            self._finish("failed", f"{type(e).__name__}: {e}")
            return
        self.recompute_progress()
        n_err = sum(p["errors"] for p in self.progress.values())
        verify_ko = bool(self.report["verify"].get("problems"))
        n_unfinished = self.count_unfinished()
        err = None
        if n_unfinished:
            err = f"{n_unfinished} unfinished item(s) (pending / in progress) — rerun the job"
            self.log(err, "error")
        status = "done_with_errors" if (n_err or verify_ko or n_unfinished) else "done"
        self.log(f"Finished: {status} ({n_err} error(s))", "warning" if status != "done" else "info")
        self._finish(status, err)

    def count_unfinished(self) -> int:
        """Items of active phases left `pending`/`inflight`: the job cannot be "finished"."""
        kinds = [k for k in ("mail", "event", "contact") if self._phase_enabled(k)]
        if not kinds or not self.pst_key:
            return 0
        return db.q1(f"SELECT COUNT(*) n FROM items WHERE account_id=? AND pst_key=? AND status IN ('pending','inflight') "
                     f"AND kind IN ({','.join('?' * len(kinds))})", (self.account_id, self.pst_key, *kinds))["n"]

    def _phase_enabled(self, kind: str) -> bool:
        if kind == "mail":
            return bool(self.acc["opt_mail"])
        if kind == "event":
            return bool(self.srv["dav_enabled"] and self.acc["opt_calendar"])
        return bool(self.srv["dav_enabled"] and self.acc["opt_contacts"])

    def _finish(self, status: str, error: str | None) -> None:
        now = time.time()
        try:
            if self.account_id and self.pst_key:
                self.flush(force=True)
        except Exception:
            pass
        path = None
        try:
            path = self.write_report(status, error, now)
        except Exception as e:
            self.log(f"Report not written: {e}", "error")
        # run finished: crash counter reset BEFORE the final status becomes visible. Otherwise a worker
        # stop right after (subprocess still alive, killed -> _run_job does not call reset_crashes) left
        # `job:<id>:crashes` > 0 and the next "errors" rerun skipped the "libpff crash" items.
        crash.reset_crashes(self.job_id)
        # average rate of the import phases kept (displayed for finished jobs, even very short ones)
        db.ex("UPDATE jobs SET status=?, error=?, finished_at=?, worker_id=NULL, rate=?, eta=NULL, "
              "report_path=COALESCE(?, report_path), current_folder=NULL WHERE id=?",
              (status, error, now, self._avg_rate(), path, self.job_id))

    # ------------------------------------------------------------ precheck
    def precheck(self) -> None:
        self.set_phase("precheck")
        params = self.imap_params()
        try:
            conn = ImapConn(params)
            with_retry(lambda c: c.connect(), conn, 3, should_stop=self._should_stop)
        except ImapError as e:
            if e.kind == "auth":
                db.ex("UPDATE accounts SET status='auth_failed', last_test=? WHERE id=?", (time.time(), self.account_id))
            raise JobFailed(f"IMAP: {e}")
        try:
            quota = conn.quota()
            self.report["precheck"]["imap"] = {"ok": True, "capabilities": sorted(conn.caps)[:50], "quota": quota}
            self.log(f"IMAP connection OK ({params.host})")
            if quota and quota.get("limit"):
                free = quota["limit"] - (quota.get("used") or 0)
                est = int(self.pst_row["size"] * 0.8)
                if free < est:
                    w = (f"quota: {free // 2**20} MB free for a PST of {self.pst_row['size'] // 2**20} MB "
                         f"(estimate {est // 2**20} MB) — risk of exceeding")
                    self.report["warnings"].append(w)
                    self.log(w, "warning")
        finally:
            conn.close()
        if self.srv["dav_enabled"]:
            for phase, kind, opt, tmpl in (("calendar", "caldav", "opt_calendar", "caldav_url"),
                                          ("contacts", "carddav", "opt_contacts", "carddav_url")):
                if not self.acc[opt]:
                    continue
                need = any(f.kind == phase for f in self.folders)
                if not need:
                    continue
                try:
                    with DavClient(params.login, params.password, bool(self.srv["verify_tls"])) as dav:
                        url = dav.discover(kind, self.srv[tmpl] or None, self.acc["email"], self.srv["imap_host"])
                except DavError as e:
                    raise JobFailed(f"{kind.upper()}: {e}")
                self.dav_targets[phase] = url
                self.report["dav"][phase] = {"url": url}
                self.log(f"{kind.upper()}: target collection {url}")
        db.ex("UPDATE accounts SET status='ok', last_test=? WHERE id=?", (time.time(), self.account_id))
        self.check()

    # ------------------------------------------------------------ mail
    def _ensure_folder(self, conn: ImapConn, name: str, existing: set[str], delim: str, flag: str | None = None) -> None:
        if name.upper() == "INBOX" or name in existing:
            return
        parts = name.split(delim)
        for i in range(1, len(parts)):
            parent = delim.join(parts[:i])
            if parent and parent not in existing and parent.upper() != "INBOX":
                with_retry(lambda c: c.create(parent), conn, settings.max_retries, should_stop=self._should_stop)
                existing.add(parent)
        with_retry(lambda c: c.create(name, flag), conn, settings.max_retries, should_stop=self._should_stop)
        existing.add(name)

    def phase_mail(self) -> None:
        self.set_phase("mail")
        mail_folders = [f for f in self.folders if f.kind == "mail"]
        params = self.imap_params()
        conn = ImapConn(params)
        with_retry(lambda c: c.connect(), conn, settings.max_retries, should_stop=self._should_stop)
        pool = None
        try:
            boxes = conn.list()
            delim = conn.delimiter()
            prefix = personal_prefix(conn)
            if prefix and not prefix.endswith(delim):
                prefix = ""
            existing = {b.name for b in boxes}
            plan = plan_folders(mail_folders, boxes, delim, (self.srv["root_folder"] or "").strip() or None, prefix)
            self.report["imap_delimiter"] = delim
            for f in mail_folders:
                fk = folder_key(f.path)
                name, flag = plan[fk]
                try:
                    self._ensure_folder(conn, name, existing, delim, flag)
                except ImapError as e:
                    if e.kind in ("temp", "fatal", "auth"):
                        raise
                    # CREATE refused: only the items of this folder are in error, the mailbox continues
                    msg = f"IMAP folder '{utf7_decode(name)}' not created: {e}"
                    self.folder_errors[fk] = msg
                    self.log(f"Folder '{fk}': {msg}", "error")
            self.log(f"{len(mail_folders)} mail folder(s) ready (separator '{delim}')")

            def create_folder(c: ImapConn, target: str) -> None:
                c.create(target)

            pool = AppendPool(params, int(self.srv["workers"] or 1), create_folder, self._should_stop)
            for f in mail_folders:
                self._mail_folder(f, plan[folder_key(f.path)][0], conn, pool)
        finally:
            if pool is not None:
                try:
                    self._wait_pool(pool)
                finally:
                    pool.close()  # waits for all threads: no more APPEND after the phase ends
                    try:
                        self._handle_results(pool.drain())
                    except JobFailed:
                        pass
            conn.close()

    def _handle_results(self, results: list[AppendResult]) -> None:
        fatal = None
        for r in results:
            if r.ok:
                self.consecutive_temp = 0
                self.set_item(r.item_id, "ok")
            elif r.kind == "cancel":
                self.set_item(r.item_id, "pending")  # never sent (pause/stop before the APPEND)
            elif r.kind == "temp" and self._should_stop():
                continue  # interrupted by pause/stop: stays "inflight", rechecked on resume
            else:
                self.set_item(r.item_id, "error", f"[{r.kind}] {r.msg}")
                self.log(f"APPEND failure (item {r.item_id}): {r.msg}", "error")
                if r.kind == "fatal":
                    fatal = r.msg
                elif r.kind in ("temp", "auth"):
                    self.consecutive_temp += 1
        if fatal:
            raise JobFailed(f"permanent server error: {fatal}")
        if self.consecutive_temp >= max(5, 3 * int(self.srv["workers"] or 1)):
            raise JobFailed("IMAP server unavailable (too many consecutive errors)")

    def _wait_pool(self, pool: AppendPool) -> None:
        """Waits for ALL results (no time limit: each APPEND is bounded by the socket timeout and
        the retries). Pause/stop: tasks not yet started are cancelled, APPENDs in progress run to completion."""
        while pool.pending > 0:
            if not pool.abort.is_set() and self._should_stop():
                pool.abort.set()
            self._handle_results(pool.drain(block=True, timeout=0.5))
        self._handle_results(pool.drain())

    def _resolve_on_server(self, conn: ImapConn, rows: dict[str, dict]) -> None:
        """Before any resend: `inflight` items (APPEND possibly succeeded) and `error` items already tried (hash known,
        e.g. timeout after the literal was sent) are searched for EXACTLY by X-PST-Import-Id."""
        todo = [r for r in rows.values()
                if r["target"] and (r["status"] == "inflight"
                                    or (r["status"] == "error" and r["hash"] and self.wants(r)))]
        if not todo:
            return
        n_infl = sum(r["status"] == "inflight" for r in todo)
        self.log(f"{n_infl} 'inflight' item(s) and {len(todo) - n_infl} in error to check on the server")
        for r in todo:
            self.check()
            import_id = import_id_of(self.pst_row["pst_sha"], r["item_key"])
            target = r["target"]
            try:
                uids = with_retry(lambda c: c.find_import_id(target, IMPORT_HEADER, import_id), conn,
                                  settings.max_retries, should_stop=self._should_stop)
            except ImapError as e:
                if e.kind == "temp":
                    raise JobFailed(f"cannot verify items already sent: {e}")
                uids = []  # folder missing -> not sent yet
            if uids:
                self.set_item(r["id"], "ok")
                r["status"] = "ok"
                if len(uids) > 1:
                    self.log(f"Warning: {len(uids)} copies of {import_id} already present", "warning")
            elif r["status"] == "inflight":
                self.set_item(r["id"], "pending")
                r["status"] = "pending"

    def _mail_folder(self, f, target: str, conn: ImapConn, pool: AppendPool) -> None:
        fkey = folder_key(f.path)
        self.current_folder = fkey
        rows = self.item_rows("mail", fkey, f.path)
        if self.mode == "errors" and not any(r["status"] in ("error", "inflight", "pending") for r in rows.values()):
            return
        self.log(f"Folder '{fkey}' -> {target} ({f.count} item(s))")
        self.flush(force=True)
        self._wait_pool(pool)
        if fkey in self.folder_errors:
            self._fail_folder(f, fkey, "mail", rows, target, self.folder_errors[fkey])
            return
        self._resolve_on_server(conn, rows)
        fallback = self.fallback_rows(rows)
        pst_sha = self.pst_row["pst_sha"]
        for idx, key, handle in self._iter(f, "mail", fkey):
            self.check()
            self._handle_results(pool.drain())
            r, fb = self._resolve_key(rows, fallback, idx, key)
            if not self.wants(r):
                continue
            self._mail_item(f, fkey, idx, key, handle, target, pool, pst_sha)
            self._drop_fallback(fb)
        self._wait_pool(pool)
        # post-folder check: messages bearing our fingerprint >= sent (ok) to this folder
        self.check_folder_status(conn, fkey, target)

    def _fail_folder(self, f, fkey: str, kind: str, rows: dict, target: str, msg: str) -> None:
        """Unusable target folder: its items to send are marked as errors (without reading the content)."""
        fallback = self.fallback_rows(rows)
        for idx, key, handle in self._iter(f, kind, fkey):
            self.check()
            r, fb = self._resolve_key(rows, fallback, idx, key)
            if not self.wants(r) or (r is not None and r["status"] == "inflight"):
                continue
            self.upsert_item(kind, key, fkey, "error", target=target, error=msg)
            self._drop_fallback(fb)

    def _mail_item(self, f, fkey: str, idx: int, key: str, handle, target: str, pool: AppendPool, pst_sha: str) -> None:
        import_id = import_id_of(pst_sha, key)
        self._mark("mail", fkey, idx, key)  # libpff read of this item
        try:
            built = self.pst.build_message(handle, import_id)
        except Exception as e:
            self.upsert_item("mail", key, fkey, "error", target=target, error=f"construction: {type(e).__name__}: {e}")
            self.log(f"Cannot build ({fkey} #{idx}): {e}", "error")
            return
        if built is None:
            self.upsert_item("mail", key, fkey, "skipped", target=target, error="not a mail")
            return
        try:
            errs = list(self.eng.verify_message(built, handle) or [])
        except Exception as e:
            errs = [f"verification: {type(e).__name__}: {e}"]
        if IMPORT_HEADER.lower().encode() not in built.raw[:65536].lower():
            errs.append(f"header {IMPORT_HEADER} missing")
        warnings = db.jdump(built.warnings) if built.warnings else None
        if errs:
            self.upsert_item("mail", key, fkey, "error", target=target, subject=built.subject,
                             error="; ".join(errs)[:2000], warnings=warnings)
            self.log(f"Mail rejected '{built.subject}': {'; '.join(errs)}", "error")
            return
        if built.warnings:
            self.log(f"Warning '{built.subject}': {', '.join(built.warnings)}", "warning")
        item_id = self.upsert_item("mail", key, fkey, "inflight", target=target, subject=built.subject,
                                   hash=norm_hash(built.raw), size=len(built.raw), warnings=warnings)
        pool.submit(AppendTask(item_id, target, built.raw, list(built.flags or []), built.internaldate, len(built.raw),
                               import_id))

    def _path_of(self, fkey: str) -> list[str]:
        for f in getattr(self, "folders", []):
            if folder_key(f.path) == fkey or legacy_folder_key(f.path) == fkey:
                return list(f.path)
        return [fkey]

    def check_folder_status(self, conn: ImapConn, fkey: str, target: str) -> dict:
        """Check of a target folder: number of messages bearing OUR fingerprint (X-PST-Import-Id containing
        "<sha>-") >= items marked as sent to this folder. Pre-existing content is not counted."""
        n_ok = db.q1("SELECT COUNT(*) n FROM items WHERE account_id=? AND pst_key=? AND kind='mail' AND target=? AND status='ok'",
                     (self.account_id, self.pst_key, target))["n"]
        try:
            prefix = f"{self.pst_row['pst_sha']}-"
            present = with_retry(lambda c: c.count_import_prefix(target, IMPORT_HEADER, prefix), conn,
                                 settings.max_retries, should_stop=self._should_stop)
        except ImapError as e:
            present = None
            self.log(f"Cannot count {target}: {e}", "warning")
        ok = present is not None and present >= n_ok
        entry = {"path": self._path_of(fkey), "folder": fkey, "target": target, "sent": n_ok,
                 "present": present, "ok": ok}
        self.report["folders"] = [x for x in self.report["folders"] if x["target"] != target] + [entry]
        if not ok:
            self.log(f"Folder check '{fkey}': {present} present < {n_ok} sent", "error")
        return entry

    # ------------------------------------------------------------ DAV
    def phase_dav(self, phase: str) -> None:
        folders = [f for f in self.folders if f.kind == phase]
        if not folders:
            return
        self.set_phase(phase)
        kind = KIND_OF_PHASE[phase]
        url = self.dav_targets.get(phase)
        params = self.imap_params()
        with DavClient(params.login, params.password, bool(self.srv["verify_tls"])) as dav:
            if not url:
                dk = "caldav" if phase == "calendar" else "carddav"
                tmpl = self.srv["caldav_url" if phase == "calendar" else "carddav_url"] or None
                url = dav.discover(dk, tmpl, self.acc["email"], self.srv["imap_host"])
                self.dav_targets[phase] = url
            builder = self.pst.build_event if phase == "calendar" else self.pst.build_contact
            for f in folders:
                fkey = folder_key(f.path)
                self.current_folder = fkey
                rows = self.item_rows(kind, fkey, f.path)
                if self.mode == "errors" and not any(r["status"] in ("error", "inflight", "pending") for r in rows.values()):
                    continue
                self.log(f"Folder '{fkey}' -> {url} ({f.count} item(s))")
                fallback = self.fallback_rows(rows)
                for idx, key, handle in self._iter(f, kind, fkey):
                    self.check()
                    r, fb = self._resolve_key(rows, fallback, idx, key)
                    if not self.wants(r):
                        continue
                    self._dav_item(dav, url, phase, kind, fkey, idx, key, handle, builder)
                    self._drop_fallback(fb)
        self.flush(force=True)

    def _dav_item(self, dav: DavClient, url: str, phase: str, kind: str, fkey: str, idx: int, key: str, handle,
                  builder) -> None:
        self._mark(kind, fkey, idx, key)  # libpff read of this item
        try:
            obj = builder(handle)
        except Exception as e:
            self.upsert_item(kind, key, fkey, "error", target=url, error=f"construction: {type(e).__name__}: {e}")
            self.log(f"Cannot build ({fkey} #{idx}): {e}", "error")
            return
        if obj is None:
            self.upsert_item(kind, key, fkey, "skipped", target=url, error="item cannot be converted")
            return
        item_id = self.upsert_item(kind, key, fkey, "inflight", target=url, subject=obj.summary,
                                   hash=obj.filename, size=len(obj.content.encode("utf-8")))
        err = self._dav_put(dav, url, obj, phase)
        if err:
            self.set_item(item_id, "error", err)
            self.log(f"PUT failure '{obj.summary}': {err}", "error")
        else:
            self.set_item(item_id, "ok")

    def _dav_put(self, dav: DavClient, url: str, obj, phase: str) -> str | None:
        kind = "caldav" if phase == "calendar" else "carddav"
        recreated = False
        for attempt in range(settings.max_retries + 1):
            try:
                dav.put(url, obj.filename, obj.content, kind)
                return None
            except DavError as e:
                if e.status in (404, 409) and not recreated:
                    # collection disappeared since discovery: recreated (MKCALENDAR / extended MKCOL), then retried
                    recreated = True
                    try:
                        dav.ensure_collection(kind, url)
                    except DavError as ce:
                        return f"[{'temp' if ce.temporary else 'perm'}] {ce}"
                    self.log(f"{kind.upper()}: collection {url} missing, recreated", "warning")
                    continue
                if not e.temporary or attempt >= settings.max_retries:
                    return f"[{'temp' if e.temporary else 'perm'}] {e}"
                self._sleep(min(60.0, 2.0 ** attempt))
        return "failure"

    # ------------------------------------------------------------ final checks
    def phase_verify(self) -> None:
        self.set_phase("verify")
        problems: list[str] = []
        verify: dict = {}
        if self.acc["opt_mail"]:
            params = self.imap_params()
            conn = ImapConn(params)
            try:
                with_retry(lambda c: c.connect(), conn, settings.max_retries, should_stop=self._should_stop)
                targets = db.q("SELECT target, MIN(folder) folder FROM items WHERE account_id=? AND pst_key=? AND kind='mail' "
                               "AND status='ok' GROUP BY target", (self.account_id, self.pst_key))
                for t in targets:
                    self.check()
                    e = self.check_folder_status(conn, t["folder"], t["target"])
                    if not e["ok"]:
                        problems.append(f"folder {t['folder']}: {e['present']} present < {e['sent']} sent")
                # sample re-read by header (exact match) + hash
                ids = [r["id"] for r in db.q("SELECT id FROM items WHERE account_id=? AND pst_key=? AND kind='mail' AND status='ok'",
                                             (self.account_id, self.pst_key))]
                sample = random.sample(ids, min(settings.verify_sample, len(ids)))
                checked, good, bad = 0, 0, []
                for iid in sample:
                    self.check()
                    it = db.q1("SELECT * FROM items WHERE id=?", (iid,))
                    import_id = import_id_of(self.pst_row["pst_sha"], it["item_key"])
                    try:
                        uids = with_retry(lambda c: c.find_import_id(it["target"], IMPORT_HEADER, import_id), conn,
                                          settings.max_retries, should_stop=self._should_stop)
                        raw = with_retry(lambda c: c.fetch_raw(it["target"], uids[0]), conn, settings.max_retries,
                                         should_stop=self._should_stop) if uids else None
                    except ImapError as e:
                        bad.append({"item_key": it["item_key"], "subject": it["subject"], "problem": f"re-read: {e}"})
                        checked += 1
                        continue
                    checked += 1
                    if not raw:
                        bad.append({"item_key": it["item_key"], "subject": it["subject"], "problem": "not found on the server"})
                    elif it["hash"] and norm_hash(raw) != it["hash"]:
                        bad.append({"item_key": it["item_key"], "subject": it["subject"], "problem": "hash differs"})
                    else:
                        good += 1
                verify["sample"] = {"checked": checked, "ok": good, "problems": bad}
                if bad:
                    problems.append(f"sample: {len(bad)}/{checked} non-conforming mail(s)")
                self.log(f"Sample re-read: {good}/{checked} conforming",
                         "info" if not bad else "error")
            except ImapError as e:
                problems.append(f"IMAP verification failed: {e}")
                self.log(f"IMAP verification failed: {e}", "error")
            finally:
                conn.close()
        if self.srv["dav_enabled"]:
            params = self.imap_params()
            for phase, kind in (("calendar", "event"), ("contacts", "contact")):
                url = self.dav_targets.get(phase)
                if not url:
                    continue
                ours = {r["hash"] for r in db.q("SELECT hash FROM items WHERE account_id=? AND pst_key=? AND kind=? "
                                                "AND status='ok' AND hash IS NOT NULL", (self.account_id, self.pst_key, kind))}
                n_ok = db.q1("SELECT COUNT(*) n FROM items WHERE account_id=? AND pst_key=? AND kind=? AND status='ok'",
                             (self.account_id, self.pst_key, kind))["n"]
                try:
                    with DavClient(params.login, params.password, bool(self.srv["verify_tls"])) as dav:
                        # only OUR resources (file name = deterministic UID) count, not pre-existing content
                        present = len(dav.names(url) & ours)
                except DavError as e:
                    present = None
                    problems.append(f"{phase}: cannot count ({e})")
                ok = present is not None and present >= n_ok
                self.report["dav"][phase] = {"url": url, "sent": n_ok, "present": present, "ok": ok}
                self.log(f"{phase}: {present} resource(s) present for {n_ok} sent",
                         "info" if ok else "error")
                if present is not None and not ok:
                    problems.append(f"{phase}: {present} present < {n_ok} sent")
        verify["problems"] = problems
        self.report["verify"] = verify

    # ------------------------------------------------------------ report
    def write_report(self, status: str, error: str | None, finished: float) -> str | None:
        if not self.account_id:
            return None
        settings.reports_dir.mkdir(parents=True, exist_ok=True)
        totals = {}
        for kind, pk in PROGRESS_KEY.items():
            rows = db.q("SELECT status, COUNT(*) n FROM items WHERE account_id=? AND pst_key=? AND kind=? GROUP BY status",
                        (self.account_id, self.pst_key, kind)) if self.pst_id else []
            c = {r["status"]: r["n"] for r in rows}
            nwarn = db.q1("SELECT COUNT(*) n FROM items WHERE account_id=? AND pst_key=? AND kind=? AND warnings IS NOT NULL",
                          (self.account_id, self.pst_key, kind))["n"] if self.pst_id else 0
            present = None
            if pk == "mail":
                fs = [f.get("present") for f in self.report.get("folders", [])]
                present = sum(x for x in fs if x is not None) if fs else None
            else:
                present = (self.report.get("dav", {}).get(pk) or {}).get("present")
            totals[pk] = {"source": self.totals[kind], "sent": c.get("ok", 0), "present": present,
                          "errors": c.get("error", 0), "warnings": nwarn, "skipped": c.get("skipped", 0),
                          "pending": c.get("pending", 0) + c.get("inflight", 0)}
        errors = [dict(r) for r in db.q(
            "SELECT kind, folder, item_key, subject, error FROM items WHERE account_id=? AND pst_key=? AND status='error' "
            "ORDER BY id LIMIT 5000", (self.account_id, self.pst_key))] if self.pst_id else []
        warns = []
        if self.pst_id:
            for r in db.q("SELECT kind, folder, item_key, subject, warnings FROM items WHERE account_id=? AND pst_key=? "
                          "AND warnings IS NOT NULL ORDER BY id LIMIT 5000", (self.account_id, self.pst_key)):
                d = dict(r)
                d["warnings"] = db.jload(d["warnings"], [])
                warns.append(d)
        rep = dict(self.report)
        for pk in ("mail", "calendar", "contacts"):
            t = totals[pk]
            rep[pk] = {k: t[k] for k in ("sent", "present", "errors", "warnings", "skipped")}
        smp = (self.report.get("verify") or {}).get("sample")
        if smp:
            rep["sample_check"] = (f"{smp['ok']}/{smp['checked']} mail(s) re-read by X-PST-Import-Id, identical hash"
                                   + (f" — {len(smp['problems'])} problem(s)" if smp["problems"] else ""))
        rep.update({"status": status, "error": error, "finished_at": finished, "totals": totals,
                    "errors": errors, "item_warnings": warns,
                    "pst": {"id": self.pst_id, "filename": self.pst_row["filename"] if self.pst_row else None,
                            "sha": self.pst_row["pst_sha"] if self.pst_row else None}})
        path = settings.reports_dir / f"job-{self.job_id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)
        return str(path)


def run_job(job_id: int, worker_id: str) -> None:
    JobRunner(job_id, worker_id).run()
