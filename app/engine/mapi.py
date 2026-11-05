"""MAPI property access on top of pypff record sets.

pypff exposes properties as record entries (``entry_type`` = property id,
``value_type`` = MAPI type, ``data`` = raw bytes).  We never trust
``get_data_as_string()`` (it mis-decodes PT_STRING8 values): every value is
decoded here from its raw bytes, PT_STRING8 with the item's codepage.
"""
from __future__ import annotations

import struct
import uuid
from datetime import datetime, timedelta, timezone

from .charset import ansi_for, codec_for_cpid, decode_bytes, guess_codec, is_unicode_codec

# --- MAPI property types -------------------------------------------------
PT_SHORT = 0x0002
PT_LONG = 0x0003
PT_FLOAT = 0x0004
PT_DOUBLE = 0x0005
PT_CURRENCY = 0x0006
PT_APPTIME = 0x0007
PT_ERROR = 0x000A
PT_BOOLEAN = 0x000B
PT_OBJECT = 0x000D
PT_I8 = 0x0014
PT_STRING8 = 0x001E
PT_UNICODE = 0x001F
PT_SYSTIME = 0x0040
PT_CLSID = 0x0048
PT_BINARY = 0x0102
PT_MV_LONG = 0x1003
PT_MV_STRING8 = 0x101E
PT_MV_UNICODE = 0x101F
PT_MV_BINARY = 0x1102

# --- property ids ----------------------------------------------------------
PR_IMPORTANCE = 0x0017
PR_MESSAGE_CLASS = 0x001A
PR_PRIORITY = 0x0026
PR_SENSITIVITY = 0x0036
PR_SUBJECT = 0x0037
PR_CLIENT_SUBMIT_TIME = 0x0039
PR_SENT_REPRESENTING_NAME = 0x0042
PR_START_DATE = 0x0060
PR_END_DATE = 0x0061
PR_SENT_REPRESENTING_ADDRTYPE = 0x0064
PR_SENT_REPRESENTING_EMAIL = 0x0065
PR_SUBJECT_PREFIX = 0x003D
PR_NORMALIZED_SUBJECT = 0x0E1D
PR_TRANSPORT_MESSAGE_HEADERS = 0x007D
PR_RECIPIENT_TYPE = 0x0C15
PR_SENDER_NAME = 0x0C1A
PR_SENDER_ADDRTYPE = 0x0C1E
PR_SENDER_EMAIL = 0x0C1F
PR_DISPLAY_BCC = 0x0E02
PR_DISPLAY_CC = 0x0E03
PR_DISPLAY_TO = 0x0E04
PR_MESSAGE_DELIVERY_TIME = 0x0E06
PR_MESSAGE_FLAGS = 0x0E07
PR_MESSAGE_SIZE = 0x0E08
PR_BODY = 0x1000
PR_RTF_COMPRESSED = 0x1009
PR_HTML = 0x1013
PR_INTERNET_MESSAGE_ID = 0x1035
PR_INTERNET_REFERENCES = 0x1039
PR_IN_REPLY_TO_ID = 0x1042
PR_ICON_INDEX = 0x1080
PR_LAST_VERB_EXECUTED = 0x1081
PR_FLAG_STATUS = 0x1090
PR_DISPLAY_NAME = 0x3001
PR_ADDRTYPE = 0x3002
PR_EMAIL_ADDRESS = 0x3003
PR_CREATION_TIME = 0x3007
PR_LAST_MODIFICATION_TIME = 0x3008
PR_CONTAINER_CLASS = 0x3613
PR_ATTACH_DATA = 0x3701
PR_ATTACH_EXTENSION = 0x3703
PR_ATTACH_FILENAME = 0x3704
PR_ATTACH_METHOD = 0x3705
PR_ATTACH_LONG_FILENAME = 0x3707
PR_ATTACH_MIME_TAG = 0x370E
PR_ATTACH_CONTENT_ID = 0x3712
PR_ATTACH_CONTENT_LOCATION = 0x3713
PR_ATTACH_FLAGS = 0x3714
PR_ATTACH_SIZE = 0x0E20
PR_ATTACHMENT_HIDDEN = 0x7FFE
PR_ATTACHMENT_CONTACTPHOTO = 0x7FFF
PR_SMTP_ADDRESS = 0x39FE
PR_INTERNET_CPID = 0x3FDE
PR_MESSAGE_CODEPAGE = 0x3FFD
PR_CODE_PAGE_ID = 0x66C3
PR_SENDER_SMTP_ADDRESS = 0x5D01
PR_SENT_REPRESENTING_SMTP_ADDRESS = 0x5D02
PR_RECIPIENT_FLAGS = 0x5FFD
PR_RECIPIENT_TRACKSTATUS = 0x5FFF
PR_RECORD_KEY = 0x0FF9
PR_IPM_SUBTREE_ENTRYID = 0x35E0
PR_IPM_OUTBOX_ENTRYID = 0x35E2
PR_IPM_WASTEBASKET_ENTRYID = 0x35E3
PR_IPM_SENTMAIL_ENTRYID = 0x35E4
PR_IPM_DRAFTS_ENTRYID = 0x36D7
PR_IPM_APPOINTMENT_ENTRYID = 0x36D0
PR_IPM_CONTACT_ENTRYID = 0x36D1
PR_ADDITIONAL_REN_ENTRYIDS = 0x36D8

