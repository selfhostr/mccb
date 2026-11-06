"""Connector interface: what a source and a target must provide for JobRunner to migrate between them.

A source lists folders and items cheaply, then yields the raw bytes of one item at a time. A target writes
items and can tell afterwards whether an item is already there. Pause, stop, resume, duplicates, reports and
tuning live in the runner (app/server/connector_runner.py): connectors never touch the `items` table.
See docs/connectors.md.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator

# value carried by every migrated mail (name kept: earlier imports are recognized by it)
IMPORT_HEADER = "X-PST-Import-Id"

MAIL, CALENDAR, CONTACTS, TASKS = "mail", "calendar", "contacts", "tasks"
ITEM_KINDS = (MAIL, CALENDAR, CONTACTS, TASKS)

# capabilities (declared in the catalogue, read by the runner and the UI; unknown ones are simply displayed)
CAP_FOLDERS = "folders"              # keeps a folder hierarchy
CAP_FLAGS = "flags"                  # read / sets per-message flags
CAP_INTERNAL_DATE = "internal_date"  # read / sets the received date
CAP_INCREMENTAL = "incremental"      # source: changed_since() lists only what changed
CAP_QUOTA = "quota"                  # target: reports used / limit
CAP_PARALLEL = "parallel_writes"     # target: several sessions may write at once (auto-tuned)
CAP_READBACK = "readback"            # target: read() returns what was written (verify phase re-reads a sample)
CAP_RULES = "rules"                  # source: read_rules() returns the mail rules of the mailbox (see sieve.py)


class ConnectorError(Exception):
    """Failure of a connector, classified so that the runner knows what to do with it.

    auth: credentials refused, config: wrong settings (retrying is useless), unreachable: cannot connect,
    transient: temporary failure of an established session, rejected: this item is refused (the others go on),
    quota: the target is full (stop the mailbox), folder_missing: the target folder does not exist (created
    then retried by the runner)."""
    AUTH = "auth"
    CONFIG = "config"
    UNREACHABLE = "unreachable"
    TRANSIENT = "transient"
    REJECTED = "rejected"
    QUOTA = "quota"
    FOLDER_MISSING = "folder_missing"

    _RESULT = {AUTH: "auth", CONFIG: "fatal", UNREACHABLE: "temp", TRANSIENT: "temp", REJECTED: "perm",
               QUOTA: "fatal", FOLDER_MISSING: "trycreate"}

    def __init__(self, msg: str, kind: str = TRANSIENT):
        super().__init__(msg)
        self.kind = kind

    @property
    def retryable(self) -> bool:
        return self.kind in (self.UNREACHABLE, self.TRANSIENT)

    @property
    def result_kind(self) -> str:
        """Kind understood by JobRunner._handle_results and the tuner (temp, perm, fatal, auth, trycreate)."""
        return self._RESULT.get(self.kind, "perm")


@dataclass
class SourceFolder:
    path: list[str]               # decoded names from the root, e.g. ["INBOX", "Projects"]
    kind: str = MAIL              # mail | calendar | contacts | tasks (anything else is ignored by the runner)
    special: str | None = None    # inbox | sent | drafts | trash | junk | None
    count: int | None = None      # number of items if cheap to know (refined by the listing)
    ref: Any = None               # connector data (mailbox name, handle...)


@dataclass
class SourceItem:
    """One line of a listing: no body, kept in memory for every item still to migrate."""
    key: str                      # stable across runs, unique within kind + account. ASCII, 200 chars at most
    alt_key: str | None = None    # second identity (e.g. Message-ID digest) surviving a change of `key`
    date: datetime | None = None  # sort date (newest first); items without a date go last
    size: int | None = None
    flags: list[str] = field(default_factory=list)
    ref: Any = None               # connector data needed by fetch()
    index: int | None = None      # position in the folder, for sources running native code (crash marker)


@dataclass
class FetchedItem:
    raw: bytes                    # RFC 5322 (mail), iCalendar (calendar, tasks) or vCard (contacts)
    internal_date: datetime | None = None  # tz-aware; naive values are read as UTC
    flags: list[str] = field(default_factory=list)
    subject: str = ""
    name: str | None = None       # deterministic resource name for DAV-like targets (None: derived from the key)
    warnings: list[str] = field(default_factory=list)


@dataclass
class OutgoingItem:
    kind: str                     # mail | calendar | contacts | tasks
    raw: bytes
    import_id: str                # "<namespace-key>": lets a target answer "is it already there?"
    flags: list[str] = field(default_factory=list)
    internal_date: datetime | None = None
    name: str | None = None


@dataclass
class FolderPlan:
    name: str                     # target folder identifier (IMAP mailbox name, DAV collection URL...)
    flag: str | None = None       # special-use flag to ask for when creating it


@dataclass
class Changes:
    items: list[SourceItem]
    cursor: str | None            # opaque, to give back to the next changed_since()
    reset: bool = False           # True: `items` is a full listing (no cursor, or the cursor is no longer valid)


class Source(abc.ABC):
    """One connection to a source, used by a single thread. Calls on it may be retried after close() + open()."""
    native_code = False           # True: runs native code that may crash the process (see PstSource)

    @abc.abstractmethod
    def open(self) -> None:
        """Connects / opens. Raises ConnectorError (auth, config, unreachable)."""

    @abc.abstractmethod
    def close(self) -> None:
        """Never raises."""

    def __enter__(self) -> "Source":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @abc.abstractmethod
    def folders(self) -> list[SourceFolder]:
        """Every folder, in a stable order. A server without SPECIAL-USE still gets `special` from the names."""

    @abc.abstractmethod
    def iter_items(self, folder: SourceFolder) -> Iterator[SourceItem]:
        """Items of a folder. To be consumed entirely before any other call on this source."""

    @abc.abstractmethod
    def fetch(self, folder: SourceFolder, item: SourceItem, import_id: str) -> FetchedItem | None:
        """Bytes of one item, never altering the source. None: nothing to migrate (gone, not convertible).
        `import_id` is only useful to sources that build the message themselves and can embed the header;
        the runner adds the header otherwise."""

    def changed_since(self, folder: SourceFolder, cursor: str | None) -> Changes:
        """Items changed since `cursor`. Default for protocols that cannot tell: a full listing."""
        return Changes(list(self.iter_items(folder)), None, reset=True)

    def read_rules(self) -> list[dict]:
        """Mail rules as sieve.py expects them. Only for sources declaring CAP_RULES, called after open()."""
        return []


class TargetSession(abc.ABC):
    """One connection to a target, used by a single thread."""

    @property
    @abc.abstractmethod
    def connected(self) -> bool: ...

    @abc.abstractmethod
    def connect(self) -> None:
        """Raises ConnectorError (auth, config, unreachable)."""

    @abc.abstractmethod
    def close(self) -> None:
        """Never raises."""

    def ensure(self) -> None:
        if not self.connected:
            self.connect()

    @abc.abstractmethod
    def plan_folders(self, folders: list[SourceFolder]) -> dict[str, FolderPlan]:
        """{folder_key(path): plan}. Deterministic: a resume must get the same names."""

    @abc.abstractmethod
    def ensure_folder(self, name: str, flag: str | None = None) -> None:
        """Creates the folder (and its parents) if needed. Already existing is not an error."""

    @abc.abstractmethod
    def put(self, folder: str, item: OutgoingItem) -> str | None:
        """Writes one item, returns its identifier on the target if known. May be called again for an item
        already written only when the target overwrites (DAV PUT); otherwise the runner calls find() first."""

    @abc.abstractmethod
    def find(self, folder: str, item: OutgoingItem) -> list[str]:
        """Identifiers of the copies of this item already on the target: EXACT match. Only `import_id` and
        `name` of the item are set when called on resume."""

    @abc.abstractmethod
    def count_ours(self, folder: str, namespace: str, names: set[str] | None = None) -> int:
        """Number of items in the folder written by us (never the pre-existing content): those whose import id
        starts with "<namespace>-", or, for targets working with resource names, those listed in `names`."""

    def read(self, folder: str, remote_id: str) -> bytes | None:
        """Content of an item (only with CAP_READBACK)."""
        return None

    def quota(self) -> dict | None:
        """{'used': bytes, 'limit': bytes}, None if unknown."""
        return None

    def info(self) -> dict:
        """Free-form details for the report and the connection test."""
        return {}

    def has_folder(self, name: str) -> bool | None:
        """After plan_folders: does this folder already exist? None when the target cannot tell (dry run only)."""
        return None


class Target(abc.ABC):
    """Settings of a target (thread-safe, holds no connection). Makes the sessions."""
    capabilities: frozenset[str] = frozenset()

    @abc.abstractmethod
    def session(self) -> TargetSession:
        """A new session, not connected yet."""


def stamp_import_id(raw: bytes, import_id: str) -> bytes:
    """Adds the import id header at the top of a mail, unless the source already embedded this exact value.
    Another value already present (a mail imported earlier by this tool) stays below it: nothing is removed,
    and added headers do not invalidate DKIM signatures."""
    head = raw[:65536]
    wanted = import_id.encode("ascii", "replace")
    if wanted in head:
        return raw
    eol = b"\r\n" if b"\r\n" in head else b"\n"
    return IMPORT_HEADER.encode("ascii") + b": " + wanted + eol + raw


def subject_of(raw: bytes) -> str:
    """Decoded subject of an RFC 5322 message, "" when it cannot be read."""
    from email import policy
    from email.parser import BytesHeaderParser
    try:
        msg = BytesHeaderParser(policy=policy.default).parsebytes(raw[:65536])
        return " ".join(str(msg["Subject"] or "").split())[:500]
    except Exception:  # noqa: BLE001 - informative only
        return ""


__all__ = [
    "CALENDAR", "CAP_FLAGS", "CAP_FOLDERS", "CAP_INCREMENTAL", "CAP_INTERNAL_DATE", "CAP_PARALLEL", "CAP_QUOTA",
    "CAP_READBACK", "CAP_RULES", "CONTACTS", "Changes", "ConnectorError", "FetchedItem", "FolderPlan", "IMPORT_HEADER",
    "ITEM_KINDS", "MAIL", "OutgoingItem", "Source", "SourceFolder", "SourceItem", "TASKS", "Target",
    "TargetSession", "stamp_import_id", "subject_of",
]
