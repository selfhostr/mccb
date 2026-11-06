"""Exchange on premises through EWS, as a source and as a target: mail as MIME, calendar, contacts and tasks through
the neutral model of pim.py. Tested against a simulated server only, see docs/exchange.md."""
from __future__ import annotations

import base64
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import Iterator
from xml.sax.saxutils import escape, quoteattr

import httpx

from . import exchange_base as xb
from . import oauth, pim, registry
from .base import (CALENDAR, CAP_FLAGS, CAP_FOLDERS, CAP_INCREMENTAL, CAP_INTERNAL_DATE, CAP_PARALLEL, CAP_RULES,
                   CONTACTS, MAIL, TASKS, Changes, ConnectorError, FetchedItem, FolderPlan, OutgoingItem, Source,
                   SourceFolder, SourceItem, Target, TargetSession, subject_of)

NS_M = "http://schemas.microsoft.com/exchange/services/2006/messages"
NS_T = "http://schemas.microsoft.com/exchange/services/2006/types"
NS_S = "http://schemas.xmlsoap.org/soap/envelope/"
PAGE = 500
SYNC_PAGE = 512
FOLDER_PAGE = 500
GET_BATCH = 50
BUSY_RETRIES = 6
BUSY_DEFAULT = 5.0
BUSY_MAX_WAIT = 300.0
TAG_FLAG, TAG_VERB, TAG_MSGFLAGS, TAG_DELIVERY, TAG_SUBMIT, TAG_HIDDEN = (
    "0x1090", "0x1081", "0x0E07", "0x0E06", "0x0039", "0x10F4")
MSGFLAG_READ, MSGFLAG_UNSENT = 1, 8
KIND_OF_CLASS = {"ipf.note": MAIL, "ipf": MAIL, "ipf.appointment": CALENDAR, "ipf.contact": CONTACTS,
                 "ipf.task": TASKS}
WELL_KNOWN_SPECIAL = {"inbox": "inbox", "sentitems": "sent", "drafts": "drafts", "deleteditems": "trash",
                      "junkemail": "junk"}
WELL_KNOWN_DEFAULT = {"calendar": CALENDAR, "contacts": CONTACTS, "tasks": TASKS}
WELL_KNOWN_SKIPPED = ("outbox", "syncissues", "conflicts", "localfailures", "serverfailures", "searchfolders")
TRANSIENT_CODES = {
    "ErrorServerBusy", "ErrorInternalServerTransientError", "ErrorMailboxStoreUnavailable", "ErrorMailboxMoveInProgress",
    "ErrorConnectionFailed", "ErrorTimeoutExpired", "ErrorBatchProcessingStopped", "ErrorTooManyObjectsOpened",
    "ErrorADUnavailable", "ErrorInternalServerError", "ErrorExceededConnectionCount", "ErrorRpcFailed"}
AUTH_CODES = {"ErrorAccessDenied", "ErrorImpersonateUserDenied", "ErrorImpersonationDenied",
              "ErrorImpersonationFailed", "ErrorNotDelegate", "ErrorDelegateNoUser", "ErrorInvalidLicense"}
CONFIG_CODES = {"ErrorNonExistentMailbox", "ErrorInvalidSmtpAddress", "ErrorMailboxLogonFailed",
                "ErrorInvalidServerVersion", "ErrorUnsupportedPathForQuery", "ErrorInvalidRequest"}
BUSY_CODES = {"ErrorServerBusy", "ErrorTooManyObjectsOpened", "ErrorExceededConnectionCount"}
FOLDER_CODES = {"ErrorFolderNotFound", "ErrorParentFolderNotFound"}
GONE_CODES = {"ErrorItemNotFound", "ErrorInvalidIdNotAnItemId"}
BAD_SYNC_CODES = {"ErrorInvalidSyncStateData", "ErrorSyncFolderNotFound"}
DAY_NAMES = {"SU": "Sunday", "MO": "Monday", "TU": "Tuesday", "WE": "Wednesday", "TH": "Thursday", "FR": "Friday",
             "SA": "Saturday"}
DAY_CODES = {v: k for k, v in DAY_NAMES.items()}
DAY_GROUPS = {"Day": list(pim.DAYS), "Weekday": ["MO", "TU", "WE", "TH", "FR"], "WeekendDay": ["SA", "SU"]}
INDEX_NAMES = {1: "First", 2: "Second", 3: "Third", 4: "Fourth", -1: "Last"}
INDEX_CODES = {v: k for k, v in INDEX_NAMES.items()}
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December")
PARTSTAT = {"Accept": "ACCEPTED", "Tentative": "TENTATIVE", "Decline": "DECLINED"}
BUSY_OF = {"Free": "FREE", "Tentative": "TENTATIVE", "Busy": "BUSY", "OOF": "OOF", "WorkingElsewhere": "WORKINGELSEWHERE"}
EWS_BUSY = {v: k for k, v in BUSY_OF.items()}
SENS_OF = {"Private": "PRIVATE", "Confidential": "CONFIDENTIAL", "Personal": "PRIVATE"}
EWS_SENS = {"PUBLIC": "Normal", "PRIVATE": "Private", "CONFIDENTIAL": "Confidential"}
TASK_STATUS = {"NotStarted": "NEEDS-ACTION", "InProgress": "IN-PROCESS", "Completed": "COMPLETED",
               "WaitingOnOthers": "IN-PROCESS", "Deferred": "CANCELLED"}
EWS_TASK_STATUS = {"NEEDS-ACTION": "NotStarted", "IN-PROCESS": "InProgress", "COMPLETED": "Completed",
                   "CANCELLED": "Deferred"}
PHONE_KEYS = {"AssistantPhone": "assistant", "BusinessFax": "business_fax", "BusinessPhone": "business",
              "BusinessPhone2": "business2", "Callback": "callback", "CarPhone": "car", "CompanyMainPhone": "company",
              "HomeFax": "home_fax", "HomePhone": "home", "HomePhone2": "home2", "Isdn": "isdn", "MobilePhone": "mobile",
              "OtherFax": "other_fax", "OtherTelephone": "other", "Pager": "pager", "PrimaryPhone": "primary",
              "RadioPhone": "radio", "Telex": "telex"}
EWS_PHONE = {v: k for k, v in PHONE_KEYS.items()}
ADDRESS_KEYS = {"Business": "work", "Home": "home", "Other": "other"}
EWS_ADDRESS = {v: k for k, v in ADDRESS_KEYS.items()}

_sleep = time.sleep  # replaced in the tests


def t_(name: str) -> str:
    return f"{{{NS_T}}}{name}"


def m_(name: str) -> str:
    return f"{{{NS_M}}}{name}"


def _text(el, name: str, default: str = "") -> str:
    if el is None:
        return default
    found = el.findtext(t_(name))
    return found if found is not None else default


def _bool(el, name: str) -> bool:
    return _text(el, name).strip().lower() == "true"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _x(value) -> str:
    return escape(str(value))


def _prop_uri(tag: str, kind: str) -> str:
    return f'<t:ExtendedFieldURI PropertyTag="{tag}" PropertyType="{kind}"/>'


IMPORT_URI = f'<t:ExtendedFieldURI PropertySetId="{xb.IMPORT_GUID}" PropertyName="{xb.IMPORT_PROP}" PropertyType="String"/>'


def _ext(uri: str, value) -> str:
    return f"<t:ExtendedProperty>{uri}<t:Value>{_x(value)}</t:Value></t:ExtendedProperty>"


def _ext_values(item) -> dict[int | str, str]:
    """{property tag as a number, or property name: value} of the extended properties of an item."""
    out: dict[int | str, str] = {}
    for p in item.findall(t_("ExtendedProperty")):
        uri = p.find(t_("ExtendedFieldURI"))
        if uri is None:
            continue
        value = p.findtext(t_("Value")) or ""
        if uri.get("PropertyTag"):
            try:
                out[int(uri.get("PropertyTag"), 0)] = value
            except ValueError:
                pass
        elif uri.get("PropertyName"):
            out[uri.get("PropertyName")] = value
    return out


def _int(value) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def classify(code: str, msg: str, connecting: bool = False) -> ConnectorError:
    text = f"EWS {code}: {msg}"[:1000] if msg else f"EWS {code}"
    if code in TRANSIENT_CODES:
        return ConnectorError(text, ConnectorError.UNREACHABLE if connecting else ConnectorError.TRANSIENT)
    if code == "ErrorQuotaExceeded":
        return ConnectorError(text, ConnectorError.QUOTA)
    if code in AUTH_CODES:
        return ConnectorError(text, ConnectorError.AUTH)
    if code in CONFIG_CODES:
        return ConnectorError(text, ConnectorError.CONFIG)
    if code in FOLDER_CODES:
        return ConnectorError(text, ConnectorError.FOLDER_MISSING)
    return ConnectorError(text, ConnectorError.REJECTED)


