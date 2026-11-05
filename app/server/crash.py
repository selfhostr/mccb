"""Native crash (libpff) isolation: "current item" marker, crash counter, test hook.

The subprocess (child.py) records in `kv` the item it is about to read with libpff
(`job:<id>:current`). If the process dies from a signal, the parent worker reads this marker: the faulty item
is marked `error` "libpff crash" (never an `inflight` item, resolved on resume by UID SEARCH), the job is
re-queued and resumes after it. Beyond MAX_CHILD_CRASHES crashes of the same job, it goes to `failed`.
"""
from __future__ import annotations

import logging
import os
import signal
import time

from . import db, repo
from .config import settings

CRASH_MSG = "libpff crash"
VALIDATE_CRASH_MSG = "libpff crash during analysis"
TEST_CRASH_ENV = "PSTMIG_TEST_CRASH"
TEST_OOM_ENV = "PSTMIG_TEST_OOM"  # builder SIGKILLed on these item keys ("key" always, "key@/flag" once)
OOM_MSG = "builder process killed (SIGKILL, probably out of memory)"
TEST_DELAY_ENV = "PSTMIG_TEST_ITEM_DELAY"  # seconds of pause before each item (e2e tests: pause/stop/kill)
PARENT_PID_ENV = "PSTMIG_PARENT_PID"
IN_CHILD = False  # set by child.main()

log = logging.getLogger("crash")


# ------------------------------------------------------------------ subprocess side
def maybe_crash(*tokens: str) -> None:
    """Test hook: PSTMIG_TEST_CRASH = comma-separated list among
    "validate" (PST analysis), "open" (PST opening), "*" (every item), "<item_key>",
    "fetch:<index>" (reading item number index of a folder). Sends SIGSEGV to the subprocess.
    No effect outside the subprocess or if the variable is absent."""
    if not IN_CHILD:
        return
    spec = os.environ.get(TEST_CRASH_ENV, "")
    if not spec:
        return
    wanted = {s.strip() for s in spec.split(",") if s.strip()}
    if any(t in wanted for t in tokens if t):
        log.warning("%s : simulated SIGSEGV (%s)", TEST_CRASH_ENV, tokens)
        os.kill(os.getpid(), signal.SIGSEGV)
        time.sleep(5)  # pragma: no cover


def maybe_oom(key: str) -> None:
    """Test hook: PSTMIG_TEST_OOM = comma-separated "<item_key>" (SIGKILL every time) or
    "<item_key>@<flag path>" (SIGKILL only while the flag file does not exist; created first), like the
    kernel OOM killer would. No effect outside the subprocess or if the variable is absent."""
    if not IN_CHILD:
        return
    for spec in (s.strip() for s in os.environ.get(TEST_OOM_ENV, "").split(",")):
        k, _, flag = spec.partition("@")
        if not spec or k != key:
            continue
        if flag:
            if os.path.exists(flag):
                continue
            with open(flag, "w"):
                pass
        log.warning("%s : simulated OOM kill (%s)", TEST_OOM_ENV, key)
        os.kill(os.getpid(), signal.SIGKILL)
        time.sleep(5)  # pragma: no cover


def maybe_delay() -> None:
    """Test hook: PSTMIG_TEST_ITEM_DELAY = delay (s) before reading each item, to give end-to-end
    tests time to pause / stop / kill the worker during a real import.
    No effect outside the subprocess or if the variable is absent."""
    if not IN_CHILD:
        return
    try:
        d = float(os.environ.get(TEST_DELAY_ENV, "") or 0)
    except ValueError:
        return
    if d > 0:
        time.sleep(min(d, 10.0))


def die_with_parent() -> None:
    """Linux: the subprocess receives SIGKILL if the parent worker dies (no orphan that keeps running)."""
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:  # pragma: no cover - non-Linux
        return
    ppid = os.environ.get(PARENT_PID_ENV)
    if ppid and ppid.isdigit() and os.getppid() != int(ppid):
        os._exit(1)  # parent already dead before prctl


def _mkey(job_id: int) -> str:
    return f"job:{job_id}:current"


def set_marker(job_id: int, marker: dict | None) -> None:
    if marker is None:
        db.ex("DELETE FROM kv WHERE key=?", (_mkey(job_id),))
    else:
        db.kv_set(_mkey(job_id), db.jdump(marker))


def get_marker(job_id: int) -> dict | None:
    return db.jload(db.kv_get(_mkey(job_id)), None)


def get_crashes(job_id: int) -> int:
    try:
        return int(db.kv_get(f"job:{job_id}:crashes") or 0)
    except ValueError:
        return 0


def reset_crashes(job_id: int) -> None:
    db.ex("DELETE FROM kv WHERE key IN (?, ?)", (f"job:{job_id}:crashes", _mkey(job_id)))


def get_skips(pst_key: str) -> set[tuple[str, str, int]]:
    """Indexes of items whose mere reading (iter_items) crashes libpff: skipped during iteration.
    Attached to the PST fingerprint (`pst_key`, like the items table), not to its id."""
    return {(k, f, int(i)) for k, f, i in db.jload(db.kv_get(f"pst:{pst_key}:crash_skips"), [])}


def _add_skip(pst_key: str, kind: str, folder: str, idx: int) -> None:
    s = get_skips(pst_key)
    s.add((kind, folder, int(idx)))
    db.kv_set(f"pst:{pst_key}:crash_skips", db.jdump(sorted(s)))


