"""exchange_ews, m365_graph, IMAP XOAUTH2, master user, Sieve, against simulated servers (fake_exchange.py). These tests
prove the logic of the connectors. They do not prove that real Exchange behaves like the simulation: docs/exchange.md."""
import base64
import socket
import threading
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app.connectors import ews, m365, oauth, pim, registry, sieve, wiring
from app.connectors import exchange_base as xb
from app.connectors import imap as cimap
from app.connectors.base import (CALENDAR, CONTACTS, MAIL, TASKS, ConnectorError, FolderPlan, OutgoingItem, SourceFolder,
                                 Target, TargetSession)
from tests.server import fake_exchange as fx
from tests.server.test_connectors import log_of, make_account, run_job, start

EWS_CONFIG = {"url": "https://ews.example.test/EWS/Exchange.asmx", "login": "me@example.test"}
GRAPH_CONFIG = {"tenant_id": "contoso", "client_id": "app-1", "mailbox": "me@contoso.test"}
UTC = timezone.utc
MIME = (b"From: a@example.test\r\nTo: b@example.test\r\nSubject: Hello\r\nMessage-ID: <1@example.test>\r\n"
        b"Date: Fri, 01 Mar 2024 12:00:00 +0000\r\n\r\nbody\r\n")


@pytest.fixture
def waits(monkeypatch):
    out: list[float] = []
    monkeypatch.setattr(ews, "_sleep", out.append)
    monkeypatch.setattr(m365, "_sleep", out.append)
    return out


@pytest.fixture
def fake_ews(monkeypatch, waits):
    fake = fx.FakeEws()
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(fake))
    return fake


@pytest.fixture
def fake_graph(monkeypatch, waits):
    fake = fx.FakeGraph()
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(fake))
    return fake


def ews_source(**config):
    src = ews.EwsSource({**EWS_CONFIG, "auth": "basic", **config}, {"password": "pw"})
    src.open()
    return src


def ews_session(**config):
    target = ews.EwsTarget({**EWS_CONFIG, "auth": "basic", **config}, {"password": "pw"})
    session = target.session()
    session.connect()
    return session


def graph_source(**config):
    src = m365.GraphSource({**GRAPH_CONFIG, **config}, {"client_secret": fx.FakeGraph.SECRET})
    src.open()
    return src


def graph_session(**config):
    session = m365.GraphTarget({**GRAPH_CONFIG, **config}, {"client_secret": fx.FakeGraph.SECRET}).session()
    session.connect()
    return session


def folder_of(folders, *path, kind=None):
    for f in folders:
        if f.path == list(path) and (kind is None or f.kind == kind):
            return f
    raise AssertionError(f"no folder {path} in {[x.path for x in folders]}")


# ------------------------------------------------------------------ the catalogue
def test_catalogue():
    cat = {c["kind"]: c for c in registry.catalogue()}
    for kind in ("exchange_ews", "m365_graph"):
        assert cat[kind]["source"]["items"] == ["mail", "calendar", "contacts", "tasks"]
        assert cat[kind]["target"]["items"] == ["mail", "calendar", "contacts", "tasks"]
        assert "rules" in cat[kind]["source"]["capabilities"] and "incremental" in cat[kind]["source"]["capabilities"]
        assert "readback" not in cat[kind]["target"]["capabilities"]  # Exchange rewrites the MIME
        assert "not real Exchange" in cat[kind]["description"]
    secrets = {f["name"] for f in cat["m365_graph"]["source"]["fields"] if f["secret"]}
    assert secrets == {"client_secret"}
    assert {f["name"] for f in cat["exchange_ews"]["source"]["fields"] if f["secret"]} == {"password"}
    assert {f["name"] for f in cat["imap_oauth2"]["source"]["fields"] if f["secret"]} == {"client_secret", "refresh_token"}
    assert "ews" not in str(cat["m365_graph"]["source"]["fields"]).lower()
    assert cat["carddav"]["source"]["items"] == ["contacts"]


def test_identity_is_the_mailbox_not_the_credential():
    own = registry.namespace("exchange_ews", "source", {**EWS_CONFIG})
    assert own == registry.namespace("exchange_ews", "source", {**EWS_CONFIG, "auth": "ntlm"})
    a = registry.namespace("exchange_ews", "source", {**EWS_CONFIG, "access": "impersonation", "mailbox": "a@x.test"})
    b = registry.namespace("exchange_ews", "source", {**EWS_CONFIG, "login": "svc2", "access": "impersonation", "mailbox": "A@x.test"})
    assert a == b and a != own
    g = registry.namespace("m365_graph", "source", GRAPH_CONFIG)
    assert g != registry.namespace("m365_graph", "source", {**GRAPH_CONFIG, "mailbox": "other@contoso.test"})


# ------------------------------------------------------------------ neutral model
def series() -> pim.Event:
    start = datetime(2024, 3, 4, 8, 0, tzinfo=UTC)  # a Monday, 09:00 in Paris
    e = pim.Event(uid="uid-1", start=start, end=start + timedelta(hours=1), subject="Weekly été", body="line 1\nline 2",
                  location="Room; A", tz="Europe/Paris", categories=["Work"], reminder_minutes=15,
                  organizer=pim.Person("boss@example.test", "The Boss"),
                  attendees=[pim.Person("a@example.test", "Ann", "REQ", "ACCEPTED"), pim.Person("b@example.test", "", "OPT"),
                             pim.Person("room@example.test", "Room", "RES")],
                  recurrence=pim.Recurrence("WEEKLY", 1, ["MO"], count=10, week_start="MO"),
                  exdates=[start + timedelta(days=7)])
    moved = start + timedelta(days=14)
    e.overrides.append(pim.Event(uid="uid-1", start=moved + timedelta(hours=2), end=moved + timedelta(hours=3), subject="Moved",
                                 tz="Europe/Paris", recurrence_id=moved))
    return e


def test_event_round_trip():
    raw = pim.event_to_ics(series())
    assert b"BEGIN:VTIMEZONE" in raw and b"TZID=Europe/Paris" in raw
    assert b"SCHEDULE-AGENT=CLIENT" in raw
    back = pim.ics_to_event(raw)
    assert back.uid == "uid-1" and back.subject == "Weekly été" and back.tz == "Europe/Paris"
    assert back.start == datetime(2024, 3, 4, 8, 0, tzinfo=UTC) and back.location == "Room; A"
    assert back.body == "line 1\nline 2" and back.reminder_minutes == 15 and back.categories == ["Work"]
    assert back.recurrence.freq == "WEEKLY" and back.recurrence.by_day == ["MO"] and back.recurrence.count == 10
    assert back.exdates == [datetime(2024, 3, 11, 8, 0, tzinfo=UTC)]
    assert len(back.overrides) == 1 and back.overrides[0].subject == "Moved"
    assert back.overrides[0].recurrence_id == datetime(2024, 3, 18, 8, 0, tzinfo=UTC)
    assert back.organizer.email == "boss@example.test"
    roles = {a.email: (a.role, a.partstat) for a in back.attendees}
    assert roles == {"a@example.test": ("REQ", "ACCEPTED"), "b@example.test": ("OPT", "NEEDS-ACTION"),
                     "room@example.test": ("RES", "NEEDS-ACTION")}


def test_all_day_and_until_round_trip():
    e = pim.Event(uid="u2", start=date(2024, 7, 1), end=date(2024, 7, 2), subject="Day", tz="Europe/Paris",
                  recurrence=pim.Recurrence("YEARLY", 1, month_day=1, month=7, until=date(2030, 7, 1)))
    back = pim.ics_to_event(pim.event_to_ics(e))
    assert back.all_day and back.start == date(2024, 7, 1) and back.end == date(2024, 7, 2)
    assert back.recurrence.until == date(2030, 7, 1) and back.recurrence.month == 7 and back.recurrence.month_day == 1
    t2 = pim.Event(uid="u3", start=datetime(2024, 3, 1, 8, tzinfo=UTC), end=datetime(2024, 3, 1, 9, tzinfo=UTC), tz="Asia/Tokyo",
                   recurrence=pim.Recurrence("MONTHLY", 2, ["FR"], set_pos=-1, until=date(2025, 3, 28)))
    back = pim.ics_to_event(pim.event_to_ics(t2))
    assert (back.recurrence.by_day, back.recurrence.set_pos, back.recurrence.interval) == (["FR"], -1, 2)
    assert back.recurrence.until == date(2025, 3, 28)