# ------------------------------------------------------------------ authentication
class EwsAuth(httpx.Auth):
    """Basic or NTLM. `auto` follows the WWW-Authenticate of the server, NTLM first. Basic never goes over plain HTTP.
    NTLM authenticates the connection: a client is used by one thread, and sends its requests on one connection."""
    requires_response_body = True

    def __init__(self, username: str, password: str, mode: str = "auto"):
        self.username = username
        self.password = password
        self.mode = mode
        self._use_basic = mode == "basic"

    def _basic(self, request: httpx.Request) -> None:
        if request.url.scheme != "https":
            raise ConnectorError("Basic authentication refused over plain HTTP: use https", ConnectorError.CONFIG)
        token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
        request.headers["Authorization"] = f"Basic {token}"

    def auth_flow(self, request: httpx.Request):
        if self._use_basic:
            self._basic(request)
            yield request
            return
        response = yield request
        if response.status_code != 401:
            return
        offered = {p.strip().split(" ")[0].lower()
                   for v in response.headers.get_list("www-authenticate") for p in v.split(",")}
        if self.mode == "ntlm" or (self.mode == "auto" and ("ntlm" in offered or "negotiate" in offered)):
            yield from self._ntlm(request)
        elif "basic" in offered:
            self._use_basic = True
            self._basic(request)
            yield request

    def _ntlm(self, request: httpx.Request):
        import spnego  # only for servers that ask for NTLM
        ctx = spnego.client(self.username, self.password, hostname=request.url.host, service="HTTP",
                            protocol="ntlm", options=spnego.NegotiateOptions.use_ntlm)
        request.headers["Authorization"] = "NTLM " + base64.b64encode(ctx.step()).decode("ascii")
        response = yield request
        challenge = None
        for v in response.headers.get_list("www-authenticate"):
            m = re.match(r"^NTLM\s+(\S+)", v.strip(), re.I)
            if m:
                challenge = m.group(1)
        if response.status_code != 401 or challenge is None:
            return
        request.headers["Authorization"] = "NTLM " + base64.b64encode(ctx.step(base64.b64decode(challenge))).decode("ascii")
        yield request


# ------------------------------------------------------------------ client
class EwsClient:
    """One HTTP connection, one thread. `access`: own (the login's mailbox), impersonation (ApplicationImpersonation
    role, the budget is charged to the mailbox), delegate (the login has rights on a shared mailbox)."""

    def __init__(self, config: dict, secrets: dict):
        self.url = str(config.get("url") or "")
        if not self.url.lower().startswith(("https://", "http://")):
            raise ConnectorError("EWS URL must start with https://", ConnectorError.CONFIG)
        self.login = str(config.get("login") or "")
        self.access = config.get("access") or "own"
        self.mailbox = str(config.get("mailbox") or "").strip()
        if self.access != "own" and not self.mailbox:
            raise ConnectorError(f"'mailbox' is required with access '{self.access}'", ConnectorError.CONFIG)
        self.version = config.get("server_version") or "Exchange2013"
        self.http = oauth.http_client(bool(config.get("verify_tls", True)), 300.0,
                                      auth=EwsAuth(self.login, secrets.get("password") or "", config.get("auth") or "auto"))

    def close(self) -> None:
        self.http.close()

    def _envelope(self, body: str) -> bytes:
        head = f'<t:RequestServerVersion Version="{self.version}"/>'
        if self.access == "impersonation":
            head += ("<t:ExchangeImpersonation><t:ConnectingSID><t:SmtpAddress>" + _x(self.mailbox)
                     + "</t:SmtpAddress></t:ConnectingSID></t:ExchangeImpersonation>")
        return (f'<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="{NS_S}" xmlns:m="{NS_M}" xmlns:t="{NS_T}">'
                f"<s:Header>{head}</s:Header><s:Body>{body}</s:Body></s:Envelope>").encode("utf-8")

    def call(self, body: str, connecting: bool = False) -> ET.Element:
        """The element under <s:Body>. Waits and tries again when the server asks for it, then raises `transient`."""
        data = self._envelope(body)
        headers = {"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml"}
        if self.access == "impersonation":
            headers["X-AnchorMailbox"] = self.mailbox
        attempt = 0
        while True:
            attempt += 1
            try:
                r = self.http.post(self.url, content=data, headers=headers)
            except httpx.HTTPError as e:
                raise ConnectorError(f"EWS {self.url}: {type(e).__name__}: {e}",
                                     ConnectorError.UNREACHABLE if connecting else ConnectorError.TRANSIENT) from e
            if r.status_code in (401, 403):
                raise ConnectorError(f"EWS refused the credentials (HTTP {r.status_code})", ConnectorError.AUTH)
            if r.status_code in (301, 302, 303, 307, 308):
                raise ConnectorError(f"EWS redirects to {r.headers.get('location')}: set this URL",
                                     ConnectorError.CONFIG)
            if r.status_code == 404:
                raise ConnectorError(f"no EWS at {self.url} (HTTP 404)", ConnectorError.CONFIG)
            root = None
            try:
                root = ET.fromstring(r.content) if r.content else None
            except ET.ParseError as e:
                if r.status_code == 200:
                    raise ConnectorError(f"EWS: invalid XML ({e})", ConnectorError.TRANSIENT) from e
            wait = self._busy_wait(root, r)
            if wait is not None:
                if attempt > BUSY_RETRIES:
                    raise ConnectorError(f"EWS server busy after {attempt} attempts", ConnectorError.TRANSIENT)
                _sleep(min(wait, BUSY_MAX_WAIT))
                continue
            body_el = root.find(f"{{{NS_S}}}Body") if root is not None else None
            if body_el is None or len(body_el) == 0:
                kind = ConnectorError.TRANSIENT if r.status_code >= 500 else ConnectorError.REJECTED
                raise ConnectorError(f"EWS: unexpected answer (HTTP {r.status_code})", kind)
            first = body_el[0]
            if _local(first.tag) == "Fault":
                code = ""
                for el in first.iter():
                    if _local(el.tag) == "ResponseCode" and el.text:
                        code = el.text.strip()
                code = code or (first.findtext("faultcode") or "Fault").split(":")[-1]
                raise classify(code, first.findtext("faultstring") or "", connecting)
            return first

    @staticmethod
    def _busy_wait(root, r: httpx.Response) -> float | None:
        """Seconds to wait if the server is busy, None if it is not. Documented forms: HTTP 503, HTTP 500 or 200 with
        ErrorServerBusy, in a SOAP fault or a response message, with a BackOffMilliseconds hint."""
        busy = r.status_code in (429, 503)
        backoff = None
        if root is not None:
            for el in root.iter():
                name = _local(el.tag)
                if name in ("ResponseCode", "faultcode") and (el.text or "").strip().split(":")[-1] in BUSY_CODES:
                    busy = True
                elif name == "Value" and el.get("Name") == "BackOffMilliseconds":
                    backoff = _int(el.text)
        if not busy:
            return None
        if backoff is not None:
            return backoff / 1000.0
        after = _int(r.headers.get("retry-after"))
        return float(after) if after is not None else BUSY_DEFAULT

    # -- responses
    @staticmethod
    def messages(resp: ET.Element) -> list[ET.Element]:
        box = resp.find(m_("ResponseMessages"))
        return list(box) if box is not None else [resp]

    @staticmethod
    def check(msg: ET.Element, connecting: bool = False) -> ET.Element:
        if msg.get("ResponseClass") == "Error":
            code = msg.findtext(m_("ResponseCode")) or "Error"
            raise classify(code, msg.findtext(m_("MessageText")) or "", connecting)
        return msg

    def one(self, body: str, connecting: bool = False) -> ET.Element:
        return self.check(self.messages(self.call(body, connecting))[0], connecting)

    # -- folder references
    def distinguished(self, name: str) -> str:
        box = f"<t:Mailbox><t:EmailAddress>{_x(self.mailbox)}</t:EmailAddress></t:Mailbox>" if self.access == "delegate" else ""
        return f'<t:DistinguishedFolderId Id="{name}">{box}</t:DistinguishedFolderId>'

    # -- folders
    def get_folder_ids(self, names: list[str]) -> dict[str, str]:
        """{distinguished name: folder id}, without the names this mailbox does not have."""
        refs = "".join(self.distinguished(n) for n in names)
        resp = self.call(f"<m:GetFolder><m:FolderShape><t:BaseShape>IdOnly</t:BaseShape></m:FolderShape>"
                         f"<m:FolderIds>{refs}</m:FolderIds></m:GetFolder>")
        out: dict[str, str] = {}
        for name, msg in zip(names, self.messages(resp)):
            if msg.get("ResponseClass") == "Error":
                code = msg.findtext(m_("ResponseCode")) or ""
                if code in FOLDER_CODES or code == "ErrorInvalidOperation":
                    continue
                self.check(msg)
            for el in msg.iter(t_("FolderId")):
                out[name] = el.get("Id") or ""
                break
        return out

    def find_folders(self, parent_xml: str, deep: bool) -> list[ET.Element]:
        props = ("<t:AdditionalProperties><t:FieldURI FieldURI=\"folder:DisplayName\"/>"
                 "<t:FieldURI FieldURI=\"folder:FolderClass\"/><t:FieldURI FieldURI=\"folder:ParentFolderId\"/>"
                 "<t:FieldURI FieldURI=\"folder:TotalCount\"/>" + _prop_uri(TAG_HIDDEN, "Boolean")
                 + "</t:AdditionalProperties>")
        out: list[ET.Element] = []
        offset = 0
        while True:
            msg = self.one(f'<m:FindFolder Traversal="{"Deep" if deep else "Shallow"}"><m:FolderShape>'
                           f"<t:BaseShape>IdOnly</t:BaseShape>{props}</m:FolderShape>"
                           f'<m:IndexedPageFolderView MaxEntriesReturned="{FOLDER_PAGE}" Offset="{offset}" '
                           f'BasePoint="Beginning"/><m:ParentFolderIds>{parent_xml}</m:ParentFolderIds></m:FindFolder>')
            root = msg.find(m_("RootFolder"))
            if root is None:
                return out
            box = root.find(t_("Folders"))
            got = list(box) if box is not None else []
            out.extend(got)
            if root.get("IncludesLastItemInRange", "true").lower() == "true" or not got:
                return out
            offset += len(got)

    # -- items
    def get_items(self, ids: list[str], shape: str) -> list[tuple[str, ET.Element | None]]:
        """[(item id, element or None when gone)] in the order of `ids`."""
        refs = "".join(f"<t:ItemId Id={quoteattr(i)}/>" for i in ids)
        resp = self.call(f"<m:GetItem><m:ItemShape>{shape}</m:ItemShape><m:ItemIds>{refs}</m:ItemIds></m:GetItem>")
        out: list[tuple[str, ET.Element | None]] = []
        for i, msg in zip(ids, self.messages(resp)):
            if msg.get("ResponseClass") == "Error" and msg.findtext(m_("ResponseCode")) in GONE_CODES:
                out.append((i, None))
                continue
            self.check(msg)
            box = msg.find(m_("Items"))
            out.append((i, box[0] if box is not None and len(box) else None))
        return out


