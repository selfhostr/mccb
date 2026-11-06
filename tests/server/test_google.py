"""Google source against simulated servers: the token endpoint (service account JWT and refresh token), the Calendar and
People APIs, and Gmail over IMAP (a fake in place of the IMAP class). Nothing here talks to Google."""
import base64
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.connectors import google
from app.connectors.base import CALENDAR, CONTACTS, MAIL, ConnectorError
from app.server.imap import Mailbox
from tests.server.http_fakes import FakeHttp

USER = "alice@example.test"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
SA_KEY = json.dumps({"type": "service_account", "client_email": "sa@proj.iam.gserviceaccount.com", "private_key": PEM,
                     "private_key_id": "kid-1", "token_uri": "https://oauth2.googleapis.com/token"})


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class GoogleFake:
    """Token endpoint + Calendar + People. Tokens are `tok-N`; every API call must carry the last one issued."""

    def __init__(self):
        self.http = FakeHttp(self.handle)
        self.issued: list[dict] = []
        self.refresh_ok = True
        self.expire_next_call = False
        self.events: dict[str, list[dict]] = {"primary": [], "work@group": []}
        self.event_pages = 2
        self.people: list[dict] = []
        self.calendar_sync = "cs1"
        self.people_sync = "ps1"
        self.sync_gone = False
        self.token_n = 0

    def close(self):
        self.http.close()

    def token_ok(self, req):
        return req.headers.get("Authorization") == f"Bearer tok-{self.token_n}"

    def handle(self, req):
        p = req.path
        if p == "/token":
            form = {k: v[0] for k, v in parse_qs(req.body.decode()).items()}
            if form["grant_type"].endswith("jwt-bearer"):
                head, claims, sig = form["assertion"].split(".")
                try:
                    KEY.public_key().verify(b64d(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
                except Exception:  # noqa: BLE001
                    return 400, {}, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."}
                c = json.loads(b64d(claims))
                if c["sub"] == "unknown@example.test":
                    return 400, {}, {"error": "invalid_grant", "error_description": "Invalid email or User ID"}
                if "forbidden" in c["scope"]:
                    return 401, {}, {"error": "unauthorized_client", "error_description": "Client is unauthorized"}
                self.issued.append({"head": json.loads(b64d(head)), "claims": c})
            else:
                if not self.refresh_ok or form["refresh_token"] != "rt-1" or form["client_id"] != "cid":
                    return 400, {}, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
                self.issued.append({"refresh": form["refresh_token"]})
            self.token_n += 1
            return 200, {}, {"access_token": f"tok-{self.token_n}", "expires_in": 3599, "token_type": "Bearer"}
        if not self.token_ok(req) or self.expire_next_call:
            self.expire_next_call = False
            return 401, {}, {"error": {"code": 401, "message": "Invalid Credentials", "status": "UNAUTHENTICATED"}}
        if p == "/calendar/v3/users/me/calendarList":
            return 200, {}, {"items": [{"id": "primary", "summary": "Alice", "accessRole": "owner"},
                                       {"id": "work@group", "summary": "Work", "accessRole": "reader"},
                                       {"id": "gone@group", "summary": "Old", "deleted": True}]}
        if p.startswith("/calendar/v3/calendars/") and p.endswith("/events"):
            cal = p.split("/")[4].replace("%40", "@")
            if req.query.get("syncToken"):
                if self.sync_gone:
                    return 410, {}, {"error": {"code": 410, "message": "Sync token is no longer valid, a full sync is required."}}
                changed = [e for e in self.events[cal] if e["id"] == "ev-new"]
                return 200, {}, {"items": changed, "nextSyncToken": "cs2"}
            if req.query.get("iCalUID"):
                return 200, {}, {"items": [e for e in self.events[cal] if e.get("iCalUID") == req.query["iCalUID"]]}
            items = self.events[cal]
            if self.event_pages > 1 and not req.query.get("pageToken"):
                return 200, {}, {"items": items[:2], "nextPageToken": "p2"}
            rest = items[2:] if self.event_pages > 1 else items
            return 200, {}, {"items": rest, "nextSyncToken": self.calendar_sync}
        if p == "/v1/people/me/connections":
            if req.query.get("syncToken"):
                if self.sync_gone:
                    return 400, {}, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "x",
                                               "details": [{"reason": "EXPIRED_SYNC_TOKEN"}]}}
                return 200, {}, {"connections": [self.people[-1], {"resourceName": "people/c9", "metadata": {"deleted": True}}],
                                 "nextSyncToken": "ps2"}
            if not req.query.get("pageToken"):
                return 200, {}, {"connections": self.people[:1], "nextPageToken": "n2"}
            return 200, {}, {"connections": self.people[1:], "nextSyncToken": self.people_sync}
        return 404, {}, b""