def test_unsupported_recurrence_warns_and_keeps_the_event():
    raw = (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:u\r\nDTSTAMP:20240101T000000Z\r\n"
           b"DTSTART:20240101T100000Z\r\nDTEND:20240101T110000Z\r\nRRULE:FREQ=HOURLY;INTERVAL=2\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    e = pim.ics_to_event(raw)
    assert e.recurrence is None and any("recurrence-unsupported" in w for w in e.warnings)


def test_ics_refused():
    with pytest.raises(ConnectorError) as err:
        pim.ics_to_event(b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    assert err.value.kind == ConnectorError.REJECTED


def test_contact_round_trip():
    c = pim.Contact(uid="c1", full_name="Ann, O'Neil", surname="O'Neil", given="Ann", prefix="Dr", company="ACME; Inc",
                    department="R&D", title="CTO", emails=["ann@example.test", "a2@example.test"],
                    phones=[("business", "+33 1"), ("mobile", "+33 6"), ("home_fax", "+33 2"), ("pager", "42")],
                    addresses=[{"kind": "work", "street": "1 rue\nBis", "city": "Paris", "postal": "75001", "country": "FR"}],
                    birthday=date(1980, 2, 3), anniversary=date(2005, 6, 7), urls=["https://example.test"],
                    categories=["A", "B"], note="Note, with; marks\nand lines", im="ann@im.test")
    back = pim.vcard_to_contact(pim.contact_to_vcard(c))
    for name in ("uid", "full_name", "surname", "given", "prefix", "company", "department", "title", "birthday", "anniversary",
                 "urls", "categories", "note", "im", "emails"):
        assert getattr(back, name) == getattr(c, name), name
    assert sorted(back.phones) == sorted(c.phones)
    assert back.addresses[0]["street"] == "1 rue\nBis" and back.addresses[0]["city"] == "Paris"


def test_vcard_from_elsewhere():
    raw = (b"BEGIN:VCARD\r\nVERSION:4.0\r\nFN:Bob\r\nN:;Bob;;;\r\nitem1.TEL;TYPE=work:tel:+1\r\nTEL;TYPE=cell:+2\r\n"
           b"EMAIL:bob@example.test\r\nBDAY:19700102\r\nPHOTO:data:x\r\nEND:VCARD\r\n")
    c = pim.vcard_to_contact(raw)
    assert c.full_name == "Bob" and ("business", "+1") in c.phones and ("mobile", "+2") in c.phones
    assert c.birthday == date(1970, 1, 2) and "photo-not-copied" in c.warnings


def test_task_round_trip():
    t = pim.Task(uid="t1", subject="Do it", body="now", status="IN-PROCESS", priority="high", percent=40,
                 due=datetime(2024, 5, 1, 0, 0, tzinfo=UTC), start=datetime(2024, 4, 1, 0, 0, tzinfo=UTC), categories=["X"],
                 recurrence=pim.Recurrence("WEEKLY", 2, ["TU"]))
    back = pim.ics_to_task(pim.task_to_ics(t))
    assert (back.subject, back.body, back.status, back.priority, back.percent) == ("Do it", "now", "IN-PROCESS", "high", 40)
    assert back.due == t.due and back.start == t.start and back.recurrence.by_day == ["TU"]


def test_time_zones():
    assert pim.iana_of("Romance Standard Time") == "Europe/Paris" and pim.iana_of("Europe/Berlin") == "Europe/Berlin"
    assert pim.iana_of("UTC") == "UTC" and pim.iana_of("tzone://Microsoft/Custom") is None
    assert pim.windows_zone("Europe/Paris") == "Romance Standard Time"
    # no line in the CLDR territory 001 table: found by its offsets over the year
    other = pim.windows_zone("Europe/Zurich", datetime(2024, 1, 1, tzinfo=UTC))
    assert other and pim.zone(pim.iana_of(other)).utcoffset(datetime(2024, 7, 1)) == pim.zone("Europe/Zurich").utcoffset(datetime(2024, 7, 1))
    assert pim.windows_zone("Not/AZone") is None


def test_parse_utc_and_all_day():
    assert pim.parse_utc("2024-03-01T12:00:00.0000000") == datetime(2024, 3, 1, 12, tzinfo=UTC)
    assert pim.parse_utc("2024-03-01T12:00:00+01:00") == datetime(2024, 3, 1, 11, tzinfo=UTC)
    assert pim.parse_utc("nope") is None
    assert pim.all_day_date(datetime(2024, 2, 29, 23, tzinfo=UTC), "Europe/Paris") == date(2024, 3, 1)
    assert pim.all_day_date(datetime(2024, 3, 1, 0, tzinfo=UTC), "Europe/Paris") == date(2024, 3, 1)


def test_folder_plan():
    from app.server.runner import folder_key
    folders = [SourceFolder(["INBOX"], MAIL, "inbox"), SourceFolder(["Envoyés"], MAIL, "sent"), SourceFolder(["A/B", "C"], MAIL),
               SourceFolder(["Calendrier"], CALENDAR), SourceFolder(["Travail"], CALENDAR), SourceFolder(["P"], CONTACTS, xb.DEFAULT)]
    plan = xb.plan(folders, "Imported")
    assert plan["INBOX"].name == "mail:@inbox" and plan["Envoyés"].name == "mail:@sentitems"
    assert plan["Calendrier"].name == "calendar:@default" and plan["P"].name == "contacts:@default"
    assert plan["Travail"].name == "calendar:Imported/Travail"
    assert xb.parse_spec(plan[folder_key(["A/B", "C"])].name) == ("mail", None, ["Imported", "A/B", "C"])
    assert xb.parse_spec("calendar:@default") == ("calendar", "default", [])


# ------------------------------------------------------------------ EWS: reading
MAIL_XML = ('<t:Message xmlns:t="{t}"><t:MimeContent CharacterSet="UTF-8">{mime}</t:MimeContent><t:ItemClass>IPM.Note</t:ItemClass>'
            "<t:Size>{size}</t:Size><t:DateTimeReceived>{date}</t:DateTimeReceived><t:IsDraft>false</t:IsDraft>"
            '<t:ExtendedProperty><t:ExtendedFieldURI PropertyTag="0x1090" PropertyType="Integer"/><t:Value>2</t:Value></t:ExtendedProperty>'
            '<t:ExtendedProperty><t:ExtendedFieldURI PropertyTag="0x1081" PropertyType="Integer"/><t:Value>102</t:Value></t:ExtendedProperty>'
            "<t:InternetMessageId>{mid}</t:InternetMessageId><t:IsRead>{read}</t:IsRead></t:Message>")


def mail_xml(i=1, read="true", date_text="2024-03-01T12:00:00Z"):
    return MAIL_XML.format(t=fx.NS_T, mime=base64.b64encode(MIME).decode(), size=len(MIME), date=date_text,
                           mid=f"&lt;{i}@example.test&gt;", read=read)


RECURRING_XML = """<t:CalendarItem xmlns:t="{t}"><t:Subject>Standup</t:Subject><t:Sensitivity>Normal</t:Sensitivity>
<t:Body BodyType="Text">Daily talk</t:Body><t:DateTimeCreated>2024-01-01T00:00:00Z</t:DateTimeCreated>
<t:ReminderIsSet>true</t:ReminderIsSet><t:ReminderMinutesBeforeStart>10</t:ReminderMinutesBeforeStart>
<t:HasAttachments>true</t:HasAttachments><t:LastModifiedTime>2024-02-01T00:00:00Z</t:LastModifiedTime><t:UID>040000008200E00</t:UID>
<t:Start>2024-03-04T08:00:00Z</t:Start><t:End>2024-03-04T08:30:00Z</t:End><t:IsAllDayEvent>false</t:IsAllDayEvent>
<t:LegacyFreeBusyStatus>Busy</t:LegacyFreeBusyStatus><t:Location>Room 1</t:Location><t:IsCancelled>false</t:IsCancelled>
<t:CalendarItemType>RecurringMaster</t:CalendarItemType>
<t:Organizer><t:Mailbox><t:Name>Boss</t:Name><t:EmailAddress>boss@example.test</t:EmailAddress><t:RoutingType>SMTP</t:RoutingType></t:Mailbox></t:Organizer>
<t:RequiredAttendees><t:Attendee><t:Mailbox><t:Name>Ann</t:Name><t:EmailAddress>ann@example.test</t:EmailAddress></t:Mailbox><t:ResponseType>Accept</t:ResponseType></t:Attendee>
<t:Attendee><t:Mailbox><t:Name>Old</t:Name><t:EmailAddress>/O=ACME/CN=OLD</t:EmailAddress><t:RoutingType>EX</t:RoutingType></t:Mailbox></t:Attendee></t:RequiredAttendees>
<t:OptionalAttendees><t:Attendee><t:Mailbox><t:EmailAddress>bob@example.test</t:EmailAddress></t:Mailbox><t:ResponseType>Tentative</t:ResponseType></t:Attendee></t:OptionalAttendees>
<t:Recurrence><t:WeeklyRecurrence><t:Interval>1</t:Interval><t:DaysOfWeek>Monday</t:DaysOfWeek><t:FirstDayOfWeek>Monday</t:FirstDayOfWeek></t:WeeklyRecurrence>
<t:NumberedRecurrence><t:StartDate>2024-03-04</t:StartDate><t:NumberOfOccurrences>8</t:NumberOfOccurrences></t:NumberedRecurrence></t:Recurrence>
<t:ModifiedOccurrences><t:Occurrence><t:ItemId Id="exc-1"/><t:Start>2024-03-11T09:00:00Z</t:Start><t:End>2024-03-11T09:30:00Z</t:End><t:OriginalStart>2024-03-11T08:00:00Z</t:OriginalStart></t:Occurrence></t:ModifiedOccurrences>
<t:DeletedOccurrences><t:DeletedOccurrence><t:Start>2024-03-18T08:00:00Z</t:Start></t:DeletedOccurrence></t:DeletedOccurrences>
<t:StartTimeZone Id="Romance Standard Time" Name="Paris"/></t:CalendarItem>"""
EXCEPTION_XML = """<t:CalendarItem xmlns:t="{t}"><t:Subject>Standup moved</t:Subject><t:Body BodyType="Text">Later</t:Body><t:UID>040000008200E00</t:UID>
<t:Start>2024-03-11T09:00:00Z</t:Start><t:End>2024-03-11T09:30:00Z</t:End><t:IsAllDayEvent>false</t:IsAllDayEvent>
<t:CalendarItemType>Exception</t:CalendarItemType><t:OriginalStart>2024-03-11T08:00:00Z</t:OriginalStart></t:CalendarItem>"""
CONTACT_XML = """<t:Contact xmlns:t="{t}"><t:Body BodyType="Text">A note</t:Body><t:Categories><t:String>VIP</t:String></t:Categories>
<t:LastModifiedTime>2024-01-05T00:00:00Z</t:LastModifiedTime><t:DisplayName>Ann Lee</t:DisplayName><t:GivenName>Ann</t:GivenName>
<t:CompleteName><t:Title>Dr</t:Title></t:CompleteName><t:CompanyName>ACME</t:CompanyName>
<t:EmailAddresses><t:Entry Key="EmailAddress1">SMTP:ann@example.test</t:Entry><t:Entry Key="EmailAddress2">/O=X/CN=Y</t:Entry></t:EmailAddresses>
<t:PhysicalAddresses><t:Entry Key="Business"><t:Street>1 Main</t:Street><t:City>Lyon</t:City><t:PostalCode>69000</t:PostalCode></t:Entry></t:PhysicalAddresses>
<t:PhoneNumbers><t:Entry Key="MobilePhone">+336</t:Entry><t:Entry Key="BusinessFax">+331</t:Entry></t:PhoneNumbers>
<t:Birthday>1980-02-03T00:00:00Z</t:Birthday><t:JobTitle>CTO</t:JobTitle><t:Surname>Lee</t:Surname></t:Contact>"""
TASK_XML = """<t:Task xmlns:t="{t}"><t:Subject>Report</t:Subject><t:Body BodyType="Text">Write it</t:Body><t:Importance>High</t:Importance>
<t:DueDate>2024-04-01T00:00:00Z</t:DueDate><t:PercentComplete>50</t:PercentComplete><t:StartDate>2024-03-01T00:00:00Z</t:StartDate><t:Status>InProgress</t:Status></t:Task>"""


def seed_ews(fake):
    fake.add_folder("work", "Work", "inbox")
    fake.add_folder("hidden", "Sync", "msgfolderroot", hidden=True)
    fake.add_folder("quick", "Quick Contacts", "contacts", "IPF.Contact.MOC.QuickContacts")
    fake.add_folder("cal2", "Team", "msgfolderroot", "IPF.Appointment")
    for i in range(5):
        fake.add_item("id-inbox", mail_xml(i, "true" if i % 2 else "false"), rid=f"m{i}")
    fake.add_item("id-calendar", RECURRING_XML.format(t=fx.NS_T), rid="rec1")
    fake.add_item("id-calendar", EXCEPTION_XML.format(t=fx.NS_T), rid="exc-1")
    del fake.items["exc-1"]["folder"]
    fake.items["exc-1"]["folder"] = "elsewhere"  # exceptions are not listed in the calendar folder
    fake.add_item("id-contacts", CONTACT_XML.format(t=fx.NS_T), rid="ct1")
    fake.add_item("id-tasks", TASK_XML.format(t=fx.NS_T), rid="tk1")


def test_ews_folders(fake_ews):
    seed_ews(fake_ews)
    src = ews_source()
    folders = src.folders()
    paths = {(f.kind, tuple(f.path)): f for f in folders}
    assert (MAIL, ("Inbox",)) in paths and paths[(MAIL, ("Inbox",))].special == "inbox"
    assert paths[(MAIL, ("Sentitems",))].special == "sent" and paths[(MAIL, ("Inbox", "Work"))].special is None
    assert paths[(CALENDAR, ("Calendar",))].special == xb.DEFAULT and (CALENDAR, ("Team",)) in paths
    assert paths[(CONTACTS, ("Contacts",))].special == xb.DEFAULT and paths[(TASKS, ("Tasks",))].special == xb.DEFAULT
    assert (MAIL, ("Sync",)) not in paths and (MAIL, ("Outbox",)) not in paths  # hidden, outbox
    assert not [f for f in folders if "Quick" in f.path[-1]]
    assert paths[(MAIL, ("Inbox",))].count == 5


def test_ews_paging_keys_and_flags(fake_ews):
    seed_ews(fake_ews)
    src = ews_source()
    inbox = folder_of(src.folders(), "Inbox")
    items = list(src.iter_items(inbox))
    assert len(items) == 5  # three pages of two
    assert fake_ews.calls.count("FindItem") == 3
    assert len({i.key for i in items}) == 5 and all(i.key.isascii() and len(i.key) <= 200 for i in items)
    again = [i.key for i in src.iter_items(inbox)]
    assert again == [i.key for i in items]
    assert items[1].flags == ["\\Seen", "\\Flagged", "\\Answered"] and items[0].flags == ["\\Flagged", "\\Answered"]
    assert items[0].date == datetime(2024, 3, 1, 12, tzinfo=UTC) and items[0].size == len(MIME)
    assert items[0].alt_key.startswith("m:") and items[0].alt_key != items[1].alt_key


def test_ews_fetch_mail(fake_ews):
    seed_ews(fake_ews)
    src = ews_source()
    inbox = folder_of(src.folders(), "Inbox")
    item = next(iter(src.iter_items(inbox)))
    got = src.fetch(inbox, item, "<x>")
    assert got.raw == MIME and got.subject == "Hello" and got.internal_date == datetime(2024, 3, 1, 12, tzinfo=UTC)
    assert "\\Flagged" in got.flags
    item.ref = "gone"
    assert src.fetch(inbox, item, "<x>") is None


def test_ews_fetch_event_with_exceptions(fake_ews):
    seed_ews(fake_ews)
    src = ews_source()
    cal = folder_of(src.folders(), "Calendar")
    item = next(iter(src.iter_items(cal)))
    assert item.date == datetime(2024, 3, 4, 8, tzinfo=UTC)
    got = src.fetch(cal, item, "<x>")
    e = pim.ics_to_event(got.raw)
    assert e.uid == "040000008200E00" and e.tz == "Europe/Paris" and e.recurrence.count == 8
    assert e.recurrence.by_day == ["MO"] and e.recurrence.week_start == "MO"
    assert e.exdates == [datetime(2024, 3, 18, 8, tzinfo=UTC)]
    assert [(o.subject, o.recurrence_id) for o in e.overrides] == [("Standup moved", datetime(2024, 3, 11, 8, tzinfo=UTC))]
    assert e.overrides[0].start == datetime(2024, 3, 11, 9, tzinfo=UTC)
    assert {a.email: (a.role, a.partstat) for a in e.attendees} == {"ann@example.test": ("REQ", "ACCEPTED"),
                                                                    "bob@example.test": ("OPT", "TENTATIVE")}
    assert e.organizer.email == "boss@example.test" and e.reminder_minutes == 10
    assert "attachments-not-copied" in got.warnings
    assert any("attendee-without-address:Old" in w for w in got.warnings)


def test_ews_contact_and_task(fake_ews):
    seed_ews(fake_ews)
    src = ews_source()
    folders = src.folders()
    book = folder_of(folders, "Contacts")
    got = src.fetch(book, next(iter(src.iter_items(book))), "<x>")
    c = pim.vcard_to_contact(got.raw)
    assert c.full_name == "Ann Lee" and c.prefix == "Dr" and c.emails == ["ann@example.test"]
    assert ("mobile", "+336") in c.phones and ("business_fax", "+331") in c.phones
    assert c.addresses[0]["kind"] == "work" and c.addresses[0]["city"] == "Lyon" and c.birthday == date(1980, 2, 3)
    assert c.note == "A note" and c.categories == ["VIP"] and got.subject == "Ann Lee"
    assert "email-without-smtp-address" in got.warnings
    tasks = folder_of(folders, "Tasks")
    got = src.fetch(tasks, next(iter(src.iter_items(tasks))), "<x>")
    t = pim.ics_to_task(got.raw)
    assert t.subject == "Report" and t.status == "IN-PROCESS" and t.priority == "high" and t.percent == 50
    assert t.due == datetime(2024, 4, 1, tzinfo=UTC)


def test_ews_changed_since(fake_ews):
    seed_ews(fake_ews)
    src = ews_source()
    inbox = folder_of(src.folders(), "Inbox")
    first = src.changed_since(inbox, None)
    assert first.reset and len(first.items) == 5 and first.cursor == "state-5"
    assert fake_ews.calls.count("SyncFolderItems") == 3
    fake_ews.add_item("id-inbox", mail_xml(9), rid="m9")
    second = src.changed_since(inbox, first.cursor)
    assert not second.reset and [i.ref for i in second.items] == ["m9"]
    fake_ews.sync_invalid = True  # the state no longer valid: a full listing, said so
    third = src.changed_since(inbox, second.cursor)
    assert third.reset and len(third.items) == 6


def test_ews_server_busy_backs_off(fake_ews, waits):
    src = ews_source()
    fake_ews.busy_next = 2
    src.folders()
    assert waits == [1.5, 1.5]
    waits.clear()
    fake_ews.busy_next = 1
    fake_ews.busy_http500 = True
    src.folders()
    assert waits == [1.5]


def test_ews_server_busy_gives_up_as_transient(fake_ews):
    src = ews_source()
    fake_ews.busy_next = 100
    with pytest.raises(ConnectorError) as err:
        src.folders()
    assert err.value.kind == ConnectorError.TRANSIENT and err.value.retryable


def test_ews_bad_password_and_errors(monkeypatch, fake_ews):
    bad = ews.EwsSource({**EWS_CONFIG, "auth": "basic"}, {"password": "no"})
    with pytest.raises(ConnectorError) as err:
        bad.open()
    assert err.value.kind == ConnectorError.AUTH
    assert ews.classify("ErrorQuotaExceeded", "").kind == ConnectorError.QUOTA
    assert ews.classify("ErrorImpersonateUserDenied", "").kind == ConnectorError.AUTH
    assert ews.classify("ErrorNonExistentMailbox", "").kind == ConnectorError.CONFIG
    assert ews.classify("ErrorFolderNotFound", "").kind == ConnectorError.FOLDER_MISSING
    assert ews.classify("ErrorMessageSizeExceeded", "").kind == ConnectorError.REJECTED
    assert ews.classify("ErrorMailboxStoreUnavailable", "", connecting=True).kind == ConnectorError.UNREACHABLE
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(ConnectorError) as err:
        ews.EwsSource({**EWS_CONFIG, "auth": "basic"}, {"password": "pw"}).open()
    assert err.value.kind == ConnectorError.CONFIG


def test_ews_network_failure(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(boom))
    with pytest.raises(ConnectorError) as err:
        ews.EwsSource({**EWS_CONFIG, "auth": "basic"}, {"password": "pw"}).open()
    assert err.value.kind == ConnectorError.UNREACHABLE


def test_ews_impersonation(fake_ews):
    src = ews_source(access="impersonation", mailbox="ann@example.test")
    src.folders()
    head = fake_ews.requests[0].find(f"{{{fx.NS_S}}}Header")
    assert head.find(fx.t("ExchangeImpersonation")).find(fx.t("ConnectingSID")).findtext(fx.t("SmtpAddress")) == "ann@example.test"
    assert fake_ews.headers[0]["x-anchormailbox"] == "ann@example.test"
    assert head.find(fx.t("RequestServerVersion")).get("Version") == "Exchange2013"


def test_ews_delegate_names_the_mailbox_in_folder_ids(fake_ews):
    src = ews_source(access="delegate", mailbox="shared@example.test")
    src.folders()
    refs = list(fake_ews.requests[-1].iter(fx.t("DistinguishedFolderId")))
    assert refs and all(r.find(fx.t("Mailbox")).findtext(fx.t("EmailAddress")) == "shared@example.test" for r in refs)
    assert fake_ews.requests[-1].find(f"{{{fx.NS_S}}}Header").find(fx.t("ExchangeImpersonation")) is None


def test_ews_requires_a_mailbox_for_admin_access():
    with pytest.raises(ConnectorError) as err:
        ews.EwsSource({**EWS_CONFIG, "access": "impersonation"}, {"password": "pw"}).open()
    assert err.value.kind == ConnectorError.CONFIG


def test_ews_basic_refused_over_http(fake_ews):
    with pytest.raises(ConnectorError) as err:
        ews.EwsSource({**EWS_CONFIG, "url": "http://ews.example.test/EWS/Exchange.asmx", "auth": "basic"}, {"password": "pw"}).open()
    assert err.value.kind == ConnectorError.CONFIG and "plain HTTP" in str(err.value)


def test_ews_auto_uses_basic_when_it_is_what_the_server_asks(fake_ews):
    src = ews.EwsSource({**EWS_CONFIG}, {"password": "pw"})
    src.open()
    assert src.client is not None


def test_ews_ntlm_handshake(monkeypatch):
    """Messages of the handshake, not a real server: the challenge is the example of MS-NLMP section 4.2.4."""
    challenge = bytes.fromhex(
        "4e544c4d53535000020000000c000c003800000033828ae20123456789abcdef00000000000000002400240044000000"
        "060070170000000f53006500720076006500720002000c0044006f006d00610069006e0001000c005300650072007600"
        "6500720000000000")
    seen: list[str] = []

    def server(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        seen.append(auth.split(" ")[0])
        if not auth:
            return httpx.Response(401, headers={"WWW-Authenticate": "NTLM"})
        blob = base64.b64decode(auth.split(" ", 1)[1])
        assert blob.startswith(b"NTLMSSP\0")
        kind = int.from_bytes(blob[8:12], "little")
        if kind == 1:
            return httpx.Response(401, headers={"WWW-Authenticate": "NTLM " + base64.b64encode(challenge).decode()})
        assert kind == 3
        seen.append("type3")
        return httpx.Response(200, content=(
            f'<s:Envelope xmlns:s="{fx.NS_S}" xmlns:m="{fx.NS_M}"><s:Body><m:GetFolderResponse><m:ResponseMessages>'
            '<m:Message ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode></m:Message>'
            "</m:ResponseMessages></m:GetFolderResponse></s:Body></s:Envelope>").encode())

    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(server))
    src = ews.EwsSource({**EWS_CONFIG, "login": "DOMAIN\\User", "auth": "ntlm"}, {"password": "Password"})
    src.open()
    assert seen == ["", "NTLM", "NTLM", "type3"]


# ------------------------------------------------------------------ EWS: writing
def out_item(kind, raw, import_id="<src-1-a>", flags=(), when=None, name=None):
    return OutgoingItem(kind, raw, import_id, list(flags), when, name)


def test_ews_put_mail_keeps_flags_and_dates(fake_ews):
    s = ews_session()
    when = datetime(2024, 3, 1, 12, tzinfo=UTC)
    rid = s.put("mail:@inbox", out_item(MAIL, MIME, flags=["\\Seen", "\\Flagged", "\\Answered"], when=when))
    item = fake_ews.items[rid]
    el = item["el"]
    assert item["folder"] == "id-inbox" and item["attrs"]["MessageDisposition"] == "SaveOnly"
    assert base64.b64decode(el.findtext(fx.t("MimeContent"))) == MIME and el.findtext(fx.t("IsRead")) == "true"
    props = {(p.find(fx.t("ExtendedFieldURI")).get("PropertyTag") or p.find(fx.t("ExtendedFieldURI")).get("PropertyName")): p.findtext(fx.t("Value"))
             for p in el.findall(fx.t("ExtendedProperty"))}
    assert props[xb.IMPORT_PROP] == "<src-1-a>" and props["0x0E07"] == "1"
    assert props["0x0E06"] == props["0x0039"] == "2024-03-01T12:00:00Z"
    assert props["0x1090"] == "2" and props["0x1081"] == "102"
    # element order of the schema: MimeContent first, IsRead after the extended properties
    tags = [c.tag.rsplit("}", 1)[-1] for c in el if c.tag != fx.t("ItemId")]
    assert tags[0] == "MimeContent" and tags[-1] == "IsRead"


def test_ews_draft_stays_a_draft(fake_ews):
    s = ews_session()
    rid = s.put("mail:@drafts", out_item(MAIL, MIME, flags=["\\Draft"]))
    props = {p.find(fx.t("ExtendedFieldURI")).get("PropertyTag"): p.findtext(fx.t("Value"))
             for p in fake_ews.items[rid]["el"].findall(fx.t("ExtendedProperty"))}
    assert props["0x0E07"] == "8"


def test_ews_find_is_exact_and_count_only_ours(fake_ews):
    s = ews_session()
    s.put("mail:@inbox", out_item(MAIL, MIME, "<src-1-abc>"))
    s.put("mail:@inbox", out_item(MAIL, MIME, "<src-1-ABC>"))
    s.put("mail:@inbox", out_item(MAIL, MIME, "<src-2-z>"))
    fake_ews.add_item("id-inbox", mail_xml(1), rid="theirs")
    assert len(s.find("mail:@inbox", out_item(MAIL, b"", "<src-1-abc>"))) == 1  # the server's comparison ignores case
    assert s.find("mail:@inbox", out_item(MAIL, b"", "<src-1-ab>")) == []
    assert s.count_ours("mail:@inbox", "src-1") == 2 and s.count_ours("mail:@inbox", "src-3") == 0


def test_ews_folders_are_created_by_kind(fake_ews):
    s = ews_session()
    with pytest.raises(ConnectorError) as err:
        s.put("mail:Projects/2024", out_item(MAIL, MIME))
    assert err.value.kind == ConnectorError.FOLDER_MISSING
    s.ensure_folder("mail:Projects/2024")
    s.ensure_folder("mail:Projects/2024")  # already there: not an error
    s.ensure_folder("calendar:Team")
    s.ensure_folder("contacts:Friends")
    s.ensure_folder("tasks:Later")
    classes = {f["name"]: f["class"] for f in fake_ews.folders.values()}
    assert classes["Projects"] == "IPF.Note" and classes["2024"] == "IPF.Note"
    assert classes["Team"] == "IPF.Appointment" and classes["Friends"] == "IPF.Contact" and classes["Later"] == "IPF.Task"
    rid = s.put("mail:Projects/2024", out_item(MAIL, MIME))
    assert fake_ews.folders[fake_ews.items[rid]["folder"]]["name"] == "2024"
    assert len([f for f in fake_ews.folders.values() if f["name"] == "2024"]) == 1


def test_ews_create_folder_race(fake_ews):
    s = ews_session()
    s.ensure_folder("mail:Race")
    other = ews_session()
    real = other._child
    answers = [None]  # the first lookup comes before the other session creates it, the next ones see it
    other._child = lambda parent, name: answers.pop() if answers else real(parent, name)
    other.ensure_folder("mail:Race")  # ErrorFolderExists: the existing one is used
    assert len([f for f in fake_ews.folders.values() if f["name"] == "Race"]) == 1


def test_ews_put_event_never_invites_and_applies_exceptions(fake_ews):
    s = ews_session()
    rid = s.put("calendar:@default", out_item(CALENDAR, pim.event_to_ics(series())))
    item = fake_ews.items[rid]
    el = item["el"]
    assert item["folder"] == "id-calendar" and item["attrs"]["SendMeetingInvitations"] == "SendToNone"
    assert el.findtext(fx.t("UID")) == "uid-1" and el.findtext(fx.t("Start")) == "2024-03-04T08:00:00Z"
    assert el.find(fx.t("StartTimeZone")).get("Id") == "Romance Standard Time"
    assert [a.find(fx.t("Mailbox")).findtext(fx.t("EmailAddress")) for a in el.find(fx.t("RequiredAttendees"))] == ["a@example.test"]
    assert el.find(fx.t("Resources")) is not None and el.find(fx.t("OptionalAttendees")) is not None
    assert el.find(fx.t("Recurrence")).find(fx.t("WeeklyRecurrence")).findtext(fx.t("DaysOfWeek")) == "Monday"
    assert el.find(fx.t("Recurrence")).find(fx.t("NumberedRecurrence")).findtext(fx.t("NumberOfOccurrences")) == "10"
    # element order of the schema
    tags = [c.tag.rsplit("}", 1)[-1] for c in el if c.tag != fx.t("ItemId")]
    assert tags.index("UID") < tags.index("Start") < tags.index("Location") < tags.index("Recurrence") < tags.index("StartTimeZone")
    assert fake_ews.deleted_occurrences == [f"occ-{rid}-1"]
    change = fake_ews.updated_occurrences[f"occ-{rid}-2"]
    assert fake_ews.last_update_attrs["SendMeetingInvitationsOrCancellations"] == "SendToNone"
    fields = {f.find(fx.t("FieldURI")).get("FieldURI"): f.find(fx.t("CalendarItem"))[0].text for f in change.find(fx.t("Updates"))}
    assert fields["item:Subject"] == "Moved" and fields["calendar:Start"] == "2024-03-18T10:00:00Z"
    assert s.find("calendar:@default", out_item(CALENDAR, b"")) == [rid]
    assert s.count_ours("calendar:@default", "src-1") == 1


def test_ews_failed_exception_removes_the_series(fake_ews):
    s = ews_session()
    fake_ews.fail_op["UpdateItem"] = "ErrorInternalServerTransientError"
    with pytest.raises(ConnectorError) as err:
        s.put("calendar:@default", out_item(CALENDAR, pim.event_to_ics(series())))
    assert err.value.retryable
    assert fake_ews.in_folder("id-calendar") == []  # find() must not take it for migrated


def test_ews_all_day_event_in_its_zone(fake_ews):
    s = ews_session()
    e = pim.Event(uid="ad", start=date(2024, 7, 1), end=date(2024, 7, 2), subject="Off", tz="Europe/Paris")
    rid = s.put("calendar:@default", out_item(CALENDAR, pim.event_to_ics(e)))
    el = fake_ews.items[rid]["el"]
    assert el.findtext(fx.t("IsAllDayEvent")) == "true"
    assert el.findtext(fx.t("Start")) == "2024-06-30T22:00:00Z"  # midnight in Paris
    assert el.find(fx.t("StartTimeZone")).get("Id") == "Romance Standard Time"


def test_ews_all_day_event_that_does_not_say_its_zone(fake_ews):
    raw = (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:u\r\nDTSTAMP:20240101T000000Z\r\n"
           b"DTSTART;VALUE=DATE:20240701\r\nDTEND;VALUE=DATE:20240702\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    rid = ews_session().put("calendar:@default", out_item(CALENDAR, raw))
    assert fake_ews.items[rid]["el"].findtext(fx.t("Start")) == "2024-07-01T00:00:00Z"
    rid = ews_session(time_zone="Europe/Paris").put("calendar:@default", out_item(CALENDAR, raw, "<src-1-b>"))
    assert fake_ews.items[rid]["el"].findtext(fx.t("Start")) == "2024-06-30T22:00:00Z"


def test_ews_put_contact_and_task(fake_ews):
    s = ews_session()
    c = pim.Contact(uid="c", full_name="Ann Lee", given="Ann", surname="Lee", prefix="Dr", emails=["a@x.test"],
                    phones=[("mobile", "+33")], addresses=[{"kind": "work", "city": "Lyon"}], birthday=date(1980, 2, 3), note="n")
    rid = s.put("contacts:@default", out_item(CONTACTS, pim.contact_to_vcard(c)))
    el = fake_ews.items[rid]["el"]
    assert fake_ews.items[rid]["folder"] == "id-contacts" and el.findtext(fx.t("DisplayName")) == "Ann Lee"
    assert el.find(fx.t("EmailAddresses"))[0].get("Key") == "EmailAddress1" and el.find(fx.t("PhoneNumbers"))[0].get("Key") == "MobilePhone"
    assert el.findtext(fx.t("Birthday")) == "1980-02-03T00:00:00Z" and el.findtext(fx.t("Surname")) == "Lee"
    tags = [x.tag.rsplit("}", 1)[-1] for x in el if x.tag != fx.t("ItemId")]
    assert tags.index("GivenName") < tags.index("EmailAddresses") < tags.index("PhoneNumbers") < tags.index("Birthday") < tags.index("Surname")
    t = pim.Task(uid="t", subject="Do", status="COMPLETED", completed=datetime(2024, 1, 2, tzinfo=UTC), due=datetime(2024, 1, 3, tzinfo=UTC))
    rid = s.put("tasks:@default", out_item(TASKS, pim.task_to_ics(t), "<src-1-t>"))
    el = fake_ews.items[rid]["el"]
    assert fake_ews.items[rid]["folder"] == "id-tasks" and el.findtext(fx.t("Status")) == "Completed"
    assert el.findtext(fx.t("CompleteDate")) == "2024-01-02T00:00:00Z"
    assert s.find("tasks:@default", out_item(TASKS, b"", "<src-1-t>")) == [rid]


def test_ews_recurrence_xml():
    w: list[str] = []
    rec = pim.Recurrence("MONTHLY", 1, ["MO", "TU", "WE", "TH", "FR"], set_pos=1, until=date(2024, 12, 31))
    xml = ews.recurrence_xml(rec, datetime(2024, 1, 1, 9, tzinfo=UTC), "UTC")
    root = ET.fromstring(f'<r xmlns:t="{fx.NS_T}">{xml}</r>')[0]
    assert root.find(fx.t("RelativeMonthlyRecurrence")).findtext(fx.t("DaysOfWeek")) == "Weekday"
    assert root.find(fx.t("EndDateRecurrence")).findtext(fx.t("EndDate")) == "2024-12-31"
    back = ews.recurrence_from_xml(root, w)
    assert back.by_day == ["MO", "TU", "WE", "TH", "FR"] and back.set_pos == 1 and back.until == date(2024, 12, 31)
    yearly = ews.recurrence_xml(pim.Recurrence("YEARLY", month_day=5, month=2), date(2024, 2, 5), None)
    assert "AbsoluteYearlyRecurrence" in yearly and "<t:Month>February</t:Month>" in yearly and "NoEndRecurrence" in yearly
    with pytest.raises(ConnectorError):
        ews.recurrence_xml(pim.Recurrence("MONTHLY", by_day=["MO", "TU"], set_pos=1), date(2024, 1, 1), None)
    regen = ET.fromstring(f'<r xmlns:t="{fx.NS_T}"><t:Recurrence><t:DailyRegeneration><t:Interval>2</t:Interval></t:DailyRegeneration></t:Recurrence></r>')[0]
    assert ews.recurrence_from_xml(regen, w) is None and "recurrence-unsupported:DailyRegeneration" in w


# ------------------------------------------------------------------ Graph: authentication and transport
def test_graph_token_request_and_refresh(fake_graph):
    src = graph_source()
    form = fake_graph.token_requests[0]
    assert form["grant_type"] == "client_credentials" and form["client_id"] == "app-1"
    assert form["scope"] == "https://graph.microsoft.com/.default"
    fake_graph.expire_next_token = True  # the token is refused once: a new one, the call goes through
    src.folders()
    assert fake_graph.tokens == 2


def test_graph_wrong_secret_is_auth_and_not_leaked(fake_graph):
    src = m365.GraphSource(GRAPH_CONFIG, {"client_secret": "wrong-value"})
    with pytest.raises(ConnectorError) as err:
        src.open()
    assert err.value.kind == ConnectorError.AUTH and "wrong-value" not in str(err.value)


def test_graph_retry_after(fake_graph, waits):
    src = graph_source()
    fake_graph.throttle = [7, 3]
    src.folders()
    assert waits[:2] == [7.0, 3.0]


def test_graph_gives_up_as_transient(fake_graph):
    src = graph_source()
    fake_graph.throttle = [1] * 50
    with pytest.raises(ConnectorError) as err:
        src.folders()
    assert err.value.kind == ConnectorError.TRANSIENT


def test_graph_permissions_error_names_them(fake_graph):
    src = graph_source()
    fake_graph.forbid.add("/calendars")
    with pytest.raises(ConnectorError) as err:
        src.folders()
    assert err.value.kind == ConnectorError.AUTH and "Calendars.ReadWrite" in str(err.value)


def test_graph_unknown_mailbox_is_config(fake_graph):
    src = m365.GraphSource({**GRAPH_CONFIG, "mailbox": "nobody@contoso.test"}, {"client_secret": fake_graph.SECRET})
    with pytest.raises(ConnectorError) as err:
        src.open()
    assert err.value.kind == ConnectorError.CONFIG


def test_graph_next_link_to_elsewhere_is_refused(fake_graph):
    src = graph_source()
    with pytest.raises(ConnectorError) as err:
        src._c().get_json("https://evil.example.test/v1.0/x")
    assert err.value.kind == ConnectorError.REJECTED and not any(r.url.host == "evil.example.test" for r in fake_graph.requests)


def test_graph_concurrency_is_bounded(fake_graph):
    target = m365.GraphTarget(GRAPH_CONFIG, {"client_secret": fake_graph.SECRET})
    assert target.gate._value == 4  # documented limit per app and mailbox, shared by every session
    assert target.session().target.gate is target.gate


def test_graph_immutable_ids_and_utc_are_asked(fake_graph):
    graph_source()
    prefer = fake_graph.requests[0].headers.get_list("prefer", split_commas=False)
    assert 'IdType="ImmutableId"' in " ".join(prefer) and 'outlook.timezone="UTC"' in " ".join(prefer)


# ------------------------------------------------------------------ Graph: reading
def seed_graph(fake):
    fake.mail_folders["sub"] = {"id": "mf-sub", "displayName": "Work", "parent": "mf-inbox"}
    props = [{"id": "Integer 0x0e08", "value": "321"}, {"id": "Integer 0x1081", "value": "104"}]
    for i in range(3):
        fake.add_message("mf-inbox", MIME, id=f"msg-{i}", internetMessageId=f"<{i}@x.test>", receivedDateTime="2024-03-01T12:00:00Z",
                         isRead=bool(i % 2), flag={"flagStatus": "flagged" if i == 0 else "notFlagged"},
                         singleValueExtendedProperties=props)
    fake.events["ev-m"] = {
        "id": "ev-m", "calendar": "cal-1", "type": "seriesMaster", "iCalUId": "ical-1", "subject": "Sync",
        "body": {"contentType": "text", "content": "Agenda"}, "isAllDay": False, "showAs": "tentative", "sensitivity": "private",
        "start": {"dateTime": "2024-03-04T08:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2024-03-04T09:00:00.0000000", "timeZone": "UTC"},
        "originalStartTimeZone": "Romance Standard Time", "location": {"displayName": "Room"}, "categories": ["Blue"],
        "isReminderOn": True, "reminderMinutesBeforeStart": 5, "hasAttachments": False, "isCancelled": False,
        "createdDateTime": "2024-01-01T00:00:00Z", "lastModifiedDateTime": "2024-01-02T00:00:00Z",
        "organizer": {"emailAddress": {"name": "Boss", "address": "boss@x.test"}},
        "attendees": [{"type": "required", "status": {"response": "accepted"}, "emailAddress": {"name": "Ann", "address": "ann@x.test"}},
                      {"type": "resource", "status": {"response": "none"}, "emailAddress": {"name": "Room", "address": "room@x.test"}}],
        "recurrence": {"pattern": {"type": "relativeMonthly", "interval": 1, "daysOfWeek": ["monday"], "index": "first"},
                       "range": {"type": "endDate", "startDate": "2024-03-04", "endDate": "2024-12-31",
                                 "recurrenceTimeZone": "Romance Standard Time"}},
        "cancelledOccurrences": ["OID.ev-m.2024-05-06"],
        "exceptionOccurrences": [{"id": "ev-x", "subject": "Sync moved", "isAllDay": False, "originalStart": "2024-04-01T07:00:00Z",
                                  "start": {"dateTime": "2024-04-01T10:00:00.0000000", "timeZone": "UTC"},
                                  "end": {"dateTime": "2024-04-01T11:00:00.0000000", "timeZone": "UTC"},
                                  "body": {"content": "moved"}}]}
    fake.contacts["c1"] = {"id": "c1", "folder": None, "displayName": "Ann Lee", "givenName": "Ann", "surname": "Lee", "title": "Dr",
                           "emailAddresses": [{"address": "ann@x.test", "name": "Ann"}], "businessPhones": ["+331", "+332"],
                           "mobilePhone": "+336", "homeAddress": {"city": "Lyon", "street": "", "state": "", "postalCode": "", "countryOrRegion": ""},
                           "birthday": "1980-02-03T00:00:00Z", "personalNotes": "note", "lastModifiedDateTime": "2024-01-01T00:00:00Z"}
    fake.tasks["t1"] = {"id": "t1", "listId": "list-1", "title": "Plan", "status": "inProgress", "importance": "high",
                        "body": {"content": "body", "contentType": "text"},
                        "dueDateTime": {"dateTime": "2024-04-01T00:00:00.0000000", "timeZone": "UTC"},
                        "lastModifiedDateTime": "2024-01-01T00:00:00Z"}


def test_graph_folders(fake_graph):
    seed_graph(fake_graph)
    src = graph_source()
    folders = src.folders()
    paths = {(f.kind, tuple(f.path)): f for f in folders}
    assert paths[(MAIL, ("Inbox",))].special == "inbox" and (MAIL, ("Inbox", "Work")) in paths
    assert paths[(MAIL, ("Sentitems",))].special == "sent" and (MAIL, ("Outbox",)) not in paths
    assert paths[(CALENDAR, ("Calendar",))].special == xb.DEFAULT
    assert paths[(CONTACTS, ("Contacts",))].special == xb.DEFAULT and paths[(TASKS, ("Tasks",))].special == xb.DEFAULT


def test_graph_tasks_can_be_left_out(fake_graph):
    src = graph_source(include_tasks=False)
    assert not [f for f in src.folders() if f.kind == TASKS]
    src = graph_source()
    fake_graph.forbid.add("/todo")
    with pytest.raises(ConnectorError) as err:
        src.folders()
    assert "Include tasks" in str(err.value)


def test_graph_listing_paging_flags(fake_graph):
    seed_graph(fake_graph)
    src = graph_source()
    inbox = folder_of(src.folders(), "Inbox")
    items = list(src.iter_items(inbox))
    assert len(items) == 3 and len({i.key for i in items}) == 3  # two pages
    assert items[0].flags == ["\\Flagged", "$Forwarded"] and items[1].flags == ["\\Seen", "$Forwarded"]
    assert items[0].size == 321 and items[0].date == datetime(2024, 3, 1, 12, tzinfo=UTC)
    assert items[0].alt_key.startswith("m:")
    got = src.fetch(inbox, items[0], "<x>")
    assert got.raw == MIME and got.internal_date == items[0].date and got.subject == "Hello"
    items[0].ref = "gone"
    assert src.fetch(inbox, items[0], "<x>") is None


def test_graph_delta(fake_graph):
    """Exchange Online refuses $expand on a delta query (HTTP 400 '... initialState'): the round asks for plain fields,
    the size and the answered state of the changed messages are read one by one."""
    seed_graph(fake_graph)
    src = graph_source()
    inbox = folder_of(src.folders(), "Inbox")
    first = src.changed_since(inbox, None)  # first delta pass after a full copy: tracking starts, full listing
    assert first.reset and len(first.items) == 3 and "deltatoken=3" in first.cursor
    assert all(i.size == 321 for i in first.items)
    props = [{"id": "Integer 0x0e08", "value": "555"}, {"id": "Integer 0x1081", "value": "102"}]
    fake_graph.add_message("mf-inbox", MIME, id="msg-new", receivedDateTime="2024-03-02T12:00:00Z",
                           internetMessageId="<new@x.test>", singleValueExtendedProperties=props)
    second = src.changed_since(inbox, first.cursor)
    assert not second.reset and [i.ref for i in second.items] == ["msg-new"]
    assert second.items[0].size == 555 and "\\Answered" in second.items[0].flags
    assert fake_graph.delta_calls and not any("$expand" in c for c in fake_graph.delta_calls)
    fake_graph.delta_expired = True  # 410: a full round, said so
    third = src.changed_since(inbox, second.cursor)
    assert third.reset and len(third.items) == 4 and third.cursor
    cal = folder_of(src.folders(), "Calendar")
    full = src.changed_since(cal, "ignored")
    assert full.reset and full.cursor is None  # no per-calendar delta


def test_graph_delta_many_changes_and_other_kinds(fake_graph, monkeypatch):
    seed_graph(fake_graph)
    src = graph_source()
    folders = src.folders()
    inbox = folder_of(folders, "Inbox")
    cur = src.changed_since(inbox, None).cursor
    monkeypatch.setattr(m365, "DELTA_DETAILS_MAX", 1)
    for i in range(2):
        fake_graph.add_message("mf-inbox", MIME, id=f"msg-x{i}", receivedDateTime="2024-03-02T12:00:00Z")
    many = src.changed_since(inbox, cur)  # too many to read one by one: the normal listing
    assert many.reset and len(many.items) == 5 and many.cursor
    contacts = next(f for f in folders if f.kind == CONTACTS and not f.ref)
    got = src.changed_since(contacts, None)  # no delta for the default contact folder in v1.0
    assert got.reset and got.cursor is None and len(got.items) == 1
    tasks = folder_of(folders, "Tasks")
    t1 = src.changed_since(tasks, None)
    assert t1.reset and len(t1.items) == 1 and t1.cursor
    assert src.changed_since(tasks, t1.cursor).items == []


def test_graph_event_with_exceptions(fake_graph):
    seed_graph(fake_graph)
    src = graph_source()
    cal = folder_of(src.folders(), "Calendar")
    got = src.fetch(cal, next(iter(src.iter_items(cal))), "<x>")
    e = pim.ics_to_event(got.raw)
    assert e.uid == "ical-1" and e.tz == "Europe/Paris" and e.start == datetime(2024, 3, 4, 8, tzinfo=UTC)
    assert (e.recurrence.freq, e.recurrence.by_day, e.recurrence.set_pos, e.recurrence.until) == ("MONTHLY", ["MO"], 1, date(2024, 12, 31))
    assert e.exdates == [datetime(2024, 5, 6, 7, tzinfo=UTC)]  # 09:00 in Paris, summer time
    assert [(o.subject, o.recurrence_id) for o in e.overrides] == [("Sync moved", datetime(2024, 4, 1, 7, tzinfo=UTC))]
    assert e.free_busy == "TENTATIVE" and e.sensitivity == "PRIVATE" and e.reminder_minutes == 5
    assert {a.email: (a.role, a.partstat) for a in e.attendees} == {"ann@x.test": ("REQ", "ACCEPTED"), "room@x.test": ("RES", "NEEDS-ACTION")}


def test_graph_custom_zone_warns(fake_graph):
    e = m365.event_from_json({"id": "x", "originalStartTimeZone": "tzone://Microsoft/Custom", "isAllDay": False,
                              "start": {"dateTime": "2024-01-01T10:00:00", "timeZone": "UTC"},
                              "end": {"dateTime": "2024-01-01T11:00:00", "timeZone": "UTC"}}, warnings := [])
    assert e.tz is None and any("timezone-unknown" in w for w in warnings)


def test_graph_contact_and_task(fake_graph):
    seed_graph(fake_graph)
    src = graph_source()
    folders = src.folders()
    book = folder_of(folders, "Contacts")
    got = src.fetch(book, next(iter(src.iter_items(book))), "<x>")
    c = pim.vcard_to_contact(got.raw)
    assert c.full_name == "Ann Lee" and c.prefix == "Dr" and ("business2", "+332") in c.phones and ("mobile", "+336") in c.phones
    assert c.birthday == date(1980, 2, 3) and c.addresses[0]["kind"] == "home"
    tasks = folder_of(folders, "Tasks")
    got = src.fetch(tasks, next(iter(src.iter_items(tasks))), "<x>")
    t = pim.ics_to_task(got.raw)
    assert (t.subject, t.status, t.priority) == ("Plan", "IN-PROCESS", "high") and t.due == datetime(2024, 4, 1, tzinfo=UTC)


# ------------------------------------------------------------------ Graph: writing
def test_graph_import_mime_then_flags(fake_graph):
    s = graph_session()
    when = datetime(2024, 3, 1, 12, tzinfo=UTC)
    rid = s.put("mail:@inbox", out_item(MAIL, MIME, "<src-1-a>", ["\\Seen", "\\Flagged", "$Forwarded"], when))
    msg = fake_graph.messages[rid]
    assert msg["mime"] == MIME and msg["isDraft"] is False and msg["parentFolderId"] == "mf-inbox"
    assert "beta" in fake_graph.base_versions  # documented in beta only
    assert msg["isRead"] is True and msg["flag"] == {"flagStatus": "flagged"}
    props = {p["id"]: p["value"] for p in msg["singleValueExtendedProperties"]}
    assert props[m365.PROP_IMPORT] == "<src-1-a>" and props[m365.PROP_VERB] == "104"
    assert props[m365.PROP_DELIVERY] == "2024-03-01T12:00:00Z"
    assert s.find("mail:@inbox", out_item(MAIL, b"", "<src-1-a>")) == [rid]
    assert s.find("mail:@inbox", out_item(MAIL, b"", "<SRC-1-A>")) == []  # the server ignores case, the answer does not
    assert s.count_ours("mail:@inbox", "src-1") == 1 and s.count_ours("mail:@inbox", "src-2") == 0


def test_graph_draft_mode_uses_v1(fake_graph):
    s = graph_session(import_mode="draft")
    rid = s.put("mail:@inbox", out_item(MAIL, MIME, flags=[]))
    assert fake_graph.messages[rid]["isDraft"] is True and fake_graph.messages[rid]["isRead"] is False
    assert "beta" not in fake_graph.base_versions


def test_graph_flags_failure_does_not_write_twice(fake_graph):
    s = graph_session()
    fake_graph.seq = 100  # ids are predictable: msg101
    fake_graph.fail["PATCH:/messages/msg101"] = (500, "InternalServerError")
    rid = s.put("mail:@inbox", out_item(MAIL, MIME))
    assert rid == "msg101" and len(fake_graph.messages) == 1  # the write is reported as a success


def test_graph_message_too_large_is_rejected(fake_graph):
    s = graph_session()
    fake_graph.max_body = 10
    with pytest.raises(ConnectorError) as err:
        s.put("mail:@inbox", out_item(MAIL, MIME))
    assert err.value.kind == ConnectorError.REJECTED and "too large" in str(err.value)


def test_graph_folder_missing_then_created(fake_graph):
    s = graph_session(root_folder="Imported")
    plan = s.plan_folders([SourceFolder(["A", "B"], MAIL)])
    name = plan["A/B"].name
    assert name == "mail:Imported/A/B"
    with pytest.raises(ConnectorError) as err:
        s.put(name, out_item(MAIL, MIME))
    assert err.value.kind == ConnectorError.FOLDER_MISSING
    s.ensure_folder(name)
    s.ensure_folder(name)
    rid = s.put(name, out_item(MAIL, MIME))
    assert fake_graph.mail_folders[fake_graph.messages[rid]["parentFolderId"]]["displayName"] == "B"
    assert len([f for f in fake_graph.mail_folders.values() if f["displayName"] == "Imported"]) == 1


def test_graph_event_keeps_attendees_as_text_by_default(fake_graph):
    s = graph_session()
    rid = s.put("calendar:@default", out_item(CALENDAR, pim.event_to_ics(series())))
    e = fake_graph.events[rid]
    assert "attendees" not in e  # documented: creating attendees always mails them
    assert "Required: Ann <a@example.test> (accepted)" in e["body"]["content"] and "Organizer: The Boss" in e["body"]["content"]
    assert e["start"] == {"dateTime": "2024-03-04T09:00:00", "timeZone": "Romance Standard Time"}
    assert e["recurrence"]["pattern"]["type"] == "weekly" and e["recurrence"]["range"]["recurrenceTimeZone"] == "Romance Standard Time"
    assert e["transactionId"] and e["responseRequested"] is False
    occ = fake_graph.instances[rid]
    assert [x["id"] for x in occ if x["id"].endswith("occ1")] == []  # the exdate is gone
    moved = [x for x in occ if x["id"].endswith("occ2")][0]
    assert moved["subject"] == "Moved" and moved["type"] == "exception"
    assert s.find("calendar:@default", out_item(CALENDAR, b"")) == [rid] and s.count_ours("calendar:@default", "src-1") == 1


def test_graph_event_attendees_only_if_asked(fake_graph):
    s = graph_session(send_invitations=True)
    rid = s.put("calendar:@default", out_item(CALENDAR, pim.event_to_ics(series())))
    e = fake_graph.events[rid]
    assert {a["emailAddress"]["address"]: a["type"] for a in e["attendees"]} == {
        "a@example.test": "required", "b@example.test": "optional", "room@example.test": "resource"}


def test_graph_transaction_id_prevents_a_second_event(fake_graph):
    s = graph_session()
    single = pim.Event(uid="s", start=datetime(2024, 1, 1, 9, tzinfo=UTC), end=datetime(2024, 1, 1, 10, tzinfo=UTC), subject="One", tz="UTC")
    a = s.put("calendar:@default", out_item(CALENDAR, pim.event_to_ics(single)))
    b = s.put("calendar:@default", out_item(CALENDAR, pim.event_to_ics(single)))
    assert a == b and len(fake_graph.events) == 1


def test_graph_failed_exception_removes_the_series(fake_graph):
    s = graph_session()
    fake_graph.seq = 50
    fake_graph.fail["PATCH:/events/ev51-occ2"] = (500, "InternalServerError")
    with pytest.raises(ConnectorError) as err:
        s.put("calendar:@default", out_item(CALENDAR, pim.event_to_ics(series())))
    assert err.value.retryable and fake_graph.events == {}


def test_graph_contact_extra_phones_go_to_the_notes(fake_graph):
    s = graph_session()
    c = pim.Contact(uid="c", full_name="Ann", given="Ann", prefix="Dr", phones=[("mobile", "+336"), ("business_fax", "+331"), ("pager", "42")],
                    emails=["a@x.test", "b@x.test", "c@x.test", "d@x.test"], note="hello")
    rid = s.put("contacts:@default", out_item(CONTACTS, pim.contact_to_vcard(c)))
    x = fake_graph.contacts[rid]
    assert x["mobilePhone"] == "+336" and x["title"] == "Dr" and len(x["emailAddresses"]) == 3
    assert "business_fax: +331" in x["personalNotes"] and "pager: 42" in x["personalNotes"] and "d@x.test" in x["personalNotes"]
    assert x["personalNotes"].startswith("hello")
    assert s.find("contacts:@default", out_item(CONTACTS, b"")) == [rid]
    s.ensure_folder("contacts:Friends")
    rid = s.put("contacts:Friends", out_item(CONTACTS, pim.contact_to_vcard(c), "<src-1-b>"))
    assert fake_graph.contacts[rid]["folder"]


def test_graph_task(fake_graph):
    s = graph_session()
    t = pim.Task(uid="t", subject="Do", status="COMPLETED", priority="low", completed=datetime(2024, 1, 2, tzinfo=UTC),
                 due=datetime(2024, 1, 3, tzinfo=UTC), recurrence=pim.Recurrence("WEEKLY", 1, ["MO"]))
    rid = s.put("tasks:@default", out_item(TASKS, pim.task_to_ics(t), "<src-1-t>"))
    x = fake_graph.tasks[rid]
    assert x["listId"] == "list-1" and x["status"] == "completed" and x["importance"] == "low"
    assert x["linkedResources"][0]["externalId"] == "<src-1-t>" and x["recurrence"]["pattern"]["type"] == "weekly"
    assert s.find("tasks:@default", out_item(TASKS, b"", "<src-1-t>")) == [rid]
    assert s.find("tasks:@default", out_item(TASKS, b"", "<src-1-other>")) == []
    assert s.count_ours("tasks:@default", "src-1") == 1
    s.ensure_folder("tasks:Later")
    assert [x for x in fake_graph.lists.values() if x["displayName"] == "Later"]


# ------------------------------------------------------------------ OAuth, XOAUTH2, master user
def test_oauth_refresh_token_grant_and_rotation(monkeypatch):
    forms = []

    def server(request):
        forms.append(request.content.decode())
        return httpx.Response(200, json={"access_token": f"a{len(forms)}", "expires_in": 3600, "refresh_token": f"r{len(forms)}"})
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(server))
    tok = oauth.OAuthToken("https://idp.example.test/token", "cid", "", "imap", "r0")
    assert tok.token() == "a1" and tok.token() == "a1"
    assert "grant_type=refresh_token" in forms[0] and "refresh_token=r0" in forms[0]
    assert tok.token(force=True) == "a2" and "refresh_token=r1" in forms[1]  # the rotated one


def test_oauth_expiry_and_errors(monkeypatch):
    answers = [httpx.Response(200, json={"access_token": "a", "expires_in": 10}), httpx.Response(200, json={"access_token": "b", "expires_in": 3600})]
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(lambda r: answers.pop(0)))
    tok = oauth.OAuthToken("https://idp.example.test/token", "cid", "sec")
    assert tok.token() == "a" and tok.token() == "b"  # under the margin: refreshed

    def status(code, error=None):
        monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(code, json={"error": error} if error else {})))
        with pytest.raises(ConnectorError) as err:
            oauth.OAuthToken("https://idp.example.test/token", "cid", "topsecret").token()
        assert "topsecret" not in str(err.value)
        return err.value.kind
    assert status(400, "invalid_client") == ConnectorError.AUTH
    assert status(400, "invalid_grant") == ConnectorError.AUTH
    assert status(400, "invalid_scope") == ConnectorError.CONFIG
    assert status(503) == ConnectorError.UNREACHABLE
    with pytest.raises(ConnectorError) as err:
        oauth.OAuthToken("https://idp.example.test/token", "cid").token()
    assert err.value.kind == ConnectorError.CONFIG


