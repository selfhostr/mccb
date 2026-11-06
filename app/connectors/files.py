"""Files as a source: mbox, Maildir (zipped), loose .eml, Outlook .msg, .ics and .vcf, and .zip archives of those.

They arrive through the upload and the drop folder like a PST (a `psts` row, analysed by `uploads.validate`), then an
account reads them with this connector. A .zip is never extracted to disk, except an mbox inside it, which needs
random access. Keys come from the path inside the file and the position in it, so the same file gives the same keys.
See docs/protocols.md."""
from __future__ import annotations

import email.utils
import hashlib
import os
import posixpath
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from typing import Callable, Iterator

from . import registry
from .base import (CALENDAR, CAP_FLAGS, CAP_FOLDERS, CAP_INTERNAL_DATE, CONTACTS, MAIL, ConnectorError, FetchedItem,
                   Source, SourceFolder, SourceItem, subject_of)
from .convert import unfold

FILES_KIND = "files"
PST_SIGNATURE = b"!BDN"
OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
MAX_ENTRY = 512 * 2**20         # one file inside an archive, uncompressed
MAX_TEXT = 256 * 2**20          # one .ics or .vcf, read whole
MAX_RATIO = 250                 # uncompressed / compressed above this, for an entry over 64 MB, is refused (zip bomb)
MBOX_FROM = re.compile(rb"^From \S+\s+\w{3}\s+\w{3}\s+\d{1,2}\s+\d{1,2}:\d{2}")
EXT_FORMAT = {".mbox": "mbox", ".mbx": "mbox", ".eml": "eml", ".msg": "msg", ".ics": "ics", ".ical": "ics",
              ".vcf": "vcf", ".vcard": "vcf", ".zip": "zip"}
SPECIAL_BY_NAME = {"inbox": "inbox", "sent": "sent", "sent items": "sent", "sent mail": "sent", "sent messages": "sent",
                   "drafts": "drafts", "draft": "drafts", "trash": "trash", "deleted items": "trash",
                   "deleted": "trash", "junk": "junk", "spam": "junk", "junk e-mail": "junk", "junk email": "junk"}


def _h(text: str, n: int = 20) -> str:
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:n]  # noqa: S324 - identifier, not security


@dataclass
class Entry:
    """A file of the container: its path in the archive (or the original file name) and how to read it."""
    path: str
    size: int
    read: Callable[[], bytes]
    date: datetime | None = None
    open_file: Callable[[], object] | None = None   # a file-like object for large sequential reads (mbox)


# ------------------------------------------------------------------ containers
def _name_ok(name: str) -> bool:
    parts = name.split("/")
    return not (name.endswith("/") or "__MACOSX" in parts or parts[-1].startswith(".") or parts[-1] == "Thumbs.db")


def _maildir_dir(part: str) -> bool:
    return part.startswith(".") and len(part) > 1


def entries_of(path: str, filename: str) -> list[Entry]:
    """The files in an upload: the file itself, or the entries of a zip. Raises ValueError for a bad archive."""
    if filename.lower().endswith(".zip") or _is_zip(path):
        try:
            zf = zipfile.ZipFile(path)
        except zipfile.BadZipFile as e:
            raise ValueError(f"not a valid zip file: {e}") from e
        out = []
        for zi in zf.infolist():
            name = zi.filename.replace("\\", "/")
            if zi.is_dir() or not _name_ok(name) or zi.flag_bits & 0x1:  # encrypted entries are not readable
                continue
            if zi.file_size > MAX_ENTRY or (zi.file_size > 64 * 2**20 and zi.file_size > MAX_RATIO * max(zi.compress_size, 1)):
                continue
            try:
                d = datetime(*zi.date_time, tzinfo=timezone.utc)
            except ValueError:
                d = None
            out.append(Entry(name, zi.file_size, (lambda zi=zi: zf.read(zi)), d, (lambda zi=zi: zf.open(zi))))
        return out
    size = os.path.getsize(path)

    def read() -> bytes:
        with open(path, "rb") as f:
            return f.read(MAX_TEXT if size > MAX_TEXT else size)
    return [Entry(os.path.basename(filename.replace("\\", "/")), size, read, None, lambda: open(path, "rb"))]


def _is_zip(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) in (b"PK\x03\x04", b"PK\x05\x06")
    except OSError:
        return False


