"""Job of an account with a connector source or target: same bookkeeping as JobRunner (items table, pause, stop,
resume, report, tuning), with the reading and writing delegated to app/connectors.

Every kind follows one path: list the source, resolve what a previous run left `inflight`, then fetch in this
process (newest first) and write through a resizable pool of target sessions. Nothing is written without its row
committed as `inflight` first, and after a temporary error the pool asks the target before writing again.
"""
from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass
from datetime import timezone
from urllib.parse import unquote

from ..connectors import registry, wiring
from ..connectors import davfix
from ..connectors.base import (CAP_DELETE, CAP_INCREMENTAL, CAP_MOVE, CAP_PARALLEL, CAP_READBACK, CAP_SET_FLAGS,
                               CAP_UPDATE, CALENDAR, CONTACTS, DELETE_HARD, MAIL, NOT_SENT, TASKS, ConnectorError,
                               MailboxSize, OutgoingItem, Target, TargetSession, stamp_import_id)
from . import crash, db, foldermap, mailbox_size, ordering
from . import mirror as mm
from . import policy as pol
from .config import settings
from .imap import flags_to_add
from .runner import (KIND_OF_PHASE, AppendPool, AppendResult, AppendTask, JobFailed, JobRunner, PauseRequested,
                     StopRequested, folder_key, import_id_of, legacy_folder_key, norm_hash)
from .tuner import ErrorNote, classify_error

MAX_SOURCE_ERRORS = 5  # items in a row whose fetch fails after its retries: the source is down, not the items
DAV_EXT = {"calendar": "ics", "contacts": "vcf", TASKS: "ics"}
# tasks only exist on this route: the historical path, its progress and its reports know three kinds
CARRIED = {**KIND_OF_PHASE, TASKS: "task"}
PHASE_OF_KIND = {kind: phase for phase, kind in CARRIED.items()}
COLLECTIONS = {CALENDAR: "calendars", CONTACTS: "address books", TASKS: "task lists"}
SAME_UID = "same_uid: "  # first words of the warning of an item whose UID another item of its collection has


def _pin_key(account_id: int, pst_key: str, phase: str) -> str:
    return f"dav-collections:{account_id}:{pst_key}:{phase}"


def _ukey(url: str) -> str:
    return unquote(url if url.endswith("/") else url + "/")


def collection_history(account_id: int, pst_key: str, phase: str) -> dict:
    """For a DAV target's plan (DavSession.history): the collections the items of earlier runs went to, and the
    one each folder was given by the last run (kept while it is there: a plan never goes back and forth)."""
    rows = db.q("SELECT DISTINCT target FROM items WHERE account_id=? AND pst_key=? AND kind=? AND status='ok' "
                "AND target IS NOT NULL AND target<>''", (account_id, pst_key, CARRIED[phase]))
    pinned = db.jload(db.kv_get(_pin_key(account_id, pst_key, phase)), {}) or {}
    return {"previous": {_ukey(r["target"]) for r in rows}, "pinned": pinned if isinstance(pinned, dict) else {}}


def pin_collections(account_id: int, pst_key: str, phase: str, plan: dict) -> None:
    """Keeps the existing collection given to each folder (not one still to create: its URL never changes)."""
    pins = db.jload(db.kv_get(_pin_key(account_id, pst_key, phase)), {}) or {}
    if not isinstance(pins, dict):
        pins = {}
    pins.update({fk: p.name for fk, p in plan.items() if not p.label})
    db.kv_set(_pin_key(account_id, pst_key, phase), db.jdump(pins))


def as_connector_error(e: BaseException) -> ConnectorError:
    if isinstance(e, ConnectorError):
        return e
    return ConnectorError(f"{type(e).__name__}: {e}", ConnectorError.REJECTED)


@dataclass
class TargetTask(AppendTask):
    kind: str = MAIL
    name: str | None = None


class TargetPool(AppendPool):
    """AppendPool over target sessions: threads, resizing, cancellation and statistics are inherited. `_do` has the
    rule of the IMAP path: after a temporary error, ask the target before writing again."""

    def __init__(self, target: Target, n: int, should_stop, base_delay: float = 1.0):
        self.target = target  # before super().__init__: it starts the threads
        self.tunable = CAP_PARALLEL in target.capabilities
        super().__init__(None, n, None, should_stop, base_delay)

    def _new_conn(self):
        return self.target.session()

    def _note_error(self, err: ConnectorError) -> None:
        note = ErrorNote(err.result_kind, str(err)[:300])
        if classify_error(note):
            with self.cond:
                self._errors.append(note)
                self._note_pressure()

    def _do(self, session, t: TargetTask) -> AppendResult:
        item = OutgoingItem(t.kind, t.raw, t.import_id, t.flags, t.internaldate, t.name)
        created = check = False
        attempt = 0
        while True:
            try:
                session.ensure()
                if check:
                    try:
                        found = session.find(t.target, item)
                    except ConnectorError as se:
                        if se.retryable:
                            raise
                        found = []  # folder missing / unreadable: the item is not there
                    if found:
                        return AppendResult(t.item_id, True, uid=found[0], size=t.size, msg="already present")
                uid = session.put(t.target, item)
                return AppendResult(t.item_id, True, uid=uid, size=t.size,
                                    version=session.written_version(t.target, uid) if uid else None,
                                    note=getattr(session, "last_note", "") or "")
            except Exception as e:  # noqa: BLE001 - classified, reported per item
                err = as_connector_error(e)
                if err.kind != ConnectorError.FOLDER_MISSING:
                    self._note_error(err)
                if err.kind == ConnectorError.FOLDER_MISSING and not created:
                    created = True
                    try:
                        session.ensure_folder(t.target)
                        continue
                    except Exception as ce:  # noqa: BLE001
                        cerr = as_connector_error(ce)
                        return AppendResult(t.item_id, False, cerr.result_kind, str(cerr))
                if err.retryable and attempt < settings.max_retries and not self._stopping():
                    attempt += 1
                    check = True
                    session.close()
                    end = time.time() + min(60.0, self.base_delay * (2 ** (attempt - 1)))
                    while time.time() < end and not self._stopping():
                        time.sleep(0.1)
                    continue
                kind = "perm" if err.kind == ConnectorError.FOLDER_MISSING else err.result_kind
                return AppendResult(t.item_id, False, kind, str(err))


class _NativeGuard:
    """Crash marker of the sources running native code (PST): see JobRunner._mark and crash.py."""

    def __init__(self, runner: "ConnectorJobRunner"):
        self.r = runner

    def before_read(self, folder, idx: int) -> None:
        self.r._mark(KIND_OF_PHASE[folder.kind], folder_key(folder.path), idx)

    def skipped(self, folder, idx: int) -> bool:
        keys = {folder_key(folder.path), legacy_folder_key(folder.path)}
        return any(k == KIND_OF_PHASE[folder.kind] and fo in keys and i == idx for k, fo, i in self.r.skips)

    def clear(self) -> None:
        self.r._mark()


