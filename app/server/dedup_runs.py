"""Duplicate runs: analysis and removal of duplicates in mailboxes already known to MCCB (see dedup.py,
docs/duplicates.md).

The mailboxes are the migration accounts whose target is their IMAP server, and the mailboxes of the repair tab: no
password is asked again. A box is named by a key, "account:<id>" or "repair:<id>". A run works on the server of the
mailbox (the target of a migration), like the repair tab, and runs in a thread of the worker. Modes:
  scan   reads (EXAMINE, BODY.PEEK of four headers, RFC822.SIZE; PROPFIND and multiget for DAV), groups the copies
         per folder or collection, counts them and keeps a few examples. Writes nothing.
  dedup  the same reading, then in each group every item but one is deleted, PERMANENTLY:
           IMAP  \\Deleted then UID EXPUNGE of those UIDs only (UIDPLUS). Without UIDPLUS the folder is refused: a
                 plain EXPUNGE would also remove what the owner had flagged \\Deleted.
           DAV   DELETE with If-Match: the etag read just before.
         Each batch of groups is read again right before deleting: an item that changed or left, or a group whose
         kept item changed or left, is skipped. A group always keeps one item.

A run interrupted (stop, crash, restart) can be started again: it reads the mailbox again from the start.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from . import db, dedup
from .dav import DavClient, DavError
from .imap import ImapConn, ImapError, ImapParams
from .repair_runs import BACKUP_ROOT
from .security import decrypt

log = logging.getLogger("dedup")

FETCH_BATCH = 500          # headers of this many messages per UID FETCH
DELETE_BATCH = 200         # messages re-checked and deleted together
DAV_BATCH = 100            # resources per multiget
MAX_EXAMPLES_PER_FOLDER = 3
MAX_EXAMPLES = 60
MAX_ERRORS = 100
MAX_LOG = 400
ACTIVE = ("queued", "running", "stopping")
MODES = ("scan", "dedup")
JOB_ACTIVE = ("queued", "running", "paused", "stopping")
DAV_KINDS = {"calendar": ("caldav", "caldav_url"), "contacts": ("carddav", "carddav_url")}


def max_parallel() -> int:
    try:
        return max(1, int(os.environ.get("DEDUP_PARALLEL", "") or 2))
    except ValueError:
        return 2


# ------------------------------------------------------------------ mailboxes
def _sources() -> list[dict]:
    """Every mailbox MCCB knows a password for on a server, once per (server, address): a migration account first."""
    out: dict[tuple[int, str], dict] = {}
    for r in db.q("SELECT * FROM accounts WHERE target_kind IS NULL OR target_kind='' ORDER BY email, id"):
        out.setdefault((r["server_id"], r["email"].lower()), {"source": "account", "row": r})
    for r in db.q("SELECT * FROM repair_boxes ORDER BY email, id"):
        out.setdefault((r["server_id"], r["email"].lower()), {"source": "repair", "row": r})
    return sorted(out.values(), key=lambda s: (s["row"]["email"].lower(), s["source"], s["row"]["id"]))


def resolve(key: str) -> dict | None:
    """{source, row, server} of a box key, or None (unknown, or an account migrated to another kind of target)."""
    source, _, ref = (key or "").partition(":")
    if not ref.isdigit() or source not in ("account", "repair"):
        return None
    table = "accounts" if source == "account" else "repair_boxes"
    row = db.q1(f"SELECT * FROM {table} WHERE id=?", (int(ref),))
    if row is None or (source == "account" and row["target_kind"]):
        return None
    server = db.q1("SELECT * FROM servers WHERE id=?", (row["server_id"],))
    if server is None:
        return None
    return {"source": source, "row": row, "server": server, "key": f"{source}:{row['id']}"}


def busy_reason(server_id: int, email: str) -> str | None:
    """Another part of MCCB writes to this mailbox now: a migration, or a repair."""
    jm = ",".join("?" * len(JOB_ACTIVE))
    if db.q1(f"SELECT j.id FROM jobs j JOIN accounts a ON a.id=j.account_id WHERE a.server_id=? AND "
             f"lower(a.email)=lower(?) AND j.status IN ({jm})", (server_id, email, *JOB_ACTIVE)):
        return "migration"
    rm = ",".join("?" * len(ACTIVE))
    if db.q1(f"SELECT r.id FROM repair_runs r JOIN repair_boxes b ON b.id=r.box_id WHERE b.server_id=? AND "
             f"lower(b.email)=lower(?) AND r.mode IN ('repair','restore') AND r.status IN ({rm})",
             (server_id, email, *ACTIVE)):
        return "repair"
    return None


def run_view(r, full: bool = False) -> dict:
    report = db.jload(r["report"], None) or empty_report()
    out = {
        "id": r["id"], "box_key": r["box_key"], "email": r["email"], "mode": r["mode"],
        "kinds": db.jload(r["kinds"], None) or list(dedup.KINDS), "status": r["status"],
        "progress": db.jload(r["progress"], None) or empty_progress(), "totals": report.get("totals", {}),
        "error": r["error"], "created_at": r["created_at"], "started_at": r["started_at"],
        "finished_at": r["finished_at"],
    }
    if full:
        out["report"] = {k: v for k, v in report.items() if k != "totals"}
    return out


def empty_progress() -> dict:
    return {"phase": None, "folder": None, "folders_done": 0, "folders_total": 0, "seen": 0, "total": 0,
            "groups": 0, "duplicates": 0, "deleted": 0, "skipped": 0, "errors": 0}


def empty_report() -> dict:
    return {"totals": {}, "folders": [], "examples": [], "errors": [], "notes": [], "log": []}


def boxes_with_runs() -> list[dict]:
    marks = ",".join("?" * len(ACTIVE))

    def latest(where: str) -> dict[str, dict]:
        return {r["box_key"]: run_view(r) for r in db.q(
            f"SELECT * FROM dedup_runs WHERE id IN (SELECT MAX(id) FROM dedup_runs WHERE {where} GROUP BY box_key)",
            ACTIVE)}

    active = latest(f"status IN ({marks})")
    last = latest(f"status NOT IN ({marks})")
    scans = _last_scans()
    for key, run in list(last.items()):       # an analysis older than a finished removal no longer tells the truth
        if run["mode"] == "dedup" and run["status"] in ("done", "done_with_errors") and \
                key in scans and scans[key]["id"] < run["id"]:
            del scans[key]
    servers = {s["id"]: s for s in db.q("SELECT * FROM servers")}
    out = []
    for s in _sources():
        r = s["row"]
        key = f"{s['source']}:{r['id']}"
        srv = servers.get(r["server_id"])
        if srv is None:
            continue
        out.append({
            "key": key, "source": s["source"], "ref_id": r["id"], "server_id": r["server_id"], "email": r["email"],
            "login": r["login"] or "", "status": r["status"], "dav": bool(srv["dav_enabled"]),
            "usable": bool(r["password_enc"]), "unusable_reason": None if r["password_enc"] else "no_password",
            "busy": busy_reason(r["server_id"], r["email"]),
            "last_scan": scans.get(key), "last_run": last.get(key), "active_run": active.get(key),
        })
    return out


def _last_scans() -> dict[str, dict]:
    return {r["box_key"]: run_view(r) for r in db.q(
        "SELECT * FROM dedup_runs WHERE id IN (SELECT MAX(id) FROM dedup_runs WHERE mode='scan' AND "
        "status IN ('done','done_with_errors') GROUP BY box_key)")}


# ------------------------------------------------------------------ worker side
class Runs:
    """Queued runs, started in threads of the worker (network only)."""

    def __init__(self):
        self.threads: dict[int, threading.Thread] = {}

    def recover(self) -> None:
        db.ex("UPDATE dedup_runs SET status='queued' WHERE status='running'")
        db.ex("UPDATE dedup_runs SET status='stopped', finished_at=? WHERE status='stopping'", (time.time(),))

    def tick(self) -> None:
        for k, t in list(self.threads.items()):
            if not t.is_alive():
                del self.threads[k]
        if len(self.threads) >= max_parallel():
            return
        busy = {r["box_key"] for r in db.q("SELECT box_key FROM dedup_runs WHERE status='running'")}
        for r in db.q("SELECT id, box_key FROM dedup_runs WHERE status='queued' ORDER BY id"):
            if len(self.threads) >= max_parallel():
                return
            if r["box_key"] in busy:
                continue
            cur = db.ex("UPDATE dedup_runs SET status='running', started_at=COALESCE(started_at, ?), heartbeat=? "
                        "WHERE id=? AND status='queued'", (time.time(), time.time(), r["id"]))
            if cur.rowcount:
                busy.add(r["box_key"])
                t = threading.Thread(target=execute, args=(r["id"],), daemon=True, name=f"dedup-{r['id']}")
                self.threads[r["id"]] = t
                t.start()


class Stopped(Exception):
    pass


class Run:
    def __init__(self, row):
        self.id = row["id"]
        self.mode = row["mode"]
        self.write = row["mode"] == "dedup"
        self.kinds = [k for k in (db.jload(row["kinds"], None) or dedup.KINDS) if k in dedup.KINDS]
        self.progress = empty_progress()
        self.report = empty_report()
        self.report["totals"] = {k: {"items": 0, "groups": 0, "duplicates": 0, "deleted": 0, "skipped": 0}
                                 for k in self.kinds}
        self._saved = 0.0

    def save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._saved < 1.0:
            return
        self._saved = now
        db.ex("UPDATE dedup_runs SET progress=?, report=?, heartbeat=? WHERE id=?",
              (db.jdump(self.progress), db.jdump(self.report), now, self.id))

    def check_stop(self) -> None:
        r = db.q1("SELECT stop_requested FROM dedup_runs WHERE id=?", (self.id,))
        if r is None or r["stop_requested"]:
            raise Stopped()

    def log(self, msg: str, level: str = "info") -> None:
        lines = self.report["log"]
        if len(lines) >= MAX_LOG:
            del lines[:50]
        lines.append({"ts": time.time(), "level": level, "msg": msg[:500]})

    def error(self, kind: str, folder: str | None, msg: str) -> None:
        self.progress["errors"] += 1
        if len(self.report["errors"]) < MAX_ERRORS:
            self.report["errors"].append({"kind": kind, "folder": folder, "msg": msg[:500]})
        self.log(f"{folder or kind}: {msg}", "error")

    def note(self, text: str) -> None:
        if text not in self.report["notes"]:
            self.report["notes"].append(text)

    def folder(self, kind: str, name: str, items: int) -> dict:
        f = {"kind": kind, "name": name, "items": items, "groups": 0, "duplicates": 0, "deleted": 0, "skipped": 0,
             "refused": None}
        self.report["folders"].append(f)
        self.report["totals"][kind]["items"] += items
        return f

    def found(self, kind: str, f: dict, groups: list[dedup.Group]) -> None:
        n = sum(len(g.remove) for g in groups)
        f["groups"], f["duplicates"] = len(groups), n
        t = self.report["totals"][kind]
        t["groups"] += len(groups)
        t["duplicates"] += n
        self.progress["groups"] += len(groups)
        self.progress["duplicates"] += n

    def example(self, kind: str, folder: str, g: dedup.Group, label: str, detail: str, shown) -> None:
        mine = sum(1 for e in self.report["examples"] if e["kind"] == kind and e["folder"] == folder)
        if mine >= MAX_EXAMPLES_PER_FOLDER or len(self.report["examples"]) >= MAX_EXAMPLES:
            return
        self.report["examples"].append({"kind": kind, "folder": folder, "label": label[:200], "detail": detail[:200],
                                        "copies": len(g.members), "keep": shown(g.keep),
                                        "remove": [shown(i) for i in g.remove[:5]]})

    def done(self, kind: str, f: dict, deleted: int, skipped: int) -> None:
        f["deleted"] += deleted
        f["skipped"] += skipped
        t = self.report["totals"][kind]
        t["deleted"] += deleted
        t["skipped"] += skipped
        self.progress["deleted"] += deleted
        self.progress["skipped"] += skipped


def _credentials(box: dict) -> tuple[str, str]:
    r = box["row"]
    return r["login"] or r["email"], decrypt(r["password_enc"])


def execute(run_id: int) -> None:
    row = db.q1("SELECT * FROM dedup_runs WHERE id=?", (run_id,))
    if row is None:
        return
    run = Run(row)
    box = resolve(row["box_key"])
    status, error = "done", None
    try:
        if box is None:
            raise ImapError("mailbox or server deleted", "perm")
        if not box["row"]["password_enc"]:
            raise ImapError("no password known for this mailbox", "perm")
        if run.write:
            why = busy_reason(box["server"]["id"], box["row"]["email"])
            if why:
                raise ImapError(f"a {why} is writing to this mailbox: run refused", "perm")
        login, password = _credentials(box)
        run.log(f"{'Removal of duplicates' if run.write else 'Analysis'} of {box['row']['email']} on "
                f"{box['server']['name']} ({', '.join(run.kinds)})")
        if "mail" in run.kinds:
            _mail(run, box, login, password)
        for kind in ("calendar", "contacts"):
            if kind in run.kinds:
                _dav(run, box, kind, login, password)
        if run.progress["errors"]:
            status = "done_with_errors"
    except Stopped:
        status = "stopped"
        run.log("stopped", "warning")
    except (ImapError, DavError, ValueError) as e:
        status, error = "failed", str(e)
        run.log(str(e), "error")
    except Exception as e:  # never let a thread die silently
        log.exception("dedup run %s", run_id)
        status, error = "failed", f"{type(e).__name__}: {e}"
    run.progress["phase"] = run.progress["folder"] = None
    p = run.progress
    run.log(f"End: {p['groups']} group(s), {p['duplicates']} duplicate(s), {p['deleted']} deleted, "
            f"{p['skipped']} skipped ({status})")
    run.save(force=True)
    db.ex("UPDATE dedup_runs SET status=?, error=?, finished_at=? WHERE id=?", (status, error, time.time(), run_id))
    log.info("dedup run %s (%s): %s", run_id, run.mode, status)


# ------------------------------------------------------------------ mail
def params_of(box: dict, login: str, password: str) -> ImapParams:
    s = box["server"]
    return ImapParams(s["imap_host"], s["imap_port"], s["imap_security"], bool(s["verify_tls"]), login, password,
                      timeout=60)


def _mail(run: Run, box: dict, login: str, password: str) -> None:
    run.progress["phase"] = "mail"
    with ImapConn(params_of(box, login, password)) as conn:
        delim = conn.delimiter()
        folders = []
        for b in conn.list():
            if any(f.lower() in ("\\noselect", "\\nonexistent") for f in b.flags):
                continue
            if b.decoded == BACKUP_ROOT or b.decoded.startswith(BACKUP_ROOT + delim):
                continue      # the originals kept by the repair tab: its restore needs them as they are
            folders.append((b.name, b.decoded))
        run.progress["folders_total"] += len(folders)
        for raw, _ in folders:
            try:
                run.progress["total"] += conn.status_messages(raw)
            except ImapError:
                pass
        uidplus = "UIDPLUS" in conn.caps
        if run.write and not uidplus:
            run.note("no_uidplus")
            run.log("the server does not offer UIDPLUS: no message will be deleted (a plain EXPUNGE could also "
                    "remove messages the owner flagged as deleted)", "warning")
        run.save(force=True)
        for raw, name in folders:
            run.check_stop()
            run.progress["folder"] = name
            try:
                _mail_folder(run, conn, raw, name, uidplus)
            except ImapError as e:
                if e.temporary:
                    raise
                run.error("mail", name, str(e))
            run.progress["folders_done"] += 1
            run.save(force=True)


def _mail_folder(run: Run, conn: ImapConn, raw: str, name: str, uidplus: bool) -> None:
    validity = conn.uidvalidity(raw)
    uids = conn.uids(raw, readonly=True)          # the analysis reads only (EXAMINE), even before a removal
    info: dict[str, dedup.MailInfo] = {}
    for i in range(0, len(uids), FETCH_BATCH):
        run.check_stop()
        for uid, size, flags, header in conn.fetch_dedup(raw, uids[i:i + FETCH_BATCH], readonly=True):
            run.progress["seen"] += 1
            if any(f.lower() == "\\deleted" for f in flags):
                continue
            info[uid] = dedup.mail_info(size, header)
        run.save()
    groups = dedup.group([(u, [m.key], (int(u),)) for u, m in info.items() if m.key])
    f = run.folder("mail", name, len(uids))
    run.found("mail", f, groups)
    for g in groups:
        m = info[g.keep]
        run.example("mail", name, g, m.subject or "(no subject)", f"{m.date} · {m.sender}".strip(" ·"),
                    lambda u: f"UID {u}")
    if groups:
        run.log(f"{name}: {len(uids)} message(s), {len(groups)} group(s) of copies, {f['duplicates']} to delete")
    if not run.write or not groups:
        return
    if not uidplus:
        f["refused"] = "no_uidplus"
        return
    conn.ensure_selected(raw, False)
    if conn.uidvalidity(raw) != validity:
        f["refused"] = "the folder was rebuilt by the server during the analysis (UIDVALIDITY changed)"
        run.log(f"{name}: {f['refused']}", "warning")
        return
    chunk: list[dedup.Group] = []
    for g in groups:
        chunk.append(g)
        if sum(len(c.members) for c in chunk) >= DELETE_BATCH:
            _mail_delete(run, conn, raw, f, chunk, info)
            chunk = []
    if chunk:
        _mail_delete(run, conn, raw, f, chunk, info)
    run.log(f"{name}: {f['deleted']} deleted, {f['skipped']} skipped")


def _mail_delete(run: Run, conn: ImapConn, raw: str, f: dict, groups: list[dedup.Group],
                 info: dict[str, dedup.MailInfo]) -> None:
    """Reads the groups again and deletes the copies that are still copies of a kept message that is still there."""
    run.check_stop()
    now = {}
    for uid, size, flags, header in conn.fetch_dedup(raw, [u for g in groups for u in g.members], readonly=False):
        if not any(x.lower() == "\\deleted" for x in flags):
            now[uid] = dedup.mail_info(size, header).key
    doomed: list[str] = []
    skipped = 0
    for g in groups:
        key = info[g.keep].key
        if not key or now.get(g.keep) != key:
            skipped += len(g.remove)       # the message kept changed or left: the whole group stays
            continue
        for u in g.remove:
            if u != g.keep and now.get(u) == key:
                doomed.append(u)
            else:
                skipped += 1
    if doomed:
        assert not {g.keep for g in groups} & set(doomed)
        if not conn.uid_delete(raw, doomed):     # pragma: no cover: UIDPLUS was checked before
            raise ImapError("UID EXPUNGE not available", "perm")
    run.done("mail", f, len(doomed), skipped)
    run.save()


# ------------------------------------------------------------------ calendars and address books
def _dav(run: Run, box: dict, kind: str, login: str, password: str) -> None:
    srv = box["server"]
    dk, tmpl = DAV_KINDS[kind]
    if not srv["dav_enabled"]:
        run.note("dav_disabled")
        run.log(f"{kind}: DAV is disabled for this server", "warning")
        return
    run.progress["phase"] = kind
    run.progress["folder"] = None
    run.save(force=True)
    reader = dedup.event_info if kind == "calendar" else dedup.contact_info
    with DavClient(login, password, bool(srv["verify_tls"])) as dav:
        try:
            colls = dav.collections(dk, srv[tmpl] or None, box["row"]["email"], srv["imap_host"])
        except DavError as e:
            if e.status in (404,):
                run.log(f"{kind}: no collection found")
                return
            run.error(kind, None, f"{dk.upper()}: {e}")
            return
        if kind == "calendar":
            colls = [c for c in colls if not c[2] or "VEVENT" in c[2]]
        run.progress["folders_total"] += len(colls)
        for url, name, _ in colls:
            run.check_stop()
            run.progress["folder"] = name
            try:
                _collection(run, dav, kind, dk, url, name, reader)
            except DavError as e:
                run.error(kind, name, str(e))
            run.progress["folders_done"] += 1
            run.save(force=True)


def _collection(run: Run, dav: DavClient, kind: str, dk: str, url: str, name: str, reader) -> None:
    tags = dav.etags(url)
    hrefs = sorted(tags)
    run.progress["total"] += len(hrefs)
    infos: dict[str, dedup.DavInfo] = {}
    etag: dict[str, str] = {}
    for i in range(0, len(hrefs), DAV_BATCH):
        run.check_stop()
        for href, (tag, data) in dav.multiget(dk, url, hrefs[i:i + DAV_BATCH]).items():
            got = reader(data)
            etag[href] = tag or tags.get(href, "")
            if got is not None and got.keys:
                infos[href] = got
        run.progress["seen"] += len(hrefs[i:i + DAV_BATCH])
        run.save()
    groups = dedup.group([(h, d.keys, (-d.completeness, h)) for h, d in infos.items()])
    f = run.folder(kind, name, len(hrefs))
    run.found(kind, f, groups)

    def short(h: str) -> str:
        return h.rstrip("/").rsplit("/", 1)[-1]

    for g in groups:
        d = infos[g.keep]
        run.example(kind, name, g, d.label or "(no name)", d.detail, short)
    if groups:
        run.log(f"{name}: {len(hrefs)} item(s), {len(groups)} group(s) of copies, {f['duplicates']} to delete")
    if not run.write or not groups:
        return
    if any(not etag.get(h) for g in groups for h in g.members):
        f["refused"] = "no_etag"
        run.log(f"{name}: the server gives no etag, nothing deleted (a deletion could not be made conditional)",
                "warning")
        return
    for i in range(0, len(groups), DAV_BATCH // 2):
        run.check_stop()
        chunk = groups[i:i + DAV_BATCH // 2]
        fresh = dav.multiget(dk, url, [h for g in chunk for h in g.members])
        deleted = skipped = 0
        for g in chunk:
            if g.keep not in fresh or fresh[g.keep][0] != etag[g.keep]:
                skipped += len(g.remove)   # the item kept changed or left: the whole group stays
                continue
            for h in g.remove:
                if h == g.keep or h not in fresh or fresh[h][0] != etag[h]:
                    skipped += 1
                    continue
                sc = dav.delete_if_match(h, etag[h])
                if 200 <= sc < 300:
                    deleted += 1
                else:
                    skipped += 1           # 412 changed since, 404 gone already
        run.done(kind, f, deleted, skipped)
        run.save()
    run.log(f"{name}: {f['deleted']} deleted, {f['skipped']} skipped")


# ------------------------------------------------------------------ API helpers
def start(boxes: list[dict], mode: str, kinds: list[str]) -> list[dict]:
    now = time.time()
    out = []
    for b in boxes:
        cur = db.ex("INSERT INTO dedup_runs(box_key, server_id, email, mode, kinds, status, created_at) "
                    "VALUES (?,?,?,?,?,'queued',?)",
                    (b["key"], b["server"]["id"], b["row"]["email"], mode, db.jdump(kinds), now))
        out.append(run_view(db.q1("SELECT * FROM dedup_runs WHERE id=?", (cur.lastrowid,))))
    return out
