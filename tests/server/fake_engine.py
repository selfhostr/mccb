"""Fake ENGINE honoring the "ENGINE contract" (docs/specification.md) to test the server without pypff.

Content is driven by CONFIG (modifiable by tests); the "PST" is a file starting with !BDN.
The worker runs jobs in a subprocess: every CONFIG change is therefore copied to the
JSON file named by PSTMIG_FAKE_CONFIG (`env` fixture), re-read on each PstFile open.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from typing import Iterator

FAKE_CONFIG_ENV = "PSTMIG_FAKE_CONFIG"


class _Config(dict):
    """dict persisted to $PSTMIG_FAKE_CONFIG on every change (shared with the subprocess)."""

    def __setitem__(self, k, v):
        super().__setitem__(k, v)
        self.save()

    def update(self, *a, **kw):
        super().update(*a, **kw)
        self.save()

    def save(self) -> None:
        path = os.environ.get(FAKE_CONFIG_ENV)
        if not path:
            return
        data = {k: (sorted(v) if isinstance(v, set) else v) for k, v in self.items()}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)

    def load(self) -> None:
        path = os.environ.get(FAKE_CONFIG_ENV)
        if not path or not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for k in ("fail_keys", "reject_keys", "unreadable"):
            if k in data:
                data[k] = set(data[k])
        if "folders" in data:
            data["folders"] = [tuple(x) for x in data["folders"]]
        dict.update(self, data)


CONFIG: dict = _Config({
    "folders": [
        # (path, kind, container_class, special, count)
        (["Boîte de réception"], "mail", "IPF.Note", "inbox", 6),
        (["Boîte de réception", "Été 2024 – Projets"], "mail", "IPF.Note", None, 3),
        (["Éléments envoyés"], "mail", "IPF.Note", "sent", 2),
        (["Calendrier"], "calendar", "IPF.Appointment", None, 3),
        (["Contacts"], "contacts", "IPF.Contact", None, 2),
        (["Tâches"], "tasks", "IPF.Task", None, 1),
    ],
    "delay": 0.0,          # seconds per built item
    "fail_keys": set(),    # item_keys whose construction raises an exception
    "reject_keys": set(),  # item_keys rejected by verify_message
    "unreadable": set(),   # "<path joined by />#<index>" unreadable during iteration (fallback key <fid>-x<i>)
    "warnings": {},        # item_key -> list of (non-blocking) build warnings
    # "<path joined by />#<index>" -> epoch seconds (None = no date) overriding the default item date
    # (default: 2024-07-01 10:00 UTC + index hours, also the INTERNALDATE of the built mail)
    "dates": {},
})


@dataclass
class FolderInfo:
    path: list[str]
    kind: str
    container_class: str
    special: str | None
    count: int
    _folder: object = None


@dataclass
class BuiltMessage:
    raw: bytes
    internaldate: datetime
    flags: list[str]
    subject: str
    attachments: int
    size: int
    warnings: list[str] = field(default_factory=list)


@dataclass
class DavObject:
    uid: str
    filename: str
    content: str
    summary: str


def _fid(path: list[str]) -> str:
    return hashlib.sha1("/".join(path).encode()).hexdigest()[:8]


def item_key(path: list[str], i: int) -> str:
    return f"{1000 + i}-{_fid(path)}"


def check_pst_signature(path: str) -> bool:
    with open(path, "rb") as f:
        return f.read(4) == b"!BDN"


def verify_message(built: BuiltMessage, handle) -> list[str]:
    return ["rejected by the fake engine"] if handle["key"] in CONFIG["reject_keys"] else []


class PstFile:
    def __init__(self, path: str):
        if not check_pst_signature(path):
            raise ValueError("not a PST")
        CONFIG.load()
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def folders(self) -> list[FolderInfo]:
        return [FolderInfo(list(p), k, c, s, n, _folder=(tuple(p), n)) for p, k, c, s, n in CONFIG["folders"]]

    def iter_items(self, folder: FolderInfo, start: int = 0) -> Iterator[tuple[int, str, object]]:
        for i in range(start, folder.count):
            if f"{'/'.join(folder.path)}#{i}" in CONFIG.get("unreadable", ()):
                key = f"{_fid(folder.path)}-x{i}"  # like pst.iter_items: fallback key
                yield i, key, {"key": key, "i": i, "path": folder.path, "kind": folder.kind, "unreadable": True}
                continue
            key = item_key(folder.path, i)
            yield i, key, {"key": key, "i": i, "path": folder.path, "kind": folder.kind}

    @staticmethod
    def _date(handle) -> datetime | None:
        ref = f"{'/'.join(handle['path'])}#{handle['i']}"
        dates = CONFIG.get("dates") or {}
        if ref in dates:
            v = dates[ref]
            return None if v is None else datetime.fromtimestamp(v, timezone.utc)
        return datetime(2024, 7, 1, 10, 0, tzinfo=timezone.utc) + timedelta(hours=handle["i"])

    def item_date(self, handle, kind: str = "mail") -> datetime | None:
        """Sort date (engine extension): cheap, never builds the item."""
        if handle.get("unreadable"):
            return None
        return self._date(handle)

    def _wait(self):
        if CONFIG["delay"]:
            time.sleep(CONFIG["delay"])

    def build_message(self, handle, import_id: str) -> BuiltMessage | None:
        self._wait()
        if handle.get("unreadable"):
            raise RuntimeError("unreadable item (simulated)")
        if handle["key"] in CONFIG["fail_keys"]:
            raise RuntimeError("corrupted item (simulated)")
        if handle["kind"] != "mail":
            return None
        i = handle["i"]
        dt = self._date(handle) or datetime(1980, 1, 1, tzinfo=timezone.utc)
        m = EmailMessage()
        m["From"] = "Hélène Dupré <helene@example.test>"
        m["To"] = "client@example.test"
        m["Subject"] = f"Réunion d'été n°{i} – « {handle['path'][-1]} »"
        m["Date"] = format_datetime(dt)
        m["Message-ID"] = f"<{handle['key']}@fake.pst>"
        m["X-PST-Import-Id"] = import_id
        m.set_content(f"Bonjour,\n\nCorps accentué éàùç € n°{i}.\n", charset="utf-8")
        m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="Compte-rendu été.pdf")
        raw = m.as_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        return BuiltMessage(raw=raw, internaldate=dt, flags=["\\Seen"] if i % 2 == 0 else [],
                            subject=str(m["Subject"]), attachments=1, size=len(raw),
                            warnings=list(CONFIG.get("warnings", {}).get(handle["key"], [])))

    def build_event(self, handle) -> DavObject | None:
        self._wait()
        if handle["key"] in CONFIG["fail_keys"]:
            raise RuntimeError("corrupted appointment (simulated)")
        uid = f"pst-{handle['key']}"
        i = handle["i"]
        ics = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//fake//pst//FR\r\nBEGIN:VEVENT\r\n"
               f"UID:{uid}\r\nDTSTAMP:20240101T000000Z\r\nDTSTART:2024070{1 + i % 8}T090000Z\r\n"
               f"DTEND:2024070{1 + i % 8}T100000Z\r\nSUMMARY:Rendez-vous n°{i} à Genève\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        return DavObject(uid=uid, filename=f"{uid}.ics", content=ics, summary=f"Rendez-vous n°{i}")

    def build_contact(self, handle) -> DavObject | None:
        self._wait()
        if handle["key"] in CONFIG["fail_keys"]:
            raise RuntimeError("corrupted contact (simulated)")
        uid = f"pst-{handle['key']}"
        i = handle["i"]
        vcf = (f"BEGIN:VCARD\r\nVERSION:3.0\r\nUID:{uid}\r\nFN:Zoé Lefèvre {i}\r\nN:Lefèvre;Zoé {i};;;\r\n"
               f"EMAIL:zoe{i}@example.test\r\nEND:VCARD\r\n")
        return DavObject(uid=uid, filename=f"{uid}.vcf", content=vcf, summary=f"Zoé Lefèvre {i}")

    def scan(self) -> dict:
        fs = [{"path": list(p), "kind": k, "count": n} for p, k, c, s, n in CONFIG["folders"]]
        tot = {"mail": 0, "contacts": 0, "calendar": 0}
        for p, k, c, s, n in CONFIG["folders"]:
            if k in tot:
                tot[k] += n
        return {"folders": fs, "totals": tot, "errors": []}


def make_fake_pst(path: str, size: int = 300_000) -> bytes:
    import os
    data = b"!BDN" + os.urandom(size - 4)
    with open(path, "wb") as f:
        f.write(data)
    return data
