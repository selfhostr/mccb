"""Planning tools that run next to the jobs: dry run, source-versus-target comparison, allowed hours, queue order,
batches. The API is in api_planning.py; the worker calls `Jobs.tick` and `window_tick` every second.

Nothing here writes to a source or to a target: a dry run connects, lists and counts; a comparison counts and
looks up. See docs/planning.md.
"""
from __future__ import annotations

import csv
import io
import logging
import threading
import time
from types import SimpleNamespace

from ..connectors import wiring
from ..connectors.base import ConnectorError
from . import db, foldermap, policy as pol, repo
from .imap import utf7_decode

log = logging.getLogger("planning")

ACTIVE = ("queued", "running", "paused", "stopping")
TERMINAL = ("stopped", "done", "done_with_errors", "failed")
PHASES = ("mail", "calendar", "contacts")
ITEM_KIND = {"mail": "mail", "calendar": "event", "contacts": "contact"}   # phase -> items.kind
PHASE_OF_ITEM = {v: k for k, v in ITEM_KIND.items()}
MAX_PARALLEL = 2          # plans and comparisons running at once in the worker
DETAIL_MAX = 500          # mails looked up one by one by a detailed comparison
PST_BYTES_SHARE = 0.8     # a mail takes about this much of the PST file once rebuilt (same estimate as the job)


# ============================================================================ helpers
def forget_cursors(account_id: int) -> None:
    """Delta cursors of an account: to call whenever its items are forgotten (new target)."""
    db.ex("DELETE FROM kv WHERE key LIKE ?", (f"delta:{int(account_id)}:%",))


def _shown(name: str) -> str:
    try:
        return utf7_decode(name) if "&" in name else name
    except Exception:  # noqa: BLE001
        return name


def _row_counts(account_id: int, key: str) -> dict[tuple[str, str], dict[str, int]]:
    """{(item kind, folder key): {status: count}} for the items already known of this source."""
    out: dict[tuple[str, str], dict[str, int]] = {}
    for r in db.q("SELECT kind, folder, status, COUNT(*) n FROM items WHERE account_id=? AND pst_key=? "
                  "GROUP BY kind, folder, status", (account_id, key)):
        out.setdefault((r["kind"], r["folder"]), {})[r["status"]] = r["n"]
    return out


# ============================================================================ dry run
def _folder_entry(f, phase: str) -> dict:
    return {"path": list(f.path), "kind": phase, "special": getattr(f, "special", None), "items": int(f.count or 0),
            "bytes": None, "bytes_estimated": False, "done": 0, "to_migrate": int(f.count or 0),
            "to_migrate_bytes": None, "filtered": {}, "oversized": 0, "oversized_bytes": 0, "excluded": False,
            "target": None, "create": None}


def _scan_listing(src, f, entry: dict, acc, key: str, phase: str, policy: pol.Policy, limit: int | None) -> None:
    """Lists a folder of a live source and judges every item, as the job would."""
    from .runner import folder_key, legacy_folder_key
    kind = ITEM_KIND[phase]
    done = {r["item_key"] for r in db.conn().execute(
        "SELECT item_key FROM items WHERE account_id=? AND pst_key=? AND kind=? AND folder IN (?, ?) "
        "AND status IN ('ok','skipped')", (acc["id"], key, kind, folder_key(f.path), legacy_folder_key(f.path)))}
    n = nbytes = todo = todo_bytes = over = over_bytes = 0
    unknown = False
    filtered: dict[str, int] = {}
    for it in src.iter_items(f):
        n += 1
        if it.size is None:
            unknown = True
        nbytes += it.size or 0
        if it.key in done:
            continue
        why = policy.reason(it.date, it.size if phase == "mail" else None) if phase != "contacts" else None
        if why:
            filtered[why] = filtered.get(why, 0) + 1
            continue
        todo += 1
        todo_bytes += it.size or 0
        if limit and phase == "mail" and (it.size or 0) > limit:
            over += 1
            over_bytes += it.size or 0
    entry.update(items=n, bytes=None if unknown and not nbytes else nbytes, bytes_estimated=unknown,
                 done=len(done), to_migrate=todo, to_migrate_bytes=None if unknown and not todo_bytes else todo_bytes,
                 filtered=filtered, oversized=over, oversized_bytes=over_bytes)


