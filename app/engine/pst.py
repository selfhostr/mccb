"""PstFile: folder discovery, item iteration and builders (contract of SPEC)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator

import pypff

from . import mapi
from .charset import normalize
from .contact import build_contact as _build_contact
from .event import build_event as _build_event, deterministic_uid
from .message import (BuildError, MessageBuilder, clean_subject, internaldate_of, is_mail_class,
                      message_class, message_flags)
from .source import Context, PffMessage


class PstError(Exception):
    """The PST cannot be opened / read at all."""


class ItemError(Exception):
    """One item cannot be read or converted (the run continues)."""


@dataclass
class FolderInfo:
    path: list[str]
    kind: str
    container_class: str
    special: str | None
    count: int
    _folder: object = field(repr=False, compare=False, default=None)


@dataclass
class BuiltMessage:
    raw: bytes
    internaldate: datetime
    flags: list[str]
    subject: str
    attachments: int
    size: int
    warnings: list[str]


@dataclass
class DavObject:
    uid: str
    filename: str
    content: str
    summary: str
    warnings: list[str] = field(default_factory=list)   # extension: non-blocking issues


class ItemHandle:
    """Opaque handle returned by iter_items()."""
    __slots__ = ("pst", "item", "folder", "index", "key", "error", "_src")

    def __init__(self, pst, item, folder, index, key, error=None):
        self.pst = pst
        self.item = item
        self.folder = folder
        self.index = index
        self.key = key
        self.error = error
        self._src = None

    def source(self):
        if self.error is not None:
            raise ItemError("unreadable item %s: %s" % (self.key, self.error))
        if self._src is None:
            try:
                self._src = PffMessage(self.item, self.pst._ctx)
            except Exception as ex:  # noqa: BLE001
                raise ItemError("unreadable item %s: %s" % (self.key, ex)) from ex
        return self._src


_INBOX_NAMES = {"inbox", "boîte de réception", "boite de reception", "posteingang", "bandeja de entrada",
                "posta in arrivo", "postvak in", "caixa de entrada", "indbakke", "inkorgen", "innboks",
                "saapuneet", "skrzynka odbiorcza", "doručená pošta", "beérkezett üzenetek",
                "входящие", "gelen kutusu", "εισερχόμενα", "受信トレイ", "收件箱", "받은 편지함"}
_SPECIAL_NAMES = {
    "sent": {"sent items", "éléments envoyés", "elements envoyes", "gesendete elemente", "elementos enviados",
             "posta inviata", "verzonden items", "itens enviados", "sendt post", "skickat", "sent", "sent mail"},
    "drafts": {"drafts", "brouillons", "entwürfe", "borradores", "bozze", "concepten", "rascunhos", "kladder",
               "utkast"},
    "trash": {"deleted items", "éléments supprimés", "elements supprimes", "gelöschte elemente",
              "elementos eliminados", "posta eliminata", "verwijderde items", "itens excluídos", "trash",
              "corbeille"},
    "junk": {"junk e-mail", "junk email", "courrier indésirable", "junk-e-mail", "correo no deseado",
             "posta indesiderata", "ongewenste e-mail", "lixo eletrônico", "spam", "junk"},
    "outbox": {"outbox", "boîte d'envoi", "boite d'envoi", "postausgang", "bandeja de salida",
               "posta in uscita", "postvak uit", "caixa de saída"},
}
_SYNC_ISSUES = {"sync issues", "sync issues (this computer only)", "problèmes de synchronisation",
                "synchronisierungsprobleme", "problemas de sincronización", "problemi di sincronizzazione"}
_OTHER_CONTACT_CLASSES = ("ipf.contact.recipientcache", "ipf.contact.galcontacts", "ipf.contact.moc.",
                          "ipf.contact.peoplecentricconversationbuddies", "ipf.contact.organizationalcontacts",
                          "ipf.contact.mecontact")


def folder_kind(container_class: str | None) -> str:
    c = (container_class or "").lower()
    if not c or c.startswith("ipf.note") or c == "ipf.imap" or c.startswith("ipf.post"):
        return "mail"
    if c.startswith("ipf.contact"):
        return "other" if c.startswith(_OTHER_CONTACT_CLASSES) else "contacts"
    if c.startswith("ipf.appointment"):
        return "calendar"
    if c.startswith("ipf.task"):
        return "tasks"
    if c.startswith("ipf.stickynote"):
        return "notes"
    if c.startswith("ipf.journal"):
        return "journal"
    return "other"


def check_pst_signature(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"!BDN"
    except OSError:
        return False


class PstFile:
    def __init__(self, path: str, default_codepage: str | None = None):
        if not os.path.isfile(path):
            raise PstError("file not found: %s" % path)
        if not check_pst_signature(path):
            raise PstError("not a PST/OST file (bad signature)")
        self.path = path
        self._f = pypff.file()
        try:
            self._f.open(path)
        except Exception as ex:  # noqa: BLE001
            raise PstError("cannot open PST: %s" % ex) from ex
        cp = default_codepage
        if cp is None:
            try:
                acp = self._f.ascii_codepage
                cp = acp.decode() if isinstance(acp, bytes) else acp
            except Exception:  # noqa: BLE001
                cp = None
        cp = normalize(cp) or "cp1252"
        try:
            nm = mapi.NameMap(self._f.name_to_id_map)
        except Exception:  # noqa: BLE001
            nm = mapi.NameMap()
        self._ctx = Context(nm, cp)
        self._folders: list[FolderInfo] | None = None
        self._errors: list[str] = []
        try:
            store = mapi.PropBag.from_item(self._f.message_store, nm, default_codepage=cp)
        except Exception:  # noqa: BLE001
            store = mapi.PropBag({}, nm)
        self._store = store
        self.store_key = bytes(store.raw(mapi.PR_RECORD_KEY) or b"") or os.path.basename(path).encode()

    # context manager -------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        try:
            self._f.close()
        except Exception:  # noqa: BLE001
            pass

    # folders -----------------------------------------------------------------
    def _bag(self, item):
        return mapi.PropBag.from_item(item, self._ctx.namemap, default_codepage=self._ctx.default_codepage)

    @staticmethod
    def _children(folder):
        try:
            n = folder.number_of_sub_folders
        except Exception:  # noqa: BLE001
            return []
        out = []
        for i in range(n):
            try:
                out.append(folder.get_sub_folder(i))
            except Exception:  # noqa: BLE001
                continue
        return out

    def _find_ipm_root(self):
        root = self._f.root_folder
        nid = mapi.entryid_nid(self._store.raw(mapi.PR_IPM_SUBTREE_ENTRYID))
        queue = [(root, 0)]
        by_name = None
        best = -1
        while queue:
            f, d = queue.pop(0)
            try:
                ident = f.identifier
            except Exception:  # noqa: BLE001
                ident = None
            if nid is not None and ident == nid:
                return f
            try:
                nm = (f.name or "")
            except Exception:  # noqa: BLE001
                nm = ""
            if nm in ("Top of Personal Folders", "IPM_SUBTREE", "Top of Outlook data file",
                      "Début du fichier de données Outlook"):
                # OSTs have a public and a mailbox IPM_SUBTREE: keep the richest one
                try:
                    nsub = f.number_of_sub_folders
                except Exception:  # noqa: BLE001
                    nsub = 0
                if nsub > best:
                    by_name, best = f, nsub
            if d < 3:
                queue.extend((c, d + 1) for c in self._children(f))
        return by_name or root

    def folders(self) -> list[FolderInfo]:
        if self._folders is not None:
            return self._folders
        ipm = self._find_ipm_root()
        special_nids: dict[int, str] = {}
        other_nids: set[int] = set()
        for tag, sp in ((mapi.PR_IPM_SENTMAIL_ENTRYID, "sent"), (mapi.PR_IPM_WASTEBASKET_ENTRYID, "trash"),
                        (mapi.PR_IPM_OUTBOX_ENTRYID, "outbox")):
            n = mapi.entryid_nid(self._store.raw(tag))
            if n:
                special_nids[n] = sp
        bags = []
        for f in (self._f.root_folder, ipm):
            try:
                bags.append(self._bag(f))
            except Exception:  # noqa: BLE001
                pass
        top = self._children(ipm)
        for f in top:  # Exchange/OST put the special ids on the Inbox too
            try:
                b = self._bag(f)
                if b.has(mapi.PR_IPM_DRAFTS_ENTRYID) or b.has(mapi.PR_ADDITIONAL_REN_ENTRYIDS):
                    bags.append(b)
                    if b.has(mapi.PR_IPM_APPOINTMENT_ENTRYID):
                        special_nids.setdefault(f.identifier, "inbox")
            except Exception:  # noqa: BLE001
                pass
        for b in bags:
            n = mapi.entryid_nid(b.raw(mapi.PR_IPM_DRAFTS_ENTRYID))
            if n:
                special_nids.setdefault(n, "drafts")
            ren = b.binaries(mapi.PR_ADDITIONAL_REN_ENTRYIDS)
            for i, eid in enumerate(ren):
                n = mapi.entryid_nid(eid)
                if not n:
                    continue
                if i == 4:
                    special_nids.setdefault(n, "junk")
                elif i < 4:
                    other_nids.add(n)  # conflicts / sync issues / local & server failures
        out: list[FolderInfo] = []
        used = set(special_nids.values())

        def walk(folder, path, depth):
            for child in self._children(folder):
                try:
                    b = self._bag(child)
                    name = b.string(mapi.PR_DISPLAY_NAME) or child.name or "(sans nom)"
                    cc = b.string(mapi.PR_CONTAINER_CLASS) or ""
                    nid = child.identifier
                except Exception as ex:  # noqa: BLE001
                    self._errors.append("folder unreadable under %s: %s" % ("/".join(path) or "/", ex))
                    continue
                name = name.replace("\x00", "").strip() or "(sans nom)"
                kind = folder_kind(cc)
                if kind == "mail" and cc.lower() in ("", "ipf.note"):
                    kind = self._sniff_kind(child) or kind
                special = special_nids.get(nid)
                if nid in other_nids or (depth == 0 and name.lower() in _SYNC_ISSUES):
                    kind = "other"
                if special is None and depth == 0 and kind == "mail":
                    low = name.lower()
                    if low in _INBOX_NAMES and "inbox" not in used:
                        special = "inbox"
                    else:
                        for sp, names in _SPECIAL_NAMES.items():
                            if low in names and sp not in used:
                                special = sp
                                break
                    if special:
                        used.add(special)
                if special and kind not in ("mail",):
                    special = None
                try:
                    count = child.number_of_sub_messages
                except Exception as ex:  # noqa: BLE001
                    self._errors.append("folder %s: cannot count items: %s" % ("/".join(path + [name]), ex))
                    count = 0
                out.append(FolderInfo(path=path + [name], kind=kind, container_class=cc,
                                      special=special, count=count, _folder=child))
                if depth < 64:
                    walk(child, path + [name], depth + 1)

        walk(ipm, [], 0)
        self._folders = out
        return out

    def _sniff_kind(self, folder):
        """Folders created by third-party tools sometimes lack a proper
        PR_CONTAINER_CLASS: look at the first items' message class."""
        from .contact import is_contact_class
        from .event import is_event_class
        try:
            n = min(folder.number_of_sub_messages, 3)
            classes = [message_class(self._bag(folder.get_sub_message(i))) for i in range(n)]
        except Exception:  # noqa: BLE001
            return None
        if classes and all(is_contact_class(c) for c in classes):
            return "contacts"
        if classes and all(is_event_class(c) for c in classes):
            return "calendar"
        return None

    # items -----------------------------------------------------------------
    def iter_items(self, folder: FolderInfo, start: int = 0) -> Iterator[tuple[int, str, object]]:
        try:
            fnid = folder._folder.identifier
        except Exception:  # noqa: BLE001
            fnid = 0
        for i in range(max(0, start), folder.count):
            try:
                item = folder._folder.get_sub_message(i)
                key = "%d-%d" % (fnid, item.identifier)
                yield i, key, ItemHandle(self, item, folder, i, key)
            except Exception as ex:  # noqa: BLE001
                key = "%d-x%d" % (fnid, i)
                yield i, key, ItemHandle(self, None, folder, i, key, error=ex)

    def _wrap(self, handle):
        if isinstance(handle, ItemHandle):
            return handle
        return ItemHandle(self, handle, None, -1, str(getattr(handle, "identifier", "?")))

    def build_message(self, handle, import_id: str) -> BuiltMessage | None:
        h = self._wrap(handle)
        src = h.source()
        try:
            if not is_mail_class(message_class(src.bag)):
                return None
            built = MessageBuilder(import_id).build_raw(src)
            bm = BuiltMessage(raw=built.raw, internaldate=internaldate_of(src.bag),
                              flags=message_flags(src.bag), subject=built.parts.subject,
                              attachments=len(built.parts.atts), size=len(built.raw),
                              warnings=list(built.warnings))
            bm._expect = built.expect
            return bm
        except ItemError:
            raise
        except Exception as ex:  # noqa: BLE001
            raise ItemError("message %s: %s" % (h.key, ex)) from ex

    def _expectations(self, handle):
        from .message import expectations
        src = self._wrap(handle).source()
        parts = MessageBuilder("verify").gather(src, 0)
        return expectations(parts)

    def _uid(self, src, kind):
        return deterministic_uid(self.store_key, src.identifier, kind)

    def build_contact(self, handle) -> DavObject | None:
        h = self._wrap(handle)
        src = h.source()
        try:
            uid = self._uid(src, "vcf")
            r = _build_contact(src, uid)
            if r is None:
                return None
            summary, text, warnings = r
            return DavObject(uid=uid, filename=uid + ".vcf", content=text, summary=summary, warnings=warnings)
        except Exception as ex:  # noqa: BLE001
            raise ItemError("contact %s: %s" % (h.key, ex)) from ex

    def build_event(self, handle) -> DavObject | None:
        h = self._wrap(handle)
        src = h.source()
        try:
            uid = self._uid(src, "ics")
            r = _build_event(src, uid)
            if r is None:
                return None
            summary, text, warnings = r
            return DavObject(uid=uid, filename=uid + ".ics", content=text, summary=summary, warnings=warnings)
        except (BuildError, Exception) as ex:  # noqa: BLE001
            raise ItemError("event %s: %s" % (h.key, ex)) from ex

    def scan(self) -> dict:
        folders = self.folders()
        totals = {"mail": 0, "contacts": 0, "calendar": 0}
        out = []
        for f in folders:
            out.append({"path": f.path, "kind": f.kind, "count": f.count, "special": f.special,
                        "container_class": f.container_class})
            if f.kind in totals:
                totals[f.kind] += f.count
        return {"folders": out, "totals": totals, "errors": list(self._errors)}


__all__ = ["PstFile", "FolderInfo", "BuiltMessage", "DavObject", "PstError", "ItemError",
           "check_pst_signature", "clean_subject"]
