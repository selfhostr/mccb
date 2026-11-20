"""An IMAP source with the CalDAV / CardDAV of the same server: mail from IMAP, calendars, tasks and contacts from
DAV, in one job. Built by wiring.source_of_account when the IMAP source is on a server saved with DAV on.

The mail part is the IMAP source itself, untouched: same folders, same keys, same namespace as without DAV, so
what an earlier run copied is never copied again. DAV keys ("<folder hash>-<32 hex>") never look like IMAP keys
("<folder hash>-<UIDVALIDITY>-<UID>", "<folder hash>-m<20 hex>"...), and items rows are per kind anyway.

A DAV part that cannot be read does not stop the mail: its folders are left out of the listing and the reason is
in `problems` (the runner logs it and ends the job with errors). The source is only read, as each part is."""
from __future__ import annotations

import re
from typing import Iterator

from .base import (CALENDAR, CONTACTS, MAIL, TASKS, Changes, ConnectorError, FetchedItem, Listing, MailboxSize,
                   Source, SourceFolder, SourceItem)

DAV_KEY_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{32}$")  # see DavSource._key
PART_OF_KIND = {CALENDAR: "caldav", TASKS: "caldav", CONTACTS: "carddav"}
PHASES_OF_PART = {"caldav": (CALENDAR, TASKS), "carddav": (CONTACTS,)}


class ImapDavSource(Source):
    def __init__(self, mail: Source, dav: dict[str, Source], where: str = ""):
        self.mail = mail
        self.dav = dav            # {"caldav": CalDavSource, "carddav": CardDavSource}, either may be missing
        self.where = where        # name of the server, for the messages
        self.problems: list[str] = []   # DAV parts that could not be listed in the last folders()
        self.failed: set[str] = set()   # their phases (calendar, tasks, contacts)

    @property
    def native_code(self) -> bool:  # type: ignore[override]
        return bool(getattr(self.mail, "native_code", False))

    def _part(self, folder: SourceFolder) -> Source:
        if folder.kind == MAIL:
            return self.mail
        part = self.dav.get(PART_OF_KIND.get(folder.kind, ""))
        if part is None:
            raise ConnectorError(f"no source for {folder.kind} folders", ConnectorError.CONFIG)
        return part

    def open(self) -> None:
        self.mail.open()
        for part in self.dav.values():
            part.open()  # DAV: no network here, a refused login is seen by folders()

    def close(self) -> None:
        self.mail.close()
        for part in self.dav.values():
            part.close()

    def folders(self) -> list[SourceFolder]:
        out = list(self.mail.folders())  # an IMAP error is the job's, as without DAV
        self.problems, self.failed = [], set()
        for name, part in self.dav.items():
            try:
                found = [f for f in part.folders() if f.kind in PHASES_OF_PART[name]]
            except ConnectorError as e:
                self.problems.append(f"{name.upper()} of the source server{f' {self.where!r}' if self.where else ''} "
                                     f"could not be read ({e})")
                self.failed |= set(PHASES_OF_PART[name])
                continue
            out.extend(found)
        return out

    def iter_items(self, folder: SourceFolder) -> Iterator[SourceItem]:
        return self._part(folder).iter_items(folder)

    def fetch(self, folder: SourceFolder, item: SourceItem, import_id: str) -> FetchedItem | None:
        return self._part(folder).fetch(folder, item, import_id)

    def changed_since(self, folder: SourceFolder, cursor: str | None) -> Changes:
        part = self._part(folder)
        if part is self.mail:
            return part.changed_since(folder, cursor)
        return Changes(list(part.iter_items(folder)), None, reset=True)  # DAV: a full listing, no cursor kept

    def flags_of(self, folder: SourceFolder) -> dict[str, list[str]] | None:
        part = self._part(folder)
        return part.flags_of(folder) if part is self.mail else None

    def read_rules(self) -> list[dict]:
        return self.mail.read_rules()

    def mailbox_size(self, folders: list[SourceFolder], deadline: float) -> MailboxSize | None:
        return self.mail.mailbox_size([f for f in folders if f.kind == MAIL], deadline)

    def mirror_listing(self, folder: SourceFolder) -> Listing:
        return self._part(folder).mirror_listing(folder)

    def exists(self, folder: SourceFolder, keys: list[str], refs: dict | None = None) -> dict[str, bool]:
        return self._part(folder).exists(folder, keys, refs)

    def key_epoch(self, key: str) -> str | None:
        return None if DAV_KEY_RE.match(key) else self.mail.key_epoch(key)
