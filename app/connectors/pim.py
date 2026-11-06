"""Calendar, contacts and tasks between Exchange and the DAV formats: a neutral model filled from EWS or Graph, and
its conversion to and from iCalendar and vCard. Pure: no network. See docs/exchange.md for what does not survive.

The engine (app/engine) is imported on first use only: importing it loads libpff, which a web process does not need."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from .base import ConnectorError

DAYS = ("SU", "MO", "TU", "WE", "TH", "FR", "SA")
UTC = timezone.utc
PRODID = "-//yety//MCCB//EN"


@dataclass
class Person:
    email: str
    name: str = ""
    role: str = "REQ"             # REQ | OPT | RES
    partstat: str = "NEEDS-ACTION"


@dataclass
class Recurrence:
    freq: str                     # DAILY | WEEKLY | MONTHLY | YEARLY
    interval: int = 1
    by_day: list[str] = field(default_factory=list)  # MO, TU...
    set_pos: int | None = None    # 1 to 4, or -1 for the last: "second Monday", with by_day
    month_day: int | None = None
    month: int | None = None
    week_start: str | None = None
    count: int | None = None
    until: date | None = None     # last day, in the time zone of the event


@dataclass
class Event:
    uid: str
    start: datetime | date
    end: datetime | date          # datetime: aware. date: all-day, exclusive end
    subject: str = ""
    body: str = ""
    location: str = ""
    tz: str | None = None         # IANA name the event was written in; None: UTC
    organizer: Person | None = None
    attendees: list[Person] = field(default_factory=list)
    free_busy: str = "BUSY"       # FREE | TENTATIVE | BUSY | OOF | WORKINGELSEWHERE
    sensitivity: str = "PUBLIC"   # PUBLIC | PRIVATE | CONFIDENTIAL
    categories: list[str] = field(default_factory=list)
    reminder_minutes: int | None = None
    recurrence: Recurrence | None = None
    exdates: list[datetime | date] = field(default_factory=list)
    overrides: list["Event"] = field(default_factory=list)
    recurrence_id: datetime | date | None = None
    created: datetime | None = None
    modified: datetime | None = None
    cancelled: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)


@dataclass
class Contact:
    uid: str
    full_name: str = ""
    surname: str = ""
    given: str = ""
    middle: str = ""
    prefix: str = ""
    suffix: str = ""
    nickname: str = ""
    company: str = ""
    department: str = ""
    title: str = ""
    profession: str = ""
    emails: list[str] = field(default_factory=list)
    phones: list[tuple[str, str]] = field(default_factory=list)  # (kind of PHONE_TYPES, number)
    addresses: list[dict] = field(default_factory=list)  # {kind: work|home|other, pobox, street, city, state, postal, country}
    birthday: date | None = None
    anniversary: date | None = None
    urls: list[str] = field(default_factory=list)
    im: str = ""
    spouse: str = ""
    assistant: str = ""
    categories: list[str] = field(default_factory=list)
    note: str = ""
    modified: datetime | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class Task:
    uid: str
    subject: str = ""
    body: str = ""
    status: str = "NEEDS-ACTION"  # NEEDS-ACTION | IN-PROCESS | COMPLETED | CANCELLED
    priority: str = "normal"      # low | normal | high
    percent: int | None = None
    start: datetime | date | None = None
    due: datetime | date | None = None
    completed: datetime | None = None
    categories: list[str] = field(default_factory=list)
    recurrence: Recurrence | None = None
    created: datetime | None = None
    modified: datetime | None = None
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ time zones
def _tzwin():
    from ..engine import tzwin
    return tzwin


@lru_cache(maxsize=512)
def zone(name: str | None):
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):
        return None


def iana_of(name: str | None) -> str | None:
    """IANA name of a Windows or IANA zone name. None: unknown (a custom zone, for instance)."""
    if not name:
        return None
    name = name.strip()
    if name.upper() in ("UTC", "ETC/UTC", "GMT"):
        return "UTC"
    got = _tzwin().iana_for(name)
    return got if got and zone(got) is not None else None


def windows_zone(iana: str | None, when: datetime | date | None = None) -> str | None:
    """Windows zone id of an IANA zone: from the CLDR table when it lists this zone (CLDR territory 001), otherwise
    the first Windows zone with the same offsets over the year of the event. None: no match, the caller uses UTC."""
    if not iana:
        return None
    if iana == "UTC":
        return "UTC"
    table = _tzwin().WIN_TO_IANA
    for win, name in table.items():
        if name == iana:
            return win
    z = zone(iana)
    if z is None:
        return None
    year = when.year if when is not None else datetime.now(UTC).year
    probes = [datetime(year, m, 15, 12, tzinfo=UTC) for m in (1, 4, 7, 10)]
    wanted = [p.astimezone(z).utcoffset() for p in probes]
    for win, name in table.items():
        other = zone(name)
        if other is not None and [p.astimezone(other).utcoffset() for p in probes] == wanted:
            return win
    return None


def parse_utc(text: str | None) -> datetime | None:
    """A timestamp from EWS or Graph: "2024-03-01T12:00:00Z", or no suffix and up to 7 decimals, meaning UTC."""
    if not text:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?$", text.strip())
    if not m:
        return None
    try:
        dt = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}" + ("+00:00" if m.group(3) in (None, "Z") else m.group(3)))
    except ValueError:
        return None
    return dt.astimezone(UTC)


def utc_text(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def all_day_date(dt: datetime, tz: str | None) -> date:
    """Day of an all-day boundary sent as an instant: midnight UTC is taken as is, anything else is midnight in the
    zone of the event (23:00Z the day before, for Paris in winter)."""
    z = zone(tz)
    if (dt.hour, dt.minute) == (0, 0) or z is None:
        return dt.date()
    return dt.astimezone(z).date()


def local_of(dt: datetime, tz: str | None) -> datetime:
    z = zone(tz)
    return dt.astimezone(z) if z is not None else dt.astimezone(UTC)


# ------------------------------------------------------------------ iCalendar: writing
def _ical():
    import icalendar
    return icalendar


def _address(person: Person, **params):
    from ..engine.event import _cal_address
    return _cal_address(person.name, person.email, **params)


def _rrule_dict(rec: Recurrence, start: datetime | date, tz: str | None) -> dict:
    rr: dict = {"FREQ": rec.freq}
    if rec.interval and rec.interval > 1:
        rr["INTERVAL"] = rec.interval
    if rec.by_day:
        rr["BYDAY"] = list(rec.by_day)
    if rec.set_pos is not None:
        rr["BYSETPOS"] = rec.set_pos
    if rec.month_day is not None:
        rr["BYMONTHDAY"] = rec.month_day
    if rec.month is not None:
        rr["BYMONTH"] = rec.month
    if rec.week_start:
        rr["WKST"] = rec.week_start
    if rec.count:
        rr["COUNT"] = rec.count
    elif rec.until is not None:
        if isinstance(start, datetime):
            local = local_of(start, tz)
            rr["UNTIL"] = datetime.combine(rec.until, local.time(), local.tzinfo).astimezone(UTC)
        else:
            rr["UNTIL"] = rec.until
    return rr


def _fill_event(ev, e: Event, master: Event | None = None) -> None:
    def when(value):
        return local_of(value, e.tz or (master.tz if master else None)) if isinstance(value, datetime) else value
    stamp = e.modified or e.created or (master.modified if master else None) or datetime(2000, 1, 1, tzinfo=UTC)
    ev.add("uid", (master or e).uid)
    ev.add("dtstamp", stamp.astimezone(UTC))
    if e.created:
        ev.add("created", e.created.astimezone(UTC))
    if e.modified:
        ev.add("last-modified", e.modified.astimezone(UTC))
    if e.recurrence_id is not None:
        ev.add("recurrence-id", when(e.recurrence_id))
    ev.add("dtstart", when(e.start))
    ev.add("dtend", when(e.end))
    ev.add("summary", e.subject or "")
    if e.location:
        ev.add("location", e.location)
    if e.body.strip():
        ev.add("description", e.body.strip())
    if e.categories:
        ev.add("categories", list(e.categories))
    ev.add("class", e.sensitivity)
    ev.add("transp", "TRANSPARENT" if e.free_busy == "FREE" else "OPAQUE")
    ev.add("x-microsoft-cdo-busystatus", e.free_busy)
    ev.add("status", "CANCELLED" if e.cancelled else "CONFIRMED")
    if e.recurrence is not None and master is None:
        ev.add("rrule", _rrule_dict(e.recurrence, e.start, e.tz))
    if e.exdates and master is None:
        ev.add("exdate", [when(d) for d in e.exdates])
    if e.organizer is not None and e.attendees:
        ev.add("organizer", _address(e.organizer, schedule_agent="CLIENT"))
    # SCHEDULE-AGENT=CLIENT: the DAV server must not send invitations for an event being migrated
    for a in e.attendees:
        params = {"partstat": a.partstat, "schedule_agent": "CLIENT"}
        if a.role == "RES":
            params.update(cutype="RESOURCE", role="NON-PARTICIPANT")
        else:
            params.update(cutype="INDIVIDUAL", role="OPT-PARTICIPANT" if a.role == "OPT" else "REQ-PARTICIPANT")
        ev.add("attendee", _address(a, **params))
    if e.reminder_minutes is not None:
        ical = _ical()
        al = ical.Alarm()
        al.add("action", "DISPLAY")
        al.add("description", e.subject or "Reminder")
        al.add("trigger", timedelta(minutes=-e.reminder_minutes))
        ev.add_component(al)


def event_to_ics(e: Event) -> bytes:
    ical = _ical()
    cal = ical.Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    if e.all_day and e.tz:  # a date has no zone: this says which one its day belongs to
        cal.add("x-wr-timezone", e.tz)
    ev = ical.Event()
    _fill_event(ev, e)
    cal.add_component(ev)
    for o in e.overrides:
        ov = ical.Event()
        _fill_event(ov, o, e)
        cal.add_component(ov)
    try:
        cal.add_missing_timezones()
    except Exception as ex:  # noqa: BLE001 - the zone is still named by its TZID
        e.warnings.append(f"vtimezone-failed:{ex}")
    return cal.to_ical()


def task_to_ics(t: Task) -> bytes:
    ical = _ical()
    cal = ical.Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    todo = ical.Todo()
    stamp = t.modified or t.created or datetime(2000, 1, 1, tzinfo=UTC)
    todo.add("uid", t.uid)
    todo.add("dtstamp", stamp.astimezone(UTC))
    if t.created:
        todo.add("created", t.created.astimezone(UTC))
    if t.modified:
        todo.add("last-modified", t.modified.astimezone(UTC))
    todo.add("summary", t.subject or "")
    if t.body.strip():
        todo.add("description", t.body.strip())
    todo.add("status", t.status)
    todo.add("priority", {"high": 1, "low": 9}.get(t.priority, 5))
    if t.percent is not None:
        todo.add("percent-complete", t.percent)
    if t.start is not None:
        todo.add("dtstart", t.start)
    if t.due is not None:
        todo.add("due", t.due)
    if t.completed is not None:
        todo.add("completed", t.completed.astimezone(UTC))
    if t.categories:
        todo.add("categories", list(t.categories))
    if t.recurrence is not None:
        anchor = t.start if t.start is not None else t.due
        if anchor is None:
            t.warnings.append("task-recurrence-without-date")
        else:
            todo.add("rrule", _rrule_dict(t.recurrence, anchor, None))
    cal.add_component(todo)
    return cal.to_ical()


# ------------------------------------------------------------------ iCalendar: reading
def _dt_of(comp, name: str):
    prop = comp.get(name)
    return getattr(prop, "dt", None) if prop is not None else None


def _tz_name(prop) -> str | None:
    value = getattr(prop, "dt", None)
    key = getattr(getattr(value, "tzinfo", None), "key", None)
    if key and zone(key) is not None:
        return key
    params = getattr(prop, "params", None)
    tzid = params.get("TZID") if params is not None else None
    if tzid:
        return iana_of(str(tzid))
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() == timedelta(0):
        return "UTC"
    return None


def _aware(value, tz: str | None, warnings: list[str]):
    """A datetime in UTC, a date as is. A floating time is read in the zone of the event, otherwise as UTC."""
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        z = zone(tz)
        if z is None:
            warnings.append("floating-time-read-as-utc")
        value = value.replace(tzinfo=z or UTC)
    return value.astimezone(UTC)


def _list_of(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _categories(comp) -> list[str]:
    out: list[str] = []
    for c in _list_of(comp.get("categories")):
        cats = getattr(c, "cats", None)
        out.extend(str(x) for x in cats) if cats is not None else out.append(str(c))
    return out


def _dates_of(comp, name: str) -> list:
    out = []
    for block in _list_of(comp.get(name)):
        dts = getattr(block, "dts", None)
        for d in dts if dts is not None else [block]:
            value = getattr(d, "dt", None)
            if value is not None:
                out.append(value)
    return out


def _first(rr, key):
    v = rr.get(key)
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v


def recurrence_from_rrule(rr, tz: str | None, warnings: list[str]) -> Recurrence | None:
    """A Recurrence from an RRULE, None with a warning if Exchange's recurrence model cannot hold it."""
    if rr is None:
        return None
    if isinstance(rr, list):
        if len(rr) > 1:
            warnings.append("recurrence-unsupported:several RRULE")
            return None
        rr = rr[0]
    freq = str(_first(rr, "FREQ") or "").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        warnings.append(f"recurrence-unsupported:FREQ={freq}")
        return None
    for key in ("BYHOUR", "BYMINUTE", "BYSECOND", "BYWEEKNO", "BYYEARDAY"):
        if rr.get(key):
            warnings.append(f"recurrence-unsupported:{key}")
            return None
    rec = Recurrence(freq=freq, interval=int(_first(rr, "INTERVAL") or 1))
    days = rr.get("BYDAY") or []
    days = days if isinstance(days, (list, tuple)) else [days]
    for d in days:
        m = re.match(r"^([+-]?\d+)?([A-Za-z]{2})$", str(d))
        if not m:
            warnings.append(f"recurrence-unsupported:BYDAY={d}")
            return None
        rec.by_day.append(m.group(2).upper())
        if m.group(1):
            rec.set_pos = int(m.group(1))
    if _first(rr, "BYSETPOS") is not None:
        rec.set_pos = int(_first(rr, "BYSETPOS"))
    if rec.set_pos is not None and rec.set_pos not in (1, 2, 3, 4, -1):
        warnings.append(f"recurrence-unsupported:position {rec.set_pos}")
        return None
    month_days = rr.get("BYMONTHDAY") or []
    month_days = month_days if isinstance(month_days, (list, tuple)) else [month_days]
    if len(month_days) > 1:
        warnings.append("recurrence-unsupported:several BYMONTHDAY")
        return None
    if month_days:
        rec.month_day = int(month_days[0])
        if rec.month_day < 1:
            warnings.append("recurrence-unsupported:BYMONTHDAY from the end")
            return None
    months = rr.get("BYMONTH") or []
    months = months if isinstance(months, (list, tuple)) else [months]
    if len(months) > 1:
        warnings.append("recurrence-unsupported:several BYMONTH")
        return None
    if months:
        rec.month = int(months[0])
    if _first(rr, "WKST"):
        rec.week_start = str(_first(rr, "WKST")).upper()
    if _first(rr, "COUNT"):
        rec.count = int(_first(rr, "COUNT"))
    until = _first(rr, "UNTIL")
    if until is not None:
        if isinstance(until, datetime):
            until = until.replace(tzinfo=until.tzinfo or UTC)
            rec.until = local_of(until, tz).date()
        else:
            rec.until = until
    return rec


