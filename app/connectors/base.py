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
# mirror mode (docs/planning.md "Mirror"): the source is only ever READ, the target changes only what mccb wrote
CAP_MIRROR = "mirror"                # source: mirror_listing() lists a folder completely or says it could not
CAP_DELETE = "delete"                # target: delete() removes one of OUR items (to the trash first when there is one)
CAP_UPDATE = "update"                # target: update() rewrites one of OUR items in place (events, contacts, tasks)
CAP_SET_FLAGS = "set_flags"          # target: set_flags() changes the flags of one of OUR mails
CAP_MOVE = "move"                    # target: move() moves one of OUR mails to another folder of the target
CAP_INVENTORY = "inventory"          # target: inventory() lists OUR items of a folder (needed by every mirror write)

# keyword put on a mail the mirror deleted (moved to the trash or flagged \Deleted): finds them again to undo
MIRROR_DELETED_FLAG = "$MccbMirrorDeleted"
# delete_mode() answers
DELETE_TRASH, DELETE_SOFT, DELETE_HARD = "trash", "soft", "hard"


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
    skip: str | None = None       # why this folder is not migrated (logged by the runner), e.g. a generated calendar


@dataclass
class MailboxSize:
    """Size of the mail of a source (Source.mailbox_size): bytes and number of messages. approx: an estimate (e.g. the
    quota usage of the account, which counts every folder), shown as such."""
    bytes: int
    count: int | None = None
    approx: bool = False


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
    # changes whenever the item changes at the source (DAV etag, EWS ChangeKey, Graph changeKey, JMAP state...),
    # None when unknown. Mail flags are compared on their own (`flags`): `version` is for the content.
    version: str | None = None


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
    label: str | None = None      # display name to give the folder when it is created (DAV collections)
    fallback: str | None = None   # where its items go if the target refuses to create it (DAV: the default collection)


@dataclass
class Changes:
    items: list[SourceItem]
    cursor: str | None            # opaque, to give back to the next changed_since()
    reset: bool = False           # True: `items` is a full listing (no cursor, or the cursor is no longer valid)
    # keys (SourceItem.key) the source reports as removed since the cursor (JMAP destroyed, Google cancelled /
    # metadata.deleted...). Informative: the mirror deletes only on a complete mirror_listing() + exists() check
    removed: list[str] = field(default_factory=list)


@dataclass
class Listing:
    """Answer of Source.mirror_listing(): the mirror deletes on the target only what a COMPLETE listing no longer has."""
    items: list[SourceItem]
    complete: bool = True         # False: something may be missing (paging cut short, partial answer...): no deletion
    epoch: str | None = None      # what the keys depend on (IMAP UIDVALIDITY): see Source.key_epoch()
    problem: str | None = None    # why the listing is not complete, for the report


@dataclass
class RemoteItem:
    """One of OUR items on the target, as TargetSession.inventory() finds it."""
    ref: str                      # identifier to give back to delete / update / set_flags / move (IMAP UID, DAV name)
    flags: list[str] = field(default_factory=list)
    version: str | None = None    # etag or equivalent, when the target has one


# epochs meaning "these keys cannot be trusted for deletions" (a folder renamed or moved, an unverified identity)
UNSTABLE_EPOCHS = ("renamed-or-moved", "unverified")

