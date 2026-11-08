"""Exchange Online through Microsoft Graph, as a source and as a target, app-only with client credentials: one Entra
application reaches every mailbox of a tenant. Tested against a simulated server only, see docs/exchange.md.

EWS is not offered for Exchange Online: blocked by default since 2026-10-01, removal planned 2027-04-01."""
from __future__ import annotations

import base64
import hashlib
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Iterator
from urllib.parse import quote

import httpx

from . import exchange_base as xb
from . import oauth, pim, registry
from .base import (CALENDAR, CAP_FLAGS, CAP_FOLDERS, CAP_INCREMENTAL, CAP_INTERNAL_DATE, CAP_PARALLEL, CAP_RULES,
                   CONTACTS, MAIL, TASKS, Changes, ConnectorError, FetchedItem, FolderPlan, OutgoingItem, Source,
                   SourceFolder, SourceItem, Target, TargetSession, subject_of)

# global service, US Government L4, US Government L5 (DoD), China operated by 21Vianet
CLOUDS = {"global": ("https://login.microsoftonline.com", "https://graph.microsoft.com"),
          "usgov": ("https://login.microsoftonline.us", "https://graph.microsoft.us"),
          "usgov-dod": ("https://login.microsoftonline.us", "https://dod-graph.microsoft.us"),
          "china": ("https://login.partner.microsoftonline.cn", "https://microsoftgraph.chinacloudapi.cn")}
MAX_CONCURRENT = 4          # documented limit per app and mailbox: more only earns 429
RETRIES = 6
RETRY_DEFAULT = 10.0
RETRY_MAX_WAIT = 300.0
PAGE = 100
PROP_IMPORT = f"String {{{xb.IMPORT_GUID}}} Name {xb.IMPORT_PROP}"
# the import id header, as Exchange names an x-header of a MIME message when it stores it as a property
PROP_HEADER = "String {00020386-0000-0000-C000-000000000046} Name X-PST-Import-Id"
PROP_SIZE, PROP_VERB, PROP_DELIVERY = "Integer 0x0E08", "Integer 0x1081", "SystemTime 0x0E06"
MESSAGE_FIELDS = "id,internetMessageId,receivedDateTime,isRead,isDraft,flag"
MESSAGE_EXPAND = f"singleValueExtendedProperties($filter=id eq '{PROP_SIZE}' or id eq '{PROP_VERB}')"
EVENT_FIELDS = ("id,iCalUId,type,subject,body,start,end,isAllDay,location,organizer,attendees,showAs,sensitivity,"
                "categories,isReminderOn,reminderMinutesBeforeStart,recurrence,originalStart,originalStartTimeZone,"
                "isCancelled,createdDateTime,lastModifiedDateTime,hasAttachments")
WELL_KNOWN_SPECIAL = {"inbox": "inbox", "sentitems": "sent", "drafts": "drafts", "deleteditems": "trash",
                      "junkemail": "junk"}
WELL_KNOWN_SKIPPED = ("outbox", "syncissues", "conflicts", "localfailures", "serverfailures")
PARTSTAT = {"accepted": "ACCEPTED", "tentativelyAccepted": "TENTATIVE", "declined": "DECLINED"}
SHOW_AS = {"free": "FREE", "tentative": "TENTATIVE", "busy": "BUSY", "oof": "OOF", "workingElsewhere": "WORKINGELSEWHERE"}
GRAPH_SHOW_AS = {v: k for k, v in SHOW_AS.items()}
SENS = {"private": "PRIVATE", "confidential": "CONFIDENTIAL", "personal": "PRIVATE"}
GRAPH_SENS = {"PUBLIC": "normal", "PRIVATE": "private", "CONFIDENTIAL": "confidential"}
TASK_STATUS = {"notStarted": "NEEDS-ACTION", "inProgress": "IN-PROCESS", "completed": "COMPLETED",
               "waitingOnOthers": "IN-PROCESS", "deferred": "CANCELLED"}
GRAPH_TASK_STATUS = {"NEEDS-ACTION": "notStarted", "IN-PROCESS": "inProgress", "COMPLETED": "completed",
                     "CANCELLED": "deferred"}
DAY_NAMES = {"SU": "sunday", "MO": "monday", "TU": "tuesday", "WE": "wednesday", "TH": "thursday", "FR": "friday",
             "SA": "saturday"}
DAY_CODES = {v: k for k, v in DAY_NAMES.items()}
INDEX_NAMES = {1: "first", 2: "second", 3: "third", 4: "fourth", -1: "last"}
INDEX_CODES = {v: k for k, v in INDEX_NAMES.items()}
PERMISSIONS = "Mail.ReadWrite, Calendars.ReadWrite, Contacts.ReadWrite, Tasks.ReadWrite.All, MailboxSettings.Read"

_sleep = time.sleep  # replaced in the tests


class GraphError(ConnectorError):
    def __init__(self, msg: str, kind: str, status: int, code: str = "", text: str = ""):
        super().__init__(msg, kind)
        self.status = status
        self.code = code
        self.text = text  # the message of Graph's error body


def _odata(value: str) -> str:
    return value.replace("'", "''")