def plan_account(account_id: int) -> dict:
    """Dry run of one account. Never raises for a problem of the account: it is the answer."""
    from .runner import KIND_OF_PHASE, folder_key
    acc = db.q1("SELECT * FROM accounts WHERE id=?", (account_id,))
    out: dict = {"account_id": account_id, "email": acc["email"] if acc else None, "status": "ok", "source": None,
                 "targets": {}, "kinds": {}, "folders": [], "quota": None, "target_limit": None, "warnings": [],
                 "notes": [], "excluded_folders": []}
    if acc is None:
        out.update(status="error", error="unknown mailbox")
        return out
    srv = db.q1("SELECT * FROM servers WHERE id=?", (acc["server_id"],))
    policy = pol.effective(acc, srv)
    warnings: list[str] = out["warnings"]
    problem = wiring.source_problem(acc)
    if problem:
        out.update(status="error", source={"kind": wiring.source_kind(acc), "ok": False, "msg": problem})
        return out
    key = wiring.items_key_of_account(acc)
    src_kind = wiring.source_kind(acc)
    src = None
    pst_bytes = None
    try:
        # ---------------- source: folders (and, for a live source, their items below)
        if src_kind == wiring.PST:
            row = db.q1("SELECT * FROM psts WHERE id=?", (acc["pst_id"],))
            scan = db.jload(row["scan"], {}) or {}
            folders = [SimpleNamespace(path=list(f["path"]), kind=f["kind"], special=f.get("special"),
                                       count=int(f.get("count") or 0), ref=None)
                       for f in scan.get("folders", []) if f.get("kind") in PHASES]
            pst_bytes = int(row["size"] or 0)
            out["source"] = {"kind": "pst", "ok": True, "msg": f"{row['filename']}: {len(folders)} folder(s) analysed"}
        else:
            src = wiring.source_of_account(acc)
            src.open()
            folders = [f for f in src.folders() if f.kind in PHASES]
            out["source"] = {"kind": src_kind, "ok": True, "msg": f"{len(folders)} folder(s) found"}
    except ConnectorError as e:
        out.update(status="error", source={"kind": src_kind, "ok": False, "error_kind": e.kind, "msg": str(e)})
        return out
    except Exception as e:  # noqa: BLE001 - a plan reports, it does not fail
        out.update(status="error", source={"kind": src_kind, "ok": False, "msg": f"{type(e).__name__}: {e}"})
        return out
    try:
        entries: dict[str, tuple] = {}   # folder key -> (source folder, entry)
        for f in folders:
            phase = f.kind
            if not acc[f"opt_{phase}"]:
                continue
            e = _folder_entry(f, phase)
            if policy.excluded(list(f.path)):
                e.update(excluded=True, to_migrate=0)
                out["excluded_folders"].append("/".join(f.path))
            entries[phase + "\0" + folder_key(f.path)] = (f, e)
        # ---------------- targets: connection, quota, folders to create, size limit
        try:
            targets = wiring.targets_of_account(acc, srv)
        except ConnectorError as e:
            targets = {}
            out["status"] = "error"
            out["targets"]["all"] = {"ok": False, "error_kind": e.kind, "msg": str(e)}
        for phase in PHASES:
            mine = [(f, e) for f, e in entries.values() if e["kind"] == phase and not e["excluded"]]
            if not mine:
                continue
            if phase not in targets:
                out["targets"][phase] = {"ok": False, "msg": f"no target for {phase}: {len(mine)} folder(s) not migrated"}
                warnings.append(f"no target for {phase} items")
                for _f, e in mine:
                    e["to_migrate"] = 0
                continue
            session = targets[phase].session()
            try:
                session.connect()
                info = session.info()
                quota = session.quota()
                res = {"ok": True, "msg": "connection successful"}
                if quota:
                    res["quota"] = quota
                    if phase == "mail":
                        out["quota"] = quota
                if info.get("append_limit") and phase == "mail":
                    out["target_limit"] = int(info["append_limit"])
                    res["append_limit"] = out["target_limit"]
                srcs = [f for f, _e in mine]
                if phase == "mail":
                    plan = foldermap.plan_mapped(session.plan_folders, srcs, policy.mapper())
                else:
                    plan = session.plan_folders(srcs)
                for f, e in mine:
                    fp = plan.get(folder_key(f.path))
                    if fp is not None:
                        e["target"] = _shown(fp.name)
                        has = session.has_folder(fp.name)
                        e["create"] = None if has is None else not has
                out["targets"][phase] = res
            except ConnectorError as ce:
                out["targets"][phase] = {"ok": False, "error_kind": ce.kind, "msg": str(ce)}
                out["status"] = "error"
            except Exception as ex:  # noqa: BLE001
                out["targets"][phase] = {"ok": False, "msg": f"{type(ex).__name__}: {ex}"}
                out["status"] = "error"
            finally:
                session.close()
        # ---------------- items
        limit = out["target_limit"]
        counts = _row_counts(account_id, key) if key else {}
        total_items = sum(e["items"] for _f, e in entries.values()) or 1
        for _k, (f, e) in entries.items():
            if e["excluded"]:
                continue
            phase = e["kind"]
            if src is not None:
                try:
                    _scan_listing(src, f, e, acc, key, phase, policy, limit)
                except ConnectorError as ce:
                    e["error"] = str(ce)
                    out["status"] = "error"
            else:  # a PST: counts of the analysis, sizes estimated from the file, filters judged during the run
                c = counts.get((ITEM_KIND[phase], folder_key(f.path)), {})
                done = c.get("ok", 0) + c.get("skipped", 0)
                e["done"], e["to_migrate"] = done, max(0, e["items"] - done)
                if phase == "mail" and pst_bytes:
                    e["bytes"] = int(pst_bytes * PST_BYTES_SHARE * e["items"] / total_items)
                    e["bytes_estimated"] = True
                    e["to_migrate_bytes"] = int(e["bytes"] * e["to_migrate"] / e["items"]) if e["items"] else 0
        # ---------------- totals per kind
        for phase in PHASES:
            mine = [e for _f, e in entries.values() if e["kind"] == phase]
            if not mine:
                continue
            live = [e for e in mine if not e["excluded"]]
            k = {"folders": len(mine), "excluded_folders": len(mine) - len(live),
                 "items": sum(e["items"] for e in mine),
                 "excluded_items": sum(e["items"] for e in mine if e["excluded"]),
                 "done": sum(e["done"] for e in live), "to_migrate": sum(e["to_migrate"] for e in live),
                 "bytes": _sum_bytes(live, "bytes"), "to_migrate_bytes": _sum_bytes(live, "to_migrate_bytes"),
                 "bytes_estimated": any(e["bytes_estimated"] for e in live),
                 "filtered": {}, "oversized": sum(e["oversized"] for e in live),
                 "oversized_bytes": sum(e["oversized_bytes"] for e in live),
                 "folders_to_create": sum(1 for e in live if e["create"])}
            for e in live:
                for why, n in e["filtered"].items():
                    k["filtered"][why] = k["filtered"].get(why, 0) + n
            out["kinds"][phase] = k
        out["folders"] = [e for _f, e in entries.values()]
    finally:
        if src is not None:
            src.close()
    # ---------------- warnings
    mail = out["kinds"].get("mail")
    if out["quota"] and out["quota"].get("limit") and mail and mail["to_migrate_bytes"] is not None:
        free = out["quota"]["limit"] - (out["quota"].get("used") or 0)
        out["quota"].update(free=free, need=mail["to_migrate_bytes"], enough=mail["to_migrate_bytes"] <= free)
        if mail["to_migrate_bytes"] > free:
            warnings.append(f"quota: {free // 2 ** 20} MB free on the target for about "
                            f"{mail['to_migrate_bytes'] // 2 ** 20} MB to migrate")
    if mail and mail["oversized"]:
        warnings.append(f"{mail['oversized']} message(s) ({mail['oversized_bytes'] // 2 ** 20} MB) are over the limit of "
                        f"the target ({(out['target_limit'] or 0) // 2 ** 20} MB) and will be refused")
    notes: list[str] = out["notes"]  # things to know that are not problems
    if src is None and policy.has_filters:
        notes.append("date and size filters of a PST are judged item by item during the run: the counts do not "
                     "include them")
    if pst_bytes:
        notes.append("sizes of a PST are estimated from the file size")
    if out["excluded_folders"]:
        notes.append(f"{len(out['excluded_folders'])} folder(s) excluded by the policy")
    if out["status"] == "ok" and warnings:
        out["status"] = "warning"
    return out