# the only Source methods the mirror calls (app/server/mirror.py wraps the source so that nothing else can be reached)
READ_ONLY_SOURCE_METHODS = frozenset({"open", "close", "folders", "iter_items", "fetch", "mirror_listing", "key_epoch",
                                      "exists"})


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

    def mailbox_size(self, folders: list[SourceFolder], deadline: float) -> MailboxSize | None:
        """Size of the mail folders among `folders` (from folders(), after open()), READ ONLY and without reading any
        body. None: the source cannot tell cheaply, or `deadline` (time.monotonic()) passed. Never a guess shown as
        exact: an estimate says approx=True."""
        return None

    def read_rules(self) -> list[dict]:
        """Mail rules as sieve.py expects them. Only for sources declaring CAP_RULES, called after open()."""
        return []

    # ---- mirror (only for sources declaring CAP_MIRROR). READ ONLY: never a write, a flag, a mark at the source.
    def mirror_listing(self, folder: SourceFolder) -> Listing:
        """Every item of the folder, with `version` and `flags` filled in when the protocol has them. A source that
        cannot be sure it saw everything (paging stopped early, an error swallowed) says complete=False: the mirror
        then deletes nothing in that folder. Errors are raised as ConnectorError (no deletion either)."""
        return Listing(list(self.iter_items(folder)))

    def exists(self, folder: SourceFolder, keys: list[str], refs: dict[str, Any] | None = None) -> dict[str, bool]:
        """Direct read, item by item, of keys a listing no longer has, just before the mirror deletes them on the
        target: {key: still at the source}. True also means "cannot tell": the item is then NOT deleted. The default
        (everything True) therefore blocks every deletion until a connector answers for real. `refs`: {key: the
        SourceItem.ref recorded when the item was written (items.src_ref, JSON round trip: a tuple comes back as a
        list), None when not recorded}, for protocols whose keys cannot be turned back into an id (hashes)."""
        return {k: True for k in keys}

    def key_epoch(self, key: str) -> str | None:
        """The epoch a key was made under (IMAP: the UIDVALIDITY inside it), None when keys have none. A tracked item
        whose key has another epoch than the current listing means the keys were reset: the mirror refuses the
        folder (`uidvalidity_changed`) instead of deleting everything and copying it again. An epoch (of a key or of
        Listing.epoch) containing one of UNSTABLE_EPOCHS refuses the folder as well."""
        return None


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

    # ---- mirror. Every method below acts on ONE item mccb wrote itself (found by inventory()), never on another.
    def inventory(self, folder: str, namespace: str, names: set[str] | None = None) -> dict[str, list[RemoteItem]]:
        """OUR items of the folder: {import id ("<namespace-key>", mail) or resource name (in `names`, DAV-like):
        copies}. Read only. Pre-existing content and items arrived later on the target are never in it.
        (CAP_INVENTORY)"""
        raise ConnectorError("this target cannot list its items for the mirror", ConnectorError.CONFIG)

    def delete_many(self, folder: str, refs: list[str],
                    expected_import_ids: dict[str, str] | None = None) -> dict[str, str]:
        """Several deletions in one go: {ref: mode returned by delete()} of those done; a ref missing from the answer
        failed (counted as an error, tried again at the next pass). Default: delete() one by one."""
        out = {}
        for ref in refs:
            out[ref] = self.delete(folder, ref, (expected_import_ids or {}).get(ref))
        return out

    def written_version(self, folder: str, ref: str) -> str | None:
        """The version (DAV: etag) of what this session last wrote to `ref`, None when unknown. Kept by the runner
        (items.target_etag): the mirror then touches the item only while the target still has that version."""
        return None

    def delete_mode(self, folder: str) -> str:
        """How delete() works in this folder: DELETE_TRASH (moved to the trash of the target, recoverable there),
        DELETE_SOFT (marked, still there) or DELETE_HARD (gone: the runner keeps a copy first, see mirror.py)."""
        return DELETE_HARD

    def delete(self, folder: str, ref: str, expected_import_id: str | None = None) -> str:
        """Removes one of our items, the way delete_mode() says; returns that mode. Already gone is not an error.
        A mail is marked with MIRROR_DELETED_FLAG. `expected_import_id`: the import id (mail) or resource name the
        item must still carry; a target that can check it refuses (ConnectorError REJECTED) when it differs. The
        mirror always calls inventory() and then delete() / update() on the SAME session. (CAP_DELETE)"""
        raise ConnectorError("this target cannot delete", ConnectorError.CONFIG)

    def update(self, folder: str, ref: str, item: OutgoingItem) -> str | None:
        """Replaces the content of one of our items (event, contact, task) by item.raw; returns its new ref.
        (CAP_UPDATE)"""
        raise ConnectorError("this target cannot update an item", ConnectorError.CONFIG)

    def set_flags(self, folder: str, ref: str, add: list[str], remove: list[str]) -> None:
        """Adds and removes flags of one of our mails (\\Seen, \\Flagged, \\Answered, keywords). (CAP_SET_FLAGS)"""
        raise ConnectorError("this target cannot change flags", ConnectorError.CONFIG)

    def move(self, folder: str, ref: str, to_folder: str) -> str | None:
        """Moves one of our mails to another folder of the target; returns its new ref if known. (CAP_MOVE)"""
        raise ConnectorError("this target cannot move an item", ConnectorError.CONFIG)


class Target(abc.ABC):
    """Settings of a target (thread-safe, holds no connection). Makes the sessions."""
    capabilities: frozenset[str] = frozenset()
    # True: an event, contact or task IS its resource name there (DAV): two items of one name are one resource
    names_resources: bool = False

    @abc.abstractmethod
    def session(self) -> TargetSession:
        """A new session, not connected yet."""

    def resource_name(self, raw: bytes, default: str) -> str:
        """Name to give a calendar, contact or task object (`default`: derived from its import id)."""
        return default


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
    "CALENDAR", "CAP_DELETE", "CAP_FLAGS", "CAP_FOLDERS", "CAP_INCREMENTAL", "CAP_INTERNAL_DATE", "CAP_INVENTORY",
    "CAP_MIRROR", "CAP_MOVE", "CAP_PARALLEL", "CAP_QUOTA", "CAP_READBACK", "CAP_RULES", "CAP_SET_FLAGS", "CAP_UPDATE",
    "CONTACTS", "Changes", "ConnectorError", "DELETE_HARD", "DELETE_SOFT", "DELETE_TRASH", "FetchedItem", "FolderPlan",
    "IMPORT_HEADER", "ITEM_KINDS", "Listing", "UNSTABLE_EPOCHS", "MAIL", "MIRROR_DELETED_FLAG", "OutgoingItem", "READ_ONLY_SOURCE_METHODS",
    "RemoteItem", "Source", "SourceFolder", "SourceItem", "TASKS", "Target", "TargetSession", "stamp_import_id",
    "subject_of",
]
