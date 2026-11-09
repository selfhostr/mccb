"""Duplicates: which items of ONE folder (IMAP) or ONE collection (CalDAV, CardDAV) are copies of each other.

Pure functions, no network: dedup_runs.py reads the items, asks here for their keys, groups them and deletes all but
one item of each group. See docs/duplicates.md.

The criteria are strict on purpose: deleting is permanent, so an item is a duplicate only when nothing that a person
would see differs. A pair that looks alike but differs in one compared field is kept.

  mail      same Message-ID, same RFC822 size, and same Date, From and Subject headers (exact, whitespace folded).
            The size is the whole message byte for byte: two messages with the same Message-ID but another size (one
            lost an attachment, a list added a footer) are both kept. Date, From and Subject are compared too because
            some mailers reuse a Message-ID. A message without Message-ID is a duplicate only with one that has the
            same Date, From, Subject and size; without Date or From it is never one.
  events    the same UIDs (with their RECURRENCE-ID) in both resources; or, when the UIDs differ (an import made
            new ones), the same SUMMARY, DTSTART, DTEND (or DURATION), RRULE and set of exceptions, all exact.
            An event without SUMMARY or DTSTART only matches by UID.
  contacts  the same FN, the same set of e-mail addresses (case-insensitive) and the same set of phone numbers
            (digits and a leading +). A card without e-mail and phone, or a group, is never a duplicate.

Which one stays: mail, the oldest copy (lowest UID: the first one stored in the folder); events and contacts, the most
complete one (most property lines), then the first href. A group always keeps exactly one item.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.parser import BytesHeaderParser
from email import policy

KINDS = ("mail", "calendar", "contacts")
_WS = re.compile(r"\s+")


def _fold(s: str) -> str:
    return _WS.sub(" ", s or "").strip()


def _decoded(raw: str) -> str:
    try:
        return _fold(str(make_header(decode_header(raw))))
    except Exception:
        return _fold(raw)


# ------------------------------------------------------------------ mail
@dataclass
class MailInfo:
    key: str | None          # None: never a duplicate
    subject: str
    date: str
    sender: str


def mail_info(size: int, header: bytes) -> MailInfo:
    """Key of a message from its RFC822.SIZE and its MESSAGE-ID, DATE, FROM, SUBJECT header lines."""
    msg = BytesHeaderParser(policy=policy.compat32).parsebytes(header or b"")

    def raw(name: str) -> str:
        return " ".join(_fold(str(v)) for v in (msg.get_all(name) or []))

    mid = _WS.sub("", raw("Message-ID"))
    date, sender, subject = raw("Date"), raw("From"), raw("Subject")
    info = MailInfo(None, _decoded(subject), date, _decoded(sender))
    if not mid and not (date and sender):
        return info
    h = hashlib.sha256("\0".join((mid, date, sender, subject, str(int(size)))).encode("utf-8", "surrogateescape"))
    info.key = ("m:" if mid else "h:") + h.hexdigest()
    return info


# ------------------------------------------------------------------ iCalendar / vCard lines
def _lines(text: str) -> list[str]:
    """Content lines, unfolded (RFC 5545 3.1)."""
    out: list[str] = []
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        elif line.strip():
            out.append(line)
    return out


def _split(line: str) -> tuple[str, str, str]:
    """(NAME, ;params, value). The colon inside a quoted parameter value does not end the name part."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            head, value = line[:i], line[i + 1:]
            name, _, params = head.partition(";")
            # a group prefix (vCard "item1.EMAIL") is not part of the name
            return name.rsplit(".", 1)[-1].upper(), params, value
    return line.upper(), "", ""


@dataclass
class Component:
    kind: str
    props: list[tuple[str, str, str]] = field(default_factory=list)

    def get(self, name: str) -> tuple[str, str] | None:
        for n, p, v in self.props:
            if n == name:
                return p, v
        return None

    def value(self, name: str) -> str:
        got = self.get(name)
        return _fold(got[1]) if got else ""

    def full(self, name: str) -> str:
        """Parameters and value: DTSTART;TZID=Europe/Zurich:20240305T100000 differs from the same time in UTC."""
        got = self.get(name)
        if not got:
            return ""
        params = ";".join(sorted(p.strip().upper() for p in got[0].split(";") if p.strip()))
        return f"{params}:{got[1].strip()}"


