"""In-memory source and target for the mirror mode: a source that can lose, move and change items, and that RAISES on
any call that would alter it; a target that keeps what mccb wrote apart from its own (native) content."""
from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.connectors import registry
from app.connectors.base import (CAP_DELETE, CAP_INVENTORY, CAP_MIRROR, CAP_MOVE, CAP_SET_FLAGS, CAP_UPDATE,
                                 CONTACTS, DELETE_SOFT, DELETE_TRASH, MAIL, MIRROR_DELETED_FLAG, ConnectorError,
                                 FetchedItem, FolderPlan, Listing, RemoteItem, Source, SourceFolder, SourceItem, Target,
                                 TargetSession)

T0 = datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc)
SOURCES: dict[str, "Box"] = {}
TARGETS: dict[str, "Store"] = {}


class SourceWritten(AssertionError):
    """The mirror tried to alter the source."""


@dataclass
class Msg:
    mid: str
    subject: str
    flags: list[str] = field(default_factory=list)
    version: str = "1"
    body: str = "body"


class Box:
    """folders: {(kind, name): {uid: Msg}}. `epoch` per folder is part of the keys (like UIDVALIDITY)."""

    def __init__(self):
        self.folders: dict[tuple[str, str], dict[int, Msg]] = {}
        self.epoch: dict[tuple[str, str], int] = {}
        self._uid = itertools.count(1)
        self.list_errors: dict[str, ConnectorError] = {}   # folder name -> raised by mirror_listing
        self.incomplete: set[str] = set()                   # folder names listed with complete=False
        self.hidden: set[str] = set()                       # Message-IDs a listing misses (still at the source)
        self.hidden_in: set[tuple[str, str]] = set()        # (folder name, Message-ID): the same, in one folder
        self.exists_errors: dict[str, ConnectorError] = {}  # folder name -> raised by exists
        self.exists_refs: dict = {}                         # refs exists() was given (recorded src_ref)
        self.unstable: set[str] = set()                     # folder names whose epoch is "renamed-or-moved"
        self.exists_omit: set[str] = set()                  # keys exists() leaves out of its answer
        self.calls: list[str] = []

    def add(self, name: str, mid: str, kind: str = MAIL, **kw) -> int:
        f = self.folders.setdefault((kind, name), {})
        self.epoch.setdefault((kind, name), 1)
        uid = next(self._uid)
        f[uid] = Msg(mid, kw.pop("subject", mid), **kw)
        return uid

    def find(self, mid: str) -> tuple[tuple[str, str], int]:
        for fk, items in self.folders.items():
            for uid, m in items.items():
                if m.mid == mid:
                    return fk, uid
        raise KeyError(mid)

    def remove(self, mid: str, name: str | None = None) -> None:
        """Removes `mid` (from the folder `name` only when given: the same item may be in several)."""
        if name is not None:
            for kind_name, items in self.folders.items():
                if kind_name[1] == name:
                    for uid, m in list(items.items()):
                        if m.mid == mid:
                            del items[uid]
                            return
            raise KeyError(mid)
        fk, uid = self.find(mid)
        del self.folders[fk][uid]

    def move(self, mid: str, to: str) -> None:
        fk, uid = self.find(mid)
        m = self.folders[fk].pop(uid)
        self.folders.setdefault((fk[0], to), {})[next(self._uid)] = m
        self.epoch.setdefault((fk[0], to), 1)

    def get(self, mid: str) -> Msg:
        fk, uid = self.find(mid)
        return self.folders[fk][uid]


def key_of(kind: str, name: str, epoch: int, uid: int) -> str:
    return f"{kind[0]}{name}-{epoch}-{uid}"


