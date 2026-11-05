"""Job execution (= one mailbox): precheck -> mail -> calendar -> contacts -> verify.

Idempotence: `items` table (account_id, pst_key = PST fingerprint, kind, item_key); a mail is marked
`inflight` (committed) BEFORE its APPEND, then `ok`/`error`. On resume, `inflight` items (and `error` items already tried)
are checked by `UID SEARCH HEADER X-PST-Import-Id` + exact re-read of the header (`<sha-key>`).
DAV: deterministic UIDs -> idempotent PUT.
"""
from __future__ import annotations

import collections
import json
import os
import queue
import random
import re
import threading
import time
from dataclasses import dataclass, field

from . import builders, crash, db, ordering, repo, tuner
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
# unresolved cid warnings: aggregated per folder in the log (still recorded per item in items.warnings)
CID_WARNING_RE = re.compile(r"^(embedded:)*cid-unresolved:")


class PauseRequested(Exception):
    pass


class StopRequested(Exception):
    pass


class JobFailed(Exception):
    pass


norm_hash = builders.norm_hash  # line-ending-insensitive hash (the server stores CRLF)


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
    """N parallel IMAP connections, each in its own thread, resizable while running (auto-tuning).

    - After a temporary error (timeout, dropped connection — possibly AFTER the literal was sent),
      the APPEND is never blindly replayed: reconnect then exact search for X-PST-Import-Id;
      resend only if the message is absent.
    - Pause/stop: tasks not yet started are cancelled ("cancel" result -> item `pending`),
      those in progress run to completion; close() waits for ALL threads to finish (no APPEND after the
      job ends).
    - resize(n): growing starts threads (new connections); shrinking lets surplus threads exit only BETWEEN
      two tasks — after the current APPEND is finished and its result reported — and queued tasks stay queued
      for the remaining threads: no task is lost or run twice. The task queue bound (2 x connections)
      follows the current size."""

    def __init__(self, params: ImapParams, n: int, create_folder, should_stop, base_delay: float = 1.0):
        self.params = params
        self.create_folder = create_folder
        self.should_stop = should_stop
        self.base_delay = base_delay
        self.abort = threading.Event()
        self.results: queue.Queue = queue.Queue()
        self.pending = 0                     # submitted, result not yet drained
        self.lock = threading.Lock()         # pending counter
        self.cond = threading.Condition()    # task queue, sizes, statistics
        self._queue: collections.deque = collections.deque()
        self._target = 0
        self._alive = 0                      # threads not (yet) retired
        self._busy = 0                       # threads running an APPEND
        self._closing = False
        self._seq = 0
        self.threads: list[threading.Thread] = []
        self._lat_sum = 0.0
        self._lat_n = 0
        self._lat_buckets: dict = {}         # size bucket -> (sum of seconds, count): tuner input
        self._errors: list = []              # transient errors seen (tuner.ErrorNote), retried or not
        self.resize(max(1, n))

    @property
    def n(self) -> int:
        """Current (target) number of connections."""
        return self._target

    def resize(self, n: int) -> None:
        n = max(1, int(n))
        with self.cond:
            if self._closing:
                return
            self._target = n
            for _ in range(max(0, n - self._alive)):
                t = threading.Thread(target=self._run, daemon=True, name=f"append-{self._seq}")
                self._seq += 1
                self._alive += 1
                self.threads.append(t)
                t.start()
            self.cond.notify_all()  # surplus idle threads retire, blocked submitters re-check the bound

    def alive(self) -> int:
        with self.cond:
            return self._alive

    def queued(self) -> int:
        with self.cond:
            return len(self._queue)

    def busy(self) -> int:
        with self.cond:
            return self._busy

    def take_stats(self) -> tuple[float | None, list]:
        """(mean APPEND latency, transient errors as tuner.ErrorNote) since the previous call."""
        lat, errs, _b = self.take_stats_full()
        return lat, errs

    def take_stats_full(self) -> tuple[float | None, list, dict]:
        """(mean APPEND latency, transient errors, latencies by size bucket) since the previous call."""
        with self.cond:
            lat = self._lat_sum / self._lat_n if self._lat_n else None
            errs, self._errors = self._errors, []
            buckets, self._lat_buckets = self._lat_buckets, {}
            self._lat_sum, self._lat_n = 0.0, 0
        return lat, errs, buckets

    def _note_error(self, err) -> None:
        """Keeps the classified kind (ImapError.kind) with the message: `[Errno 113]` or `NO [CONTACTADMIN]`
        are `temp` for wrap_exc without matching any text pattern of the tuner."""
        from .tuner import ErrorNote, classify_error
        if classify_error(err):
            with self.cond:
                self._errors.append(ErrorNote.of(err))

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

    def _next(self):
        """Next task, or None when this thread must exit (surplus after a shrink, or closing with an empty queue)."""
        with self.cond:
            while True:
                if self._alive > self._target and not self._closing:
                    self._alive -= 1  # retire between two tasks: nothing in hand
                    self.cond.notify_all()
                    return None
                if self._queue:
                    t = self._queue.popleft()
                    self._busy += 1
                    self.cond.notify_all()  # room for a blocked submit()
                    return t
                if self._closing:
                    self._alive -= 1
                    return None
                self.cond.wait()

    def _run(self) -> None:
        conn = ImapConn(self.params)
        try:
            while True:
                t = self._next()
                if t is None:
                    return
                try:
                    if self._stopping():
                        r = AppendResult(t.item_id, False, "cancel", "cancelled before sending (pause/stop)")
                    else:
                        t0 = time.monotonic()
                        try:
                            r = self._do(conn, t)
                        except Exception as e:  # pragma: no cover - safety
                            r = AppendResult(t.item_id, False, "temp", f"{type(e).__name__}: {e}")
                        if r.ok:
                            dt = time.monotonic() - t0
                            b = tuner.size_bucket(t.size if t.size else len(t.raw or b""))
                            with self.cond:
                                self._lat_sum += dt
                                self._lat_n += 1
                                st, sn = self._lat_buckets.get(b, (0.0, 0))
                                self._lat_buckets[b] = (st + dt, sn + 1)
                    self.results.put(r)  # reported BEFORE this thread may retire
                finally:
                    with self.cond:
                        self._busy -= 1
                        self.cond.notify_all()
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
                if err.kind != "trycreate":
                    self._note_error(err)
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
        with self.cond:
            while len(self._queue) >= 2 * self._target and not self._closing:
                self.cond.wait(0.5)
            self._queue.append(t)
            self.cond.notify_all()

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
        with self.cond:
            self._closing = True
            if self._alive == 0 and self._queue:  # pragma: no cover - cannot happen (target >= 1)
                for t in list(self._queue):
                    self.results.put(AppendResult(t.item_id, False, "cancel", "cancelled before sending (pause/stop)"))
                self._queue.clear()
            self.cond.notify_all()
        for t in list(self.threads):
            t.join()


