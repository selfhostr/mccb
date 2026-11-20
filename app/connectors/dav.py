"""CalDAV and CardDAV. As targets: one collection per account and item kind, deterministic resource names, idempotent
PUT. As sources: every collection, each object as it is on the server."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import unquote

from ..server.dav import DavClient, DavError, DavIncomplete
from . import registry
from .base import (CALENDAR, CAP_DELETE, CAP_FOLDERS, CAP_INVENTORY, CAP_MIRROR, CAP_READBACK, CAP_UPDATE, CONTACTS,
                   DELETE_HARD, TASKS, ConnectorError, FetchedItem, FolderPlan, Listing, OutgoingItem, RemoteItem, Source,
                   SourceFolder, SourceItem, Target, TargetSession)
from .imap import folder_hash


def convert(e: DavError) -> ConnectorError:
    if e.status in (401, 403):
        return ConnectorError(str(e), ConnectorError.AUTH)
    if e.temporary:
        return ConnectorError(str(e), ConnectorError.TRANSIENT)
    return ConnectorError(str(e), ConnectorError.REJECTED)


class DavSession(TargetSession):
    def __init__(self, dav_kind: str, config: dict, secrets: dict, component: str = "VEVENT",
                 labels: dict[str, str] | None = None, listed: dict[str, str] | None = None):
        self.dav_kind = dav_kind  # caldav | carddav
        self.labels = labels if labels is not None else {}  # collection to create -> its name (shared by the target)
        # url_key of every collection the plan saw listed -> its name (shared by the target): never created
        self.listed = listed if listed is not None else {}
        # set by the runner before plan_folders: {"previous": url_key of the collections the items of earlier runs
        # went to, "pinned": {folder key: collection an earlier run of the job chose}}
        self.history: dict = {}
        self.notes: list[str] = []  # the last plan, in words (logged by the runner)
        self.component = component  # VTODO: a calendar that accepts tasks
        self.config = config
        self.password = secrets.get("password") or ""
        self.client: DavClient | None = None
        self._etags: dict[tuple[str, str], str] = {}  # (collection, name) -> etag seen by the last inventory()
        self._written: dict[tuple[str, str], str] = {}  # (collection, name) -> etag of what this session wrote
        self.keep_versions = False  # the mirror: an etag the PUT does not give back is read

    @property
    def connected(self) -> bool:
        return self.client is not None

    def connect(self) -> None:
        self.close()  # DAV has no session: authentication is checked by the first request
        self.client = DavClient(self.config.get("login") or "", self.password,
                                bool(self.config.get("verify_tls", True)))

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        self.client = None

    def _c(self) -> DavClient:
        self.ensure()
        return self.client  # type: ignore[return-value]

    def _discovery(self) -> tuple[str, str | None]:
        """(default collection, created when there is none; home of the collections, None when unknown)."""
        cfg = self.config
        c = self._c()
        try:
            coll, home = c._discover(self.dav_kind, cfg.get("url") or None, cfg.get("email") or cfg.get("login") or "",
                                     cfg.get("discovery_host") or "", True, self.component)
        except DavError as e:
            raise convert(e) from e
        if coll is None:
            raise ConnectorError(f"no {self.dav_kind} collection found", ConnectorError.REJECTED)
        return coll, home

    def _collection(self) -> str:
        return self._discovery()[0]

    def _collections(self, default: str, home: str | None) -> list[Collection]:
        """The collections of the target that can take this kind of item, as the server lists them (all of them
        are kept in `listed`). A collection that grants no write is left out, unless none does (a server that
        says it wrongly would otherwise get a second copy of every collection)."""
        try:
            rows = self._c().listing(self.dav_kind, default, home, privileges=True)
        except DavError as e:
            raise convert(e) from e
        usable = []
        for url, label, comps, writable in rows:
            self.listed[url_key(url)] = label
            if self.dav_kind == "caldav" and comps and self.component not in comps:
                continue
            usable.append(Collection(url, label, writable))
        if not any(c.writable for c in usable):
            return usable
        return [c for c in usable if c.writable]

    def plan_folders(self, folders: list[SourceFolder]) -> dict[str, FolderPlan]:
        """Each source folder goes to an existing collection when there is one for it (choose_collection: the
        default folder of the source to the default collection of the target, the others to the collection of the
        same name), else to a new one of its name (created by ensure_folder; refused: to the default collection).
        A name the target already has is never created a second time. `notes`: the plan, said in the job's log."""
        from ..server.runner import folder_key
        from .exchange_base import is_default_folder
        default, home = self._discovery()
        colls = self._collections(default, home)
        kind = item_kind(self.dav_kind, self.component)
        previous, pinned = self.history.get("previous", set()), self.history.get("pinned", {})
        out: dict[str, FolderPlan] = {}
        self.notes = []
        # no folder says it is the default one: a lone folder (one file, one calendar) is
        lone = len(folders) == 1 and not is_default_folder(folders[0])
        for f in folders:
            fk = folder_key(f.path)
            label = " / ".join(p.strip() for p in f.path if p.strip()) or "Import"
            is_default = lone or is_default_folder(f)
            hit, why = choose_collection(kind, label, is_default, colls, default, previous, pinned.get(fk))
            if hit is not None:
                out[fk] = FolderPlan(hit.url)
                self.notes.append(f"'{label}' -> existing '{hit.label}' ({hit.url}, {why})")
            elif is_default or home is None:  # home None: the URL names one collection, no home for others
                out[fk] = FolderPlan(default)
                self.notes.append(f"'{label}' -> {default} (the collection found by the discovery)")
            else:
                url = home + collection_slug(self.dav_kind, self.component, label)
                self.labels[url] = label
                out[fk] = FolderPlan(url, label=label, fallback=default)
                self.notes.append(f"'{label}' -> new collection '{label}' ({url})")
        return out

    def _default_label(self) -> str:
        return "Contacts" if self.dav_kind == "carddav" else "Tasks" if self.component == "VTODO" else "Calendar"

    def has_folder(self, name: str) -> bool | None:
        try:
            return self._c()._is_collection(self.dav_kind, name)
        except DavError:
            return None

    def ensure_folder(self, name: str, flag: str | None = None, label: str | None = None) -> None:
        """The collection `name` exists. One the server listed when the plan was made is never created (over
        itself or a second time): an error if it no longer opens. One to create is not created when the target now
        has a collection of that name at another URL (a second "Contacts"): refused, its items go to the fallback."""
        c = self._c()
        listed = self.listed.get(url_key(name))
        # without a label (a collection gone since the plan): the name it was planned under, never "PST Import"
        want = label or self.labels.get(name) or listed or self._default_label()
        try:
            if listed is not None:
                c.ensure_collection(self.dav_kind, name, self.component, want, create=False)
                return
            if c._probe(self.dav_kind, name) is None:
                return
            default, home = self._discovery()
            for url, other, _comps, _w in c.listing(self.dav_kind, default, home):
                if url_key(url) != url_key(name) and norm_name(other) == norm_name(want):
                    raise ConnectorError(f"the target already has a collection named '{other}' ({url}): a second "
                                         f"one is not created", ConnectorError.REJECTED)
            c.ensure_collection(self.dav_kind, name, self.component, want)
        except DavError as e:
            if e.status == 403:  # signed in already: a refused creation (MDaemon, shared homes), not a bad password
                raise ConnectorError(str(e), ConnectorError.REJECTED) from e
            raise convert(e) from e

    def put(self, folder: str, item: OutgoingItem) -> str | None:
        """Writes the resource, over one already there as mccb always did (a migration is idempotent: its items
        rows may be gone, its namespace new, while the resources it wrote are still on the target). The first PUT
        says If-None-Match: *, so that a write over an existing resource is known: it then goes with If-Match on
        the etag just read, and is marked "!" in written_version(): its owner is not proven, and the mirror never
        deletes nor updates it on that ground."""
        name = item.name or ""
        text = item.raw.decode("utf-8", "replace")
        c = self._c()
        mark = ""
        try:
            c.put(folder, name, text, self.dav_kind, if_none_match=True)
        except DavError as e:
            if e.status in (404, 409):  # collection gone since the discovery: recreated then retried by the runner
                raise ConnectorError(str(e), ConnectorError.FOLDER_MISSING) from e
            if e.status != 412:
                raise convert(e) from e
            mine = self._written.get((folder, name))
            mark = "" if mine and not mine.startswith("!") else "!"  # written by this very session: still ours
            try:
                old = c.etag_of(folder, name)
                c.put(folder, name, text, self.dav_kind, if_match=old or None)
            except DavError as e2:
                if e2.status in (404, 409):
                    raise ConnectorError(str(e2), ConnectorError.FOLDER_MISSING) from e2
                raise convert(e2) from e2
        self._remember(folder, name, mark)
        return item.name

    def _remember(self, folder: str, name: str, mark: str = "") -> None:
        """Keeps the etag of a write. Without one in the answer, it is read (PROPFIND) only for the mirror
        (keep_versions): a normal migration does not pay a request more per item."""
        c = self._c()
        et = getattr(c, "last_etag", "") or ""
        if not et and self.keep_versions:
            try:
                et = c.etag_of(folder, name)
            except DavError:
                et = ""
        if et or mark:
            self._written[(folder, name)] = mark + et
        else:
            self._written.pop((folder, name), None)

    def written_version(self, folder: str, ref: str) -> str | None:
        """The etag of what this session last wrote to `ref`, "!" first when it was written over a resource that
        was already there (None: unknown)."""
        return self._written.get((folder, ref))

    def find(self, folder: str, item: OutgoingItem) -> list[str]:
        if not item.name:
            return []
        try:
            return [item.name] if self._c().exists(folder, item.name) else []
        except DavError as e:
            raise convert(e) from e

    def count_ours(self, folder: str, namespace: str, names: set[str] | None = None) -> int:
        try:  # only OUR resources count, not pre-existing content
            return len(self._c().names(folder) & (names or set()))
        except DavError as e:
            raise convert(e) from e

    def read(self, folder: str, remote_id: str) -> bytes | None:
        try:
            text = self._c().get(folder, remote_id)
        except DavError as e:
            raise convert(e) from e
        return text.encode("utf-8") if text is not None else None

    # ---- mirror: only the resources named in `names` (ours), with If-Match on what the inventory saw
    def inventory(self, folder: str, namespace: str, names: set[str] | None = None) -> dict[str, list[RemoteItem]]:
        try:
            found = self._c().names_etags(folder, strict=True)  # cut short: an error, not a smaller collection
        except DavError as e:
            if e.status == 404:
                return {}
            raise convert(e) from e
        out = {}
        wanted = {n.casefold(): n for n in names or ()}  # some servers do not keep the case of a name
        for name, et in found.items():
            asked = wanted.get(name.casefold())
            if asked is not None:
                self._etags[(folder, name)] = et
                out[asked] = [RemoteItem(name, version=et or None)]
        return out

    def delete_mode(self, folder: str) -> str:
        return DELETE_HARD  # no trash in DAV: the runner keeps a copy (read()) before deleting

    def delete(self, folder: str, ref: str, expected_import_id: str | None = None) -> str:
        if expected_import_id is not None and expected_import_id.casefold() != ref.casefold():
            raise ConnectorError(f"{ref} is not {expected_import_id}: not deleted", ConnectorError.REJECTED)
        try:
            sc = self._c().delete_item(folder, ref, self._etags.get((folder, ref)))
        except DavError as e:
            raise convert(e) from e
        if sc == 412:
            raise ConnectorError(f"{ref} changed on the target since it was listed: not deleted",
                                 ConnectorError.REJECTED)
        self._etags.pop((folder, ref), None)
        return DELETE_HARD

    def update(self, folder: str, ref: str, item: OutgoingItem) -> str | None:
        """PUT over our resource, with If-Match on the etag the inventory saw: changed on the target since (412),
        it is not overwritten (a temporary error: the next pass sees it again)."""
        etag = self._etags.get((folder, ref))
        try:
            self._c().put(folder, ref, item.raw.decode("utf-8", "replace"), self.dav_kind, if_match=etag or None)
        except DavError as e:
            if e.status in (404, 409):
                raise ConnectorError(str(e), ConnectorError.FOLDER_MISSING) from e
            raise convert(e) from e
        self._remember(folder, ref)  # the etag of the inventory stays: a second write without one gets 412
        return ref


