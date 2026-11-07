"""CalDAV and CardDAV. As targets: one collection per account and item kind, deterministic resource names, idempotent
PUT. As sources: every collection, each object as it is on the server."""
from __future__ import annotations

import hashlib
import re
from typing import Iterator

from ..server.dav import DavClient, DavError
from . import registry
from .base import (CALENDAR, CAP_FOLDERS, CAP_READBACK, CONTACTS, TASKS, ConnectorError, FetchedItem, FolderPlan,
                   OutgoingItem, Source, SourceFolder, SourceItem, Target, TargetSession)
from .imap import folder_hash


def convert(e: DavError) -> ConnectorError:
    if e.status in (401, 403):
        return ConnectorError(str(e), ConnectorError.AUTH)
    if e.temporary:
        return ConnectorError(str(e), ConnectorError.TRANSIENT)
    return ConnectorError(str(e), ConnectorError.REJECTED)


class DavSession(TargetSession):
    def __init__(self, dav_kind: str, config: dict, secrets: dict, component: str = "VEVENT"):
        self.dav_kind = dav_kind  # caldav | carddav
        self.component = component  # VTODO: a calendar that accepts tasks
        self.config = config
        self.password = secrets.get("password") or ""
        self.client: DavClient | None = None

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

    def _collection(self) -> str:
        cfg = self.config
        try:
            return self._c().discover(self.dav_kind, cfg.get("url") or None, cfg.get("email") or cfg.get("login") or "",
                                      cfg.get("discovery_host") or "", component=self.component)
        except DavError as e:
            raise convert(e) from e

    def plan_folders(self, folders: list[SourceFolder]) -> dict[str, FolderPlan]:
        from ..server.runner import folder_key
        url = self._collection()  # every source folder of the kind goes to the same collection, as for a PST
        return {folder_key(f.path): FolderPlan(url) for f in folders}

    def ensure_folder(self, name: str, flag: str | None = None) -> None:
        try:
            self._c().ensure_collection(self.dav_kind, name, self.component)
        except DavError as e:
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


class DavTarget(Target):
    capabilities = frozenset({CAP_READBACK})
    dav_kind = "caldav"
    component = "VEVENT"

    def __init__(self, config: dict, secrets: dict, ctx: dict | None = None):
        self.config = config
        self.secrets = secrets

    def session(self) -> DavSession:
        return DavSession(self.dav_kind, self.config, self.secrets, self.component)

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

    def iter_items(self, folder: SourceFolder) -> Iterator[SourceItem]:
        try:
            names = sorted(self._c().names(folder.ref))
        except DavError as e:
            raise convert(e) from e
        for name in names:
            digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:32]  # noqa: S324 - identifier, not security
            yield SourceItem(key=f"{folder_hash(folder.path)}-{digest}", ref=name)

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
    source=registry.Role(CalDavSource, (CALENDAR, TASKS), frozenset({CAP_FOLDERS}), _FIELDS, identity),
    target=registry.Role(CalDavTarget, (CALENDAR, TASKS), CalDavTarget.capabilities, _FIELDS, identity)))
registry.register(registry.ConnectorSpec(
    kind="carddav", label="CardDAV", description="Address books on a CardDAV server.",
    source=registry.Role(CardDavSource, (CONTACTS,), frozenset({CAP_FOLDERS}), _FIELDS, identity),
    target=registry.Role(CardDavTarget, (CONTACTS,), CardDavTarget.capabilities, _FIELDS, identity)))
