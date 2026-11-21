"""Size of the mail of an account at its source (accounts.source_size_*) and bytes already imported (items.size).

The source size is known as early as possible: computed in the background when a source is set (one computation at
a time, in the web process), on request, and refreshed for free at each job from its listing when the source gives the
size of every item. Unknown stays NULL (shown "—"), never 0. The source is only read (see Source.mailbox_size)."""
from __future__ import annotations

import logging
import queue
import threading
import time

from ..connectors import registry, wiring
from ..connectors.base import ConnectorError, MailboxSize, Source
from . import db
from .config import settings

log = logging.getLogger("mailbox_size")
BACKGROUND_BUDGET = 600.0  # seconds for one computation in the background (then the quota estimate, or nothing)
PROBE_BUDGET = 3.0         # seconds added at most to a connection test
PENDING_STALE = 1800.0     # a computation started longer ago is no longer shown as running (process restarted)

_queue: "queue.Queue[tuple[int, str]]" = queue.Queue()  # (account id, database it belongs to)
_thread: threading.Thread | None = None
_lock = threading.Lock()


def _namespace(r) -> str | None:
    try:
        return wiring.items_key_of_account(r)
    except Exception:  # noqa: BLE001 - an unreadable source (connector gone): nothing measured for it
        return None


def view(r, imported: int | None) -> dict:
    """{bytes, count, at, approx, basis, computing, measurable, imported}: bytes/count null while unknown. A value
    measured on another source than the current one is not shown. basis: how an estimate was made ('quota': usage
    of the account, every folder; 'file': size of the uploaded file), null when exact. measurable: it can be
    measured before a job (POST /api/accounts/{id}/size)."""
    has = "source_size_bytes" in r.keys()
    pending = r["source_size_pending"] if has else None
    out = {"bytes": None, "count": None, "at": None, "approx": False, "basis": None,
           "computing": pending is not None and time.time() - pending < PENDING_STALE,
           "measurable": _measurable_kind(r), "imported": imported}
    ns = _namespace(r)
    if has and r["source_size_bytes"] is not None and ns is not None and r["source_size_key"] == ns:
        approx = bool(r["source_size_approx"])
        out.update(bytes=r["source_size_bytes"], count=r["source_size_count"], at=r["source_size_at"], approx=approx,
                   basis="quota" if approx else None)
    elif wiring.source_kind(r) in (wiring.PST, wiring.FILES) and ns is not None:
        pid = r["pst_id"] if wiring.source_kind(r) == wiring.PST else wiring.source_config(r).get("pst_id")
        f = db.q1("SELECT size FROM psts WHERE id=? AND status='valid'", (pid,)) if pid else None
        if f is not None and f["size"]:  # until a job lists it: the file (calendars, contacts and overhead included)
            out.update(bytes=int(f["size"]), approx=True, basis="file")
    return out


def imported_bytes(acc) -> int:
    """Bytes of the mails of the current source already written to the target (one indexed SUM)."""
    key = _namespace(acc)
    if key is None:
        return 0
    r = db.q1("SELECT SUM(size) s FROM items WHERE kind='mail' AND status='ok' AND account_id=? AND pst_key=?",
              (acc["id"], key))
    return int(r["s"] or 0)


def imported_by_account(rows) -> dict[int, int]:
    """{account id: imported bytes} for many accounts in ONE GROUP BY (items_bytes index), current source only."""
    wanted = {r["id"]: _namespace(r) for r in rows}
    out = {aid: 0 for aid in wanted}
    for g in db.q("SELECT account_id, pst_key, SUM(size) s FROM items WHERE kind='mail' AND status='ok' "
                  "GROUP BY account_id, pst_key"):
        if g["account_id"] in wanted and wanted[g["account_id"]] == g["pst_key"]:
            out[g["account_id"]] = int(g["s"] or 0)
    return out


def measurable(kind: str) -> bool:
    """True: the connector answers Source.mailbox_size (the others: their job's listing gives the size)."""
    try:
        cls = registry.role_of(kind, "source").factory
    except ConnectorError:
        return False
    return isinstance(cls, type) and getattr(cls, "mailbox_size", None) is not Source.mailbox_size


def _measurable_kind(acc) -> bool:
    kind = wiring.source_kind(acc)
    return kind not in (wiring.PST, wiring.FILES) and measurable(kind)


def sizable(acc) -> bool:
    """True: the source of the account can be measured before a job."""
    return _measurable_kind(acc) and wiring.source_problem(acc) is None


def measure(kind: str, config: dict, secrets: dict, budget: float) -> MailboxSize | None:
    """Opens the source, lists its folders, asks its size. ConnectorError raised as is."""
    deadline = time.monotonic() + budget
    with registry.make_source(kind, config, secrets) as src:
        folders = src.folders()
        return src.mailbox_size(folders, deadline)


def store(aid: int, size: MailboxSize, namespace: str) -> None:
    """Records a result measured on the source `namespace` (items.pst_key): shown while the account reads it."""
    db.ex("UPDATE accounts SET source_size_bytes=?, source_size_count=?, source_size_at=?, source_size_approx=?, "
          "source_size_key=?, source_size_pending=NULL WHERE id=?",
          (int(size.bytes), size.count, time.time(), int(size.approx), namespace, aid))


def compute(aid: int, budget: float = BACKGROUND_BUDGET) -> None:
    acc = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
    if acc is None:
        return
    if not sizable(acc):
        db.ex("UPDATE accounts SET source_size_pending=NULL WHERE id=?", (aid,))
        return
    namespace = _namespace(acc)
    try:
        size = measure(wiring.source_kind(acc), wiring.source_config(acc), wiring.source_secrets(acc), budget)
    except (ConnectorError, OSError, ValueError) as e:
        log.info("size of the source of mailbox %s not computed: %s", aid, e)
        size = None
    except Exception:  # noqa: BLE001 - never kills the thread
        log.exception("size of the source of mailbox %s", aid)
        size = None
    if size is None or namespace is None:  # unknown: a value read earlier (a job's listing) stays
        db.ex("UPDATE accounts SET source_size_pending=NULL WHERE id=?", (aid,))
        return
    store(aid, size, namespace)


def _loop() -> None:
    while True:
        aid, path = _queue.get()
        try:
            if path == str(settings.db_path):  # tests: another database since
                compute(aid)
        except Exception:  # noqa: BLE001
            log.exception("size of mailbox %s", aid)
        finally:
            _queue.task_done()


def start(aid: int) -> bool:
    """Queues the computation (background thread, one at a time). False: nothing to compute for this source."""
    global _thread
    acc = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
    if acc is None or not sizable(acc):
        return False
    db.ex("UPDATE accounts SET source_size_pending=? WHERE id=?", (time.time(), aid))
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="mailbox-size", daemon=True)
            _thread.start()
    _queue.put((aid, str(settings.db_path)))
    return True


def wait_idle() -> None:
    """Tests: until every queued computation ended."""
    _queue.join()


def reset_pending() -> None:
    """Web process start: computations of a previous process will not end."""
    db.ex("UPDATE accounts SET source_size_pending=NULL WHERE source_size_pending IS NOT NULL")
