"""Google account as a source: Gmail over IMAP (labels become folders, a mail carried by several labels is copied
once), Google Calendar and People APIs. Access is either a service account with domain-wide delegation (Google
Workspace) or a per-user OAuth refresh token. See docs/protocols.md. Tested against simulated servers only."""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx

from ..server.imap import ImapConn, ImapError, ImapParams, Mailbox, _text, utf7_decode
from . import registry
from .base import (CALENDAR, CAP_FLAGS, CAP_FOLDERS, CAP_INCREMENTAL, CAP_INTERNAL_DATE, CONTACTS, MAIL, Changes,
                   ConnectorError, FetchedItem, Source, SourceFolder, SourceItem, subject_of)
from .convert import esc, join_lines
from .imap import _digest, keep_flags, parse_internaldate
from .jmap import key_of

TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
PEOPLE_API = "https://people.googleapis.com/v1"
GMAIL_HOST, GMAIL_PORT, GMAIL_SECURITY = "imap.gmail.com", 993, "ssl"
# Workspace delegation can use gmail.imap_admin, which shows every label whatever the user's IMAP settings
SCOPES = {"mail": "https://mail.google.com/", "calendar": "https://www.googleapis.com/auth/calendar.readonly",
          "contacts": "https://www.googleapis.com/auth/contacts.readonly"}
SCOPE_MAIL_ADMIN = "https://www.googleapis.com/auth/gmail.imap_admin"
PERSON_FIELDS = "names,emailAddresses,phoneNumbers,addresses,organizations,birthdays,urls,biographies,nicknames,metadata"
FETCH_CHUNK = 500