@pytest.fixture
def gg(monkeypatch):
    g = GoogleFake()
    monkeypatch.setattr(google, "TOKEN_URL", g.http.url + "/token")
    monkeypatch.setattr(google, "CALENDAR_API", g.http.url + "/calendar/v3")
    monkeypatch.setattr(google, "PEOPLE_API", g.http.url + "/v1")
    yield g
    g.close()


def events():
    master = {"id": "ev1", "iCalUID": "ev1@google.com", "summary": "Team; sync", "description": "line1\nline2",
              "start": {"dateTime": "2026-01-05T10:00:00+01:00", "timeZone": "Europe/Paris"},
              "end": {"dateTime": "2026-01-05T11:00:00+01:00", "timeZone": "Europe/Paris"},
              "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=4"], "updated": "2026-01-01T08:00:00.000Z",
              "attendees": [{"email": "bob@example.test", "displayName": "Bob", "responseStatus": "accepted"}],
              "organizer": {"email": USER}, "reminders": {"overrides": [{"method": "popup", "minutes": 10}]}}
    moved = {"id": "ev1_20260112T090000Z", "iCalUID": "ev1@google.com", "recurringEventId": "ev1", "summary": "Team (moved)",
             "originalStartTime": {"dateTime": "2026-01-12T10:00:00+01:00", "timeZone": "Europe/Paris"},
             "start": {"dateTime": "2026-01-12T14:00:00+01:00", "timeZone": "Europe/Paris"},
             "end": {"dateTime": "2026-01-12T15:00:00+01:00", "timeZone": "Europe/Paris"}}
    skipped = {"id": "ev1_20260119T090000Z", "iCalUID": "ev1@google.com", "recurringEventId": "ev1", "status": "cancelled",
               "originalStartTime": {"dateTime": "2026-01-19T10:00:00+01:00", "timeZone": "Europe/Paris"}}
    allday = {"id": "ev2", "iCalUID": "ev2@google.com", "summary": "Holiday", "start": {"date": "2026-02-01"},
              "end": {"date": "2026-02-02"}, "transparency": "transparent"}
    utc = {"id": "ev3", "iCalUID": "ev3@google.com", "summary": "Call", "start": {"dateTime": "2026-03-01T09:00:00Z"},
           "end": {"dateTime": "2026-03-01T09:30:00Z"}}
    deleted = {"id": "ev4", "iCalUID": "ev4@google.com", "status": "cancelled", "start": {"date": "2026-04-01"}}
    bday = {"id": "ev5", "iCalUID": "ev5@google.com", "eventType": "birthday", "start": {"date": "2026-05-01"}}
    return [master, moved, skipped, allday, utc, deleted, bday]