class FakeImap:
    """What ImapConn needs of imaplib for a login."""
    capabilities = ("AUTH=XOAUTH2",)
    calls: list = []

    def __init__(self, *a, **k):
        pass

    def authenticate(self, mech, cb):
        FakeImap.calls.append((mech, cb(b""), cb(b'{"status":"401"}')))
        return "OK", []

    def capability(self):
        return "OK", [b"IMAP4rev1 AUTH=XOAUTH2"]

    def logout(self):
        pass


def test_imap_xoauth2_uses_the_current_token(monkeypatch):
    from app.server import imap as simap
    monkeypatch.setattr(simap.imaplib, "IMAP4_SSL", FakeImap)
    FakeImap.calls = []
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})))
    params = cimap.oauth_params_of({"host": "h", "login": "me@x.test", "token_url": "https://idp.example.test/token", "client_id": "c"},
                                   {"client_secret": "s"})
    assert params.password == ""
    simap.ImapConn(params).connect()
    mech, first, second = FakeImap.calls[0]
    assert mech == "XOAUTH2" and first == b"user=me@x.test\x01auth=Bearer tok-1\x01\x01" and second == b""


def test_imap_token_failure_keeps_its_kind(monkeypatch):
    from app.server import imap as simap
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"})))
    params = cimap.oauth_params_of({"host": "h", "login": "m", "token_url": "https://idp.example.test/token", "client_id": "c"}, {"refresh_token": "r"})
    with pytest.raises(ConnectorError) as err:
        simap.ImapConn(params).connect()
    assert err.value.kind == ConnectorError.AUTH