class ConnectorJobRunner(JobRunner):
    def __init__(self, job_id: int, worker_id: str):
        super().__init__(job_id, worker_id)
        self.source = None
        self.targets: dict[str, Target] = {}
        self.plans: dict[str, dict] = {}
        self.setup_error: str | None = None
        self.source_errors = 0
        self.totals["task"] = 0
        self._incremental = False                 # the source can list what changed since a cursor (delta mode)
        self._cursors: dict[str, str] = {}        # delta cursors to store once the phase is complete
        self._mirror_entries: dict[tuple[str, str], list] = {}  # mirror: (kind, folder key) -> entries to add
        self._known_folders: set[str] = set()     # keys of every folder the source has, excluded ones too
        self._source_keys: set[str] = set()       # folder_key() of every folder the source has (no old keys)
        self._deferred: list = []                 # items waiting for another write of their resource name
        self._deferring = False
        self._src_flags: dict[str, dict[str, list[str]]] = {}  # folder key -> {item key: flags} of a full listing
        self._sizing: list[int] | None = None    # [bytes, messages] of the mail listing in progress (source size)
        self._uids: dict[tuple[str, str], tuple[str, str]] = {}  # DAV: (collection, UID) -> (resource name, subject)
        if not self.acc:
            return
        self.source_kind = wiring.source_kind(self.acc)
        if self.source_kind == wiring.PST:
            return  # pst_id, pst_key: those of the PST, its state is shared with the historical path
        self.pst_row = None
        self.pst_id = 0
        self.skips = set()
        self.pst_key = wiring.items_key_of_account(self.acc)
        self.setup_error = wiring.source_problem(self.acc)

    # ------------------------------------------------------------ retries
    def _retry(self, fn, reset):
        """fn() with backoff on retryable errors, reset() between two tries (pause and stop are honoured)."""
        attempt = 0
        while True:
            try:
                return fn()
            except ConnectorError as e:
                if not e.retryable or attempt >= settings.max_retries:
                    raise
            attempt += 1
            self._sleep(min(60.0, 2.0 ** (attempt - 1)))
            reset()

    def _reset_source(self) -> None:
        self.source.close()
        try:
            self.source.open()
        except ConnectorError as e:
            if not e.retryable:
                raise

    def _on_source(self, fn):
        return self._retry(fn, self._reset_source)

    def _on_session(self, session, fn):
        def call():
            session.ensure()
            return fn()
        return self._retry(call, session.close)

    # ------------------------------------------------------------ job
    def _setup_tuning(self) -> None:
        super()._setup_tuning()
        self.build_procs = 0  # fetched in this process: only the number of target sessions moves
        self._save_tuning()
        self.log("This route reads items in the job process: no builder process, only the connections are tuned")

    def _wanted(self, phase: str) -> bool:
        """Tasks have no option of their own: they follow the calendars."""
        return bool(self.acc["opt_calendar" if phase == TASKS else f"opt_{phase}"])

    def _phase_enabled(self, kind: str) -> bool:
        phase = PHASE_OF_KIND[kind]
        return self._wanted(phase) and phase in self.targets

    def recompute_progress(self) -> None:
        super().recompute_progress()
        rows = db.q("SELECT status, COUNT(*) n FROM items WHERE account_id=? AND pst_key=? AND kind='task' GROUP BY status",
                    (self.account_id, self.pst_key))
        c = {r["status"]: r["n"] for r in rows}
        if c or self.totals["task"]:
            self.progress[TASKS] = {"done": c.get("ok", 0) + c.get("skipped", 0), "total": self.totals["task"],
                                    "errors": c.get("error", 0)}

    def _execute(self) -> None:
        if self.setup_error:
            raise JobFailed(self.setup_error)
        if not self.pst_key:
            raise JobFailed("no PST linked to the mailbox" if self.source_kind == wiring.PST else "source not set")
        ctx = {"guard": _NativeGuard(self)}
        if self.mode == "mirror":
            blocked = mm.preflight(self.acc)
            if blocked:
                self.report["mirror"] = {"blocked": blocked}
                raise JobFailed("mirror refused: " + "; ".join(f"{b['code']}: {b['message']}" for b in blocked))
        try:
            self.targets = wiring.targets_of_account(self.acc, self.srv)
            self.source = wiring.source_of_account(self.acc, ctx)
        except ConnectorError as e:
            raise JobFailed(str(e)) from e
        composite = self.source  # problems of its DAV part, read below (the mirror wrapper hides them)
        if self.mode == "mirror":
            self.source = mm.ReadOnlySource(self.source)  # nothing but reads can reach the source
        self.report["source"] = {"kind": self.source_kind, "namespace": self.pst_key}
        self.log(f"Route: {self.source_kind} to " + (self.acc["target_kind"] or "the server of the mailbox"))
        try:
            if self.source.native_code:
                self._mark(phase="open")
            try:
                self._on_source(self.source.open)
                self.folders = self._on_source(self.source.folders)
            except ConnectorError as e:
                raise JobFailed(f"Source ({self.source_kind}): {e}") from e
            self._mark()
            self._source_gaps(composite)
            for f in self.folders:
                self._known_folders |= {folder_key(f.path), legacy_folder_key(f.path)}
                self._source_keys.add(folder_key(f.path))
            self.folders = [f for f in self.folders if not self._excluded(f)]
            for f in self.folders:
                if f.skip and f.kind in CARRIED and self._wanted(f.kind):
                    self.log(f"Folder '{folder_key(f.path)}' ({f.kind}) not migrated: {f.skip}")
                    self.report.setdefault("folders_skipped", []).append(
                        {"folder": folder_key(f.path), "kind": f.kind, "reason": f.skip})
                    # items of an earlier run are no longer part of the migration (nor of its checks)
                    db.ex("UPDATE items SET status='skipped', error=? WHERE account_id=? AND pst_key=? AND kind=? "
                          "AND folder IN (?, ?)", (f"not migrated: {f.skip}"[:2000], self.account_id, self.pst_key,
                                                   CARRIED[f.kind], folder_key(f.path), legacy_folder_key(f.path)))
            self.folders = [f for f in self.folders if not f.skip]
            if self.mode == "delta":
                try:
                    self._incremental = CAP_INCREMENTAL in registry.role_of(self.source_kind, "source").capabilities
                except ConnectorError:
                    self._incremental = False
                self.log("Final resync (delta): " + ("only what changed since the previous pass is listed"
                                                     if self._incremental else
                                                     "this source cannot list changes, full listing filtered by the items already migrated"))
            phases = []
            for phase, kind in CARRIED.items():
                mine = [f for f in self.folders if f.kind == phase]
                if not mine or not self._wanted(phase):
                    continue
                if phase not in self.targets:
                    self.log(f"{len(mine)} {phase} folder(s) at the source, no target for them: not migrated", "warning")
                    continue
                phases.append(phase)
                self.totals[kind] += sum(int(f.count or 0) for f in mine)
            self.flush(force=True)
            self.precheck_targets(phases)
            if self.mode == "mirror":
                self._run_mirror(phases)
            else:
                for phase in phases:
                    self._run_phase(phase)
            if MAIL in phases:
                self._repair_flags()
            self.verify_targets(phases)
            if TASKS in phases:
                self.report[TASKS] = dict(self.progress.get(TASKS) or {})
        finally:
            self.source.close()

    def _source_gaps(self, source) -> None:
        """Calendars or contacts wanted that this source does not bring: said, never skipped in silence. A DAV part
        of the source that could not be read ends the job with errors; the mail goes on."""
        for p in getattr(source, "problems", None) or []:
            msg = f"Source: {p}: calendars, tasks and contacts not copied in this run"
            self.log(msg, "error")
            self.problems.append(msg)
            self.report["warnings"].append(msg)
        try:
            gaps = wiring.source_gaps(self.acc)
        except ConnectorError:
            gaps = {}
        gaps = {k: v for k, v in gaps.items() if k in self.targets}  # nowhere to go either: nothing lost here
        for phase, why in gaps.items():
            msg = f"{'Calendars' if phase == CALENDAR else 'Contacts'} wanted but {why}: not copied"
            self.log(msg, "warning")
            self.report["warnings"].append(msg)
        if gaps:
            self.report["source"]["missing"] = gaps

    # ------------------------------------------------------------ precheck
    def precheck_targets(self, phases: list[str]) -> None:
        self.set_phase("precheck")
        self._free_bytes = None
        for phase in phases:
            session = self.targets[phase].session()
            try:
                self._on_session(session, session.ensure)
                folders = [f for f in self.folders if f.kind == phase]
                if phase == MAIL:
                    self.plans[phase] = self._on_session(
                        session, lambda: foldermap.plan_mapped(session.plan_folders, folders, self.policy.mapper()))
                else:
                    dav = hasattr(session, "history")  # DAV: which collections earlier runs wrote to
                    if dav:
                        session.history = collection_history(self.account_id, self.pst_key, phase)
                    self.plans[phase] = self._on_session(session, lambda: session.plan_folders(folders))
                    for note in getattr(session, "notes", None) or []:
                        self.log(f"Target for {phase}: {note}")
                    if dav:
                        pin_collections(self.account_id, self.pst_key, phase, self.plans[phase])
                quota = self._on_session(session, session.quota)
                info = session.info()
                self.report["precheck"][phase] = {"ok": True, "quota": quota, **info}
                if phase == MAIL and info.get("append_limit"):
                    self.append_limit = int(info["append_limit"])
                    self.log(f"The target accepts messages up to {self.append_limit / 2 ** 20:.1f} MB")
                if phase == MAIL and quota and quota.get("limit"):
                    self._free_bytes = quota["limit"] - (quota.get("used") or 0)
            except ConnectorError as e:
                if e.kind == ConnectorError.AUTH:
                    db.ex("UPDATE accounts SET status='auth_failed', last_test=? WHERE id=?",
                          (time.time(), self.account_id))
                raise JobFailed(f"Target ({phase}): {e}") from e
            finally:
                session.close()
            self.log(f"Target for {phase}: connection OK")
        db.ex("UPDATE accounts SET status='ok', last_test=? WHERE id=?", (time.time(), self.account_id))
        self.check()

    # ------------------------------------------------------------ one phase
    def _rows(self, kind: str, fkey: str, path: list[str]) -> dict[str, dict]:
        rows = db.q("SELECT id, item_key, status, target, hash, error, source_ref, stamp FROM items WHERE account_id=? AND "
                    "pst_key=? AND kind=? AND folder IN (?, ?)",
                    (self.account_id, self.pst_key, kind, fkey, legacy_folder_key(path)))
        return {r["item_key"]: dict(r) for r in rows}

    def _resolve(self, session, kind: str, rows: dict[str, dict]) -> None:
        """Before any write: `inflight` items (the write may have succeeded) and `error` items already tried are
        looked up on the target."""
        todo = [r for r in rows.values()
                if r["target"] and (r["status"] == "inflight"
                                    or (r["status"] == "error" and r["hash"] and self.wants(r)))]
        if todo:
            n = sum(r["status"] == "inflight" for r in todo)
            self.log(f"{n} 'inflight' item(s) and {len(todo) - n} in error to check on the target")
        for r in todo:
            self.check()
            probe = OutgoingItem(PHASE_OF_KIND[kind], b"", r.get("stamp") or import_id_of(self.pst_key, r["item_key"]),
                                 name=None if kind == MAIL else r["hash"])
            try:
                found = self._on_session(session, lambda: session.find(r["target"], probe))
            except ConnectorError as e:
                if e.retryable or e.kind == ConnectorError.AUTH:
                    raise JobFailed(f"cannot verify items already sent: {e}") from e
                found = []  # folder missing: not sent yet
            if found:
                self.set_item(r["id"], "ok")
                r["status"] = "ok"
                if len(found) > 1:
                    self.log(f"Warning: {len(found)} copies of {probe.import_id} already present", "warning")
            elif r["status"] == "inflight":
                self.set_item(r["id"], "pending")
                r["status"] = "pending"

    def _relocate(self, session, kind: str, fkey: str, rows: dict[str, dict], seen: set[str], plan_name: str) -> None:
        """DAV: the items an earlier run copied into another collection than the one the plan now gives their folder
        (the second "Contacts" an earlier version created) go to this one. Each is first looked up there by its
        resource name: one already there is left as it is, never written over; the others are copied. The earlier
        copies are never deleted: the log and the report name that collection, to delete by hand once checked."""
        target = self.targets.get(PHASE_OF_KIND.get(kind, ""))
        if self.mode == "errors" or kind == MAIL or not getattr(target, "names_resources", False):
            return
        moved = [r for r in rows.values() if r["item_key"] in seen and r["status"] == "ok" and r["target"]
                 and _ukey(r["target"]) != _ukey(plan_name)]
        if not moved:
            return
        was: dict[str, int] = {}
        for r in moved:
            was[r["target"]] = was.get(r["target"], 0) + 1
            db.ex("UPDATE items SET status='inflight', target=?, target_etag=NULL WHERE id=?", (plan_name, r["id"]))
            r["status"], r["target"] = "inflight", plan_name
        names = getattr(target, "listed", None) or {}
        for old, n in was.items():
            name = names.get(_ukey(old))
            w = (f"collection_changed: {n} item(s) of '{fkey}' were copied by an earlier run into "
                 f"{f'{name!r} ' if name else ''}({old}); this folder now goes to {plan_name}: they are copied there "
                 f"too (those already there are left as they are). The earlier copies are not deleted: once checked, "
                 f"that collection can be deleted by hand if nothing else is in it")
            self.log(w, "warning")
            self.report["warnings"].append(w)
            self.report.setdefault("dav_moved", []).append(
                {"folder": fkey, "from": old, "from_name": name, "to": plan_name, "items": n})
        self._resolve(session, kind, {r["item_key"]: r for r in moved})

    def _list_folder(self, session, f, kind: str, fi: int, plan_name: str) -> list:
        """Entries (sort value, key, folder index, item) of a folder still to migrate."""
        fkey = folder_key(f.path)
        if self.mode == "mirror":  # listed and judged by the mirror analysis already
            entries = self._mirror_entries.get((kind, fkey), [])
            self.log(f"Folder '{fkey}' -> {plan_name}: {len(entries)} new item(s) to add (mirror)")
            return entries
        rows = self._rows(kind, fkey, f.path)
        self._resolve(session, kind, rows)
        self.current_folder = f"Listing {fkey}"
        self.flush()
        partial = False  # delta: the listing holds what changed, not the whole folder
        if self.mode == "delta" and self._incremental:
            ckey = f"delta:{self.account_id}:{self.pst_key}:{fkey}"
            ch = self._on_source(lambda: self.source.changed_since(f, db.kv_get(ckey)))
            listing, partial = ch.items, not ch.reset
            if ch.cursor:
                self._cursors[ckey] = ch.cursor
        else:
            listing = self._on_source(lambda: list(self.source.iter_items(f)))
        if not partial:
            self.totals[kind] += len(listing) - int(f.count or 0)
            f.count = len(listing)
            if kind == MAIL:  # the flags of the whole folder, for the copies already on the target (_repair_flags)
                self._src_flags[fkey] = {it.key: it.flags for it in listing if flags_to_add(it.flags, ())}
        if self._sizing is not None:  # size of the mail at the source, from this listing (see _run_phase)
            if partial or any(it.size is None for it in listing):
                self._sizing = None
            else:
                self._sizing[0] += sum(it.size for it in listing)
                self._sizing[1] += len(listing)
        seen = {it.key for it in listing}
        # rows of keys the source no longer lists: the same messages may be back under other keys (UIDVALIDITY)
        orphans: dict[str, list[dict]] = {}
        for r in rows.values():
            if not partial and r["item_key"] not in seen and r["source_ref"]:
                orphans.setdefault(r["source_ref"], []).append(r)
        self._relocate(session, kind, fkey, rows, seen, plan_name)
        entries = []
        filtered: dict[str, list[tuple]] = {}
        for it in listing:
            r = rows.get(it.key)
            old = None
            if r is None and it.alt_key and orphans.get(it.alt_key):
                old = r = orphans[it.alt_key].pop()
            if not self.wants(r):
                continue
            if old is not None:  # processed again under its new key (an `ok` one was never wanted)
                db.ex("DELETE FROM items WHERE id=? AND status<>'ok'", (old["id"],))
            if self.policy.has_filters and kind != "contact":
                why = self.policy.reason(it.date, it.size if kind == "mail" else None)
                if why:
                    filtered.setdefault(why, []).append((it.key, fkey, it.size, None, it.alt_key))
                    continue
            ts = it.date.timestamp() if it.date is not None else None
            entries.append((-ts if ts is not None else ordering.NO_DATE, it.key, fi, it))
        for why, group in filtered.items():
            self._mark_filtered(kind, group, why)
        # gone from the source before being written: nothing left to wait for
        if not partial:
            for group in orphans.values():
                for r in group:
                    if r["status"] in ("pending", "inflight"):
                        self.set_item(r["id"], "skipped", "no longer at the source")
            for r in rows.values():
                if r["item_key"] not in seen and not r["source_ref"] and r["status"] in ("pending", "inflight"):
                    self.set_item(r["id"], "skipped", "no longer at the source")
        self.log(f"Folder '{fkey}' -> {plan_name}: {len(listing)} item(s)"
                 + (" changed since the previous pass" if partial else "") + f", {len(entries)} to migrate")
        return entries

    def _run_phase(self, phase: str) -> None:
        self.set_phase(phase)
        kind = CARRIED[phase]
        target = self.targets[phase]
        plan = self.plans[phase]
        session = target.session()
        pool = None
        try:
            folders = [f for f in self.folders if f.kind == phase]
            uncreated: set[str] = set()  # DAV collections still to create: only once their folder has items
            for f in folders:
                fk = folder_key(f.path)
                if plan[fk].label:
                    uncreated.add(fk)
                else:
                    self._ensure_folder(session, phase, plan, fk)
            parallel = CAP_PARALLEL in target.capabilities
            pool = TargetPool(target, self.connections if parallel else 1, self._should_stop)
            pool.limiter = self._limiter
            self._tune_t0 = None
            entries: list = []
            todo = [f for f in folders if self._folder_has_work(kind, f)]
            # every mail folder listed in full: the size of the mail at the source comes for free
            self._sizing = [0, 0] if phase == MAIL and len(todo) == len(folders) and self.mode != "mirror" else None
            for f in todo:
                self.check()
                fk = folder_key(f.path)
                try:
                    listed = self._list_folder(session, f, kind, self._folder_index(f), plan[fk].name)
                except ConnectorError as e:
                    raise JobFailed(f"Source ({self.source_kind}): {e}") from e
                if fk in uncreated and listed:
                    uncreated.discard(fk)
                    self._ensure_folder(session, phase, plan, fk)
                elif fk in uncreated and not f.count:
                    self.log(f"Folder '{fk}': nothing to copy, the collection '{plan[fk].label}' is not created")
                if fk in self.folder_errors:
                    for _sv, key, _fi, it in listed:
                        self.upsert_item(kind, key, fk, "error", target=plan[fk].name, error=self.folder_errors[fk],
                                         source_ref=it.alt_key)
                    continue
                entries.extend(listed)
            if self._sizing is not None:
                mailbox_size.store(self.account_id, MailboxSize(self._sizing[0], self._sizing[1]), self.pst_key)
                self._sizing = None
            entries.sort(key=lambda e: (e[0], e[1]))
            self._warn_quota(phase, entries)
            self.current_folder = None
            now = time.time()  # items/s and ETA measure the migration, not the listing
            self._rate_window = [(now, sum(p["done"] for p in self.progress.values()), self._bytes_done)]
            entries.reverse()  # pop() = next entry, memory released as the job progresses
            while entries:
                _sv, key, fi, it = entries.pop()
                self.check()
                self._handle_results(pool.drain())
                self._tune_tick(pool)
                self._migrate_item(self.folders[fi], kind, phase, key, it, plan, pool)
            self._wait_pool(pool)
            self._say_not_sent(target)
            if self._deferred:  # items waiting for the write of another item of the same resource name
                waiting, self._deferred = self._deferred, []
                self._deferring = True
                try:
                    for df, dkind, dphase, dkey, dit in waiting:
                        self.check()
                        self._migrate_item(df, dkind, dphase, dkey, dit, plan, pool)
                    self._wait_pool(pool)
                finally:
                    self._deferring = False
                self._say_not_sent(target)
            self._mark()
            self.check()
            # everything listed is now recorded: the next delta pass may start from these cursors
            for ckey, cursor in self._cursors.items():
                db.kv_set(ckey, cursor)
            self._cursors.clear()
            # one check per target folder: several source folders may share it (e.g. one default collection)
            shared: dict[str, list[str]] = {}
            for f in folders:
                fk = folder_key(f.path)
                if fk not in self.folder_errors and not (fk in uncreated and not f.count):  # empty, never created
                    shared.setdefault(plan[fk].name, []).append(fk)
            for target_name, fks in shared.items():
                self._check_target(session, phase, fks, target_name)
        finally:
            if pool is not None:
                try:
                    self._wait_pool(pool)
                finally:
                    pool.close()  # waits for all threads: nothing written after the phase ends
                    try:
                        self._handle_results(pool.drain())
                    except JobFailed:
                        pass
            session.close()
        self.current_folder = None
        self.flush(force=True)

    def _ensure_folder(self, session, phase: str, plan: dict, fk: str) -> None:
        """The target folder of `fk` exists (created when it has to be). Refused: a folder error, said once, and its
        items are in error with that reason; never put in another collection (the separation of the source is
        kept). Any other failure ends the job."""
        p = plan[fk]
        try:
            if p.label:  # a DAV collection to create under the name of the source folder
                self._on_session(session, lambda: session.ensure_folder(p.name, p.flag, label=p.label))
            else:
                self._on_session(session, lambda: session.ensure_folder(p.name, p.flag))
        except ConnectorError as e:
            if e.kind != ConnectorError.REJECTED:
                raise JobFailed(f"Target ({phase}): {e}") from e
            if p.label:
                what = COLLECTIONS.get(phase, "collections")
                self.folder_errors[fk] = (f"the target refuses to create {what}: create '{p.label}' in the webmail "
                                          f"then rerun ({e})")
            else:  # refused: only the items of this folder are in error, the mailbox continues
                self.folder_errors[fk] = f"folder '{p.name}' not created: {e}"
            self.log(f"Folder '{fk}': {self.folder_errors[fk]}", "error")
            return
        renamed = (getattr(session, "renamed", None) or {}).get(p.name)
        if renamed:
            self.log(f"Folder '{fk}': the target refused the name '{p.label}', the collection is named '{renamed}'",
                     "warning")

    def _say_not_sent(self, target) -> None:
        """One line per collection whose items were not sent because it refused every one before them."""
        breaker = getattr(target, "breaker", None)
        for coll, n, why in breaker.summary() if breaker is not None else []:
            msg = (f"{n} item(s) for {coll} not sent: the collection refused every item in a row while it answers "
                   f"(last refusal: {why}). They are in error: rerun once the cause is fixed")
            self.log(msg, "error")
            self.report["warnings"].append(msg)

    def _handle_results(self, results) -> None:
        """Items a DAV target did not send (NOT_SENT): in error without a line each (_say_not_sent says it once).
        Written with a note (a second form of the object): the note is logged and kept with the item."""
        rest = []
        for r in results:
            if not r.ok and r.msg.startswith(NOT_SENT):
                self._inflight_bytes.pop(r.item_id, None)
                self.set_item(r.item_id, "error", f"[{r.kind}] {r.msg[len(NOT_SENT):]}")
                continue
            if r.ok and r.note:
                row = db.q1("SELECT warnings, subject FROM items WHERE id=?", (r.item_id,))
                notes = (db.jload(row["warnings"], []) or []) if row else []
                db.ex("UPDATE items SET warnings=? WHERE id=?", (db.jdump([*notes, r.note]), r.item_id))
                self._item_log(f"Item {r.item_id} ('{row['subject'] if row else ''}'): {r.note}", "warning")
            rest.append(r)
        super()._handle_results(rest)

    def _warn_quota(self, phase: str, entries: list) -> None:
        free = getattr(self, "_free_bytes", None)
        if phase != MAIL or free is None:
            return
        need = sum(e[3].size or 0 for e in entries)
        if need > free:
            w = f"quota: {free // 2**20} MB free for {need // 2**20} MB to migrate, risk of exceeding"
            self.report["warnings"].append(w)
            self.log(w, "warning")

    def _migrate_item(self, f, kind: str, phase: str, key: str, it, plan: dict, pool: TargetPool) -> None:
        fkey = folder_key(f.path)
        self.current_folder = fkey
        target = plan[fkey].name
        import_id = import_id_of(self.pst_key, key)
        if self.source.native_code:
            self._mark(kind, fkey, it.index, key)  # a crash here marks this item, the resume goes on after it
        else:
            crash.maybe_delay()
        try:
            got = self._on_source(lambda: self.source.fetch(f, it, import_id))
        except (PauseRequested, StopRequested, JobFailed):
            raise
        except Exception as e:  # noqa: BLE001 - reported per item
            err = as_connector_error(e)
            if err.kind in (ConnectorError.AUTH, ConnectorError.CONFIG):
                raise JobFailed(f"Source ({self.source_kind}): {err}") from e
            self.upsert_item(kind, key, fkey, "error", target=target, error=f"source: {err}"[:2000],
                             source_ref=it.alt_key)
            self.log(f"Cannot read ({fkey}, {key}): {err}", "error")
            if err.retryable:
                self.source_errors += 1
                if self.source_errors >= MAX_SOURCE_ERRORS:
                    raise JobFailed(f"Source ({self.source_kind}) unavailable: {err}") from e
            return
        self.source_errors = 0
        if got is None:
            self.upsert_item(kind, key, fkey, "skipped", target=target, error="nothing to migrate at the source",
                             source_ref=it.alt_key)
            return
        date = got.internal_date
        if date is not None and date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        if kind == MAIL:
            raw, name = stamp_import_id(got.raw, import_id), None
            mark = norm_hash(raw)
        else:
            digest = hashlib.sha256(import_id.encode("utf-8")).hexdigest()[:32]
            raw, name = got.raw, got.name or self.targets[phase].resource_name(got.raw, f"{digest}.{DAV_EXT[phase]}")
            mark = name  # as on the PST path: the hash column of a DAV item is its resource name
            other = None
            if getattr(self.targets[phase], "names_resources", False):  # the name IS the item there (DAV)
                other = db.q1("SELECT folder, hash, status, target_etag FROM items WHERE account_id=? AND pst_key=? AND kind=? "
                              "AND target=? AND hash=? COLLATE NOCASE AND status IN ('ok','inflight') "
                              "AND NOT (folder=? AND item_key=?) ORDER BY status='ok' DESC LIMIT 1",
                              (self.account_id, self.pst_key, kind, target, name, fkey, key))
                if other is not None and other["status"] != "ok":
                    if not self._deferring:
                        # its write is still in progress: this one waits for it (after the pool is drained)
                        self._deferred.append((f, kind, phase, key, it))
                        return
                    other = None  # that write did not succeed: this one is written itself
            if other is not None:
                # the resource is already the copy of another item (same UID in two source folders): never written
                # over; both rows carry it, the mirror deletes it only when both go (mirror.py, shared resources)
                row_id = self.upsert_item(kind, key, fkey, "ok", target=target, subject=got.subject,
                                          hash=other["hash"], size=len(raw), source_ref=it.alt_key,
                                          version=mm.version_of(phase, it), src_ref=mm.ref_json(it.ref))
                db.ex("UPDATE items SET target_etag=? WHERE id=?", (other["target_etag"], row_id))  # one resource
                w = (f"shared_resource: '{got.subject}' ({fkey}) has the resource name '{name}' of an item of "
                     f"'{other['folder']}' in {target}: not written over it, one copy for both")
                self.report["warnings"].append(w)
                self.log(w, "warning")
                return
        if self._size_gate(kind, key, fkey, len(raw), got.subject, target, it.alt_key):
            return
        notes = list(got.warnings or [])
        if kind != MAIL and getattr(self.targets[phase], "names_resources", False):
            uid = davfix.object_uid(raw)
            seen = self._uids.setdefault((_ukey(target), uid), (name, got.subject)) if uid else None
            if seen is not None and seen[0].casefold() != name.casefold():
                # two objects of one UID under two names: a server that keeps one per UID keeps only one of them
                notes.append(f"{SAME_UID}UID '{uid}' also has '{seen[1]}' ({seen[0]}) in this collection: the "
                             "server may keep only one of them")
        warnings = db.jdump(notes) if notes else None
        if notes:
            self.log(f"Warning '{got.subject}': {', '.join(notes)}", "warning")
        item_id = self.upsert_item(kind, key, fkey, "inflight", target=target, subject=got.subject, hash=mark,
                                   size=len(raw), warnings=warnings, source_ref=it.alt_key,
                                   version=mm.version_of(phase, it), src_ref=mm.ref_json(it.ref))
        if kind != MAIL:  # the etag of an earlier write no longer says anything about this one
            db.ex("UPDATE items SET target_etag=NULL WHERE id=? AND target_etag IS NOT NULL", (item_id,))
        self._submit(pool, TargetTask(item_id, target, raw, list(got.flags), date, len(raw), import_id, kind=phase,
                                      name=name))

    # ------------------------------------------------------------ mirror (docs/planning.md "Mirror")
    def _run_mirror(self, phases: list[str]) -> None:
        """Analysis (read only), then moves, flags, updates and deletions of OUR items on the target, then the new
        items through the normal pipeline. Every action is logged with its key, its target ref and its result."""
        opts = mm.Options.of((db.jload(self.job["mirror"], {}) or {}).get("options"))
        self.set_phase("mirror")
        stats = {p: mm.counters() for p in phases}
        self.report["mirror"] = {"options": opts.view(), "blocked": [], **stats}
        sessions = {}
        for p in phases:  # every write's etag is kept, even from a server whose PUT does not give it (DAV)
            if hasattr(self.targets[p], "keep_versions"):
                self.targets[p].keep_versions = True
        try:
            for p in phases:
                s = self.targets[p].session()
                self._on_session(s, s.ensure)
                sessions[p] = s
            for p in phases:  # what a previous run left inflight may be on the target already: settled first
                kind = CARRIED[p]
                for f in [f for f in self.folders if f.kind == p]:
                    self._resolve(sessions[p], kind, self._rows(kind, folder_key(f.path), f.path))
            an = mm.analyze(self.source, [f for f in self.folders if f.kind in phases], self._known_folders, sessions,
                            {p: self.targets[p].capabilities for p in phases}, self.account_id, self.pst_key,
                            self.policy, opts, retry=self._on_source,
                            retry_target=lambda s, fn: self._on_session(s, fn), log=self.log,
                            source_keys=self._source_keys,
                            owners={p for p in phases if getattr(self.targets[p], "names_resources", False)})
            self.report["mirror"]["blocked"] = an.blocked
            self.report["mirror"]["held"] = an.held
            self.report["mirror"]["notes"] = an.notes
            if an.fatal:
                raise JobFailed("mirror refused: " + "; ".join(f"{b['code']}: {b['message']}" for b in an.blocked))
            for b in an.blocked:
                self.problems.append(f"mirror {b['code']}: {b['message']}")
            for p in phases:
                stats[p]["skipped_by_safety"] = an.skipped.get(p, 0)
            for r in an.gone_unfinished:  # confirmed gone by the source (exists), never after a listing error
                self.set_item(r["id"], "skipped", mm.GAVE_UP)
            for row_id, ref in an.backfill:  # rows migrated before src_ref was kept (the dry run writes nothing)
                db.ex("UPDATE items SET src_ref=? WHERE id=? AND src_ref IS NULL", (ref, row_id))
            for p in phases:
                self._mirror_apply(p, an, sessions[p], stats[p])
        finally:
            for s in sessions.values():
                s.close()
            mm.save_counters(self.job_id, self.report["mirror"])
        adds: dict[str, list[int]] = {}
        for p in phases:
            kind = CARRIED[p]
            for a in an.of(p, "add"):
                ts = a.item.date.timestamp() if a.item.date is not None else None
                self._mirror_entries.setdefault((kind, folder_key(a.folder.path)), []).append(
                    (-ts if ts is not None else ordering.NO_DATE, a.item.key, self._folder_index(a.folder), a.item))
                adds.setdefault(p, []).append(a.item.key)
        try:
            for p in phases:
                if adds.get(p):
                    self._run_phase(p)
        finally:
            for p, keys in adds.items():
                kind = CARRIED[p]
                for i in range(0, len(keys), 500):
                    chunk = keys[i:i + 500]
                    rows = db.q(f"SELECT status, COUNT(*) n FROM items WHERE account_id=? AND pst_key=? AND kind=? AND "
                                f"item_key IN ({','.join('?' * len(chunk))}) GROUP BY status",
                                (self.account_id, self.pst_key, kind, *chunk))
                    c = {r["status"]: r["n"] for r in rows}
                    stats[p]["added"] += c.get("ok", 0)
                    stats[p]["errors"] += c.get("error", 0)
            mm.save_counters(self.job_id, self.report["mirror"])
        for p in phases:
            if stats[p]["errors"]:
                self.problems.append(f"mirror: {stats[p]['errors']} {p} action(s) failed")
            self.log(f"Mirror {p}: " + ", ".join(f"{k} {v}" for k, v in stats[p].items()))

    def _mirror_adopt(self, phase: str, a, session) -> None:
        """A row without the etag of mccb's write (migrated before they were kept): when the target holds the same
        object as the source (same lines, in any order: servers reorder them), the etag the inventory saw is kept,
        and the mirror may delete or update it from the next pass on. Different: left as it is, never touched."""
        key, target = a.row["item_key"], a.row["target"]
        import_id = import_id_of(self.pst_key, key)
        try:
            got = self._on_source(lambda: self.source.fetch(a.folder, a.item, import_id))
            there = self._on_session(session, lambda: session.read(target, a.refs[0].ref)) if got else None
        except ConnectorError as e:
            self._item_log(f"Mirror adopt {phase} key={key}: cannot compare ({e}), kept unverified", "warning")
            return
        if got is None or there is None or a.refs[0].version is None:
            return
        if mm.same_object(got.raw, there):
            db.ex("UPDATE items SET target_etag=? WHERE id=? AND target_etag IS NULL", (a.refs[0].version, a.row["id"]))
            self.log(f"Mirror adopt {phase} key={key} ref={a.refs[0].ref}: same object on both sides, its etag kept")
        else:
            self.log(f"Mirror adopt {phase} key={key} ref={a.refs[0].ref}: the target holds another version, never "
                     "touched by the mirror", "warning")

    def _mirror_act(self, session, st: dict, what: str, key: str, fn):
        """(result, ok) of one write on the target; a failure is counted and logged, the pass goes on."""
        self.check()
        try:
            return self._on_session(session, fn), True
        except ConnectorError as e:
            if e.kind in (ConnectorError.AUTH, ConnectorError.QUOTA):
                raise JobFailed(f"Target: {e}") from e
            st["errors"] += 1
            self._item_log(f"Mirror {what} key={key}: failed ({e})", "error")
            return None, False

    def _mirror_apply(self, phase: str, an: "mm.Analysis", session, st: dict) -> None:
        caps = self.targets[phase].capabilities
        plan = self.plans[phase]
        now = time.time
        for a in an.of(phase, "move"):
            key, old_target = a.row["item_key"], a.row["target"]
            new_fk = folder_key(a.folder.path)
            if new_fk not in plan:
                continue
            new_target = plan[new_fk].name
            ref = a.refs[0].ref
            if new_target != old_target:
                _, ok = self._mirror_act(session, st, "ensure folder", key,
                                         lambda: session.ensure_folder(new_target, plan[new_fk].flag))
                if not ok:
                    continue
                moved, ok = self._mirror_act(session, st, "move", key, lambda: session.move(old_target, ref, new_target))
                if not ok:
                    continue
                ref = moved
            ident = mm.ident_of(phase, self.pst_key, a.row)
            with db.tx() as c:  # the row follows the mail: new key and folder, the import id it still carries
                c.execute("DELETE FROM items WHERE account_id=? AND pst_key=? AND kind=? AND item_key=? AND id<>?",
                          (self.account_id, self.pst_key, CARRIED[phase], a.item.key, a.row["id"]))
                c.execute("UPDATE items SET item_key=?, folder=?, target=?, stamp=?, job_id=?, updated_at=? WHERE id=?",
                          (a.item.key, new_fk, new_target, ident, self.job_id, now(), a.row["id"]))
            st["updated"] += 1
            self.log(f"Mirror move {phase} key={key} -> key={a.item.key} ref={a.refs[0].ref}: '{old_target}' -> "
                     f"'{new_target}': ok")
            # flags changed as well: set on the moved mail when its new ref is known, else at the next pass
            src = mm.canon_flags(a.item.flags)
            if a.row["version"] is not None and a.row["version"] != src and ref and CAP_SET_FLAGS in caps:
                old, new = mm.split_flags(a.row["version"]), mm.split_flags(src)
                _, ok = self._mirror_act(session, st, "flags", a.item.key, lambda: session.set_flags(
                    new_target, ref, sorted(new - old), sorted(old - new)))
                if ok:
                    db.ex("UPDATE items SET version=? WHERE id=?", (src, a.row["id"]))
        for a in an.of(phase, "flags"):
            key, target = a.row["item_key"], a.row["target"]
            if CAP_SET_FLAGS not in caps:
                self.log(f"Mirror flags {phase} key={key}: the target cannot change flags, skipped", "warning")
                continue
            ok = True
            for r in a.refs:
                ok = self._mirror_act(session, st, "flags", key,
                                      lambda: session.set_flags(target, r.ref, a.add, a.remove))[1] and ok
            if ok:
                db.ex("UPDATE items SET version=?, job_id=?, updated_at=? WHERE id=?",
                      (mm.canon_flags(a.item.flags), self.job_id, now(), a.row["id"]))
                st["updated"] += 1
                self.log(f"Mirror flags {phase} key={key} ref={','.join(r.ref for r in a.refs)} in '{target}': "
                         f"+{' '.join(a.add) or '-'} -{' '.join(a.remove) or '-'}: ok")
        for a in an.of(phase, "record"):  # written before versions were kept, same flags on both sides
            db.ex("UPDATE items SET version=? WHERE id=?", (mm.canon_flags(a.item.flags), a.row["id"]))
        for a in an.of(phase, "update"):
            key, target = a.row["item_key"], a.row["target"]
            if CAP_UPDATE not in caps:
                self.log(f"Mirror update {phase} key={key}: the target cannot update items, skipped", "warning")
                continue
            import_id = import_id_of(self.pst_key, key)
            try:
                got = self._on_source(lambda: self.source.fetch(a.folder, a.item, import_id))
            except ConnectorError as e:
                st["errors"] += 1
                self._item_log(f"Mirror update {phase} key={key}: cannot read the source ({e})", "error")
                continue
            if got is None:
                continue
            item = OutgoingItem(phase, got.raw, import_id, name=a.row["hash"])
            # ONE copy is updated, and never replayed: a target that replaces the item (Exchange, Graph) writes a
            # new copy and then trashes the old one; a replay after a failure in between would write yet another
            # copy. A failure is one error, the next pass starts again from what inventory() then finds.
            first = a.refs[0].ref
            self.check()
            try:
                session.ensure()
                new = session.update(target, first, item)
            except ConnectorError as e:
                if e.kind in (ConnectorError.AUTH, ConnectorError.QUOTA):
                    raise JobFailed(f"Target: {e}") from e
                if e.retryable:  # a dead connection must not fail every update after this one
                    session.close()
                st["errors"] += 1
                self._item_log(f"Mirror update {phase} key={key} ref={first} in '{target}': failed ({e})", "error")
                continue
            # other copies of the same item (a trash that failed in an earlier pass): to the trash, now that one
            # copy is up to date; never the one just written
            extra = [r.ref for r in a.refs[1:] if r.ref not in (first, new)]
            if extra:
                if session.delete_mode(target) == DELETE_HARD:
                    self.log(f"Mirror update {phase} key={key}: {len(extra)} older copy(ies) left on the target (it "
                             "would delete them for good)", "warning")
                else:
                    res, good = self._mirror_act(session, st, "trash older copies", key,
                                                 lambda: session.delete_many(target, extra, {r: a.ident for r in extra}))
                    self.log(f"Mirror update {phase} key={key}: older copies {','.join(extra)} in '{target}': "
                             + ("to the trash" if good and set(res or {}) >= set(extra) else "not all removed"),
                             "info" if good else "error")
            etag = session.written_version(target, new or first)
            db.ex("UPDATE items SET version=?, subject=?, size=?, target_etag=?, job_id=?, updated_at=? WHERE id=?",
                  (a.item.version, got.subject, len(got.raw), etag, self.job_id, now(), a.row["id"]))
            if etag and getattr(self.targets[phase], "names_resources", False) and a.row.get("hash"):
                # the other rows of this same resource (same UID in another folder) follow its new etag
                db.ex("UPDATE items SET target_etag=? WHERE account_id=? AND kind=? AND target=? AND "
                      "hash=? COLLATE NOCASE AND status='ok' AND id<>?",
                      (etag, self.account_id, CARRIED[phase], target, a.row["hash"], a.row["id"]))
            st["updated"] += 1
            self.log(f"Mirror update {phase} key={key} ref={first} -> {new or first} in '{target}': ok")
        for a in an.of(phase, "adopt"):  # written before etags were kept: adopted when both sides hold the same
            self._mirror_adopt(phase, a, session)
        for a in an.of(phase, "release"):  # its resource is also the copy of another item still at the source
            self.set_item(a.row["id"], "deleted", "mirror: deleted at the source; its resource on the target is "
                          "shared with another item still at the source: kept")
            self.log(f"Mirror delete {phase} key={a.row['item_key']} in '{a.row['target']}': resource "
                     f"'{a.row['hash']}' shared with another item, kept on the target", "warning")
        ready: dict[str, list[tuple]] = {}  # target folder -> [(action, ref, mode)] to delete
        for a in an.of(phase, "delete"):
            key, target = a.row["item_key"], a.row["target"]
            if not a.refs:  # gone from the target already (deleted there by hand): only recorded
                self.set_item(a.row["id"], "deleted", "mirror: deleted at the source, already absent from the target")
                self.log(f"Mirror delete {phase} key={key} in '{target}': already absent")
                continue
            if CAP_DELETE not in caps:
                st["skipped_by_safety"] += 1
                self.log(f"Mirror delete {phase} key={key}: the target cannot delete, kept", "warning")
                continue
            mode = session.delete_mode(target)
            kept = False
            for r in a.refs:
                if mode == DELETE_HARD:  # gone for good: a copy is kept first, or nothing is deleted
                    raw = None
                    if CAP_READBACK in caps:
                        raw, _ = self._mirror_act(session, st, "read before delete", key,
                                                  lambda: session.read(target, r.ref))
                    if not raw:
                        kept = True
                        self.log(f"Mirror delete {phase} key={key} ref={r.ref}: no copy could be kept, kept", "warning")
                        continue
                    path = mm.backup_copy(self.job_id, phase, a.ident + r.ref, raw)
                    self.log(f"Mirror delete {phase} key={key} ref={r.ref}: copy kept in {path}")
                ready.setdefault(target, []).append((a, r.ref, mode))
            if kept:
                st["skipped_by_safety"] += 1
                a.row["_kept"] = True
        done_refs: dict[int, list[bool]] = {}
        for target, group in ready.items():
            for i in range(0, len(group), 100):
                chunk = group[i:i + 100]
                expected = {ref: a.ident for a, ref, _ in chunk}
                res, good = self._mirror_act(session, st, "delete", f"{len(chunk)} item(s) in '{target}'",
                                             lambda: session.delete_many(target, [ref for _, ref, _ in chunk], expected))
                res = res or {}
                for a, ref, mode in chunk:
                    ok = good and ref in res
                    done_refs.setdefault(id(a), []).append(ok)
                    self.log(f"Mirror delete {phase} key={a.row['item_key']} ref={ref} in '{target}': "
                             + (res[ref] if ok else "failed"), "info" if ok else "error")
                    if good and not ok:
                        st["errors"] += 1
        for a in an.of(phase, "delete"):
            results = done_refs.get(id(a))
            if results and all(results) and not a.row.get("_kept"):
                self.set_item(a.row["id"], "deleted", f"mirror: deleted at the source "
                              f"({session.delete_mode(a.row['target'])})")
                st["deleted"] += 1

    # ------------------------------------------------------------ flags
    def _repair_flags(self) -> None:
        """Every copy of ours on the target is given the read, answered and starred flags its source has and it
        lacks: what the source has now when this pass read its flags (a full listing, else Source.flags_of), else
        the flags it was written with. This settles a target that kept none of the flags of APPEND, a copy found on
        the target instead of being written again, and a mail read or starred at the source since an earlier pass.
        Nothing is ever removed here (the mirror does that), and only our own copies are touched (exact import id)."""
        session = self.targets[MAIL].session()
        if type(session).add_missing_flags is TargetSession.add_missing_flags:
            return  # this target does not do it (its flags are those written): the source is not read for it
        try:
            wanted = self._flags_wanted()
            totals = {"checked": 0, "fixed": 0, "not_kept": 0}
            if wanted:
                self.report["flags"] = totals
            for name, want in wanted.items():
                self.check()
                self.current_folder = f"Flags {name}"
                try:
                    checked, fixed, lacking = self._on_session(
                        session, lambda: session.add_missing_flags(name, self.pst_key, want))
                except ConnectorError as e:
                    msg = f"Flags of our mails in '{name}' not checked: {e}"
                    self.log(msg, "error")
                    self.problems.append(msg)
                    continue
                totals["checked"] += checked
                totals["fixed"] += fixed
                totals["not_kept"] += lacking
                if fixed:
                    self.log(f"Folder '{name}': flags of the source added to {fixed} of our {checked} mail(s) that "
                             "lacked them (read, answered, starred)")
                if lacking:
                    w = (f"Folder '{name}': the target did not keep the flags set on {lacking} mail(s): their read "
                         "state may differ from the source")
                    self.log(w, "warning")
                    self.report["warnings"].append(w)
        finally:
            session.close()
            self.current_folder = None

    def _flags_wanted(self) -> dict[str, dict[str, list[str]]]:
        """{target folder: {bare import id: flags its copy must carry}} for the mails of the folders of this pass."""
        folders = [f for f in self.folders if f.kind == MAIL]
        current: dict[str, dict[str, list[str]] | None] = {}  # folder key -> flags at the source now, None: unknown
        for f in folders:
            fk = folder_key(f.path)
            got = self._src_flags.get(fk)
            if got is None and self.mode != "mirror":  # mirror: its analysis has just recorded them (version)
                self.check()
                try:
                    got = self._on_source(lambda: self.source.flags_of(f))
                except ConnectorError as e:
                    self.log(f"Flags of '{fk}' unreadable at the source ({e}): those it was written with are used",
                             "warning")
            current[fk] = got
        for f in folders:  # rows of earlier imports may carry the old key; a folder key always wins
            current.setdefault(legacy_folder_key(f.path), current[folder_key(f.path)])
        wanted: dict[str, dict[str, list[str]]] = {}
        for r in db.q("SELECT item_key, folder, target, stamp, version FROM items WHERE account_id=? AND pst_key=? "
                      "AND kind='mail' AND status='ok' AND target IS NOT NULL", (self.account_id, self.pst_key)):
            if r["folder"] not in current:
                continue  # a folder outside this pass
            now = current[r["folder"]]
            flags = flags_to_add(now.get(r["item_key"]) if now is not None else mm.split_flags(r["version"]), ())
            if flags:
                ident = r["stamp"] or import_id_of(self.pst_key, r["item_key"])
                wanted.setdefault(r["target"], {})[ident.strip("<>")] = flags
        return wanted

    # ------------------------------------------------------------ checks
    def _check_target(self, session, phase: str, fkeys: list[str], target: str) -> dict:
        """Items of ours present in a target folder >= items marked as sent to it, whatever source folders they came
        from (`fkeys`: those mapped to it). Pre-existing content is not counted. Same report entries as the PST path."""
        fkey = fkeys[0]
        label = ", ".join(f"'{k}'" for k in fkeys)
        kind = CARRIED[phase]
        where = (self.account_id, self.pst_key, kind, target)
        n_ok = db.q1("SELECT COUNT(*) n FROM items WHERE account_id=? AND pst_key=? AND kind=? AND target=? AND "
                     "status='ok'", where)["n"]
        names = None
        rows = []
        if kind != MAIL:
            rows = db.q("SELECT hash, subject, warnings FROM items WHERE account_id=? AND pst_key=? AND kind=? AND "
                        "target=? AND status='ok' AND hash IS NOT NULL", where)
            names = {r["hash"] for r in rows}
            # one resource per name: the same UID in two source folders sent to one collection is one item there
            n_ok -= len(rows) - len({n.casefold() for n in names})
        try:
            present = self._on_session(session, lambda: session.count_ours(target, self.pst_key, names))
        except ConnectorError as e:
            present = None
            self.log(f"Cannot count {target}: {e}", "warning" if n_ok else "info")
        ok = (present is not None and present >= n_ok) or (present is None and n_ok == 0)  # nothing sent: nothing due
        if not ok and present is not None and names and hasattr(session, "missing_ours"):
            ok = self._explain_missing(session, label, target, rows, names, present, n_ok)
        entry = {"path": self._path_of(fkey), "folder": fkey, "target": target, "sent": n_ok, "present": present,
                 "ok": ok}
        if len(fkeys) > 1:
            entry["folders"] = list(fkeys)
        if phase == MAIL:
            self.report["folders"] = [x for x in self.report["folders"] if x["target"] != target] + [entry]
        else:
            dav = self.report["dav"].get(phase) or {}
            cols = {c["url"]: c for c in dav.get("collections") or []}
            cols[target] = {"url": target, "folders": list(fkeys), "sent": n_ok, "present": present, "ok": ok}
            rows = list(cols.values())
            self.report["dav"][phase] = {
                "url": rows[0]["url"], "sent": sum(c["sent"] for c in rows),
                "present": None if any(c["present"] is None for c in rows) else sum(c["present"] for c in rows),
                "ok": all(c["ok"] for c in rows), "collections": rows}
        if not ok:
            where = "" if phase == MAIL else f" -> {target}"
            self.log(f"Folder check {label}{where}: {present} present < {n_ok} sent", "error")
        return entry

    def _explain_missing(self, session, label: str, target: str, rows: list, names: set[str], present: int,
                         n_ok: int) -> bool:
        """A DAV collection holds fewer of our objects than were sent: which ones are missing, said in the log. When
        no more are missing than objects were sent there with the UID of another one (SAME_UID: a server that keeps
        one per UID keeps one of the two, either), the count is right for that server: True (said, not a
        verification problem)."""
        try:
            missing = self._on_session(session, lambda: session.missing_ours(target, names))
        except ConnectorError:
            return False
        if not missing:
            return False
        same = [w[len(SAME_UID):] for r in rows for w in (db.jload(r["warnings"], []) or [])
                if isinstance(w, str) and w.startswith(SAME_UID)]
        if same and len(missing) <= len(same) and present + len(same) >= n_ok:
            self.log(f"Folder check {label} -> {target}: {present} present for {n_ok} sent: {len(same)} object(s) "
                     f"share their UID with another one and the server keeps one per UID ({'; '.join(same[:5])})")
            return True
        by_name = {r["hash"]: r for r in rows}
        shown = ", ".join(f"'{by_name[n]['subject'] if n in by_name else ''}' ({n})" for n in sorted(missing)[:10])
        self.log(f"Folder check {label} -> {target}: missing on the target: {shown}"
                 + (f" and {len(missing) - 10} more" if len(missing) > 10 else ""), "warning")
        return False

    def verify_targets(self, phases: list[str]) -> None:
        self.set_phase("verify")
        problems: list[str] = []
        verify: dict = {}
        for phase in phases:
            kind = CARRIED[phase]
            target = self.targets[phase]
            session = target.session()
            try:
                rows = db.q("SELECT target, folder FROM items WHERE account_id=? AND pst_key=? AND kind=? "
                            "AND status='ok' GROUP BY target, folder ORDER BY target, folder",
                            (self.account_id, self.pst_key, kind))
                by_target: dict[str, list[str]] = {}
                for t in rows:
                    by_target.setdefault(t["target"], []).append(t["folder"])
                for target_name, fks in by_target.items():
                    self.check()
                    e = self._check_target(session, phase, fks, target_name)
                    if not e["ok"]:
                        problems.append(f"folder {', '.join(fks)}: {e['present']} present < {e['sent']} sent")
                if phase == MAIL and CAP_READBACK in target.capabilities:
                    self._verify_sample(session, verify, problems)
            except ConnectorError as e:
                problems.append(f"{phase} verification failed: {e}")
                self.log(f"{phase} verification failed: {e}", "error")
            finally:
                session.close()
        verify["problems"] = problems
        self.report["verify"] = verify

    def _verify_sample(self, session, verify: dict, problems: list[str]) -> None:
        """Sample re-read by exact import id + hash."""
        ids = [r["id"] for r in db.q("SELECT id FROM items WHERE account_id=? AND pst_key=? AND kind='mail' AND "
                                     "status='ok'", (self.account_id, self.pst_key))]
        checked, good, bad = 0, 0, []
        for iid in random.sample(ids, min(settings.verify_sample, len(ids))):
            self.check()
            it = db.q1("SELECT * FROM items WHERE id=?", (iid,))
            probe = OutgoingItem(MAIL, b"", it["stamp"] or import_id_of(self.pst_key, it["item_key"]))
            checked += 1
            try:
                found = self._on_session(session, lambda: session.find(it["target"], probe))
                raw = self._on_session(session, lambda: session.read(it["target"], found[0])) if found else None
            except ConnectorError as e:
                bad.append({"item_key": it["item_key"], "subject": it["subject"], "problem": f"re-read: {e}"})
                continue
            if not raw:
                bad.append({"item_key": it["item_key"], "subject": it["subject"], "problem": "not found on the target"})
            elif it["hash"] and norm_hash(raw) != it["hash"]:
                bad.append({"item_key": it["item_key"], "subject": it["subject"], "problem": "hash differs"})
            else:
                good += 1
        verify["sample"] = {"checked": checked, "ok": good, "problems": bad}
        if bad:
            problems.append(f"sample: {len(bad)}/{checked} non-conforming mail(s)")
        self.log(f"Sample re-read: {good}/{checked} conforming", "info" if not bad else "error")

