"""Second forms of a vCard or an iCalendar object, for a DAV server that answered 500 to the object as it is (seen on
SmarterMail). Only used after that refusal: the object as the source has it is always written first.

- `normalized`: the same object in the plainest form of its format: vCard 3.0 (a 4.0 card converted), FN and N
  present, CRLF line ends, lines folded at 75 octets, an oversized PHOTO left out; an iCalendar object with VERSION,
  PRODID and a DTSTAMP in each component, and an oversized inline attachment left out.
- `minimal`: also without the X- properties (the members of a contact group kept) and the binary properties
  (photos, logos, sounds, keys, attachments).

Each returns (text, notes): what was changed, in words, for the log and the item's warnings."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from ..server.dav import decode_object

BIG = 256 * 1024  # characters of an inline value (base64) above which a photo or an attachment is left out
_LINE = re.compile(r'^((?:[^:"]|"[^"]*")*):(.*)$')
_BINARY = frozenset({"PHOTO", "LOGO", "SOUND", "KEY"})


def unfold(text: str) -> list[str]:
    return [ln for ln in re.split(r"\r\n|\r|\n", re.sub(r"\r?\n[ \t]", "", text)) if ln.strip()]


def fold(line: str) -> str:
    """One content line folded at 75 octets (RFC 6350 / 5545), never inside a UTF-8 character."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > 75:
            out.append(cur)
            cur, size = " ", 1
        cur += ch
        size += n
    out.append(cur)
    return "\r\n".join(out)


def _split(line: str) -> tuple[str, list[str], str]:
    """(NAME without its group, params, value)."""
    m = _LINE.match(line)
    head, value = (m.group(1), m.group(2)) if m else (line, "")
    parts = head.split(";")
    name = parts[0].split(".")[-1].upper()  # "item1.EMAIL" (Apple): the group is dropped
    return name, parts[1:], value


def _join(name: str, params: list[str], value: str) -> str:
    return ";".join([name, *params]) + ":" + value


def object_uid(raw: bytes | str) -> str | None:
    text = decode_object(raw) if isinstance(raw, bytes) else raw
    for line in unfold(text):
        name, _p, value = _split(line)
        if name == "UID" and value.strip():
            return value.strip()
    return None


# ------------------------------------------------------------------ vCard
def vcard(text: str, minimal: bool = False) -> tuple[str, list[str]]:
    notes: list[str] = []
    lines = unfold(text)
    if sum(1 for ln in lines if ln.upper() == "BEGIN:VCARD") != 1:
        return text, []  # not one card: left as it is
    out: list[str] = []
    version = None
    fn = n = None
    org = email = None
    dropped: set[str] = set()
    for line in lines:
        name, params, value = _split(line)
        if name in ("BEGIN", "END"):
            continue
        if name == "VERSION":
            version = value.strip()
            continue
        if name == "KIND":  # 4.0
            if value.strip().lower() == "group":
                out.append("X-ADDRESSBOOKSERVER-KIND:group")
            continue
        if minimal and ((name.startswith("X-") and not name.startswith("X-ADDRESSBOOKSERVER-")) or name in _BINARY):
            dropped.add(name)
            continue
        up = [p.upper() for p in params]
        if name == "TEL" and "VALUE=URI" in up:  # 4.0: TEL;VALUE=uri:tel:+41...
            params = [p for p in params if p.upper() != "VALUE=URI"]
            value = re.sub(r"^tel:", "", value, flags=re.I)
        if name in _BINARY:
            m = re.match(r"^data:([\w.+-]+)/([\w.+-]+)?(?:;[^,]*)?;base64,(.*)$", value, re.I | re.S)
            if m:  # 4.0 data URI -> 3.0 inline value
                params = [p for p in params if not p.upper().startswith(("VALUE=", "MEDIATYPE="))]
                params += ["ENCODING=b", f"TYPE={(m.group(2) or 'jpeg').upper()}"]
                value = m.group(3)
            params = ["ENCODING=b" if p.upper() in ("ENCODING=BASE64", "BASE64") else p for p in params]
            if len(value) > BIG:
                notes.append(f"{name.lower()} of {len(value) * 3 // 4 // 1024} KB left out")
                continue
        if name == "FN":
            fn = value
        elif name == "N":
            n = value
        elif name == "ORG" and org is None:
            org = value.split(";")[0]
        elif name == "EMAIL" and email is None:
            email = value
        out.append(_join(name, params, value))
    if version != "3.0":
        notes.append(f"vCard {version or 'without version'} written as 3.0")
    if not (fn or "").strip():
        parts = [p for p in (n or "").split(";")[:2] if p.strip()]
        fn = " ".join(reversed(parts)) or org or email or "Contact"
        out.insert(0, _join("FN", [], fn))
        notes.append("FN added")
    if n is None:
        words = fn.split()
        n = f"{words[-1]};{' '.join(words[:-1])};;;" if len(words) > 1 else f"{fn};;;;"
        out.insert(1, _join("N", [], n))
        notes.append("N added")
    if dropped:
        notes.append("left out: " + ", ".join(sorted(dropped)))
    body = ["BEGIN:VCARD", "VERSION:3.0", *out, "END:VCARD"]
    return "".join(fold(ln) + "\r\n" for ln in body), notes