def _components(text: str) -> tuple[list[Component], int]:
    """Top-level components below the VCALENDAR (or the VCARDs) and the number of content lines."""
    lines = _lines(text)
    top: list[Component] = []
    stack: list[Component] = []
    for line in lines:
        name, params, value = _split(line)
        if name == "BEGIN":
            c = Component(value.strip().upper())
            if not stack or stack[-1].kind == "VCALENDAR":
                if c.kind != "VCALENDAR":
                    top.append(c)
            stack.append(c)
        elif name == "END":
            if stack:
                stack.pop()
        elif stack:
            stack[-1].props.append((name, params, value))
    return top, len(lines)


# ------------------------------------------------------------------ events
@dataclass
class DavInfo:
    keys: list[str]          # empty: never a duplicate
    label: str
    detail: str
    completeness: int


def event_info(text: str) -> DavInfo | None:
    """Keys of a calendar resource; None when it holds no VEVENT (a task, a journal, unreadable)."""
    comps, n = _components(text)
    events = [c for c in comps if c.kind == "VEVENT"]
    if not events:
        return None
    master = next((e for e in events if not e.get("RECURRENCE-ID")), events[0])
    keys = []
    uids = [(e.value("UID"), e.full("RECURRENCE-ID")) for e in events]
    if all(u for u, _ in uids):
        keys.append("u:" + hashlib.sha256(repr(sorted(uids)).encode()).hexdigest())
    summary, start = master.value("SUMMARY"), master.full("DTSTART")
    if summary and start:
        end = master.full("DTEND") or ("D" + master.full("DURATION"))
        content = (summary, start, end, master.full("RRULE"), sorted(r for _, r in uids if r))
        keys.append("c:" + hashlib.sha256(repr(content).encode()).hexdigest())
    return DavInfo(keys, summary, master.value("DTSTART"), n)


# ------------------------------------------------------------------ contacts
def _email(v: str) -> str:
    v = v.strip().lower()
    return v[7:] if v.startswith("mailto:") else v


def _tel(v: str) -> str:
    v = v.strip()
    if v.lower().startswith("tel:"):
        v = v[4:]
    v = v.split(";", 1)[0]           # tel: URI parameters (;ext=...)
    digits = re.sub(r"\D", "", v)
    return ("+" if v.lstrip().startswith("+") else "") + digits if digits else ""


def contact_info(text: str) -> DavInfo | None:
    """Key of an address book resource; None when it is not one vCard."""
    comps, n = _components(text)
    cards = [c for c in comps if c.kind == "VCARD"]
    if len(cards) != 1:
        return None
    card = cards[0]
    fn = card.value("FN")
    emails = sorted({e for name, _, v in card.props if name == "EMAIL" for e in [_email(v)] if e})
    tels = sorted({t for name, _, v in card.props if name == "TEL" for t in [_tel(v)] if t})
    kind = (card.value("KIND") or card.value("X-ADDRESSBOOKSERVER-KIND")).lower()
    keys = []
    if fn and (emails or tels) and kind not in ("group", "org", "location"):
        keys.append("v:" + hashlib.sha256(repr((fn, emails, tels)).encode()).hexdigest())
    return DavInfo(keys, fn, (emails or tels or [""])[0], n)


# ------------------------------------------------------------------ groups
@dataclass
class Group:
    keep: str
    remove: list[str]

    @property
    def members(self) -> list[str]:
        return [self.keep, *self.remove]


def group(items: list[tuple[str, list[str], tuple]]) -> list[Group]:
    """items: (id, keys, rank). Items sharing a key are copies (transitively). Each group of two or more keeps the
    item of lowest rank and removes the others: never all of them."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    owner: dict[str, str] = {}
    rank = {}
    for iid, keys, r in items:
        parent.setdefault(iid, iid)
        rank[iid] = r
        for k in keys:
            if k in owner:
                a, b = find(owner[k]), find(iid)
                if a != b:
                    parent[b] = a
            else:
                owner[k] = iid
    members: dict[str, list[str]] = {}
    for iid in parent:
        members.setdefault(find(iid), []).append(iid)
    out = []
    for ids in members.values():
        if len(ids) < 2:
            continue
        ids.sort(key=lambda i: rank[i])
        out.append(Group(ids[0], ids[1:]))
    out.sort(key=lambda g: rank[g.keep])
    return out