# ------------------------------------------------------------------ recurrence
def recurrence_from_xml(el, warnings: list[str]) -> pim.Recurrence | None:
    if el is None:
        return None
    pattern, rng = None, None
    for child in el:
        name = _local(child.tag)
        if name.endswith("Regeneration"):
            warnings.append(f"recurrence-unsupported:{name}")
            return None
        if name.endswith("Recurrence") and name not in ("NoEndRecurrence", "EndDateRecurrence", "NumberedRecurrence"):
            pattern = child
        else:
            rng = child
    if pattern is None:
        return None
    kind = _local(pattern.tag)
    rec = pim.Recurrence(freq="DAILY", interval=_int(_text(pattern, "Interval")) or 1)
    days = []
    for word in _text(pattern, "DaysOfWeek").split():
        days.extend(DAY_GROUPS.get(word) or ([DAY_CODES[word]] if word in DAY_CODES else []))
    if kind == "DailyRecurrence":
        rec.freq = "DAILY"
    elif kind == "WeeklyRecurrence":
        rec.freq = "WEEKLY"
        rec.by_day = days
        if _text(pattern, "FirstDayOfWeek") in DAY_CODES:
            rec.week_start = DAY_CODES[_text(pattern, "FirstDayOfWeek")]
    elif kind in ("AbsoluteMonthlyRecurrence", "RelativeMonthlyRecurrence", "AbsoluteYearlyRecurrence",
                  "RelativeYearlyRecurrence"):
        rec.freq = "MONTHLY" if "Monthly" in kind else "YEARLY"
        if kind.startswith("Absolute"):
            rec.month_day = _int(_text(pattern, "DayOfMonth"))
        else:
            rec.by_day = days
            rec.set_pos = INDEX_CODES.get(_text(pattern, "DayOfWeekIndex"))
        if "Yearly" in kind and _text(pattern, "Month") in MONTH_NAMES:
            rec.month = MONTH_NAMES.index(_text(pattern, "Month")) + 1
    else:
        warnings.append(f"recurrence-unsupported:{kind}")
        return None
    if rng is not None:
        if _local(rng.tag) == "NumberedRecurrence":
            rec.count = _int(_text(rng, "NumberOfOccurrences"))
        elif _local(rng.tag) == "EndDateRecurrence":
            rec.until = _date_of(_text(rng, "EndDate"))
    return rec


def _date_of(text: str) -> date | None:
    try:
        return date.fromisoformat((text or "")[:10])
    except ValueError:
        return None


def _days_word(days: list[str]) -> str | None:
    """EWS holds one value when a position is given: a day, or a group of days."""
    if len(days) == 1:
        return DAY_NAMES.get(days[0])
    for word, group in DAY_GROUPS.items():
        if set(days) == set(group):
            return word
    return None


def recurrence_xml(rec: pim.Recurrence, start: datetime | date, tz: str | None) -> str:
    """Raises ConnectorError (rejected) for a rule EWS cannot hold."""
    first = pim.local_of(start, tz).date() if isinstance(start, datetime) else start
    inter = f"<t:Interval>{max(1, rec.interval)}</t:Interval>"
    if rec.freq == "DAILY":
        pattern = f"<t:DailyRecurrence>{inter}</t:DailyRecurrence>"
    elif rec.freq == "WEEKLY":
        days = rec.by_day or [pim.DAYS[(first.weekday() + 1) % 7]]
        wk = f"<t:FirstDayOfWeek>{DAY_NAMES[rec.week_start]}</t:FirstDayOfWeek>" if rec.week_start in DAY_NAMES else ""
        pattern = (f"<t:WeeklyRecurrence>{inter}<t:DaysOfWeek>" + " ".join(DAY_NAMES[d] for d in days)
                   + f"</t:DaysOfWeek>{wk}</t:WeeklyRecurrence>")
    elif rec.freq in ("MONTHLY", "YEARLY"):
        month = rec.month or first.month
        month_xml = f"<t:Month>{MONTH_NAMES[month - 1]}</t:Month>"
        if rec.by_day:
            word = _days_word(rec.by_day)
            if word is None or rec.set_pos is None:
                raise ConnectorError("recurrence not supported by EWS: several days without a position",
                                     ConnectorError.REJECTED)
            index = f"<t:DaysOfWeek>{word}</t:DaysOfWeek><t:DayOfWeekIndex>{INDEX_NAMES[rec.set_pos]}</t:DayOfWeekIndex>"
            pattern = (f"<t:RelativeMonthlyRecurrence>{inter}{index}</t:RelativeMonthlyRecurrence>" if rec.freq == "MONTHLY"
                       else f"<t:RelativeYearlyRecurrence>{index}{month_xml}</t:RelativeYearlyRecurrence>")
        else:
            day = rec.month_day or first.day
            pattern = (f"<t:AbsoluteMonthlyRecurrence>{inter}<t:DayOfMonth>{day}</t:DayOfMonth>"
                       "</t:AbsoluteMonthlyRecurrence>" if rec.freq == "MONTHLY"
                       else f"<t:AbsoluteYearlyRecurrence><t:DayOfMonth>{day}</t:DayOfMonth>{month_xml}"
                            "</t:AbsoluteYearlyRecurrence>")
    else:
        raise ConnectorError(f"recurrence not supported by EWS: {rec.freq}", ConnectorError.REJECTED)
    begin = f"<t:StartDate>{first.isoformat()}</t:StartDate>"
    if rec.count:
        rng = f"<t:NumberedRecurrence>{begin}<t:NumberOfOccurrences>{rec.count}</t:NumberOfOccurrences></t:NumberedRecurrence>"
    elif rec.until is not None:
        rng = f"<t:EndDateRecurrence>{begin}<t:EndDate>{rec.until.isoformat()}</t:EndDate></t:EndDateRecurrence>"
    else:
        rng = f"<t:NoEndRecurrence>{begin}</t:NoEndRecurrence>"
    return f"<t:Recurrence>{pattern}{rng}</t:Recurrence>"


# ------------------------------------------------------------------ EWS items -> neutral model
def _person(box, warnings: list[str]) -> pim.Person | None:
    if box is None:
        return None
    addr = _text(box, "EmailAddress").strip()
    name = _text(box, "Name")
    if "@" not in addr:  # legacy Exchange address (RoutingType EX): no SMTP address in this answer
        warnings.append(f"attendee-without-address:{name or addr}"[:200])
        return None
    return pim.Person(addr, name)