class GraphClient:
    """One thread. The token source and the limit on concurrent requests are shared by the sessions of a target."""

    def __init__(self, config: dict, secrets: dict, token: oauth.OAuthToken, gate: threading.BoundedSemaphore,
                 tenant: bool = False):
        self.root = CLOUDS.get(config.get("cloud") or "global", CLOUDS["global"])[1]
        self.mailbox = str(config.get("mailbox") or "").strip()
        if tenant:  # directory mode: paths are tenant-wide, no /users/{mailbox} prefix
            self.mailbox = ""
            self.user = ""
        else:
            if not self.mailbox:
                raise ConnectorError("'mailbox' is required", ConnectorError.CONFIG)
            self.user = f"/users/{quote(self.mailbox, safe='@')}"
        self.token = token
        self.gate = gate
        self.http = oauth.http_client(True, 300.0)

    def close(self) -> None:
        self.http.close()

    def url(self, path: str, version: str = "v1.0") -> str:
        return f"{self.root}/{version}{self.user}{path}"

    def request(self, method: str, url: str, *, params: dict | None = None, json_body=None, content: bytes | None = None,
                headers: dict | None = None, connecting: bool = False) -> httpx.Response:
        """Waits and tries again on 429, 503 and 504, as long as Retry-After says. Raises GraphError otherwise."""
        if not url.startswith(self.root + "/"):  # a next link from elsewhere must not receive the token
            raise ConnectorError(f"Graph: unexpected link to {url[:80]}", ConnectorError.REJECTED)
        attempt = 0
        refreshed = False
        while True:
            attempt += 1
            tok = self.token.token()
            all_headers = [("Authorization", f"Bearer {tok}"), ("Accept", "application/json"),
                           ("Prefer", 'IdType="ImmutableId"'), ("Prefer", 'outlook.timezone="UTC"'),
                           ("Prefer", 'outlook.body-content-type="text"')]
            all_headers.extend((headers or {}).items())
            try:
                with self.gate:
                    if json_body is not None:
                        r = self.http.request(method, url, params=params, json=json_body, headers=all_headers)
                    else:
                        r = self.http.request(method, url, params=params, content=content, headers=all_headers)
            except httpx.HTTPError as e:
                kind = ConnectorError.UNREACHABLE if connecting else ConnectorError.TRANSIENT
                raise ConnectorError(f"Graph: {type(e).__name__}: {e}", kind) from e
            if r.status_code < 300:
                return r
            if r.status_code == 401 and not refreshed:
                refreshed = True
                self.token.invalidate(tok)
                continue
            if r.status_code in (429, 503, 504):
                if attempt > RETRIES:
                    raise GraphError(f"Graph: throttled (HTTP {r.status_code}) after {attempt} attempts",
                                     ConnectorError.TRANSIENT, r.status_code)
                _sleep(min(self._retry_after(r, attempt), RETRY_MAX_WAIT))
                continue
            raise self._error(r, connecting)

    @staticmethod
    def _retry_after(r: httpx.Response, attempt: int) -> float:
        try:
            return float(r.headers.get("retry-after", ""))
        except ValueError:
            return RETRY_DEFAULT * (2 ** (attempt - 1)) * random.uniform(0.8, 1.2)  # noqa: S311 - not security

    def _error(self, r: httpx.Response, connecting: bool) -> GraphError:
        code, text = "", ""
        try:
            err = r.json().get("error") or {}
            code, text = str(err.get("code") or ""), str(err.get("message") or "")
        except (ValueError, AttributeError):
            pass
        msg = f"Graph HTTP {r.status_code} {code}: {text}"[:1000]
        st = r.status_code
        if code in ("ErrorInvalidUser", "MailboxNotEnabledForRESTAPI", "ResourceNotFound") and "mailFolders/inbox" in str(r.url):
            kind = ConnectorError.CONFIG
        elif st in (401, 403):
            msg += f" (application permissions needed, with admin consent: {PERMISSIONS})"
            kind = ConnectorError.AUTH
        elif code == "ErrorQuotaExceeded" or st == 507:
            kind = ConnectorError.QUOTA
        elif st == 413:
            msg = f"Graph HTTP 413: message too large for this API. {text}"[:1000]
            kind = ConnectorError.REJECTED
        elif st >= 500:
            kind = ConnectorError.UNREACHABLE if connecting else ConnectorError.TRANSIENT
        else:
            kind = ConnectorError.REJECTED
        return GraphError(msg, kind, st, code, text)

    def get_json(self, path_or_url: str, params: dict | None = None, connecting: bool = False) -> dict:
        url = path_or_url if path_or_url.startswith("https://") else self.url(path_or_url)
        r = self.request("GET", url, params=params, connecting=connecting)
        try:
            return r.json()
        except ValueError as e:
            raise ConnectorError(f"Graph: invalid JSON from {url[:80]}", ConnectorError.TRANSIENT) from e

    def pages(self, path_or_url: str, params: dict | None = None) -> Iterator[dict]:
        """Every page of a collection, following @odata.nextLink (it carries the parameters)."""
        body = self.get_json(path_or_url, params)
        while True:
            yield body
            link = body.get("@odata.nextLink")
            if not link:
                return
            body = self.get_json(link)

    def all(self, path_or_url: str, params: dict | None = None) -> Iterator[dict]:
        for page in self.pages(path_or_url, params):
            yield from page.get("value") or []


# ------------------------------------------------------------------ JSON <-> neutral model
def _instant(block: dict | None) -> datetime | None:
    """A dateTimeTimeZone. Asked in UTC (see `request`), other zones are honoured all the same."""
    if not block or not block.get("dateTime"):
        return None
    tz = pim.iana_of(block.get("timeZone"))
    text = str(block["dateTime"])
    if tz and tz != "UTC":
        naive = pim.parse_utc(text)
        if naive is None:
            return None
        return naive.replace(tzinfo=pim.zone(tz)).astimezone(pim.UTC)
    return pim.parse_utc(text)


def recurrence_from_json(rec: dict | None, warnings: list[str]) -> pim.Recurrence | None:
    if not rec:
        return None
    pat, rng = rec.get("pattern") or {}, rec.get("range") or {}
    typ = pat.get("type")
    out = pim.Recurrence(freq="DAILY", interval=int(pat.get("interval") or 1))
    days = [DAY_CODES[d] for d in pat.get("daysOfWeek") or [] if d in DAY_CODES]
    if typ == "daily":
        out.freq = "DAILY"
    elif typ == "weekly":
        out.freq = "WEEKLY"
        out.by_day = days
        out.week_start = DAY_CODES.get(pat.get("firstDayOfWeek") or "")
    elif typ in ("absoluteMonthly", "absoluteYearly"):
        out.freq = "MONTHLY" if typ == "absoluteMonthly" else "YEARLY"
        out.month_day = pat.get("dayOfMonth") or None
        out.month = (pat.get("month") or None) if typ == "absoluteYearly" else None
    elif typ in ("relativeMonthly", "relativeYearly"):
        out.freq = "MONTHLY" if typ == "relativeMonthly" else "YEARLY"
        out.by_day = days
        out.set_pos = INDEX_CODES.get(pat.get("index") or "first", 1)
        out.month = (pat.get("month") or None) if typ == "relativeYearly" else None
    else:
        warnings.append(f"recurrence-unsupported:{typ}")
        return None
    if rng.get("type") == "numbered":
        out.count = rng.get("numberOfOccurrences") or None
    elif rng.get("type") == "endDate" and rng.get("endDate"):
        try:
            out.until = date.fromisoformat(rng["endDate"][:10])
        except ValueError:
            warnings.append("recurrence-end-unreadable")
    return out


def recurrence_json(rec: pim.Recurrence, start: datetime | date, tz: str | None, win: str | None) -> dict:
    first = pim.local_of(start, tz).date() if isinstance(start, datetime) else start
    pat: dict = {"interval": max(1, rec.interval)}
    if rec.freq == "DAILY":
        pat["type"] = "daily"
    elif rec.freq == "WEEKLY":
        pat.update(type="weekly", daysOfWeek=[DAY_NAMES[d] for d in (rec.by_day or [pim.DAYS[(first.weekday() + 1) % 7]])])
        if rec.week_start in DAY_NAMES:
            pat["firstDayOfWeek"] = DAY_NAMES[rec.week_start]
    elif rec.freq in ("MONTHLY", "YEARLY"):
        word = "Monthly" if rec.freq == "MONTHLY" else "Yearly"
        if rec.freq == "YEARLY":
            pat["month"] = rec.month or first.month
        if rec.by_day:
            if rec.set_pos is None:
                raise ConnectorError("recurrence not supported by Graph: several days without a position",
                                     ConnectorError.REJECTED)
            pat.update(type=f"relative{word}", daysOfWeek=[DAY_NAMES[d] for d in rec.by_day], index=INDEX_NAMES[rec.set_pos])
        else:
            pat.update(type=f"absolute{word}", dayOfMonth=rec.month_day or first.day)
    else:
        raise ConnectorError(f"recurrence not supported by Graph: {rec.freq}", ConnectorError.REJECTED)
    rng: dict = {"startDate": first.isoformat(), "recurrenceTimeZone": win or "UTC"}
    if rec.count:
        rng.update(type="numbered", numberOfOccurrences=rec.count)
    elif rec.until is not None:
        rng.update(type="endDate", endDate=rec.until.isoformat())
    else:
        rng["type"] = "noEnd"
    return {"pattern": pat, "range": rng}


def _person(block: dict | None, warnings: list[str]) -> pim.Person | None:
    addr = ((block or {}).get("emailAddress") or {})
    email = str(addr.get("address") or "").strip()
    name = str(addr.get("name") or "")
    if "@" not in email:
        if name or email:
            warnings.append(f"attendee-without-address:{name or email}"[:200])
        return None
    return pim.Person(email, name)


def _body_text(obj: dict) -> str:
    return str((obj.get("body") or {}).get("content") or "")


