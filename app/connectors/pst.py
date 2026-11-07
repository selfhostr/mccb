"""An uploaded PST as a source. Accounts going from a PST to the server's IMAP and DAV keep the historical path
of JobRunner (builder processes); this wrapper serves the routes from a PST to any other target."""
from __future__ import annotations

from typing import Iterator

from . import registry
from .base import (CALENDAR, CAP_FLAGS, CAP_FOLDERS, CAP_INTERNAL_DATE, CONTACTS, MAIL, ConnectorError, FetchedItem,
                   Source, SourceFolder, SourceItem)

_DATE_KIND = {MAIL: "mail", CALENDAR: "event", CONTACTS: "contact"}


class PstSource(Source):
    """`ctx["guard"]`, when given, is told about each libpff read (before_read, clear) and answers which indexes
    must not be read again (skipped): a crash of the native library kills the job process, see crash.py."""
    native_code = True

    def __init__(self, config: dict, secrets: dict, ctx: dict | None = None):
        self.pst_id = config.get("pst_id")
        self.guard = (ctx or {}).get("guard")
        self._file = None
        self.pst = None
        self.eng = None

    def open(self) -> None:
        from ..server import db
        from ..server.engine_loader import engine
        row = db.q1("SELECT * FROM psts WHERE id=?", (self.pst_id,)) if self.pst_id else None
        if row is None:
            raise ConnectorError("no PST linked to the mailbox", ConnectorError.CONFIG)
        if row["status"] != "valid":
            raise ConnectorError(f"PST not valid (status {row['status']})", ConnectorError.CONFIG)
        self.eng = engine()
        try:
            self._file = self.eng.PstFile(row["path"])
            self.pst = self._file.__enter__()
        except Exception as e:  # noqa: BLE001 - engine errors are not typed
            self._file = None
            raise ConnectorError(f"cannot open the PST: {e}", ConnectorError.CONFIG) from e

    def close(self) -> None:
        f, self._file, self.pst = self._file, None, None
        if f is not None:
            try:
                f.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    def folders(self) -> list[SourceFolder]:
        return [SourceFolder(path=list(f.path), kind=f.kind, special=f.special, count=int(f.count or 0), ref=f)
                for f in self.pst.folders()]

    def _date(self, handle, kind: str):
        date_of = getattr(self.pst, "item_date", None)
        if date_of is None or kind not in _DATE_KIND:
            return None
        try:
            return date_of(handle, _DATE_KIND[kind])
        except Exception:  # noqa: BLE001 - no date: imported last
            return None

    def iter_items(self, folder: SourceFolder) -> Iterator[SourceItem]:
        g = self.guard
        count = int(folder.count or 0)
        start = 0
        while start < count:
            if g and g.skipped(folder, start):
                start += 1
                continue
            if g:
                g.before_read(folder, start)
            nxt = None
            gen = self.pst.iter_items(folder.ref, start)
            try:
                for idx, key, handle in gen:
                    yield SourceItem(key=key, date=self._date(handle, folder.kind), index=idx, ref=idx)
                    if g and g.skipped(folder, idx + 1):
                        nxt = idx + 1
                        break
                    if g and idx + 1 < count:
                        g.before_read(folder, idx + 1)
            finally:
                gen.close()
            if nxt is None:
                break
            start = nxt
        if g:
            g.clear()

    def fetch(self, folder: SourceFolder, item: SourceItem, import_id: str) -> FetchedItem | None:
        from ..server import builders
        gen = self.pst.iter_items(folder.ref, item.ref)
        try:
            got = next(gen, None)
        finally:
            gen.close()
        if got is None or got[0] != item.ref or got[1] != item.key:
            raise ConnectorError(f"item key mismatch ({got[1] if got else None} != {item.key})",
                                 ConnectorError.REJECTED)
        handle = got[2]
        try:
            if folder.kind == MAIL:
                built = self.pst.build_message(handle, import_id)
            elif folder.kind == CALENDAR:
                built = self.pst.build_event(handle)
            else:
                built = self.pst.build_contact(handle)
        except Exception as e:  # noqa: BLE001 - the error is reported per item
            raise ConnectorError(f"construction: {type(e).__name__}: {e}", ConnectorError.REJECTED) from e
        if built is None:
            return None
        if folder.kind != MAIL:
            return FetchedItem(raw=built.content.encode("utf-8"), subject=built.summary, name=built.filename)
        res = builders.finish_built(self.eng, built, handle)
        if res["status"] != "ok":
            raise ConnectorError("; ".join(res.get("errs") or [res.get("error") or "rejected"])[:2000],
                                 ConnectorError.REJECTED)
        return FetchedItem(raw=res["raw"], internal_date=res["internaldate"], flags=res["flags"],
                           subject=res["subject"], warnings=res["warnings"])


registry.register(registry.ConnectorSpec(
    kind="pst", label="Outlook PST file", description="A PST file uploaded to this server.",
    source=registry.Role(PstSource, (MAIL, CALENDAR, CONTACTS),
                         frozenset({CAP_FOLDERS, CAP_FLAGS, CAP_INTERNAL_DATE}),
                         (registry.Field("pst_id", "PST file", "number", required=True,
                                         help="Identifier of an uploaded PST (GET /api/psts)."),),
                         lambda config: str(config.get("pst_id", ""))),
))