def people():
    return [{"resourceName": "people/c101", "names": [{"displayName": "Ann Lee", "givenName": "Ann", "familyName": "Lee"}],
             "emailAddresses": [{"value": "ann@example.test", "type": "work"}, {"value": "a2@example.test", "type": "home"}],
             "phoneNumbers": [{"value": "+33 1 23", "type": "mobile"}],
             "addresses": [{"streetAddress": "1 rue A", "city": "Paris", "postalCode": "75001", "country": "France", "type": "home"}],
             "organizations": [{"name": "ACME", "title": "CTO", "department": "R&D"}],
             "birthdays": [{"date": {"year": 1980, "month": 2, "day": 3}}], "biographies": [{"value": "note"}],
             "metadata": {"sources": [{"updateTime": "2025-12-01T10:00:00.5Z"}]}},
            {"resourceName": "people/c102", "emailAddresses": [{"value": "only@example.test"}]},
            {"resourceName": "people/c103", "metadata": {}}]


def cfg(**extra):
    return {"user": USER, "auth": "service_account", "sync_mail": False, "sync_calendar": True, "sync_contacts": True,
            **extra}


# ------------------------------------------------------------------ authentication
def test_service_account_token(gg):
    auth = google.GoogleAuth(cfg(), {"service_account_key": SA_KEY})
    t1 = auth.token([google.SCOPES["calendar"]])
    assert t1 == "tok-1" and auth.token([google.SCOPES["calendar"]]) == "tok-1"   # cached
    assert len(gg.issued) == 1
    claims = gg.issued[0]["claims"]
    assert claims["iss"] == "sa@proj.iam.gserviceaccount.com" and claims["sub"] == USER
    assert claims["scope"] == google.SCOPES["calendar"] and claims["exp"] - claims["iat"] == 3600
    assert claims["aud"] == google.TOKEN_URL
    assert gg.issued[0]["head"] == {"alg": "RS256", "typ": "JWT", "kid": "kid-1"}
    assert auth.token([google.SCOPES["contacts"]]) == "tok-2"                    # one token per scope set
    assert auth.token([google.SCOPES["calendar"]], fresh=True) == "tok-3"


def test_oauth_refresh_token(gg):
    c = {"user": USER, "auth": "oauth", "client_id": "cid"}
    assert google.GoogleAuth(c, {"client_secret": "cs", "refresh_token": "rt-1"}).token(["s"]) == "tok-1"
    gg.refresh_ok = False
    with pytest.raises(ConnectorError) as e:
        google.GoogleAuth(c, {"client_secret": "cs", "refresh_token": "rt-1"}).token(["s"])
    assert e.value.kind == ConnectorError.AUTH and "expired or revoked" in str(e.value)


def test_auth_refusals(gg):
    with pytest.raises(ConnectorError) as e:
        google.GoogleAuth({"user": "unknown@example.test"}, {"service_account_key": SA_KEY}).token(["s"])
    assert e.value.kind == ConnectorError.AUTH
    with pytest.raises(ConnectorError) as e:
        google.GoogleAuth({"user": USER}, {"service_account_key": SA_KEY}).token(["forbidden"])
    assert e.value.kind == ConnectorError.AUTH and "authorized" in str(e.value)
    for bad in ({}, {"service_account_key": "not json"}, {"service_account_key": '{"client_email": "x"}'}):
        with pytest.raises(ConnectorError) as e:
            google.GoogleAuth({"user": USER}, bad)
        assert e.value.kind == ConnectorError.CONFIG
    with pytest.raises(ConnectorError) as e:
        google.GoogleAuth({"user": USER, "auth": "oauth"}, {"client_secret": "x"})
    assert e.value.kind == ConnectorError.CONFIG
    wrong_key = json.dumps({"client_email": "x", "private_key": "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"})
    with pytest.raises(ConnectorError) as e:
        google.GoogleAuth({"user": USER}, {"service_account_key": wrong_key}).token(["s"])
    assert e.value.kind == ConnectorError.CONFIG


def test_source_needs_a_service_enabled(gg):
    with pytest.raises(ConnectorError) as e:
        google.GoogleSource(cfg(sync_calendar=False, sync_contacts=False), {"service_account_key": SA_KEY}).open()
    assert e.value.kind == ConnectorError.CONFIG