def collection_slug(dav_kind: str, component: str, label: str) -> str:
    """Path segment of a collection created for a source folder: readable, stable across runs, distinct per kind
    (CalDAV and CardDAV often share their home)."""
    text = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40] or "folder"
    digest = hashlib.sha1(f"{dav_kind}:{component}:{label}".encode("utf-8")).hexdigest()[:8]  # noqa: S324
    return f"mccb-{text}-{digest}/"


# ------------------------------------------------------------------ which collection for which source folder
# lists some servers keep as address books (SmarterMail), under the name of the account's language: the one of the
# source goes to the one of the target, never to a second list of another name
SENDER_LISTS = (
    frozenset({"allowed senders", "trusted senders", "safe senders", "expéditeurs autorisés",
               "expéditeurs approuvés", "erlaubte absender", "vertrauenswürdige absender", "remitentes permitidos",
               "mittenti consentiti", "mittenti attendibili", "toegestane afzenders", "remetentes permitidos"}),
    frozenset({"blocked senders", "expéditeurs bloqués", "blockierte absender", "gesperrte absender",
               "remitentes bloqueados", "mittenti bloccati", "geblokkeerde afzenders", "remetentes bloqueados"}),
)
_MADE_BY_MCCB = re.compile(r"/(?:mccb-[a-z0-9-]*|import-pst-[a-z]+)/?$")