# ------------------------------------------------------------------ iCalendar
_COMPONENTS = ("VEVENT", "VTODO", "VJOURNAL")


def ical(text: str, minimal: bool = False) -> tuple[str, list[str]]:
    notes: list[str] = []
    lines = unfold(text)
    if not lines or lines[0].upper() != "BEGIN:VCALENDAR":
        return text, []
    out: list[str] = []
    stack: list[tuple[str, dict[str, str]]] = []  # open components: (name, {property: value})
    dropped: set[str] = set()
    top: set[str] = set()
    for line in lines:
        name, params, value = _split(line)
        if name == "BEGIN":
            stack.append((value.strip().upper(), {}))
            out.append(f"BEGIN:{stack[-1][0]}")
            continue
        if name == "END":
            comp, props = stack.pop() if stack else (value.strip().upper(), {})
            if comp in _COMPONENTS and "DTSTAMP" not in props:  # required by RFC 5545
                when = props.get("LAST-MODIFIED") or props.get("CREATED") or ""
                if not re.fullmatch(r"\d{8}T\d{6}Z", when):
                    when = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                out.append(f"DTSTAMP:{when}")
                notes.append("DTSTAMP added")
            out.append(f"END:{comp}")
            continue
        if len(stack) == 1:
            top.add(name)
            if name == "VERSION":
                value = "2.0"
        if minimal and (name.startswith("X-") or name == "ATTACH"):
            dropped.add(name)
            continue
        if name == "ATTACH" and len(value) > BIG:
            notes.append(f"attachment of {len(value) * 3 // 4 // 1024} KB left out")
            continue
        if stack:
            stack[-1][1][name] = value.strip()
        out.append(_join(name, params, value))
    if "PRODID" not in top:
        out.insert(1, "PRODID:-//mccb//migration//EN")
        notes.append("PRODID added")
    if "VERSION" not in top:
        out.insert(1, "VERSION:2.0")
        notes.append("VERSION added")
    if dropped:
        notes.append("left out: " + ", ".join(sorted(dropped)))
    return "".join(fold(ln) + "\r\n" for ln in out), list(dict.fromkeys(notes))


def forms(dav_kind: str, text: str) -> list[tuple[str, str, list[str]]]:
    """[(name of the form, text, notes)] to try after the object as it is, each different from the one before."""
    fix = vcard if dav_kind == "carddav" else ical
    out = []
    seen = {text}
    for label, minimal in (("normalized", False), ("minimal", True)):
        t, notes = fix(text, minimal)
        if t not in seen:
            seen.add(t)
            out.append((label, t, notes))
    return out