# ------------------------------------------------------------------ calendar
def test_calendar_listing_and_ics(gg):
    gg.events["primary"] = events()
    src = google.GoogleSource(cfg(sync_contacts=False), {"service_account_key": SA_KEY})
    src.open()
    folders = src.folders()
    assert [(f.path, f.kind) for f in folders] == [(["Alice"], CALENDAR), (["Work"], CALENDAR)]   # deleted one is out
    items = list(src.iter_items(folders[0]))
    # series + exception + cancelled occurrence are ONE resource; all-day and UTC events; deleted and birthday dropped
    assert len(items) == 3 and len({i.key for i in items}) == 3
    by = {i.alt_key: i for i in items}
    series = src.fetch(folders[0], by["u:" + google._digest("ev1@google.com")], "<x>")
    text = series.raw.decode()
    assert series.subject == "Team; sync"
    assert text.count("BEGIN:VEVENT") == 2 and text.count("UID:ev1@google.com") == 2
    assert "DTSTART;TZID=Europe/Paris:20260105T100000" in text and "DTEND;TZID=Europe/Paris:20260105T110000" in text
    assert "RRULE:FREQ=WEEKLY;COUNT=4" in text
    assert "EXDATE;TZID=Europe/Paris:20260119T100000" in text                       # the cancelled occurrence
    assert "RECURRENCE-ID;TZID=Europe/Paris:20260112T100000" in text and "SUMMARY:Team (moved)" in text
    assert "DESCRIPTION:line1\\nline2" in text and "SUMMARY:Team\\; sync" in text
    assert "ATTENDEE;CN=\"Bob\";PARTSTAT=ACCEPTED:mailto:bob@example.test" in text
    assert "ORGANIZER:mailto:alice@example.test" in text
    assert "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT10M" in text
    assert text.endswith("END:VCALENDAR\r\n") and "\r\n" in text and "\n" not in text.replace("\r\n", "")
    assert "VTIMEZONE" not in text
    allday = src.fetch(folders[0], by["u:" + google._digest("ev2@google.com")], "<x>").raw.decode()
    assert "DTSTART;VALUE=DATE:20260201" in allday and "DTEND;VALUE=DATE:20260202" in allday and "TRANSP:TRANSPARENT" in allday
    plain = src.fetch(folders[0], by["u:" + google._digest("ev3@google.com")], "<x>").raw.decode()
    assert "DTSTART:20260301T090000Z" in plain                                      # not recurring: UTC, always exact
    again = [i.key for i in src.iter_items(folders[0])]
    assert again == [i.key for i in items]                                          # stable keys
    assert [r.query.get("showDeleted") for r in gg.http.requests if r.path.endswith("/events")][0] == "true"


def test_calendar_incremental(gg):
    gg.events["primary"] = events()
    src = google.GoogleSource(cfg(sync_contacts=False), {"service_account_key": SA_KEY})
    src.open()
    f = src.folders()[0]
    first = src.changed_since(f, None)
    assert first.reset and len(first.items) == 3 and json.loads(first.cursor) == {"sync": "cs1"}
    gg.events["primary"].append({"id": "ev-new", "iCalUID": "evn@google.com", "summary": "New",
                                 "start": {"date": "2026-06-01"}, "end": {"date": "2026-06-02"}})
    more = src.changed_since(f, first.cursor)
    assert not more.reset and len(more.items) == 1 and json.loads(more.cursor) == {"sync": "cs2"}
    gg.sync_gone = True
    full = src.changed_since(f, more.cursor)
    assert full.reset and len(full.items) == 4


def test_api_errors_are_classified(gg):
    src = google.GoogleSource(cfg(sync_contacts=False), {"service_account_key": SA_KEY})
    src.open()
    api = src._api()
    assert api.get(google.CALENDAR_API + "/users/me/calendarList", {}, "calendar")["items"]
    gg.expire_next_call = True    # one 401: the token is renewed and the call repeated
    assert api.get(google.CALENDAR_API + "/users/me/calendarList", {}, "calendar")["items"]
    assert gg.token_n == 2        # the token of open(), then the renewed one
    with pytest.raises(ConnectorError) as e:
        api.get(google.CALENDAR_API + "/nothing", {}, "calendar")
    assert e.value.kind == ConnectorError.REJECTED


