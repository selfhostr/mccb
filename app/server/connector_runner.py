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

from ..connectors import wiring
from ..connectors.base import (CAP_PARALLEL, CAP_READBACK, MAIL, ConnectorError, OutgoingItem, Target, stamp_import_id)
from . import crash, db, ordering
from .config import settings
from .runner import (KIND_OF_PHASE, AppendPool, AppendResult, AppendTask, JobFailed, JobRunner, PauseRequested,
                     StopRequested, folder_key, import_id_of, legacy_folder_key, norm_hash)
from .tuner import ErrorNote, classify_error

MAX_SOURCE_ERRORS = 5  # items in a row whose fetch fails after its retries: the source is down, not the items
DAV_EXT = {"calendar": "ics", "contacts": "vcf"}
PHASE_OF_KIND = {kind: phase for phase, kind in KIND_OF_PHASE.items()}


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
                return AppendResult(t.item_id, True, uid=uid, size=t.size)
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

    def _phase_enabled(self, kind: str) -> bool:
        phase = PHASE_OF_KIND[kind]
        return bool(self.acc[f"opt_{phase}"]) and phase in self.targets

    def _execute(self) -> None:
        if self.setup_error:
            raise JobFailed(self.setup_error)
        if not self.pst_key:
            raise JobFailed("no PST linked to the account" if self.source_kind == wiring.PST else "source not set")
        ctx = {"guard": _NativeGuard(self)}
        try:
            self.targets = wiring.targets_of_account(self.acc, self.srv)
            self.source = wiring.source_of_account(self.acc, ctx)
        except ConnectorError as e:
            raise JobFailed(str(e)) from e
        self.report["source"] = {"kind": self.source_kind, "namespace": self.pst_key}
        self.log(f"Route: {self.source_kind} to " + (self.acc["target_kind"] or "the server of the account"))
        try:
            if self.source.native_code:
                self._mark(phase="open")
            try:
                self._on_source(self.source.open)
                self.folders = self._on_source(self.source.folders)
            except ConnectorError as e:
                raise JobFailed(f"Source ({self.source_kind}): {e}") from e
            self._mark()
            phases = []
            for phase, kind in KIND_OF_PHASE.items():
                mine = [f for f in self.folders if f.kind == phase]
                if not mine or not self.acc[f"opt_{phase}"]:
                    continue
                if phase not in self.targets:
                    self.log(f"{len(mine)} {phase} folder(s) at the source, no target for them: not migrated", "warning")
                    continue
                phases.append(phase)
                self.totals[kind] += sum(int(f.count or 0) for f in mine)
            self.flush(force=True)
            self.precheck_targets(phases)
            for phase in phases:
                self._run_phase(phase)
            self.verify_targets(phases)
        finally:
            self.source.close()

    # ------------------------------------------------------------ precheck
    def precheck_targets(self, phases: list[str]) -> None:
        self.set_phase("precheck")
        self._free_bytes = None
        for phase in phases:
            session = self.targets[phase].session()
            try:
                self._on_session(session, session.ensure)
                folders = [f for f in self.folders if f.kind == phase]
                self.plans[phase] = self._on_session(session, lambda: session.plan_folders(folders))
                quota = self._on_session(session, session.quota)
                self.report["precheck"][phase] = {"ok": True, "quota": quota, **session.info()}
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
        rows = db.q("SELECT id, item_key, status, target, hash, error, source_ref FROM items WHERE account_id=? AND "
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
            probe = OutgoingItem(PHASE_OF_KIND[kind], b"", import_id_of(self.pst_key, r["item_key"]),
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

    def _list_folder(self, session, f, kind: str, fi: int, plan_name: str) -> list:
        """Entries (sort value, key, folder index, item) of a folder still to migrate."""
        fkey = folder_key(f.path)
        rows = self._rows(kind, fkey, f.path)
        self._resolve(session, kind, rows)
        self.current_folder = f"Listing {fkey}"
        self.flush()
        listing = self._on_source(lambda: list(self.source.iter_items(f)))
        self.totals[kind] += len(listing) - int(f.count or 0)
        f.count = len(listing)
        seen = {it.key for it in listing}
        # rows of keys the source no longer lists: the same messages may be back under other keys (UIDVALIDITY)
        orphans: dict[str, list[dict]] = {}
        for r in rows.values():
            if r["item_key"] not in seen and r["source_ref"]:
                orphans.setdefault(r["source_ref"], []).append(r)
        entries = []
        for it in listing:
            r = rows.get(it.key)
            old = None
            if r is None and it.alt_key and orphans.get(it.alt_key):
                old = r = orphans[it.alt_key].pop()
            if not self.wants(r):
                continue
            if old is not None:  # processed again under its new key (an `ok` one was never wanted)
                db.ex("DELETE FROM items WHERE id=? AND status<>'ok'", (old["id"],))
            ts = it.date.timestamp() if it.date is not None else None
            entries.append((-ts if ts is not None else ordering.NO_DATE, it.key, fi, it))
        # gone from the source before being written: nothing left to wait for
        for group in orphans.values():
            for r in group:
                if r["status"] in ("pending", "inflight"):
                    self.set_item(r["id"], "skipped", "no longer at the source")
        for r in rows.values():
            if r["item_key"] not in seen and not r["source_ref"] and r["status"] in ("pending", "inflight"):
                self.set_item(r["id"], "skipped", "no longer at the source")
        self.log(f"Folder '{fkey}' -> {plan_name}: {len(listing)} item(s), {len(entries)} to migrate")
        return entries

    def _run_phase(self, phase: str) -> None:
        self.set_phase(phase)
        kind = KIND_OF_PHASE[phase]
        target = self.targets[phase]
        plan = self.plans[phase]
        session = target.session()
        pool = None
        try:
            folders = [f for f in self.folders if f.kind == phase]
            for f in folders:
                fk = folder_key(f.path)
                try:
                    self._on_session(session, lambda: session.ensure_folder(plan[fk].name, plan[fk].flag))
                except ConnectorError as e:
                    if e.kind != ConnectorError.REJECTED:
                        raise JobFailed(f"Target ({phase}): {e}") from e
                    # refused: only the items of this folder are in error, the mailbox continues
                    self.folder_errors[fk] = f"folder '{plan[fk].name}' not created: {e}"
                    self.log(f"Folder '{fk}': {self.folder_errors[fk]}", "error")
            parallel = CAP_PARALLEL in target.capabilities
            pool = TargetPool(target, self.connections if parallel else 1, self._should_stop)
            self._tune_t0 = None
            entries: list = []
            todo = [f for f in folders if self._folder_has_work(kind, f)]
            for f in todo:
                self.check()
                fk = folder_key(f.path)
                try:
                    listed = self._list_folder(session, f, kind, self._folder_index(f), plan[fk].name)
                except ConnectorError as e:
                    raise JobFailed(f"Source ({self.source_kind}): {e}") from e
                if fk in self.folder_errors:
                    for _sv, key, _fi, it in listed:
                        self.upsert_item(kind, key, fk, "error", target=plan[fk].name, error=self.folder_errors[fk],
                                         source_ref=it.alt_key)
                    continue
                entries.extend(listed)
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
            self._mark()
            self.check()
            for f in folders:
                fk = folder_key(f.path)
                if fk not in self.folder_errors:
                    self._check_target(session, phase, fk, plan[fk].name)
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
            raw, name = got.raw, got.name or f"{digest}.{DAV_EXT[phase]}"
            mark = name  # as on the PST path: the hash column of a DAV item is its resource name
        warnings = db.jdump(got.warnings) if got.warnings else None
        if got.warnings:
            self.log(f"Warning '{got.subject}': {', '.join(got.warnings)}", "warning")
        item_id = self.upsert_item(kind, key, fkey, "inflight", target=target, subject=got.subject, hash=mark,
                                   size=len(raw), warnings=warnings, source_ref=it.alt_key)
        self._submit(pool, TargetTask(item_id, target, raw, list(got.flags), date, len(raw), import_id, kind=phase,
                                      name=name))

    # ------------------------------------------------------------ checks
    def _check_target(self, session, phase: str, fkey: str, target: str) -> dict:
        """Items of ours present in a target folder >= items marked as sent to it. Pre-existing content is not
        counted. Same report entries as the PST path."""
        kind = KIND_OF_PHASE[phase]
        where = (self.account_id, self.pst_key, kind, target)
        n_ok = db.q1("SELECT COUNT(*) n FROM items WHERE account_id=? AND pst_key=? AND kind=? AND target=? AND "
                     "status='ok'", where)["n"]
        names = None
        if kind != MAIL:
            names = {r["hash"] for r in db.q("SELECT hash FROM items WHERE account_id=? AND pst_key=? AND kind=? AND "
                                             "target=? AND status='ok' AND hash IS NOT NULL", where)}
        try:
            present = self._on_session(session, lambda: session.count_ours(target, self.pst_key, names))
        except ConnectorError as e:
            present = None
            self.log(f"Cannot count {target}: {e}", "warning")
        ok = present is not None and present >= n_ok
        entry = {"path": self._path_of(fkey), "folder": fkey, "target": target, "sent": n_ok, "present": present,
                 "ok": ok}
        if phase == MAIL:
            self.report["folders"] = [x for x in self.report["folders"] if x["target"] != target] + [entry]
        else:
            self.report["dav"][phase] = {"url": target, "sent": n_ok, "present": present, "ok": ok}
        if not ok:
            self.log(f"Folder check '{fkey}': {present} present < {n_ok} sent", "error")
        return entry

    def verify_targets(self, phases: list[str]) -> None:
        self.set_phase("verify")
        problems: list[str] = []
        verify: dict = {}
        for phase in phases:
            kind = KIND_OF_PHASE[phase]
            target = self.targets[phase]
            session = target.session()
            try:
                rows = db.q("SELECT target, MIN(folder) folder FROM items WHERE account_id=? AND pst_key=? AND kind=? "
                            "AND status='ok' GROUP BY target", (self.account_id, self.pst_key, kind))
                for t in rows:
                    self.check()
                    e = self._check_target(session, phase, t["folder"], t["target"])
                    if not e["ok"]:
                        problems.append(f"folder {t['folder']}: {e['present']} present < {e['sent']} sent")
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
            probe = OutgoingItem(MAIL, b"", import_id_of(self.pst_key, it["item_key"]))
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