def _attendee_of(value, warnings: list[str]) -> Person | None:
    text = str(value)
    addr = text[7:] if text.lower().startswith("mailto:") else text
    params = getattr(value, "params", {}) or {}
    name = str(params.get("CN") or "")
    if "@" not in addr:
        warnings.append(f"attendee-without-address:{name or addr}"[:200])
        return None
    role = "OPT" if str(params.get("ROLE") or "").upper() == "OPT-PARTICIPANT" else "REQ"
    if str(params.get("CUTYPE") or "").upper() in ("RESOURCE", "ROOM"):
        role = "RES"
    return Person(addr, name, role, str(params.get("PARTSTAT") or "NEEDS-ACTION").upper())


def _event_of(comp, warnings: list[str], fallback_tz: str | None = None) -> Event:
    prop = comp.get("dtstart")
    start_raw = getattr(prop, "dt", None)
    if start_raw is None:
        raise ConnectorError("event without DTSTART", ConnectorError.REJECTED)
    tz = _tz_name(prop) or fallback_tz
    start = _aware(start_raw, tz, warnings)
    end_raw = _dt_of(comp, "dtend")
    if end_raw is None:
        dur = _dt_of(comp, "duration")
        if isinstance(start, datetime):
            end = start + (dur if isinstance(dur, timedelta) else timedelta(0))
        else:
            end = start + (dur if isinstance(dur, timedelta) and dur.days > 0 else timedelta(days=1))
    else:
        end = _aware(end_raw, tz, warnings)
    if isinstance(start, datetime) != isinstance(end, datetime):
        raise ConnectorError("event mixing a date and a time", ConnectorError.REJECTED)
    e = Event(uid=str(comp.get("uid") or ""), start=start, end=end, subject=str(comp.get("summary") or ""),
              body=str(comp.get("description") or ""), location=str(comp.get("location") or ""), tz=tz,
              warnings=warnings)
    busy = str(comp.get("x-microsoft-cdo-busystatus") or "").upper()
    if busy in ("FREE", "TENTATIVE", "BUSY", "OOF", "WORKINGELSEWHERE"):
        e.free_busy = busy
    elif str(comp.get("transp") or "").upper() == "TRANSPARENT":
        e.free_busy = "FREE"
    sens = str(comp.get("class") or "").upper()
    if sens in ("PRIVATE", "CONFIDENTIAL"):
        e.sensitivity = sens
    e.cancelled = str(comp.get("status") or "").upper() == "CANCELLED"
    e.categories = _categories(comp)
    created, modified = _dt_of(comp, "created"), _dt_of(comp, "last-modified")
    e.created = _aware(created, "UTC", warnings) if isinstance(created, datetime) else None
    e.modified = _aware(modified, "UTC", warnings) if isinstance(modified, datetime) else None
    if comp.get("organizer") is not None:
        e.organizer = _attendee_of(comp.get("organizer"), warnings)
    for a in _list_of(comp.get("attendee")):
        person = _attendee_of(a, warnings)
        if person is not None:
            e.attendees.append(person)
    for alarm in comp.walk("VALARM"):
        trigger = _dt_of(alarm, "trigger")
        if isinstance(trigger, timedelta) and trigger <= timedelta(0):
            e.reminder_minutes = int(-trigger.total_seconds() // 60)
            break
    return e


def _parse_calendar(raw: bytes):
    try:
        return _ical().Calendar.from_ical(raw)
    except (ValueError, IndexError, KeyError) as ex:
        raise ConnectorError(f"unreadable iCalendar: {ex}", ConnectorError.REJECTED) from ex


def ics_to_event(raw: bytes) -> Event:
    cal = _parse_calendar(raw)
    comps = cal.walk("VEVENT")
    if not comps:
        raise ConnectorError("no VEVENT in this iCalendar", ConnectorError.REJECTED)
    masters = [c for c in comps if c.get("recurrence-id") is None]
    warnings: list[str] = []
    master = masters[0] if masters else comps[0]
    e = _event_of(master, warnings)
    if e.tz is None and e.all_day:
        named = str(cal.get("x-wr-timezone") or "")
        e.tz = named if zone(named) is not None else None
    if not masters:
        warnings.append("override-without-master")
    if len(masters) > 1:
        warnings.append(f"several-events-in-one-object:{len(masters)}")
    e.recurrence = recurrence_from_rrule(master.get("rrule"), e.tz, warnings)
    if e.recurrence is not None:
        e.exdates = [_aware(d, e.tz, warnings) for d in _dates_of(master, "exdate")]
        for c in comps:
            if c is master or c.get("recurrence-id") is None:
                continue
            o = _event_of(c, warnings, e.tz)
            rid = c.get("recurrence-id")
            o.recurrence_id = _aware(rid.dt, _tz_name(rid) or e.tz, warnings)
            e.overrides.append(o)
    if not e.uid:
        raise ConnectorError("event without UID", ConnectorError.REJECTED)
    return e


def ics_to_task(raw: bytes) -> Task:
    cal = _parse_calendar(raw)
    comps = cal.walk("VTODO")
    if not comps:
        raise ConnectorError("no VTODO in this iCalendar", ConnectorError.REJECTED)
    c = comps[0]
    warnings: list[str] = []
    t = Task(uid=str(c.get("uid") or ""), subject=str(c.get("summary") or ""), body=str(c.get("description") or ""),
             warnings=warnings)
    status = str(c.get("status") or "").upper()
    if status in ("NEEDS-ACTION", "IN-PROCESS", "COMPLETED", "CANCELLED"):
        t.status = status
    try:
        prio = int(str(c.get("priority") or 0))
    except ValueError:
        prio = 0
    t.priority = "high" if 1 <= prio <= 4 else "low" if prio >= 6 else "normal"
    if c.get("percent-complete") is not None:
        t.percent = int(str(c.get("percent-complete")))
    for name, attr in (("dtstart", "start"), ("due", "due")):
        prop = c.get(name)
        if prop is not None:
            setattr(t, attr, _aware(prop.dt, _tz_name(prop), warnings))
    done = _dt_of(c, "completed")
    t.completed = _aware(done, "UTC", warnings) if isinstance(done, datetime) else None
    t.categories = _categories(c)
    created, modified = _dt_of(c, "created"), _dt_of(c, "last-modified")
    t.created = _aware(created, "UTC", warnings) if isinstance(created, datetime) else None
    t.modified = _aware(modified, "UTC", warnings) if isinstance(modified, datetime) else None
    t.recurrence = recurrence_from_rrule(c.get("rrule"), None, warnings)
    if not t.uid:
        raise ConnectorError("task without UID", ConnectorError.REJECTED)
    return t


# ------------------------------------------------------------------ vCard
# kind of a phone number <-> TEL types. The table of the engine (contact.py) for what a PST holds, read back here.
PHONE_TYPES = {
    "business": "WORK,VOICE", "business2": "WORK,VOICE", "home": "HOME,VOICE", "home2": "HOME,VOICE",
    "mobile": "CELL,VOICE", "business_fax": "WORK,FAX", "home_fax": "HOME,FAX", "other_fax": "FAX", "pager": "PAGER",
    "car": "CAR", "primary": "PREF,VOICE", "other": "VOICE", "assistant": "X-ASSISTANT,VOICE",
    "company": "WORK,X-COMPANY,VOICE", "isdn": "ISDN", "radio": "VOICE", "callback": "VOICE", "telex": "VOICE",
}
_ADR_TYPE = {"work": "WORK", "home": "HOME", "other": "POSTAL"}


def contact_to_vcard(c: Contact) -> bytes:
    from ..engine.contact import _esc, _fold
    surname, given = c.surname, c.given
    fn = c.full_name or " ".join(x for x in (c.prefix, given, c.middle, surname, c.suffix) if x) or c.company or (
        c.emails[0] if c.emails else "") or "Contact"
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"PRODID:{PRODID}", "UID:" + _esc(c.uid), "FN:" + _esc(fn.strip()),
             "N:" + ";".join(_esc(x) for x in (surname, given, c.middle, c.prefix, c.suffix))]
    if c.nickname:
        lines.append("NICKNAME:" + _esc(c.nickname))
    if c.company or c.department:
        lines.append("ORG:" + _esc(c.company) + (";" + _esc(c.department) if c.department else ""))
    if c.title:
        lines.append("TITLE:" + _esc(c.title))
    if c.profession:
        lines.append("ROLE:" + _esc(c.profession))
    for i, mail in enumerate(c.emails):
        lines.append("EMAIL;TYPE=INTERNET%s:%s" % (",PREF" if i == 0 else "", _esc(mail)))
    seen = set()
    for kind, number in c.phones:
        typ = PHONE_TYPES.get(kind, "VOICE")
        if number and (number, typ) not in seen:
            seen.add((number, typ))
            lines.append(f"TEL;TYPE={typ}:{_esc(number)}")
    for a in c.addresses:
        parts = [a.get(k) or "" for k in ("pobox", "ext", "street", "city", "state", "postal", "country")]
        if any(parts):
            lines.append(f"ADR;TYPE={_ADR_TYPE.get(a.get('kind') or '', 'POSTAL')}:" + ";".join(_esc(x) for x in parts))
    if c.birthday:
        lines.append("BDAY;VALUE=date:" + c.birthday.isoformat())
    if c.anniversary:
        lines.append("X-ANNIVERSARY:" + c.anniversary.isoformat())
    for u in c.urls:
        lines.append("URL:" + _esc(u))
    if c.im:
        lines.append("X-MS-IMADDRESS:" + _esc(c.im))
    if c.spouse:
        lines.append("X-SPOUSE:" + _esc(c.spouse))
    if c.assistant:
        lines.append("X-ASSISTANT:" + _esc(c.assistant))
    if c.categories:
        lines.append("CATEGORIES:" + ",".join(_esc(x) for x in c.categories))
    if c.note.strip():
        lines.append("NOTE:" + _esc(c.note.strip()))
    if c.modified:
        lines.append("REV:" + utc_text(c.modified))
    lines.append("END:VCARD")
    return "".join(_fold(x) for x in lines).encode("utf-8")