def norm_name(label: str) -> str:
    """A display name as compared: case, accents composition and spaces do not count."""
    return " ".join(unicodedata.normalize("NFC", label or "").split()).casefold()


def url_key(url: str) -> str:
    """A collection URL as compared: the servers do not all encode the same characters."""
    return unquote(url if url.endswith("/") else url + "/")


def item_kind(dav_kind: str, component: str) -> str:
    return CONTACTS if dav_kind == "carddav" else TASKS if component == "VTODO" else CALENDAR


def is_default_name(kind: str, label: str) -> bool:
    from .exchange_base import DEFAULT_NAMES
    return norm_name(label) in {norm_name(n) for n in DEFAULT_NAMES.get(kind, ())}


def made_by_mccb(url: str) -> bool:
    """A collection mccb created (collection_slug, or "import-pst-*" of the discovery)."""
    return bool(_MADE_BY_MCCB.search(url_key(url).lower()))


def default_index(kind: str, labels: list[str]) -> int | None:
    """Which of these collections (in the order the server lists them) is the default one: the only one, else the
    first with the default name of its kind in some language ("Contacts", "Calendrier", "Kalender"...), else the
    first. None when there is none."""
    if not labels:
        return None
    return next((i for i, label in enumerate(labels) if is_default_name(kind, label)), 0)