# ------------------------------------------------------------------ classification
def classify(name: str, head: bytes) -> str | None:
    """Format of one file: mbox, eml, msg, ics, vcf, or None. The extension decides, the content decides when the
    extension says nothing (Thunderbird mbox files have none)."""
    base = posixpath.basename(name).lower()
    ext = posixpath.splitext(base)[1]
    if ext in EXT_FORMAT and ext != ".zip":
        return EXT_FORMAT[ext]
    if head.startswith(OLE_MAGIC) and ext == ".msg":
        return "msg"
    stripped = head.lstrip(b"\xef\xbb\xbf \r\n\t")
    if stripped[:15].upper() == b"BEGIN:VCALENDAR":
        return "ics"
    if stripped[:11].upper() == b"BEGIN:VCARD":
        return "vcf"
    if MBOX_FROM.match(head):
        return "mbox"
    return None


def is_maildir_file(parts: list[str]) -> bool:
    return len(parts) >= 2 and parts[-2] in ("cur", "new")


def special_of(path: list[str]) -> str | None:
    return SPECIAL_BY_NAME.get(path[-1].lower()) if path else None


# ------------------------------------------------------------------ mbox
def scan_mbox(f) -> list[dict]:
    """Messages of an mbox file object: {start, end, flags, date, mid, size}. Offsets delimit the message without its
    From_ line and without the blank line that ends it."""
    out: list[dict] = []
    off, prev_blank, cur, hdr = 0, True, None, None
    for line in f:
        n = len(line)
        if prev_blank and line.startswith(b"From ") and MBOX_FROM.match(line):
            if cur is not None:
                cur["end"] = max(cur["start"], off - (cur["_blank"]))
                out.append(cur)
            cur, hdr = {"start": off + n, "_blank": 0}, []
        elif cur is not None and hdr is not None:
            if line.strip() == b"":
                _finish_headers(cur, b"".join(hdr))
                hdr = None
            else:
                hdr.append(line)
        prev_blank = line.strip() == b""
        if cur is not None:
            cur["_blank"] = n if prev_blank else 0
        off += n
    if cur is not None:
        if hdr:
            _finish_headers(cur, b"".join(hdr))
        cur["end"] = max(cur["start"], off - cur["_blank"])
        out.append(cur)
    for m in out:
        m.pop("_blank", None)
        m["size"] = m["end"] - m["start"]
    return out


def _finish_headers(rec: dict, raw: bytes) -> None:
    h = BytesHeaderParser(policy=policy.compat32).parsebytes(raw)
    rec["mid"] = " ".join(str(h.get("Message-ID") or "").split()) or None
    rec["date"] = parse_date(str(h.get("Date") or ""))
    rec["flags"] = mbox_flags(str(h.get("Status") or ""), str(h.get("X-Status") or ""),
                              str(h.get("X-Mozilla-Status") or ""))


def parse_date(text: str) -> datetime | None:
    if not text.strip():
        return None
    try:
        d = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def mbox_flags(status: str, x_status: str, mozilla: str) -> list[str] | None:
    """IMAP flags from Status / X-Status (Unix mail) and X-Mozilla-Status (Thunderbird). None: deleted."""
    flags: list[str] = []
    if "D" in x_status.upper():
        return None
    if "R" in status.upper():
        flags.append("\\Seen")
    if "A" in x_status.upper():
        flags.append("\\Answered")
    if "F" in x_status.upper():
        flags.append("\\Flagged")
    if "T" in x_status.upper():
        flags.append("\\Draft")
    if mozilla.strip():
        try:
            bits = int(mozilla.strip(), 16)
        except ValueError:
            bits = 0
        if bits & 0x0008:
            return None
        for bit, flag in ((0x0001, "\\Seen"), (0x0002, "\\Answered"), (0x0004, "\\Flagged"), (0x1000, "$Forwarded")):
            if bits & bit and flag not in flags:
                flags.append(flag)
    return flags


def unquote_mboxrd(raw: bytes) -> bytes:
    """Undo the From_ quoting of mboxrd (and mboxo): ">From " becomes "From ", ">>From " becomes ">From "."""
    return re.sub(rb"(?m)^>(>*From )", rb"\1", raw)