AUTO_START_CONNECTIONS = 4  # IMAP connections of an auto-tuned job before the first decision


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def initial_tuning(srv: dict, running_jobs: int = 1, probe: "tuner.MachineProbe | None" = None) -> dict:
    """Pipeline sizes of a job at its start: {mode, builders, connections, fallback, max_builders, forced}.

    - manual: `workers` IMAP connections, `builders` builder processes (NULL = machine default);
    - auto: builders from the machine probe split between the `running_jobs`, AUTO_START_CONNECTIONS
      connections (then adjusted by the AutoTuner); `max_builders` = the probe limit for one job.
    BUILD_PROCESSES, when set, forces the builder count in both modes (`forced`)."""
    forced = tuner.parse_env_builders(os.environ.get("BUILD_PROCESSES"))
    if not tuner.is_auto(srv):
        need_default = forced is None and srv.get("builders") is None  # probe only when actually needed
        m = tuner.manual_settings(srv, forced, default_builders=builders.machine_default() if need_default else 1)
        m.update(max_builders=m["builders"], forced=forced is not None)
        return m
    if probe is None:
        probe = tuner.probe_machine()
    b = tuner.choose_builders(probe, env_override=forced, running_jobs=running_jobs)
    mx = tuner.choose_builders(probe, env_override=forced, running_jobs=1)
    return {"builders": b, "connections": AUTO_START_CONNECTIONS, "mode": "auto", "fallback": False,
            "max_builders": max(b, mx), "forced": forced is not None}


