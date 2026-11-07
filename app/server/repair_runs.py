"""Repair runs: analysis, repair and restore of mailboxes already on an IMAP server (see repair.py, docs/repair.md).

A run belongs to one repair mailbox (table repair_boxes, its own list: not the migration accounts) and runs in a
thread of the worker. Modes:
  scan     reads every message (BODY.PEEK: nothing is marked read), counts the damage per folder. Writes nothing.
  repair   same reading; a message that repair.repair() fixes and verify() accepts is written next to the
           original (same folder, flags and date), the original is copied to BACKUP_ROOT/<folder>, then
           removed with UID EXPUNGE (only that message). Nothing is lost: restore brings the originals back.
  restore  every original in BACKUP_ROOT goes back to its folder and the repaired copy is removed.

Interrupted at any point (stop, crash, restart), a run can simply be started again:
  - a repaired message carries X-MCCB-Repair with the hash of its original: never repaired twice, and before an
    APPEND the folder is searched for that hash, so an APPEND that happened before a crash is not repeated;
  - the original is removed only after its copy in the backup folder succeeded.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from . import db, repair
from .imap import ImapConn, ImapError, ImapParams, utf7_encode
from .security import decrypt

log = logging.getLogger("repair")

BACKUP_ROOT = os.environ.get("REPAIR_BACKUP_FOLDER", "").strip() or "_originaux_avant_reparation"
BATCH = 25
MAX_EXAMPLES = 50
MAX_ERRORS = 100
ACTIVE = ("queued", "running", "stopping")
MODES = ("scan", "repair", "restore")


def max_parallel() -> int:
    try:
        return max(1, int(os.environ.get("REPAIR_PARALLEL", "") or 2))
    except ValueError:
        return 2


# ------------------------------------------------------------------ views
def box_view(r, last: dict | None = None, active: dict | None = None) -> dict:
    return {
        "id": r["id"], "server_id": r["server_id"], "email": r["email"], "login": r["login"] or "",
        "has_password": bool(r["password_enc"]), "status": r["status"], "last_test": r["last_test"],
        "last_test_result": db.jload(r["last_test_result"], None), "created_at": r["created_at"],
        "last_scan": last, "active_run": active,
    }


def run_view(r, full: bool = False) -> dict:
    report = db.jload(r["report"], None) or empty_report()
    out = {
        "id": r["id"], "box_id": r["box_id"], "mode": r["mode"], "status": r["status"], "folder": r["folder"],
        "progress": db.jload(r["progress"], None) or empty_progress(), "error": r["error"],
        "created_at": r["created_at"], "started_at": r["started_at"], "finished_at": r["finished_at"],
        "issues": report.get("issues", {}),
    }
    if full:
        out["report"] = report
    return out


def empty_progress() -> dict:
    return {"total": 0, "seen": 0, "affected": 0, "repaired": 0, "restored": 0, "skipped": 0, "errors": 0,
            "folder": None, "folders_done": 0, "folders_total": 0}


def empty_report() -> dict:
    return {"issues": {}, "folders": {}, "examples": [], "errors": [], "notes": []}


def boxes_with_runs() -> list[dict]:
    """Every repair mailbox with its run in progress (if any) and its last finished run."""
    rows = db.q("SELECT * FROM repair_boxes ORDER BY email, id")
    marks = ",".join("?" * len(ACTIVE))
    active = {r["box_id"]: run_view(r) for r in db.q(
        f"SELECT * FROM repair_runs WHERE id IN (SELECT MAX(id) FROM repair_runs WHERE status IN ({marks}) "
        "GROUP BY box_id)", ACTIVE)}
    last = {r["box_id"]: run_view(r) for r in db.q(
        f"SELECT * FROM repair_runs WHERE id IN (SELECT MAX(id) FROM repair_runs WHERE status NOT IN ({marks}) "
        "GROUP BY box_id)", ACTIVE)}
    return [box_view(r, last.get(r["id"]), active.get(r["id"])) for r in rows]


# ------------------------------------------------------------------ connection
def params_of(box, server) -> ImapParams:
    return ImapParams(server["imap_host"], server["imap_port"], server["imap_security"], bool(server["verify_tls"]),
                      box["login"] or box["email"], decrypt(box["password_enc"]), timeout=60)


def test_box(box_id: int) -> dict:
    box = db.q1("SELECT * FROM repair_boxes WHERE id=?", (box_id,))
    server = db.q1("SELECT * FROM servers WHERE id=?", (box["server_id"],))
    status = "ok"
    try:
        with ImapConn(params_of(box, server)) as c:
            quota = c.quota()
            res = {"imap": {"ok": True, "msg": "IMAP connection successful", "quota": quota,
                            "uidplus": "UIDPLUS" in c.caps}}
    except ValueError as e:      # the password cannot be decrypted (SECRET_KEY changed)
        res = {"imap": {"ok": False, "msg": str(e), "quota": None}}
        status = "error"
    except ImapError as e:
        res = {"imap": {"ok": False, "msg": str(e), "quota": None}}
        status = "auth_failed" if e.kind == "auth" else "error"
    db.ex("UPDATE repair_boxes SET status=?, last_test=?, last_test_result=? WHERE id=?",
          (status, time.time(), db.jdump(res), box_id))
    return res


# ------------------------------------------------------------------ worker side
class Runs:
    """Queued runs, started in threads of the worker (network only, no native code)."""

    def __init__(self):
        self.threads: dict[int, threading.Thread] = {}

    def recover(self) -> None:
        """At worker start: what was running died with the previous worker. A run is safe to start again."""
        db.ex("UPDATE repair_runs SET status='queued' WHERE status='running'")
        db.ex("UPDATE repair_runs SET status='stopped', finished_at=? WHERE status='stopping'", (time.time(),))

    def tick(self) -> None:
        for k, t in list(self.threads.items()):
            if not t.is_alive():
                del self.threads[k]
        if len(self.threads) >= max_parallel():
            return
        busy = {r["box_id"] for r in db.q("SELECT box_id FROM repair_runs WHERE status='running'")}
        for r in db.q("SELECT id, box_id FROM repair_runs WHERE status='queued' ORDER BY id"):
            if len(self.threads) >= max_parallel():
                return
            if r["box_id"] in busy:
                continue
            cur = db.ex("UPDATE repair_runs SET status='running', started_at=COALESCE(started_at, ?), heartbeat=? "
                        "WHERE id=? AND status='queued'", (time.time(), time.time(), r["id"]))
            if cur.rowcount:
                busy.add(r["box_id"])
                t = threading.Thread(target=execute, args=(r["id"],), daemon=True, name=f"repair-{r['id']}")
                self.threads[r["id"]] = t
                t.start()


class Stopped(Exception):
    pass


class Run:
    def __init__(self, row):
        self.id = row["id"]
        self.mode = row["mode"]
        self.only = (row["folder"] or "").strip()
        # a run started again (after a stop or a restart) counts from zero: what it did already is skipped
        # thanks to the mark of the repaired messages
        self.progress = empty_progress()
        self.report = empty_report()
        self._saved = 0.0

    # -- state
    def save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._saved < 1.0:
            return
        self._saved = now
        db.ex("UPDATE repair_runs SET progress=?, report=?, heartbeat=? WHERE id=?",
              (db.jdump(self.progress), db.jdump(self.report), now, self.id))

    def check_stop(self) -> None:
        r = db.q1("SELECT stop_requested FROM repair_runs WHERE id=?", (self.id,))
        if r is None or r["stop_requested"]:
            raise Stopped()

    def error(self, folder: str, uid: str | None, msg: str) -> None:
        self.progress["errors"] += 1
        if len(self.report["errors"]) < MAX_ERRORS:
            self.report["errors"].append({"folder": folder, "uid": uid, "msg": msg[:500]})

    def note(self, text: str) -> None:
        if text not in self.report["notes"]:
            self.report["notes"].append(text)

    def count(self, folder: str, uid: str, analysis: repair.Analysis, fixed: list[str] | None = None) -> None:
        f = self.report["folders"].setdefault(folder, {"seen": 0, "affected": 0, "repaired": 0, "issues": {}})
        if analysis.issues:
            f["affected"] += 1
            self.progress["affected"] += 1
            for i in analysis.issues:
                f["issues"][i] = f["issues"].get(i, 0) + 1
                self.report["issues"][i] = self.report["issues"].get(i, 0) + 1
            if len(self.report["examples"]) < MAX_EXAMPLES:
                self.report["examples"].append({"folder": folder, "uid": uid, "subject": analysis.subject[:200],
                                                "date": analysis.date, "issues": analysis.issues,
                                                "fixed": fixed})
        if fixed:
            f["repaired"] += 1


def execute(run_id: int) -> None:
    row = db.q1("SELECT * FROM repair_runs WHERE id=?", (run_id,))
    if row is None:
        return
    run = Run(row)
    box = db.q1("SELECT * FROM repair_boxes WHERE id=?", (row["box_id"],))
    server = db.q1("SELECT * FROM servers WHERE id=?", (box["server_id"],)) if box else None
    status, error = "done", None
    try:
        if box is None or server is None:
            raise ImapError("mailbox or server deleted", "perm")
        with ImapConn(params_of(box, server)) as conn:
            if run.mode == "restore":
                _restore(run, conn)
            else:
                _scan(run, conn, write=run.mode == "repair")
            if run.mode != "scan" and "UIDPLUS" not in conn.caps:
                run.note("no_uidplus")
        if run.progress["errors"]:
            status = "done_with_errors"
    except Stopped:
        status = "stopped"
    except (ImapError, ValueError) as e:
        status, error = "failed", str(e)
    except Exception as e:  # never let a thread die silently
        log.exception("repair run %s", run_id)
        status, error = "failed", f"{type(e).__name__}: {e}"
    run.progress["folder"] = None
    run.save(force=True)
    db.ex("UPDATE repair_runs SET status=?, error=?, finished_at=? WHERE id=?", (status, error, time.time(), run_id))
    log.info("repair run %s (%s): %s", run_id, run.mode, status)


def _folders(conn: ImapConn, only: str) -> tuple[list[tuple[str, str]], str]:
    delim = conn.delimiter()
    out = []
    for b in conn.list():
        if any(f.lower() in ("\\noselect", "\\nonexistent") for f in b.flags):
            continue
        name = b.decoded
        if name == BACKUP_ROOT or name.startswith(BACKUP_ROOT + delim):
            continue
        if only and not (name == only or name.startswith(only + delim)):
            continue
        out.append((b.name, name))
    return out, delim


def _backup_name(raw_folder: str, delim: str) -> str:
    return utf7_encode(BACKUP_ROOT) + delim + raw_folder


def _scan(run: Run, conn: ImapConn, write: bool) -> None:
    folders, delim = _folders(conn, run.only)
    run.progress["folders_total"] = len(folders)
    total = 0
    for raw_name, _ in folders:
        try:
            total += conn.status_messages(raw_name)
        except ImapError:
            pass
    run.progress["total"] = total
    run.save(force=True)
    created: set[str] = set()
    for raw_name, name in folders:
        run.check_stop()
        run.progress["folder"] = name
        run.report["folders"].setdefault(name, {"seen": 0, "affected": 0, "repaired": 0, "issues": {}})
        try:
            uids = conn.uids(raw_name, readonly=not write)
        except ImapError as e:
            run.error(name, None, f"folder not readable: {e}")
            continue
        for i in range(0, len(uids), BATCH):
            run.check_stop()
            batch = uids[i:i + BATCH]
            try:
                messages = conn.fetch_messages(raw_name, batch, readonly=not write)
            except ImapError as e:
                if e.temporary:
                    raise
                run.error(name, ",".join(batch), f"fetch failed: {e}")
                continue
            done: list[str] = []
            for uid, flags, when, raw in messages:
                run.progress["seen"] += 1
                run.report["folders"][name]["seen"] += 1
                a = repair.analyze(raw)
                if a.already:
                    run.progress["skipped"] += 1
                    continue
                if not write or not a.issues:
                    run.count(name, uid, a)
                    continue
                try:
                    fixed = repair.repair(raw)
                except repair.RepairCheckError as e:
                    run.count(name, uid, a)
                    run.error(name, uid, f"not repaired, check failed: {e}")
                    continue
                except Exception as e:
                    run.count(name, uid, a)
                    run.error(name, uid, f"not repaired: {type(e).__name__}: {e}")
                    continue
                if fixed is None:
                    run.count(name, uid, a)
                    continue
                sha = repair.orig_hash(raw)
                if not conn.search_header(raw_name, repair.MARK, f"orig={sha}"):
                    conn.append(raw_name, fixed.raw, flags, when)
                run.count(name, uid, a, fixed.fixes)
                done.append(uid)
                run.save()
            if done:
                backup = _backup_name(raw_name, delim)
                if backup not in created:
                    conn.create(backup)
                    created.add(backup)
                conn.uid_copy(raw_name, done, backup)        # the originals are safe first
                conn.uid_delete(raw_name, done)              # then only these leave the folder
                run.progress["repaired"] += len(done)
            run.save()
        run.progress["folders_done"] += 1
        run.save(force=True)


def _restore(run: Run, conn: ImapConn) -> None:
    delim = conn.delimiter()
    root = BACKUP_ROOT + delim
    folders = [(b.name, b.decoded) for b in conn.list()
               if b.decoded.startswith(root) and not any(f.lower() == "\\noselect" for f in b.flags)]
    if run.only:
        folders = [(r, n) for r, n in folders if n[len(root):] == run.only or n[len(root):].startswith(run.only + delim)]
    run.progress["folders_total"] = len(folders)
    run.progress["total"] = sum(conn.status_messages(r) for r, _ in folders)
    run.save(force=True)
    raw_root = utf7_encode(BACKUP_ROOT) + delim
    for braw, bname in folders:
        run.check_stop()
        target_raw, target = braw[len(raw_root):], bname[len(root):]
        run.progress["folder"] = target
        f = run.report["folders"].setdefault(target, {"seen": 0, "affected": 0, "repaired": 0, "issues": {}})
        conn.create(target_raw)                                 # nothing when it exists
        uids = conn.uids(braw, readonly=False)
        for i in range(0, len(uids), BATCH):
            run.check_stop()
            batch = uids[i:i + BATCH]
            messages = conn.fetch_messages(braw, batch, readonly=False)
            done: list[str] = []
            for uid, flags, when, raw in messages:
                run.progress["seen"] += 1
                f["seen"] += 1
                try:
                    conn.ensure_selected(target_raw, False)
                    sha = repair.orig_hash(raw)
                    copies = conn.search_header(target_raw, repair.MARK, f"orig={sha}")
                    if copies or not _already_back(conn, target_raw, raw, sha):
                        conn.append(target_raw, raw, flags, when)
                    if copies:
                        conn.uid_delete(target_raw, copies)
                    done.append(uid)
                    f["repaired"] += 1
                except ImapError as e:
                    if e.temporary:
                        raise
                    run.error(target, uid, f"not restored: {e}")
                run.save()
            if done:
                conn.uid_delete(braw, done)
                run.progress["restored"] += len(done)
            run.save()
        run.progress["folders_done"] += 1
        run.save(force=True)


def _already_back(conn: ImapConn, folder: str, raw: bytes, sha: str) -> bool:
    """After a crash between the APPEND of an original and the end of its restore: is it in the folder already?"""
    try:
        mid = str(repair._parse(raw).get("Message-ID") or "").strip()
    except Exception:
        mid = ""
    if not mid:
        return False
    for uid in conn.search_header(folder, "Message-ID", mid)[:20]:
        other = conn.fetch_raw(folder, uid)
        if other is not None and repair.orig_hash(other) == sha:
            return True
    return False


# ------------------------------------------------------------------ API helpers
def start(box_ids: list[int], mode: str, folder: str | None) -> list[dict]:
    now = time.time()
    out = []
    for bid in box_ids:
        cur = db.ex("INSERT INTO repair_runs(box_id, mode, status, folder, created_at) VALUES (?,?,'queued',?,?)",
                    (bid, mode, (folder or "").strip() or None, now))
        out.append(run_view(db.q1("SELECT * FROM repair_runs WHERE id=?", (cur.lastrowid,))))
    return out


def preview(box_id: int, folder: str, uid: str) -> dict[str, Any]:
    """The message as it is and as a repair would write it. Reads only (EXAMINE, BODY.PEEK)."""
    box = db.q1("SELECT * FROM repair_boxes WHERE id=?", (box_id,))
    server = db.q1("SELECT * FROM servers WHERE id=?", (box["server_id"],))
    with ImapConn(params_of(box, server)) as conn:
        raw_name = next((b.name for b in conn.list() if b.decoded == folder), None)
        if raw_name is None:
            raise ImapError(f"folder {folder!r} not found", "perm")
        got = conn.fetch_messages(raw_name, [uid], readonly=True)
    if not got:
        raise ImapError("message not found (repaired or moved since the analysis?)", "perm")
    raw = got[0][3]
    a = repair.analyze(raw)
    out: dict[str, Any] = {"issues": a.issues, "already": a.already, "before": repair.summary(raw),
                           "after": None, "fixes": [], "left": [], "error": None}
    try:
        fixed = repair.repair(raw)
    except repair.RepairCheckError as e:
        out["error"] = f"check failed: {e}"
        return out
    if fixed is not None:
        out.update(after=repair.summary(fixed.raw), fixes=fixed.fixes, left=fixed.left)
    return out