# ------------------------------------------------------------------ contacts
def test_people_listing_and_vcard(gg):
    gg.people = people()
    src = google.GoogleSource(cfg(sync_calendar=False), {"service_account_key": SA_KEY})
    src.open()
    folder = src.folders()[0]
    assert folder.kind == CONTACTS and folder.path == ["Contacts"]
    items = list(src.iter_items(folder))
    assert [i.key for i in items] == ["pc101", "pc102", "pc103"]                     # two pages
    ann = src.fetch(folder, items[0], "<x>")
    text = ann.raw.decode()
    assert ann.subject == "Ann Lee"
    for line in ("UID:people/c101", "FN:Ann Lee", "N:Lee;Ann;;;", "EMAIL;TYPE=work:ann@example.test", "EMAIL;TYPE=home:a2@example.test",
                 "TEL;TYPE=cell:+33 1 23", "ADR;TYPE=home:;;1 rue A;Paris;;75001;France", "ORG:ACME;R&D", "TITLE:CTO",
                 "BDAY:1980-02-03", "NOTE:note", "REV:2025-12-01T10:00:00Z"):
        assert line in text, line
    assert src.fetch(folder, items[1], "<x>").subject == ""            # no name: email is used for FN, subject stays empty
    assert b"FN:only@example.test" in src.fetch(folder, items[1], "<x>").raw
    assert src.fetch(folder, items[2], "<x>") is None                  # nothing to put in an address book


def test_people_incremental(gg):
    gg.people = people()
    src = google.GoogleSource(cfg(sync_calendar=False), {"service_account_key": SA_KEY})
    src.open()
    f = src.folders()[0]
    first = src.changed_since(f, None)
    assert first.reset and len(first.items) == 3 and json.loads(first.cursor) == {"sync": "ps1"}
    more = src.changed_since(f, first.cursor)
    assert not more.reset and [i.key for i in more.items] == ["pc103"]    # the deleted person is dropped
    gg.sync_gone = True
    full = src.changed_since(f, more.cursor)
    assert full.reset and len(full.items) == 3


# ------------------------------------------------------------------ Gmail
def rec(uid, msgid, labels, flags=("\\Seen",), day_=1, mailbox="[Gmail]/All Mail"):
    return {"uid": uid, "msgid": msgid, "flags": list(flags), "size": 100 + uid, "labels": labels,
            "date": datetime(2024, 3, day_, 12, 0, tzinfo=timezone.utc)}


class FakeGmailImap:
    """Same four calls as GmailImap, and the facts of the Gmail documentation: All Mail holds every label."""
    instances: list = []
    spam = [rec(1, "900", [])]
    trash = [rec(1, "901", [])]
    all_mail = [rec(1, "100", ["\\Inbox", "Work", "\\Important"]), rec(2, "101", ["Work/Projects", "Alpha"]),
                rec(3, "102", []), rec(4, "103", ["\\Sent"], flags=()), rec(5, "104", ["\\Drafts", "Work"]),
                rec(6, "105", ["Work"]), rec(7, "106", ["CATEGORY_PROMOTIONS", "\\Inbox"])]

    def __init__(self, params):
        self.params = params
        self.closed = False
        FakeGmailImap.instances.append(self)

    def connect(self):
        if self.params.token_source() == "bad":
            raise ConnectorError("authentication refused", ConnectorError.AUTH)

    def close(self):
        self.closed = True

    def mailboxes(self):
        return [Mailbox(name="INBOX", flags=[], delim="/"), Mailbox(name="[Gmail]", flags=["\\Noselect", "\\HasChildren"], delim="/"),
                Mailbox(name="[Gmail]/All Mail", flags=["\\HasNoChildren", "\\All"], delim="/"),
                Mailbox(name="[Gmail]/Spam", flags=["\\HasNoChildren", "\\Junk"], delim="/"),
                Mailbox(name="[Gmail]/Trash", flags=["\\HasNoChildren", "\\Trash"], delim="/")]

    def scan(self, name, after_uid=0):
        data = {"[Gmail]/All Mail": self.all_mail, "[Gmail]/Spam": self.spam, "[Gmail]/Trash": self.trash}[name]
        return 77, [dict(r) for r in data if r["uid"] > after_uid]

    def fetch_raw(self, name, uidvalidity, uid):
        if uid == 99:
            return None
        return f"Subject: mail {name} {uid}\r\nMessage-ID: <{uid}@x>\r\n\r\nbody\r\n".encode()