def _unfold_lines(text: str) -> list[str]:
    out: list[str] = []
    for line in re.split(r"\r\n|\n|\r", text):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        elif line:
            out.append(line)
    return out


def _unesc(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), s, flags=re.S)


def _split_value(s: str, sep: str) -> list[str]:
    return [_unesc(p) for p in re.split(r"(?<!\\)" + re.escape(sep), s)]


def _vcard_date(value: str) -> date | None:
    value = value.strip()
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", value)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _phone_kind(types: set[str], taken: set[str]) -> str:
    def pick(*names: str) -> str:
        for n in names:
            if n not in taken:
                return n
        return "other"
    if "CELL" in types or "MOBILE" in types:
        return pick("mobile", "other")
    if "FAX" in types:
        return pick("business_fax" if "WORK" in types else "home_fax" if "HOME" in types else "other_fax", "other_fax")
    if "PAGER" in types:
        return pick("pager")
    if "CAR" in types:
        return pick("car")
    if "ISDN" in types:
        return pick("isdn")
    if "X-ASSISTANT" in types:
        return pick("assistant")
    if "X-COMPANY" in types:
        return pick("company")
    if "WORK" in types:
        return pick("business", "business2")
    if "HOME" in types:
        return pick("home", "home2")
    if "PREF" in types:
        return pick("primary")
    return pick("other")


