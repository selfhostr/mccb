"""In-memory source and target, to run the connector route without any network service."""
from __future__ import annotations

import threading
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.connectors import registry
from app.connectors.base import (CAP_PARALLEL, CAP_READBACK, ConnectorError, FetchedItem, FolderPlan, Source,
                                 SourceFolder, SourceItem, Target, TargetSession)

T0 = datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc)
SOURCES: dict[str, "MemoryMailbox"] = {}
TARGETS: dict[str, "MemoryStore"] = {}


class MemoryMailbox:
    def __init__(self, folders: dict[str, int]):
        self.folders = dict(folders)
        self.generation = 1      # part of the keys: changing it mimics a new UIDVALIDITY
        self.fetch_errors: dict[str, ConnectorError] = {}
        self.fetches: list[str] = []

    def key(self, folder: str, i: int) -> str:
        return f"{folder}-{self.generation}-{i}"

    def raw(self, folder: str, i: int) -> bytes:
        return (f"From: a@example.test\r\nTo: b@example.test\r\nSubject: {folder} {i}\r\n"
                f"Message-ID: <{folder}.{i}@example.test>\r\n\r\nbody {i}\r\n").encode()


class MemorySource(Source):
    def __init__(self, config, secrets, ctx=None):
        self.box = SOURCES[config["name"]]
        self.secret = secrets.get("token")

    def open(self):
        if self.secret != "ok":
            raise ConnectorError("bad token", ConnectorError.AUTH)

    def close(self):
        pass

    def folders(self):
        return [SourceFolder(path=[name], special="inbox" if name == "INBOX" else None, count=n, ref=name)
                for name, n in self.box.folders.items()]

    def iter_items(self, folder):
        for i in range(self.box.folders[folder.ref]):
            yield SourceItem(key=self.box.key(folder.ref, i), alt_key=f"m:{folder.ref}.{i}",
                             date=T0 + timedelta(hours=i), size=100, flags=["\\Seen"] if i % 2 else [], ref=i)

    def fetch(self, folder, item, import_id):
        self.box.fetches.append(item.key)
        err = self.box.fetch_errors.get(item.key)
        if err is not None:
            raise err
        return FetchedItem(raw=self.box.raw(folder.ref, item.ref), internal_date=item.date, flags=list(item.flags),
                           subject=f"{folder.ref} {item.ref}")


class MemoryStore:
    """What the target holds. `lose_response` writes the item then fails: the answer to the APPEND was lost."""

    def __init__(self):
        self.lock = threading.Lock()
        self.items: list[tuple[str, str, bytes, list, object]] = []  # folder, import id, raw, flags, date
        self.folders: set[str] = set()
        self.lose_response: set[str] = set()
        self.on_put = None

    def copies(self) -> Counter:
        with self.lock:
            return Counter(i[1] for i in self.items)


class MemorySession(TargetSession):
    def __init__(self, store: MemoryStore):
        self.store = store
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
        return {folder_key(f.path): FolderPlan("/".join(f.path)) for f in folders}

    def ensure_folder(self, name, flag=None):
        with self.store.lock:
            self.store.folders.add(name)

    def put(self, folder, item):
        if self.store.on_put:
            self.store.on_put(item)
        with self.store.lock:
            self.store.items.append((folder, item.import_id, item.raw, list(item.flags), item.internal_date))
            lost = item.import_id in self.store.lose_response
            self.store.lose_response.discard(item.import_id)
        if lost:
            raise ConnectorError("connection lost", ConnectorError.TRANSIENT)
        return item.import_id

    def find(self, folder, item):
        with self.store.lock:
            return [i[1] for i in self.store.items if i[0] == folder and i[1] == item.import_id]

    def count_ours(self, folder, namespace, names=None):
        with self.store.lock:
            return sum(i[0] == folder and i[1].startswith(f"<{namespace}-") for i in self.store.items)

    def read(self, folder, remote_id):
        with self.store.lock:
            for i in self.store.items:
                if i[0] == folder and i[1] == remote_id:
                    return i[2]
        return None


class MemoryTarget(Target):
    capabilities = frozenset({CAP_PARALLEL, CAP_READBACK})

    def __init__(self, config, secrets, ctx=None):
        self.store = TARGETS[config["name"]]

    def session(self):
        return MemorySession(self.store)


def register() -> None:
    field = registry.Field("name", "Name", required=True)
    token = registry.Field("token", "Token", "password", required=True, secret=True)
    registry.register(registry.ConnectorSpec(
        kind="memory", label="Memory",
        source=registry.Role(MemorySource, ("mail",), frozenset(), (field, token), lambda c: c["name"]),
        target=registry.Role(MemoryTarget, ("mail",), MemoryTarget.capabilities, (field,), lambda c: c["name"])))


def unregister() -> None:
    registry.unregister("memory")
    SOURCES.clear()
    TARGETS.clear()