def gmail_source(gg, **extra):
    FakeGmailImap.instances.clear()
    src = google.GoogleSource(cfg(sync_mail=True, sync_calendar=False, sync_contacts=False, **extra),
                              {"service_account_key": SA_KEY}, {"imap": FakeGmailImap})
    src.open()
    return src


def test_gmail_labels_become_folders_without_duplicates(gg):
    src = gmail_source(gg)
    folders = src.folders()
    by = {tuple(f.path): f for f in folders}
    assert set(by) == {("INBOX",), ("Drafts",), ("Sent",), ("Alpha",), ("Work",), ("Archive",)}   # no Spam, no Trash
    assert by[("INBOX",)].special == "inbox" and by[("Sent",)].special == "sent" and by[("Drafts",)].special == "drafts"
    all_keys = [i.key for f in folders for i in src.iter_items(f)]
    assert len(all_keys) == 7 == len(set(all_keys))                       # every mail once, whatever its labels
    assert sorted(i.key for i in src.iter_items(by[("INBOX",)])) == ["g100", "g106"]   # Inbox wins over Work and the category
    assert [i.key for i in src.iter_items(by[("Alpha",)])] == ["g101"]     # first user label by name, not Work/Projects
    assert [i.key for i in src.iter_items(by[("Drafts",)])] == ["g104"]    # a draft with a label stays a draft
    assert [i.key for i in src.iter_items(by[("Work",)])] == ["g105"]
    item = next(src.iter_items(by[("Sent",)]))
    assert item.flags == [] and item.alt_key.startswith("d:") and item.size == 104
    got = src.fetch(by[("Sent",)], item, "<x>")
    assert got.raw.startswith(b"Subject: mail [Gmail]/All Mail 4") and got.subject == "mail [Gmail]/All Mail 4"
    assert got.flags == [] and got.internal_date.year == 2024
    inbox = next(iter(src.iter_items(by[("INBOX",)])))
    assert src.fetch(by[("INBOX",)], inbox, "<x>").flags == ["\\Seen"]
    p = FakeGmailImap.instances[0].params
    assert p.host == "imap.gmail.com" and p.login == USER and p.token_source() == "tok-1" and p.security == "ssl"
    assert gg.issued[0]["claims"]["scope"] == google.SCOPES["mail"]


def test_gmail_spam_and_trash_are_optional(gg):
    src = gmail_source(gg, include_trash_spam=True)
    by = {tuple(f.path): f for f in src.folders()}
    assert by[("Spam",)].special == "junk" and by[("Trash",)].special == "trash"
    assert [i.key for i in src.iter_items(by[("Trash",)])] == ["g901"]
    assert len([i for f in by.values() for i in src.iter_items(f)]) == 9


def test_gmail_incremental_by_uid(gg):
    src = gmail_source(gg)
    inbox = next(f for f in src.folders() if f.path == ["INBOX"])
    first = src.changed_since(inbox, None)
    assert first.reset and len(first.items) == 2
    FakeGmailImap.all_mail = FakeGmailImap.all_mail + [rec(8, "107", ["\\Inbox"]), rec(9, "108", ["Work"])]
    try:
        more = src.changed_since(inbox, first.cursor)
        assert not more.reset and [i.key for i in more.items] == ["g107"]
        assert json.loads(more.cursor)["top"] == {"[Gmail]/All Mail": 9}
    finally:
        FakeGmailImap.all_mail = FakeGmailImap.all_mail[:-2]


