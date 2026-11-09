"""In-process fakes for the duplicates tab: an IMAP connection (UIDPLUS optional) and a CalDAV/CardDAV server that
honours If-Match, both with hooks to change the mailbox between the analysis and the deletion."""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from email.parser import BytesHeaderParser
from email import policy

import httpx

from app.server.imap import ImapError, Mailbox


class FakeImap:
    """Stands for ImapConn. folders: {name: {uid(int): [raw bytes, set(flags)]}}."""

    def __init__(self, folders: dict[str, dict[int, list]], uidplus: bool = True):
        self.folders = folders
        self.caps = {"IMAP4REV1"} | ({"UIDPLUS"} if uidplus else set())
        self.selected: str | None = None
        self.readonly = True
        self.writes: list[tuple] = []        # every STORE / EXPUNGE, for "the analysis writes nothing"
        self.selects: list[tuple[str, bool]] = []
        self.on_recheck = None               # called before a read-write fetch (the re-check before deleting)

    def __call__(self, _params):             # used as the ImapConn class
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def delimiter(self) -> str:
        return "/"

    def list(self):
        return [Mailbox(n, [], "/") for n in self.folders]

    def status_messages(self, name: str) -> int:
        return len(self.folders[name])

    def uidvalidity(self, name: str) -> str:
        return "1"

    def ensure_selected(self, name: str, readonly: bool) -> None:
        if self.selected != name or (not readonly and self.readonly):
            self.selected, self.readonly = name, readonly
            self.selects.append((name, readonly))

    def uids(self, name: str, readonly: bool = True) -> list[str]:
        self.ensure_selected(name, readonly)
        return [str(u) for u, (_, fl) in sorted(self.folders[name].items()) if "\\Deleted" not in fl]

    def fetch_dedup(self, name, uids, readonly=True):
        self.ensure_selected(name, readonly)
        if not readonly and self.on_recheck:
            self.on_recheck(self)
        out = []
        for u in uids:
            got = self.folders[name].get(int(u))
            if got is None:
                continue
            raw, flags = got
            head = raw.split(b"\r\n\r\n", 1)[0]
            msg = BytesHeaderParser(policy=policy.compat32).parsebytes(head)
            lines = b"".join(f"{k}: {v}\r\n".encode() for k, v in msg.items()
                             if k.lower() in ("message-id", "date", "from", "subject"))
            out.append((u, len(raw), sorted(flags), lines + b"\r\n"))
        return out

    def uid_delete(self, name: str, uids: list[str]) -> bool:
        assert self.selected == name and not self.readonly, "deleting in a folder opened read-only"
        for u in uids:
            self.folders[name][int(u)][1].add("\\Deleted")
        self.writes.append(("STORE", name, tuple(uids)))
        if "UIDPLUS" not in self.caps:
            return False
        for u in uids:
            del self.folders[name][int(u)]
        self.writes.append(("UID EXPUNGE", name, tuple(uids)))
        return True


def mail(subject: str, mid: str | None = "<a@x>", body: str = "hello", date: str = "Tue, 05 Mar 2024 10:00:00 +0100",
         sender: str = "zoe@example.com") -> bytes:
    h = [f"From: {sender}", "To: team@example.com", f"Subject: {subject}", f"Date: {date}"]
    if mid:
        h.append(f"Message-ID: {mid}")
    return ("\r\n".join(h) + "\r\n\r\n" + body + "\r\n").encode()


# ---------------------------------------------------------------------------------------------- DAV
D, C, CARD = "DAV:", "urn:ietf:params:xml:ns:caldav", "urn:ietf:params:xml:ns:carddav"