# contacts (standard tags)
PR_GENERATION = 0x3A05
PR_GIVEN_NAME = 0x3A06
PR_BUSINESS_TELEPHONE = 0x3A08
PR_HOME_TELEPHONE = 0x3A09
PR_INITIALS = 0x3A0A
PR_SURNAME = 0x3A11
PR_COMPANY_NAME = 0x3A16
PR_TITLE = 0x3A17
PR_DEPARTMENT_NAME = 0x3A18
PR_OFFICE_LOCATION = 0x3A19
PR_PRIMARY_TELEPHONE = 0x3A1A
PR_BUSINESS2_TELEPHONE = 0x3A1B
PR_MOBILE_TELEPHONE = 0x3A1C
PR_RADIO_TELEPHONE = 0x3A1D
PR_CAR_TELEPHONE = 0x3A1E
PR_OTHER_TELEPHONE = 0x3A1F
PR_PAGER_TELEPHONE = 0x3A21
PR_PRIMARY_FAX = 0x3A23
PR_BUSINESS_FAX = 0x3A24
PR_HOME_FAX = 0x3A25
PR_BUSINESS_COUNTRY = 0x3A26
PR_BUSINESS_CITY = 0x3A27
PR_BUSINESS_STATE = 0x3A28
PR_BUSINESS_STREET = 0x3A29
PR_BUSINESS_POSTAL_CODE = 0x3A2A
PR_BUSINESS_PO_BOX = 0x3A2B
PR_ISDN = 0x3A2D
PR_ASSISTANT_TELEPHONE = 0x3A2E
PR_HOME2_TELEPHONE = 0x3A2F
PR_ASSISTANT = 0x3A30
PR_WEDDING_ANNIVERSARY = 0x3A41
PR_BIRTHDAY = 0x3A42
PR_MIDDLE_NAME = 0x3A44
PR_DISPLAY_NAME_PREFIX = 0x3A45
PR_PROFESSION = 0x3A46
PR_SPOUSE_NAME = 0x3A48
PR_NICKNAME = 0x3A4F
PR_PERSONAL_HOME_PAGE = 0x3A50
PR_BUSINESS_HOME_PAGE = 0x3A51
PR_COMPANY_MAIN_PHONE = 0x3A57
PR_HOME_CITY = 0x3A59
PR_HOME_COUNTRY = 0x3A5A
PR_HOME_POSTAL_CODE = 0x3A5B
PR_HOME_STATE = 0x3A5C
PR_HOME_STREET = 0x3A5D
PR_HOME_PO_BOX = 0x3A5E
PR_OTHER_CITY = 0x3A5F
PR_OTHER_COUNTRY = 0x3A60
PR_OTHER_POSTAL_CODE = 0x3A61
PR_OTHER_STATE = 0x3A62
PR_OTHER_STREET = 0x3A63
PR_OTHER_PO_BOX = 0x3A64