def test_imap_oauth_connector_declares_no_required_secret():
    role = registry.role_of("imap_oauth2", "source")
    assert registry.merge_secrets(role, {}, {"refresh_token": "r"}) == {"refresh_token": "r"}
    assert isinstance(role.factory({"host": "h", "login": "l", "token_url": "https://x.test/t", "client_id": "c"}, {"client_secret": "s"}, {}),
                      cimap.ImapOAuthSource)


def test_imap_master_user():
    config = {"host": "h", "login": "ann@example.test", "master_user": "admin"}
    assert cimap.params_of(config, {"password": "pw"}).login == "ann@example.test*admin"
    assert cimap.params_of({**config, "master_separator": "%"}, {"password": "pw"}).login == "ann@example.test%admin"
    assert cimap.params_of({"host": "h", "login": "ann"}, {"password": "pw"}).login == "ann"
    # the mailbox names the state, not the admin who reaches it
    plain = registry.namespace("imap", "source", {"host": "h", "port": 993, "login": "ann"})
    assert plain == registry.namespace("imap", "source", {"host": "h", "port": 993, "login": "ann", "master_user": "admin"})


# ------------------------------------------------------------------ DAV: tasks and source
def test_dav_tasks_get_a_collection_that_accepts_them():
    from app.connectors import dav as cdav
    target = registry.make_target("caldav", {"url": "https://d.test/{user}/", "login": "u"}, {"password": "p"})
    tasks = target.for_item(TASKS)
    assert tasks.component == "VTODO" and tasks.session().component == "VTODO"
    assert target.for_item(CALENDAR) is target
    assert cdav.CardDavTarget({}, {}).for_item(CONTACTS).dav_kind == "carddav"