def describe_rc(rc: int | None) -> str:
    if rc is None:
        return "unknown termination"
    if rc < 0:
        try:
            return f"signal {signal.Signals(-rc).name}"
        except ValueError:
            return f"signal {-rc}"
    return f"exit code {rc}"


# ------------------------------------------------------------------ parent worker side
def _blame(marker: dict, job_id: int, why: str, msg: str = CRASH_MSG) -> str | None:
    """Mark the item designated by the marker as an error `msg` ("libpff crash"). Returns its description."""
    kind, folder, aid, pid = marker.get("kind"), marker.get("folder") or "", marker.get("a"), marker.get("p")
    if not (kind and aid and pid):
        return None
    pkey = marker.get("s") or db.pst_key_of(db.q1("SELECT id, pst_sha FROM psts WHERE id=?", (pid,))) or f"id:{pid}"
    key = marker.get("key")
    now = time.time()
    if key:
        desc = f"item {key} (folder '{folder}', no. {marker.get('idx')})"
    else:
        # crash while reading item no. idx: no known key -> index skipped during iteration
        key = f"{folder}#crash{marker.get('idx')}"
        _add_skip(pkey, kind, folder, int(marker.get("idx") or 0))
        desc = f"reading of item no. {marker.get('idx')} (folder '{folder}')"
    err = f"{msg} ({why}) — item skipped" if msg == CRASH_MSG else f"{msg} — item skipped"
    # never an item already `ok` or `inflight` (possibly APPENDed: checked on resume by _resolve_inflight)
    db.ex("INSERT INTO items(account_id,pst_id,pst_key,kind,item_key,folder,status,error,job_id,updated_at) "
          "VALUES (?,?,?,?,?,?,'error',?,?,?) "
          "ON CONFLICT(account_id,pst_key,kind,item_key) DO UPDATE SET status='error', error=excluded.error, "
          "job_id=excluded.job_id, updated_at=excluded.updated_at WHERE items.status NOT IN ('ok','inflight')",
          (aid, pid, pkey, kind, key, folder, err, job_id, now))
    return desc


def record_builder_crash(job_id: int, marker: dict, rc: int | None) -> tuple[int, str | None, str]:
    """A builder process (see builders.py) died holding the item described by `marker`: the item becomes
    an error "libpff crash" (like a crash of the job process itself) and the job's crash counter, shared
    with handle_job_crash (MAX_CHILD_CRASHES guard), is incremented. Returns (count, description, why)."""
    why = describe_rc(rc)
    desc = _blame(marker, job_id, why)
    n = get_crashes(job_id) + 1
    db.kv_set(f"job:{job_id}:crashes", str(n))
    return n, desc, why


def record_builder_oom(job_id: int, marker: dict) -> str | None:
    """A builder was SIGKILLed (OOM killer) twice building the item of `marker`: the item becomes an error
    OOM_MSG - not a "libpff crash", and the job's crash counter is left alone. Returns its description."""
    return _blame(marker, job_id, "SIGKILL", OOM_MSG)


def handle_job_crash(job_id: int, rc: int | None) -> str:
    """The job's subprocess died abnormally. Returns the new job status."""
    why = describe_rc(rc)
    job = db.q1("SELECT status FROM jobs WHERE id=?", (job_id,))
    if job is None:
        return "deleted"
    st = job["status"]
    if st not in ("running", "stopping", "paused"):
        # job already finished (crash on process shutdown): nothing to resume
        set_marker(job_id, None)
        repo.log(job_id, f"Subprocess terminated abnormally ({why}) after the job ended", "warning")
        return st
    marker = get_marker(job_id) or {}
    desc = _blame(marker, job_id, why)
    set_marker(job_id, None)
    n = get_crashes(job_id) + 1
    db.kv_set(f"job:{job_id}:crashes", str(n))
    where = desc or f"outside any item (phase {marker.get('phase') or 'unknown'})"
    repo.log(job_id, f"Subprocess crash ({why}, {n}/{settings.max_child_crashes}): {where}"
             + (f" — marked as error '{CRASH_MSG}', resuming after this item" if desc else ""), "error")
    now = time.time()
    if st == "stopping":
        db.ex("UPDATE jobs SET status='stopped', worker_id=NULL, finished_at=?, rate=NULL, eta=NULL, "
              "current_folder=NULL WHERE id=? AND status='stopping'", (now, job_id))
        reset_crashes(job_id)
        return "stopped"
    if st == "paused":
        db.ex("UPDATE jobs SET worker_id=NULL, rate=NULL, eta=NULL WHERE id=?", (job_id,))
        return "paused"
    if n >= settings.max_child_crashes:
        msg = f"The subprocess crashed {n} times (probable libpff crash)"
        db.ex("UPDATE jobs SET status='failed', error=?, worker_id=NULL, finished_at=?, rate=NULL, eta=NULL, "
              "current_folder=NULL WHERE id=? AND status='running'", (msg, now, job_id))
        repo.log(job_id, f"Failure: {msg}", "error")
        reset_crashes(job_id)
        return "failed"
    db.ex("UPDATE jobs SET status='queued', worker_id=NULL, rate=NULL, eta=NULL WHERE id=? AND status='running'",
          (job_id,))
    return "queued"


def handle_validate_crash(pst_id: int, rc: int | None) -> None:
    db.ex("UPDATE psts SET status='invalid', error=?, updated_at=? WHERE id=? AND status='validating'",
          (f"{VALIDATE_CRASH_MSG} ({describe_rc(rc)}): PST file probably corrupted", time.time(), pst_id))