def _sum_bytes(entries: list[dict], field: str) -> int | None:
    vals = [e[field] for e in entries if e[field] is not None]
    return sum(vals) if vals else None


def plan_totals(accounts: list[dict]) -> dict:
    t = {"accounts": len(accounts), "ok": 0, "warning": 0, "error": 0, "to_migrate": 0, "to_migrate_bytes": 0,
         "oversized": 0, "filtered": 0}
    for a in accounts:
        t[a.get("status", "error")] = t.get(a.get("status", "error"), 0) + 1
        for k in (a.get("kinds") or {}).values():
            t["to_migrate"] += k["to_migrate"]
            t["to_migrate_bytes"] += k["to_migrate_bytes"] or 0
            t["oversized"] += k["oversized"]
            t["filtered"] += sum(k["filtered"].values())
    return t


def plan_view(r, full: bool = True) -> dict:
    """A plan for the API. `full=False` (events): accounts without their folder lists."""
    res = db.jload(r["result"], None)
    if res is not None and not full:
        res = {**res, "accounts": [{k: v for k, v in a.items() if k != "folders"} for a in res.get("accounts", [])]}
    return {"id": r["id"], "status": r["status"], "created_at": r["created_at"], "updated_at": r["updated_at"],
            "account_ids": (db.jload(r["request"], {}) or {}).get("account_ids", []), "error": r["error"],
            "result": res}