def _attendees(item, warnings: list[str]) -> list[pim.Person]:
    out = []
    for group, role in (("RequiredAttendees", "REQ"), ("OptionalAttendees", "OPT"), ("Resources", "RES")):
        box = item.find(t_(group))
        for a in box.findall(t_("Attendee")) if box is not None else []:
            p = _person(a.find(t_("Mailbox")), warnings)
            if p is not None:
                p.role = role
                p.partstat = PARTSTAT.get(_text(a, "ResponseType"), "NEEDS-ACTION")
                out.append(p)
    return out


def _tz_of_item(item) -> str | None:
    tz = item.find(t_("StartTimeZone"))
    names = [tz.get("Id") if tz is not None else None, _text(item, "StartTimeZoneId") or None]
    for name in names:
        iana = pim.iana_of(name)
        if iana:
            return iana
    return None


def _categories(item) -> list[str]:
    box = item.find(t_("Categories"))
    return [c.text for c in box.findall(t_("String")) if c.text] if box is not None else []


def event_from_xml(item, warnings: list[str], tz: str | None = None) -> pim.Event:
    tz = _tz_of_item(item) or tz
    start = pim.parse_utc(_text(item, "Start"))
    end = pim.parse_utc(_text(item, "End"))
    if start is None or end is None:
        raise ConnectorError("calendar item without start or end", ConnectorError.REJECTED)
    if tz is None:
        warnings.append("timezone-unknown-kept-as-utc")
    begin: datetime | date = start
    finish: datetime | date = end
    if _bool(item, "IsAllDayEvent"):
        begin, finish = pim.all_day_date(start, tz), pim.all_day_date(end, tz)
        if finish <= begin:
            finish = begin + timedelta(days=1)
    ident = item.find(t_("ItemId"))
    uid = _text(item, "UID") or (xb.item_key(["uid"], ident.get("Id") or "") if ident is not None else "")
    e = pim.Event(uid=uid, start=begin, end=finish, subject=_text(item, "Subject"), body=_text(item, "Body"),
                  location=_text(item, "Location"), tz=tz, warnings=warnings)
    org = item.find(t_("Organizer"))
    e.organizer = _person(org.find(t_("Mailbox")), warnings) if org is not None else None
    e.attendees = _attendees(item, warnings)
    e.free_busy = BUSY_OF.get(_text(item, "LegacyFreeBusyStatus"), "BUSY")
    e.sensitivity = SENS_OF.get(_text(item, "Sensitivity"), "PUBLIC")
    e.categories = _categories(item)
    if _bool(item, "ReminderIsSet"):
        e.reminder_minutes = _int(_text(item, "ReminderMinutesBeforeStart"))
    e.created = pim.parse_utc(_text(item, "DateTimeCreated"))
    e.modified = pim.parse_utc(_text(item, "LastModifiedTime"))
    e.cancelled = _bool(item, "IsCancelled")
    if _bool(item, "HasAttachments"):
        warnings.append("attachments-not-copied")
    e.recurrence = recurrence_from_xml(item.find(t_("Recurrence")), warnings)
    return e


def contact_from_xml(item, uid: str) -> pim.Contact:
    c = pim.Contact(uid=uid, full_name=_text(item, "DisplayName"), surname=_text(item, "Surname"),
                    given=_text(item, "GivenName"), middle=_text(item, "MiddleName"), suffix=_text(item, "Generation"),
                    nickname=_text(item, "Nickname"), company=_text(item, "CompanyName"),
                    department=_text(item, "Department"), title=_text(item, "JobTitle"),
                    profession=_text(item, "Profession"), note=_text(item, "Body"), spouse=_text(item, "SpouseName"),
                    assistant=_text(item, "AssistantName"), categories=_categories(item),
                    modified=pim.parse_utc(_text(item, "LastModifiedTime")))
    complete = item.find(t_("CompleteName"))
    if complete is not None:
        c.prefix = _text(complete, "Title")
    box = item.find(t_("EmailAddresses"))
    for key in ("EmailAddress1", "EmailAddress2", "EmailAddress3"):
        for entry in box.findall(t_("Entry")) if box is not None else []:
            if entry.get("Key") == key and (entry.text or "").strip():
                addr = entry.text.strip()
                if addr.lower().startswith("smtp:"):
                    addr = addr[5:]
                if "@" in addr:
                    c.emails.append(addr)
                else:
                    c.warnings.append("email-without-smtp-address")
    box = item.find(t_("PhoneNumbers"))
    for entry in box.findall(t_("Entry")) if box is not None else []:
        kind = PHONE_KEYS.get(entry.get("Key") or "")
        if kind and (entry.text or "").strip():
            c.phones.append((kind, entry.text.strip()))
    box = item.find(t_("PhysicalAddresses"))
    for entry in box.findall(t_("Entry")) if box is not None else []:
        kind = ADDRESS_KEYS.get(entry.get("Key") or "")
        if kind:
            c.addresses.append({"kind": kind, "street": _text(entry, "Street"), "city": _text(entry, "City"),
                                "state": _text(entry, "State"), "postal": _text(entry, "PostalCode"),
                                "country": _text(entry, "CountryOrRegion")})
    box = item.find(t_("ImAddresses"))
    for entry in box.findall(t_("Entry")) if box is not None else []:
        if entry.get("Key") == "ImAddress1" and entry.text:
            c.im = entry.text
    for name, attr in (("Birthday", "birthday"), ("WeddingAnniversary", "anniversary")):
        value = _text(item, name)
        if value:
            setattr(c, attr, _date_of(value))
    home = _text(item, "BusinessHomePage")
    if home:
        c.urls.append(home)
    if _bool(item, "HasPicture"):
        c.warnings.append("photo-not-copied")
    return c


def task_from_xml(item, uid: str) -> pim.Task:
    t = pim.Task(uid=uid, subject=_text(item, "Subject"), body=_text(item, "Body"), categories=_categories(item),
                 status=TASK_STATUS.get(_text(item, "Status"), "NEEDS-ACTION"),
                 priority={"High": "high", "Low": "low"}.get(_text(item, "Importance"), "normal"),
                 percent=_int((_text(item, "PercentComplete") or "").split(".")[0]),
                 start=pim.parse_utc(_text(item, "StartDate")), due=pim.parse_utc(_text(item, "DueDate")),
                 completed=pim.parse_utc(_text(item, "CompleteDate")),
                 created=pim.parse_utc(_text(item, "DateTimeCreated")),
                 modified=pim.parse_utc(_text(item, "LastModifiedTime")))
    t.recurrence = recurrence_from_xml(item.find(t_("Recurrence")), t.warnings)
    return t


# ------------------------------------------------------------------ neutral model -> EWS items
def _categories_xml(values: list[str]) -> str:
    if not values:
        return ""
    return "<t:Categories>" + "".join(f"<t:String>{_x(v)}</t:String>" for v in values) + "</t:Categories>"


def _instant(value: datetime | date, tz: str | None) -> str:
    if isinstance(value, datetime):
        return pim.utc_text(value)
    z = pim.zone(tz) or pim.UTC
    return pim.utc_text(datetime(value.year, value.month, value.day, tzinfo=z))


def _attendee_group(name: str, people: list[pim.Person]) -> str:
    if not people:
        return ""
    return f"<t:{name}>" + "".join(
        f"<t:Attendee><t:Mailbox><t:Name>{_x(p.name)}</t:Name><t:EmailAddress>{_x(p.email)}</t:EmailAddress>"
        "</t:Mailbox></t:Attendee>" for p in people) + f"</t:{name}>"


def event_xml(e: pim.Event, import_id: str) -> str:
    """Element order: the schema's (t:CalendarItemType). Exchange answers ErrorSchemaValidation otherwise."""
    win = pim.windows_zone(e.tz, e.start)
    tz = e.tz if win else None
    if e.tz and not win:
        xb.log.info("event %s: no Windows zone for %s, written in UTC", e.uid, e.tz)
    win = win or "UTC"
    parts = [f"<t:Subject>{_x(e.subject)}</t:Subject>", f"<t:Sensitivity>{EWS_SENS.get(e.sensitivity, 'Normal')}</t:Sensitivity>",
             f'<t:Body BodyType="Text">{_x(e.body)}</t:Body>', _categories_xml(e.categories)]
    if e.reminder_minutes is not None:
        parts.append(f"<t:ReminderIsSet>true</t:ReminderIsSet><t:ReminderMinutesBeforeStart>{e.reminder_minutes}"
                     "</t:ReminderMinutesBeforeStart>")
    else:
        parts.append("<t:ReminderIsSet>false</t:ReminderIsSet>")
    parts.append(_ext(IMPORT_URI, import_id))
    parts.append(f"<t:UID>{_x(e.uid)}</t:UID>")
    parts.append(f"<t:Start>{_instant(e.start, tz)}</t:Start><t:End>{_instant(e.end, tz)}</t:End>")
    parts.append(f"<t:IsAllDayEvent>{'true' if e.all_day else 'false'}</t:IsAllDayEvent>")
    parts.append(f"<t:LegacyFreeBusyStatus>{EWS_BUSY.get(e.free_busy, 'Busy')}</t:LegacyFreeBusyStatus>")
    if e.location:
        parts.append(f"<t:Location>{_x(e.location)}</t:Location>")
    for name, role in (("RequiredAttendees", "REQ"), ("OptionalAttendees", "OPT"), ("Resources", "RES")):
        parts.append(_attendee_group(name, [a for a in e.attendees if a.role == role]))
    if e.recurrence is not None:
        parts.append(recurrence_xml(e.recurrence, e.start, tz))
    parts.append(f'<t:StartTimeZone Id="{win}"/><t:EndTimeZone Id="{win}"/>')
    return "<t:CalendarItem>" + "".join(parts) + "</t:CalendarItem>"