def _proc_stat() -> tuple[float, float] | None:
    """(idle jiffies, total jiffies) of the machine (/proc/stat), None if unavailable."""
    try:
        with open("/proc/stat", encoding="ascii") as fh:
            parts = fh.readline().split()
    except OSError:
        return None
    if not parts or parts[0] != "cpu":
        return None
    v = [float(x) for x in parts[1:]]
    return (v[3] + (v[4] if len(v) > 4 else 0.0)), sum(v[:8])


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
        # samples (time, items done, bytes appended by this run)
        self._rate_window: list[tuple[float, int, int]] = []
        # average rate over the import phases of this run (first / last sample): displayed at the end of the job
        self._rate_first: tuple[float, int, int] | None = None
        self._rate_last: tuple[float, int, int] | None = None
        self._bytes_done = 0                     # bytes APPENDed by this run (rate_bytes)
        self._inflight_bytes: dict[int, int] = {}  # item_id -> size: built, submitted, not yet APPENDed
        self.build_procs = builders.default_processes()
        self.byte_budget = builders.byte_budget()
        # pipeline sizes (see _setup_tuning): builders = self.build_procs, IMAP connections = self.connections
        self.connections = max(1, int(self.srv["workers"] or 1)) if self.srv else 1
        self.tuning_mode = "manual"
        self.tuning_fallback = False
        self.tuner: tuner.AutoTuner | None = None
        self._builders_forced = False
        self._cpus = os.cpu_count() or 1
        self.tune_interval = max(0.05, _float_env("AUTOTUNE_INTERVAL", 5.0))  # seconds between two samples
        self._tune_t0: float | None = None
        self._tune_b0 = 0
        self._tune_cpu: tuple[float, float] | None = None
        self._phase = ""
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
        self._phase = phase
        self._item_phase = phase in ("mail", "calendar", "contacts")
        if self._item_phase and self.account_id and self.pst_key:
            # rate window seeded at the start of the phase: a short job has a rate from the 1st flush
            self.recompute_progress()
            done = sum(p["done"] for p in self.progress.values())
            self._rate_window = [(now, done, self._bytes_done)]
            if self._rate_first is None:
                self._rate_first = (now, done, self._bytes_done)

    def _avg_rate(self) -> float | None:
        if not (self._rate_first and self._rate_last):
            return None
        (t0, d0, _b0), (t1, d1, _b1) = self._rate_first, self._rate_last
        return round((d1 - d0) / (t1 - t0), 2) if t1 - t0 > 0 and d1 >= d0 else None

    def _avg_rate_bytes(self) -> float | None:
        if not (self._rate_first and self._rate_last):
            return None
        (t0, _d0, b0), (t1, _d1, b1) = self._rate_first, self._rate_last
        return round((b1 - b0) / (t1 - t0), 1) if t1 - t0 > 0 and b1 > b0 else None

    def _mail_avg_size(self) -> float | None:
        """Average size of the mails of this PST already sent (all runs): converts remaining items to bytes."""
        r = db.q1("SELECT COUNT(size) n, SUM(size) s FROM items WHERE account_id=? AND pst_key=? AND kind='mail' "
                  "AND status='ok'", (self.account_id, self.pst_key))
        return (r["s"] / r["n"]) if r and r["n"] else None

    def recompute_progress(self) -> None:
        for kind, pk in PROGRESS_KEY.items():
            rows = db.q("SELECT status, COUNT(*) n FROM items WHERE account_id=? AND pst_key=? AND kind=? GROUP BY status",
                        (self.account_id, self.pst_key, kind))
            c = {r["status"]: r["n"] for r in rows}
            self.progress[pk] = {"done": c.get("ok", 0) + c.get("skipped", 0), "total": self.totals[kind],
                                 "errors": c.get("error", 0)}

    def flush(self, force: bool = False) -> None:
        now = time.time()
        # young rate window (start of an import phase, or right after its indexing): 4 samples/s for 5 s,
        # so that short jobs show a rate; then 1/s
        young = bool(self._rate_window) and now - self._rate_window[0][0] < 5.0
        if not force and now - self._last_flush < (0.25 if young else 1.0):
            return
        self._last_flush = now
        self.recompute_progress()
        done = sum(p["done"] for p in self.progress.values())
        total = sum(p["total"] for p in self.progress.values())
        self._rate_window.append((now, done, self._bytes_done))
        # sliding average over 60 s, otherwise since the start of the phase (first sample kept)
        self._rate_window = [x for x in self._rate_window if now - x[0] <= 60]
        t0, d0, b0 = self._rate_window[0]
        measurable = now - t0 >= 0.2 and (done > d0 or now - t0 >= 5.0)  # no "0 items/s" before the 1st item
        rate = (done - d0) / (now - t0) if measurable else None
        rate_bytes = (self._bytes_done - b0) / (now - t0) if measurable else None
        if self._item_phase:
            self._rate_last = (now, done, self._bytes_done)
        eta = None
        if self._phase == "mail" and rate_bytes and rate_bytes > 0:
            # big messages lower items/s while MB/s stays steady: remaining mails converted to bytes
            # (average size of the mails already sent) divided by the byte rate
            avg = self._mail_avg_size()
            if avg:
                m = self.progress.get("mail") or {}
                rem_mail = max(0, m.get("total", 0) - m.get("done", 0))
                rem_other = max(0, total - done - rem_mail)
                eta = rem_mail * avg / rate_bytes + (rem_other / rate if rate else 0)
        if eta is None:
            eta = (max(0, total - done) / rate) if rate is not None and rate > 0 else (0 if total and done >= total else None)
        db.ex("UPDATE jobs SET progress=?, current_folder=?, rate=?, rate_bytes=?, eta=?, heartbeat=? WHERE id=?",
              (db.jdump(self.progress), self.current_folder, round(rate, 2) if rate is not None else None,
               round(rate_bytes, 1) if rate_bytes is not None else None,
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
            self._setup_tuning()
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
        db.ex("UPDATE jobs SET status=?, error=?, finished_at=?, worker_id=NULL, rate=?, rate_bytes=?, eta=NULL, "
              "report_path=COALESCE(?, report_path), current_folder=NULL WHERE id=?",
              (status, error, now, self._avg_rate(), self._avg_rate_bytes(), path, self.job_id))

    # ------------------------------------------------------------ tuning (builders / IMAP connections)
    def _setup_tuning(self) -> None:
        """Manual: the server's values. Auto: machine probe (+ ~1-2 s build benchmark), builders split between
        the running jobs, then an AutoTuner adjusts both counts during the mail phase."""
        srv = dict(self.srv)
        if tuner.is_auto(srv):
            secs = _float_env("AUTOTUNE_BENCH_SECONDS", 1.5)
            probe = tuner.probe_machine(benchmark=(lambda: tuner.default_benchmark(secs)) if secs > 0 else None)
            running = db.q1("SELECT COUNT(*) n FROM jobs WHERE status='running'")["n"] or 1
            t = initial_tuning(srv, running, probe)
            self._cpus = probe.cpus
            bench = f"{probe.bench_rate:.0f} msg/s" if probe.bench_rate else "n/a"
            self.log(f"Auto-tune: probe {probe.cpus} CPUs, {probe.mem_available_bytes / 2 ** 30:.1f} GiB free, "
                     f"bench {bench}, {running} running job(s) → {t['builders']} builders"
                     + (" (BUILD_PROCESSES)" if t["forced"] else "") + f", IMAP connections {t['connections']}")
            self.tuner = tuner.AutoTuner(builders=max(1, t["builders"]), max_builders=max(1, t["max_builders"]),
                                         connections=t["connections"], min_window_s=self.tune_interval)
            if t["forced"]:  # the builder count never moves: the tuner adjusts the connections only
                self.tuner.max_builders = self.tuner.fallback_builders = self.tuner.builders
        else:
            t = initial_tuning(srv)
            self.log(f"Tuning: manual — {t['builders']} builder(s)" + (" (BUILD_PROCESSES)" if t["forced"] else "")
                     + f", IMAP connections {t['connections']}")
        self.tuning_mode = t["mode"]
        self._builders_forced = t["forced"]
        self.build_procs = t["builders"]
        self.connections = t["connections"]
        self._save_tuning()

    def tuning_view(self) -> dict:
        return {"mode": self.tuning_mode, "builders": self.build_procs, "connections": self.connections,
                "fallback": self.tuning_fallback}

    def _save_tuning(self) -> None:
        db.ex("UPDATE jobs SET tuning=? WHERE id=?", (db.jdump(self.tuning_view()), self.job_id))

    def _cpu_headroom(self) -> float | None:
        """Idle cores since the previous call (/proc/stat), else from the load average."""
        cur = _proc_stat()
        prev, self._tune_cpu = self._tune_cpu, cur
        if cur is not None and prev is not None and cur[1] > prev[1]:
            return max(0.0, self._cpus * (cur[0] - prev[0]) / (cur[1] - prev[1]))
        try:
            return max(0.0, self._cpus - os.getloadavg()[0])
        except OSError:  # pragma: no cover
            return None

    def _tune_tick(self, pool: AppendPool) -> None:
        """Mail phase: one Sample about every `tune_interval` seconds (sooner on a transient IMAP error)
        fed to the AutoTuner, whose decision is applied at once."""
        if self.tuner is None or pool is None:
            return
        now = time.monotonic()
        bpool = getattr(self, "_bpool", None)
        if self._tune_t0 is None:
            self._tune_t0, self._tune_b0 = now, self._bytes_done
            pool.take_stats()
            if bpool is not None:
                bpool.take_utilisation()
            self._cpu_headroom()
            return
        dur = now - self._tune_t0
        if dur < self.tune_interval and not (pool._errors and dur >= min(0.5, self.tune_interval)):
            return
        lat, errs, lat_buckets = pool.take_stats_full()
        util = bpool.take_utilisation() if bpool is not None else None
        state = tuner.queue_state(pool.pending, pool.n, sum(self._inflight_bytes.values()), self.byte_budget,
                                  idle_appenders=max(0, pool.alive() - pool.busy()))
        sample = tuner.Sample(bytes_appended=self._bytes_done - self._tune_b0, duration_s=dur, queue_state=state,
                              append_buckets=lat_buckets or None, builder_util=util,
                              cpu_headroom=self._cpu_headroom(), errors=errs)
        self._tune_t0, self._tune_b0 = now, self._bytes_done
        self._apply_decision(self.tuner.feed(sample), pool)

    def _apply_decision(self, d: "tuner.Decision", pool: AppendPool) -> None:
        """Resizes the AppendPool / BuilderPool (graceful: nothing in flight is lost) and logs the decision."""
        if not d.changed and not d.log:
            return
        if d.connections != pool.n:
            pool.resize(d.connections)
        self.connections = pool.n
        bpool = getattr(self, "_bpool", None)
        if d.builders != self.build_procs and not self._builders_forced and self.build_procs > 0:
            if bpool is not None:
                bpool.resize(d.builders)
            self.build_procs = d.builders
        self.tuning_fallback = self.tuning_fallback or d.fallback
        if d.log:
            self.log(d.log, "warning" if d.fallback else "info")
        self._save_tuning()

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
        """Mail phase: all IMAP folders created up front, then every mail still to import, across all the
        folders, newest first (see ordering.py)."""
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

            pool = AppendPool(params, self.connections, create_folder, self._should_stop)
            self._tune_t0 = None
            todo = []
            for f in mail_folders:
                fk = folder_key(f.path)
                if not self._folder_has_work("mail", f):
                    continue
                if fk in self.folder_errors:
                    self.current_folder = fk
                    self._fail_folder(f, fk, "mail", self.item_rows("mail", fk, f.path), plan[fk][0],
                                      self.folder_errors[fk])
                    continue
                todo.append(f)
            targets = {self._folder_index(f): plan[folder_key(f.path)][0] for f in todo}

            def prepare(spec: ordering.FolderSpec, rows: dict) -> None:
                self.log(f"Folder '{spec.fkey}' -> {targets[spec.folder_index]} ({spec.folder.count} item(s))")
                self._resolve_on_server(conn, rows)  # before any resend: inflight items searched on the server

            # builder processes started for the indexing are kept for the builds (BUILD_PROCESSES > 0)
            entries = self._index_items("mail", todo, prepare, procs=self.build_procs,
                                        keep_pool=self.build_procs > 0)
            pst_sha = self.pst_row["pst_sha"]
            self._cid_msgs = {}
            if self.build_procs > 0 and entries:
                self._mail_parallel(entries, pool, self._pool(self.build_procs), targets, pst_sha)
            else:
                self._close_builders()  # indexing process(es) no longer needed: builds run in this process
                self._mail_inprocess(entries, pool, targets, pst_sha)
            self._wait_pool(pool)
            self._mark()
            for f in todo:
                fk = folder_key(f.path)
                n = self._cid_msgs.get(fk)
                if n:
                    self.log(f"{fk}: {n} message(s) reference images missing from the PST (quoted replies) — harmless")
                # post-folder check: messages bearing our fingerprint >= sent (ok) to this folder
                self.check_folder_status(conn, fk, targets[self._folder_index(f)])
        finally:
            self._close_builders()  # in-flight builds discarded (pause/stop/failure): nothing recorded for them
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
        self.current_folder = None
        self.flush(force=True)  # last sample of the phase (end-of-job average rates)

    def _handle_results(self, results: list[AppendResult]) -> None:
        fatal = None
        for r in results:
            size = self._inflight_bytes.pop(r.item_id, 0)
            if r.ok:
                self.consecutive_temp = 0
                self._bytes_done += r.size or size
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
        if self.consecutive_temp >= max(5, 3 * max(1, self.connections)):  # current connection count
            raise JobFailed("IMAP server unavailable (too many consecutive errors)")

    def _wait_pool(self, pool: AppendPool) -> None:
        """Waits for ALL results (no time limit: each APPEND is bounded by the socket timeout and
        the retries). Pause/stop: tasks not yet started are cancelled, APPENDs in progress run to completion."""
        while pool.pending > 0:
            if not pool.abort.is_set() and self._should_stop():
                pool.abort.set()
            self._handle_results(pool.drain(block=True, timeout=0.5))
            if not pool.abort.is_set():
                self._tune_tick(pool)
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

    def _folder_has_work(self, kind: str, f) -> bool:
        """"Retry errors" mode: folders without error/inflight/pending item are not even indexed."""
        if self.mode != "errors":
            return True
        return db.q1("SELECT 1 FROM items WHERE account_id=? AND pst_key=? AND kind=? AND folder IN (?, ?) "
                     "AND status IN ('error','inflight','pending') LIMIT 1",
                     (self.account_id, self.pst_key, kind, folder_key(f.path), legacy_folder_key(f.path))) is not None

    def _fetch(self, f, kind: str, fkey: str, idx: int, key: str, target: str):
        """Handle of item no. idx (job process, marker set: a libpff crash here skips the index on resume).
        None (item recorded as an error) if the key read differs from the indexed one."""
        self._mark(kind, fkey, idx)
        gen = self.pst.iter_items(f, idx)
        try:
            got = next(gen, None)
        finally:
            gen.close()
        if got is None or got[0] != idx or got[1] != key:
            self.upsert_item(kind, key, fkey, "error", target=target,
                             error=f"construction: item key mismatch ({got[1] if got else None} != {key})")
            return None
        return got[2]

    def _mail_inprocess(self, entries: list, pool: AppendPool, targets: dict, pst_sha: str) -> None:
        """Index order, build in this process (BUILD_PROCESSES=0)."""
        entries.reverse()  # pop() = next entry, memory released as the import progresses
        while entries:
            _sv, key, fi, idx = entries.pop()
            self.check()
            self._handle_results(pool.drain())
            self._tune_tick(pool)
            f = self.folders[fi]
            fkey = folder_key(f.path)
            self.current_folder = fkey
            handle = self._fetch(f, "mail", fkey, idx, key, targets[fi])
            if handle is None:
                continue
            self._mail_item(f, fkey, idx, key, handle, targets[fi], pool, pst_sha)
            self._drop_fallback(self._fallbacks.pop((fi, idx), None))

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
        self._record_built(fkey, idx, key, target, pool, import_id, builders.finish_built(self.eng, built, handle))

    def _record_built(self, fkey: str, idx: int, key: str, target: str, pool: AppendPool, import_id: str,
                      res: dict) -> None:
        """Bookkeeping of one build result (in-process or from a builder process), then APPEND submission."""
        st = res["status"]
        if st == "error":
            self.upsert_item("mail", key, fkey, "error", target=target, error=res["error"])
            self.log(f"Cannot build ({fkey} #{idx}): {res['error'].removeprefix('construction: ')}", "error")
            return
        if st == "skipped":
            self.upsert_item("mail", key, fkey, "skipped", target=target, error="not a mail")
            return
        warns = res.get("warnings") or []
        warnings = db.jdump(warns) if warns else None
        if st == "rejected":
            errs = res["errs"]
            self.upsert_item("mail", key, fkey, "error", target=target, subject=res["subject"],
                             error="; ".join(errs)[:2000], warnings=warnings)
            self.log(f"Mail rejected '{res['subject']}': {'; '.join(errs)}", "error")
            return
        others = [w for w in warns if not CID_WARNING_RE.match(w)]
        if len(others) != len(warns):
            cid = getattr(self, "_cid_msgs", None)
            if cid is None:
                cid = self._cid_msgs = {}
            cid[fkey] = cid.get(fkey, 0) + 1
        if others:
            self.log(f"Warning '{res['subject']}': {', '.join(others)}", "warning")
        raw = res["raw"]
        item_id = self.upsert_item("mail", key, fkey, "inflight", target=target, subject=res["subject"],
                                   hash=res["hash"], size=len(raw), warnings=warnings)
        self._submit(pool, AppendTask(item_id, target, raw, res["flags"], res["internaldate"], len(raw), import_id))

    def _submit(self, pool: AppendPool, task: AppendTask) -> None:
        """APPEND submission within the byte budget of built messages in flight (memory capped whatever the
        message sizes); the item is already `inflight` (committed) at this point."""
        while self._inflight_bytes and sum(self._inflight_bytes.values()) + task.size > self.byte_budget \
                and pool.pending > 0:
            if not pool.abort.is_set() and self._should_stop():
                pool.abort.set()
            self._handle_results(pool.drain(block=True, timeout=0.5))
            self._tune_tick(pool)
        self._inflight_bytes[task.item_id] = task.size
        pool.submit(task)

    # ------------------------------------------------------------ builder processes / indexing
    def _pool(self, n: int, what: str = "builder") -> builders.BuilderPool:
        """Builder processes (started on first use, shared by the indexing and the builds of a phase)."""
        bp = getattr(self, "_bpool", None)
        if bp is None:
            bp = builders.BuilderPool(self.pst_row["path"], max(1, n), in_child=crash.IN_CHILD)
            self._bpool = bp
            self.log(f"{bp.size} {what} process(es)")
        return bp

    def _close_builders(self) -> None:
        bp = getattr(self, "_bpool", None)
        self._bpool = None
        if bp is not None:
            bp.close()

    def _folder_index(self, f) -> int:
        for i, x in enumerate(self.folders):
            if x is f:
                return i
        raise JobFailed(f"folder not found: {f.path}")

    def _index_items(self, kind: str, folders: list, prepare=None, procs: int = 1, keep_pool: bool = False) -> list:
        """Entries ``(sort value, item_key, folder index, item index)`` of the items of `folders` still to
        import, newest first (ordering.py). Dates are read in builder processes (crash-isolated).
        `prepare(spec, rows)` runs once per folder before its items are judged (e.g. server check of
        `inflight` items). Fallback rows to drop once processed: self._fallbacks[(folder index, idx)]."""
        self._fallbacks: dict[tuple[int, int], dict] = {}
        specs = []
        for f in folders:
            fkey = folder_key(f.path)
            fkeys = {fkey, legacy_folder_key(f.path)}
            specs.append(ordering.FolderSpec(self._folder_index(f), f, fkey,
                                             {i for k, fo, i in self.skips if k == kind and fo in fkeys}))
        total = sum(int(f.count or 0) for f in folders)
        if not total:
            return []
        state: dict = {}

        def begin(spec: ordering.FolderSpec) -> None:
            rows = self.item_rows(kind, spec.fkey, spec.folder.path)
            if prepare is not None:
                prepare(spec, rows)
            state["rows"], state["fallback"] = rows, self.fallback_rows(rows)

        def end(spec: ordering.FolderSpec) -> None:
            state.clear()  # DB rows kept for one folder at a time

        def keep(spec: ordering.FolderSpec, idx: int, key: str) -> bool:
            r, fb = self._resolve_key(state["rows"], state["fallback"], idx, key)
            if not self.wants(r):
                return False
            if fb is not None:
                self._fallbacks[(spec.folder_index, idx)] = {"id": fb["id"]}
            return True

        def progress(done: int, tot: int) -> None:
            self.current_folder = f"Indexing {done:,} / {tot:,}"
            self.flush()

        def on_fetch_crash(spec: ordering.FolderSpec, idx: int, rc: int | None) -> None:
            # the mere reading of the item kills libpff: same handling as a crash during iteration
            marker = {"a": self.account_id, "p": self.pst_id, "s": self.pst_key, "kind": kind,
                      "folder": spec.fkey, "idx": idx, "key": None}
            n, desc, why = crash.record_builder_crash(self.job_id, marker, rc)
            self.crashes = n
            self.skips.add((kind, spec.fkey, idx))
            self.log(f"Subprocess crash ({why}, {n}/{settings.max_child_crashes}): {desc} — marked as error "
                     f"'{crash.CRASH_MSG}', index skipped (builder process restarted)", "error")
            if n >= settings.max_child_crashes:
                raise JobFailed(f"The subprocess crashed {n} times (probable libpff crash)")

        if not keep_pool:  # pool closed after the indexing: no more processes than chunks to read
            procs = min(procs, sum(-(-int(f.count or 0) // ordering.CHUNK) for f in folders))
        pool = self._pool(max(1, procs), "builder" if keep_pool else "indexing")
        self.current_folder = f"Indexing 0 / {total:,}"
        self.flush(force=True)
        try:
            entries, st = ordering.build_index(pool, specs, kind, keep, self.check, progress, on_fetch_crash,
                                               begin, end)
        except ordering.IndexingError as e:
            raise JobFailed(str(e)) from e
        self.current_folder = None
        self.log(f"Indexed {st.seen:,} item(s) in {st.seconds:.1f} s — {st.kept:,} to import, newest first"
                 + (f" ({st.undated:,} without date, imported last)" if st.undated else ""))
        if st.date_crashes:
            self.log(f"{st.date_crashes} item(s): reading the date crashed a builder process — no date, "
                     f"imported last", "warning")
        # rate window restarted after indexing (items/s and ETA measure the import, not the indexing)
        now = time.time()
        done = sum(p["done"] for p in self.progress.values())
        self._rate_window = [(now, done, self._bytes_done)]
        return entries

    def _mail_parallel(self, entries: list, pool: AppendPool, bpool: builders.BuilderPool, targets: dict,
                       pst_sha: str) -> None:
        """Builders build, the job process records and submits. Results are recorded and submitted in the
        index order (reorder buffer, bounded to 2 x builders results): APPENDs follow the newest-first
        order, strictly with one IMAP connection."""
        entries.reverse()  # pop() = next entry
        seq_next = seq_rec = 0
        ready: dict[int, builders.BuildEvent] = {}
        ready_bytes = 0
        retry: list[builders.BuildTask] = []  # OOM-killed builds, rebuilt once with fewer builders
        oom_retried: set[int] = set()
        while True:
            self.check()
            self._handle_results(pool.drain())
            self._tune_tick(pool)
            while retry and bpool.can_dispatch():
                bpool.dispatch(retry.pop(0))
            # reorder buffer bounded to 2 x the CURRENT builder count (resized by the auto-tuner)
            while entries and bpool.can_dispatch() and len(ready) < 2 * bpool.size and \
                    sum(self._inflight_bytes.values()) + ready_bytes < self.byte_budget:
                _sv, key, fi, idx = entries.pop()
                f = self.folders[fi]
                fb = self._fallbacks.pop((fi, idx), None)
                bpool.dispatch(builders.BuildTask(fi, list(f.path), idx, key, import_id_of(pst_sha, key),
                                                  meta=(seq_next, folder_key(f.path), targets[fi], fb)))
                seq_next += 1
            for ev in bpool.wait(0.2):
                if builders.is_oom_kill(ev) and ev.task.meta[0] not in oom_retried:
                    oom_retried.add(ev.task.meta[0])
                    self._on_oom_kill(ev.task, bpool)
                    retry.append(ev.task)
                    continue
                ready[ev.task.meta[0]] = ev
                ready_bytes += len((ev.result or {}).get("raw") or b"")
            while seq_rec in ready:
                ev = ready.pop(seq_rec)
                seq_rec += 1
                ready_bytes -= len((ev.result or {}).get("raw") or b"")
                self._on_built(ev, pool)
            if not entries and not bpool.busy() and not ready and not retry:
                return

    def _on_oom_kill(self, t: builders.BuildTask, bpool: builders.BuilderPool) -> None:
        """A builder was SIGKILLed while building `t` (the kernel OOM killer, in practice): not a libpff crash.
        One builder fewer (for the rest of the job; the auto-tuner never grows back above it), the item is
        rebuilt once."""
        old = bpool.size
        new = max(1, old - 1)
        if new < old:
            bpool.resize(new)
            self.build_procs = new
            if self.tuner is not None:
                self.tuner.builders = min(self.tuner.builders, new)
                self.tuner.max_builders = max(1, min(self.tuner.max_builders, new))
            self._save_tuning()
        self.log(f"Builder process killed by SIGKILL while building item {t.key} (folder '{t.meta[1]}', "
                 f"no. {t.index}) — probably out of memory: retrying it once with {new} builder(s)"
                 + ("" if new < old else " (already the minimum)"), "warning")

    def _on_built(self, ev: builders.BuildEvent, pool: AppendPool) -> None:
        t = ev.task
        _seq, fkey, target, fb = t.meta
        self.current_folder = fkey
        if builders.is_oom_kill(ev):  # killed again on its retry: an error for this item, not a libpff crash
            marker = {"a": self.account_id, "p": self.pst_id, "s": self.pst_key, "kind": "mail", "folder": fkey,
                      "idx": t.index, "key": t.key}
            desc = crash.record_builder_oom(self.job_id, marker)
            self.log(f"Builder process killed by SIGKILL again (probably out of memory): {desc} — marked as error "
                     f"'{crash.OOM_MSG}', continuing", "error")
            return
        if ev.crashed is not None:
            marker = {"a": self.account_id, "p": self.pst_id, "s": self.pst_key, "kind": "mail", "folder": fkey,
                      "idx": t.index, "key": t.key}
            n, desc, why = crash.record_builder_crash(self.job_id, marker, ev.crashed)
            self.crashes = n
            self.log(f"Subprocess crash ({why}, {n}/{settings.max_child_crashes}): {desc} — marked as error "
                     f"'{crash.CRASH_MSG}', resuming after this item (builder process restarted)", "error")
            if n >= settings.max_child_crashes:
                raise JobFailed(f"The subprocess crashed {n} times (probable libpff crash)")
            return
        self._record_built(fkey, t.index, t.key, target, pool, t.import_id, ev.result)
        self._drop_fallback(fb)

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
        """Calendar (start desc) / contacts (last modification desc): all the folders of the phase, newest
        first (see ordering.py); builds and PUTs in this process."""
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
            todo = [f for f in folders if self._folder_has_work(kind, f)]

            def prepare(spec: ordering.FolderSpec, rows: dict) -> None:
                self.log(f"Folder '{spec.fkey}' -> {url} ({spec.folder.count} item(s))")

            try:
                entries = self._index_items(kind, todo, prepare, procs=self.build_procs)
            finally:
                self._close_builders()
            entries.reverse()
            while entries:
                _sv, key, fi, idx = entries.pop()
                self.check()
                f = self.folders[fi]
                fkey = folder_key(f.path)
                self.current_folder = fkey
                handle = self._fetch(f, kind, fkey, idx, key, url)
                if handle is None:
                    continue
                self._dav_item(dav, url, phase, kind, fkey, idx, key, handle, builder)
                self._drop_fallback(self._fallbacks.pop((fi, idx), None))
            self._mark()
        self.current_folder = None
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
