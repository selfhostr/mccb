"""CalDAV and CardDAV as targets: one collection per account, deterministic resource names, idempotent PUT."""
from __future__ import annotations

from ..server.dav import DavClient, DavError
from . import registry
from .base import (CALENDAR, CAP_READBACK, CONTACTS, ConnectorError, FolderPlan, OutgoingItem, SourceFolder, Target,
                   TargetSession)


def convert(e: DavError) -> ConnectorError:
    if e.status in (401, 403):
        return ConnectorError(str(e), ConnectorError.AUTH)
    if e.temporary:
        return ConnectorError(str(e), ConnectorError.TRANSIENT)
    return ConnectorError(str(e), ConnectorError.REJECTED)


class DavSession(TargetSession):
    def __init__(self, dav_kind: str, config: dict, secrets: dict):
        self.dav_kind = dav_kind  # caldav | carddav
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
                                      cfg.get("discovery_host") or "")
        except DavError as e:
            raise convert(e) from e

    def plan_folders(self, folders: list[SourceFolder]) -> dict[str, FolderPlan]:
        from ..server.runner import folder_key
        url = self._collection()  # every source folder of the kind goes to the same collection, as for a PST
        return {folder_key(f.path): FolderPlan(url) for f in folders}

    def ensure_folder(self, name: str, flag: str | None = None) -> None:
        try:
            self._c().ensure_collection(self.dav_kind, name)
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

    def __init__(self, config: dict, secrets: dict, ctx: dict | None = None):
        self.config = config
        self.secrets = secrets

    def session(self) -> DavSession:
        return DavSession(self.dav_kind, self.config, self.secrets)


class CalDavTarget(DavTarget):
    dav_kind = "caldav"


class CardDavTarget(DavTarget):
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
    kind="caldav", label="CalDAV", description="Calendars on a CalDAV server.",
    target=registry.Role(CalDavTarget, (CALENDAR,), CalDavTarget.capabilities, _FIELDS, identity)))
registry.register(registry.ConnectorSpec(
    kind="carddav", label="CardDAV", description="Address books on a CardDAV server.",
    target=registry.Role(CardDavTarget, (CONTACTS,), CardDavTarget.capabilities, _FIELDS, identity)))