def run_plan(plan_id: int) -> None:
    r = db.q1("SELECT * FROM plans WHERE id=?", (plan_id,))
    ids = (db.jload(r["request"], {}) or {}).get("account_ids", [])
    result: dict = {"accounts": [], "totals": plan_totals([]), "done": 0, "total": len(ids)}
    try:
        for aid in ids:
            try:
                a = plan_account(aid)
            except Exception as e:  # noqa: BLE001 - one account must not lose the others
                log.exception("plan %s account %s", plan_id, aid)
                a = {"account_id": aid, "status": "error", "error": f"{type(e).__name__}: {e}"}
            result["accounts"].append(a)
            result["done"] += 1
            result["totals"] = plan_totals(result["accounts"])
            db.ex("UPDATE plans SET result=?, updated_at=? WHERE id=?", (db.jdump(result), time.time(), plan_id))
        db.ex("UPDATE plans SET status='done', result=?, updated_at=? WHERE id=?",
              (db.jdump(result), time.time(), plan_id))
    except Exception as e:  # noqa: BLE001
        log.exception("plan %s", plan_id)
        db.ex("UPDATE plans SET status='failed', error=?, updated_at=? WHERE id=?",
              (f"{type(e).__name__}: {e}", time.time(), plan_id))


# ============================================================================ comparison
def compare_job(job_id: int, detail: bool = False) -> dict:
    """Source versus target, per target folder: what the job meant to put there (`expected`), what is there
    (`on_target`, only the items carrying this migration's marker) and what is `missing`."""
    job = db.q1("SELECT * FROM jobs WHERE id=?", (job_id,))
    acc = db.q1("SELECT * FROM accounts WHERE id=?", (job["account_id"],)) if job else None
    if acc is None:
        raise ConnectorError("mailbox deleted", ConnectorError.CONFIG)
    srv = db.q1("SELECT * FROM servers WHERE id=?", (acc["server_id"],))
    key = wiring.items_key_of_account(acc)
    res: dict = {"job_id": job_id, "account_id": acc["id"], "namespace": key, "generated_at": time.time(),
                 "detail": bool(detail), "folders": [], "ok": True,
                 "totals": {"expected": 0, "on_target": 0, "missing": 0, "sent": 0, "errors": 0, "pending": 0,
                            "filtered": 0, "skipped": 0}}
    if not key:
        return res
    groups: dict[tuple, dict] = {}
    for r in db.q("SELECT kind, folder, target, status, COUNT(*) n FROM items WHERE account_id=? AND pst_key=? "
                  "GROUP BY kind, folder, target, status", (acc["id"], key)):
        gk = (r["kind"], r["target"]) if r["target"] else (r["kind"], None, r["folder"])
        g = groups.setdefault(gk, {"kind": PHASE_OF_ITEM.get(r["kind"], r["kind"]), "item_kind": r["kind"],
                                   "target": _shown(r["target"]) if r["target"] else None, "raw_target": r["target"],
                                   "folders": [], "sent": 0, "errors": 0, "pending": 0, "filtered": 0, "skipped": 0})
        if r["folder"] not in g["folders"]:
            g["folders"].append(r["folder"])
        st = r["status"]
        g["sent" if st == "ok" else "errors" if st == "error" else "pending" if st in ("pending", "inflight")
          else st if st in ("filtered", "skipped") else "pending"] += r["n"]
    try:
        targets = wiring.targets_of_account(acc, srv)
    except ConnectorError as e:
        targets = {}
        res["error"] = str(e)
    sessions: dict[str, object] = {}
    try:
        for gk in sorted(groups, key=lambda k: (str(k[0]), str(k[1]))):
            g = groups[gk]
            g["expected"] = g["sent"] + g["errors"] + g["pending"]
            g["on_target"] = None
            g["missing"] = 0
            g["extra"] = 0
            phase = g["kind"]
            if g["raw_target"] and g["expected"]:
                try:
                    sess = sessions.get(phase)
                    if sess is None:
                        if phase not in targets:
                            raise ConnectorError(f"no target for {phase}", ConnectorError.CONFIG)
                        sess = sessions[phase] = targets[phase].session()
                        sess.connect()
                    names = None
                    if phase != "mail":
                        names = {r["hash"] for r in db.q(
                            "SELECT hash FROM items WHERE account_id=? AND pst_key=? AND kind=? AND target=? AND "
                            "status='ok' AND hash IS NOT NULL", (acc["id"], key, g["item_kind"], g["raw_target"]))}
                    g["on_target"] = sess.count_ours(g["raw_target"], key, names)
                    g["missing"] = max(0, g["expected"] - g["on_target"])
                    g["extra"] = max(0, g["on_target"] - g["expected"])
                    if detail and phase == "mail" and g["sent"] > g["on_target"]:
                        g["missing_items"] = _lookup_missing(sess, acc["id"], key, g["raw_target"])
                except ConnectorError as e:
                    g["error"] = str(e)
                    res["ok"] = False
            for k in ("expected", "sent", "errors", "pending", "filtered", "skipped"):
                res["totals"][k] += g[k]
            res["totals"]["on_target"] += g["on_target"] or 0
            res["totals"]["missing"] += g["missing"]
            if g["missing"]:
                res["ok"] = False
            g.pop("raw_target")
            g.pop("item_kind")
            res["folders"].append(g)
    finally:
        for s in sessions.values():
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
    return res


