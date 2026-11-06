"""IPM.Appointment -> VCALENDAR (RFC 5545)."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import icalendar

from . import mapi
from . import recur as recurmod
from .message import BuildError, clean_subject, extract_bodies, html_to_text, message_class
from .tzwin import WinTZ, iana_for, parse_tzdefinition, parse_tzstruct

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

A = mapi.PSETID_APPOINTMENT
C = mapi.PSETID_COMMON
PRODID = "-//yety//MCCB//EN"
_PARTSTAT = {0: "NEEDS-ACTION", 1: "ACCEPTED", 2: "TENTATIVE", 3: "ACCEPTED", 4: "DECLINED",
             5: "NEEDS-ACTION"}
_BUSY = {0: "FREE", 1: "TENTATIVE", 2: "BUSY", 3: "OOF", 4: "WORKINGELSEWHERE"}


def is_event_class(cls: str) -> bool:
    return (cls or "").lower().startswith("ipm.appointment")


def _zone(name):
    if not name or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def _resolve_tz(bag, recurring: bool):
    """Returns (tzinfo|None, label, is_iana)."""
    candidates = []
    for lid in ((mapi.LID_TZDEF_RECUR, mapi.LID_TZDEF_START) if recurring else
                (mapi.LID_TZDEF_START, mapi.LID_TZDEF_RECUR)):
        key, rule = parse_tzdefinition(bag.raw(bag.named_tag(A, lid)) if bag.named_tag(A, lid) else None)
        if key or rule:
            candidates.append((key, rule))
    desc = bag.string((A, mapi.LID_TZ_DESCRIPTION))
    struct_rule = parse_tzstruct(bag.raw(bag.named_tag(A, mapi.LID_TZ_STRUCT))
                                 if bag.named_tag(A, mapi.LID_TZ_STRUCT) else None)
    for key, rule in candidates:
        z = _zone(iana_for(key))
        if z is not None:
            return z, key, True
    z = _zone(iana_for(None, desc))
    if z is not None:
        return z, desc, True
    for key, rule in candidates:
        if rule is not None:
            return WinTZ(rule, key or "Custom"), key or "Custom", False
    if struct_rule is not None:
        return WinTZ(struct_rule, desc or "Custom"), desc or "Custom", False
    return None, None, False


def _vtimezone_for_wintz(tz: WinTZ, tzid: str) -> icalendar.Timezone:
    vt = icalendar.Timezone()
    vt.add("tzid", tzid)
    std_off, dst_off = tz.offsets()
    r = tz.rule
    if not r.has_dst:
        st = icalendar.TimezoneStandard()
        st.add("dtstart", datetime(1601, 1, 1))
        st.add("tzoffsetfrom", std_off)
        st.add("tzoffsetto", std_off)
        vt.add_component(st)
        return vt

    def comp(cls, t, off_from, off_to):
        c = cls()
        c.add("dtstart", datetime(1601, t.month, 1, t.hour, t.minute, t.second))
        c.add("tzoffsetfrom", off_from)
        c.add("tzoffsetto", off_to)
        nth = -1 if t.day >= 5 else t.day
        c.add("rrule", {"FREQ": "YEARLY", "BYMONTH": t.month,
                        "BYDAY": "%d%s" % (nth, recurmod.DAYS[t.dow % 7])})
        return c

    vt.add_component(comp(icalendar.TimezoneStandard, r.std, dst_off, std_off))
    vt.add_component(comp(icalendar.TimezoneDaylight, r.dst, std_off, dst_off))
    return vt


def deterministic_uid(store_key: bytes, nid: int, kind: str) -> str:
    h = hashlib.sha1(store_key + b"|" + kind.encode() + b"|" + str(nid).encode()).hexdigest()
    return "pst-%s-%s" % (kind, h[:32])


def _body_text(bag, warnings):
    text, html = extract_bodies(bag, warnings)
    if text:
        return text
    if html:
        return html_to_text(html)
    return None


def _cal_address(name, addr, **params):
    a = icalendar.vCalAddress("mailto:" + addr)
    if name and name.strip("'\" ") and name.strip().lower() != addr.lower():
        a.params["CN"] = name.strip().strip("'\"")
    for k, v in params.items():
        a.params[k.replace("_", "-").upper()] = v
    return a


def build_event(src, uid: str):
    """Returns (summary, ics_text, warnings) or None if not an appointment."""
    from .message import _smtp, recipient_of
    bag = src.bag
    if not is_event_class(message_class(bag)):
        return None
    warnings: list[str] = []
    start = (bag.time((A, mapi.LID_START_WHOLE)) or bag.time((C, mapi.LID_COMMON_START))
             or bag.time(mapi.PR_START_DATE))
    end = (bag.time((A, mapi.LID_END_WHOLE)) or bag.time((C, mapi.LID_COMMON_END))
           or bag.time(mapi.PR_END_DATE))
    if start is None:
        raise BuildError("appointment without start time")
    if end is None or end < start:
        dur = bag.int((A, mapi.LID_DURATION))
        end = start + timedelta(minutes=dur) if dur and dur > 0 else start
    allday = bag.bool((A, mapi.LID_SUBTYPE))
    summary = clean_subject(bag.string(mapi.PR_SUBJECT))
    rec = None
    rblob = bag.raw(bag.named_tag(A, mapi.LID_RECUR)) if bag.named_tag(A, mapi.LID_RECUR) else None
    is_recurring = bag.bool((A, mapi.LID_RECURRING)) or bool(rblob)
    if is_recurring:
        if rblob:
            try:
                rec = recurmod.parse(rblob, bag.codepage)
            except recurmod.RecurError as ex:
                warnings.append("recurrence-unreadable:%s" % ex)
        else:
            warnings.append("recurrence-missing")
    tz, tzlabel, tz_iana = _resolve_tz(bag, rec is not None)
    if tz is None and rec is not None and rec.start_offset:
        # derive a fixed offset from local (blob) vs UTC (StartWhole)
        local = recurmod.occurrence_start(rec, rec.start_date)
        diff = local - start.replace(tzinfo=None)
        minutes = round(diff.total_seconds() / 900) * 15
        if abs(minutes) <= 14 * 60:
            tz = timezone(timedelta(minutes=minutes))
            tzlabel = None
            warnings.append("timezone-guessed")

    cal = icalendar.Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    ev = icalendar.Event()
    ev.add("uid", uid)
    stamp = bag.time(mapi.PR_LAST_MODIFICATION_TIME) or bag.time(mapi.PR_CREATION_TIME) or start
    ev.add("dtstamp", stamp.astimezone(timezone.utc))
    created = bag.time(mapi.PR_CREATION_TIME)
    if created:
        ev.add("created", created.astimezone(timezone.utc))
    lm = bag.time(mapi.PR_LAST_MODIFICATION_TIME)
    if lm:
        ev.add("last-modified", lm.astimezone(timezone.utc))
    ev.add("summary", summary or "")

    def local(dt):
        if tz is None:
            return dt.astimezone(timezone.utc)
        return dt.astimezone(tz)

    custom_tzid = None
    if isinstance(tz, WinTZ):
        custom_tzid = (tzlabel or "Custom").replace('"', "").strip() or "Custom"

    def add_dt(comp, name, value, is_date=False):
        if is_date:
            comp.add(name, value)
        elif custom_tzid:
            comp.add(name, value.replace(tzinfo=None), parameters={"TZID": custom_tzid})
        else:
            comp.add(name, value)

    if allday:
        if tz is not None:
            sd, ed = start.astimezone(tz).date(), end.astimezone(tz).date()
        else:
            sd, ed = (start + timedelta(hours=12)).date(), (end + timedelta(hours=12)).date()
        if rec is not None:
            sd = rec.start_date.date() if rec.start_date.year > 1601 else sd
        if ed <= sd:
            ed = sd + timedelta(days=max(1, round((end - start).total_seconds() / 86400)))
        add_dt(ev, "dtstart", sd, True)
        add_dt(ev, "dtend", ed, True)
    else:
        add_dt(ev, "dtstart", local(start))
        add_dt(ev, "dtend", local(end))

    if rec is not None:
        try:
            first_local = start.astimezone(tz) if tz is not None else start
            rr = rec.rrule(month=first_local.month)
            if rec.ends_by_date:
                last_day = rec.end_date
                if allday:
                    rr["UNTIL"] = last_day.date()
                else:
                    lt = recurmod.occurrence_start(rec, last_day)
                    if tz is not None:
                        rr["UNTIL"] = lt.replace(tzinfo=tz).astimezone(timezone.utc)
                    else:
                        rr["UNTIL"] = lt.replace(tzinfo=timezone.utc)
            ev.add("rrule", rr)
            exc_orig = {e.original_start.date() for e in rec.exceptions}
            exdates = []
            for d in rec.deleted:
                if d.date() in exc_orig:
                    continue
                if allday:
                    exdates.append(d.date())
                else:
                    lt = recurmod.occurrence_start(rec, d)
                    exdates.append(lt.replace(tzinfo=tz) if tz is not None else lt.replace(tzinfo=timezone.utc))
            if exdates:
                if allday:
                    ev.add("exdate", exdates)
                elif custom_tzid:
                    ev.add("exdate", [x.replace(tzinfo=None) for x in exdates],
                           parameters={"TZID": custom_tzid})
                else:
                    ev.add("exdate", exdates)
        except recurmod.RecurError as ex:
            warnings.append("recurrence-unsupported:%s" % ex)
            rec = None

    location = bag.string((A, mapi.LID_LOCATION))
    if location:
        ev.add("location", location)
    desc = _body_text(bag, warnings)
    if desc and desc.strip():
        ev.add("description", desc.strip())
    cats = bag.strings((mapi.PS_PUBLIC_STRINGS, mapi.LID_CATEGORIES_NAME))
    if cats:
        ev.add("categories", cats)
    sens = bag.int(mapi.PR_SENSITIVITY, 0) or 0
    ev.add("class", {2: "PRIVATE", 3: "CONFIDENTIAL"}.get(sens, "PUBLIC"))
    busy = bag.int((A, mapi.LID_BUSY_STATUS))
    if busy is not None:
        ev.add("transp", "TRANSPARENT" if busy == 0 else "OPAQUE")
        if busy in _BUSY:
            ev.add("x-microsoft-cdo-busystatus", _BUSY[busy])
    state = bag.int((A, mapi.LID_STATE_FLAGS), 0) or 0
    if state & 0x4:
        ev.add("status", "CANCELLED")
    else:
        ev.add("status", "CONFIRMED")

    # organizer / attendees (SCHEDULE-AGENT=CLIENT: the server must not send invitations)
    attendees = []
    for rb in src.recipients():
        try:
            name, addr, rtype = recipient_of(rb)
        except Exception:  # noqa: BLE001
            continue
        rflags = rb.int(mapi.PR_RECIPIENT_FLAGS, 0) or 0
        if rflags & 0x0002:  # recipOrganizer
            continue
        if not addr:
            if name:
                warnings.append("attendee-without-address:%s" % name)
            continue
        track = rb.int(mapi.PR_RECIPIENT_TRACKSTATUS, 0) or 0
        params = {"partstat": _PARTSTAT.get(track, "NEEDS-ACTION"), "schedule_agent": "CLIENT"}
        if rtype == 3:
            params.update(cutype="RESOURCE", role="NON-PARTICIPANT")
        else:
            params.update(cutype="INDIVIDUAL",
                          role="OPT-PARTICIPANT" if rtype == 2 else "REQ-PARTICIPANT")
        attendees.append(_cal_address(name, addr, **params))
    if attendees:
        oname = bag.string(mapi.PR_SENT_REPRESENTING_NAME) or bag.string(mapi.PR_SENDER_NAME)
        oaddr = (_smtp(bag.string(mapi.PR_SENT_REPRESENTING_ADDRTYPE),
                       bag.string(mapi.PR_SENT_REPRESENTING_EMAIL),
                       bag.string(mapi.PR_SENT_REPRESENTING_SMTP_ADDRESS))
                 or _smtp(bag.string(mapi.PR_SENDER_ADDRTYPE), bag.string(mapi.PR_SENDER_EMAIL),
                          bag.string(mapi.PR_SENDER_SMTP_ADDRESS)))
        if oaddr:
            ev.add("organizer", _cal_address(oname, oaddr, schedule_agent="CLIENT"))
        for a in attendees:
            ev.add("attendee", a)

    if bag.bool((C, mapi.LID_REMINDER_SET)):
        delta = bag.int((C, mapi.LID_REMINDER_DELTA))
        if delta is None or delta < 0 or delta > 60 * 24 * 365:
            delta = 15
        al = icalendar.Alarm()
        al.add("action", "DISPLAY")
        al.add("description", summary or "Rappel")
        al.add("trigger", timedelta(minutes=-delta))
        ev.add_component(al)

    cal.add_component(ev)

    # exceptions (modified occurrences)
    if rec is not None:
        for ex in rec.exceptions:
            try:
                ov = icalendar.Event()
                ov.add("uid", uid)
                ov.add("dtstamp", stamp.astimezone(timezone.utc))
                if allday:
                    ov.add("recurrence-id", ex.original_start.date())
                    ov.add("dtstart", ex.start.date())
                    ov.add("dtend", max(ex.end.date(), ex.start.date() + timedelta(days=1)))
                else:
                    def mk(d):
                        return d.replace(tzinfo=tz) if tz is not None else d.replace(tzinfo=timezone.utc)
                    orig = mk(ex.original_start)
                    if ex.original_start.hour == 0 and ex.original_start.minute == 0 and rec.start_offset:
                        orig = mk(recurmod.occurrence_start(rec, ex.original_start))
                    add_dt(ov, "recurrence-id", orig)
                    add_dt(ov, "dtstart", mk(ex.start))
                    add_dt(ov, "dtend", mk(ex.end))
                ov.add("summary", ex.subject if ex.subject is not None else (summary or ""))
                loc = ex.location if ex.location is not None else location
                if loc:
                    ov.add("location", loc)
                if desc and desc.strip():
                    ov.add("description", desc.strip())
                for a in ev.get("attendee", []) if isinstance(ev.get("attendee"), list) else (
                        [ev["attendee"]] if "attendee" in ev else []):
                    ov.add("attendee", a)
                if "organizer" in ev:
                    ov.add("organizer", ev["organizer"])
                cal.add_component(ov)
            except Exception as e:  # noqa: BLE001
                warnings.append("exception-skipped:%s" % e)

    if custom_tzid:
        cal.subcomponents.insert(0, _vtimezone_for_wintz(tz, custom_tzid))
    elif tz is not None and tz_iana:
        try:
            cal.add_missing_timezones()
        except Exception as e:  # noqa: BLE001
            warnings.append("vtimezone-failed:%s" % e)
    text = cal.to_ical().decode("utf-8")
    return summary, text, warnings
