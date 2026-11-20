"""Mirror mode: one more pass after a migration that makes the target follow the source. It adds what is new, deletes
on the target what disappeared from the source, and updates what changed there (mail flags and folder, events,
contacts and tasks by their version). See docs/planning.md "Mirror".

Safety, in the order it is applied:
- the source is reached through ReadOnlySource: only reads exist on it, a write cannot even be called;
- only items of the `items` table with status `ok` (written by mccb) are ever touched on the target, found again by
  their import id or resource name (TargetSession.inventory): native content and later arrivals are never in it;
- a deletion needs a COMPLETE and successful listing of the folder in this pass; one listing error or one incomplete
  listing anywhere in the pass and nothing is deleted, moved or given up at all (`source_unlistable`);
- keys reset (UIDVALIDITY) refuse the folder (`uidvalidity_changed`), a folder that lists nothing while it had
  migrated items refuses its deletions (`source_empty`), more than `max_delete_percent` % of the items tracked in the
  folders listed and accepted in this pass refuses them too (`too_many_deletes`), unless `allow_large_delete`;
- then the source is asked key by key (Source.exists): only an explicit "gone" deletes, moves or gives up an item;
- a resource of the target that is the copy of several rows (same DAV name) is deleted only when all of them go;
- deleted mails go to the trash of the target (or are flagged, never expunged); a target that can only delete for
  good gets a copy kept first under <data>/mirror-trash/ (or deletes nothing when it cannot give one back);
- the dry run (the default of the API) uses read-only wrappers on both sides.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any

from ..connectors import registry, wiring
from ..connectors.base import (CAP_INVENTORY, CAP_MIRROR, CAP_MOVE, CAP_READBACK, DELETE_HARD, DELETE_SOFT, MAIL,
                               MIRROR_DELETED_FLAG, READ_ONLY_SOURCE_METHODS, TASKS, UNSTABLE_EPOCHS, ConnectorError,
                               Listing, RemoteItem, SourceFolder, SourceItem)
from . import crash, db
from . import policy as pol

PHASES = ("mail", "calendar", "contacts", TASKS)
ROW_KIND = {"mail": "mail", "calendar": "event", "contacts": "contact", TASKS: "task"}  # items.kind of each phase
SYSTEM_FLAGS = {"\\seen": "\\Seen", "\\answered": "\\Answered", "\\flagged": "\\Flagged", "\\draft": "\\Draft"}
SAMPLE_MAX = 20
SUBJECT_MAX = 80
# blocked codes (API contract)
MIRROR_OFF, SOURCE_UNLISTABLE, TOO_MANY_DELETES, SOURCE_EMPTY = "mirror_off", "source_unlistable", "too_many_deletes", "source_empty"
NO_PRIOR_RUN, UIDVALIDITY_CHANGED, TARGET_UNREACHABLE = "no_prior_run", "uidvalidity_changed", "target_unreachable"
JOB_ACTIVE = "job_active"  # a job of the mailbox is queued or running: no mirror job is created next to it
UNKNOWN_ACCOUNT = "unknown_account"
FATAL_UNKNOWN = {"code": UNKNOWN_ACCOUNT, "message": "unknown mailbox"}
FATAL = {MIRROR_OFF, NO_PRIOR_RUN, TARGET_UNREACHABLE, UNKNOWN_ACCOUNT}  # nothing at all is done
# notes (API contract): no block, what the pass will do that the user should know
SOFT_DELETE, HARD_DELETE, NO_COPY_KEPT, SHARED_RESOURCE = "soft_delete", "hard_delete", "no_copy_kept", "shared_resource"
TARGET_MODIFIED, UNVERIFIED_OWNER = "target_modified", "unverified_owner"
GAVE_UP = "no longer at the source"  # items.error of a row given up: it holds no resource of anyone
ACTIVE_STATUSES = ("queued", "running", "paused", "stopping")


@dataclass
class Options:
    max_delete_percent: int = 20
    allow_large_delete: bool = False
    # the percentage applies only above this many deletions of a kind (None: MIRROR_MIN_DELETE_COUNT, 10)
    min_delete_count: int | None = None
    allow_empty_folders: list[str] = field(default_factory=list)  # folder keys whose empty listing is accepted

    def __post_init__(self):
        if self.min_delete_count is None:
            from .config import settings
            self.min_delete_count = settings.mirror_min_delete_count
        self.allow_empty_folders = [str(k) for k in (self.allow_empty_folders or [])]

    def view(self) -> dict:
        return {"max_delete_percent": self.max_delete_percent, "allow_large_delete": self.allow_large_delete,
                "min_delete_count": self.min_delete_count, "allow_empty_folders": list(self.allow_empty_folders)}

    @classmethod
    def of(cls, d: dict | None) -> "Options":
        d = d or {}
        n = d.get("min_delete_count")
        return cls(int(d.get("max_delete_percent", 20)), bool(d.get("allow_large_delete", False)),
                   int(n) if n is not None else None, list(d.get("allow_empty_folders") or []))


# ------------------------------------------------------------------ read-only wrappers
class ReadOnlySource:
    """The source as the mirror sees it: READ_ONLY_SOURCE_METHODS and nothing else (AttributeError otherwise)."""

    def __init__(self, source):
        object.__setattr__(self, "_src", source)

    def __getattr__(self, name: str):
        if name in READ_ONLY_SOURCE_METHODS or name == "native_code":
            return getattr(self._src, name)
        raise AttributeError(f"the mirror only reads the source: '{name}' is not allowed")

    def __setattr__(self, name, value):
        raise AttributeError("the mirror only reads the source")


TARGET_READS = frozenset({"connected", "connect", "close", "ensure", "inventory", "delete_mode", "info", "has_folder"})


class ReadOnlyTarget:
    """A target session for the dry run: listing only."""

    def __init__(self, session):
        object.__setattr__(self, "_s", session)

    def __getattr__(self, name: str):
        if name in TARGET_READS:
            return getattr(self._s, name)
        raise AttributeError(f"dry run: '{name}' would write to the target")


# ------------------------------------------------------------------ helpers
def canon_flags(flags) -> str:
    """Flags as stored in items.version for a mail: system flags in their usual case, sorted, our mark left out."""
    out = set()
    for f in flags or []:
        low = f.lower()
        if low in SYSTEM_FLAGS:
            out.add(SYSTEM_FLAGS[low])
        elif low in ("\\recent", "\\deleted") or f == MIRROR_DELETED_FLAG:
            continue
        else:
            out.add(f)
    return " ".join(sorted(out))


def split_flags(text: str | None) -> set[str]:
    return set((text or "").split())


def version_of(phase: str, it: SourceItem) -> str | None:
    """items.version of a listed item."""
    return canon_flags(it.flags) if phase == MAIL else it.version


def ref_json(ref) -> str | None:
    """items.src_ref: the SourceItem.ref as JSON, None when it is not JSON (it is then never given to exists())."""
    if ref is None:
        return None
    try:
        text = json.dumps(ref)
    except (TypeError, ValueError):
        return None
    return text if len(text) <= 4000 else None


def ref_of(text: str | None):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def unstable(epoch: str | None) -> bool:
    return bool(epoch) and any(m in epoch for m in UNSTABLE_EPOCHS)


def idents_of(phase: str, pst_key: str, row: dict) -> list[str]:
    """How the target may know one of our items, in the order tried against inventory(): a mail by its import id
    (the one it still carries after a move); an event, contact or task by its resource name (DAV-like targets) and
    by its import id (targets that mark items with it: EWS, Graph, JMAP), the resource name possibly coming from
    the source and not from the import id."""
    from .runner import import_id_of
    if phase == MAIL:
        return [row.get("stamp") or import_id_of(pst_key, row["item_key"])]
    out = [row["hash"]] if row.get("hash") else []
    iid = row.get("stamp") or import_id_of(pst_key, row["item_key"])
    return out + [iid] if iid not in out else out


def ident_of(phase: str, pst_key: str, row: dict) -> str:
    """The first of idents_of(): the one used when nothing was matched on the target."""
    return idents_of(phase, pst_key, row)[0]


def wants(row: dict | None) -> bool:
    """Item to (re)write, as a normal run decides (see JobRunner.wants)."""
    if row is None:
        return True
    st = row["status"]
    err = row.get("error") or ""
    if st == "error" and (err.startswith(crash.CRASH_MSG) or err.startswith(pol.OVERSIZE_PREFIX)):
        return False
    return st not in ("ok", "skipped", pol.FILTERED)


def short(text: str | None) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= SUBJECT_MAX else t[:SUBJECT_MAX - 1] + "…"


def counters() -> dict:
    return {"added": 0, "updated": 0, "deleted": 0, "skipped_by_safety": 0, "errors": 0}


def zero() -> dict:
    return {p: 0 for p in PHASES}


@dataclass
class Action:
    action: str                      # add | delete | flags | record | move | update
    phase: str
    row: dict | None = None          # items row (all but add)
    folder: SourceFolder | None = None  # source folder of the item (add, update, move: the new one)
    item: SourceItem | None = None   # listed item (all but delete)
    refs: list[RemoteItem] = field(default_factory=list)
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    ident: str = ""                  # the key of inventory() the item was found under (import id or resource name)
    orig: "Action | None" = None     # move: the add it replaced (done instead when the target has no copy to move)

    @property
    def counted(self) -> bool:
        return self.action != "record"


@dataclass
class Analysis:
    blocked: list[dict] = field(default_factory=list)
    actions: dict[str, list[Action]] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)       # deletions held back by a safety rule
    gone_unfinished: list[dict] = field(default_factory=list)   # pending / inflight / error rows confirmed gone
    refused: set[tuple[str, str]] = field(default_factory=set)  # (phase, folder key) left alone
    notes: list[dict] = field(default_factory=list)             # what the pass will do that the user should know
    held: dict[str, dict[str, int]] = field(default_factory=dict)  # phase -> {reason: deletions/updates held}
    # candidates, settled by _confirm_gone: unfinished rows no longer listed, moves (the add and the old row)
    unfinished: list[tuple[str, dict]] = field(default_factory=list)
    moves: list[tuple[str, Action, dict]] = field(default_factory=list)
    rows: dict[str, list[dict]] = field(default_factory=dict)   # phase -> every row of its kind (shared resources)
    backfill: list[tuple[int, str]] = field(default_factory=list)  # (row id, src_ref) to record (job only)
    gone_ids: dict[str, set[int]] = field(default_factory=dict)  # phase -> ok rows no longer listed (before safety)

    def block(self, code: str, message: str, **extra) -> None:
        if not any(b["code"] == code and b["message"] == message for b in self.blocked):
            self.blocked.append({"code": code, "message": message, **extra})

    def hold(self, phase: str, reason: str, n: int = 1) -> None:
        if n:
            by = self.held.setdefault(phase, {})
            by[reason] = by.get(reason, 0) + n
            self.skipped[phase] = self.skipped.get(phase, 0) + n

    def note(self, code: str, message: str) -> None:
        if not any(n["code"] == code and n["message"] == message for n in self.notes):
            self.notes.append({"code": code, "message": message})

    def drop_deletes(self) -> None:
        """No deletion at all in this pass: every one counted as held, every move back to a plain add."""
        for phase, acts in self.actions.items():
            n = sum(1 for a in acts if a.action == "delete")
            self.skipped[phase] = self.skipped.get(phase, 0) + n
            self.actions[phase] = [a for a in acts if a.action != "delete"]
        for phase, add, _row in self.moves:
            self.actions.setdefault(phase, []).append(add)
        self.moves = []
        self.unfinished = []

    @property
    def codes(self) -> set[str]:
        return {b["code"] for b in self.blocked}

    @property
    def fatal(self) -> bool:
        return bool(self.codes & FATAL)

    def of(self, phase: str, *names: str) -> list[Action]:
        return [a for a in self.actions.get(phase, []) if a.action in names]


def preflight(acc) -> list[dict]:
    """Blocked codes known without any connection."""
    out = []
    if not (acc["opt_mirror"] if "opt_mirror" in acc.keys() else 0):
        out.append({"code": MIRROR_OFF, "message": "mirror mode is not enabled for this mailbox"})
    kind = wiring.source_kind(acc)
    try:
        caps = registry.role_of(kind, "source").capabilities if kind != wiring.PST else frozenset()
    except ConnectorError:
        caps = frozenset()
    if CAP_MIRROR not in caps:
        out.append({"code": SOURCE_UNLISTABLE,
                    "message": f"the source '{kind}' cannot be listed completely: the mirror does not support it"})
    done = db.q1("SELECT 1 FROM jobs WHERE account_id=? AND status IN ('done','done_with_errors') LIMIT 1", (acc["id"],))
    if done is None:
        out.append({"code": NO_PRIOR_RUN, "message": "never migrated: run a first full pass before mirroring"})
    return out


# ------------------------------------------------------------------ analysis (read only, shared by dry run and job)
def analyze(source, folders: list[SourceFolder], known_folders: set[str], sessions: dict[str, Any],
            capabilities: dict[str, frozenset], account_id: int, pst_key: str, policy: pol.Policy, opts: Options,
            retry=None, retry_target=None, log=None, source_keys: set[str] | None = None,
            owners: set[str] | None = None) -> Analysis:
    """What a mirror pass would do. `source` is a ReadOnlySource, `folders` the source folders to mirror (exclusions
    applied), `known_folders` the keys of every folder the source still has (excluded ones too, old keys too),
    `source_keys` the folder_key() of every folder the source has (None: those of `folders`), `sessions` {phase:
    connected target session}, `owners` the phases whose target proves an item is mccb's by the etag of mccb's
    write (DAV: Target.names_resources). Reads only: the source, the items table and the inventory of the
    target."""
    from .runner import folder_key, legacy_folder_key
    retry = retry or (lambda fn: fn())
    retry_target = retry_target or (lambda session, fn: fn())
    log = log or (lambda msg, level="info": None)
    if source_keys is None:
        source_keys = {folder_key(f.path) for f in folders}
    an = Analysis()
    listing_failed = False
    for phase in PHASES:
        if phase not in sessions:
            continue
        kind = ROW_KIND[phase]
        an.actions[phase] = []
        an.skipped[phase] = 0
        rows_all = [dict(r) for r in db.q(
            "SELECT id, item_key, folder, target, status, error, hash, source_ref, version, stamp, subject, src_ref, "
            "target_etag "
            "FROM items "
            "WHERE account_id=? AND pst_key=? AND kind=?", (account_id, pst_key, kind))]
        an.rows[phase] = rows_all
        by_folder: dict[str, dict[str, dict]] = {}
        for r in rows_all:
            by_folder.setdefault(r["folder"], {})[r["item_key"]] = r
        mine = [f for f in folders if f.kind == phase]
        listed_keys: set[str] = set()
        tracked = 0           # ok rows of the folders listed and accepted in this pass: the base of the threshold
        adds: list[Action] = []
        per_folder: list[tuple[str, int, int, list[dict]]] = []  # (folder key, ok rows, listed, gone ok rows)
        for f in mine:
            fk, lk = folder_key(f.path), legacy_folder_key(f.path)
            rows = dict(by_folder.get(fk, {}))
            if lk != fk and lk not in source_keys:
                # rows of an earlier import under the old key ("/".join); never when that old key is the key of
                # another folder of the source ("A/B" the name and B inside A): those rows are that folder's
                rows = {**by_folder.get(lk, {}), **rows}
            listed_keys |= {fk, lk}
            try:
                listing: Listing = retry(lambda: source.mirror_listing(f))
            except ConnectorError as e:
                listing_failed = True
                an.refused.add((phase, fk))
                an.block(SOURCE_UNLISTABLE, f"{phase} folder '{fk}' could not be listed: {e}")
                log(f"Mirror: {phase} folder '{fk}' could not be listed ({e}): no deletion in this pass", "error")
                continue
            if not listing.complete:
                listing_failed = True
                an.block(SOURCE_UNLISTABLE, f"{phase} folder '{fk}' listed incompletely"
                         + (f": {listing.problem}" if listing.problem else ""))
                log(f"Mirror: {phase} folder '{fk}' listed incompletely: no deletion in this pass", "error")
            ok_rows = [r for r in rows.values() if r["status"] == "ok"]
            shaky = sorted({e for e in [listing.epoch] + [source.key_epoch(r["item_key"]) for r in ok_rows] if unstable(e)})
            if shaky:
                an.refused.add((phase, fk))
                an.block(UIDVALIDITY_CHANGED, f"{phase} folder '{fk}': the keys of the source cannot be trusted "
                         f"({', '.join(shaky)}): run a full pass")
                log(f"Mirror: folder '{fk}' refused, keys not trusted ({', '.join(shaky)})", "error")
                continue
            if listing.epoch:
                epochs = {e for e in (source.key_epoch(r["item_key"]) for r in ok_rows) if e is not None}
                if epochs and epochs != {listing.epoch}:
                    an.refused.add((phase, fk))
                    an.block(UIDVALIDITY_CHANGED, f"{phase} folder '{fk}': the keys of the source were reset "
                             f"(UIDVALIDITY {', '.join(sorted(epochs))} -> {listing.epoch}): run a full pass")
                    log(f"Mirror: folder '{fk}' refused, UIDVALIDITY changed: run a full pass", "error")
                    continue
            tracked += len(ok_rows)
            seen = set()
            for it in listing.items:
                seen.add(it.key)
                r = rows.get(it.key)
                if wants(r):
                    if policy.has_filters and kind != "contact" and policy.reason(it.date, it.size if phase == MAIL else None):
                        continue  # left out by a filter of the policy: not added, as in a normal run
                    adds.append(Action("add", phase, row=r, folder=f, item=it))
                elif r["status"] == "ok":
                    if not r.get("src_ref") and ref_json(it.ref):
                        # migrated before src_ref was kept: recorded now, so that exists() can confirm it later
                        an.backfill.append((r["id"], ref_json(it.ref)))
                    if phase == MAIL:
                        src = canon_flags(it.flags)
                        if r["version"] is None:
                            # no recorded flags (migrated by an earlier version): compared with the target
                            an.actions[phase].append(Action("record", phase, row=r, folder=f, item=it))
                        elif r["version"] != src:
                            old, new = split_flags(r["version"]), split_flags(src)
                            an.actions[phase].append(Action("flags", phase, row=r, folder=f, item=it,
                                                            add=sorted(new - old), remove=sorted(old - new)))
                    elif it.version is not None and r["version"] != it.version:
                        an.actions[phase].append(Action("update", phase, row=r, folder=f, item=it))
                    elif phase in (owners or ()) and not r.get("target_etag"):
                        # written before etags were kept: the job compares the target with the source, and keeps
                        # the etag when they hold the same object (adopted); until then it is never touched
                        an.actions[phase].append(Action("adopt", phase, row=r, folder=f, item=it))
            gone_f = []
            for r in rows.values():
                if r["item_key"] in seen:
                    continue
                if r["status"] == "ok":
                    gone_f.append(r)
                elif r["status"] in ("pending", "inflight", "error"):
                    an.unfinished.append((phase, r))  # given up only when exists() confirms it is gone
            per_folder.append((fk, len(ok_rows), len(listing.items), gone_f))
        an.gone_ids[phase] = {r["id"] for _fk, _n, _l, g in per_folder for r in g}
        # tracked items of folders the source no longer lists: never deleted (no listing confirms it)
        vanished = [r for r in rows_all if r["status"] == "ok" and r["folder"] not in listed_keys
                    and r["folder"] not in known_folders]
        if vanished:
            an.skipped[phase] += len(vanished)
            names = sorted({r["folder"] for r in vanished})
            an.block(SOURCE_UNLISTABLE, f"{phase}: folder(s) no longer at the source (deleted or renamed): "
                     f"{', '.join(names[:5])}{' ...' if len(names) > 5 else ''}; their {len(vanished)} item(s) are "
                     "not deleted on the target")
            log(f"Mirror: {len(vanished)} {phase} item(s) in folder(s) the source no longer has: not deleted "
                "(no listing of the folder confirms it)", "warning")
        # moves: same Message-ID gone from one folder, new in another (one to one). Only candidates here: applied
        # after the source has confirmed that the old key is gone (_confirm_gone), else a plain add
        paired: set[int] = set()
        if phase == MAIL and CAP_MOVE in capabilities.get(phase, frozenset()):
            pool: dict[str, list[dict]] = {}
            for _fk, _n, _l, gone_f in per_folder:
                for r in gone_f:
                    if (r["source_ref"] or "").startswith("m:"):
                        pool.setdefault(r["source_ref"], []).append(r)
            kept = []
            for a in adds:
                cands = [r for r in pool.get(a.item.alt_key or "") or [] if r["folder"] != folder_key(a.folder.path)]
                if a.item.alt_key and cands:
                    r = cands[0]
                    pool[a.item.alt_key].remove(r)
                    paired.add(r["id"])
                    an.moves.append((phase, a, r))
                else:
                    kept.append(a)
            adds = kept
        an.actions[phase].extend(adds)
        # safety on deletions: per folder (a folder that lists nothing), then for the kind
        dels: list[Action] = []
        held = 0
        for fk, n_ok, n_listed, gone_f in per_folder:
            gone_f = [r for r in gone_f if r["id"] not in paired]
            if gone_f and n_listed == 0 and n_ok and not opts.allow_large_delete and fk not in opts.allow_empty_folders:
                msg = (f"{phase} folder '{fk}': the source lists nothing while {n_ok} item(s) were migrated; if it "
                       f"was emptied on purpose, run again with allow_empty_folders: [\"{fk}\"]")
                an.block(SOURCE_EMPTY, msg, kind=phase, folder=fk)
                held += len(gone_f)
                log(f"Mirror: deletions held back, {msg}", "warning")
                continue
            dels.extend(Action("delete", phase, row=r) for r in gone_f)
        if (dels and tracked and len(dels) > opts.min_delete_count
                and len(dels) * 100 > opts.max_delete_percent * tracked and not opts.allow_large_delete):
            need = -(-len(dels) * 100 // tracked)  # the smallest whole percentage that lets them through
            msg = (f"{phase}: {len(dels)} of {tracked} migrated item(s) of the folders mirrored would be deleted "
                   f"(more than {opts.max_delete_percent} %); if that is expected, run again with "
                   f"max_delete_percent: {need} or allow_large_delete: true")
            an.block(TOO_MANY_DELETES, msg, kind=phase, needed_percent=need)
            held += len(dels)
            log(f"Mirror: deletions held back, {msg}", "warning")
            dels = []
        an.skipped[phase] += held
        an.actions[phase].extend(dels)
    if listing_failed:  # one error anywhere in the pass: nothing is deleted, moved or given up at all
        an.drop_deletes()
    else:
        _confirm_gone(an, source, folders, retry, log, source_keys)
    _inventory(an, sessions, capabilities, pst_key, retry_target, log, owners or set())
    _shared_resources(an, log)
    _delete_notes(an, sessions, capabilities, log)
    return an


def _confirm_gone(an: Analysis, source, folders: list[SourceFolder], retry, log,
                  source_keys: set[str] | None = None) -> None:
    """Last check before a deletion, a move or giving up an unfinished item: the source is asked directly, key by
    key (Source.exists), whether each item the listing no longer has is really gone. Only an explicit "gone" (False)
    counts: still there, left out of the answer, or no folder to ask in, it is kept. An error: nothing deleted."""
    from .runner import folder_key, legacy_folder_key
    if source_keys is None:
        source_keys = {folder_key(f.path) for f in folders}
    by_key: dict[tuple[str, str], SourceFolder] = {}
    for f in folders:
        lk = legacy_folder_key(f.path)
        if lk not in source_keys:
            by_key[(f.kind, lk)] = f
    for f in folders:  # a folder key always wins over the old key of another folder
        by_key[(f.kind, folder_key(f.path))] = f
    asked: dict[tuple[str, str], dict[int, dict]] = {}  # (phase, folder) -> {row id: row}
    for phase, acts in an.actions.items():
        for a in acts:
            if a.action == "delete":
                asked.setdefault((phase, a.row["folder"]), {})[a.row["id"]] = a.row
    for phase, _add, r in an.moves:
        asked.setdefault((phase, r["folder"]), {})[r["id"]] = r
    for phase, r in an.unfinished:
        asked.setdefault((phase, r["folder"]), {})[r["id"]] = r
    gone: set[int] = set()
    for (phase, fk), group in asked.items():
        f = by_key.get((phase, fk))
        if f is None:
            log(f"Mirror: {phase} folder '{fk}' is not in this pass: its {len(group)} item(s) are kept", "warning")
            continue
        rows = list(group.values())
        keys = [r["item_key"] for r in rows]
        refs = {r["item_key"]: ref_of(r.get("src_ref")) for r in rows}
        try:
            there = retry(lambda: source.exists(f, keys, refs))
        except ConnectorError as e:
            an.block(SOURCE_UNLISTABLE, f"{phase} folder '{fk}': the source could not confirm the deletions: {e}")
            log(f"Mirror: the source could not confirm the deletions in '{fk}' ({e}): no deletion in this pass",
                "error")
            an.drop_deletes()
            return
        there = there or {}
        for r in rows:
            if there.get(r["item_key"], True) is False:
                gone.add(r["id"])
    for phase, acts in an.actions.items():
        held = [a for a in acts if a.action == "delete" and a.row["id"] not in gone]
        for a in held:
            why = ("not confirmed gone at the source" if a.row.get("src_ref") else
                   "confirmation impossible: unknown source reference (migrated before it was recorded; it is "
                   "recorded when the item is listed again)")
            log(f"Mirror: {phase} key={a.row['item_key']} not listed but {why}: kept", "warning")
        if held:
            an.skipped[phase] = an.skipped.get(phase, 0) + len(held)
            ids = {id(a) for a in held}
            an.actions[phase] = [a for a in acts if id(a) not in ids]
    for phase, add, r in an.moves:
        if r["id"] in gone:
            an.actions.setdefault(phase, []).append(Action("move", phase, row=r, folder=add.folder, item=add.item,
                                                           orig=add))
        else:  # the old copy is still at the source: the new one is a copy, the old one stays
            an.actions.setdefault(phase, []).append(add)
    an.moves = []
    an.gone_unfinished = [r for _p, r in an.unfinished if r["id"] in gone]
    an.unfinished = []


def _inventory(an: Analysis, sessions: dict, capabilities: dict, pst_key: str, retry_target, log,
               owners: set[str] = frozenset()) -> None:
    """Finds our items on the target for every action that touches one: the refs, the flags of mails written before
    versions were kept. An item no longer on the target is left alone (delete: just recorded), except an update:
    written again (added), whatever removed it from the target."""
    for phase, acts in an.actions.items():
        touching = [a for a in acts if a.action != "add"]
        if not touching:
            continue
        if CAP_INVENTORY not in capabilities.get(phase, frozenset()):
            an.block(TARGET_UNREACHABLE, f"{phase}: the target cannot list its items for the mirror")
            return
        session = sessions[phase]
        by_target: dict[str, list[Action]] = {}
        for a in touching:
            by_target.setdefault(a.row["target"] or "", []).append(a)
        for target, group in by_target.items():
            names = None if phase == MAIL else {i for a in group for i in idents_of(phase, pst_key, a.row)}
            try:
                inv = retry_target(session, lambda: session.inventory(target, pst_key, names))
            except ConnectorError as e:
                an.block(TARGET_UNREACHABLE, f"{phase}: cannot list '{target}' on the target: {e}")
                log(f"Mirror: cannot list '{target}' on the target: {e}", "error")
                return
            for a in group:
                idents = idents_of(phase, pst_key, a.row)
                a.ident = idents[0]
                for ident in idents:
                    if inv.get(ident):
                        a.refs, a.ident = list(inv[ident]), ident
                        break
                if a.action == "record" and a.refs:
                    # written before versions were kept: compared with what the target holds
                    have, want = split_flags(canon_flags(a.refs[0].flags)), split_flags(canon_flags(a.item.flags))
                    add = sorted(want - have)
                    remove = sorted((have & set(SYSTEM_FLAGS.values())) - want)
                    if add or remove:
                        a.action, a.add, a.remove = "flags", add, remove
                elif a.action == "update" and not a.refs:
                    # gone from the target (an update that failed half-way, a deletion by hand): written again
                    log(f"Mirror: {phase} key={a.row['item_key']} changed at the source and no longer on the "
                        "target: added again", "warning")
                    a.action = "add"
                elif a.action == "move" and not a.refs and a.orig is not None:
                    # nothing to move on the target (deleted there by hand): the mail is added to its new folder
                    log(f"Mirror: {phase} key={a.row['item_key']} moved at the source, no copy on the target: added "
                        "to its new folder", "warning")
                    acts_p = an.actions[phase]
                    acts_p[next(i for i, x in enumerate(acts_p) if x is a)] = a.orig
    for phase in an.actions:
        # nothing to do on an item the target no longer has, except recording a deletion
        an.actions[phase] = [a for a in an.actions[phase]
                             if a.action in ("add", "delete") or a.refs or a.action == "record"]
    _target_modified(an, owners, log)


def same_object(a: bytes, b: bytes) -> bool:
    """Two iCalendar / vCard texts hold the same object: the same unfolded lines, in any order (servers reorder
    properties), line endings aside."""
    def lines(raw: bytes) -> list[str]:
        text = raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\n ", "").replace("\n\t", "")
        return sorted(x.strip() for x in text.split("\n") if x.strip())
    return lines(a) == lines(b)


def _norm_etag(v: str | None) -> str:
    v = (v or "").strip()
    return v[2:] if v.startswith("W/") else v


def _target_modified(an: Analysis, owners: set[str], log) -> None:
    """On a target that proves its items by their etag (`owners`, DAV), an event, contact or task is deleted or
    updated only when the etag the inventory sees is the one of mccb's own write (items.target_etag):
    - another etag: changed on the target since (target_modified);
    - none kept, or kept with "!" (written over a resource that was already there, owner unknown), or no etag in
      the inventory: mccb cannot show it is its own (unverified_owner).
    Either way it is neither deleted nor overwritten, counted in skipped_by_safety and in `held`."""
    for phase, acts in an.actions.items():
        if phase == MAIL:
            continue
        out = []
        modified = unverified = 0
        for a in acts:
            mine = (a.row or {}).get("target_etag")
            seen = a.refs[0].version if a.refs else None
            verb = "deleted" if a.action == "delete" else "overwritten"
            if a.action in ("delete", "update") and a.refs and phase in owners:
                if not mine or mine.startswith("!") or not seen:
                    unverified += 1
                    log(f"Mirror: {phase} key={a.row['item_key']}: mccb cannot show the item on the target is the "
                        f"one it wrote (no etag of its write): not {verb}", "warning")
                    continue
                if _norm_etag(seen) != _norm_etag(mine):
                    modified += 1
                    log(f"Mirror: {phase} key={a.row['item_key']} was changed on the target since mccb wrote it: not "
                        f"{verb}", "warning")
                    continue
            out.append(a)
        an.actions[phase] = out
        an.hold(phase, TARGET_MODIFIED, modified)
        an.hold(phase, UNVERIFIED_OWNER, unverified)
        if modified:
            an.note(TARGET_MODIFIED, f"{phase}: {modified} item(s) were changed on the target since mccb wrote them "
                    "(by someone, or by the server itself): they are neither deleted nor overwritten")
        if unverified:
            an.note(UNVERIFIED_OWNER, f"{phase}: {unverified} item(s) cannot be shown to be the ones mccb wrote (written "
                    "before etags were kept, or over a resource that was already there): they are neither deleted "
                    "nor overwritten")


def _shared_resources(an: Analysis, log) -> None:
    """One resource of the target may be the copy of several rows: a DAV target names an event or a contact by its
    UID, and the same UID may be in two source folders written to one collection. Such a resource is deleted only
    when every row it carries goes, and updated only when no other row still at the source carries it."""
    for phase, acts in an.actions.items():
        if phase == MAIL:
            continue
        groups: dict[tuple[str, str], list[dict]] = {}
        for r in an.rows.get(phase, []):
            # holders of a resource: our items, and those of a folder no longer migrated (skipped: still at the
            # source, their copy still on the target); not a row given up before it was ever written
            holds = r["status"] == "ok" or (r["status"] == "skipped" and (r.get("error") or "") != GAVE_UP)
            if holds and r.get("hash"):  # names compared without case: some DAV servers ignore it
                groups.setdefault((r["target"] or "", r["hash"].casefold()), []).append(r)
        deleting = {a.row["id"] for a in acts if a.action == "delete"}
        gone = an.gone_ids.get(phase, set())
        out, released, held = [], 0, 0
        for a in acts:
            row = a.row or {}
            # found by its resource name
            by_name = a.refs and row.get("hash") and a.ident.casefold() == row["hash"].casefold()
            others = [r for r in groups.get((row.get("target") or "", (row.get("hash") or "").casefold()), [])
                      if r["id"] != row.get("id")] if by_name else []
            if a.action == "delete" and any(r["id"] not in deleting for r in others):
                a.action, a.refs = "release", []
                released += 1
                log(f"Mirror: {phase} key={row['item_key']} gone at the source, but its resource '{row['hash']}' on "
                    "the target is also the copy of another item still there: kept", "warning")
            elif a.action == "update" and any(r["id"] not in gone for r in others):
                held += 1
                log(f"Mirror: {phase} key={row['item_key']} changed at the source, but its resource '{row['hash']}' "
                    "on the target is also the copy of another item: not overwritten", "warning")
                continue
            out.append(a)
        an.actions[phase] = out
        an.skipped[phase] = an.skipped.get(phase, 0) + held
        if released or held:
            an.note(SHARED_RESOURCE, f"{phase}: {released + held} item(s) share their resource on the target with "
                    "another item still at the source (same UID in two source folders): that resource is kept as "
                    "it is")


def _delete_notes(an: Analysis, sessions: dict, capabilities: dict, log) -> None:
    """How the target will delete, said before it does (dry run and report). A target that deletes for good and
    cannot give a copy back first deletes nothing: those deletions are held here already."""
    for phase, acts in an.actions.items():
        dels = [a for a in acts if a.action == "delete" and a.refs]
        if not dels:
            continue
        session = sessions[phase]
        modes: dict[str, str | None] = {}
        for a in dels:
            t = a.row["target"] or ""
            if t not in modes:
                try:
                    modes[t] = session.delete_mode(t)
                except (ConnectorError, AttributeError):
                    modes[t] = None
        hard = {t for t, m in modes.items() if m == DELETE_HARD}
        if phase == MAIL and any(m == DELETE_SOFT for m in modes.values()):
            an.note(SOFT_DELETE, "mail: the target has no trash folder (or the mails are in it): deleted mails are "
                    f"only flagged \\Deleted and {MIRROR_DELETED_FLAG}, never expunged")
        if not hard:
            continue
        if CAP_READBACK in capabilities.get(phase, frozenset()):
            an.note(HARD_DELETE, f"{phase}: the target deletes for good: a copy of each item is kept first under "
                    "<data>/mirror-trash/")
            continue
        held = [a for a in dels if (a.row["target"] or "") in hard]
        ids = {id(a) for a in held}
        an.actions[phase] = [a for a in acts if id(a) not in ids]
        an.skipped[phase] = an.skipped.get(phase, 0) + len(held)
        an.note(NO_COPY_KEPT, f"{phase}: the target deletes for good and cannot give a copy back: its {len(held)} "
                "deletion(s) are not done (skipped_by_safety)")
        log(f"Mirror: {len(held)} {phase} deletion(s) held, the target cannot keep a copy first", "warning")


def summary(an: Analysis) -> dict:
    out = {"to_add": zero(), "to_update": zero(), "to_delete": zero()}
    for phase, acts in an.actions.items():
        for a in acts:
            if a.action == "add":
                out["to_add"][phase] += 1
            elif a.action in ("flags", "move", "update"):
                out["to_update"][phase] += 1
            elif a.action == "delete" and a.refs:
                out["to_delete"][phase] += 1
    return out


def sample(an: Analysis) -> list[dict]:
    from .runner import folder_key
    out = []
    order = {"delete": 0, "move": 1, "update": 2, "flags": 3, "add": 4}
    acts = sorted((a for acts in an.actions.values() for a in acts
                   if a.action in order and (a.action != "delete" or a.refs)), key=lambda a: order[a.action])
    for a in acts[:SAMPLE_MAX]:
        folder = folder_key(a.folder.path) if a.folder is not None else (a.row["folder"] if a.row else "")
        date = a.item.date.isoformat() if a.item is not None and a.item.date is not None else ""
        out.append({"kind": a.phase, "action": "update" if a.action in ("flags", "move") else a.action,
                    "detail": a.action, "folder": folder,
                    "subject": short((a.row or {}).get("subject") if a.row else ""), "date": date})
    return out


# ------------------------------------------------------------------ dry run
def _folders_of(src, acc, policy: pol.Policy, phases: list[str]) -> tuple[list[SourceFolder], set[str], set[str]]:
    """(folders to mirror, every key of every source folder (old ones too), the folder_key() of every source folder)."""
    from .runner import folder_key, legacy_folder_key
    folders = src.folders()
    known = set()
    for f in folders:
        known |= {folder_key(f.path), legacy_folder_key(f.path)}
    out = [f for f in folders if f.kind in phases and not f.skip
           and not (policy.exclude and policy.excluded(list(f.path)))]
    return out, known, {folder_key(f.path) for f in folders}


def active_job(account_id: int) -> dict | None:
    """The job of this mailbox still queued or running, if any: a mirror job is never created next to it."""
    r = db.q1(f"SELECT id, mode, status FROM jobs WHERE account_id=? AND status IN ({','.join('?' * len(ACTIVE_STATUSES))}) "
              "ORDER BY id DESC LIMIT 1", (account_id, *ACTIVE_STATUSES))
    return dict(r) if r else None


def job_active_block(account_id: int) -> dict | None:
    j = active_job(account_id)
    if j is None:
        return None
    return {"code": JOB_ACTIVE, "message": f"job {j['id']} ({j['mode']}, {j['status']}) is not over: wait for it or "
                                           "stop it before a mirror pass", "job_id": j["id"]}


def wanted_phases(acc, targets: dict) -> list[str]:
    def on(phase):
        return bool(acc["opt_calendar" if phase == TASKS else f"opt_{phase}"])
    return [p for p in PHASES if on(p) and p in targets]


def preview_account(account_id: int, opts: Options) -> dict:
    """POST /api/runs {mode: "mirror", dry_run: true}: what a mirror pass would do. Writes nothing anywhere."""
    acc = db.q1("SELECT * FROM accounts WHERE id=?", (account_id,))
    out = {"account_id": account_id, "email": acc["email"] if acc else None, "ready": False,
           "to_add": zero(), "to_update": zero(), "to_delete": zero(), "blocked": [], "sample": [], "notes": []}
    if acc is None:
        out["blocked"].append(dict(FATAL_UNKNOWN))
        return out
    out["blocked"] = preflight(acc)
    if out["blocked"]:
        return out
    srv = db.q1("SELECT * FROM servers WHERE id=?", (acc["server_id"],))
    policy = pol.effective(acc, srv)
    pst_key = wiring.items_key_of_account(acc)
    src = None
    sessions: dict[str, Any] = {}
    an = Analysis()
    try:
        try:
            targets = wiring.targets_of_account(acc, srv)
        except ConnectorError as e:
            an.block(TARGET_UNREACHABLE, str(e))
            targets = {}
        phases = wanted_phases(acc, targets)
        for phase in phases:
            s = ReadOnlyTarget(targets[phase].session())
            try:
                s.ensure()
            except ConnectorError as e:
                an.block(TARGET_UNREACHABLE, f"{phase}: {e}")
            sessions[phase] = s
        if not an.blocked:
            try:
                src = ReadOnlySource(wiring.source_of_account(acc))
                src.open()
                folders, known, keys = _folders_of(src, acc, policy, phases)
            except ConnectorError as e:
                an.block(SOURCE_UNLISTABLE, f"the source could not be read: {e}")
            else:
                caps = {p: targets[p].capabilities for p in phases}
                owners = {p for p in phases if getattr(targets[p], "names_resources", False)}
                an = analyze(src, folders, known, sessions, caps, account_id, pst_key, policy, opts, source_keys=keys,
                             owners=owners)
    finally:
        if src is not None:
            src.close()
        for s in sessions.values():
            s.close()
    out["blocked"] = an.blocked
    busy = job_active_block(account_id)
    if busy:
        out["blocked"] = out["blocked"] + [busy]
    if not an.fatal:
        out.update(summary(an))
        out["sample"] = sample(an)
        out["notes"] = an.notes
        out["held"] = an.held
    out["ready"] = not out["blocked"]
    return out


def start(account_ids: list[int], opts: Options, name: str | None = None, priority: int = 0,
          requested_by: str = "") -> dict:
    """POST /api/runs {mode: "mirror", dry_run: false}: one job per account that may mirror. The job checks every
    rule again when it runs (the source may have changed since). `requested_by` goes to the log of each job."""
    from . import runs
    accounts, ok = [], []
    for aid in account_ids:
        acc = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
        blocked = preflight(acc) if acc is not None else [dict(FATAL_UNKNOWN)]
        busy = job_active_block(aid) if acc is not None else None
        if busy:  # runs.create_jobs would give that job back, and the options of this request would be lost
            blocked.append(busy)
        accounts.append({"account_id": aid, "email": acc["email"] if acc else None, "ready": not blocked,
                         "blocked": blocked})
        if not blocked:
            ok.append(aid)
    jobs, errors, _ = runs.create_jobs(ok, name, "mirror", priority, mirror=opts.view()) if ok else ([], [], None)
    # a job started by another request between the check above and create_jobs: given back, not created here
    for j in [j for j in jobs if j.get("existing")]:
        for a in accounts:
            if a["account_id"] == j["account_id"]:
                a["ready"] = False
                a["blocked"].append({"code": JOB_ACTIVE, "message": f"job {j['id']} ({j.get('mode')}, "
                                     f"{j.get('status')}) is not over: wait for it or stop it before a mirror pass",
                                     "job_id": j["id"]})
    jobs = [j for j in jobs if not j.get("existing")]
    from . import repo
    for j in jobs:
        repo.log(j["id"], f"Mirror requested by {requested_by or 'the API'}: "
                 + " ".join(f"{k}={v}" for k, v in opts.view().items()))
    for e in errors:
        for a in accounts:
            if a["account_id"] == e["account_id"]:
                a["ready"] = False
                a["blocked"].append({"code": TARGET_UNREACHABLE, "message": e["msg"]})
    by_acc = {j["account_id"]: j["id"] for j in jobs}
    for a in accounts:
        if a["account_id"] in by_acc:
            a["job_id"] = by_acc[a["account_id"]]
    return {"accounts": accounts, "jobs": jobs}


# ------------------------------------------------------------------ helpers for the job
TRASH_DIR = "mirror-trash"


def backup_copy(job_id: int, phase: str, ident: str, raw: bytes) -> str:
    """Copy of an item a target deletes for good, kept before the deletion: <data>/mirror-trash/job-<id>/<phase>/.
    Private to mccb (directories 0700, files 0600): it is mail and contact content. Purged by purge_backups()."""
    from .config import settings
    root = settings.data_dir / TRASH_DIR
    d = root / f"job-{job_id}" / phase
    for x in (root, root / f"job-{job_id}", d):
        x.mkdir(mode=0o700, exist_ok=True)
        os.chmod(x, 0o700)  # mkdir's mode goes through the umask, and an older directory may be wider
    ext = "eml" if phase == MAIL else "vcf" if phase == "contacts" else "ics"
    p = d / f"{hashlib.sha256(ident.encode('utf-8')).hexdigest()[:24]}.{ext}"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)
    os.chmod(p, 0o600)
    return str(p)


def purge_backups(days: int | None = None) -> int:
    """Removes the job-<id> directories of <data>/mirror-trash/ untouched for `days` days (MIRROR_TRASH_DAYS, 30 by
    default; 0 keeps them for ever). Never while a job is running: it may be writing there. The number removed."""
    import shutil
    import time
    from .config import settings
    days = settings.mirror_trash_days if days is None else days
    root = settings.data_dir / TRASH_DIR
    if days <= 0 or not root.is_dir():
        return 0
    if db.q1("SELECT 1 FROM jobs WHERE status IN ('running','paused','stopping') LIMIT 1") is not None:
        return 0
    limit = time.time() - days * 86400
    n = 0
    def mtime(x) -> float:
        try:
            return x.lstat().st_mtime  # a link is not followed: a dangling one is only its own date
        except OSError:
            return 0.0
    for d in root.iterdir():
        if d.is_symlink() or not (d.is_dir() and d.name.startswith("job-")):
            continue
        try:
            newest = max([mtime(d)] + [mtime(x) for x in d.rglob("*")])
        except OSError:  # removed meanwhile, unreadable: left for the next purge
            continue
        if newest < limit:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


def save_counters(job_id: int, data: dict) -> None:
    db.ex("UPDATE jobs SET mirror=? WHERE id=?", (db.jdump(data), job_id))