@dataclass
class Collection:
    url: str
    label: str
    writable: bool = True


def choose_collection(kind: str, label: str, default: bool, colls: list[Collection], discovered: str | None,
                      previous: set[str] | frozenset = frozenset(), pinned: str | None = None
                      ) -> tuple[Collection | None, str]:
    """The existing collection a source folder goes to, None: one to create. (collection, why).

    `colls`: the collections of the target that can take these items, in the order the server lists them;
    `discovered`: the one the discovery found first; `previous`: url_key of the collections the items of earlier runs
    went to; `pinned`: the collection an earlier run of this job chose for this folder (kept while it is there).

    Candidates: the collections of the same name (whatever the case); the list of the same role (SENDER_LISTS); for
    the default folder of the source, also every collection with a default name, then the discovered one. Among
    them, never one that duplicates another candidate: a collection mccb created next to one of the server's own, or
    a collection holding the items of an earlier run next to one of the same name that holds none (the second
    "Contacts" an earlier run made). Then one already used by an earlier run, then the closest name, then the order
    of the server."""
    if pinned is not None:
        hit = next((c for c in colls if url_key(c.url) == url_key(pinned)), None)
        if hit is not None:
            return hit, "chosen by an earlier run"
    name = norm_name(label)
    group = next((g for g in SENDER_LISTS if name in g), frozenset())
    cands: list[tuple[int, int, Collection]] = []
    for i, c in enumerate(colls):
        n = norm_name(c.label)
        if n == name:
            tier = 0
        elif n in group or (default and is_default_name(kind, c.label)):
            tier = 1
        elif default and discovered is not None and url_key(c.url) == url_key(discovered):
            tier = 2
        else:
            continue
        cands.append((tier, i, c))
    if not cands:
        return None, "no collection of that name"
    prev = {url_key(u) for u in previous}

    def extra(c: Collection) -> bool:
        if made_by_mccb(c.url) and any(not made_by_mccb(o.url) for _t, _i, o in cands):
            return True
        return url_key(c.url) in prev and any(
            o is not c and norm_name(o.label) == norm_name(c.label) and url_key(o.url) not in prev
            for _t, _i, o in cands)

    tier, _i, best = min(cands, key=lambda e: (extra(e[2]), url_key(e[2].url) not in prev, e[0], e[1]))
    why = ("same name" if tier == 0 else "same list" if group and norm_name(best.label) in group
           else "default collection" if tier == 1 else "first collection of the server")
    return best, why