# ------------------------------------------------------------------ authentication
def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_jwt(key: dict, claims: dict) -> str:
    """RS256 JWT of a service account key (RFC 7515, Google's service-account flow)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    header = {"alg": "RS256", "typ": "JWT"}
    if key.get("private_key_id"):
        header["kid"] = key["private_key_id"]
    signing_input = b64url(json.dumps(header, separators=(",", ":")).encode()) + "." + \
        b64url(json.dumps(claims, separators=(",", ":")).encode())
    try:
        pk = serialization.load_pem_private_key(key["private_key"].encode("utf-8"), password=None)
        sig = pk.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    except (ValueError, TypeError, KeyError) as e:
        raise ConnectorError(f"unusable service account key: {e}", ConnectorError.CONFIG) from e
    return signing_input + "." + b64url(sig)


class GoogleAuth:
    """Access tokens for a user, one per scope set, cached until a minute before they expire."""

    def __init__(self, config: dict, secrets: dict, transport: httpx.BaseTransport | None = None):
        self.user = config["user"]
        self.mode = config.get("auth") or "service_account"
        self.client_id = config.get("client_id") or ""
        self.secrets = secrets
        self.http = httpx.Client(timeout=60.0, transport=transport)
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()
        self.key: dict = {}
        if self.mode == "service_account":
            try:
                self.key = json.loads(secrets.get("service_account_key") or "")
                self.key["client_email"], self.key["private_key"]
            except (ValueError, KeyError, TypeError):
                raise ConnectorError("service_account_key must be the JSON key file of the service account",
                                     ConnectorError.CONFIG) from None
        elif not (self.client_id and secrets.get("client_secret") and secrets.get("refresh_token")):
            raise ConnectorError("OAuth needs client_id, client_secret and refresh_token", ConnectorError.CONFIG)

    def close(self) -> None:
        self.http.close()

    def token(self, scopes: list[str], fresh: bool = False) -> str:
        name = " ".join(scopes)
        with self._lock:
            hit = self._cache.get(name)
            if hit and not fresh and hit[1] > time.time() + 60:
                return hit[0]
            tok, ttl = self._request(scopes)
            self._cache[name] = (tok, time.time() + ttl)
            return tok

    def _request(self, scopes: list[str]) -> tuple[str, float]:
        if self.mode == "service_account":
            now = int(time.time())
            claims = {"iss": self.key["client_email"], "sub": self.user, "scope": " ".join(scopes),
                      "aud": TOKEN_URL, "iat": now, "exp": now + 3600}
            data = {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": sign_jwt(self.key, claims)}
        else:
            data = {"grant_type": "refresh_token", "client_id": self.client_id,
                    "client_secret": self.secrets["client_secret"], "refresh_token": self.secrets["refresh_token"]}
        try:
            resp = self.http.post(TOKEN_URL, data=data)
        except httpx.HTTPError as e:
            raise ConnectorError(f"token endpoint: {type(e).__name__}: {e}", ConnectorError.UNREACHABLE) from e
        if resp.status_code >= 500 or resp.status_code == 429:
            raise ConnectorError(f"token endpoint -> HTTP {resp.status_code}", ConnectorError.TRANSIENT)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or "access_token" not in body:
            err = f"{body.get('error', 'error')}: {body.get('error_description', '')}".strip()
            hint = " (is the client id authorized for these scopes in the Workspace admin console?)" \
                if body.get("error") == "unauthorized_client" else ""
            raise ConnectorError(f"Google refused the credentials: {err}{hint}", ConnectorError.AUTH)
        return body["access_token"], float(body.get("expires_in") or 3600)


class GoogleApi:
    """JSON GET on the Calendar and People APIs with the right token."""

    def __init__(self, auth: GoogleAuth, transport: httpx.BaseTransport | None = None):
        self.auth = auth
        self.http = httpx.Client(timeout=120.0, transport=transport)

    def close(self) -> None:
        self.http.close()

    def get(self, url: str, params: dict, service: str) -> dict:
        scopes = [SCOPES[service]]
        for attempt in (0, 1):
            tok = self.auth.token(scopes, fresh=attempt == 1)
            try:
                resp = self.http.get(url, params=params, headers={"Authorization": f"Bearer {tok}"})
            except httpx.HTTPError as e:
                raise ConnectorError(f"{url}: {type(e).__name__}: {e}", ConnectorError.UNREACHABLE) from e
            if resp.status_code == 401 and attempt == 0:
                continue
            break
        sc = resp.status_code
        if sc == 200:
            return resp.json()
        try:
            err = resp.json().get("error", {})
        except ValueError:
            err = {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
        reasons = " ".join(str(e.get("reason", "")) for e in (err.get("errors") or [])) if isinstance(err, dict) else ""
        status = err.get("status", "") if isinstance(err, dict) else ""
        text = f"{url.split('?')[0]} -> HTTP {sc} {msg or ''}".strip()
        if sc == 401:
            raise ConnectorError(text, ConnectorError.AUTH)
        if sc == 429 or sc >= 500 or (sc == 403 and re.search(r"rateLimit|quotaExceeded|userRateLimit", reasons + status)):
            raise ConnectorError(text, ConnectorError.TRANSIENT)
        if sc == 403:
            raise ConnectorError(text + " (is the API enabled in the Google Cloud project, and the scope granted?)",
                                 ConnectorError.CONFIG)
        if sc in (400, 410):
            e = ConnectorError(text, ConnectorError.REJECTED)
            e.gone = sc == 410 or "EXPIRED_SYNC_TOKEN" in json.dumps(err)  # type: ignore[attr-defined]
            raise e
        raise ConnectorError(text, ConnectorError.REJECTED)


def _gone(e: ConnectorError) -> bool:
    return bool(getattr(e, "gone", False))


# ------------------------------------------------------------------ Gmail labels
def parse_labels(text: str) -> list[str]:
    """Labels of an `X-GM-LABELS (...)` list, given the text after the opening parenthesis: atoms and quoted strings
    (RFC 3501 astring), modified UTF-7 decoded. Stops at the closing parenthesis."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(text[j])
                j += 1
            out.append("".join(buf))
            i = j + 1
        elif c == ")":
            break
        elif c in " \t":
            i += 1
        else:
            j = i
            while j < n and text[j] not in ' \t")':
                j += 1
            out.append(text[i:j])
            i = j
    return [x if x.startswith("\\") else utf7_decode(x) for x in out]


def folder_of_labels(labels: list[str]) -> tuple[list[str], str | None]:
    """(folder path, special) a mail with these labels is copied to. One folder only, chosen the same way on every
    run: drafts, inbox, sent, then the first user label by name, else Archive."""
    lows = {x.lower() for x in labels}
    if "\\drafts" in lows:
        return ["Drafts"], "drafts"
    if "\\inbox" in lows:
        return ["INBOX"], "inbox"
    if "\\sent" in lows:
        return ["Sent"], "sent"
    user = sorted((x for x in labels if not x.startswith("\\") and not x.upper().startswith("CATEGORY_")),
                  key=lambda x: (x.lower(), x))
    if user:
        return [p for p in user[0].split("/") if p] or [user[0]], None
    return ["Archive"], None


