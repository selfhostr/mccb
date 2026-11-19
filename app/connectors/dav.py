"""CalDAV and CardDAV. As targets: one collection per account and item kind, deterministic resource names, idempotent
PUT. As sources: every collection, each object as it is on the server."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterator

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
                 labels: dict[str, str] | None = None):
        self.dav_kind = dav_kind  # caldav | carddav
        self.labels = labels if labels is not None else {}  # collection to create -> its name (shared by the target)
        self.component = component  # VTODO: a calendar that accepts tasks
        self.config = config
        self.password = secrets.get("password") or ""
        self.client: DavClient | None = None
        self._etags: dict[tuple[str, str], str] = {}  # (collection, name) -> etag seen by the last inventory()

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

    def plan_folders(self, folders: list[SourceFolder]) -> dict[str, FolderPlan]:
        """The default folder of the source goes to the default collection; every other one to the collection of the
        same display name, else to a new one (created by ensure_folder; refused: back to the default collection)."""
        from ..server.runner import folder_key
        from .exchange_base import is_default_folder
        default, home = self._discovery()
        by_label: dict[str, str] = {}
        if home is not None:
            try:
                rows = self._c().collections_at(self.dav_kind, default, home)
            except DavError as e:
                raise convert(e) from e
            for url, label, comps in rows:
                if self.dav_kind == "caldav" and comps and self.component not in comps:
                    continue
                by_label.setdefault(label.strip().casefold(), url)
        out: dict[str, FolderPlan] = {}
        # no folder says it is the default one: a lone folder (one file, one calendar) is
        lone = len(folders) == 1 and not is_default_folder(folders[0])
        for f in folders:
            label = " / ".join(p.strip() for p in f.path if p.strip()) or "Import"
            if lone or is_default_folder(f):
                out[folder_key(f.path)] = FolderPlan(default)
            elif label.casefold() in by_label:
                out[folder_key(f.path)] = FolderPlan(by_label[label.casefold()])
            elif home is None:  # the URL names one collection: no home to create others in
                out[folder_key(f.path)] = FolderPlan(default)
            else:
                url = home + collection_slug(self.dav_kind, self.component, label)
                self.labels[url] = label
                out[folder_key(f.path)] = FolderPlan(url, label=label, fallback=default)
        return out

    def _default_label(self) -> str:
        return "Contacts" if self.dav_kind == "carddav" else "Tasks" if self.component == "VTODO" else "Calendar"

    def has_folder(self, name: str) -> bool | None:
        try:
            return self._c()._is_collection(self.dav_kind, name)
        except DavError:
            return None

    def ensure_folder(self, name: str, flag: str | None = None, label: str | None = None) -> None:
        try:
            # without a label (a collection gone since the plan): the name it was planned under, never "PST Import"
            self._c().ensure_collection(self.dav_kind, name, self.component, label or self.labels.get(name) or self._default_label())
        except DavError as e:
            if e.status == 403:  # signed in already: a refused creation (MDaemon, shared homes), not a bad password
                raise ConnectorError(str(e), ConnectorError.REJECTED) from e
            raise convert(e) from e

    def put(self, folder: str, item: OutgoingItem) -> str | None:
        try:
            self._c().put(folder, item.name or "", item.raw.decode("utf-8", "replace"), self.dav_kind)
            return item.name
        except DavError as e:
            if e.status in (404, 409):  # collection gone since the discovery: recreated then retried by the runner
                raise ConnectorError(str(e), ConnectorError.FOLDER_MISSING) from e
            raise convert(e) from e

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
        for name, et in found.items():
            if names and name in names:
                self._etags[(folder, name)] = et
                out[name] = [RemoteItem(name, version=et or None)]
        return out

    def delete_mode(self, folder: str) -> str:
        return DELETE_HARD  # no trash in DAV: the runner keeps a copy (read()) before deleting

    def delete(self, folder: str, ref: str, expected_import_id: str | None = None) -> str:
        if expected_import_id is not None and expected_import_id != ref:
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
        return ref  # the etag kept is now stale: another write without a new inventory gets 412, never overwrites


def collection_slug(dav_kind: str, component: str, label: str) -> str:
    """Path segment of a collection created for a source folder: readable, stable across runs, distinct per kind
    (CalDAV and CardDAV often share their home)."""
    text = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40] or "folder"
    digest = hashlib.sha1(f"{dav_kind}:{component}:{label}".encode("utf-8")).hexdigest()[:8]  # noqa: S324
    return f"mccb-{text}-{digest}/"


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
    dav_kind = "caldav"
    component = "VEVENT"

    def resource_name(self, raw: bytes, default: str) -> str:
        return uid_name(raw, default.rsplit(".", 1)[-1] if "." in default else
                        ("vcf" if self.dav_kind == "carddav" else "ics")) or default

    def __init__(self, config: dict, secrets: dict, ctx: dict | None = None):
        self.config = config
        self.secrets = secrets
        self.labels: dict[str, str] = {}

    def session(self) -> DavSession:
        return DavSession(self.dav_kind, self.config, self.secrets, self.component, self.labels)

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
        seen: set[str] = set()
        for url, label, comps in rows:
            kinds = [CONTACTS] if self.dav_kind == "carddav" else [
                k for k, c in ((CALENDAR, "VEVENT"), (TASKS, "VTODO")) if not comps or c in comps]
            for kind in kinds:
                out.append(SourceFolder(path=[label], kind=kind, special="default" if kind not in seen else None, ref=url))
                seen.add(kind)
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