def contact_xml(c: pim.Contact, import_id: str) -> str:
    """Element order: the schema's (t:ContactItemType)."""
    parts = []
    if c.note.strip():
        parts.append(f'<t:Body BodyType="Text">{_x(c.note.strip())}</t:Body>')
    parts.append(_categories_xml(c.categories))
    parts.append(_ext(IMPORT_URI, import_id))
    if c.prefix:  # CompleteName is read-only: PR_DISPLAY_NAME_PREFIX is what Exchange reads it from
        parts.append(_ext(_prop_uri("0x3A45", "String"), c.prefix))
    full = c.full_name or " ".join(x for x in (c.given, c.middle, c.surname) if x) or c.company or (
        c.emails[0] if c.emails else "Contact")
    parts.append(f"<t:FileAs>{_x(full)}</t:FileAs><t:DisplayName>{_x(full)}</t:DisplayName>")
    for name, value in (("GivenName", c.given), ("MiddleName", c.middle), ("Nickname", c.nickname),
                        ("CompanyName", c.company)):
        if value:
            parts.append(f"<t:{name}>{_x(value)}</t:{name}>")
    if c.emails:
        parts.append("<t:EmailAddresses>" + "".join(
            f'<t:Entry Key="EmailAddress{i + 1}">{_x(m)}</t:Entry>' for i, m in enumerate(c.emails[:3]))
            + "</t:EmailAddresses>")
        if len(c.emails) > 3:
            xb.log.info("contact %s: %d addresses, Exchange holds 3", c.uid, len(c.emails))
    addresses = [a for a in c.addresses if a.get("kind") in EWS_ADDRESS]
    if addresses:
        seen: set[str] = set()
        entries = []
        for a in addresses:
            key = EWS_ADDRESS[a["kind"]]
            if key in seen:
                continue
            seen.add(key)
            inner = "".join(f"<t:{tag}>{_x(a.get(k) or '')}</t:{tag}>" for tag, k in (
                ("Street", "street"), ("City", "city"), ("State", "state"), ("CountryOrRegion", "country"),
                ("PostalCode", "postal")) if a.get(k))
            entries.append(f'<t:Entry Key="{key}">{inner}</t:Entry>')
        parts.append("<t:PhysicalAddresses>" + "".join(entries) + "</t:PhysicalAddresses>")
    phones: dict[str, str] = {}
    for kind, number in c.phones:
        key = EWS_PHONE.get(kind)
        if key and key not in phones:
            phones[key] = number
    if phones:
        parts.append("<t:PhoneNumbers>" + "".join(f'<t:Entry Key="{k}">{_x(v)}</t:Entry>' for k, v in phones.items())
                     + "</t:PhoneNumbers>")
    if c.assistant:
        parts.append(f"<t:AssistantName>{_x(c.assistant)}</t:AssistantName>")
    if c.birthday:
        parts.append(f"<t:Birthday>{c.birthday.isoformat()}T00:00:00Z</t:Birthday>")
    if c.urls:
        parts.append(f"<t:BusinessHomePage>{_x(c.urls[0])}</t:BusinessHomePage>")
    if c.department:
        parts.append(f"<t:Department>{_x(c.department)}</t:Department>")
    if c.suffix:
        parts.append(f"<t:Generation>{_x(c.suffix)}</t:Generation>")
    if c.im:
        parts.append(f'<t:ImAddresses><t:Entry Key="ImAddress1">{_x(c.im)}</t:Entry></t:ImAddresses>')
    if c.title:
        parts.append(f"<t:JobTitle>{_x(c.title)}</t:JobTitle>")
    if c.profession:
        parts.append(f"<t:Profession>{_x(c.profession)}</t:Profession>")
    if c.spouse:
        parts.append(f"<t:SpouseName>{_x(c.spouse)}</t:SpouseName>")
    if c.surname:
        parts.append(f"<t:Surname>{_x(c.surname)}</t:Surname>")
    if c.anniversary:
        parts.append(f"<t:WeddingAnniversary>{c.anniversary.isoformat()}T00:00:00Z</t:WeddingAnniversary>")
    return "<t:Contact>" + "".join(parts) + "</t:Contact>"


def task_xml(t: pim.Task, import_id: str) -> str:
    """Element order: the schema's (t:TaskType)."""
    parts = [f"<t:Subject>{_x(t.subject)}</t:Subject>"]
    if t.body.strip():
        parts.append(f'<t:Body BodyType="Text">{_x(t.body.strip())}</t:Body>')
    parts.append(_categories_xml(t.categories))
    parts.append(f"<t:Importance>{ {'high': 'High', 'low': 'Low'}.get(t.priority, 'Normal') }</t:Importance>")
    parts.append(_ext(IMPORT_URI, import_id))
    if t.completed is not None:
        parts.append(f"<t:CompleteDate>{pim.utc_text(t.completed)}</t:CompleteDate>")
    if t.due is not None:
        parts.append(f"<t:DueDate>{_instant(t.due, None)}</t:DueDate>")
    if t.percent is not None:
        parts.append(f"<t:PercentComplete>{t.percent}</t:PercentComplete>")
    if t.recurrence is not None and (t.start or t.due) is not None:
        parts.append(recurrence_xml(t.recurrence, t.start or t.due, None))
    if t.start is not None:
        parts.append(f"<t:StartDate>{_instant(t.start, None)}</t:StartDate>")
    parts.append(f"<t:Status>{EWS_TASK_STATUS.get(t.status, 'NotStarted')}</t:Status>")
    return "<t:Task>" + "".join(parts) + "</t:Task>"


def mail_xml(raw: bytes, import_id: str, flags: list[str], when: datetime | None) -> str:
    """The MIME content, then what MIME does not carry: state of the message, flags, received date. Without
    PR_MESSAGE_FLAGS Exchange keeps a message created this way as an unsent draft."""
    w = xb.wanted_flags(flags)
    msg_flags = (MSGFLAG_READ if w["read"] else 0) | (MSGFLAG_UNSENT if w["draft"] else 0)
    parts = [f'<t:MimeContent CharacterSet="UTF-8">{base64.b64encode(raw).decode("ascii")}</t:MimeContent>',
             _ext(IMPORT_URI, import_id), _ext(_prop_uri(TAG_MSGFLAGS, "Integer"), msg_flags)]
    if when is not None:
        parts.append(_ext(_prop_uri(TAG_SUBMIT, "SystemTime"), pim.utc_text(when)))
        parts.append(_ext(_prop_uri(TAG_DELIVERY, "SystemTime"), pim.utc_text(when)))
    if w["flagged"]:
        parts.append(_ext(_prop_uri(TAG_FLAG, "Integer"), xb.FLAG_FLAGGED))
    if w["verb"]:
        parts.append(_ext(_prop_uri(TAG_VERB, "Integer"), w["verb"]))
    parts.append(f"<t:IsRead>{'true' if w['read'] else 'false'}</t:IsRead>")
    return "<t:Message>" + "".join(parts) + "</t:Message>"


# ------------------------------------------------------------------ source
_LIST_COMMON = ('<t:FieldURI FieldURI="item:ItemClass"/><t:FieldURI FieldURI="item:Size"/>')
_LIST_FIELDS = {
    MAIL: _LIST_COMMON + '<t:FieldURI FieldURI="item:DateTimeReceived"/><t:FieldURI FieldURI="message:IsRead"/>'
          '<t:FieldURI FieldURI="message:InternetMessageId"/><t:FieldURI FieldURI="item:IsDraft"/>'
          + _prop_uri(TAG_FLAG, "Integer") + _prop_uri(TAG_VERB, "Integer"),
    CALENDAR: _LIST_COMMON + '<t:FieldURI FieldURI="calendar:Start"/>',
    CONTACTS: _LIST_COMMON + '<t:FieldURI FieldURI="item:LastModifiedTime"/>',
    TASKS: _LIST_COMMON + '<t:FieldURI FieldURI="item:LastModifiedTime"/>',
}
_DATE_FIELD = {MAIL: "DateTimeReceived", CALENDAR: "Start", CONTACTS: "LastModifiedTime", TASKS: "LastModifiedTime"}