def default_collection(client: DavClient, dav_kind: str, template: str | None, email: str, host: str,
                       previous: set[str] | frozenset = frozenset(), component: str = "VEVENT") -> str:
    """The default collection of an account (the PST path: every folder of a kind goes there): the one an earlier
    run used, else the one with the default name of its kind, else the first the discovery finds (created when
    there is none). Raises DavError."""
    coll, home = client._discover(dav_kind, template, email, host, True, component)
    if coll is None:
        raise DavError(f"no {dav_kind} collection found")
    colls = [Collection(url, label, w) for url, label, comps, w in client.listing(dav_kind, coll, home, privileges=True)
             if not (dav_kind == "caldav" and comps and component not in comps)]
    if any(c.writable for c in colls):
        colls = [c for c in colls if c.writable]
    hit, _why = choose_collection(item_kind(dav_kind, component), "", True, colls, coll, previous)
    return hit.url if hit is not None else coll


_UID = re.compile(rb"^UID(?:;[^:\r\n]*)?:([^\r\n]*)", re.M | re.I)
_SAFE_UID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:+=-]{0,199}")


def uid_name(raw: bytes, ext: str) -> str | None:
    """"<UID>.<ext>" when the UID of the object is safe as a file name. Some servers (MDaemon) store an object under
    that name whatever the URL of the PUT: naming it so ourselves keeps our resources recognisable on every server."""
    unfolded = re.sub(rb"\r?\n[ \t]", b"", raw)
    m = _UID.search(unfolded)
    if not m:
        return None
    uid = m.group(1).decode("utf-8", "replace").strip()
    return f"{uid}.{ext}" if _SAFE_UID.fullmatch(uid) else None


class DavTarget(Target):
    capabilities = frozenset({CAP_READBACK, CAP_INVENTORY, CAP_DELETE, CAP_UPDATE})
    names_resources = True  # an item IS its resource name: two items of one name are one resource
    keep_versions = False   # set by a mirror job: every write's etag is read when the PUT does not give it
    dav_kind = "caldav"
    component = "VEVENT"

    def resource_name(self, raw: bytes, default: str) -> str:
        return uid_name(raw, default.rsplit(".", 1)[-1] if "." in default else
                        ("vcf" if self.dav_kind == "carddav" else "ics")) or default

    def __init__(self, config: dict, secrets: dict, ctx: dict | None = None):
        self.config = config
        self.secrets = secrets
        self.labels: dict[str, str] = {}
        self.listed: dict[str, str] = {}

    def session(self) -> DavSession:
        session = DavSession(self.dav_kind, self.config, self.secrets, self.component, self.labels, self.listed)
        session.keep_versions = self.keep_versions
        return session

    def for_item(self, item: str) -> "DavTarget":
        """The target to use for this item kind (wiring.py): tasks need a calendar that accepts VTODO."""
        return self


class CardDavTarget(DavTarget):
    dav_kind = "carddav"


class CalDavTasksTarget(DavTarget):
    dav_kind = "caldav"
    component = "VTODO"


class CalDavTarget(DavTarget):
    dav_kind = "caldav"

    def for_item(self, item: str) -> DavTarget:
        return CalDavTasksTarget(self.config, self.secrets) if item == TASKS else self


# ------------------------------------------------------------------ source
_SUMMARY = re.compile(rb"^(?:SUMMARY|FN)[^:\r\n]*:(.*)$", re.M)