# ------------------------------------------------------------------ Maildir
def maildir_flags(filename: str) -> list[str] | None:
    """Flags of a Maildir file name (`...:2,FLAGS`). None: trashed."""
    _b, _s, info = filename.rpartition(":2,")
    if not _s:
        return []
    if "T" in info:
        return None
    return [f for letter, f in (("D", "\\Draft"), ("F", "\\Flagged"), ("P", "$Forwarded"), ("R", "\\Answered"),
                                ("S", "\\Seen")) if letter in info]


def maildir_folder(parts: list[str], root: str) -> list[str]:
    """Folder of a Maildir file given its directory parts and the common root of all the Maildirs of the archive.
    Maildir++ sub-folders are directories named `.Parent.Child`."""
    rel = parts[len(root.split("/")) if root else 0:]
    out: list[str] = []
    for p in rel:
        out += [x for x in p[1:].split(".") if x] if _maildir_dir(p) else [p]
    return out or ["INBOX"]


# ------------------------------------------------------------------ calendar and contact files
def _uid_of(block: list[str]) -> str:
    for x in unfold("\n".join(block)):
        if x.upper().startswith(("UID:", "UID;")) and ":" in x:
            return x.split(":", 1)[1].strip()
    return ""


def split_ics(text: str) -> tuple[str, list[tuple[str, str]]]:
    """(calendar name, [(uid, VCALENDAR text)]): every event (or series with its exceptions) of the file becomes one
    resource that keeps the time zones. Tasks and journals are left out."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    head: list[str] = []
    tz: list[str] = []
    events: dict[str, list[str]] = {}
    name, i, n, idx = "", 0, len(lines), 0
    while i < n:
        ln = lines[i]
        up = ln.upper()
        if up in ("BEGIN:VEVENT", "BEGIN:VTIMEZONE", "BEGIN:VTODO", "BEGIN:VJOURNAL", "BEGIN:VFREEBUSY"):
            end = "END:" + up[6:]
            j = i
            while j < n and lines[j].upper() != end:
                j += 1
            block = lines[i:j + 1]
            if up == "BEGIN:VTIMEZONE":
                tz += block
            elif up == "BEGIN:VEVENT":
                key = _uid_of(block)
                if not key:
                    idx += 1
                    key = f"#{idx}"
                events.setdefault(key, []).extend(block)
            i = j + 1
            continue
        if up.startswith("X-WR-CALNAME:"):
            name = ln.split(":", 1)[1].strip()
        if ln and not up.startswith(("BEGIN:", "END:")):
            head.append(ln)
        i += 1
    prefix = ["BEGIN:VCALENDAR"] + head + tz
    return name, [(key, "\r\n".join(prefix + body + ["END:VCALENDAR"]) + "\r\n") for key, body in events.items()]


def split_vcf(text: str) -> list[tuple[str, str]]:
    """[(uid or '#n', vCard text)] of the cards of a .vcf file."""
    cards, cur, idx = [], None, 0
    for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        up = ln.upper()
        if up == "BEGIN:VCARD":
            cur = [ln]
        elif up == "END:VCARD" and cur is not None:
            cur.append(ln)
            idx += 1
            cards.append((_uid_of(cur) or f"#{idx}", "\r\n".join(cur) + "\r\n"))
            cur = None
        elif cur is not None:
            cur.append(ln)
    return cards


# ------------------------------------------------------------------ Outlook .msg
def _filetime(b: bytes) -> datetime | None:
    t = int.from_bytes(b[:8], "little")
    if not t:
        return None
    try:
        return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=t // 10)
    except OverflowError:
        return None


def msg_to_eml(data: bytes) -> tuple[bytes, list[str]]:
    """RFC 5322 message from the bytes of an Outlook .msg (MS-OXMSG). Best effort: headers, text and HTML body,
    file attachments. Embedded messages, OLE objects and RTF-only bodies are left out and reported as warnings."""
    try:
        import olefile
    except ImportError as e:  # pragma: no cover - olefile is in requirements.txt
        raise ValueError("reading .msg needs the olefile package") from e
    if not data.startswith(OLE_MAGIC):
        raise ValueError("not an Outlook .msg file")
    ole = olefile.OleFileIO(data)
    warns: list[str] = []
    try:
        def cp_of(prefix: str) -> str:
            props = _props(ole, prefix, 32 if prefix == "" else 8)
            cp = props.get(0x3FDE) or props.get(0x3FFD)
            return {65001: "utf-8", 1252: "cp1252", 28591: "latin-1", 1251: "cp1251", 1250: "cp1250", 932: "cp932",
                    936: "gbk", 949: "cp949", 950: "big5"}.get(int(cp), "cp1252") if cp else "cp1252"

        top_cp = cp_of("")

        def text(prefix: str, pid: int, cp: str = top_cp) -> str | None:
            for typ, enc in ((0x001F, "utf-16-le"), (0x001E, cp)):
                name = f"{prefix}__substg1.0_{pid:04X}{typ:04X}"
                if ole.exists(name):
                    return ole.openstream(name).read().decode(enc, "replace").rstrip("\x00")
            return None

        def blob(prefix: str, pid: int) -> bytes | None:
            name = f"{prefix}__substg1.0_{pid:04X}0102"
            return ole.openstream(name).read() if ole.exists(name) else None

        msg = EmailMessage(policy=policy.default)
        transport = text("", 0x007D)
        got: dict[str, str] = {}
        if transport:
            h = BytesHeaderParser(policy=policy.default).parsebytes(transport.encode("utf-8", "replace"))
            for k in ("From", "To", "Cc", "Reply-To", "Date", "Message-ID", "In-Reply-To", "References"):
                if h.get(k):
                    got[k] = " ".join(str(h[k]).split())
        subject = text("", 0x0037) or ""
        if "From" not in got:
            em = text("", 0x0C1F) or text("", 0x5D01) or text("", 0x0065) or ""
            nm = text("", 0x0C1A) or ""
            if em:
                got["From"] = email.utils.formataddr((nm, em)) if nm else em
        recips: dict[str, list[str]] = {"To": [], "Cc": []}
        for r in sorted({x[0] for x in ole.listdir(streams=False, storages=True) if x[0].startswith("__recip_version1.0_")}):
            rp = r + "/"
            props = _props(ole, rp, 8)
            addr = text(rp, 0x39FE) or text(rp, 0x3003) or ""
            nm = text(rp, 0x3001) or ""
            kind = {1: "To", 2: "Cc"}.get(int(props.get(0x0C15, 1)))
            if addr and kind:
                recips[kind].append(email.utils.formataddr((nm, addr)) if nm and nm != addr else addr)
        for k, vals in recips.items():
            if k not in got and vals:
                got[k] = ", ".join(vals)
        if "To" not in got and text("", 0x0E04):
            got["To"] = text("", 0x0E04)  # type: ignore[assignment]
        if "Date" not in got:
            props = _props(ole, "", 32)
            raw = props.get(0x0E06) or props.get(0x0039)
            d = _filetime(raw) if isinstance(raw, bytes) else None
            if d:
                got["Date"] = email.utils.format_datetime(d)
        mid = text("", 0x1035)
        if "Message-ID" not in got and mid:
            got["Message-ID"] = mid
        for k, v in got.items():
            msg[k] = v
        msg["Subject"] = subject
        body, html = text("", 0x1000), None
        hb = blob("", 0x1013)
        if hb:
            html = hb.decode(top_cp if top_cp != "cp1252" else "utf-8", "replace")
        elif text("", 0x1013):
            html = text("", 0x1013)
        if body is None and html is None:
            if ole.exists("__substg1.0_10090102"):
                warns.append("the body is RTF only: not converted")
            body = ""
        if body is not None and html is not None:
            msg.set_content(body)
            msg.add_alternative(html, subtype="html")
        elif html is not None:
            msg.set_content(html, subtype="html")
        else:
            msg.set_content(body or "")
        for a in sorted({x[0] for x in ole.listdir(streams=False, storages=True) if x[0].startswith("__attach_version1.0_")}):
            ap = a + "/"
            payload = blob(ap, 0x3701)
            if payload is None:
                warns.append("an embedded message or object attachment was left out")
                continue
            fname = text(ap, 0x3707) or text(ap, 0x3704) or "attachment"
            mime = text(ap, 0x370E) or "application/octet-stream"
            mt, _s, st = mime.partition("/")
            msg.add_attachment(payload, maintype=mt or "application", subtype=st or "octet-stream", filename=fname)
            cid = text(ap, 0x3712)
            if cid:
                msg.get_payload()[-1]["Content-ID"] = f"<{cid.strip('<>')}>"
        return msg.as_bytes(), warns
    finally:
        ole.close()


def _props(ole, prefix: str, header: int) -> dict[int, object]:
    """Fixed-size properties of a __properties_version1.0 stream: {property id: int (small types) or 8 raw bytes}."""
    name = f"{prefix}__properties_version1.0"
    if not ole.exists(name):
        return {}
    data = ole.openstream(name).read()
    out: dict[int, object] = {}
    for i in range(header, len(data) - 15, 16):
        tag = int.from_bytes(data[i:i + 4], "little")
        typ, pid = tag & 0xFFFF, tag >> 16
        val = data[i + 8:i + 16]
        if typ in (0x0002, 0x0003, 0x000B):
            out[pid] = int.from_bytes(val[:4], "little")
        elif typ == 0x0040:
            out[pid] = val
    return out


# ------------------------------------------------------------------ source
@dataclass
class _Listing:
    folders: list[SourceFolder] = field(default_factory=list)
    items: dict[str, list[SourceItem]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


def _folder_key(path: list[str]) -> str:
    return "\x1f".join(path)  # only keys this module's own listing


class _Builder:
    """Reads a container once and lists its folders and items."""

    def __init__(self, path: str, filename: str, workdir: Callable[[], str]):
        self.path, self.filename = path, filename
        self.workdir = workdir
        self.entries = entries_of(path, filename)
        self.is_archive = len(self.entries) != 1 or filename.lower().endswith(".zip") or _is_zip(path)
        self.out = _Listing()
        self.mbox_files: dict[str, str] = {}   # entry path -> readable path on disk
        self.formats: set[str] = set()
        self._index: dict[tuple, SourceFolder] = {}

    def folder(self, path: list[str], kind: str, special: str | None = None) -> SourceFolder:
        k = (kind, tuple(path))
        f = self._index.get(k)
        if f is None:
            f = SourceFolder(path=list(path), kind=kind, special=special or special_of(path) if kind == MAIL else None,
                             count=0, ref=(kind, _folder_key(path)))
            self._index[k] = f
            self.out.folders.append(f)
            self.out.items[f.ref[1] + "\0" + kind] = []
        return f

    def add(self, f: SourceFolder, item: SourceItem) -> None:
        self.out.items[f.ref[1] + "\0" + f.kind].append(item)
        f.count = (f.count or 0) + 1

    def build(self) -> _Listing:
        maildirs = sorted({posixpath.dirname(posixpath.dirname(e.path)) for e in self.entries
                           if is_maildir_file(e.path.split("/"))})
        root = posixpath.commonpath(maildirs) if len(maildirs) > 1 and "" not in maildirs else \
            (maildirs[0] if len(maildirs) == 1 else "")
        for e in self.entries:
            parts = e.path.split("/")
            if is_maildir_file(parts):
                self.formats.add("maildir")
                self._maildir(e, parts, root)
                continue
            head = self._head(e)
            fmt = classify(e.path, head)
            if fmt:
                self.formats.add(fmt)
            dirs = parts[:-1]
            stem = posixpath.splitext(parts[-1])[0]
            if fmt == "eml":
                self._eml(e, self.folder(dirs or ["INBOX"], MAIL), None)
            elif fmt == "msg":
                self._eml(e, self.folder(dirs or ["INBOX"], MAIL), None, msg=True)
            elif fmt == "mbox":
                solo = not self.is_archive
                self._mbox(e, self.folder(["INBOX"] if solo else (dirs + [stem]), MAIL))
            elif fmt == "ics":
                self._ics(e, dirs, stem)
            elif fmt == "vcf":
                self._vcf(e, dirs, stem)
            else:
                self.out.skipped.append(e.path)
        return self.out

    @staticmethod
    def _head(e: Entry) -> bytes:
        f = e.open_file() if e.open_file else None
        if f is None:
            return e.read()[:4096]
        try:
            return f.read(4096)
        finally:
            f.close()

    # -- mail
    def _maildir(self, e: Entry, parts: list[str], root: str) -> None:
        flags = maildir_flags(parts[-1])
        if flags is None:
            return
        folder = self.folder(maildir_folder(parts[:-2], root), MAIL)
        self._eml(e, folder, flags)

    def _eml(self, e: Entry, folder: SourceFolder, flags: list[str] | None, msg: bool = False) -> None:
        mid, date = None, e.date
        if not msg:
            head = self._head(e)[:65536]
            h = BytesHeaderParser(policy=policy.compat32).parsebytes(head)
            mid = " ".join(str(h.get("Message-ID") or "").split()) or None
            date = parse_date(str(h.get("Date") or "")) or e.date
        alt = ("m:" + _h(mid)) if mid else None
        self.add(folder, SourceItem(key="f" + _h(e.path, 28), alt_key=alt, date=date, size=e.size, flags=flags or [],
                                    ref=("msg" if msg else "eml", e.path)))

    def _mbox(self, e: Entry, folder: SourceFolder) -> None:
        with open(self._on_disk(e), "rb") as f:
            msgs = scan_mbox(f)
        for i, m in enumerate(msgs):
            if m["flags"] is None:
                continue
            alt = ("m:" + _h(m["mid"])) if m.get("mid") else "d:" + _h(f"{m.get('date')}|{m['size']}")
            self.add(folder, SourceItem(key=f"b{_h(e.path, 12)}-{i}", alt_key=alt, date=m.get("date"), size=m["size"],
                                        flags=m["flags"], ref=("mbox", e.path, m["start"], m["end"])))

    def _on_disk(self, e: Entry) -> str:
        """Path of the mbox on disk: the upload itself, or a copy extracted from the archive."""
        if not self.is_archive:
            return self.path
        if e.path not in self.mbox_files or not os.path.exists(self.mbox_files[e.path]):
            dest = os.path.join(self.workdir(), _h(e.path, 16) + ".mbox")
            src = e.open_file()
            try:
                with open(dest, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
            finally:
                src.close()
            self.mbox_files[e.path] = dest
        return self.mbox_files[e.path]

    # -- calendar and contacts
    def _text(self, e: Entry) -> str | None:
        if e.size > MAX_TEXT:
            self.out.skipped.append(e.path)
            return None
        return e.read().decode("utf-8-sig", "replace")

    def _ics(self, e: Entry, dirs: list[str], stem: str) -> None:
        text = self._text(e)
        if text is None:
            return
        name, events = split_ics(text)
        if not events:
            return
        folder = self.folder(dirs + [name or stem], CALENDAR)
        for uid, body in events:
            self.add(folder, SourceItem(key=f"i{_h(e.path, 12)}-{_h(uid, 20)}", alt_key="u:" + _h(uid), size=len(body),
                                        ref=("raw", body.encode("utf-8"))))

    def _vcf(self, e: Entry, dirs: list[str], stem: str) -> None:
        text = self._text(e)
        if text is None:
            return
        cards = split_vcf(text)
        if not cards:
            return
        folder = self.folder(dirs + [stem], CONTACTS)
        for uid, body in cards:
            self.add(folder, SourceItem(key=f"v{_h(e.path, 12)}-{_h(uid, 20)}", alt_key="u:" + _h(uid), size=len(body),
                                        ref=("raw", body.encode("utf-8"))))


def scan_upload(path: str, filename: str) -> dict:
    """Analysis of an uploaded file that is not a PST, for `psts.scan`. Raises ValueError when nothing in it can be
    migrated."""
    tmp = tempfile.mkdtemp(prefix="mccb-scan-")
    try:
        b = _Builder(path, filename, lambda: tmp)
        if not b.entries:
            raise ValueError("the file is empty or the archive has no readable file")
        listing = b.build()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not listing.folders:
        raise ValueError("nothing recognized: expected mbox, Maildir, .eml, .msg, .ics or .vcf files, or a .zip of them")
    totals = {"mail": 0, "calendar": 0, "contacts": 0}
    for f in listing.folders:
        totals[f.kind] += int(f.count or 0)
    return {"format": FILES_KIND, "contents": sorted(b.formats),
            "folders": [{"path": f.path, "kind": f.kind, "count": int(f.count or 0)} for f in listing.folders],
            "totals": totals, "errors": [], "skipped": listing.skipped[:100], "n_skipped": len(listing.skipped)}


def is_files_scan(scan: dict | None) -> bool:
    return isinstance(scan, dict) and scan.get("format") == FILES_KIND


def looks_like_files(path: str, filename: str) -> bool:
    """Cheap test before the full analysis: a known extension, or content that looks like one of the formats."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    if head.startswith(PST_SIGNATURE):
        return False
    if _is_zip(path) or filename.lower().endswith(".zip"):
        return True
    return classify(filename, head) is not None