def make_dav_transport(rows):
    props = """<d:response><d:href>{href}</d:href><d:propstat><d:prop><d:resourcetype><d:collection/>{rt}</d:resourcetype>
    <d:displayname>{name}</d:displayname>{comp}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"""

    def server(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        ms = '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:card="urn:ietf:params:xml:ns:carddav">{}</d:multistatus>'
        if request.method == "PROPFIND" and path == "/dav/":
            return httpx.Response(207, content=ms.format(
                '<d:response><d:href>/dav/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>'
                '<c:calendar-home-set><d:href>/dav/</d:href></c:calendar-home-set></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
                + "".join(props.format(**r) for r in rows)).encode())
        if request.method == "PROPFIND" and path.startswith("/dav/"):
            objs = "".join(
                f'<d:response><d:href>{path}{n}</d:href><d:propstat><d:prop><d:resourcetype/></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
                for n in ("a.ics", "b.ics"))
            return httpx.Response(207, content=ms.format(objs).encode())
        if request.method == "GET" and path.endswith("a.ics"):
            return httpx.Response(200, text="BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:One\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        if request.method == "GET" and path.endswith("b.ics"):
            return httpx.Response(200, text="BEGIN:VCALENDAR\r\nBEGIN:VTODO\r\nSUMMARY:Two\r\nEND:VTODO\r\nEND:VCALENDAR\r\n")
        return httpx.Response(404)
    return httpx.MockTransport(server)


def test_caldav_source(monkeypatch):
    from app.server import dav as sdav
    real = sdav.httpx.Client
    transport = make_dav_transport([
        {"href": "/dav/work/", "rt": "<c:calendar/>", "name": "Work",
         "comp": '<c:supported-calendar-component-set><c:comp name="VEVENT"/><c:comp name="VTODO"/></c:supported-calendar-component-set>'},
        {"href": "/dav/todo/", "rt": "<c:calendar/>", "name": "Later",
         "comp": '<c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set>'},
        {"href": "/dav/book/", "rt": "<card:addressbook/>", "name": "Book", "comp": ""}])
    monkeypatch.setattr(sdav.httpx, "Client", lambda **kw: real(**{**kw, "transport": transport}))
    src = registry.make_source("caldav", {"url": "https://d.test/dav/", "login": "u"}, {"password": "p"})
    with src:
        folders = src.folders()
        assert [(f.path, f.kind, f.special) for f in folders] == [(["Work"], CALENDAR, xb.DEFAULT), (["Work"], TASKS, xb.DEFAULT), (["Later"], TASKS, None)]
        cal = folders[0]
        items = list(src.iter_items(cal))
        assert len(items) == 2 and len({i.key for i in items}) == 2
        got = {i.ref: src.fetch(cal, i, "<x>") for i in items}
        assert got["a.ics"].subject == "One" and b"VEVENT" in got["a.ics"].raw
        assert got["b.ics"] is None  # a task, listed under the tasks kind of this collection
        tasks = folders[1]
        assert {i.ref: src.fetch(tasks, i, "<x>") is not None for i in src.iter_items(tasks)} == {"a.ics": False, "b.ics": True}


# ------------------------------------------------------------------ Sieve
def rule(name, conditions=None, actions=None, exceptions=None, enabled=True, order=1):
    return {"name": name, "enabled": enabled, "order": order, "conditions": conditions or {}, "exceptions": exceptions or {},
            "actions": actions or {}}


def test_sieve_clean_rule():
    out = sieve.convert([rule("Invoices", {"subjectContains": ["invoice", 'a"b'], "fromAddresses": ["x@y.test"]},
                              {"moveToFolder": ["Work", "Bills"], "markAsRead": True, "stopProcessingRules": True},
                              {"senderContains": ["spam"]})], delimiter=".", root="Imported")
    s = out["script"]
    assert out["converted"] == ["Invoices"] and not out["not_converted"] and not out["partial"]
    assert 'require ["fileinto", "imap4flags"];' in s
    assert r'header :contains "subject" ["invoice", "a\"b"]' in s and 'address :is "from" "x@y.test"' in s
    assert 'not address :contains "from" "spam"' in s and s.index("allof(") < s.index("{")
    assert 'addflag "\\\\Seen";' in s and 'fileinto "Imported.Work.Bills";' in s and s.index("fileinto") < s.index("stop;")


def test_sieve_what_does_not_convert_is_reported():
    out = sieve.convert([
        rule("Big", {"withinSizeRange": {"minimumSize": 1}}, {"delete": True}),
        rule("Attach", {"hasAttachments": True}, {"moveToFolder": ["A"]}),
        rule("Exc", {"subjectContains": ["x"]}, {"delete": True}, {"isSigned": True}),
        rule("Off", {"subjectContains": ["x"]}, {"delete": True}, enabled=False),
        rule("NoAct", {"subjectContains": ["x"]}, {"markImportance": "high"}),
        rule("Lost", {"subjectContains": ["x"]}, {"moveToFolder": {"unknown": "id"}}),
        rule("Cats", {"subjectContains": ["x"]}, {"delete": True, "assignCategories": ["Red"], "forwardAsAttachmentTo": ["a@x.test"]}),
    ])
    reasons = {r["name"]: r["reasons"] for r in out["not_converted"]}
    assert reasons["Big"] == ["condition: size range"] and reasons["Attach"] == ["condition: has attachments"]
    assert reasons["Exc"] == ["exception, condition: signed"] and reasons["Off"] == ["disabled in Exchange"]
    assert reasons["NoAct"] == ["no action has a Sieve equivalent"] and "moves to a folder that was not found" in reasons["Lost"]
    assert out["partial"] == [{"name": "Cats", "dropped": ["assign categories", "forward as attachment"]}]
    assert out["converted"] == [] and 'fileinto "Trash";' in out["script"]


def test_sieve_forward_warns_and_keeps_a_copy():
    out = sieve.convert([rule("Fwd", {"bodyOrSubjectContains": ["x"]}, {"forwardTo": ["a@x.test"], "permanentDelete": True})])
    assert 'redirect :copy "a@x.test";' in out["script"] and "discard;" in out["script"]
    assert '"body"' in out["script"].split("\n")[1] and '"copy"' in out["script"].split("\n")[1]
    assert out["warnings"] and "a@x.test" in out["warnings"][0]


def test_sieve_empty():
    out = sieve.convert([])
    assert out["script"].startswith("#") and "require" not in out["script"]


def test_rules_from_graph(fake_graph):
    fake_graph.mail_folders["sub"] = {"id": "mf-sub", "displayName": "Work", "parent": "mf-inbox"}
    fake_graph.rules = [
        {"id": "2", "displayName": "Second", "sequence": 2, "isEnabled": False, "conditions": {"subjectContains": ["b"]}, "actions": {"delete": True}},
        {"id": "1", "displayName": "First", "sequence": 1, "isEnabled": True,
         "conditions": {"fromAddresses": [{"emailAddress": {"name": "X", "address": "x@y.test"}}], "hasAttachments": False},
         "actions": {"moveToFolder": "mf-sub", "stopProcessingRules": True, "forwardTo": []}}]
    rules = graph_source().read_rules()
    assert [r["name"] for r in rules] == ["First", "Second"] and rules[1]["enabled"] is False
    assert rules[0]["conditions"] == {"fromAddresses": ["x@y.test"]}  # False is no condition
    assert rules[0]["actions"] == {"moveToFolder": ["Inbox", "Work"], "stopProcessingRules": True}
    assert sieve.convert(rules)["converted"] == ["First"]


def test_rules_from_ews(fake_ews):
    fake_ews.add_folder("work", "Work", "inbox")
    fake_ews.rules_xml = """<t:Rule><t:RuleId>1</t:RuleId><t:DisplayName>Boss</t:DisplayName><t:Priority>1</t:Priority><t:IsEnabled>true</t:IsEnabled>
    <t:Conditions><t:ContainsSubjectStrings><t:String>urgent</t:String></t:ContainsSubjectStrings>
    <t:FromAddresses><t:Address><t:EmailAddress>boss@x.test</t:EmailAddress></t:Address></t:FromAddresses></t:Conditions>
    <t:Exceptions><t:ContainsBodyStrings><t:String>no</t:String></t:ContainsBodyStrings></t:Exceptions>
    <t:Actions><t:MoveToFolder><t:FolderId Id="id-work"/></t:MoveToFolder><t:MarkAsRead>true</t:MarkAsRead><t:StopProcessingRules>true</t:StopProcessingRules></t:Actions></t:Rule>
    <t:Rule><t:RuleId>2</t:RuleId><t:DisplayName>Size</t:DisplayName><t:Priority>2</t:Priority><t:IsEnabled>true</t:IsEnabled>
    <t:Conditions><t:WithinSizeRange><t:MinimumSize>1</t:MinimumSize></t:WithinSizeRange></t:Conditions><t:Actions><t:Delete>true</t:Delete></t:Actions></t:Rule>"""
    rules = ews_source().read_rules()
    assert rules[0]["conditions"] == {"subjectContains": ["urgent"], "fromAddresses": ["boss@x.test"]}
    assert rules[0]["exceptions"] == {"bodyContains": ["no"]}
    assert rules[0]["actions"] == {"moveToFolder": ["Inbox", "Work"], "markAsRead": True, "stopProcessingRules": True}
    out = sieve.convert(rules)
    assert out["converted"] == ["Boss"] and out["not_converted"][0]["name"] == "Size"
    assert 'not body :text :contains "no"' in out["script"]


class FakeManageSieve(threading.Thread):
    """A ManageSieve server in a thread, RFC 5804 greeting and answers, without TLS: `security` none."""

    def __init__(self, sasl=b"PLAIN"):
        super().__init__(daemon=True)
        self.srv = socket.create_server(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        self.sasl = sasl
        self.lines: list[bytes] = []
        self.script = b""
        self.start()

    def run(self):
        conn, _ = self.srv.accept()
        f = conn.makefile("rb")
        conn.sendall(b'"IMPLEMENTATION" "Dovecot Pigeonhole"\r\n"SASL" "' + self.sasl + b'"\r\n"SIEVE" "fileinto"\r\nOK "ready"\r\n')
        while True:
            line = f.readline()
            if not line:
                return
            self.lines.append(line.strip())
            if line.startswith(b"PUTSCRIPT"):
                size = int(line.rsplit(b"{", 1)[1].split(b"+")[0])
                self.script = f.read(size)
                f.readline()
            if line.startswith(b"AUTHENTICATE") and base64.b64decode(line.split(b'"')[3]) != b"\0me\0pw":
                conn.sendall(b'NO "wrong"\r\n')
                continue
            conn.sendall(b'OK "done"\r\n')
            if line.startswith(b"LOGOUT"):
                return


def test_managesieve_upload():
    server = FakeManageSieve()
    sieve.upload("# s\n", "127.0.0.1", "me", "pw", server.port, "none", name="my script")
    server.join(5)
    assert server.script == b"# s\n" and server.lines[0].startswith(b'AUTHENTICATE "PLAIN"')
    assert not [x for x in server.lines if x.startswith(b"SETACTIVE")]  # not activated unless asked


def test_managesieve_activate_and_refusals():
    server = FakeManageSieve()
    sieve.upload("# s\n", "127.0.0.1", "me", "pw", server.port, "none", activate=True)
    server.join(5)
    assert b'SETACTIVE "mccb-exchange-rules"' in server.lines
    server = FakeManageSieve()
    with pytest.raises(ConnectorError) as err:
        sieve.upload("# s\n", "127.0.0.1", "me", "wrong", server.port, "none")
    assert err.value.kind == ConnectorError.AUTH
    server = FakeManageSieve(b"GSSAPI")
    with pytest.raises(ConnectorError) as err:
        sieve.upload("# s\n", "127.0.0.1", "me", "pw", server.port, "none")
    assert err.value.kind == ConnectorError.CONFIG
    server = FakeManageSieve()
    with pytest.raises(ConnectorError) as err:  # no STARTTLS offered: the password stays home
        sieve.upload("# s\n", "127.0.0.1", "me", "pw", server.port, "starttls")
    assert err.value.kind == ConnectorError.CONFIG and not [x for x in server.lines if x.startswith(b"AUTHENTICATE")]


def test_managesieve_unreachable():
    with socket.create_server(("127.0.0.1", 0)) as s:
        port = s.getsockname()[1]
    with pytest.raises(ConnectorError) as err:
        sieve.upload("# s\n", "127.0.0.1", "me", "pw", port, "none")
    assert err.value.kind == ConnectorError.UNREACHABLE


# ------------------------------------------------------------------ the routes, through the API and the real runner
class CollectSession(TargetSession):
    def __init__(self, store):
        self.store = store
        self.is_open = False

    @property
    def connected(self):
        return self.is_open

    def connect(self):
        self.is_open = True

    def close(self):
        self.is_open = False

    def plan_folders(self, folders):
        from app.server.runner import folder_key
        return {folder_key(f.path): FolderPlan(f"{f.kind}:" + "/".join(f.path)) for f in folders}

    def ensure_folder(self, name, flag=None):
        pass

    def put(self, folder, item):
        self.store.append((folder, item))
        return item.import_id

    def find(self, folder, item):
        return [i.import_id for f, i in self.store if f == folder and i.import_id == item.import_id]

    def count_ours(self, folder, namespace, names=None):
        return sum(1 for f, i in self.store if f == folder and i.import_id.startswith(f"<{namespace}-"))


class CollectTarget(Target):
    capabilities = frozenset()
    store: list = []

    def __init__(self, config, secrets, ctx=None):
        pass

    def session(self):
        return CollectSession(self.store)


@pytest.fixture
def collect():
    CollectTarget.store = []
    registry.register(registry.ConnectorSpec(
        kind="collect", label="Collect",
        target=registry.Role(CollectTarget, ("mail", "calendar", "contacts", "tasks"), frozenset(), (), lambda c: "c")))
    yield CollectTarget.store
    registry.unregister("collect")


def test_route_ews_to_a_target_holding_tasks(client, fake_ews, collect):
    seed_ews(fake_ews)
    aid = make_account(client)
    r = client.put(f"/api/accounts/{aid}/source", json={"kind": "exchange_ews", "config": {**EWS_CONFIG, "auth": "basic"},
                                                        "secrets": {"password": "pw"}})
    assert r.status_code == 200, r.text
    assert "pw" not in str(r.json()["source"]["config"])
    assert client.put(f"/api/accounts/{aid}/target", json={"kind": "collect"}).status_code == 200
    jid = start(client, aid)
    j = run_job(client, jid)
    assert j["status"] == "done", log_of(client, jid)
    kinds = sorted(i.kind for _f, i in collect)
    assert kinds == ["calendar", "contacts", "mail", "mail", "mail", "mail", "mail", "tasks"]
    assert j["progress"]["tasks"] == {"done": 1, "total": 1, "errors": 0}
    assert j["progress"]["mail"]["done"] == 5 and j["progress"]["calendar"]["done"] == 1 and j["progress"]["contacts"]["done"] == 1
    rep = client.get(f"/api/jobs/{jid}/report").json()
    assert rep["dav"]["tasks"]["sent"] == 1 and rep["dav"]["tasks"]["ok"] is True and rep["tasks"]["done"] == 1
    mail = [i for _f, i in collect if i.kind == MAIL][0]
    assert mail.raw.startswith(b"X-PST-Import-Id: <src-") and mail.internal_date is not None
    # no duplicates on a second run, for any kind
    assert client.post(f"/api/jobs/{jid}/retry").status_code == 200
    assert run_job(client, jid)["status"] == "done"
    assert len(collect) == 8


def test_route_tasks_follow_the_calendar_option(client, fake_ews, collect):
    from app.server import db
    seed_ews(fake_ews)
    aid = make_account(client)
    client.put(f"/api/accounts/{aid}/source", json={"kind": "exchange_ews", "config": {**EWS_CONFIG, "auth": "basic"}, "secrets": {"password": "pw"}})
    client.put(f"/api/accounts/{aid}/target", json={"kind": "collect"})
    db.ex("UPDATE accounts SET opt_calendar=0 WHERE id=?", (aid,))
    jid = start(client, aid)
    assert run_job(client, jid)["status"] == "done", log_of(client, jid)
    assert not [i for _f, i in collect if i.kind in (CALENDAR, TASKS)]


def test_route_no_target_for_tasks_is_logged(client, fake_ews, monkeypatch):
    """To the server of an account without DAV: mail goes, the others are said not to."""
    from tests.server import connector_fakes as fakes
    fakes.register()
    try:
        seed_ews(fake_ews)
        fakes.TARGETS["t"] = fakes.MemoryStore()
        aid = make_account(client)
        client.put(f"/api/accounts/{aid}/source", json={"kind": "exchange_ews", "config": {**EWS_CONFIG, "auth": "basic"}, "secrets": {"password": "pw"}})
        client.put(f"/api/accounts/{aid}/target", json={"kind": "memory", "config": {"name": "t"}})
        jid = start(client, aid)
        assert run_job(client, jid)["status"] == "done", log_of(client, jid)
        assert len(fakes.TARGETS["t"].items) == 5
        log = log_of(client, jid)
        assert "tasks folder(s) at the source, no target for them" in log and "calendar folder(s)" in log
    finally:
        fakes.unregister()


def test_route_imap_like_source_to_graph(client, fake_graph, monkeypatch):
    """The reverse direction: any source to Exchange Online, with a retry after throttling."""
    from tests.server import connector_fakes as fakes
    fakes.register()
    try:
        fakes.SOURCES["s"] = fakes.MemoryMailbox({"INBOX": 3, "Projects": 2})
        aid = make_account(client)
        client.put(f"/api/accounts/{aid}/source", json={"kind": "memory", "config": {"name": "s"}, "secrets": {"token": "ok"}})
        r = client.put(f"/api/accounts/{aid}/target", json={"kind": "m365_graph", "config": {**GRAPH_CONFIG, "root_folder": "Old"},
                                                            "secrets": {"client_secret": fake_graph.SECRET}})
        assert r.status_code == 200, r.text
        fake_graph.throttle = [2]
        jid = start(client, aid)
        j = run_job(client, jid)
        assert j["status"] == "done", log_of(client, jid)
        assert len(fake_graph.messages) == 5
        names = {f["displayName"] for f in fake_graph.mail_folders.values()}
        assert {"Old", "Projects"} <= names
        rep = client.get(f"/api/jobs/{jid}/report").json()
        assert rep["mail"]["present"] == 5 and rep["mail"]["errors"] == 0
    finally:
        fakes.unregister()


def test_route_graph_to_ews(client, monkeypatch, waits):
    """Exchange Online to Exchange on premises: every kind written, recurrences and tasks included."""
    graph, server = fx.FakeGraph(), fx.FakeEws()

    def transport(request):
        return server(request) if request.url.host == "ews.example.test" else graph(request)
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(transport))
    seed_graph(graph)
    aid = make_account(client)
    client.put(f"/api/accounts/{aid}/source", json={"kind": "m365_graph", "config": GRAPH_CONFIG, "secrets": {"client_secret": graph.SECRET}})
    client.put(f"/api/accounts/{aid}/target", json={"kind": "exchange_ews", "config": {**EWS_CONFIG, "auth": "basic"}, "secrets": {"password": "pw"}})
    jid = start(client, aid)
    j = run_job(client, jid)
    assert j["status"] == "done", log_of(client, jid)
    assert len(server.in_folder("id-inbox")) == 3 and len(server.in_folder("id-calendar")) == 1
    assert len(server.in_folder("id-contacts")) == 1 and len(server.in_folder("id-tasks")) == 1
    cal = server.in_folder("id-calendar")[0]["el"]
    assert cal.find(fx.t("Recurrence")).find(fx.t("RelativeMonthlyRecurrence")) is not None
    rep = client.get(f"/api/jobs/{jid}/report").json()
    assert rep["dav"]["tasks"]["ok"] is True and rep["dav"]["calendar"]["ok"] is True


def test_route_graph_final_resync_after_a_full_copy(client, monkeypatch, waits):
    """The cutover workflow: full copy, then a final resync (delta) with no cursor yet, then another one."""
    graph, server = fx.FakeGraph(), fx.FakeEws()

    def transport(request):
        return server(request) if request.url.host == "ews.example.test" else graph(request)
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(transport))
    seed_graph(graph)
    aid = make_account(client)
    client.put(f"/api/accounts/{aid}/source", json={"kind": "m365_graph", "config": GRAPH_CONFIG, "secrets": {"client_secret": graph.SECRET}})
    client.put(f"/api/accounts/{aid}/target", json={"kind": "exchange_ews", "config": {**EWS_CONFIG, "auth": "basic"}, "secrets": {"password": "pw"}})
    jid = start(client, aid)
    assert run_job(client, jid)["status"] == "done", log_of(client, jid)
    graph.add_message("mf-inbox", MIME, id="msg-late", receivedDateTime="2024-03-03T12:00:00Z")
    for n in (4, 5):
        assert client.post(f"/api/jobs/{jid}/delta").status_code == 200
        j = run_job(client, jid)
        assert j["status"] == "done", log_of(client, jid)
        assert len(server.in_folder("id-inbox")) == n
        graph.add_message("mf-inbox", MIME, id="msg-later", receivedDateTime="2024-03-04T12:00:00Z")
    assert "changed since the previous pass" in log_of(client, jid)
    assert not any("$expand" in c for c in graph.delta_calls)


def test_route_source_auth_failure_fails_the_job(client, fake_graph, collect):
    aid = make_account(client)
    client.put(f"/api/accounts/{aid}/source", json={"kind": "m365_graph", "config": GRAPH_CONFIG, "secrets": {"client_secret": "wrong"}})
    client.put(f"/api/accounts/{aid}/target", json={"kind": "collect"})
    j = run_job(client, start(client, aid))
    assert j["status"] == "failed" and "invalid_client" in j["error"] and "wrong" not in j["error"].replace("wrong secret", "")


def test_api_secret_never_round_trips(client, fake_graph):
    aid = make_account(client)
    r = client.put(f"/api/accounts/{aid}/source", json={"kind": "m365_graph", "config": GRAPH_CONFIG, "secrets": {"client_secret": "s3cret-value"}})
    assert r.status_code == 200 and "s3cret-value" not in r.text and r.json()["source"]["has_secret"] is True
    assert client.put(f"/api/accounts/{aid}/source", json={"kind": "m365_graph", "config": {**GRAPH_CONFIG, "client_secret": "x"}}).status_code == 422
    assert client.put(f"/api/accounts/{aid}/source", json={"kind": "m365_graph", "config": GRAPH_CONFIG, "secrets": {}}).status_code == 422


def test_api_test_endpoint(client, fake_graph):
    seed_graph(fake_graph)
    r = client.post("/api/connectors/m365_graph/test", json={"config": GRAPH_CONFIG, "secrets": {"client_secret": fake_graph.SECRET}}).json()
    assert r["ok"] is True and {"path": ["Inbox"], "kind": "mail", "special": "inbox", "count": None} in r["folders"]
    r = client.post("/api/connectors/m365_graph/test", json={"config": GRAPH_CONFIG, "secrets": {"client_secret": "no"}}).json()
    assert r["ok"] is False and r["error_kind"] == "auth"
    r = client.post("/api/connectors/m365_graph/test", json={"role": "target", "config": GRAPH_CONFIG, "secrets": {"client_secret": fake_graph.SECRET}}).json()
    assert r["ok"] is True and r["protocol"] == "Microsoft Graph" and r["import_mode"] == "import_mime"


def test_api_rules(client, fake_graph):
    fake_graph.rules = [{"id": "1", "displayName": "R", "sequence": 1, "isEnabled": True, "conditions": {"subjectContains": ["x"]},
                         "actions": {"delete": True}},
                        {"id": "2", "displayName": "S", "sequence": 2, "isEnabled": True, "conditions": {"isSigned": True},
                         "actions": {"delete": True}}]
    aid = make_account(client)
    url = f"/api/accounts/{aid}/rules"
    assert client.post(url, json={}).status_code == 422  # a PST: no rules to read
    client.put(f"/api/accounts/{aid}/source", json={"kind": "m365_graph", "config": GRAPH_CONFIG, "secrets": {"client_secret": fake_graph.SECRET}})
    r = client.post(url, json={"trash_folder": "Bin"}).json()
    assert r["ok"] is True and r["uploaded"] is False and r["converted"] == ["R"] and 'fileinto "Bin"' in r["script"]
    assert r["not_converted"] == [{"name": "S", "reasons": ["condition: signed"]}]
    server = FakeManageSieve()
    r = client.post(url, json={"managesieve": {"host": "127.0.0.1", "port": server.port, "security": "none", "login": "me", "password": "pw"}}).json()
    server.join(5)
    assert r["uploaded"] is True and r["activated"] is False and server.script.startswith(b"# Converted")
    with socket.create_server(("127.0.0.1", 0)) as s:
        port = s.getsockname()[1]
    r = client.post(url, json={"managesieve": {"host": "127.0.0.1", "port": port, "security": "none", "login": "me", "password": "pw"}}).json()
    assert r["ok"] is True and r["uploaded"] is False and r["upload_error"]["error_kind"] == "unreachable" and r["script"]


def test_wiring_gives_tasks_their_own_dav_target(client, env):
    from app.server import db
    sid = client.post("/api/servers", json={"name": "S", "imap_host": "i.test", "imap_port": 993, "imap_security": "ssl", "verify_tls": True,
                                            "dav_enabled": True, "caldav_url": "https://d.test/c/", "carddav_url": "https://d.test/a/",
                                            "workers": 2, "tuning": "manual", "notes": ""}).json()["id"]
    aid = client.post("/api/accounts", json={"server_id": sid, "email": "a@x.test", "password": "pw"}).json()["id"]
    acc = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
    srv = db.q1("SELECT * FROM servers WHERE id=?", (sid,))
    targets = wiring.targets_of_account(acc, srv)
    assert set(targets) == {"mail", "calendar", "contacts", "tasks"}
    assert targets["tasks"].component == "VTODO" and targets["calendar"].component == "VEVENT"