class FakeDav:
    """Collections {path: kind}; items {path: [etag, text]}. DELETE honours If-Match (412 when it differs)."""

    def __init__(self, etags: bool = True):
        self.collections: dict[str, str] = {}
        self.items: dict[str, list] = {}
        self.etags = etags
        self.deleted: list[str] = []
        self.on_delete = None            # called before a DELETE is answered
        self.transport = httpx.MockTransport(self.handle)
        self._n = 0

    def add(self, coll: str, name: str, text: str) -> str:
        path = coll + name
        self.items[path] = [self._tag(text), text]
        return path

    def _tag(self, text: str) -> str:
        self._n += 1
        return '"' + hashlib.sha1(f"{self._n}{text}".encode()).hexdigest()[:12] + '"'

    def change(self, path: str, text: str | None = None) -> None:
        self.items[path] = [self._tag(text or self.items[path][1]), text or self.items[path][1]]

    def _ms(self, responses) -> httpx.Response:
        root = ET.Element(f"{{{D}}}multistatus")
        for href, props in responses:
            r = ET.SubElement(root, f"{{{D}}}response")
            ET.SubElement(r, f"{{{D}}}href").text = href
            ps = ET.SubElement(r, f"{{{D}}}propstat")
            p = ET.SubElement(ps, f"{{{D}}}prop")
            for tag, val in props:
                el = ET.SubElement(p, tag)
                if isinstance(val, str):
                    el.text = val
                elif val is not None:
                    el.append(val)
            ET.SubElement(ps, f"{{{D}}}status").text = "HTTP/1.1 200 OK"
        return httpx.Response(207, content=ET.tostring(root))

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "PROPFIND":
            coll = ET.Element(f"{{{D}}}collection")
            out = [(path, [(f"{{{D}}}resourcetype", coll)])]
            for p, (tag, _) in sorted(self.items.items()):
                if p.startswith(path):
                    props = [(f"{{{D}}}resourcetype", None)]
                    if self.etags:
                        props.append((f"{{{D}}}getetag", tag))
                    out.append((p, props))
            return self._ms(out)
        if req.method == "REPORT":
            root = ET.fromstring(req.content)
            kind = self.collections[path]
            data_tag = f"{{{C}}}calendar-data" if kind == "caldav" else f"{{{CARD}}}address-data"
            out = []
            for h in root.findall(f"{{{D}}}href"):
                got = self.items.get(h.text)
                if got is not None:
                    props = [(data_tag, got[1])]
                    if self.etags:
                        props.append((f"{{{D}}}getetag", got[0]))
                    out.append((h.text, props))
            return self._ms(out)
        if req.method == "DELETE":
            if self.on_delete:
                self.on_delete(self, path)
            got = self.items.get(path)
            if got is None:
                return httpx.Response(404)
            if req.headers.get("if-match") != got[0]:
                return httpx.Response(412)
            del self.items[path]
            self.deleted.append(path)
            return httpx.Response(204)
        return httpx.Response(405)


def ics(uid: str, summary: str = "Réunion", start: str = "20240305T100000", end: str = "20240305T110000",
        extra: str = "") -> str:
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//t//EN\r\nBEGIN:VEVENT\r\n"
            f"UID:{uid}\r\nDTSTAMP:20240301T000000Z\r\nDTSTART;TZID=Europe/Zurich:{start}\r\n"
            f"DTEND;TZID=Europe/Zurich:{end}\r\nSUMMARY:{summary}\r\n{extra}END:VEVENT\r\nEND:VCALENDAR\r\n")


def vcf(uid: str, fn: str = "Zoé Müller", emails=("zoe@example.com",), tels=("+41 22 123 45 67",),
        extra: str = "") -> str:
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"UID:{uid}", f"FN:{fn}", "N:Müller;Zoé;;;"]
    lines += [f"EMAIL;TYPE=INTERNET:{e}" for e in emails] + [f"TEL;TYPE=CELL:{t}" for t in tels]
    return "\r\n".join(lines) + "\r\n" + extra + "END:VCARD\r\n"


def strip_ws(s: str) -> str:
    return re.sub(r"\s+", "", s)