def vcard_to_contact(raw: bytes) -> Contact:
    """Reads vCard 2.1, 3.0 and 4.0 well enough for these fields. Grouped properties ("item1.TEL") are read too."""
    text = raw.decode("utf-8", "replace")
    if "BEGIN:VCARD" not in text.upper():
        raise ConnectorError("not a vCard", ConnectorError.REJECTED)
    c = Contact(uid="")
    taken: set[str] = set()
    photo = False
    for line in _unfold_lines(text):
        m = re.match(r"^([^:;]+)((?:;[^:]*)?):(.*)$", line, re.S)
        if not m:
            continue
        name = m.group(1).split(".")[-1].upper()
        value = m.group(3)
        types: set[str] = set()
        for p in m.group(2).split(";"):
            if not p:
                continue
            key, _, val = p.partition("=")
            if val and key.upper() == "TYPE":
                types.update(v.strip('"').upper() for v in val.split(","))
            elif not val:
                types.add(key.upper())
        if name == "UID":
            c.uid = _unesc(value)
        elif name == "FN":
            c.full_name = _unesc(value)
        elif name == "N":
            parts = (_split_value(value, ";") + [""] * 5)[:5]
            c.surname, c.given, c.middle, c.prefix, c.suffix = parts
        elif name == "NICKNAME":
            c.nickname = _unesc(value)
        elif name == "ORG":
            parts = _split_value(value, ";")
            c.company = parts[0]
            c.department = parts[1] if len(parts) > 1 else ""
        elif name == "TITLE":
            c.title = _unesc(value)
        elif name == "ROLE":
            c.profession = _unesc(value)
        elif name == "EMAIL":
            addr = _unesc(value).strip()
            if addr and addr.lower() not in [e.lower() for e in c.emails]:
                c.emails.append(addr)
        elif name == "TEL":
            number = _unesc(value).strip()
            if number.lower().startswith("tel:"):
                number = number[4:]
            if number:
                kind = _phone_kind(types, taken)
                taken.add(kind)
                c.phones.append((kind, number))
        elif name == "ADR":
            parts = (_split_value(value, ";") + [""] * 7)[:7]
            kind = "work" if "WORK" in types else "home" if "HOME" in types else "other"
            c.addresses.append({"kind": kind, "pobox": parts[0], "ext": parts[1], "street": parts[2], "city": parts[3],
                                "state": parts[4], "postal": parts[5], "country": parts[6]})
        elif name == "BDAY":
            c.birthday = _vcard_date(value)
        elif name in ("ANNIVERSARY", "X-ANNIVERSARY"):
            c.anniversary = _vcard_date(value)
        elif name == "URL":
            c.urls.append(_unesc(value))
        elif name == "X-MS-IMADDRESS":
            c.im = _unesc(value)
        elif name == "X-SPOUSE":
            c.spouse = _unesc(value)
        elif name == "X-ASSISTANT":
            c.assistant = _unesc(value)
        elif name == "CATEGORIES":
            c.categories = [x for x in _split_value(value, ",") if x]
        elif name == "NOTE":
            c.note = _unesc(value)
        elif name == "PHOTO":
            photo = True
    if photo:
        c.warnings.append("photo-not-copied")
    return c


def date_at_midnight(d: date) -> datetime:
    return datetime.combine(d, time(0), UTC)