def parse_scan(data: list) -> list[dict]:
    """Records {uid, flags, date, size, msgid, labels} of a `UID FETCH (UID FLAGS INTERNALDATE RFC822.SIZE X-GM-MSGID
    X-GM-LABELS)` response. A label that needs a literal arrives split by imaplib and is stitched back."""
    recs: list[str] = []
    for d in data or []:
        if isinstance(d, tuple):
            meta = (d[0] or b"").decode("utf-8", "replace")
            lit = (d[1] or b"").decode("utf-8", "replace")
            meta = re.sub(r"\{\d+\}$", "", meta) + '"' + lit.replace("\\", "\\\\").replace('"', '\\"') + '"'
            if recs and not re.match(r"^\d+ \(", meta):
                recs[-1] += meta
            else:
                recs.append(meta)
        elif isinstance(d, bytes):
            t = d.decode("utf-8", "replace")
            if re.match(r"^\d+ \(", t) or not recs:
                recs.append(t)
            else:
                recs[-1] += t
    out = []
    for t in recs:
        uid = re.search(r"\bUID (\d+)", t)
        mid = re.search(r"\bX-GM-MSGID (\d+)", t)
        if not uid or not mid:
            continue
        flags = re.search(r"\bFLAGS \(([^)]*)\)", t)
        size = re.search(r"\bRFC822\.SIZE (\d+)", t)
        date_ = re.search(r'\bINTERNALDATE "([^"]+)"', t)
        lab = re.search(r"\bX-GM-LABELS \((.*)$", t)
        out.append({"uid": int(uid.group(1)), "msgid": mid.group(1), "flags": flags.group(1).split() if flags else [],
                    "size": int(size.group(1)) if size else None,
                    "date": parse_internaldate(date_.group(1)) if date_ else None,
                    "labels": parse_labels(lab.group(1)) if lab else []})
    return out


class GmailImap:
    """The few IMAP operations the Google source needs. The tests replace this class."""

    def __init__(self, params: ImapParams):
        self.conn = ImapConn(params)

    def connect(self) -> None:
        try:
            self.conn.connect()
        except ImapError as e:
            raise ConnectorError(str(e), {"auth": ConnectorError.AUTH, "temp": ConnectorError.UNREACHABLE}.get(
                e.kind, ConnectorError.CONFIG)) from e

    def close(self) -> None:
        self.conn.close()

    def mailboxes(self) -> list[Mailbox]:
        return self._guard(self.conn.list)

    def _guard(self, fn, *a):
        try:
            return fn(*a)
        except ImapError as e:
            raise ConnectorError(str(e), ConnectorError.TRANSIENT if e.temporary else ConnectorError.REJECTED) from e
        except Exception as e:  # noqa: BLE001 - imaplib errors are not typed
            from ..server.imap import wrap_exc
            w = wrap_exc(e)
            raise ConnectorError(str(w), ConnectorError.TRANSIENT if w.temporary else ConnectorError.REJECTED) from e

    def scan(self, name: str, after_uid: int = 0) -> tuple[int, list[dict]]:
        """(UIDVALIDITY, records) of the messages of a mailbox with a UID above `after_uid`, read-only."""
        def run():
            c = self.conn
            c.select(name, readonly=True)
            m = c._m()
            typ, data = m.response("UIDVALIDITY")
            uv = int(re.search(r"\d+", _text(data)).group(0)) if re.search(r"\d+", _text(data)) else 0
            typ, data = m.uid("SEARCH", "UID", f"{after_uid + 1}:*") if after_uid else m.uid("SEARCH", "ALL")
            uids = sorted(u for u in (int(x) for x in _text(c._check(typ, data, "SEARCH")).split() if x.isdigit())
                          if u > after_uid)
            recs: list[dict] = []
            for i in range(0, len(uids), FETCH_CHUNK):
                chunk = ",".join(str(u) for u in uids[i:i + FETCH_CHUNK])
                typ, data = m.uid("FETCH", chunk, "(UID FLAGS INTERNALDATE RFC822.SIZE X-GM-MSGID X-GM-LABELS)")
                recs += parse_scan(c._check(typ, data, "FETCH"))
            return uv, [r for r in recs if "\\deleted" not in {f.lower() for f in r["flags"]}]
        return self._guard(run)

    def fetch_raw(self, name: str, uidvalidity: int, uid: int) -> bytes | None:
        def run():
            c = self.conn
            c.select(name, readonly=True)
            typ, data = c._m().response("UIDVALIDITY")
            m = re.search(r"\d+", _text(data))
            if uidvalidity and m and int(m.group(0)) != uidvalidity:
                raise ConnectorError(f"UIDVALIDITY of '{name}' changed during the job: rerun to list it again",
                                     ConnectorError.REJECTED)
            return c.fetch_raw(name, str(uid))
        return self._guard(run)