def _list_shape(kind: str) -> str:
    return f"<t:BaseShape>IdOnly</t:BaseShape><t:AdditionalProperties>{_LIST_FIELDS[kind]}</t:AdditionalProperties>"


class EwsSource(Source):
    def __init__(self, config: dict, secrets: dict, ctx: dict | None = None):
        self.config = config
        self.secrets = secrets
        self.client: EwsClient | None = None
        self.include_archive = bool(config.get("include_archive"))
        self._paths: dict[str, list[str]] = {}

    def open(self) -> None:
        client = EwsClient(self.config, self.secrets)
        try:
            client.one('<m:GetFolder><m:FolderShape><t:BaseShape>IdOnly</t:BaseShape></m:FolderShape><m:FolderIds>'
                       + client.distinguished("msgfolderroot") + "</m:FolderIds></m:GetFolder>", connecting=True)
        except ConnectorError:
            client.close()
            raise
        self.client = client

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        self.client = None

    def _c(self) -> EwsClient:
        if self.client is None:
            raise ConnectorError("EWS source not connected", ConnectorError.TRANSIENT)
        return self.client

    def _walk(self, parent: str, prefix: list[str], well_known: dict[str, str], skipped: set[str]) -> list[SourceFolder]:
        c = self._c()
        names: dict[str, tuple[str, str]] = {}  # id -> display name, parent id
        rows = c.find_folders(parent, deep=True)
        for el in rows:
            ident = el.find(t_("FolderId"))
            if ident is not None:
                up = el.find(t_("ParentFolderId"))
                names[ident.get("Id") or ""] = (_text(el, "DisplayName"), up.get("Id") if up is not None else "")

        def path_of(fid: str) -> list[str] | None:
            path, cur, hops = [], fid, 0
            while cur in names and hops < 64:
                name, cur = names[cur]
                path.insert(0, name)
                hops += 1
            return path if cur not in names else None

        out = []
        for el in rows:
            ident = el.find(t_("FolderId"))
            fid = ident.get("Id") if ident is not None else ""
            cls = _text(el, "FolderClass").lower()
            kind = KIND_OF_CLASS.get(cls)
            # subclasses (IPF.Contact.GalContacts, recipient caches...) are not the user's folders
            if (not fid or kind is None or fid in skipped or _local(el.tag) == "SearchFolder"
                    or _ext_values(el).get(int(TAG_HIDDEN, 0), "").lower() == "true"):
                continue
            path = path_of(fid)
            up = el.find(t_("ParentFolderId"))
            if not path or (up is not None and up.get("Id") in skipped):
                continue
            special = None
            name = well_known.get(fid)
            if name in WELL_KNOWN_SPECIAL:
                special = WELL_KNOWN_SPECIAL[name]
            elif name in WELL_KNOWN_DEFAULT:
                special = xb.DEFAULT
            out.append(SourceFolder(path=prefix + path, kind=kind, special=special,
                                    count=_int(_text(el, "TotalCount")), ref=fid))
        return out

    def folders(self) -> list[SourceFolder]:
        c = self._c()
        ids = c.get_folder_ids(list(WELL_KNOWN_SPECIAL) + list(WELL_KNOWN_DEFAULT) + list(WELL_KNOWN_SKIPPED))
        well_known = {v: k for k, v in ids.items()}
        skipped = {ids[n] for n in WELL_KNOWN_SKIPPED if n in ids}
        out = self._walk(c.distinguished("msgfolderroot"), [], well_known, skipped)
        if self.include_archive:
            try:
                out.extend(self._walk(c.distinguished("archivemsgfolderroot"), ["Online Archive"], {}, set()))
            except ConnectorError as e:
                if e.kind != ConnectorError.FOLDER_MISSING:
                    raise
        out.sort(key=lambda f: (f.kind, f.path))
        self._paths = {f.ref: f.path for f in out}
        return out

    def _item_of(self, folder: SourceFolder, el) -> SourceItem | None:
        ident = el.find(t_("ItemId"))
        if ident is None:
            return None
        rid = ident.get("Id") or ""
        when = pim.parse_utc(_text(el, _DATE_FIELD[folder.kind]))
        size = _int(_text(el, "Size"))
        key = xb.item_key(folder.path, rid)
        if folder.kind != MAIL:
            return SourceItem(key=key, date=when, size=size, ref=rid)
        ext = _ext_values(el)
        flags = xb.flags_of(_bool(el, "IsRead"), _int(ext.get(int(TAG_FLAG, 0))) == xb.FLAG_FLAGGED,
                            _int(ext.get(int(TAG_VERB, 0))), _bool(el, "IsDraft"))
        mid = " ".join(_text(el, "InternetMessageId").split()) or None
        return SourceItem(key=key, alt_key=xb.alt_key(mid, when, size), date=when, size=size, flags=flags, ref=rid)

    def iter_items(self, folder: SourceFolder) -> Iterator[SourceItem]:
        c = self._c()
        offset = 0
        order = "item:DateTimeCreated"
        while True:
            msg = c.one(f'<m:FindItem Traversal="Shallow"><m:ItemShape>{_list_shape(folder.kind)}</m:ItemShape>'
                        f'<m:IndexedPageItemView MaxEntriesReturned="{PAGE}" Offset="{offset}" BasePoint="Beginning"/>'
                        f'<m:SortOrder><t:FieldOrder Order="Ascending"><t:FieldURI FieldURI="{order}"/></t:FieldOrder>'
                        f"</m:SortOrder><m:ParentFolderIds><t:FolderId Id={quoteattr(folder.ref)}/></m:ParentFolderIds>"
                        "</m:FindItem>")
            root = msg.find(m_("RootFolder"))
            if root is None:
                return
            box = root.find(t_("Items"))
            got = list(box) if box is not None else []
            for el in got:
                it = self._item_of(folder, el)
                if it is not None:
                    yield it
            if root.get("IncludesLastItemInRange", "true").lower() == "true" or not got:
                return
            offset += len(got)

    def changed_since(self, folder: SourceFolder, cursor: str | None) -> Changes:
        """SyncFolderItems: created and changed items since the sync state. Deleted ones are not reported."""
        c = self._c()
        state = cursor or ""
        reset = not cursor
        items: list[SourceItem] = []
        while True:
            state_xml = f"<m:SyncState>{_x(state)}</m:SyncState>" if state else ""
            try:
                msg = c.one(f"<m:SyncFolderItems><m:ItemShape>{_list_shape(folder.kind)}</m:ItemShape><m:SyncFolderId>"
                            f"<t:FolderId Id={quoteattr(folder.ref)}/></m:SyncFolderId>{state_xml}"
                            f"<m:MaxChangesReturned>{SYNC_PAGE}</m:MaxChangesReturned></m:SyncFolderItems>")
            except ConnectorError as e:
                if state and any(code in str(e) for code in BAD_SYNC_CODES):
                    state, reset, items = "", True, []  # the state is no longer valid: everything again
                    continue
                raise
            state = msg.findtext(m_("SyncState")) or state
            box = msg.find(m_("Changes"))
            for change in box if box is not None else []:
                if _local(change.tag) not in ("Create", "Update"):
                    continue  # a read flag change or a deletion: nothing to copy in one-way migration
                for el in change:
                    it = self._item_of(folder, el)
                    if it is not None:
                        items.append(it)
            if (msg.findtext(m_("IncludesLastItemInRange")) or "true").lower() == "true":
                break
        return Changes(items, state or None, reset=reset)

    def fetch(self, folder: SourceFolder, item: SourceItem, import_id: str) -> FetchedItem | None:
        if folder.kind == MAIL:
            return self._fetch_mail(item)
        if folder.kind == CALENDAR:
            return self._fetch_event(item)
        el = self._get(item.ref, "<t:BaseShape>AllProperties</t:BaseShape><t:BodyType>Text</t:BodyType>")
        if el is None or _local(el.tag) not in ("Contact", "Task"):
            return None  # gone, or a distribution list
        uid = "ews-" + xb.item_key(folder.path, item.ref)
        if folder.kind == CONTACTS:
            contact = contact_from_xml(el, uid)
            return FetchedItem(raw=pim.contact_to_vcard(contact), subject=contact.full_name, warnings=contact.warnings)
        task = task_from_xml(el, uid)
        return FetchedItem(raw=pim.task_to_ics(task), subject=task.subject, warnings=task.warnings)

    def _get(self, rid: str, shape: str):
        return self._c().get_items([rid], shape)[0][1]

    def _fetch_mail(self, item: SourceItem) -> FetchedItem | None:
        shape = ("<t:BaseShape>IdOnly</t:BaseShape><t:IncludeMimeContent>true</t:IncludeMimeContent>"
                 "<t:AdditionalProperties>" + _LIST_FIELDS[MAIL] + "</t:AdditionalProperties>")
        el = self._get(item.ref, shape)
        if el is None:
            return None
        mime = _text(el, "MimeContent")
        if not mime:
            raise ConnectorError("no MIME content for this item", ConnectorError.REJECTED)
        raw = base64.b64decode(mime)
        ext = _ext_values(el)
        flags = xb.flags_of(_bool(el, "IsRead"), _int(ext.get(int(TAG_FLAG, 0))) == xb.FLAG_FLAGGED,
                            _int(ext.get(int(TAG_VERB, 0))), _bool(el, "IsDraft"))
        return FetchedItem(raw=raw, internal_date=pim.parse_utc(_text(el, "DateTimeReceived")) or item.date, flags=flags,
                           subject=subject_of(raw))

    def _fetch_event(self, item: SourceItem) -> FetchedItem | None:
        shape = "<t:BaseShape>AllProperties</t:BaseShape><t:BodyType>Text</t:BodyType>"
        el = self._get(item.ref, shape)
        if el is None or _local(el.tag) != "CalendarItem":
            return None
        warnings: list[str] = []
        e = event_from_xml(el, warnings)
        box = el.find(t_("DeletedOccurrences"))
        for occ in box.findall(t_("DeletedOccurrence")) if box is not None else []:
            when = pim.parse_utc(_text(occ, "Start"))
            if when is not None:
                e.exdates.append(pim.all_day_date(when, e.tz) if e.all_day else when)
        box = el.find(t_("ModifiedOccurrences"))
        ids = []
        originals: dict[str, datetime | None] = {}
        for occ in box.findall(t_("Occurrence")) if box is not None else []:
            ident = occ.find(t_("ItemId"))
            if ident is not None:
                ids.append(ident.get("Id") or "")
                originals[ids[-1]] = pim.parse_utc(_text(occ, "OriginalStart"))
        for i in range(0, len(ids), GET_BATCH):
            for rid, sub in self._c().get_items(ids[i:i + GET_BATCH], shape):
                if sub is None:
                    warnings.append("exception-gone")
                    continue
                o = event_from_xml(sub, warnings, e.tz)
                o.tz = e.tz
                o.recurrence = None
                original = originals.get(rid) or pim.parse_utc(_text(sub, "OriginalStart"))
                if original is None:
                    warnings.append("exception-without-original-start")
                    continue
                o.recurrence_id = pim.all_day_date(original, e.tz) if e.all_day else original
                e.overrides.append(o)
        raw = pim.event_to_ics(e)
        return FetchedItem(raw=raw, subject=e.subject, warnings=warnings)

    def read_rules(self) -> list[dict]:
        from .sieve import rules_from_ews
        c = self._c()
        address = c.mailbox or c.login
        resp = c.call(f"<m:GetInboxRules><m:MailboxSmtpAddress>{_x(address)}</m:MailboxSmtpAddress></m:GetInboxRules>")
        c.check(resp)
        paths = {f.ref: f.path for f in self.folders()}
        return rules_from_ews(resp, paths)