class DavSource(Source):
    """Reads every collection of the account. Objects are copied as they are: nothing is parsed but the kind."""
    dav_kind = "caldav"

    def __init__(self, config: dict, secrets: dict, ctx: dict | None = None):
        self.config = config
        self.password = secrets.get("password") or ""
        self.client: DavClient | None = None

    def open(self) -> None:
        self.close()
        self.client = DavClient(self.config.get("login") or "", self.password, bool(self.config.get("verify_tls", True)))

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        self.client = None

    def _c(self) -> DavClient:
        if self.client is None:
            raise ConnectorError("DAV source not connected", ConnectorError.TRANSIENT)
        return self.client

    def folders(self) -> list[SourceFolder]:
        cfg = self.config
        try:
            rows = self._c().collections(self.dav_kind, cfg.get("url") or None, cfg.get("email") or cfg.get("login") or "",
                                         cfg.get("discovery_host") or "")
        except DavError as e:
            raise convert(e) from e
        out: list[SourceFolder] = []
        for url, label, comps in rows:
            kinds = [CONTACTS] if self.dav_kind == "carddav" else [
                k for k, c in ((CALENDAR, "VEVENT"), (TASKS, "VTODO")) if not comps or c in comps]
            for kind in kinds:
                out.append(SourceFolder(path=[label], kind=kind, ref=url))
        # the default one of each kind: by its name, else the first (default_index); never a list such as
        # "Allowed Senders" because the server lists it first
        for kind in (CALENDAR, TASKS, CONTACTS):
            mine = [f for f in out if f.kind == kind]
            i = default_index(kind, [f.path[0] for f in mine])
            if i is not None:
                mine[i].special = "default"
        return out

    def _key(self, folder: SourceFolder, name: str) -> str:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:32]  # noqa: S324 - identifier, not security
        return f"{folder_hash(folder.path)}-{digest}"

    def iter_items(self, folder: SourceFolder) -> Iterator[SourceItem]:
        """Every resource with its etag (`version`: the mirror updates an item whose etag changed)."""
        try:
            found = self._c().names_etags(folder.ref)
        except DavError as e:
            raise convert(e) from e
        for name in sorted(found):
            yield SourceItem(key=self._key(folder, name), ref=name, version=found[name] or None)

    # ---- mirror: PROPFIND and GET only
    def _listed(self, folder: SourceFolder, found: dict[str, str]) -> list[SourceItem]:
        return [SourceItem(key=self._key(folder, n), ref=n, version=found[n] or None) for n in sorted(found)]

    def mirror_listing(self, folder: SourceFolder) -> Listing:
        """The PROPFIND of the collection; complete unless the server says in it that it cut it short (507)."""
        try:
            found = self._c().names_etags(folder.ref, strict=True)
        except DavIncomplete as e:
            return Listing(self._listed(folder, e.partial or {}), complete=False, problem=str(e))
        except DavError as e:
            raise convert(e) from e
        return Listing(self._listed(folder, found))

    def exists(self, folder: SourceFolder, keys: list[str], refs: dict | None = None) -> dict[str, bool]:
        """GET of each resource whose name was recorded. A key without a recorded name cannot be read by itself
        (the key is a digest of the name) and another listing would only repeat the one that missed it: answered
        present (kept), as EWS and Graph do."""
        out = {k: True for k in keys}
        named = {k: r for k, r in (refs or {}).items() if isinstance(r, str) and k in out}
        try:
            for k, name in named.items():
                out[k] = self._c().get(folder.ref, name) is not None
        except DavError as e:
            raise convert(e) from e
        return out

    def fetch(self, folder: SourceFolder, item: SourceItem, import_id: str) -> FetchedItem | None:
        try:
            text = self._c().get(folder.ref, item.ref)
        except DavError as e:
            raise convert(e) from e
        if text is None:
            return None
        raw = text.encode("utf-8")
        wanted = {CALENDAR: b"BEGIN:VEVENT", TASKS: b"BEGIN:VTODO", CONTACTS: b"BEGIN:VCARD"}[folder.kind]
        if wanted not in raw.upper():
            return None  # a collection holding events and tasks is listed once per kind
        m = _SUMMARY.search(raw)
        return FetchedItem(raw=raw, subject=m.group(1).decode("utf-8", "replace").strip() if m else "")


class CalDavSource(DavSource):
    dav_kind = "caldav"


class CardDavSource(DavSource):
    dav_kind = "carddav"


def identity(config: dict) -> str:
    return f"{config.get('url') or config.get('discovery_host') or ''}:{config.get('login', '')}"


_FIELDS = (
    registry.Field("url", "URL", "url", help="Empty: discovery from the host below. May contain {email}, {user}, "
                                             "{domain}, {login}."),
    registry.Field("discovery_host", "Host for discovery"),
    registry.Field("email", "Email address"),
    registry.Field("login", "Login", required=True),
    registry.Field("verify_tls", "Verify the TLS certificate", "boolean", default=True),
    registry.Field("password", "Password", "password", required=True, secret=True),
)

registry.register(registry.ConnectorSpec(
    kind="caldav", label="CalDAV", description="Calendars and tasks on a CalDAV server.",
    source=registry.Role(CalDavSource, (CALENDAR, TASKS), frozenset({CAP_FOLDERS, CAP_MIRROR}), _FIELDS, identity),
    target=registry.Role(CalDavTarget, (CALENDAR, TASKS), CalDavTarget.capabilities, _FIELDS, identity)))
registry.register(registry.ConnectorSpec(
    kind="carddav", label="CardDAV", description="Address books on a CardDAV server.",
    source=registry.Role(CardDavSource, (CONTACTS,), frozenset({CAP_FOLDERS, CAP_MIRROR}), _FIELDS, identity),
    target=registry.Role(CardDavTarget, (CONTACTS,), CardDavTarget.capabilities, _FIELDS, identity)))
