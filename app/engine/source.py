"""Source abstraction: a message/attachment/recipient seen as property bags.

``PffMessage`` wraps a pypff item; tests can provide synthetic sources with the
same interface (``bag``, ``recipients()``, ``attachments()``, ``identifier``).
"""
from __future__ import annotations

from . import mapi
from .mapi import PropBag

READ_CHUNK = 1 << 20


class Context:
    def __init__(self, namemap=None, default_codepage="cp1252"):
        self.namemap = namemap or mapi.NameMap()
        self.default_codepage = default_codepage


class PffAttachment:
    def __init__(self, att, ctx: Context, codepage):
        self._att = att
        self._ctx = ctx
        self.bag = PropBag.from_item(att, ctx.namemap, codepage=codepage,
                                     default_codepage=ctx.default_codepage)
        self._codepage = codepage

    @property
    def size(self) -> int:
        try:
            return int(self._att.size)
        except Exception:
            d = self.bag.raw(mapi.PR_ATTACH_DATA)
            return len(d) if d else 0

    def read_data(self) -> bytes:
        """Attachment bytes, read by blocks from libpff."""
        try:
            size = int(self._att.size)
        except Exception:
            size = None
        if size is not None:
            buf = bytearray()
            try:
                self._att.seek_offset(0)
                while len(buf) < size:
                    chunk = self._att.read_buffer(min(READ_CHUNK, size - len(buf)))
                    if not chunk:
                        break
                    buf.extend(chunk)
            except Exception:
                buf = None
            if buf is not None and len(buf) == size:
                return bytes(buf)
        d = self.bag.raw(mapi.PR_ATTACH_DATA)
        if d is None:
            raise IOError("attachment data unreadable")
        return bytes(d)

    def embedded(self):
        from .pffext import embedded_message
        item = embedded_message(self._att)
        return PffMessage(item, self._ctx)


class PffMessage:
    def __init__(self, item, ctx: Context):
        self._item = item
        self._ctx = ctx
        self.bag = PropBag.from_item(item, ctx.namemap, default_codepage=ctx.default_codepage)
        try:
            self.identifier = int(item.identifier)
        except Exception:
            self.identifier = 0

    def recipients(self) -> list[PropBag]:
        r = getattr(self._item, "recipients", None)
        if r is None:
            return []
        out = []
        try:
            n = r.number_of_record_sets
        except Exception:
            return []
        for i in range(n):
            try:
                out.append(PropBag.from_record_set(r.get_record_set(i), self._ctx.namemap,
                                                   codepage=self.bag.codepage,
                                                   default_codepage=self._ctx.default_codepage))
            except Exception:
                continue
        return out

    def attachment_count(self) -> int:
        try:
            return int(getattr(self._item, "number_of_attachments", 0) or 0)
        except Exception:
            return 0

    def attachments(self):
        """Yields PffAttachment or an Exception instance for unreadable ones."""
        for i in range(self.attachment_count()):
            try:
                yield PffAttachment(self._item.get_attachment(i), self._ctx, self.bag.codepage)
            except Exception as ex:  # noqa: BLE001 - reported per attachment
                yield ex