# ------------------------------------------------------------------ target
class EwsSession(TargetSession):
    def __init__(self, config: dict, secrets: dict, root: str | None):
        self.config = config
        self.secrets = secrets
        self.root = root
        self.time_zone = config.get("time_zone") or None
        self.client: EwsClient | None = None
        self._ids: dict[str, str] = {}  # folder path text -> id

    @property
    def connected(self) -> bool:
        return self.client is not None

    def connect(self) -> None:
        self.close()
        client = EwsClient(self.config, self.secrets)
        try:
            client.one('<m:GetFolder><m:FolderShape><t:BaseShape>IdOnly</t:BaseShape></m:FolderShape><m:FolderIds>'
                       + client.distinguished("msgfolderroot") + "</m:FolderIds></m:GetFolder>", connecting=True)
        except ConnectorError:
            client.close()
            raise
        self.client = client

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        self.client = None

    def _c(self) -> EwsClient:
        self.ensure()
        return self.client  # type: ignore[return-value]

    def plan_folders(self, folders: list[SourceFolder]) -> dict[str, FolderPlan]:
        return xb.plan(folders, self.root)

    # -- folders
    def _well_known(self, kind: str, name: str) -> str:
        if name == xb.DEFAULT:
            name = {CALENDAR: "calendar", CONTACTS: "contacts", TASKS: "tasks"}.get(kind, "inbox")
        return self._c().distinguished(name)

    def _child(self, parent_xml: str, name: str) -> str | None:
        for el in self._c().find_folders(parent_xml, deep=False):
            if _text(el, "DisplayName").casefold() == name.casefold():
                ident = el.find(t_("FolderId"))
                if ident is not None:
                    return ident.get("Id")
        return None

    def _folder_xml(self, spec: str, create: bool = False) -> str:
        kind, well_known, path = xb.parse_spec(spec)
        if well_known:
            return self._well_known(kind, well_known)
        parent = self._c().distinguished("msgfolderroot")
        for i, name in enumerate(path):
            cache = "\0".join([kind] + path[:i + 1])
            fid = self._ids.get(cache) or self._child(parent, name)
            if fid is None:
                if not create:
                    raise ConnectorError(f"EWS folder '{'/'.join(path[:i + 1])}' not found", ConnectorError.FOLDER_MISSING)
                fid = self._create_folder(parent, name, kind if i == len(path) - 1 else MAIL)
            self._ids[cache] = fid
            parent = f"<t:FolderId Id={quoteattr(fid)}/>"
        return parent

    def _create_folder(self, parent: str, name: str, kind: str) -> str:
        tag = {CALENDAR: "CalendarFolder", CONTACTS: "ContactsFolder", TASKS: "TasksFolder"}.get(kind, "Folder")
        klass = "" if tag != "Folder" else "<t:FolderClass>IPF.Note</t:FolderClass>"
        c = self._c()
        resp = c.call(f"<m:CreateFolder><m:ParentFolderId>{parent}</m:ParentFolderId><m:Folders><t:{tag}>{klass}"
                      f"<t:DisplayName>{_x(name)}</t:DisplayName></t:{tag}></m:Folders></m:CreateFolder>")
        msg = c.messages(resp)[0]
        if msg.get("ResponseClass") == "Error" and msg.findtext(m_("ResponseCode")) == "ErrorFolderExists":
            fid = self._child(parent, name)  # another session has just created it
            if fid:
                return fid
        c.check(msg)
        for el in msg.iter(t_("FolderId")):
            return el.get("Id") or ""
        raise ConnectorError(f"EWS: no id for the new folder '{name}'", ConnectorError.TRANSIENT)

    def ensure_folder(self, name: str, flag: str | None = None) -> None:
        self._ids.clear()  # called when a folder went missing: nothing known about the tree can be trusted
        self._folder_xml(name, create=True)

    # -- writing
    def put(self, folder: str, item: OutgoingItem) -> str | None:
        target = self._folder_xml(folder)
        if item.kind == MAIL:
            return self._create(target, "SaveOnly", mail_xml(item.raw, item.import_id, item.flags, item.internal_date))
        if item.kind == CONTACTS:
            contact = pim.vcard_to_contact(item.raw)
            return self._create(target, None, contact_xml(contact, item.import_id))
        if item.kind == TASKS:
            task = pim.ics_to_task(item.raw)
            return self._create(target, None, task_xml(task, item.import_id))
        event = pim.ics_to_event(item.raw)
        if event.all_day and not event.tz:
            event.tz = self.time_zone
        return self._put_event(target, event, item.import_id)

    def _create(self, target: str, disposition: str | None, xml: str) -> str | None:
        attrs = f' MessageDisposition="{disposition}"' if disposition else ""
        if xml.startswith("<t:CalendarItem>"):
            attrs = ' SendMeetingInvitations="SendToNone"'  # never mail anyone about an event being migrated
        msg = self._c().one(f"<m:CreateItem{attrs}><m:SavedItemFolderId>{target}</m:SavedItemFolderId><m:Items>{xml}"
                            "</m:Items></m:CreateItem>")
        for el in msg.iter(t_("ItemId")):
            return el.get("Id")
        return None

    def _delete(self, rid: str) -> None:
        self._c().one('<m:DeleteItem DeleteType="HardDelete" SendMeetingCancellations="SendToNone" '
                      f'AffectedTaskOccurrences="AllOccurrences"><m:ItemIds><t:ItemId Id={quoteattr(rid)}/></m:ItemIds>'
                      "</m:DeleteItem>")

    def _occurrence(self, target: str, e: pim.Event, original: datetime | date) -> str | None:
        """Id of the occurrence of the series `e` whose original start is `original`."""
        anchor = original if isinstance(original, datetime) else pim.date_at_midnight(original)
        begin, end = anchor - timedelta(days=2), anchor + timedelta(days=2)
        msg = self._c().one(
            '<m:FindItem Traversal="Shallow"><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape><t:AdditionalProperties>'
            '<t:FieldURI FieldURI="calendar:UID"/><t:FieldURI FieldURI="calendar:Start"/>'
            '<t:FieldURI FieldURI="calendar:OriginalStart"/></t:AdditionalProperties></m:ItemShape>'
            f'<m:CalendarView MaxEntriesReturned="200" StartDate="{pim.utc_text(begin)}" EndDate="{pim.utc_text(end)}"/>'
            f"<m:ParentFolderIds>{target}</m:ParentFolderIds></m:FindItem>")
        root = msg.find(m_("RootFolder"))
        box = root.find(t_("Items")) if root is not None else None
        for el in box if box is not None else []:
            if _text(el, "UID") != e.uid:
                continue
            when = pim.parse_utc(_text(el, "OriginalStart") or _text(el, "Start"))
            if when is None:
                continue
            same = (when == original) if isinstance(original, datetime) else pim.all_day_date(when, e.tz) == original
            if same:
                ident = el.find(t_("ItemId"))
                return ident.get("Id") if ident is not None else None
        return None

    def _update_occurrence(self, rid: str, o: pim.Event, tz: str | None) -> None:
        def field(uri: str, inner: str) -> str:
            return f'<t:SetItemField><t:FieldURI FieldURI="{uri}"/><t:CalendarItem>{inner}</t:CalendarItem></t:SetItemField>'
        updates = [field("item:Subject", f"<t:Subject>{_x(o.subject)}</t:Subject>"),
                   field("calendar:Start", f"<t:Start>{_instant(o.start, tz)}</t:Start>"),
                   field("calendar:End", f"<t:End>{_instant(o.end, tz)}</t:End>"),
                   field("item:Body", f'<t:Body BodyType="Text">{_x(o.body)}</t:Body>'),
                   field("calendar:Location", f"<t:Location>{_x(o.location)}</t:Location>")]
        self._c().one('<m:UpdateItem ConflictResolution="AlwaysOverwrite" SendMeetingInvitationsOrCancellations="SendToNone">'
                      f"<m:ItemChanges><t:ItemChange><t:ItemId Id={quoteattr(rid)}/><t:Updates>" + "".join(updates)
                      + "</t:Updates></t:ItemChange></m:ItemChanges></m:UpdateItem>")

    def _put_event(self, target: str, e: pim.Event, import_id: str) -> str | None:
        """The series, then its exceptions. All or nothing: a series left without its exceptions would be taken as
        migrated by find(), so it is removed if any of them fails, and the whole item is tried again."""
        rid = self._create(target, None, event_xml(e, import_id))
        if rid is None or e.recurrence is None or not (e.exdates or e.overrides):
            return rid
        try:
            for original in e.exdates:
                occ = self._occurrence(target, e, original)
                if occ is not None:
                    self._delete(occ)
            for o in e.overrides:
                occ = self._occurrence(target, e, o.recurrence_id)
                if occ is None:
                    xb.log.info("event %s: occurrence %s not found", e.uid, o.recurrence_id)
                    continue
                self._update_occurrence(occ, o, e.tz)
        except ConnectorError:
            try:
                self._delete(rid)
            except ConnectorError as de:
                xb.log.warning("event %s: series left without its exceptions: %s", e.uid, de)
            raise
        return rid

    # -- lookups
    def _restriction_ids(self, target: str, restriction: str, limit: int, wanted: str | None = None) -> tuple[list[str], int]:
        msg = self._c().one(
            f'<m:FindItem Traversal="Shallow"><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape><t:AdditionalProperties>'
            f"{IMPORT_URI}</t:AdditionalProperties></m:ItemShape>"
            f'<m:IndexedPageItemView MaxEntriesReturned="{limit}" Offset="0" BasePoint="Beginning"/>'
            f"<m:Restriction>{restriction}</m:Restriction><m:ParentFolderIds>{target}</m:ParentFolderIds></m:FindItem>")
        root = msg.find(m_("RootFolder"))
        if root is None:
            return [], 0
        box = root.find(t_("Items"))
        ids = []
        for el in box if box is not None else []:
            ident = el.find(t_("ItemId"))
            value = _ext_values(el).get(xb.IMPORT_PROP)
            if ident is not None and (wanted is None or value == wanted):
                ids.append(ident.get("Id") or "")
        return ids, _int(root.get("TotalItemsInView")) or 0

    def find(self, folder: str, item: OutgoingItem) -> list[str]:
        target = self._folder_xml(folder)
        value = quoteattr(item.import_id)
        restriction = (f"<t:IsEqualTo>{IMPORT_URI}<t:FieldURIOrConstant><t:Constant Value={value}/></t:FieldURIOrConstant>"
                       "</t:IsEqualTo>")
        # the comparison of Exchange ignores case: the answer is compared exactly
        return self._restriction_ids(target, restriction, 20, item.import_id)[0]

    def count_ours(self, folder: str, namespace: str, names: set[str] | None = None) -> int:
        target = self._folder_xml(folder)
        value = quoteattr(f"<{namespace}-")
        restriction = (f'<t:Contains ContainmentMode="Prefixed" ContainmentComparison="IgnoreCase">{IMPORT_URI}'
                       f"<t:Constant Value={value}/></t:Contains>")
        return self._restriction_ids(target, restriction, 1)[1]

    def info(self) -> dict:
        return {"protocol": "EWS", "mailbox": self.config.get("mailbox") or self.config.get("login")}