def event_from_json(o: dict, warnings: list[str], tz: str | None = None) -> pim.Event:
    name = o.get("originalStartTimeZone")
    tz = pim.iana_of(name) or tz
    if tz is None:
        warnings.append(f"timezone-unknown-kept-as-utc:{name}"[:200])
    start, end = _instant(o.get("start")), _instant(o.get("end"))
    if start is None or end is None:
        raise ConnectorError("event without start or end", ConnectorError.REJECTED)
    begin: datetime | date = start
    finish: datetime | date = end
    if o.get("isAllDay"):
        begin, finish = pim.all_day_date(start, tz), pim.all_day_date(end, tz)
        if finish <= begin:
            finish = begin + timedelta(days=1)
    e = pim.Event(uid=str(o.get("iCalUId") or o.get("id") or ""), start=begin, end=finish, tz=tz,
                  subject=str(o.get("subject") or ""), body=_body_text(o),
                  location=str((o.get("location") or {}).get("displayName") or ""), warnings=warnings,
                  free_busy=SHOW_AS.get(o.get("showAs") or "", "BUSY"), sensitivity=SENS.get(o.get("sensitivity") or "", "PUBLIC"),
                  categories=list(o.get("categories") or []), cancelled=bool(o.get("isCancelled")),
                  created=pim.parse_utc(o.get("createdDateTime")), modified=pim.parse_utc(o.get("lastModifiedDateTime")))
    e.organizer = _person(o.get("organizer"), warnings)
    for a in o.get("attendees") or []:
        p = _person(a, warnings)
        if p is not None:
            p.role = {"optional": "OPT", "resource": "RES"}.get(a.get("type") or "", "REQ")
            p.partstat = PARTSTAT.get((a.get("status") or {}).get("response") or "", "NEEDS-ACTION")
            e.attendees.append(p)
    if o.get("isReminderOn"):
        e.reminder_minutes = o.get("reminderMinutesBeforeStart")
    if o.get("hasAttachments"):
        warnings.append("attachments-not-copied")
    e.recurrence = recurrence_from_json(o.get("recurrence"), warnings)
    return e


def _occurrence_date(occurrence_id: str) -> date | None:
    """OID.{seriesMasterId}.{occurrence-start-date}, the date being in the time zone of the recurrence."""
    tail = str(occurrence_id).rsplit(".", 1)[-1]
    try:
        return date.fromisoformat(tail[:10])
    except ValueError:
        return None


def _address(block: dict | None, kind: str) -> dict | None:
    if not block or not any(block.values()):
        return None
    return {"kind": kind, "street": block.get("street") or "", "city": block.get("city") or "",
            "state": block.get("state") or "", "postal": block.get("postalCode") or "",
            "country": block.get("countryOrRegion") or ""}


def contact_from_json(o: dict, uid: str) -> pim.Contact:
    c = pim.Contact(uid=uid, full_name=str(o.get("displayName") or ""), surname=str(o.get("surname") or ""),
                    given=str(o.get("givenName") or ""), middle=str(o.get("middleName") or ""),
                    prefix=str(o.get("title") or ""), suffix=str(o.get("generation") or ""),
                    nickname=str(o.get("nickName") or ""), company=str(o.get("companyName") or ""),
                    department=str(o.get("department") or ""), title=str(o.get("jobTitle") or ""),
                    profession=str(o.get("profession") or ""), note=str(o.get("personalNotes") or ""),
                    spouse=str(o.get("spouseName") or ""), assistant=str(o.get("assistantName") or ""),
                    categories=list(o.get("categories") or []), modified=pim.parse_utc(o.get("lastModifiedDateTime")))
    c.emails = [str(m["address"]) for m in o.get("emailAddresses") or [] if m.get("address")]
    for i, n in enumerate(o.get("businessPhones") or []):
        c.phones.append(("business" if i == 0 else "business2", n))
    for i, n in enumerate(o.get("homePhones") or []):
        c.phones.append(("home" if i == 0 else "home2", n))
    if o.get("mobilePhone"):
        c.phones.append(("mobile", o["mobilePhone"]))
    for field, kind in (("businessAddress", "work"), ("homeAddress", "home"), ("otherAddress", "other")):
        a = _address(o.get(field), kind)
        if a:
            c.addresses.append(a)
    if o.get("imAddresses"):
        c.im = str(o["imAddresses"][0])
    if o.get("birthday"):
        when = pim.parse_utc(o["birthday"])
        c.birthday = when.date() if when else None
    if o.get("businessHomePage"):
        c.urls.append(str(o["businessHomePage"]))
    return c


def contact_json(c: pim.Contact, import_id: str) -> dict:
    """The contact resource has no field for fax, pager and the other kinds of phone: those are put in the notes."""
    out: dict = {"singleValueExtendedProperties": [{"id": PROP_IMPORT, "value": import_id}]}
    for key, value in (("displayName", c.full_name), ("givenName", c.given), ("surname", c.surname), ("middleName", c.middle),
                       ("title", c.prefix), ("generation", c.suffix), ("nickName", c.nickname), ("companyName", c.company),
                       ("department", c.department), ("jobTitle", c.title), ("profession", c.profession),
                       ("spouseName", c.spouse), ("assistantName", c.assistant)):
        if value:
            out[key] = value
    if c.emails:
        out["emailAddresses"] = [{"address": m, "name": c.full_name or m} for m in c.emails[:3]]
    business = [n for k, n in c.phones if k in ("business", "business2")][:2]
    home = [n for k, n in c.phones if k in ("home", "home2")][:2]
    mobile = [n for k, n in c.phones if k == "mobile"]
    if business:
        out["businessPhones"] = business
    if home:
        out["homePhones"] = home
    if mobile:
        out["mobilePhone"] = mobile[0]
    extra = [f"{k}: {n}" for k, n in c.phones if k not in ("business", "business2", "home", "home2", "mobile")]
    extra += [f"email: {m}" for m in c.emails[3:]]
    note = c.note.strip()
    if extra:
        note = (note + "\n\n" if note else "") + "\n".join(extra)
    if note:
        out["personalNotes"] = note
    for a in c.addresses:
        key = {"work": "businessAddress", "home": "homeAddress", "other": "otherAddress"}.get(a.get("kind") or "")
        if key and key not in out:
            out[key] = {"street": a.get("street") or "", "city": a.get("city") or "", "state": a.get("state") or "",
                        "postalCode": a.get("postal") or "", "countryOrRegion": a.get("country") or ""}
    if c.im:
        out["imAddresses"] = [c.im]
    if c.birthday:
        out["birthday"] = f"{c.birthday.isoformat()}T00:00:00Z"
    if c.urls:
        out["businessHomePage"] = c.urls[0]
    if c.categories:
        out["categories"] = list(c.categories)
    return out


def task_from_json(o: dict, uid: str) -> pim.Task:
    t = pim.Task(uid=uid, subject=str(o.get("title") or ""), body=_body_text(o),
                 status=TASK_STATUS.get(o.get("status") or "", "NEEDS-ACTION"),
                 priority=o.get("importance") if o.get("importance") in ("low", "high") else "normal",
                 categories=list(o.get("categories") or []), start=_instant(o.get("startDateTime")),
                 due=_instant(o.get("dueDateTime")), completed=_instant(o.get("completedDateTime")),
                 created=pim.parse_utc(o.get("createdDateTime")), modified=pim.parse_utc(o.get("lastModifiedDateTime")))
    t.recurrence = recurrence_from_json(o.get("recurrence"), t.warnings)
    return t


def _dtz(value: datetime | date) -> dict:
    if isinstance(value, datetime):
        return {"dateTime": value.astimezone(pim.UTC).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}
    return {"dateTime": f"{value.isoformat()}T00:00:00", "timeZone": "UTC"}