def _lookup_missing(sess, account_id: int, key: str, target: str) -> list[dict]:
    """Mails recorded as sent to this folder that the target does not answer for, looked up one by one."""
    from ..connectors.base import OutgoingItem
    from .runner import import_id_of
    out = []
    cur = db.conn().execute("SELECT item_key, subject FROM items WHERE account_id=? AND pst_key=? AND kind='mail' "
                            "AND target=? AND status='ok' ORDER BY id LIMIT ?", (account_id, key, target, DETAIL_MAX))
    for r in cur.fetchall():
        try:
            found = sess.find(target, OutgoingItem("mail", b"", import_id_of(key, r["item_key"])))
        except ConnectorError:
            found = [None]  # cannot tell: not reported as missing
        if not found:
            out.append({"item_key": r["item_key"], "subject": r["subject"]})
    return out


def compare_csv(result: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["kind", "source_folders", "target_folder", "expected", "on_target", "missing", "sent", "errors",
                "pending", "filtered", "skipped", "note"])
    for g in result.get("folders", []):
        note = g.get("error") or ("; ".join(f"{m['item_key']} {m['subject'] or ''}".strip()
                                            for m in g.get("missing_items", [])[:20]))
        w.writerow([g["kind"], " | ".join(g["folders"]), g["target"] or "", g["expected"],
                    "" if g["on_target"] is None else g["on_target"], g["missing"], g["sent"], g["errors"],
                    g["pending"], g["filtered"], g["skipped"], note])
    t = result.get("totals", {})
    w.writerow(["total", "", "", t.get("expected", 0), t.get("on_target", 0), t.get("missing", 0), t.get("sent", 0),
                t.get("errors", 0), t.get("pending", 0), t.get("filtered", 0), t.get("skipped", 0), ""])
    return buf.getvalue()