# ------------------------------------------------------------------ calendar: Google JSON to iCalendar
def _ics_dt(spec: dict | None, tzid_ok: bool) -> tuple[str, str] | None:
    """(parameters, value) of a Google start/end/originalStartTime. All-day: VALUE=DATE. Timed: with the zone name
    when the series needs the local time (`tzid_ok`), else in UTC."""
    if not spec:
        return None
    if spec.get("date"):
        return ";VALUE=DATE", spec["date"].replace("-", "")
    raw = spec.get("dateTime")
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    tz = spec.get("timeZone")
    if tzid_ok and tz and tz.upper() not in ("UTC", "GMT", "ETC/UTC"):
        try:
            from zoneinfo import ZoneInfo
            return f";TZID={tz}", d.astimezone(ZoneInfo(tz)).strftime("%Y%m%dT%H%M%S")
        except Exception:  # noqa: BLE001 - unknown zone name or no tz database: UTC is always right
            pass
    return "", d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_stamp(text: str | None) -> str | None:
    p = _ics_dt({"dateTime": text}, False) if text else None
    return p[1] if p else None


_PARTSTAT = {"accepted": "ACCEPTED", "declined": "DECLINED", "tentative": "TENTATIVE", "needsAction": "NEEDS-ACTION"}


def event_lines(ev: dict, local_time: bool) -> list[str]:
    uid = ev.get("iCalUID") or f"{ev['id']}@google.com"
    out = ["BEGIN:VEVENT", "UID:" + esc(uid)]
    stamp = _utc_stamp(ev.get("updated")) or _utc_stamp(ev.get("created")) or "19700101T000000Z"  # the same text on every run
    out.append("DTSTAMP:" + stamp)
    if _utc_stamp(ev.get("created")):
        out.append("CREATED:" + _utc_stamp(ev["created"]))  # type: ignore[operator]
    if _utc_stamp(ev.get("updated")):
        out.append("LAST-MODIFIED:" + _utc_stamp(ev["updated"]))  # type: ignore[operator]
    if ev.get("sequence") is not None:
        out.append(f"SEQUENCE:{int(ev['sequence'])}")
    start = _ics_dt(ev.get("start"), local_time)
    end = None if ev.get("endTimeUnspecified") else _ics_dt(ev.get("end"), local_time)
    if start:
        out.append(f"DTSTART{start[0]}:{start[1]}")
    if end:
        out.append(f"DTEND{end[0]}:{end[1]}")
    if ev.get("recurringEventId") and ev.get("originalStartTime"):
        rid = _ics_dt(ev["originalStartTime"], local_time)
        if rid:
            out.append(f"RECURRENCE-ID{rid[0]}:{rid[1]}")
    for line in ev.get("recurrence") or []:
        out.append(line)
    if ev.get("summary"):
        out.append("SUMMARY:" + esc(ev["summary"]))
    desc = ev.get("description") or ""
    if desc:
        out.append("DESCRIPTION:" + esc(desc))
    if ev.get("location"):
        out.append("LOCATION:" + esc(ev["location"]))
    out.append("STATUS:" + {"tentative": "TENTATIVE", "cancelled": "CANCELLED"}.get(ev.get("status", ""), "CONFIRMED"))
    if ev.get("transparency") == "transparent":
        out.append("TRANSP:TRANSPARENT")
    cls = {"private": "PRIVATE", "confidential": "CONFIDENTIAL", "public": "PUBLIC"}.get(ev.get("visibility", ""))
    if cls:
        out.append("CLASS:" + cls)
    org = ev.get("organizer") or {}
    if org.get("email"):
        cn = f";CN={_param(org['displayName'])}" if org.get("displayName") else ""
        out.append(f"ORGANIZER{cn}:mailto:{org['email']}")
    for a in ev.get("attendees") or []:
        if not a.get("email"):
            continue
        params = ""
        if a.get("displayName"):
            params += f";CN={_param(a['displayName'])}"
        params += ";PARTSTAT=" + _PARTSTAT.get(a.get("responseStatus", ""), "NEEDS-ACTION")
        if a.get("optional"):
            params += ";ROLE=OPT-PARTICIPANT"
        if a.get("resource"):
            params += ";CUTYPE=RESOURCE"
        out.append(f"ATTENDEE{params}:mailto:{a['email']}")
    link = ev.get("hangoutLink") or ((ev.get("conferenceData") or {}).get("entryPoints") or [{}])[0].get("uri")
    if link:
        out.append("X-GOOGLE-CONFERENCE:" + link)
    for r in (ev.get("reminders") or {}).get("overrides") or []:
        out += ["BEGIN:VALARM", "ACTION:DISPLAY", f"TRIGGER:-PT{int(r.get('minutes') or 0)}M",
                "DESCRIPTION:" + esc(ev.get("summary") or "Reminder"), "END:VALARM"]
    out.append("END:VEVENT")
    return out


def _param(v: str) -> str:
    return '"' + v.replace('"', "'").replace("\n", " ") + '"'