# --- property sets (named properties) -----------------------------------
PS_MAPI = "00020328-0000-0000-C000-000000000046"
PS_PUBLIC_STRINGS = "00020329-0000-0000-C000-000000000046"
PSETID_APPOINTMENT = "00062002-0000-0000-C000-000000000046"
PSETID_TASK = "00062003-0000-0000-C000-000000000046"
PSETID_ADDRESS = "00062004-0000-0000-C000-000000000046"
PSETID_COMMON = "00062008-0000-0000-C000-000000000046"
PSETID_MEETING = "6ED8DA90-450B-101B-98DA-00AA003F1305"

# PSETID_Appointment
LID_BUSY_STATUS = 0x8205
LID_LOCATION = 0x8208
LID_START_WHOLE = 0x820D
LID_END_WHOLE = 0x820E
LID_DURATION = 0x8213
LID_SUBTYPE = 0x8215          # all-day
LID_RECUR = 0x8216            # PidLidAppointmentRecur
LID_STATE_FLAGS = 0x8217
LID_RECURRING = 0x8223
LID_TZ_STRUCT = 0x8233
LID_TZ_DESCRIPTION = 0x8234
LID_CLIP_START = 0x8235
LID_CLIP_END = 0x8236
LID_TZDEF_START = 0x825E
LID_TZDEF_END = 0x825F
LID_TZDEF_RECUR = 0x8260
# PSETID_Common
LID_REMINDER_DELTA = 0x8501
LID_REMINDER_TIME = 0x8502
LID_REMINDER_SET = 0x8503
LID_COMMON_START = 0x8516
LID_COMMON_END = 0x8517
LID_FLAG_REQUEST = 0x8530
LID_CATEGORIES_NAME = "Keywords"   # PS_PUBLIC_STRINGS
# PSETID_Meeting
LID_GLOBAL_OBJECT_ID = 0x0003
LID_CLEAN_GLOBAL_OBJECT_ID = 0x0023
# PSETID_Address
LID_FILE_UNDER = 0x8005
LID_HAS_PICTURE = 0x8015
LID_HOME_ADDRESS = 0x801A
LID_WORK_ADDRESS = 0x801B
LID_OTHER_ADDRESS = 0x801C
LID_HTML = 0x802B
LID_WORK_STREET = 0x8045
LID_WORK_CITY = 0x8046
LID_WORK_STATE = 0x8047
LID_WORK_POSTAL = 0x8048
LID_WORK_COUNTRY = 0x8049
LID_WORK_POBOX = 0x804A
LID_IM_ADDRESS = 0x8062
LID_EMAIL1_DISPLAY = 0x8080
LID_EMAIL1_TYPE = 0x8082
LID_EMAIL1_ADDRESS = 0x8083
LID_EMAIL1_ORIG_DISPLAY = 0x8084
LID_EMAIL2_DISPLAY = 0x8090
LID_EMAIL2_TYPE = 0x8092
LID_EMAIL2_ADDRESS = 0x8093
LID_EMAIL2_ORIG_DISPLAY = 0x8094
LID_EMAIL3_DISPLAY = 0x80A0
LID_EMAIL3_TYPE = 0x80A2
LID_EMAIL3_ADDRESS = 0x80A3
LID_EMAIL3_ORIG_DISPLAY = 0x80A4
LID_BIRTHDAY_LOCAL = 0x80DE
LID_ANNIVERSARY_LOCAL = 0x80DF

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def filetime_to_dt(value: int) -> datetime | None:
    """FILETIME (100 ns since 1601, UTC) -> aware datetime, None if unset."""
    if not value or value <= 0:
        return None
    # Outlook uses 4501-01-01 as "none"
    if value >= 0x0CB34557_4E7C9000 - 1:
        return None
    try:
        return _FILETIME_EPOCH + timedelta(microseconds=value // 10)
    except OverflowError:
        return None


def minutes_to_dt(minutes: int) -> datetime:
    """Minutes since 1601-01-01 (naive, as used by recurrence blobs)."""
    return datetime(1601, 1, 1) + timedelta(minutes=minutes)


def guid_str(raw: bytes) -> str:
    return str(uuid.UUID(bytes_le=bytes(raw[:16]))).upper()


def entryid_nid(raw: bytes | None) -> int | None:
    """Node id from a PST/OST entry id (flags[4] + provider uid[16] + nid[4])."""
    if not raw or len(raw) < 24:
        return None
    return struct.unpack_from("<I", raw, 20)[0]


class NameMap:
    """Parsed name-to-id map (MS-PST 2.4.7): (guid, lid|name) -> prop id."""

    def __init__(self, item=None):
        self.by_key: dict[tuple[str, object], int] = {}
        self.by_id: dict[int, tuple[str, object]] = {}
        if item is not None:
            try:
                self._parse(item)
            except Exception:  # corrupt map -> named props simply unavailable
                self.by_key.clear()
                self.by_id.clear()

    def _parse(self, item):
        streams = {}
        for rs in item.record_sets:
            for e in rs.entries:
                if e.entry_type in (0x0002, 0x0003, 0x0004):
                    streams[e.entry_type] = e.data or b""
            break
        guids = streams.get(0x0002, b"")
        entries = streams.get(0x0003, b"")
        strings = streams.get(0x0004, b"")
        for off in range(0, len(entries) - 7, 8):
            dw, guid_n, idx = struct.unpack_from("<IHH", entries, off)
            is_string = guid_n & 1
            wguid = guid_n >> 1
            if wguid == 1:
                guid = PS_MAPI
            elif wguid == 2:
                guid = PS_PUBLIC_STRINGS
            else:
                g = (wguid - 3) * 16
                if g < 0 or g + 16 > len(guids):
                    continue
                guid = guid_str(guids[g:g + 16])
            if is_string:
                if dw + 4 > len(strings):
                    continue
                ln = struct.unpack_from("<I", strings, dw)[0]
                name = strings[dw + 4:dw + 4 + ln].decode("utf-16-le", "replace")
                key = (guid, name)
            else:
                key = (guid, dw)
            pid = 0x8000 + idx
            self.by_key[key] = pid
            self.by_id[pid] = key

    def pid(self, guid: str, lid) -> int | None:
        return self.by_key.get((guid.upper(), lid))


class Prop:
    __slots__ = ("tag", "vtype", "_entry", "_data", "_loaded")

    def __init__(self, tag, vtype, entry=None, data=None):
        self.tag = tag
        self.vtype = vtype
        self._entry = entry
        self._data = data
        self._loaded = entry is None

    @property
    def data(self) -> bytes | None:
        if not self._loaded:
            try:
                self._data = self._entry.data
            except Exception:
                self._data = None
            self._loaded = True
        return self._data


class PropBag:
    """Properties of one pypff item (or one record set of it)."""

    def __init__(self, props: dict[int, Prop], namemap: NameMap | None = None,
                 codepage: str | None = None, default_codepage: str = "cp1252"):
        self.props = props
        self.namemap = namemap or NameMap()
        self.default_codepage = default_codepage
        self._codepage = codepage
        self._cp_known = codepage is not None

    # PT_STRING8 codepage ------------------------------------------------
    @property
    def codepage(self) -> str | None:
        """Codec of this item's PT_STRING8 values.

        PR_MESSAGE_CODEPAGE when set (and not a Unicode codepage, which makes
        no sense for 8-bit strings); otherwise the ANSI codepage matching
        PR_INTERNET_CPID, checked against (and if clearly implausible,
        replaced by) a detection over all the item's 8-bit strings; None when
        they are all ASCII (callers then use ``default_codepage``)."""
        if not self._cp_known:
            self._cp_known = True
            cp = self.int(PR_MESSAGE_CODEPAGE)
            c = codec_for_cpid(cp) if cp else None
            if c is None or is_unicode_codec(c):
                c = self._guess_codepage()
            self._codepage = c
        return self._codepage

    @codepage.setter
    def codepage(self, value: str | None) -> None:
        self._codepage = value
        self._cp_known = True

    def _guess_codepage(self) -> str | None:
        icp = self.int(PR_INTERNET_CPID)
        inet = codec_for_cpid(icp) if icp else None
        hints = [c for c in (ansi_for(inet), inet) if c and not is_unicode_codec(c)]
        samples, total = [], 0
        for p in self.props.values():
            if p.vtype not in (PT_STRING8, PT_MV_STRING8):
                continue
            try:
                d = p.data
            except Exception:  # noqa: BLE001
                continue
            if not d:
                continue
            d = bytes(d)
            pieces = [c.split(b"\x00", 1)[0] for c in split_mv(d)] if p.vtype == PT_MV_STRING8 \
                else [d.split(b"\x00", 1)[0]]
            for piece in pieces:
                if piece and not piece.isascii():
                    samples.append(piece)
                    total += min(len(piece), 8192)
            if total > 32768:
                break
        if not samples:
            return hints[0] if hints else None
        return guess_codec(samples, hints, self.default_codepage)

    # construction -----------------------------------------------------
    @classmethod
    def from_record_set(cls, rs, namemap=None, codepage=None, default_codepage="cp1252"):
        props: dict[int, Prop] = {}
        for e in rs.entries:
            try:
                tag = e.entry_type
                vt = e.value_type
            except Exception:
                continue
            if tag not in props:
                props[tag] = Prop(tag, vt, entry=e)
        return cls(props, namemap, codepage, default_codepage)

    @classmethod
    def from_item(cls, item, namemap=None, codepage=None, default_codepage="cp1252"):
        props: dict[int, Prop] = {}
        n = item.number_of_record_sets
        if n:
            rs = item.get_record_set(0)
            return cls.from_record_set(rs, namemap, codepage, default_codepage)
        return cls(props, namemap, codepage, default_codepage)

    @classmethod
    def from_dict(cls, values: dict, namemap=None, codepage=None, default_codepage="cp1252"):
        """Synthetic bag (tests): {tag: (vtype, bytes)}."""
        props = {t: Prop(t, vt, data=d) for t, (vt, d) in values.items()}
        return cls(props, namemap, codepage, default_codepage)

    # access -----------------------------------------------------------
    def has(self, tag) -> bool:
        return tag in self.props

    def raw(self, tag) -> bytes | None:
        p = self.props.get(tag)
        return p.data if p is not None else None

    def vtype(self, tag):
        p = self.props.get(tag)
        return p.vtype if p is not None else None

    def named_tag(self, guid, lid) -> int | None:
        return self.namemap.pid(guid, lid)

    def _resolve(self, tag_or_named):
        if isinstance(tag_or_named, tuple):
            return self.named_tag(*tag_or_named)
        return tag_or_named

    def string(self, tag, default=None) -> str | None:
        tag = self._resolve(tag)
        p = self.props.get(tag) if tag is not None else None
        if p is None:
            return default
        d = p.data
        if d is None:
            return default
        if p.vtype == PT_UNICODE:
            s = decode_utf16(d)
        elif p.vtype == PT_STRING8:
            s = decode_bytes(d.split(b"\x00", 1)[0], [self.codepage, self.default_codepage])
        elif p.vtype == PT_BINARY:
            s = decode_bytes(d.rstrip(b"\x00"), [self.codepage, "utf-8", self.default_codepage])
        else:
            return default
        s = s.split("\x00", 1)[0] if p.vtype != PT_BINARY else s
        return s

    def strings(self, tag) -> list[str]:
        tag = self._resolve(tag)
        p = self.props.get(tag) if tag is not None else None
        if p is None or p.data is None:
            return []
        if p.vtype in (PT_UNICODE, PT_STRING8):
            s = self.string(tag)
            return [s] if s else []
        if p.vtype not in (PT_MV_UNICODE, PT_MV_STRING8):
            return []
        out = []
        for chunk in split_mv(p.data):
            if p.vtype == PT_MV_UNICODE:
                out.append(decode_utf16(chunk))
            else:
                out.append(decode_bytes(chunk.split(b"\x00", 1)[0], [self.codepage, self.default_codepage]))
        return [s for s in out if s]

    def binaries(self, tag) -> list[bytes]:
        tag = self._resolve(tag)
        p = self.props.get(tag) if tag is not None else None
        if p is None or p.data is None:
            return []
        if p.vtype == PT_BINARY:
            return [p.data]
        if p.vtype == PT_MV_BINARY:
            return split_mv(p.data)
        return []

    def int(self, tag, default=None) -> int | None:
        tag = self._resolve(tag)
        p = self.props.get(tag) if tag is not None else None
        if p is None:
            return default
        d = p.data
        if not d:
            return default
        try:
            if p.vtype == PT_SHORT:
                return struct.unpack_from("<h", d)[0]
            if p.vtype in (PT_LONG, PT_ERROR):
                return struct.unpack_from("<i", d)[0] if len(d) >= 4 else default
            if p.vtype in (PT_I8, PT_SYSTIME, PT_CURRENCY):
                return struct.unpack_from("<q", d)[0] if len(d) >= 8 else default
            if p.vtype == PT_BOOLEAN:
                return 1 if d[0] else 0
        except struct.error:
            return default
        return default

    def uint(self, tag, default=None) -> int | None:
        v = self.int(tag, None)
        if v is None:
            return default
        return v & 0xFFFFFFFF if v < 0 and self.vtype(self._resolve(tag)) == PT_LONG else v

    def bool(self, tag, default=False) -> bool:
        v = self.int(tag, None)
        return default if v is None else bool(v)

    def time(self, tag) -> datetime | None:
        tag = self._resolve(tag)
        p = self.props.get(tag) if tag is not None else None
        if p is None or p.vtype != PT_SYSTIME or not p.data or len(p.data) < 8:
            return None
        return filetime_to_dt(struct.unpack_from("<Q", p.data)[0])

    def float(self, tag, default=None):
        tag = self._resolve(tag)
        p = self.props.get(tag) if tag is not None else None
        if p is None or not p.data:
            return default
        try:
            if p.vtype == PT_DOUBLE or p.vtype == PT_APPTIME:
                return struct.unpack_from("<d", p.data)[0]
            if p.vtype == PT_FLOAT:
                return struct.unpack_from("<f", p.data)[0]
        except struct.error:
            pass
        return default


def decode_utf16(d: bytes) -> str:
    if len(d) % 2:
        d = d[:-1]
    s = d.decode("utf-16-le", "surrogatepass")
    # lone surrogates cannot be encoded to UTF-8: replace them (source defect)
    s = s.encode("utf-8", "replace").decode("utf-8") if _has_lone_surrogate(s) else s
    return s.split("\x00", 1)[0]


def _has_lone_surrogate(s: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in s)


def split_mv(data: bytes) -> list[bytes]:
    """Variable-size multi-value: count(4) + offsets(4*count) + data."""
    if len(data) < 4:
        return []
    n = struct.unpack_from("<I", data)[0]
    if n == 0 or 4 + 4 * n > len(data):
        return []
    offs = list(struct.unpack_from("<%dI" % n, data, 4)) + [len(data)]
    out = []
    for i in range(n):
        a, b = offs[i], offs[i + 1]
        if 0 <= a <= b <= len(data):
            out.append(data[a:b])
    return out
