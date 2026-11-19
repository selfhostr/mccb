"""IPM.Contact -> vCard 3.0 (RFC 2426), UTF-8, folded at 75 octets."""
from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta, timezone

from . import mapi
from .message import extract_bodies, html_to_text, message_class

AD = mapi.PSETID_ADDRESS
# C0 controls (but TAB, LF, CR) and DEL: not allowed in a vCard value, servers (SabreDAV, vobject) refuse the card
_CTRL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def is_contact_class(cls: str) -> bool:
    return (cls or "").lower().startswith("ipm.contact")


def is_distlist_class(cls: str) -> bool:
    return (cls or "").lower().startswith("ipm.distlist")


def is_address_book_class(cls: str) -> bool:
    """Items an address book folder holds and that are migrated: contacts and contact groups."""
    return is_contact_class(cls) or is_distlist_class(cls)


def _esc(s: str) -> str:
    s = _CTRL.sub("", s)
    return (s.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n")
            .replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,"))


def _fold(line: str) -> str:
    b = line.encode("utf-8")
    if len(b) <= 75:
        return line + "\r\n"
    out, cur, limit = [], b"", 75
    for ch in line:
        cb = ch.encode("utf-8")
        if len(cur) + len(cb) > limit:
            out.append(cur)
            cur, limit = b"", 74
        cur += cb
    out.append(cur)
    return "\r\n ".join(x.decode("utf-8") for x in out) + "\r\n"


def _date_of(dt_local: datetime | None, dt_utc: datetime | None):
    if dt_local is not None:
        return dt_local.date()
    if dt_utc is not None:
        # stored as local midnight converted to UTC: round to the nearest day
        return (dt_utc + timedelta(hours=12)).date()
    return None


def build_contact(src, uid: str):
    """Returns (summary, vcard_text, warnings) or None if not a contact."""
    bag = src.bag
    cls = message_class(bag)
    if is_distlist_class(cls):
        return build_distlist(src, uid)
    if not is_contact_class(cls):
        return None
    warnings: list[str] = []
    s = bag.string
    given, surname = s(mapi.PR_GIVEN_NAME) or "", s(mapi.PR_SURNAME) or ""
    middle, prefix, suffix = s(mapi.PR_MIDDLE_NAME) or "", s(mapi.PR_DISPLAY_NAME_PREFIX) or "", s(mapi.PR_GENERATION) or ""
    company = s(mapi.PR_COMPANY_NAME) or ""
    emails = []
    for lid_addr, lid_type, lid_disp in ((mapi.LID_EMAIL1_ADDRESS, mapi.LID_EMAIL1_TYPE, mapi.LID_EMAIL1_ORIG_DISPLAY),
                                        (mapi.LID_EMAIL2_ADDRESS, mapi.LID_EMAIL2_TYPE, mapi.LID_EMAIL2_ORIG_DISPLAY),
                                        (mapi.LID_EMAIL3_ADDRESS, mapi.LID_EMAIL3_TYPE, mapi.LID_EMAIL3_ORIG_DISPLAY)):
        addr = (s((AD, lid_addr)) or "").strip()
        atype = (s((AD, lid_type)) or "SMTP").upper()
        if atype == "EX" or "@" not in addr:
            disp = s((AD, lid_disp)) or ""
            if "@" in disp:
                addr = disp.split("(")[-1].rstrip(")").strip() if "(" in disp else disp.strip()
        if addr and "@" in addr and addr.lower() not in [e.lower() for e in emails]:
            emails.append(addr)
        elif addr and "@" not in addr:
            # e.g. an Exchange (EX) legacy DN with no SMTP form anywhere on the contact: the address is lost
            warnings.append("email-not-smtp:%s" % atype)
    fn = (s(mapi.PR_DISPLAY_NAME) or " ".join(x for x in (prefix, given, middle, surname, suffix) if x)
          or s((AD, mapi.LID_FILE_UNDER)) or company or (emails[0] if emails else "") or "Contact")
    fn = fn.strip()

    lines = ["BEGIN:VCARD", "VERSION:3.0", "PRODID:-//yety//mccb//EN",
             "UID:" + _esc(uid), "FN:" + _esc(fn),
             "N:" + ";".join(_esc(x) for x in (surname, given, middle, prefix, suffix))]
    nick = s(mapi.PR_NICKNAME)
    if nick:
        lines.append("NICKNAME:" + _esc(nick))
    dept = s(mapi.PR_DEPARTMENT_NAME) or ""
    if company or dept:
        lines.append("ORG:" + _esc(company) + (";" + _esc(dept) if dept else ""))
    if s(mapi.PR_TITLE):
        lines.append("TITLE:" + _esc(s(mapi.PR_TITLE)))
    if s(mapi.PR_PROFESSION):
        lines.append("ROLE:" + _esc(s(mapi.PR_PROFESSION)))
    for i, e in enumerate(emails):
        lines.append("EMAIL;TYPE=INTERNET%s:%s" % (",PREF" if i == 0 else "", _esc(e)))
    phones = [
        (mapi.PR_BUSINESS_TELEPHONE, "WORK,VOICE"), (mapi.PR_BUSINESS2_TELEPHONE, "WORK,VOICE"),
        (mapi.PR_HOME_TELEPHONE, "HOME,VOICE"), (mapi.PR_HOME2_TELEPHONE, "HOME,VOICE"),
        (mapi.PR_MOBILE_TELEPHONE, "CELL,VOICE"), (mapi.PR_BUSINESS_FAX, "WORK,FAX"),
        (mapi.PR_HOME_FAX, "HOME,FAX"), (mapi.PR_PRIMARY_FAX, "FAX"), (mapi.PR_PAGER_TELEPHONE, "PAGER"),
        (mapi.PR_CAR_TELEPHONE, "CAR"), (mapi.PR_PRIMARY_TELEPHONE, "PREF,VOICE"),
        (mapi.PR_OTHER_TELEPHONE, "VOICE"), (mapi.PR_ASSISTANT_TELEPHONE, "X-ASSISTANT,VOICE"),
        (mapi.PR_COMPANY_MAIN_PHONE, "WORK,X-COMPANY,VOICE"), (mapi.PR_ISDN, "ISDN"),
        (mapi.PR_RADIO_TELEPHONE, "VOICE"),
    ]
    seen = set()
    for tag, typ in phones:
        v = (s(tag) or "").strip()
        if v and (v, typ) not in seen:
            seen.add((v, typ))
            lines.append("TEL;TYPE=%s:%s" % (typ, _esc(v)))
    addrs = [
        ("WORK", [s((AD, mapi.LID_WORK_POBOX)) or s(mapi.PR_BUSINESS_PO_BOX),
                  s((AD, mapi.LID_WORK_STREET)) or s(mapi.PR_BUSINESS_STREET),
                  s((AD, mapi.LID_WORK_CITY)) or s(mapi.PR_BUSINESS_CITY),
                  s((AD, mapi.LID_WORK_STATE)) or s(mapi.PR_BUSINESS_STATE),
                  s((AD, mapi.LID_WORK_POSTAL)) or s(mapi.PR_BUSINESS_POSTAL_CODE),
                  s((AD, mapi.LID_WORK_COUNTRY)) or s(mapi.PR_BUSINESS_COUNTRY)]),
        ("HOME", [s(mapi.PR_HOME_PO_BOX), s(mapi.PR_HOME_STREET), s(mapi.PR_HOME_CITY),
                  s(mapi.PR_HOME_STATE), s(mapi.PR_HOME_POSTAL_CODE), s(mapi.PR_HOME_COUNTRY)]),
        ("POSTAL", [s(mapi.PR_OTHER_PO_BOX), s(mapi.PR_OTHER_STREET), s(mapi.PR_OTHER_CITY),
                    s(mapi.PR_OTHER_STATE), s(mapi.PR_OTHER_POSTAL_CODE), s(mapi.PR_OTHER_COUNTRY)]),
    ]
    for typ, (pobox, street, city, state, postal, country) in addrs:
        if any((pobox, street, city, state, postal, country)):
            lines.append("ADR;TYPE=%s:%s" % (typ, ";".join(_esc(x or "") for x in (
                pobox, "", street, city, state, postal, country))))
    bday = _date_of(bag.time((AD, mapi.LID_BIRTHDAY_LOCAL)), bag.time(mapi.PR_BIRTHDAY))
    if bday and bday.year > 1601:
        lines.append("BDAY;VALUE=date:" + bday.isoformat())
    ann = _date_of(bag.time((AD, mapi.LID_ANNIVERSARY_LOCAL)), bag.time(mapi.PR_WEDDING_ANNIVERSARY))
    if ann and ann.year > 1601:
        lines.append("X-ANNIVERSARY:" + ann.isoformat())
    for tag in (mapi.PR_BUSINESS_HOME_PAGE, mapi.PR_PERSONAL_HOME_PAGE):
        v = s(tag)
        if v:
            lines.append("URL:" + _esc(v))
    if not (s(mapi.PR_BUSINESS_HOME_PAGE) or s(mapi.PR_PERSONAL_HOME_PAGE)) and s((AD, mapi.LID_HTML)):
        lines.append("URL:" + _esc(s((AD, mapi.LID_HTML))))
    im = s((AD, mapi.LID_IM_ADDRESS))
    if im:
        lines.append("X-MS-IMADDRESS:" + _esc(im))
    if s(mapi.PR_SPOUSE_NAME):
        lines.append("X-SPOUSE:" + _esc(s(mapi.PR_SPOUSE_NAME)))
    if s(mapi.PR_ASSISTANT):
        lines.append("X-ASSISTANT:" + _esc(s(mapi.PR_ASSISTANT)))
    cats = bag.strings((mapi.PS_PUBLIC_STRINGS, mapi.LID_CATEGORIES_NAME))
    if cats:
        lines.append("CATEGORIES:" + ",".join(_esc(c) for c in cats))
    text, html = extract_bodies(bag, warnings)
    note = text or (html_to_text(html) if html else None)
    if note and note.strip():
        lines.append("NOTE:" + _esc(note.strip()))
    # photo: attachment flagged PR_ATTACHMENT_CONTACTPHOTO
    for att in src.attachments():
        if isinstance(att, Exception):
            continue
        try:
            if not att.bag.bool(mapi.PR_ATTACHMENT_CONTACTPHOTO):
                continue
            data = att.read_data()
            from .detect import detect
            mime = detect(data, att.bag.string(mapi.PR_ATTACH_LONG_FILENAME), att.bag.string(mapi.PR_ATTACH_MIME_TAG))
            typ = mime.split("/")[-1].upper().replace("JPG", "JPEG")
            lines.append("PHOTO;ENCODING=b;TYPE=%s:%s" % (typ, base64.b64encode(data).decode("ascii")))
            break
        except Exception as ex:  # noqa: BLE001
            warnings.append("photo-unreadable:%s" % ex)
    _rev(bag, lines)
    lines.append("END:VCARD")
    return fn, "".join(_fold(l) for l in lines), warnings


def _rev(bag, lines: list[str]) -> None:
    rev = bag.time(mapi.PR_LAST_MODIFICATION_TIME)
    if rev:
        lines.append("REV:" + rev.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def distlist_members(bag) -> tuple[list[tuple[str, str]], int]:
    """([(display name, SMTP address)], members without a usable address) of an IPM.DistList.
    PidLidDistributionListOneOffMembers holds a one-off EntryID (name, address) per member, parallel to
    PidLidDistributionListMembers, whose entries are wrapped EntryIDs of contacts of the store or one-offs."""
    one_offs = bag.binaries((AD, mapi.LID_DISTLIST_ONE_OFF_MEMBERS))
    members = bag.binaries((AD, mapi.LID_DISTLIST_MEMBERS))
    cp = bag.codepage or bag.default_codepage or "cp1252"
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    missing = 0
    for i in range(max(len(one_offs), len(members))):
        parsed = None
        for eid in ((one_offs[i] if i < len(one_offs) else None), (members[i] if i < len(members) else None)):
            parsed = mapi.parse_one_off_entryid(eid, cp)
            if parsed:
                break
        if not parsed or "@" not in parsed[2] or parsed[1].upper() not in ("SMTP", ""):
            missing += 1
            continue
        name, _atype, addr = parsed
        if addr.lower() in seen:
            continue
        seen.add(addr.lower())
        out.append((name if name and name.lower() != addr.lower() else "", addr))
    return out, missing


def build_distlist(src, uid: str):
    """IPM.DistList -> vCard 3.0 contact group (Apple / SabreDAV convention X-ADDRESSBOOKSERVER-KIND:group). The
    members are kept three times, so that no client loses them: mailto members, EMAIL lines and the NOTE."""
    bag = src.bag
    warnings: list[str] = []
    s = bag.string
    name = (s(mapi.PR_DISPLAY_NAME) or s((AD, mapi.LID_DISTLIST_NAME)) or s((AD, mapi.LID_FILE_UNDER))
            or "Contact group").strip() or "Contact group"
    members, missing = distlist_members(bag)
    if missing:
        warnings.append("distlist-member-without-address:%d" % missing)
    lines = ["BEGIN:VCARD", "VERSION:3.0", "PRODID:-//yety//mccb//EN",
             "UID:" + _esc(uid), "FN:" + _esc(name), "N:" + _esc(name) + ";;;;",
             "X-ADDRESSBOOKSERVER-KIND:group"]
    for _n, addr in members:
        lines.append("X-ADDRESSBOOKSERVER-MEMBER:mailto:" + _esc(addr))
    for _n, addr in members:
        lines.append("EMAIL;TYPE=INTERNET:" + _esc(addr))
    cats = bag.strings((mapi.PS_PUBLIC_STRINGS, mapi.LID_CATEGORIES_NAME))
    if cats:
        lines.append("CATEGORIES:" + ",".join(_esc(c) for c in cats))
    text, html = extract_bodies(bag, warnings)
    note = (text or (html_to_text(html) if html else None) or "").strip()
    listing = "\n".join("%s <%s>" % (n, a) if n else a for n, a in members)
    note = "\n\n".join(x for x in (note, ("Members:\n" + listing) if listing else "") if x)
    if note:
        lines.append("NOTE:" + _esc(note))
    _rev(bag, lines)
    lines.append("END:VCARD")
    return name, "".join(_fold(l) for l in lines), warnings
