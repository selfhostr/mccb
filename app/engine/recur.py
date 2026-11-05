"""PidLidAppointmentRecur (MS-OXOCAL 2.2.1.44) -> RRULE / EXDATE / overrides."""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .charset import decode_bytes
from .mapi import minutes_to_dt

FREQ = {0x200A: "DAILY", 0x200B: "WEEKLY", 0x200C: "MONTHLY", 0x200D: "YEARLY"}
DAYS = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"]
END_DATE, END_COUNT, END_NEVER, END_NEVER2 = 0x2021, 0x2022, 0x2023, 0xFFFFFFFF


class RecurError(ValueError):
    pass


@dataclass
class ExceptionInfo:
    start: datetime            # local, naive
    end: datetime
    original_start: datetime
    flags: int
    subject: str | None = None
    location: str | None = None
    busy: int | None = None
    reminder_delta: int | None = None
    reminder_set: int | None = None
    subtype: int | None = None


@dataclass
class Recurrence:
    freq_code: int
    pattern_type: int
    calendar_type: int
    period: int
    first_dow: int
    end_type: int
    occurrences: int
    start_date: datetime          # local midnight
    end_date: datetime
    start_offset: int = 0         # minutes after midnight
    end_offset: int = 0
    day_mask: int = 0
    day_of_month: int = 0
    nth: int = 0
    deleted: list = field(default_factory=list)
    modified: list = field(default_factory=list)
    exceptions: list = field(default_factory=list)

    def rrule(self, month: int | None = None) -> dict:
        """icalendar-style dict (without UNTIL, added by caller)."""
        if self.calendar_type not in (0, 1):
            raise RecurError("non-gregorian calendar %d" % self.calendar_type)
        pt = self.pattern_type
        if pt in (0xA, 0xB, 0xC):
            raise RecurError("hijri pattern")
        r: dict = {}
        if pt == 0:
            r["FREQ"] = "DAILY"
            r["INTERVAL"] = max(1, self.period // 1440)
        elif pt == 1:
            r["FREQ"] = "WEEKLY"
            r["INTERVAL"] = max(1, self.period)
            days = [DAYS[i] for i in range(7) if self.day_mask & (1 << i)]
            if not days:
                raise RecurError("weekly pattern without days")
            r["BYDAY"] = days
            if self.first_dow in range(7):
                r["WKST"] = DAYS[self.first_dow]
        elif pt in (2, 3, 4):
            yearly = self.freq_code == 0x200D or (self.period and self.period % 12 == 0 and self.freq_code == 0x200D)
            if yearly:
                r["FREQ"] = "YEARLY"
                r["INTERVAL"] = max(1, self.period // 12)
                if month:
                    r["BYMONTH"] = month
            else:
                r["FREQ"] = "MONTHLY"
                r["INTERVAL"] = max(1, self.period)
            if pt == 2:
                r["BYMONTHDAY"] = -1 if self.day_of_month >= 31 else self.day_of_month
            elif pt == 4:
                r["BYMONTHDAY"] = -1
            else:
                days = [DAYS[i] for i in range(7) if self.day_mask & (1 << i)]
                if not days:
                    raise RecurError("monthNth without days")
                r["BYDAY"] = days
                r["BYSETPOS"] = -1 if self.nth >= 5 else self.nth
        else:
            raise RecurError("unknown pattern type %d" % pt)
        if r.get("INTERVAL") == 1:
            del r["INTERVAL"]
        if self.end_type == END_COUNT and self.occurrences:
            r["COUNT"] = self.occurrences
        return r

    @property
    def ends_by_date(self) -> bool:
        return self.end_type == END_DATE


def _u32(b, off):
    return struct.unpack_from("<I", b, off)[0], off + 4


def _u16(b, off):
    return struct.unpack_from("<H", b, off)[0], off + 2


def parse(blob: bytes, codepage: str | None = None) -> Recurrence:
    if not blob or len(blob) < 50:
        raise RecurError("recurrence blob too short")
    b = bytes(blob)
    try:
        off = 4  # reader/writer version
        freq, off = _u16(b, off)
        pt, off = _u16(b, off)
        cal, off = _u16(b, off)
        _first, off = _u32(b, off)
        period, off = _u32(b, off)
        _sliding, off = _u32(b, off)
        mask = dom = nth = 0
        if pt == 1:
            mask, off = _u32(b, off)
        elif pt in (2, 4, 0xA, 0xC):
            dom, off = _u32(b, off)
        elif pt in (3, 0xB):
            mask, off = _u32(b, off)
            nth, off = _u32(b, off)
        end_type, off = _u32(b, off)
        occ, off = _u32(b, off)
        fdow, off = _u32(b, off)
        ndel, off = _u32(b, off)
        if ndel > 10000:
            raise RecurError("absurd deleted count")
        deleted = [minutes_to_dt(struct.unpack_from("<I", b, off + 4 * i)[0]) for i in range(ndel)]
        off += 4 * ndel
        nmod, off = _u32(b, off)
        if nmod > 10000:
            raise RecurError("absurd modified count")
        modified = [minutes_to_dt(struct.unpack_from("<I", b, off + 4 * i)[0]) for i in range(nmod)]
        off += 4 * nmod
        sd, off = _u32(b, off)
        ed, off = _u32(b, off)
    except struct.error as ex:
        raise RecurError("truncated recurrence pattern: %s" % ex) from ex
    rec = Recurrence(freq_code=freq, pattern_type=pt, calendar_type=cal, period=period,
                     first_dow=fdow, end_type=end_type, occurrences=occ,
                     start_date=minutes_to_dt(sd), end_date=minutes_to_dt(min(ed, 0x5AE980DF)),
                     day_mask=mask, day_of_month=dom, nth=nth, deleted=deleted, modified=modified)
    # AppointmentRecurrencePattern extension
    try:
        off += 8  # ReaderVersion2, WriterVersion2
        writer2 = struct.unpack_from("<I", b, off - 4)[0]
        rec.start_offset, off = _u32(b, off)
        rec.end_offset, off = _u32(b, off)
        nexc, off = _u16(b, off)
        excs = []
        for _ in range(nexc):
            s, off = _u32(b, off)
            e, off = _u32(b, off)
            o, off = _u32(b, off)
            fl, off = _u16(b, off)
            ex = ExceptionInfo(minutes_to_dt(s), minutes_to_dt(e), minutes_to_dt(o), fl)
            if fl & 0x0001:
                _l1, off = _u16(b, off)
                l2, off = _u16(b, off)
                ex.subject = decode_bytes(b[off:off + l2], [codepage, "cp1252"])
                off += l2
            if fl & 0x0002:
                _mt, off = _u32(b, off)
            if fl & 0x0004:
                ex.reminder_delta, off = _u32(b, off)
            if fl & 0x0008:
                ex.reminder_set, off = _u32(b, off)
            if fl & 0x0010:
                _l1, off = _u16(b, off)
                l2, off = _u16(b, off)
                ex.location = decode_bytes(b[off:off + l2], [codepage, "cp1252"])
                off += l2
            if fl & 0x0020:
                ex.busy, off = _u32(b, off)
            if fl & 0x0040:
                _att, off = _u32(b, off)
            if fl & 0x0080:
                ex.subtype, off = _u32(b, off)
            if fl & 0x0100:
                _col, off = _u32(b, off)
            excs.append(ex)
        rec.exceptions = excs
        # ExtendedException: unicode subject / location
        try:
            rsz, off = _u32(b, off)
            off += rsz
            for ex in excs:
                if writer2 >= 0x3009:
                    chs, off = _u32(b, off)
                    off += chs
                rsz, off = _u32(b, off)
                off += rsz
                if ex.flags & 0x0011:
                    off += 12  # start, end, original start
                    if ex.flags & 0x0001:
                        n, off = _u16(b, off)
                        ex.subject = b[off:off + 2 * n].decode("utf-16-le", "replace")
                        off += 2 * n
                    if ex.flags & 0x0010:
                        n, off = _u16(b, off)
                        ex.location = b[off:off + 2 * n].decode("utf-16-le", "replace")
                        off += 2 * n
                    rsz, off = _u32(b, off)
                    off += rsz
        except struct.error:
            pass  # 8-bit values kept
    except struct.error:
        pass  # no extension: offsets stay 0, caller uses the appointment start
    return rec


def occurrence_start(rec: Recurrence, day: datetime) -> datetime:
    d = datetime(day.year, day.month, day.day)
    return d + timedelta(minutes=rec.start_offset)