def compare_view(r) -> dict:
    return {"id": r["id"], "job_id": r["job_id"], "status": r["status"], "detail": bool(r["detail"]),
            "created_at": r["created_at"], "updated_at": r["updated_at"], "error": r["error"],
            "result": db.jload(r["result"], None)}


def run_compare(cid: int) -> None:
    r = db.q1("SELECT * FROM compares WHERE id=?", (cid,))
    try:
        res = compare_job(r["job_id"], bool(r["detail"]))
        db.ex("UPDATE compares SET status='done', result=?, updated_at=? WHERE id=?",
              (db.jdump(res), time.time(), cid))
    except Exception as e:  # noqa: BLE001
        log.exception("compare %s", cid)
        db.ex("UPDATE compares SET status='failed', error=?, updated_at=? WHERE id=?",
              (f"{type(e).__name__}: {e}", time.time(), cid))


# ============================================================================ worker side
class Jobs:
    """Plans and comparisons queued by the API, run in threads of the worker (network only, no native code)."""

    def __init__(self):
        self.threads: dict[tuple[str, int], threading.Thread] = {}

    def recover(self) -> None:
        """At worker start: what was running died with the previous worker."""
        for table in ("plans", "compares"):
            db.ex(f"UPDATE {table} SET status='failed', error='worker restarted', updated_at=? WHERE status='running'",
                  (time.time(),))

    def tick(self) -> None:
        for k, t in list(self.threads.items()):
            if not t.is_alive():
                del self.threads[k]
        for table, fn in (("plans", run_plan), ("compares", run_compare)):
            for r in db.q(f"SELECT id FROM {table} WHERE status='queued' ORDER BY id LIMIT {MAX_PARALLEL}"):
                if len(self.threads) >= MAX_PARALLEL:
                    return
                cur = db.ex(f"UPDATE {table} SET status='running', updated_at=? WHERE id=? AND status='queued'",
                            (time.time(), r["id"]))
                if cur.rowcount:
                    t = threading.Thread(target=fn, args=(r["id"],), daemon=True, name=f"{table}-{r['id']}")
                    self.threads[(table, r["id"])] = t
                    t.start()