def events_to_ics(events: list[dict]) -> str:
    """One VCALENDAR with a series and its exceptions (they share the UID, so they must share the resource). A
    cancelled occurrence of a series becomes an EXDATE of the series."""
    live = [e for e in events if e.get("status") != "cancelled"]
    local = any(e.get("recurrence") or e.get("recurringEventId") for e in live)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//mccb//Google Calendar//EN", "CALSCALE:GREGORIAN"]
    skipped = [e for e in events if e.get("status") == "cancelled" and e.get("recurringEventId")]
    for e in sorted(live, key=lambda e: (1 if e.get("recurringEventId") else 0, e.get("id", ""))):
        body = event_lines(e, local)
        if e.get("recurrence"):
            for x in skipped:
                ex = _ics_dt(x.get("originalStartTime"), local)
                if ex:
                    body.insert(body.index("END:VEVENT"), f"EXDATE{ex[0]}:{ex[1]}")
        lines += body
    lines.append("END:VCALENDAR")
    return join_lines(lines)


def group_events(events: list[dict]) -> list[list[dict]]:
    """Series with their exceptions, by iCalUID (else by master id). Groups with nothing alive are dropped."""
    groups: dict[str, list[dict]] = {}
    for e in events:
        if e.get("eventType") in ("birthday", "workingLocation"):
            continue
        k = e.get("iCalUID") or e.get("recurringEventId") or e["id"]
        groups.setdefault(k, []).append(e)
    return [groups[k] for k in sorted(groups) if any(e.get("status") != "cancelled" for e in groups[k])]


def person_to_vcard(p: dict) -> tuple[str, list[str]] | None:
    """vCard 3.0 of a People API person. None for a contact without any name, email or phone."""
    def first(key):
        vals = p.get(key) or []
        prim = [v for v in vals if (v.get("metadata") or {}).get("primary")]
        return (prim or vals or [{}])[0]

    nm = first("names")
    emails, phones = p.get("emailAddresses") or [], p.get("phoneNumbers") or []
    orgs = p.get("organizations") or []
    fn = nm.get("displayName") or " ".join(x for x in (nm.get("givenName"), nm.get("familyName")) if x) \
        or (orgs[0].get("name") if orgs else "") or (emails[0].get("value") if emails else "") \
        or (phones[0].get("value") if phones else "")
    if not fn:
        return None
    warns: list[str] = []
    lines = ["BEGIN:VCARD", "VERSION:3.0", "UID:" + esc(p.get("resourceName", "")), "FN:" + esc(fn),
             "N:" + ";".join(esc(nm.get(k, "")) for k in
                             ("familyName", "givenName", "middleName", "honorificPrefix", "honorificSuffix"))]

    def typ(v):
        t = (v.get("type") or "").lower()
        return f";TYPE={t}" if t in ("home", "work", "mobile", "other") and t != "mobile" else (";TYPE=cell" if t == "mobile" else "")

    for n in p.get("nicknames") or []:
        if n.get("value"):
            lines.append("NICKNAME:" + esc(n["value"]))
    for o in orgs:
        if o.get("name") or o.get("department"):
            lines.append("ORG:" + esc(o.get("name", "")) + (";" + esc(o["department"]) if o.get("department") else ""))
        if o.get("title"):
            lines.append("TITLE:" + esc(o["title"]))
    for e in emails:
        if e.get("value"):
            lines.append("EMAIL" + typ(e) + ":" + esc(e["value"]))
    for t in phones:
        if t.get("value"):
            lines.append("TEL" + typ(t) + ":" + esc(t["value"]))
    for a in p.get("addresses") or []:
        parts = [a.get("poBox", ""), a.get("extendedAddress", ""), a.get("streetAddress", ""), a.get("city", ""),
                 a.get("region", ""), a.get("postalCode", ""), a.get("country", "")]
        if any(parts):
            lines.append("ADR" + typ(a) + ":" + ";".join(esc(x) for x in parts))
    for u in p.get("urls") or []:
        if u.get("value"):
            lines.append("URL:" + u["value"])
    for b in p.get("birthdays") or []:
        d = b.get("date") or {}
        if d.get("month") and d.get("day"):
            if d.get("year"):
                lines.append(f"BDAY:{d['year']:04d}-{d['month']:02d}-{d['day']:02d}")
            else:  # no year: the convention of Apple Contacts, read by most clients
                lines.append(f"BDAY;X-APPLE-OMIT-YEAR=1604:1604-{d['month']:02d}-{d['day']:02d}")
            break
    notes = [b["value"] for b in p.get("biographies") or [] if b.get("value")]
    if notes:
        lines.append("NOTE:" + esc("\n\n".join(notes)))
    upd = max((s.get("updateTime", "") for s in (p.get("metadata") or {}).get("sources", [])), default="")
    if re.match(r"\d{4}-\d{2}-\d{2}T", upd):
        lines.append("REV:" + re.sub(r"\.\d+", "", upd))
    lines.append("END:VCARD")
    return join_lines(lines), warns


