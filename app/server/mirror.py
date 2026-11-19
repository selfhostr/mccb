"""Mirror mode: one more pass after a migration that makes the target follow the source. It adds what is new, deletes
on the target what disappeared from the source, and updates what changed there (mail flags and folder, events,
contacts and tasks by their version). See docs/planning.md "Mirror".

Safety, in the order it is applied:
- the source is reached through ReadOnlySource: only reads exist on it, a write cannot even be called;
- only items of the `items` table with status `ok` (written by mccb) are ever touched on the target, found again by
  their import id or resource name (TargetSession.inventory): native content and later arrivals are never in it;
- a deletion needs a COMPLETE and successful listing of the folder in this pass; one listing error or one incomplete
  listing anywhere in the pass and nothing is deleted at all (`source_unlistable`);
- keys reset (UIDVALIDITY) refuse the folder (`uidvalidity_changed`), an empty source refuses every deletion of the
  kind (`source_empty`), more than `max_delete_percent` % of the tracked items refuses them too (`too_many_deletes`),
  unless `allow_large_delete`;
- deleted mails go to the trash of the target (or are flagged, never expunged); a target that can only delete for
  good gets a copy kept first under <data>/mirror-trash/;
- the dry run (the default of the API) uses read-only wrappers on both sides.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from ..connectors import registry, wiring
from ..connectors.base import (CAP_INVENTORY, CAP_MIRROR, CAP_MOVE, MAIL,
                               MIRROR_DELETED_FLAG, READ_ONLY_SOURCE_METHODS, TASKS, ConnectorError, Listing,
                               RemoteItem, SourceFolder, SourceItem)
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
FATAL = {MIRROR_OFF, NO_PRIOR_RUN, TARGET_UNREACHABLE}  # nothing at all is done


@dataclass
class Options:
    max_delete_percent: int = 20
    allow_large_delete: bool = False

    def view(self) -> dict:
        return {"max_delete_percent": self.max_delete_percent, "allow_large_delete": self.allow_large_delete}

    @classmethod
    def of(cls, d: dict | None) -> "Options":
        d = d or {}
        return cls(int(d.get("max_delete_percent", 20)), bool(d.get("allow_large_delete", False)))


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


def ident_of(phase: str, pst_key: str, row: dict) -> str:
    """How the target knows one of our items: its import id (mail) or its resource name."""
    if phase == MAIL:
        from .runner import import_id_of
        return row.get("stamp") or import_id_of(pst_key, row["item_key"])
    return row["hash"] or ""


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

    @property
    def counted(self) -> bool:
        return self.action != "record"


@dataclass
class Analysis:
    blocked: list[dict] = field(default_factory=list)
    actions: dict[str, list[Action]] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)       # deletions held back by a safety rule
    gone_unfinished: list[dict] = field(default_factory=list)   # pending / inflight / error rows no longer listed
    refused: set[tuple[str, str]] = field(default_factory=set)  # (phase, folder key) left alone

    def block(self, code: str, message: str) -> None:
        if not any(b["code"] == code and b["message"] == message for b in self.blocked):
            self.blocked.append({"code": code, "message": message})

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
    if not (acc["mirror"] if "mirror" in acc.keys() else 0):
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
            retry=None, retry_target=None, log=None) -> Analysis:
    """What a mirror pass would do. `source` is a ReadOnlySource, `folders` the source folders to mirror (exclusions
    applied), `known_folders` the keys of every folder the source still has (excluded ones too), `sessions` {phase:
    connected target session}. Reads only: the source, the items table and the inventory of the target."""
    from .runner import folder_key, legacy_folder_key
    retry = retry or (lambda fn: fn())
    retry_target = retry_target or (lambda session, fn: fn())
    log = log or (lambda msg, level="info": None)
    an = Analysis()
    listing_failed = False
    for phase in PHASES:
        if phase not in sessions:
            continue
        kind = ROW_KIND[phase]
        an.actions[phase] = []
        an.skipped[phase] = 0
        rows_all = [dict(r) for r in db.q(
            "SELECT id, item_key, folder, target, status, error, hash, source_ref, version, stamp, subject FROM items "
            "WHERE account_id=? AND pst_key=? AND kind=?", (account_id, pst_key, kind))]
        by_folder: dict[str, dict[str, dict]] = {}
        for r in rows_all:
            by_folder.setdefault(r["folder"], {})[r["item_key"]] = r
        mine = [f for f in folders if f.kind == phase]
        listed_keys: set[str] = set()
        listed_total = 0
        adds: list[Action] = []
        gone: list[dict] = []
        for f in mine:
            fk = folder_key(f.path)
            rows = {**by_folder.get(legacy_folder_key(f.path), {}), **by_folder.get(fk, {})}
            listed_keys |= {fk, legacy_folder_key(f.path)}
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
            if listing.epoch:
                epochs = {e for e in (source.key_epoch(r["item_key"]) for r in ok_rows) if e is not None}
                if epochs and epochs != {listing.epoch}:
                    an.refused.add((phase, fk))
                    an.block(UIDVALIDITY_CHANGED, f"{phase} folder '{fk}': the keys of the source were reset "
                             f"(UIDVALIDITY {', '.join(sorted(epochs))} -> {listing.epoch}): run a full pass")
                    log(f"Mirror: folder '{fk}' refused, UIDVALIDITY changed: run a full pass", "error")
                    continue
            listed_total += len(listing.items)
            seen = set()
            for it in listing.items:
                seen.add(it.key)
                r = rows.get(it.key)
                if wants(r):
                    if policy.has_filters and kind != "contact" and policy.reason(it.date, it.size if phase == MAIL else None):
                        continue  # left out by a filter: the job marks it as a normal run does
                    adds.append(Action("add", phase, row=r, folder=f, item=it))
                elif r["status"] == "ok":
                    if phase == MAIL:
                        src = canon_flags(it.flags)
                        if r["version"] is None:
                            adds_flags = Action("record", phase, row=r, folder=f, item=it)  # resolved on inventory
                            an.actions[phase].append(adds_flags)
                        elif r["version"] != src:
                            old, new = split_flags(r["version"]), split_flags(src)
                            an.actions[phase].append(Action("flags", phase, row=r, folder=f, item=it,
                                                            add=sorted(new - old), remove=sorted(old - new)))
                    elif it.version is not None and r["version"] != it.version:
                        an.actions[phase].append(Action("update", phase, row=r, folder=f, item=it))
            for r in rows.values():
                if r["item_key"] in seen:
                    continue
                if r["status"] == "ok":
                    gone.append(r)
                elif r["status"] in ("pending", "inflight", "error") and listing.complete:
                    an.gone_unfinished.append(r)
        # tracked items of folders the source no longer lists: never deleted (no listing confirms it)
        vanished = [r for r in rows_all if r["status"] == "ok" and r["folder"] not in listed_keys
                    and r["folder"] not in known_folders]
        if vanished:
            an.skipped[phase] += len(vanished)
            log(f"Mirror: {len(vanished)} {phase} item(s) in folder(s) the source no longer has: not deleted "
                "(no listing of the folder confirms it)", "warning")
        # moves: same Message-ID gone from one folder, new in another (one to one)
        if phase == MAIL and CAP_MOVE in capabilities.get(phase, frozenset()):
            pool: dict[str, list[dict]] = {}
            for r in gone:
                if (r["source_ref"] or "").startswith("m:"):
                    pool.setdefault(r["source_ref"], []).append(r)
            kept = []
            for a in adds:
                cands = pool.get(a.item.alt_key or "") or []
                cands = [r for r in cands if r["folder"] != folder_key(a.folder.path)]
                if a.item.alt_key and cands:
                    r = cands[0]
                    pool[a.item.alt_key].remove(r)
                    gone.remove(r)
                    an.actions[phase].append(Action("move", phase, row=r, folder=a.folder, item=a.item))
                else:
                    kept.append(a)
            adds = kept
        an.actions[phase].extend(adds)
        dels = [Action("delete", phase, row=r) for r in gone]
        # safety on deletions of this kind
        tracked = sum(1 for r in rows_all if r["status"] == "ok")
        reason = None
        if dels and listed_total == 0 and tracked and not opts.allow_large_delete:
            reason = (SOURCE_EMPTY, f"{phase}: the source lists nothing while {tracked} item(s) were migrated")
        elif dels and tracked and len(dels) * 100 > opts.max_delete_percent * tracked and not opts.allow_large_delete:
            reason = (TOO_MANY_DELETES, f"{phase}: {len(dels)} of {tracked} migrated item(s) would be deleted "
                      f"(more than {opts.max_delete_percent} %)")
        if reason:
            an.block(*reason)
            an.skipped[phase] += len(dels)
            log(f"Mirror: deletions held back, {reason[1]}", "warning")
        else:
            an.actions[phase].extend(dels)
    if listing_failed:  # one error anywhere in the pass: nothing is deleted at all
        for phase, acts in an.actions.items():
            n = sum(1 for a in acts if a.action == "delete")
            if n:
                an.skipped[phase] += n
                an.actions[phase] = [a for a in acts if a.action != "delete"]
    else:
        _confirm_gone(an, source, folders, retry, log)
    _inventory(an, sessions, capabilities, pst_key, retry_target, log)
    return an


def _confirm_gone(an: Analysis, source, folders: list[SourceFolder], retry, log) -> None:
    """Last check before a deletion: the source is asked directly, key by key (Source.exists), whether each item
    the listing no longer has is really gone. Still there, or no answer: not deleted. An error: nothing deleted."""
    from .runner import folder_key, legacy_folder_key
    by_key: dict[tuple[str, str], SourceFolder] = {}
    for f in folders:
        by_key[(f.kind, folder_key(f.path))] = by_key[(f.kind, legacy_folder_key(f.path))] = f
    for phase, acts in an.actions.items():
        dels = [a for a in acts if a.action == "delete"]
        groups: dict[str, list[Action]] = {}
        for a in dels:
            groups.setdefault(a.row["folder"], []).append(a)
        held: set[int] = set()
        for fk, group in groups.items():
            f = by_key.get((phase, fk))
            keys = [a.row["item_key"] for a in group]
            try:
                there = retry(lambda: source.exists(f, keys)) if f is not None else {}
            except ConnectorError as e:
                an.block(SOURCE_UNLISTABLE, f"{phase} folder '{fk}': the source could not confirm the deletions: {e}")
                log(f"Mirror: the source could not confirm the deletions in '{fk}' ({e}): no deletion in this pass",
                    "error")
                for p, other in an.actions.items():
                    n = sum(1 for a in other if a.action == "delete")
                    an.skipped[p] += n
                    an.actions[p] = [a for a in other if a.action != "delete"]
                return
            for a in group:
                if there.get(a.row["item_key"], True):
                    held.add(id(a))
                    log(f"Mirror: {phase} key={a.row['item_key']} not listed but still read at the source: kept",
                        "warning")
        if held:
            an.skipped[phase] += len(held)
            an.actions[phase] = [a for a in acts if id(a) not in held]


def _inventory(an: Analysis, sessions: dict, capabilities: dict, pst_key: str, retry_target, log) -> None:
    """Finds our items on the target for every action that touches one: the refs, the flags of mails written before
    versions were kept. An item no longer on the target is left alone (delete: just recorded)."""
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
            names = None if phase == MAIL else {a.row["hash"] for a in group if a.row["hash"]}
            try:
                inv = retry_target(session, lambda: session.inventory(target, pst_key, names))
            except ConnectorError as e:
                an.block(TARGET_UNREACHABLE, f"{phase}: cannot list '{target}' on the target: {e}")
                log(f"Mirror: cannot list '{target}' on the target: {e}", "error")
                return
            for a in group:
                a.refs = list(inv.get(ident_of(phase, pst_key, a.row), []))
                if a.action == "record" and a.refs:
                    # written before versions were kept: compared with what the target holds
                    have, want = split_flags(canon_flags(a.refs[0].flags)), split_flags(canon_flags(a.item.flags))
                    add = sorted(want - have)
                    remove = sorted((have & set(SYSTEM_FLAGS.values())) - want)
                    if add or remove:
                        a.action, a.add, a.remove = "flags", add, remove
    for phase in an.actions:
        # nothing to do on an item the target no longer has, except recording a deletion
        an.actions[phase] = [a for a in an.actions[phase]
                             if a.action in ("add", "delete") or a.refs or a.action == "record"]


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
                   if a.action != "record" and (a.action != "delete" or a.refs)), key=lambda a: order[a.action])
    for a in acts[:SAMPLE_MAX]:
        folder = folder_key(a.folder.path) if a.folder is not None else (a.row["folder"] if a.row else "")
        date = a.item.date.isoformat() if a.item is not None and a.item.date is not None else ""
        out.append({"kind": a.phase, "action": "update" if a.action in ("flags", "move") else a.action,
                    "detail": a.action, "folder": folder,
                    "subject": short((a.row or {}).get("subject") if a.row else ""), "date": date})
    return out


# ------------------------------------------------------------------ dry run
def _folders_of(src, acc, policy: pol.Policy, phases: list[str]) -> tuple[list[SourceFolder], set[str]]:
    from .runner import folder_key, legacy_folder_key
    folders = src.folders()
    known = set()
    for f in folders:
        known |= {folder_key(f.path), legacy_folder_key(f.path)}
    out = [f for f in folders if f.kind in phases and not f.skip
           and not (policy.exclude and policy.excluded(list(f.path)))]
    return out, known


def wanted_phases(acc, targets: dict) -> list[str]:
    def on(phase):
        return bool(acc["opt_calendar" if phase == TASKS else f"opt_{phase}"])
    return [p for p in PHASES if on(p) and p in targets]


def preview_account(account_id: int, opts: Options) -> dict:
    """POST /api/runs {mode: "mirror", dry_run: true}: what a mirror pass would do. Writes nothing anywhere."""
    acc = db.q1("SELECT * FROM accounts WHERE id=?", (account_id,))
    out = {"account_id": account_id, "email": acc["email"] if acc else None, "ready": False,
           "to_add": zero(), "to_update": zero(), "to_delete": zero(), "blocked": [], "sample": []}
    if acc is None:
        out["blocked"].append({"code": NO_PRIOR_RUN, "message": "unknown mailbox"})
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
                folders, known = _folders_of(src, acc, policy, phases)
            except ConnectorError as e:
                an.block(SOURCE_UNLISTABLE, f"the source could not be read: {e}")
            else:
                caps = {p: targets[p].capabilities for p in phases}
                an = analyze(src, folders, known, sessions, caps, account_id, pst_key, policy, opts)
    finally:
        if src is not None:
            src.close()
        for s in sessions.values():
            s.close()
    out["blocked"] = an.blocked
    if not an.fatal:
        out.update(summary(an))
        out["sample"] = sample(an)
    out["ready"] = not an.blocked
    return out


def start(account_ids: list[int], opts: Options, name: str | None = None, priority: int = 0) -> dict:
    """POST /api/runs {mode: "mirror", dry_run: false}: one job per account that may mirror. The job checks every
    rule again when it runs (the source may have changed since)."""
    from . import runs
    accounts, ok = [], []
    for aid in account_ids:
        acc = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
        blocked = preflight(acc) if acc is not None else [{"code": NO_PRIOR_RUN, "message": "unknown mailbox"}]
        accounts.append({"account_id": aid, "email": acc["email"] if acc else None, "ready": not blocked,
                         "blocked": blocked})
        if not blocked:
            ok.append(aid)
    jobs, errors, _ = runs.create_jobs(ok, name, "mirror", priority, mirror=opts.view()) if ok else ([], [], None)
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
def backup_copy(job_id: int, phase: str, ident: str, raw: bytes) -> str:
    """Copy of an item a target deletes for good, kept before the deletion: <data>/mirror-trash/job-<id>/<phase>/."""
    from .config import settings
    d = settings.data_dir / "mirror-trash" / f"job-{job_id}" / phase
    d.mkdir(parents=True, exist_ok=True)
    ext = "eml" if phase == MAIL else "vcf" if phase == "contacts" else "ics"
    p = d / f"{hashlib.sha256(ident.encode('utf-8')).hexdigest()[:24]}.{ext}"
    p.write_bytes(raw)
    return str(p)


def save_counters(job_id: int, data: dict) -> None:
    db.ex("UPDATE jobs SET mirror=? WHERE id=?", (db.jdump(data), job_id))