def window_tick() -> None:
    """Allowed hours of the servers: queued jobs wait outside them (paused, reason `window`), and the jobs that
    waited start again when they open. A job already running pauses itself (JobRunner.check). The administrator's
    own pauses (no reason) are never touched."""
    rows = db.q("SELECT j.id, j.status, s.policy FROM jobs j JOIN accounts a ON a.id=j.account_id "
                "JOIN servers s ON s.id=a.server_id WHERE s.policy IS NOT NULL AND (j.status='queued' OR "
                "(j.status='paused' AND j.pause_reason='window' AND j.worker_id IS NULL))")
    cache: dict[str, dict | None] = {}
    for r in rows:
        if r["policy"] not in cache:
            cache[r["policy"]] = (db.jload(r["policy"], {}) or {}).get("window") or None
        w = cache[r["policy"]]
        opened = pol.window_open(w)
        if r["status"] == "queued" and not opened:
            if db.ex("UPDATE jobs SET status='paused', pause_reason='window' WHERE id=? AND status='queued'",
                     (r["id"],)).rowcount:
                nxt = pol.next_open(w)
                repo.log(r["id"], "Waiting for the allowed hours of the server (" + pol.describe_window(w) + ")"
                         + (", next opening " + time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt)) if nxt else ""),
                         "warning")
        elif r["status"] == "paused" and opened:
            if db.ex("UPDATE jobs SET status='queued', pause_reason=NULL WHERE id=? AND status='paused' AND "
                     "pause_reason='window' AND worker_id IS NULL", (r["id"],)).rowcount:
                repo.log(r["id"], "Allowed hours open: the job starts again")
    # window removed from the server while jobs were waiting for it
    for r in db.q("SELECT j.id, s.policy FROM jobs j JOIN accounts a ON a.id=j.account_id "
                  "JOIN servers s ON s.id=a.server_id WHERE j.status='paused' AND j.pause_reason='window' "
                  "AND j.worker_id IS NULL AND s.policy IS NULL"):
        db.ex("UPDATE jobs SET status='queued', pause_reason=NULL WHERE id=? AND status='paused' AND "
              "pause_reason='window'", (r["id"],))
        repo.log(r["id"], "No allowed hours any more: the job starts again")