# ------------------------------------------------------------------ source
def _slug(text: str) -> str:
    return _digest(text)[:8]


class GoogleSource(Source):
    """Mail (IMAP), calendars and contacts of one Google account. Folders: Gmail labels turned into mail folders
    (one per mail, see folder_of_labels), one folder per calendar, one for the contacts."""

    def __init__(self, config: dict, secrets: dict, ctx: dict | None = None):
        self.config = config
        self.secrets = secrets
        ctx = ctx or {}
        self.transport = ctx.get("transport")
        self.imap_factory = ctx.get("imap") or GmailImap
        self.want = {s: bool(config.get(f"sync_{s}", True)) for s in SCOPES}
        self.auth: GoogleAuth | None = None
        self.api: GoogleApi | None = None
        self.imap: Any = None
        self._groups: dict[str, list[dict]] = {}   # folder key -> scanned messages
        self._tops: dict[str, int] = {}            # mailbox -> highest UID seen (cursor of changed_since)
        self._uv: dict[str, int] = {}
        self._events: dict[str, list[list[dict]]] = {}
        self._sync_tokens: dict[str, str] = {}

    # -- lifecycle
    def open(self) -> None:
        self.close()
        if not any(self.want.values()):
            raise ConnectorError("enable at least one of mail, calendar, contacts", ConnectorError.CONFIG)
        self.auth = GoogleAuth(self.config, self.secrets, self.transport)
        self.api = GoogleApi(self.auth, self.transport)
        first = next(s for s in SCOPES if self.want[s])
        try:
            self.auth.token([SCOPES[first]])
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for o in (self.imap, self.api, self.auth):
            if o is not None:
                try:
                    o.close()
                except Exception:  # noqa: BLE001
                    pass
        self.imap = self.api = self.auth = None
        self._groups, self._uv = {}, {}

    def _mail(self):
        if self.imap is None:
            auth = self.auth
            self.imap = self.imap_factory(ImapParams(
                host=GMAIL_HOST, port=GMAIL_PORT, security=GMAIL_SECURITY, verify_tls=True, login=self.config["user"],
                password="", token_source=lambda: auth.token([SCOPES["mail"]])))  # XOAUTH2, renewed at each connection
            self.imap.connect()
        return self.imap

    def _api(self) -> GoogleApi:
        if self.api is None:
            raise ConnectorError("Google source not open", ConnectorError.TRANSIENT)
        return self.api

    # -- folders
    def folders(self) -> list[SourceFolder]:
        out: list[SourceFolder] = []
        if self.want["mail"]:
            out += self._mail_folders()
        if self.want["calendar"]:
            out += self._calendar_folders()
        if self.want["contacts"]:
            out.append(SourceFolder(path=["Contacts"], kind=CONTACTS, ref=("people", "me")))
        return out

    # -- mail
    def _mail_boxes(self) -> dict[str, tuple[str, str | None]]:
        """{"all": mailbox name of All Mail, "junk": ..., "trash": ...} from the special-use flags."""
        found = {}
        for b in self._mail().mailboxes():
            fl = {f.lower() for f in b.flags}
            if "\\noselect" in fl:
                continue
            for flag, role in (("\\all", "all"), ("\\junk", "junk"), ("\\spam", "junk"), ("\\trash", "trash")):
                if flag in fl and role not in found:
                    found[role] = b.name
        return found

    def _scan_mail(self, after: dict[str, int] | None = None) -> None:
        boxes = self._mail_boxes()
        if "all" not in boxes:
            raise ConnectorError("no All Mail folder: is IMAP enabled for this account?", ConnectorError.CONFIG)
        todo = [("all", boxes["all"])]
        if self.config.get("include_trash_spam"):
            todo += [(r, boxes[r]) for r in ("junk", "trash") if r in boxes]
        for role, name in todo:
            uv, recs = self._mail().scan(name, (after or {}).get(name, 0))
            self._uv[name] = uv
            self._tops[name] = max([r["uid"] for r in recs] + [(after or {}).get(name, 0)])
            for r in recs:
                if role == "all":
                    path, special = folder_of_labels(r["labels"])
                else:
                    path, special = (["Spam"], "junk") if role == "junk" else (["Trash"], "trash")
                r["mailbox"], r["path"], r["special"] = name, path, special
                self._groups.setdefault("/".join(path), []).append(r)

    def _mail_folders(self) -> list[SourceFolder]:
        self._groups = {}
        self._scan_mail()
        out = []
        for key in sorted(self._groups, key=str.lower):
            recs = self._groups[key]
            out.append(SourceFolder(path=recs[0]["path"], kind=MAIL, special=recs[0]["special"], count=len(recs),
                                    ref=("mail", key)))
        return out

    def _mail_item(self, r: dict) -> SourceItem:
        return SourceItem(key=key_of("g", r["msgid"]), alt_key="d:" + _digest(f"{r['date']}|{r['size']}"),
                          date=r["date"], size=r["size"], flags=keep_flags(r["flags"]),
                          ref=(r["mailbox"], self._uv.get(r["mailbox"], 0), r["uid"]))

    # -- calendars
    def _calendar_folders(self) -> list[SourceFolder]:
        cals, token = [], None
        while True:
            r = self._api().get(f"{CALENDAR_API}/users/me/calendarList",
                                {"maxResults": 250, "minAccessRole": "reader", **({"pageToken": token} if token else {})},
                                "calendar")
            cals += [c for c in r.get("items") or [] if not c.get("deleted")]
            token = r.get("nextPageToken")
            if not token:
                break
        out, names = [], set()
        for c in sorted(cals, key=lambda c: ((c.get("summaryOverride") or c.get("summary") or c["id"]).lower(), c["id"])):
            name = c.get("summaryOverride") or c.get("summary") or c["id"]
            if name in names:
                name = f"{name} ({_slug(c['id'])})"
            names.add(name)
            out.append(SourceFolder(path=[name], kind=CALENDAR, ref=("cal", c["id"])))
        return out

    def _list_events(self, cal: str, extra: dict | None = None) -> tuple[list[dict], str | None]:
        events, token, sync = [], None, None
        while True:
            # showDeleted must be the same on the first call and on the sync calls: it also tells which occurrences of a
            # series were deleted (cancelled exceptions), which become EXDATE
            params = {"maxResults": 2500, "singleEvents": "false", "showDeleted": "true", **(extra or {}),
                      **({"pageToken": token} if token else {})}
            r = self._api().get(f"{CALENDAR_API}/calendars/{_enc(cal)}/events", params, "calendar")
            events += r.get("items") or []
            token, sync = r.get("nextPageToken"), r.get("nextSyncToken") or sync
            if not token:
                return events, sync

    def _event_item(self, cal: str, group: list[dict]) -> SourceItem:
        master = _master(group)
        start = master.get("start") or {}
        raw = start.get("dateTime") or start.get("date")
        try:
            d = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
        except ValueError:
            d = None
        if d is not None and d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return SourceItem(key=key_of(f"c{_slug(cal)}-", master.get("recurringEventId") or master["id"]),
                          alt_key="u:" + _digest(master.get("iCalUID") or master["id"]), date=d, ref=group)

    # -- listing
    def iter_items(self, folder: SourceFolder) -> Iterator[SourceItem]:
        kind, ident = folder.ref
        if kind == "mail":
            if not self._groups:
                self._scan_mail()
            for r in self._groups.get(ident, []):
                yield self._mail_item(r)
        elif kind == "cal":
            events, sync = self._list_events(ident)
            if sync:
                self._sync_tokens["cal:" + ident] = sync
            for g in group_events(events):
                yield self._event_item(ident, g)
        else:
            people, sync = self._list_people(None)
            if sync:
                self._sync_tokens["people"] = sync
            for p in people:
                yield self._person_item(p)

    def _list_people(self, sync_token: str | None) -> tuple[list[dict], str | None]:
        out, token, sync = [], None, None
        while True:
            params = {"personFields": PERSON_FIELDS, "pageSize": 1000, "requestSyncToken": "true",
                      **({"syncToken": sync_token} if sync_token else {}), **({"pageToken": token} if token else {})}
            r = self._api().get(f"{PEOPLE_API}/people/me/connections", params, "contacts")
            out += [p for p in r.get("connections") or [] if not (p.get("metadata") or {}).get("deleted")]
            token, sync = r.get("nextPageToken"), r.get("nextSyncToken") or sync
            if not token:
                return out, sync

    def _person_item(self, p: dict) -> SourceItem:
        return SourceItem(key=key_of("p", p["resourceName"].split("/")[-1]), ref=p)

    def changed_since(self, folder: SourceFolder, cursor: str | None) -> Changes:
        """Contacts and calendars with the API sync tokens, mail by UID. Deletions are not reported. A token Google no
        longer accepts gives a full listing."""
        kind, ident = folder.ref
        try:
            state = json.loads(cursor) if cursor else None
        except ValueError:
            state = None
        if kind == "mail":
            return self._mail_changes(folder, state)
        try:
            if kind == "people" and state and state.get("sync"):
                people, sync = self._list_people(state["sync"])
                return Changes([self._person_item(p) for p in people], json.dumps({"sync": sync}), reset=False)
            if kind == "cal" and state and state.get("sync"):
                events, sync = self._list_events(ident, {"syncToken": state["sync"]})
                uids = sorted({e.get("iCalUID") for e in events if e.get("status") != "cancelled" and e.get("iCalUID")})
                items = []
                for uid in uids:
                    evs, _s = self._list_events(ident, {"iCalUID": uid})
                    items += [self._event_item(ident, g) for g in group_events(evs)]
                return Changes(items, json.dumps({"sync": sync}), reset=False)
        except ConnectorError as e:
            if not _gone(e):
                raise
        items = list(self.iter_items(folder))
        sync = self._sync_tokens.get("people" if kind == "people" else f"cal:{ident}")
        return Changes(items, json.dumps({"sync": sync}) if sync else None, reset=True)

    def _mail_changes(self, folder: SourceFolder, state: dict | None) -> Changes:
        key = folder.ref[1]
        valid = isinstance(state, dict) and state.get("uv") and all(
            isinstance(v, int) for v in (state.get("top") or {}).values())
        if not valid:
            self._groups = {}
            items = list(self.iter_items(folder))
            return Changes(items, json.dumps({"uv": self._uv, "top": self._tops}), reset=True)
        self._groups = {}
        self._scan_mail(after=dict(state["top"]))
        if any(self._uv.get(n) != v for n, v in state["uv"].items()):  # UIDVALIDITY changed: start again
            self._groups, self._tops = {}, {}
            items = list(self.iter_items(folder))
            return Changes(items, json.dumps({"uv": self._uv, "top": self._tops}), reset=True)
        return Changes([self._mail_item(r) for r in self._groups.get(key, [])],
                       json.dumps({"uv": self._uv, "top": self._tops}), reset=False)

    # -- content
    def fetch(self, folder: SourceFolder, item: SourceItem, import_id: str) -> FetchedItem | None:
        kind = folder.ref[0]
        if kind == "mail":
            name, uv, uid = item.ref
            raw = self._mail().fetch_raw(name, uv, uid)
            if raw is None:
                return None
            return FetchedItem(raw=raw, internal_date=item.date, flags=list(item.flags), subject=subject_of(raw))
        if kind == "cal":
            ics = events_to_ics(item.ref)
            master = _master(item.ref)
            return FetchedItem(raw=ics.encode("utf-8"), subject=(master.get("summary") or "")[:500])
        res = person_to_vcard(item.ref)
        if res is None:
            return None
        text, warns = res
        nm = (item.ref.get("names") or [{}])[0].get("displayName") or ""
        return FetchedItem(raw=text.encode("utf-8"), subject=nm[:500], warnings=warns)