class EwsTarget(Target):
    # no CAP_READBACK: Exchange rewrites the MIME it stores, a re-read never hashes the same
    capabilities = frozenset({CAP_FOLDERS, CAP_FLAGS, CAP_INTERNAL_DATE, CAP_PARALLEL})

    def __init__(self, config: dict, secrets: dict, ctx: dict | None = None):
        self.config = config
        self.secrets = secrets
        self.root = (config.get("root_folder") or "").strip() or None

    def session(self) -> EwsSession:
        return EwsSession(self.config, self.secrets, self.root)


def identity(config: dict) -> str:
    box = config.get("mailbox") if (config.get("access") or "own") != "own" else config.get("login")
    return f"{str(config.get('url', '')).lower()}:{str(box or '').lower()}"


_FIELDS = (
    registry.Field("url", "EWS URL", "url", required=True, help="Usually https://mail.example.com/EWS/Exchange.asmx"),
    registry.Field("login", "Login", required=True, help="DOMAIN\\user or user@example.com"),
    registry.Field("password", "Password", "password", required=True, secret=True),
    registry.Field("auth", "Authentication", "select", default="auto", options=("auto", "ntlm", "basic"),
                   help="auto follows what the server asks for. Basic is refused over plain HTTP."),
    registry.Field("access", "Access", "select", default="own", options=("own", "impersonation", "delegate"),
                   help="impersonation: one account with the ApplicationImpersonation role reaches every mailbox. "
                        "delegate: the login has rights on a shared mailbox."),
    registry.Field("mailbox", "Mailbox address", help="SMTP address of the mailbox, for impersonation or delegate."),
    registry.Field("server_version", "Server version", "select", default="Exchange2013",
                   options=("Exchange2010_SP2", "Exchange2013", "Exchange2016")),
    registry.Field("verify_tls", "Verify the TLS certificate", "boolean", default=True),
)
_SOURCE_FIELDS = _FIELDS + (registry.Field("include_archive", "Include the online archive", "boolean", default=False,
                                           help="Its folders are copied under 'Online Archive'."),)
_TARGET_FIELDS = _FIELDS + (
    registry.Field("root_folder", "Root folder", help="Put everything but the well-known folders under this folder."),
    registry.Field("time_zone", "Time zone of all-day events",
                   help="IANA name, such as Europe/Paris, for all-day events that do not say. Empty: UTC."),
)
_ITEMS = (MAIL, CALENDAR, CONTACTS, TASKS)

registry.register(registry.ConnectorSpec(
    kind="exchange_ews", label="Exchange on premises (EWS)",
    description="Exchange Server 2010 SP2 to 2019 through EWS. Tested against simulated servers, not real Exchange.",
    source=registry.Role(EwsSource, _ITEMS,
                         frozenset({CAP_FOLDERS, CAP_FLAGS, CAP_INTERNAL_DATE, CAP_INCREMENTAL, CAP_RULES}),
                         _SOURCE_FIELDS, identity),
    target=registry.Role(EwsTarget, _ITEMS, EwsTarget.capabilities, _TARGET_FIELDS, identity),
))