def test_gmail_all_mail_missing(gg):
    class NoAll(FakeGmailImap):
        def mailboxes(self):
            return [Mailbox(name="INBOX", flags=[], delim="/")]
    src = google.GoogleSource(cfg(sync_mail=True, sync_calendar=False, sync_contacts=False),
                              {"service_account_key": SA_KEY}, {"imap": NoAll})
    src.open()
    with pytest.raises(ConnectorError) as e:
        src.folders()
    assert e.value.kind == ConnectorError.CONFIG and "All Mail" in str(e.value)


# ------------------------------------------------------------------ pure parsing
def test_parse_labels_and_scan():
    assert google.parse_labels('\\Inbox \\Sent Important "Muy Importante" "a)b") rest') == [
        "\\Inbox", "\\Sent", "Important", "Muy Importante", "a)b"]
    assert google.parse_labels("&AOk-t&AOk-") == ["été"]
    assert google.parse_labels(")") == []
    data = [b'1 (UID 5 FLAGS (\\Seen \\Flagged) INTERNALDATE "01-Mar-2024 12:00:00 +0100" RFC822.SIZE 42 '
            b'X-GM-MSGID 1278455344230334865 X-GM-LABELS (\\Inbox "Muy Importante"))',
            (b'2 (UID 6 FLAGS () X-GM-MSGID 12 X-GM-LABELS ({5}', b'a "b"'), b'))',
            b'3 (FLAGS (\\Seen))']   # unsolicited update: ignored
    recs = google.parse_scan(data)
    assert [r["uid"] for r in recs] == [5, 6]
    assert recs[0]["msgid"] == "1278455344230334865" and recs[0]["flags"] == ["\\Seen", "\\Flagged"]
    assert recs[0]["labels"] == ["\\Inbox", "Muy Importante"] and recs[0]["size"] == 42
    assert recs[0]["date"] == datetime(2024, 3, 1, 11, 0, tzinfo=timezone.utc)
    assert recs[1]["labels"] == ['a "b"']


def test_folder_of_labels_is_deterministic():
    f = google.folder_of_labels
    assert f(["Work", "\\Inbox"]) == (["INBOX"], "inbox")
    assert f(["Work", "\\Drafts", "\\Inbox"]) == (["Drafts"], "drafts")
    assert f(["\\Sent", "Work"]) == (["Sent"], "sent")
    assert f(["zeta", "Alpha/Sub"]) == (["Alpha", "Sub"], None)
    assert f(["\\Important", "\\Starred", "CATEGORY_UPDATES"]) == (["Archive"], None)
    assert f(["b", "a"]) == f(["a", "b"])


def test_xoauth2_initial_response():
    from app.server.imap import _login_xoauth2

    class M:
        def authenticate(self, mech, fn):
            self.mech, self.first, self.second = mech, fn(b""), fn(b'{"status":"401"}')

    m = M()
    _login_xoauth2(m, USER, "ya29.token")
    assert m.mech == "XOAUTH2" and m.first == f"user={USER}\x01auth=Bearer ya29.token\x01\x01".encode()
    assert m.second == b""      # SASL: an empty answer to the error challenge


def test_ics_helpers_exact():
    ev = {"id": "x", "iCalUID": "x@g", "start": {"dateTime": "2026-07-01T10:00:00+02:00", "timeZone": "Europe/Paris"},
          "end": {"dateTime": "2026-07-01T11:00:00+02:00", "timeZone": "Europe/Paris"}, "summary": "a" * 120}
    out = google.events_to_ics([ev])
    assert "DTSTART:20260701T080000Z" in out                      # single event: UTC
    assert all(len(line.encode()) <= 75 for line in out.split("\r\n"))   # folded
    assert "SUMMARY:" + "a" * 60 in out.replace("\r\n ", "")