class BoxSource(Source):
    def __init__(self, config, secrets, ctx=None):
        self.box = SOURCES[config["name"]]

    # ---- what a mutating client of the source would call: the mirror must never reach any of these
    def _written(self, *a, **kw):
        raise SourceWritten("the source was written to")

    append = delete = expunge = store = set_flags = move = update = put = mark_read = _written

    def open(self):
        self.box.calls.append("open")

    def close(self):
        pass

    def folders(self):
        # "A|B" is the folder B inside A (path ["A", "B"]); "A/B" is one folder named "A/B"
        return [SourceFolder(path=name.split("|"), kind=kind, special="inbox" if name == "INBOX" else None,
                             count=len(items), ref=(kind, name)) for (kind, name), items in self.box.folders.items()]

    def _items(self, folder):
        kind, name = folder.ref
        ep = self.box.epoch[(kind, name)]
        for uid, m in sorted(self.box.folders.get((kind, name), {}).items()):
            if m.mid in self.box.hidden or (name, m.mid) in self.box.hidden_in:
                continue
            yield SourceItem(key=key_of(kind, name, ep, uid), alt_key=f"m:{m.mid}", date=T0 + timedelta(minutes=uid),
                             size=100, flags=list(m.flags), ref=uid, version=None if kind == MAIL else m.version)

    def iter_items(self, folder):
        return self._items(folder)

    def mirror_listing(self, folder):
        kind, name = folder.ref
        self.box.calls.append(f"list:{name}")
        if name in self.box.list_errors:
            raise self.box.list_errors[name]
        return Listing(list(self._items(folder)), complete=name not in self.box.incomplete,
                       epoch="renamed-or-moved" if name in self.box.unstable else str(self.box.epoch[(kind, name)]),
                       problem="paging cut short" if name in self.box.incomplete else None)

    def exists(self, folder, keys, refs=None):
        """Direct read by key; `ghosts` are items a listing misses although they are still there."""
        kind, name = folder.ref
        ep = self.box.epoch[(kind, name)]
        self.box.calls.append(f"exists:{name}:{len(keys)}")
        self.box.exists_refs.update(refs or {})
        if name in self.box.exists_errors:
            raise self.box.exists_errors[name]
        here = {key_of(kind, name, ep, uid) for uid in self.box.folders.get((kind, name), {})}
        return {k: k in here for k in keys if k not in self.box.exists_omit}

    def key_epoch(self, key):
        parts = key.rsplit("-", 2)
        return parts[1] if len(parts) == 3 else None

    def fetch(self, folder, item, import_id):
        kind, name = folder.ref
        m = self.box.folders[(kind, name)].get(item.ref)
        if m is None:
            return None
        if kind == MAIL:
            raw = (f"From: a@example.test\r\nTo: b@example.test\r\nSubject: {m.subject}\r\nMessage-ID: <{m.mid}>\r\n"
                   f"\r\n{m.body}\r\n").encode()
        else:
            raw = f"BEGIN:VCARD\r\nVERSION:3.0\r\nUID:{m.mid}\r\nFN:{m.subject}\r\nNOTE:{m.body}\r\nEND:VCARD\r\n".encode()
        return FetchedItem(raw=raw, internal_date=item.date, flags=list(m.flags), subject=m.subject,
                           name=None if kind == MAIL else f"{m.mid}.vcf")


@dataclass
class Copy:
    ref: str
    ident: str            # import id (mail) or resource name
    raw: bytes
    flags: list[str]
    ours: bool = True     # False: native content of the target


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.folders: dict[str, dict[str, Copy]] = {"INBOX": {}, "Trash": {}}
        self._ref = itertools.count(1)
        self.writes: list[tuple] = []
        self.trash = "Trash"
        self.inventory_errors: set[str] = set()
        # True: events, contacts and tasks are marked by their import id like mail (EWS, Graph, JMAP), their
        # resource name is ignored; False: known by their resource name (DAV)
        self.by_import_id = False
        self.plan: dict[str, str] = {}   # source folder ("A/B") -> target folder, when not the same name
        # True: update() writes a new copy, then moves the old one to the trash (Exchange-like)
        self.update_replaces = False
        self.fail_trash = 0              # update(): so many next moves of the old copy to the trash fail
        self.casefold = False            # resource names compared without case (some DAV servers)
        self.updates = 0                 # update() calls

    def native(self, folder: str, raw: bytes, ident: str = "") -> str:
        ref = str(next(self._ref))
        self.folders.setdefault(folder, {})[ref] = Copy(ref, ident, raw, [], ours=False)
        return ref

    def all(self, folder: str) -> list[Copy]:
        return list(self.folders.get(folder, {}).values())

    def by_ident(self, ident_part: str) -> list[tuple[str, Copy]]:
        return [(fo, c) for fo, items in self.folders.items() for c in items.values() if ident_part in c.ident]