def _master(group: list[dict]) -> dict:
    live = [e for e in group if e.get("status") != "cancelled"] or group
    return next((e for e in live if not e.get("recurringEventId")), live[0])


def _enc(cal_id: str) -> str:
    from urllib.parse import quote as q
    return q(cal_id, safe="")


def identity(config: dict) -> str:
    return str(config.get("user", "")).lower()


_FIELDS = (
    registry.Field("user", "Google account", required=True, help="The email address of the user to read."),
    registry.Field("auth", "Access", "select", default="service_account", options=("service_account", "oauth"),
                   help="service_account: Workspace domain-wide delegation. oauth: a refresh token of this user."),
    registry.Field("service_account_key", "Service account key (JSON)", "password", secret=True,
                   help="The JSON key file of the service account, pasted whole. For access=service_account."),
    registry.Field("client_id", "OAuth client id", help="For access=oauth."),
    registry.Field("client_secret", "OAuth client secret", "password", secret=True, help="For access=oauth."),
    registry.Field("refresh_token", "OAuth refresh token", "password", secret=True, help="For access=oauth."),
    registry.Field("sync_mail", "Mail (Gmail over IMAP)", "boolean", default=True),
    registry.Field("sync_calendar", "Calendars", "boolean", default=True),
    registry.Field("sync_contacts", "Contacts", "boolean", default=True),
    registry.Field("include_trash_spam", "Include Spam and Trash", "boolean", default=False),
)

registry.register(registry.ConnectorSpec(
    kind="google", label="Google", description="Gmail, Google Calendar and Google Contacts of one account.",
    source=registry.Role(GoogleSource, (MAIL, CALENDAR, CONTACTS),
                         frozenset({CAP_FOLDERS, CAP_FLAGS, CAP_INTERNAL_DATE, CAP_INCREMENTAL}), _FIELDS, identity)))