# ============================================================================ queue order
def reorder(job_ids: list[int]) -> list[int]:
    """The queued jobs of `job_ids` run first, in that order; the other queued jobs keep their relative order after
    them. Returns the whole queue in its new order. The worker takes the highest priority first, then the oldest."""
    with db.tx() as c:
        queued = [r["id"] for r in c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY priority DESC, id")]
        known = set(queued)
        want = [j for j in dict.fromkeys(job_ids) if j in known]
        order = want + [j for j in queued if j not in set(want)]
        for i, j in enumerate(order):
            c.execute("UPDATE jobs SET priority=? WHERE id=? AND status='queued'", (len(order) - i, j))
    return order


def set_priority(job_id: int, priority: int) -> None:
    db.ex("UPDATE jobs SET priority=? WHERE id=?", (int(priority), job_id))


# ============================================================================ batches
def batch_view(b, jobs: list | None = None, with_jobs: bool = False) -> dict:
    """A named group of jobs started together, with totals over their progress."""
    if jobs is None:
        jobs = db.q("SELECT * FROM jobs WHERE batch_id=? ORDER BY id", (b["id"],))
    by_status: dict[str, int] = {}
    items = {k: {"done": 0, "total": 0, "errors": 0} for k in PHASES}
    for j in jobs:
        by_status[j["status"]] = by_status.get(j["status"], 0) + 1
        prog = db.jload(j["progress"], None) or {}
        for k in PHASES:
            p = prog.get(k) or {}
            for f in ("done", "total", "errors"):
                items[k][f] += int(p.get(f) or 0)
    active = sum(n for s, n in by_status.items() if s in ACTIVE)
    if not jobs:
        state = "empty"
    elif not active:
        state = "finished"
    elif by_status.get("running") or by_status.get("stopping"):
        state = "running"
    elif by_status.get("queued"):
        state = "queued"
    else:
        state = "paused"
    out = {"id": b["id"], "name": b["name"], "created_at": b["created_at"], "state": state, "jobs": len(jobs),
           "by_status": by_status, "items": items,
           "finished_at": max((j["finished_at"] or 0) for j in jobs) if jobs and not active else None,
           "failed": by_status.get("failed", 0), "with_errors": by_status.get("done_with_errors", 0)}
    if with_jobs:
        out["job_list"] = [repo.job_view(j) for j in jobs]
    return out


def list_batches(limit: int = 200) -> list[dict]:
    rows = db.q("SELECT * FROM batches ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),))
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    jobs: dict[int, list] = {i: [] for i in ids}
    for j in db.q(f"SELECT * FROM jobs WHERE batch_id IN ({','.join('?' * len(ids))}) ORDER BY id", ids):
        jobs[j["batch_id"]].append(j)
    return [batch_view(b, jobs[b["id"]]) for b in rows]


def batch_action(bid: int, action: str) -> dict:
    """pause / resume / stop / retry-failed on every job of a batch that allows it. Jobs that do not are left
    alone and counted in `skipped`."""
    jobs = db.q("SELECT * FROM jobs WHERE batch_id=? ORDER BY id", (bid,))
    changed = skipped = 0
    now = time.time()
    for j in jobs:
        jid, st = j["id"], j["status"]
        ok = False
        if action == "pause" and st in ("queued", "running"):
            ok = bool(db.ex("UPDATE jobs SET status='paused', pause_reason=NULL WHERE id=? AND status IN "
                            "('queued','running')", (jid,)).rowcount)
            if ok:
                repo.log(jid, "Pause requested (batch)")
        elif action == "resume" and st == "paused":
            if j["worker_id"]:
                db.ex("UPDATE jobs SET status='running', pause_reason=NULL WHERE id=? AND status='paused' AND "
                      "worker_id IS NOT NULL", (jid,))
            ok = bool(db.ex("UPDATE jobs SET status='queued', pause_reason=NULL WHERE id=? AND status='paused'",
                            (jid,)).rowcount) or bool(j["worker_id"])
            if ok:
                repo.log(jid, "Resume requested (batch)")
        elif action == "stop" and st in ("queued", "paused", "running", "stopping"):
            if st in ("queued", "paused") and not j["worker_id"]:
                db.ex("UPDATE jobs SET status='stopped', finished_at=? WHERE id=?", (now, jid))
                ok = True
            elif st in ("running", "paused"):
                db.ex("UPDATE jobs SET status='stopping' WHERE id=?", (jid,))
                ok = True
            if ok:
                repo.log(jid, "Stop requested (batch)")
        elif action == "retry-failed" and st in ("failed", "done_with_errors"):
            busy = db.q1("SELECT id FROM jobs WHERE account_id=? AND id<>? AND status IN "
                         "('queued','running','paused','stopping')", (j["account_id"], jid))
            if not busy:
                from . import crash
                mode = "errors" if st == "done_with_errors" else "full"
                db.ex("UPDATE jobs SET status='queued', mode=?, error=NULL, finished_at=NULL, phase='precheck', "
                      "pause_reason=NULL WHERE id=?", (mode, jid))
                crash.reset_crashes(jid)
                repo.log(jid, "Rerun (batch): errors only" if mode == "errors" else "Rerun (batch)")
                ok = True
        changed += ok
        skipped += not ok
    out = batch_view(db.q1("SELECT * FROM batches WHERE id=?", (bid,)))
    out.update(changed=changed, skipped=skipped)
    return out