class FilesSource(Source):
    def __init__(self, config: dict, secrets: dict, ctx: dict | None = None):
        self.pst_id = config.get("pst_id")
        self._tmp: str | None = None
        self._builder: _Builder | None = None
        self._listing: _Listing | None = None
        self._zip: zipfile.ZipFile | None = None

    def _workdir(self) -> str:
        if self._tmp is None:
            from ..server.config import settings
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            self._tmp = tempfile.mkdtemp(prefix="mccb-files-", dir=str(settings.data_dir))
        return self._tmp

    def open(self) -> None:
        from ..server import db
        row = db.q1("SELECT * FROM psts WHERE id=?", (self.pst_id,)) if self.pst_id else None
        if row is None:
            raise ConnectorError("no file linked to the account", ConnectorError.CONFIG)
        if row["status"] != "valid":
            raise ConnectorError(f"file not valid (status {row['status']})", ConnectorError.CONFIG)
        self.close()
        try:
            self._builder = _Builder(row["path"], row["filename"], self._workdir)
        except (OSError, ValueError) as e:
            raise ConnectorError(f"cannot open the file: {e}", ConnectorError.CONFIG) from e
        self._listing = None

    def close(self) -> None:
        self._builder = self._listing = None
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    def _list(self) -> _Listing:
        if self._builder is None:
            raise ConnectorError("file source not open", ConnectorError.TRANSIENT)
        if self._listing is None:
            try:
                self._listing = self._builder.build()
            except (OSError, ValueError, zipfile.BadZipFile) as e:
                raise ConnectorError(f"cannot read the file: {e}", ConnectorError.CONFIG) from e
        return self._listing

    def folders(self) -> list[SourceFolder]:
        return list(self._list().folders)

    def iter_items(self, folder: SourceFolder) -> Iterator[SourceItem]:
        yield from self._list().items.get(folder.ref[1] + "\0" + folder.kind, [])

    def fetch(self, folder: SourceFolder, item: SourceItem, import_id: str) -> FetchedItem | None:
        kind = item.ref[0]
        b = self._builder
        if b is None:
            raise ConnectorError("file source not open", ConnectorError.TRANSIENT)
        if kind == "raw":
            raw = item.ref[1]
            return FetchedItem(raw=raw, subject=_summary(raw))
        if kind == "mbox":
            _k, epath, start, end = item.ref
            entry = next((e for e in b.entries if e.path == epath), None)
            if entry is None:
                return None
            with open(b._on_disk(entry), "rb") as f:
                f.seek(start)
                raw = unquote_mboxrd(f.read(end - start))
            return FetchedItem(raw=raw, internal_date=item.date, flags=list(item.flags), subject=subject_of(raw))
        entry = next((e for e in b.entries if e.path == item.ref[1]), None)
        if entry is None:
            return None
        try:
            data = entry.read()
        except (OSError, zipfile.BadZipFile, RuntimeError) as e:
            raise ConnectorError(f"cannot read {entry.path}: {e}", ConnectorError.REJECTED) from e
        warns: list[str] = []
        if kind == "msg":
            try:
                data, warns = msg_to_eml(data)
            except Exception as e:  # noqa: BLE001 - one broken file must not stop the others
                raise ConnectorError(f"cannot convert {entry.path}: {e}", ConnectorError.REJECTED) from e
            date = parse_date(str(BytesHeaderParser(policy=policy.default).parsebytes(data[:65536]).get("Date") or ""))
            return FetchedItem(raw=data, internal_date=date or item.date, flags=list(item.flags),
                               subject=subject_of(data), warnings=warns)
        return FetchedItem(raw=data, internal_date=item.date, flags=list(item.flags), subject=subject_of(data))


def _summary(raw: bytes) -> str:
    m = re.search(rb"(?im)^(?:SUMMARY|FN)[^:\r\n]*:(.*)$", raw[:65536])
    return m.group(1).decode("utf-8", "replace").strip()[:500] if m else ""


registry.register(registry.ConnectorSpec(
    kind=FILES_KIND, label="Files (mbox, Maildir, .eml, .msg, .ics, .vcf, .zip)",
    description="A file uploaded to this server, or dropped in the inbox folder: mail, calendars, contacts.",
    source=registry.Role(FilesSource, (MAIL, CALENDAR, CONTACTS), frozenset({CAP_FOLDERS, CAP_FLAGS, CAP_INTERNAL_DATE}),
                         (registry.Field("pst_id", "File", "number", required=True,
                                         help="Identifier of an uploaded file (GET /api/psts)."),),
                         lambda config: str(config.get("pst_id", "")))))