class StoreSession(TargetSession):
    def __init__(self, store: Store):
        self.s = store
        self._open = False

    @property
    def connected(self):
        return self._open

    def connect(self):
        self._open = True

    def close(self):
        self._open = False

    def plan_folders(self, folders):
        from app.server.runner import folder_key
        return {folder_key(f.path): FolderPlan(self.s.plan.get("/".join(f.path), "/".join(f.path))) for f in folders}

    def ensure_folder(self, name, flag=None, label=None):
        with self.s.lock:
            self.s.folders.setdefault(name, {})

    def put(self, folder, item):
        with self.s.lock:
            box = self.s.folders.setdefault(folder, {})
            ident = item.import_id if item.kind == MAIL or self.s.by_import_id else item.name
            if item.kind != MAIL and not self.s.by_import_id:  # DAV-like: same name overwrites
                for ref, c in list(box.items()):
                    if self._same(c.ident, ident):
                        del box[ref]
            ref = str(next(self.s._ref))
            box[ref] = Copy(ref, ident, item.raw, list(item.flags))
            self.s.writes.append(("put", folder, ident))
            return ref

    def _same(self, a, b):
        return a.casefold() == b.casefold() if self.s.casefold else a == b

    def find(self, folder, item):
        ident = item.import_id if item.kind == MAIL or self.s.by_import_id else item.name
        with self.s.lock:
            return [c.ref for c in self.s.folders.get(folder, {}).values() if c.ident == ident]

    def count_ours(self, folder, namespace, names=None):
        with self.s.lock:
            return sum(1 for c in self.s.folders.get(folder, {}).values()
                       if c.ident.startswith(f"<{namespace}-") or (names and c.ident in names))

    def read(self, folder, remote_id):
        with self.s.lock:
            c = self.s.folders.get(folder, {}).get(remote_id)
            return c.raw if c else None

    # ---- mirror
    def inventory(self, folder, namespace, names=None):
        if folder in self.s.inventory_errors:
            raise ConnectorError("inventory failed", ConnectorError.TRANSIENT)
        out: dict[str, list[RemoteItem]] = {}
        with self.s.lock:
            for c in self.s.folders.get(folder, {}).values():
                if c.ident.startswith(f"<{namespace}-"):
                    out.setdefault(c.ident, []).append(RemoteItem(c.ref, list(c.flags)))
                    continue
                for n in names or ():
                    if self._same(c.ident, n):
                        out.setdefault(n, []).append(RemoteItem(c.ref, list(c.flags)))
        return out

    def delete_mode(self, folder):
        return DELETE_SOFT if folder == self.s.trash else DELETE_TRASH

    def delete(self, folder, ref, expected_import_id=None):
        with self.s.lock:
            c = self.s.folders.get(folder, {}).get(ref)
            if c is None:
                return self.delete_mode(folder)
            assert c.ours, "deleting native content of the target"
            if expected_import_id is not None and not self._same(c.ident, expected_import_id):
                raise ConnectorError("not the expected item", ConnectorError.REJECTED)
            if folder == self.s.trash:
                c.flags = sorted(set(c.flags) | {"\\Deleted", MIRROR_DELETED_FLAG})
                self.s.writes.append(("soft-delete", folder, c.ident))
                return DELETE_SOFT
            del self.s.folders[folder][ref]
            c.flags = sorted(set(c.flags) | {MIRROR_DELETED_FLAG})
            self.s.folders.setdefault(self.s.trash, {})[ref] = c
            self.s.writes.append(("trash", folder, c.ident))
            return DELETE_TRASH

    def update(self, folder, ref, item):
        self.s.updates += 1
        if self.s.update_replaces:  # a new copy first, then the old one to the trash
            with self.s.lock:
                assert self.s.folders[folder][ref].ours
            new = self.put(folder, item)
            if self.s.fail_trash:
                self.s.fail_trash -= 1
                raise ConnectorError("trash refused for now", ConnectorError.TRANSIENT)
            self.delete(folder, ref, item.import_id)
            return new
        with self.s.lock:
            c = self.s.folders[folder][ref]
            assert c.ours
            c.raw = item.raw
            self.s.writes.append(("update", folder, c.ident))
            return ref

    def set_flags(self, folder, ref, add, remove):
        with self.s.lock:
            c = self.s.folders[folder][ref]
            assert c.ours
            c.flags = sorted((set(c.flags) | set(add)) - set(remove))
            self.s.writes.append(("flags", folder, c.ident, tuple(add), tuple(remove)))

    def move(self, folder, ref, to_folder):
        with self.s.lock:
            c = self.s.folders[folder].pop(ref)
            assert c.ours
            self.s.folders.setdefault(to_folder, {})[ref] = c
            self.s.writes.append(("move", folder, to_folder, c.ident))
            return ref


class StoreTarget(Target):
    capabilities = frozenset({CAP_INVENTORY, CAP_DELETE, CAP_UPDATE, CAP_SET_FLAGS, CAP_MOVE})

    @property
    def names_resources(self):
        return not self.store.by_import_id

    def __init__(self, config, secrets, ctx=None):
        self.store = TARGETS[config["name"]]

    def session(self):
        return StoreSession(self.store)


def register() -> None:
    f = registry.Field("name", "Name", required=True)
    registry.register(registry.ConnectorSpec(
        kind="boxmirror", label="Mirror box",
        source=registry.Role(BoxSource, (MAIL, CONTACTS), frozenset({CAP_MIRROR}), (f,), lambda c: c["name"]),
        target=registry.Role(StoreTarget, (MAIL, CONTACTS), StoreTarget.capabilities, (f,), lambda c: c["name"])))


def unregister() -> None:
    registry.unregister("boxmirror")
    SOURCES.clear()
    TARGETS.clear()