def task_json(t: pim.Task, import_id: str) -> dict:
    out: dict = {"title": t.subject or "(no title)", "status": GRAPH_TASK_STATUS.get(t.status, "notStarted"),
                 "importance": t.priority,
                 "linkedResources": [{"applicationName": "mccb", "displayName": "Migrated item", "externalId": import_id}]}
    if t.body.strip():
        out["body"] = {"contentType": "text", "content": t.body.strip()}
    if t.categories:
        out["categories"] = list(t.categories)
    if t.start is not None:
        out["startDateTime"] = _dtz(t.start)
    if t.due is not None:
        out["dueDateTime"] = _dtz(t.due)
    if t.completed is not None:
        out["completedDateTime"] = _dtz(t.completed)
    if t.recurrence is not None and (t.due or t.start) is not None:
        out["recurrence"] = recurrence_json(t.recurrence, t.due or t.start, None, "UTC")
        if "dueDateTime" not in out:
            out["dueDateTime"] = _dtz(t.start)  # To Do requires a due date on a recurring task
    return out


def _event_dtz(value: datetime | date, tz: str | None, win: str | None) -> dict:
    """In the zone of the event, so that a recurrence follows its changes of offset."""
    if not isinstance(value, datetime):
        return {"dateTime": f"{value.isoformat()}T00:00:00", "timeZone": win or "UTC"}
    if not win:
        return _dtz(value)
    return {"dateTime": pim.local_of(value, tz).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": win}


def attendees_text(e: pim.Event) -> str:
    """Graph mails an invitation for every attendee of an event it creates, and this cannot be turned off: unless
    asked, attendees are kept as text in the body."""
    lines = []
    if e.organizer is not None:
        lines.append(f"Organizer: {e.organizer.name} <{e.organizer.email}>")
    labels = {"REQ": "Required", "OPT": "Optional", "RES": "Resource"}
    for a in e.attendees:
        lines.append(f"{labels.get(a.role, 'Required')}: {a.name} <{a.email}> ({a.partstat.lower()})")
    return "\n".join(lines)


def event_json(e: pim.Event, import_id: str, send_invitations: bool) -> dict:
    win = pim.windows_zone(e.tz, e.start)
    tz = e.tz if win else None
    body = e.body.strip()
    if e.attendees and not send_invitations:
        body = (body + "\n\n" if body else "") + attendees_text(e)
    out: dict = {"subject": e.subject, "start": _event_dtz(e.start, tz, win), "end": _event_dtz(e.end, tz, win),
                 "isAllDay": e.all_day, "showAs": GRAPH_SHOW_AS.get(e.free_busy, "busy"),
                 "sensitivity": GRAPH_SENS.get(e.sensitivity, "normal"), "isReminderOn": e.reminder_minutes is not None,
                 "responseRequested": False,
                 # documented: the server avoids a second event when a POST is retried with the same value
                 "transactionId": hashlib.sha256(import_id.encode("utf-8")).hexdigest()[:40],
                 "singleValueExtendedProperties": [{"id": PROP_IMPORT, "value": import_id}]}
    if body:
        out["body"] = {"contentType": "text", "content": body}
    if e.location:
        out["location"] = {"displayName": e.location}
    if e.categories:
        out["categories"] = list(e.categories)
    if e.reminder_minutes is not None:
        out["reminderMinutesBeforeStart"] = e.reminder_minutes
    if e.recurrence is not None:
        out["recurrence"] = recurrence_json(e.recurrence, e.start, tz, win)
    if send_invitations and e.attendees:
        kinds = {"REQ": "required", "OPT": "optional", "RES": "resource"}
        out["attendees"] = [{"type": kinds.get(a.role, "required"),
                             "emailAddress": {"address": a.email, "name": a.name or a.email}} for a in e.attendees]
    return out


# ------------------------------------------------------------------ source
def _ext_value(obj: dict, prop: str):
    for p in obj.get("singleValueExtendedProperties") or []:
        if str(p.get("id") or "").lower() == prop.lower():
            return p.get("value")
    return None


def _int(value) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _shared_token(config: dict, secrets: dict) -> oauth.OAuthToken:
    tenant = str(config.get("tenant_id") or "").strip()
    client_id = str(config.get("client_id") or "").strip()
    if not tenant or not client_id:
        raise ConnectorError("'tenant_id' and 'client_id' are required", ConnectorError.CONFIG)
    if not secrets.get("client_secret"):
        raise ConnectorError("'client_secret' is required", ConnectorError.CONFIG)
    login, graph = CLOUDS.get(config.get("cloud") or "global", CLOUDS["global"])
    return oauth.OAuthToken(f"{login}/{quote(tenant, safe='')}/oauth2/v2.0/token", client_id, secrets["client_secret"],
                            f"{graph}/.default")


class GraphSource(Source):
    def __init__(self, config: dict, secrets: dict, ctx: dict | None = None):
        self.config = config
        self.secrets = secrets
        self.client: GraphClient | None = None
        self.include_tasks = bool(config.get("include_tasks", True))
        self._gate = threading.BoundedSemaphore(MAX_CONCURRENT)
        self._token: oauth.OAuthToken | None = None

    def open(self) -> None:
        if self._token is None:
            self._token = _shared_token(self.config, self.secrets)
        client = GraphClient(self.config, self.secrets, self._token, self._gate)
        try:
            client.get_json("/mailFolders/inbox", {"$select": "id"}, connecting=True)
        except ConnectorError:
            client.close()
            raise
        self.client = client

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        self.client = None

    def _c(self) -> GraphClient:
        if self.client is None:
            raise ConnectorError("Graph source not connected", ConnectorError.TRANSIENT)
        return self.client

    def _well_known(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in list(WELL_KNOWN_SPECIAL) + list(WELL_KNOWN_SKIPPED):
            try:
                out[str(self._c().get_json(f"/mailFolders/{name}", {"$select": "id"})["id"])] = name
            except GraphError as e:
                if e.status != 404:
                    raise
        return out

    def _mail_tree(self, url: str, prefix: list[str], well_known: dict[str, str], out: list[SourceFolder]) -> None:
        for f in self._c().all(url, {"$top": str(PAGE)}):
            fid = str(f.get("id") or "")
            name = well_known.get(fid)
            if name in WELL_KNOWN_SKIPPED:
                continue
            path = prefix + [str(f.get("displayName") or fid)]
            out.append(SourceFolder(path=path, kind=MAIL, special=WELL_KNOWN_SPECIAL.get(name or ""),
                                    count=f.get("totalItemCount"), ref=f"/mailFolders/{fid}"))
            if f.get("childFolderCount"):
                self._mail_tree(f"/mailFolders/{fid}/childFolders", path, well_known, out)

    def _contact_tree(self, url: str, prefix: list[str], out: list[SourceFolder]) -> None:
        for f in self._c().all(url, {"$top": str(PAGE)}):
            fid = str(f.get("id") or "")
            path = prefix + [str(f.get("displayName") or fid)]
            out.append(SourceFolder(path=path, kind=CONTACTS, ref=f"/contactFolders/{fid}"))
            self._contact_tree(f"/contactFolders/{fid}/childFolders", path, out)

    def folders(self) -> list[SourceFolder]:
        c = self._c()
        out: list[SourceFolder] = []
        self._mail_tree("/mailFolders", [], self._well_known(), out)
        for cal in c.all("/calendars", {"$top": str(PAGE)}):
            out.append(SourceFolder(path=[str(cal.get("name") or cal.get("id"))], kind=CALENDAR,
                                    special=xb.DEFAULT if cal.get("isDefaultCalendar") else None,
                                    ref=f"/calendars/{cal.get('id')}"))
        out.append(SourceFolder(path=["Contacts"], kind=CONTACTS, special=xb.DEFAULT, ref=""))
        self._contact_tree("/contactFolders", [], out)
        if self.include_tasks:
            try:
                for lst in c.all("/todo/lists", {"$top": str(PAGE)}):
                    out.append(SourceFolder(path=[str(lst.get("displayName") or lst.get("id"))], kind=TASKS,
                                            special=xb.DEFAULT if lst.get("wellknownListName") == "defaultList" else None,
                                            ref=f"/todo/lists/{lst.get('id')}"))
            except GraphError as e:
                raise ConnectorError(f"tasks of {c.mailbox}: {e}. A mailbox without To Do: turn off 'Include tasks'.",
                                     e.kind) from e
        out.sort(key=lambda f: (f.kind, f.path))
        return out

    # -- listing
    def _message_item(self, folder: SourceFolder, m: dict) -> SourceItem | None:
        if m.get("@removed") or not m.get("id"):
            return None
        rid = str(m["id"])
        when = pim.parse_utc(m.get("receivedDateTime"))
        size = _int(_ext_value(m, PROP_SIZE))
        mid = " ".join(str(m.get("internetMessageId") or "").split()) or None
        flagged = ((m.get("flag") or {}).get("flagStatus")) == "flagged"
        flags = xb.flags_of(bool(m.get("isRead")), flagged, _int(_ext_value(m, PROP_VERB)), bool(m.get("isDraft")))
        return SourceItem(key=xb.item_key(folder.path, rid), alt_key=xb.alt_key(mid, when, size), date=when, size=size,
                          flags=flags, ref=rid)

    def _plain_item(self, folder: SourceFolder, o: dict) -> SourceItem | None:
        if o.get("@removed") or not o.get("id"):
            return None
        rid = str(o["id"])
        when = _instant(o.get("start")) or pim.parse_utc(o.get("lastModifiedDateTime"))
        return SourceItem(key=xb.item_key(folder.path, rid), date=when, ref=rid)

    def _listing(self, folder: SourceFolder) -> tuple[str, dict, object]:
        if folder.kind == MAIL:
            params = {"$select": MESSAGE_FIELDS, "$expand": MESSAGE_EXPAND, "$top": str(PAGE)}
            return f"{folder.ref}/messages", params, self._message_item
        if folder.kind == CALENDAR:
            return f"{folder.ref}/events", {"$select": "id,start,lastModifiedDateTime", "$top": str(PAGE)}, self._plain_item
        if folder.kind == CONTACTS:
            return f"{folder.ref}/contacts", {"$select": "id,lastModifiedDateTime", "$top": str(PAGE)}, self._plain_item
        return f"{folder.ref}/tasks", {"$top": str(PAGE)}, self._plain_item

    def iter_items(self, folder: SourceFolder) -> Iterator[SourceItem]:
        url, params, make = self._listing(folder)
        for o in self._c().all(url, params):
            it = make(folder, o)
            if it is not None:
                yield it

    def changed_since(self, folder: SourceFolder, cursor: str | None) -> Changes:
        """Delta query, per folder: the cursor is the @odata.deltaLink. Removed items are not reported. Events have
        no per-calendar delta in v1.0 (calendarView only, with a time window): always a full listing."""
        if folder.kind == CALENDAR:
            return Changes(list(self.iter_items(folder)), None, reset=True)
        c = self._c()
        url, params, make = self._listing(folder)
        reset = not cursor
        link, query = (cursor, None) if cursor else (c.url(f"{url}/delta"), {k: v for k, v in params.items() if k != "$top"})
        items: list[SourceItem] = []
        while True:
            try:
                body = c.get_json(link, query)
            except GraphError as e:
                if e.status == 410 or e.code in ("resyncRequired", "syncStateNotFound", "SyncStateNotFound"):
                    if not cursor or reset:
                        raise
                    reset = True  # the state token expired: a full round
                    link, query = c.url(f"{url}/delta"), {k: v for k, v in params.items() if k != "$top"}
                    items = []
                    continue
                raise
            query = None
            for o in body.get("value") or []:
                it = make(folder, o)
                if it is not None:
                    items.append(it)
            if body.get("@odata.nextLink"):
                link = body["@odata.nextLink"]
                continue
            return Changes(items, body.get("@odata.deltaLink"), reset=reset)

    # -- reading
    def fetch(self, folder: SourceFolder, item: SourceItem, import_id: str) -> FetchedItem | None:
        try:
            if folder.kind == MAIL:
                return self._fetch_mail(item)
            if folder.kind == CALENDAR:
                return self._fetch_event(item)
            uid = "graph-" + xb.item_key(folder.path, item.ref)
            if folder.kind == CONTACTS:
                contact = contact_from_json(self._c().get_json(f"{folder.ref}/contacts/{item.ref}"), uid)
                return FetchedItem(raw=pim.contact_to_vcard(contact), subject=contact.full_name, warnings=contact.warnings)
            task = task_from_json(self._c().get_json(f"{folder.ref}/tasks/{item.ref}"), uid)
            return FetchedItem(raw=pim.task_to_ics(task), subject=task.subject, warnings=task.warnings)
        except GraphError as e:
            if e.status == 404:
                return None  # gone since the listing
            raise

    def _fetch_mail(self, item: SourceItem) -> FetchedItem:
        c = self._c()
        raw = c.request("GET", c.url(f"/messages/{item.ref}/$value")).content
        return FetchedItem(raw=raw, internal_date=item.date, flags=list(item.flags), subject=subject_of(raw))

    def _fetch_event(self, item: SourceItem) -> FetchedItem:
        c = self._c()
        o = c.get_json(f"/events/{item.ref}", {"$select": EVENT_FIELDS})
        warnings: list[str] = []
        master = o.get("type") == "seriesMaster"
        if master:
            o = c.get_json(f"/events/{item.ref}", {"$select": EVENT_FIELDS + ",cancelledOccurrences",
                                                   "$expand": "exceptionOccurrences"})
        e = event_from_json(o, warnings)
        if master and not e.all_day:
            local = pim.local_of(e.start, e.tz)
            for occ in o.get("cancelledOccurrences") or []:
                day = _occurrence_date(occ)
                if day is not None:
                    e.exdates.append(datetime.combine(day, local.time(), local.tzinfo).astimezone(pim.UTC))
        elif master:
            e.exdates = [d for d in (_occurrence_date(x) for x in o.get("cancelledOccurrences") or []) if d is not None]
        for x in o.get("exceptionOccurrences") or []:
            sub = event_from_json({**x, "originalStartTimeZone": x.get("originalStartTimeZone") or o.get("originalStartTimeZone")},
                                  warnings, e.tz)
            sub.recurrence = None
            original = pim.parse_utc(x.get("originalStart"))
            if original is None:
                warnings.append("exception-without-original-start")
                continue
            sub.recurrence_id = pim.all_day_date(original, e.tz) if e.all_day else original
            e.overrides.append(sub)
        return FetchedItem(raw=pim.event_to_ics(e), subject=e.subject, warnings=warnings)

    def read_rules(self) -> list[dict]:
        from .sieve import rules_from_graph
        c = self._c()
        paths = {f.ref.rsplit("/", 1)[-1]: f.path for f in self.folders() if f.kind == MAIL}
        return rules_from_graph(list(c.all("/mailFolders/inbox/messageRules")), paths)


# ------------------------------------------------------------------ target
class GraphSession(TargetSession):
    def __init__(self, target: "GraphTarget"):
        self.target = target
        self.client: GraphClient | None = None
        self._urls: dict[str, str] = {}
        self._delivery_time_works = True

    @property
    def connected(self) -> bool:
        return self.client is not None

    def connect(self) -> None:
        self.close()
        client = GraphClient(self.target.config, self.target.secrets, self.target.token(), self.target.gate)
        try:
            client.get_json("/mailFolders/inbox", {"$select": "id"}, connecting=True)
        except ConnectorError:
            client.close()
            raise
        self.client = client

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        self.client = None

    def _c(self) -> GraphClient:
        self.ensure()
        return self.client  # type: ignore[return-value]

    def plan_folders(self, folders: list[SourceFolder]) -> dict[str, FolderPlan]:
        return xb.plan(folders, self.target.root)

    # -- folders: the URL of the collection of a folder
    def _mail_folder(self, well_known: str | None, path: list[str], create: bool) -> str:
        c = self._c()
        if well_known:
            return f"/mailFolders/{well_known}"
        parent = ""
        for i, name in enumerate(path):
            found = None
            url = f"{parent}/childFolders" if parent else "/mailFolders"
            for f in c.all(url, {"$top": str(PAGE)}):
                if str(f.get("displayName") or "").casefold() == name.casefold():
                    found = f"/mailFolders/{f['id']}"
                    break
            if found is None:
                if not create:
                    raise ConnectorError(f"Graph folder '{'/'.join(path[:i + 1])}' not found", ConnectorError.FOLDER_MISSING)
                found = self._create(url, {"displayName": name}, "/mailFolders", name, "displayName")
            parent = found
        return parent

    def _create(self, url: str, body: dict, prefix: str, name: str, field: str) -> str:
        c = self._c()
        try:
            return f"{prefix}/{c.request('POST', c.url(url), json_body=body).json()['id']}"
        except GraphError as e:
            if e.status != 409:
                raise
        for f in c.all(url, {"$top": str(PAGE)}):  # another session has just created it
            if str(f.get(field) or "").casefold() == name.casefold():
                return f"{prefix}/{f['id']}"
        raise ConnectorError(f"Graph: folder '{name}' exists but cannot be found", ConnectorError.TRANSIENT)

    def _flat_folder(self, kind: str, well_known: str | None, path: list[str], create: bool) -> str:
        """Calendars and task lists are not nested: "A / B" for a nested source folder."""
        c = self._c()
        name = " / ".join(path)
        if kind == CALENDAR:
            if well_known:
                return "/calendar"
            url, prefix, field, body = "/calendars", "/calendars", "name", {"name": name}
        elif kind == CONTACTS:
            if well_known:
                return ""
            url, prefix, field, body = "/contactFolders", "/contactFolders", "displayName", {"displayName": name}
        else:
            url, prefix, field, body = "/todo/lists", "/todo/lists", "displayName", {"displayName": name}
        for f in c.all(url, {"$top": str(PAGE)}):
            if well_known and f.get("wellknownListName") == "defaultList":
                return f"{prefix}/{f['id']}"
            if not well_known and str(f.get(field) or "").casefold() == name.casefold():
                return f"{prefix}/{f['id']}"
        if well_known:
            raise ConnectorError("Graph: no default task list in this mailbox", ConnectorError.REJECTED)
        if not create:
            raise ConnectorError(f"Graph folder '{name}' not found", ConnectorError.FOLDER_MISSING)
        return self._create(url, body, prefix, name, field)

    def _folder(self, spec: str, create: bool = False) -> str:
        if not create and spec in self._urls:
            return self._urls[spec]
        kind, well_known, path = xb.parse_spec(spec)
        if kind == MAIL:
            url = self._mail_folder(well_known, path, create)
        else:
            url = self._flat_folder(kind, well_known, path, create)
        self._urls[spec] = url
        return url

    def ensure_folder(self, name: str, flag: str | None = None) -> None:
        self._folder(name, create=True)

    @staticmethod
    def _items_url(folder: str, kind: str) -> str:
        if kind == CALENDAR:
            return f"{folder}/events"
        if kind == CONTACTS:
            return f"{folder}/contacts"
        if kind == TASKS:
            return f"{folder}/tasks"
        return f"{folder}/messages"

    # -- writing
    def put(self, folder: str, item: OutgoingItem) -> str | None:
        url = self._folder(folder)
        try:
            if item.kind == MAIL:
                return self._put_mail(url, item)
            c = self._c()
            if item.kind == CONTACTS:
                body = contact_json(pim.vcard_to_contact(item.raw), item.import_id)
            elif item.kind == TASKS:
                body = task_json(pim.ics_to_task(item.raw), item.import_id)
            else:
                event = pim.ics_to_event(item.raw)
                if event.all_day and not event.tz:
                    event.tz = self.target.time_zone
                return self._put_event(url, event, item.import_id)
            return str(c.request("POST", c.url(self._items_url(url, item.kind)), json_body=body).json().get("id") or "")
        except GraphError as e:
            if e.status == 404 and e.code in ("ErrorItemNotFound", "ResourceNotFound", "ErrorInvalidIdMalformed"):
                self._urls.pop(folder, None)
                raise ConnectorError(str(e), ConnectorError.FOLDER_MISSING) from e
            raise

    def _put_mail(self, url: str, item: OutgoingItem) -> str | None:
        """importMime (beta): final state, received date = now. create (v1.0): always a draft. The body is Base64 in
        both. What MIME cannot carry goes in a PATCH afterwards."""
        c = self._c()
        data = base64.b64encode(item.raw)
        headers = {"Content-Type": "text/plain"}
        if self.target.import_mode == "draft":
            r = c.request("POST", c.url(f"{url}/messages"), content=data, headers=headers)
        else:
            r = c.request("POST", c.url(f"{url}/importMime", "beta"), content=data, headers=headers)
        rid = str(r.json().get("id") or "")
        if not rid:
            raise ConnectorError("Graph: no id for the imported message", ConnectorError.TRANSIENT)
        self._finish_mail(rid, item)
        return rid

    def _finish_mail(self, rid: str, item: OutgoingItem) -> None:
        """A failure here must not be reported as a failed write: the message exists, writing it again would make a
        second copy."""
        c = self._c()
        w = xb.wanted_flags(item.flags)
        props = [{"id": PROP_IMPORT, "value": item.import_id}]
        if w["verb"]:
            props.append({"id": PROP_VERB, "value": str(w["verb"])})
        body = {"isRead": w["read"], "flag": {"flagStatus": "flagged" if w["flagged"] else "notFlagged"},
                "singleValueExtendedProperties": props}
        try:
            c.request("PATCH", c.url(f"/messages/{rid}"), json_body=body)
        except ConnectorError as e:
            xb.log.warning("message %s written, flags not set: %s", item.import_id, e)
            return
        # not documented for Graph: tried once, then left alone for this session when refused
        if item.internal_date is not None and self._delivery_time_works:
            when = {"id": PROP_DELIVERY, "value": pim.utc_text(item.internal_date)}
            try:
                c.request("PATCH", c.url(f"/messages/{rid}"), json_body={"singleValueExtendedProperties": [when]})
            except ConnectorError as e:
                self._delivery_time_works = False
                xb.log.info("received date not settable through Graph: %s", e)

    def _occurrences(self, master: str, e: pim.Event) -> list[dict]:
        begin = min([x for x in [e.start, *e.exdates, *[o.recurrence_id for o in e.overrides]] if x is not None], key=_sort_key)
        last = max([x for x in [e.start, *e.exdates, *[o.recurrence_id for o in e.overrides]] if x is not None], key=_sort_key)
        params = {"startDateTime": pim.utc_text(_as_datetime(begin) - timedelta(days=2)),
                  "endDateTime": pim.utc_text(_as_datetime(last) + timedelta(days=2)),
                  "$select": "id,start,originalStart", "$top": str(PAGE)}
        return list(self._c().all(f"/events/{master}/instances", params))

    def _put_event(self, url: str, e: pim.Event, import_id: str) -> str | None:
        """The series, then its exceptions. All or nothing, as on EWS: a series without its exceptions would pass for
        migrated. The occurrences are matched by their original start."""
        c = self._c()
        o = c.request("POST", c.url(f"{url}/events"), json_body=event_json(e, import_id, self.target.send_invitations)).json()
        rid = str(o.get("id") or "")
        if not (e.recurrence is not None and (e.exdates or e.overrides) and rid):
            return rid
        try:
            occ = self._occurrences(rid, e)

            def match(original) -> dict | None:
                for x in occ:
                    when = pim.parse_utc(x.get("originalStart")) or _instant(x.get("start"))
                    if when is None:
                        continue
                    same = when == original if isinstance(original, datetime) else pim.all_day_date(when, e.tz) == original
                    if same:
                        return x
                return None
            for original in e.exdates:
                x = match(original)
                if x is not None:
                    c.request("DELETE", c.url(f"/events/{x['id']}"))
            for sub in e.overrides:
                x = match(sub.recurrence_id)
                if x is None:
                    xb.log.info("event %s: occurrence %s not found", e.uid, sub.recurrence_id)
                    continue
                win = pim.windows_zone(e.tz, sub.start)
                tz = e.tz if win else None
                c.request("PATCH", c.url(f"/events/{x['id']}"), json_body={
                    "subject": sub.subject, "start": _event_dtz(sub.start, tz, win), "end": _event_dtz(sub.end, tz, win),
                    "location": {"displayName": sub.location}, "body": {"contentType": "text", "content": sub.body}})
        except ConnectorError:
            try:
                c.request("DELETE", c.url(f"/events/{rid}"))
            except ConnectorError as de:
                xb.log.warning("event %s: series left without its exceptions: %s", e.uid, de)
            raise
        return rid

    # -- lookups
    def _ids_with(self, items_url: str, prop: str, wanted: str) -> list[str]:
        c = self._c()
        params = {"$select": "id", "$top": str(PAGE),
                  "$filter": f"singleValueExtendedProperties/Any(ep: ep/id eq '{prop}' and ep/value eq '{_odata(wanted)}')",
                  "$expand": f"singleValueExtendedProperties($filter=id eq '{prop}')"}
        # the filter of Graph ignores case on the value: the answer is compared exactly
        return [str(o["id"]) for o in c.all(items_url, params) if _ext_value(o, prop) == wanted]

    def _task_ids(self, url: str, wanted: str | None, prefix: str | None = None) -> list[str]:
        out = []
        for t in self._c().all(f"{url}/tasks", {"$expand": "linkedResources", "$top": str(PAGE)}):
            for link in t.get("linkedResources") or []:
                ext = str(link.get("externalId") or "")
                if (wanted is not None and ext == wanted) or (prefix is not None and ext.startswith(prefix)):
                    out.append(str(t.get("id")))
                    break
        return out

    def find(self, folder: str, item: OutgoingItem) -> list[str]:
        kind, _wk, _path = xb.parse_spec(folder)
        url = self._folder(folder)
        if kind == TASKS:
            return self._task_ids(url, item.import_id)
        ids = self._ids_with(self._items_url(url, kind), PROP_IMPORT, item.import_id)
        if kind == MAIL and not ids:  # written but the PATCH after it was lost: the header may be enough
            ids = self._ids_with(self._items_url(url, kind), PROP_HEADER, item.import_id)
        return ids

    def count_ours(self, folder: str, namespace: str, names: set[str] | None = None) -> int:
        kind, _wk, _path = xb.parse_spec(folder)
        url = self._folder(folder)
        prefix = f"<{namespace}-"
        if kind == TASKS:
            return len(self._task_ids(url, None, prefix))
        params = {"$select": "id", "$top": "1000",
                  "$filter": f"singleValueExtendedProperties/Any(ep: ep/id eq '{PROP_IMPORT}' and "
                             f"startswith(ep/value, '{_odata(prefix)}'))"}
        return sum(1 for _o in self._c().all(self._items_url(url, kind), params))

    def info(self) -> dict:
        return {"protocol": "Microsoft Graph", "mailbox": self.target.config.get("mailbox"),
                "import_mode": self.target.import_mode, "send_invitations": self.target.send_invitations}


def _as_datetime(value: datetime | date) -> datetime:
    return value if isinstance(value, datetime) else pim.date_at_midnight(value)


def _sort_key(value: datetime | date) -> datetime:
    return _as_datetime(value)


class GraphTarget(Target):
    # no CAP_READBACK: Exchange rewrites the MIME it stores, a re-read never hashes the same
    capabilities = frozenset({CAP_FOLDERS, CAP_FLAGS, CAP_PARALLEL})

    def __init__(self, config: dict, secrets: dict, ctx: dict | None = None):
        self.config = config
        self.secrets = secrets
        self.root = (config.get("root_folder") or "").strip() or None
        self.import_mode = config.get("import_mode") or "import_mime"
        self.send_invitations = bool(config.get("send_invitations"))
        self.time_zone = config.get("time_zone") or None
        self.gate = threading.BoundedSemaphore(MAX_CONCURRENT)
        self._token: oauth.OAuthToken | None = None
        self._lock = threading.Lock()

    def token(self) -> oauth.OAuthToken:
        with self._lock:
            if self._token is None:
                self._token = _shared_token(self.config, self.secrets)
            return self._token

    def session(self) -> GraphSession:
        return GraphSession(self)


# ------------------------------------------------------------------ tenant directory (connection-level)
# key -> (accepted application roles, level). Each requirement is met when any listed role is granted.
DIRECTORY_ROLES = {
    "directory": (("User.Read.All", "User.ReadWrite.All", "Directory.Read.All", "Directory.ReadWrite.All"), "required"),
    "mail": (("Mail.Read", "Mail.ReadWrite"), "required"),
    "calendars": (("Calendars.Read", "Calendars.ReadWrite"), "required"),
    "contacts": (("Contacts.Read", "Contacts.ReadWrite"), "required"),
    "settings": (("MailboxSettings.Read", "MailboxSettings.ReadWrite"), "required"),
    "tasks": (("Tasks.Read.All", "Tasks.ReadWrite.All"), "optional"),
}
AADSTS = {"7000215": "Wrong or expired client secret.", "7000222": "Wrong or expired client secret.",
          "700016": "Unknown client ID in this tenant.", "90002": "Unknown tenant ID.", "900023": "Unknown tenant ID."}
PROBE_LIMIT = 500           # above this many candidates, mailboxSettings is not asked for each
PURPOSES = {"user": "user", "linked": "user", "shared": "shared", "room": "room", "equipment": "equipment"}
USER_FIELDS = "id,displayName,mail,userPrincipalName,proxyAddresses,accountEnabled,assignedLicenses,userType"


def token_roles(token: str) -> list[str] | None:
    """The `roles` claim of an access token, read without verification. None when the token is not a JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, TypeError):
        return None
    if not isinstance(claims, dict):
        return None
    roles = claims.get("roles") or []
    return [str(r) for r in roles] if isinstance(roles, list) else None


def _aadsts_text(err: ConnectorError, secret: str) -> str:
    text = str(err)
    for code, plain in AADSTS.items():
        if f"AADSTS{code}" in text:
            return plain
    if err.kind == ConnectorError.UNREACHABLE:
        return "The Microsoft login service cannot be reached."
    if secret:
        text = text.replace(secret, "***")
    return text[:300]


def _addresses(u: dict) -> tuple[str, str, list[str]]:
    mail = str(u.get("mail") or "").strip().lower()
    upn = str(u.get("userPrincipalName") or "").strip().lower()
    aliases: list[str] = []
    for p in u.get("proxyAddresses") or []:
        p = str(p)
        if p[:5].lower() == "smtp:":
            a = p[5:].strip().lower()
            if a and a not in (mail, upn) and a not in aliases:
                aliases.append(a)
    return mail, upn, aliases


class GraphDirectory:
    """What a tenant holds: connection-scoped config only (tenant_id, client_id, cloud) and the client secret."""

    @staticmethod
    def _client(config: dict, secrets: dict) -> GraphClient:
        return GraphClient(config, secrets, _shared_token(config, secrets), threading.BoundedSemaphore(MAX_CONCURRENT),
                           tenant=True)

    def check(self, config: dict, secrets: dict) -> dict:
        checks: list[dict] = []

        def add(key: str, ok: bool, msg: str, level: str = "required", missing: list[str] | None = None) -> None:
            c = {"key": key, "ok": ok, "level": level, "msg": msg}
            if missing:
                c["missing"] = missing
            checks.append(c)

        secret = str(secrets.get("client_secret") or "")
        try:
            client = self._client(config, secrets)
            tok = client.token.token()
        except ConnectorError as e:
            add("token", False, _aadsts_text(e, secret))
            return {"ok": False, "checks": checks}
        try:
            add("token", True, "Token obtained.")
            roles = token_roles(tok)
            if roles is None:
                add("permissions", False, "The application permissions could not be read from the access token.",
                    "optional")
            else:
                for key, (accepted, level) in DIRECTORY_ROLES.items():
                    if any(r in roles for r in accepted):
                        add(key, True, "Granted.", level)
                    elif not roles:
                        add(key, False, "No application permission granted: grant admin consent.", level,
                            [accepted[0]])
                    else:
                        add(key, False, f"Missing application permission {accepted[0]} (admin consent needed).",
                            level, [accepted[0]])
            first = self._probe_users(client, add)
            if first:
                try:
                    client.get_json(f"/users/{quote(first, safe='')}/mailFolders/inbox", {"$select": "id"})
                    add("mailbox", True, "A mailbox can be read.")
                except ConnectorError as e:
                    add("mailbox", False, f"Reading a mailbox failed: {_graph_text(e)}")
            name = None
            try:
                org = client.get_json("/organization", {"$select": "displayName"}).get("value") or []
                name = (org[0].get("displayName") if org else None) or None
            except ConnectorError:
                pass
        finally:
            client.close()
        out = {"ok": all(c["ok"] for c in checks if c["level"] == "required"), "checks": checks}
        if name:
            out["tenant_name"] = name
        return out

    @staticmethod
    def _probe_users(client: GraphClient, add) -> str | None:
        """Lists a few users; returns the id of the first one with a mailbox."""
        try:
            rows = client.get_json("/users", {"$top": "10", "$select": "id,mail,userType"}).get("value") or []
        except ConnectorError as e:
            add("users", False, f"Listing users failed: {_graph_text(e)}")
            return None
        add("users", True, "The directory can be read.")
        for u in rows:
            if u.get("mail") and u.get("userType") != "Guest":
                return str(u["id"])
        add("mailbox", False, "No user with a mailbox among the first ones: mailbox access was not tested.", "optional")
        return None

    def list_mailboxes(self, config: dict, secrets: dict) -> dict:
        client = self._client(config, secrets)
        try:
            hidden = {"guests": 0, "no_mailbox": 0}
            cands: list[dict] = []
            for u in client.all("/users", {"$select": USER_FIELDS, "$top": "999"}):
                if u.get("userType") == "Guest":
                    hidden["guests"] += 1
                elif not str(u.get("mail") or "").strip():
                    hidden["no_mailbox"] += 1
                else:
                    cands.append(u)
            checked = len(cands) <= PROBE_LIMIT
            purposes: dict[str, str | None] = {}
            if checked and cands:
                with ThreadPoolExecutor(MAX_CONCURRENT) as pool:
                    for u, p in zip(cands, pool.map(lambda x: self._purpose(client, x["id"]), cands)):
                        purposes[u["id"]] = p
            out = []
            for u in cands:
                mail, upn, aliases = _addresses(u)
                enabled = u.get("accountEnabled") is not False
                licensed = bool(u.get("assignedLicenses"))
                kind, guess = "unknown", None
                if checked:
                    p = purposes[u["id"]]
                    if p == "none":
                        hidden["no_mailbox"] += 1
                        continue
                    kind = PURPOSES.get(p or "", "unknown")
                if kind == "unknown":
                    guess = "user" if enabled and licensed else ("shared_or_resource" if not enabled and not licensed
                                                                 else None)
                row = {"id": str(u["id"]), "display_name": str(u.get("displayName") or ""), "mail": mail, "upn": upn,
                       "aliases": aliases, "kind": kind, "enabled": enabled, "licensed": licensed}
                if kind == "unknown":
                    row["guess"] = guess
                out.append(row)
            return {"fetched_at": time.time(), "purpose_checked": checked, "mailboxes": out, "hidden": hidden}
        finally:
            client.close()

    @staticmethod
    def _purpose(client: GraphClient, uid: str) -> str | None:
        """userPurpose, "none" for a user without mailbox, None when it cannot be told."""
        try:
            body = client.get_json(f"/users/{quote(uid, safe='')}/mailboxSettings", {"$select": "userPurpose"})
        except GraphError as e:
            return "none" if e.status == 404 else None
        except ConnectorError:
            return None
        return str(body.get("userPurpose") or "") or None


def _graph_text(e: ConnectorError) -> str:
    code = getattr(e, "code", "")
    return f"{code}: {e}" if code and code not in str(e) else str(e)


def identity(config: dict) -> str:
    return f"{str(config.get('tenant_id', '')).lower()}:{str(config.get('mailbox', '')).lower()}"


_FIELDS = (
    registry.Field("tenant_id", "Tenant ID", required=True, help="Directory (tenant) ID of the Entra application.",
                   scope="connection"),
    registry.Field("client_id", "Client ID", required=True, help="Application (client) ID.", scope="connection"),
    registry.Field("client_secret", "Client secret", "password", required=True, secret=True, scope="connection"),
    registry.Field("mailbox", "Mailbox", required=True, help="User principal name or address of the mailbox, shared mailboxes too."),
    registry.Field("cloud", "Cloud", "select", default="global", options=tuple(CLOUDS), scope="connection"),
    registry.Field("include_tasks", "Include tasks", "boolean", default=True,
                   help="Microsoft To Do. Turn off for a mailbox without To Do, or if Tasks.ReadWrite.All is not granted."),
)
_SOURCE_FIELDS = _FIELDS
_TARGET_FIELDS = _FIELDS[:-1] + (
    registry.Field("root_folder", "Root folder", help="Put everything but the well-known folders under this folder."),
    registry.Field("time_zone", "Time zone of all-day events",
                   help="IANA name, such as Europe/Paris, for all-day events that do not say. Empty: UTC."),
    registry.Field("import_mode", "Mail import", "select", default="import_mime", options=("import_mime", "draft"),
                   help="import_mime: normal messages, through a beta API, needs Mail-Advanced.ReadWrite.All. draft: "
                        "stable API, but every message stays a draft. The received date is the date of the import in both."),
    registry.Field("send_invitations", "Send invitations for events with attendees", "boolean", default=False,
                   help="Graph mails every attendee when it creates such an event, and this cannot be turned off. Off: "
                        "attendees are written as text in the body of the event."),
)
_ITEMS = (MAIL, CALENDAR, CONTACTS, TASKS)

registry.register(registry.ConnectorSpec(
    kind="m365_graph", label="Exchange Online (Microsoft Graph)",
    description="Microsoft 365 mailboxes, with one Entra application. Tested against simulated servers, not real Exchange.",
    source=registry.Role(GraphSource, _ITEMS,
                         frozenset({CAP_FOLDERS, CAP_FLAGS, CAP_INTERNAL_DATE, CAP_INCREMENTAL, CAP_RULES}),
                         _SOURCE_FIELDS, identity),
    target=registry.Role(GraphTarget, _ITEMS, GraphTarget.capabilities, _TARGET_FIELDS, identity),
    directory=GraphDirectory(),
))
