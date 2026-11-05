"""End-to-end builders on synthetic MAPI items (accents, cid, embedded, recurrence)."""
import email
import email.policy
import struct
from datetime import datetime, timedelta, timezone

import icalendar
import vobject

from app.engine import mapi
from app.engine.contact import build_contact
from app.engine.event import build_event
from app.engine.message import MessageBuilder, message_flags
from app.engine.verify import check_raw

from .conftest import BIN, BOOL, FT, I32, S8, U, FakeAtt, FakeMsg, namemap_with

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50 + bytes(range(256))
PDF = b"%PDF-1.4\n" + bytes(range(256)) * 40


def _parse(raw):
    return email.message_from_bytes(raw, policy=email.policy.default)


def _sent_mail():
    inner = FakeMsg({mapi.PR_SUBJECT: U("Pièce jointe embarquée"), mapi.PR_BODY: U("Texte intérieur ü"),
                     mapi.PR_MESSAGE_CLASS: U("IPM.Note"),
                     mapi.PR_SENDER_NAME: U("Éric"), mapi.PR_SENDER_EMAIL: U("eric@example.fr"),
                     mapi.PR_SENDER_ADDRTYPE: U("SMTP")},
                    attachments=[FakeAtt({mapi.PR_ATTACH_METHOD: I32(1),
                                          mapi.PR_ATTACH_LONG_FILENAME: U("intérieur.pdf")}, PDF)])
    html = ('<html><head><meta charset="windows-1252"></head><body><p>Voir l\'image ci-dessous, '
            'merci à vous ç</p><img src="cid:image001.png@01D9AB12.34567890"></body></html>').encode("cp1252")
    atts = [
        FakeAtt({mapi.PR_ATTACH_METHOD: I32(1), mapi.PR_ATTACH_LONG_FILENAME: U("image001.png"),
                 mapi.PR_ATTACH_CONTENT_ID: U("image001.png@01D9AB12.34567890"),
                 mapi.PR_ATTACHMENT_HIDDEN: BOOL(True)}, PNG),
        FakeAtt({mapi.PR_ATTACH_METHOD: I32(1), mapi.PR_ATTACH_LONG_FILENAME: U("Devis n°2024 – révisé.pdf"),
                 mapi.PR_ATTACH_MIME_TAG: U("application/octet-stream")}, PDF),
        FakeAtt({mapi.PR_ATTACH_METHOD: I32(1), mapi.PR_ATTACH_FILENAME: S8("RÉSUMÉ~1.TXT")},
                "Résumé en cp1252".encode("cp1252")),
        FakeAtt({mapi.PR_ATTACH_METHOD: I32(5), mapi.PR_DISPLAY_NAME: U("Untitled")}, embedded=inner),
    ]
    recips = [
        {mapi.PR_DISPLAY_NAME: U("Zoé Dupont"), mapi.PR_EMAIL_ADDRESS: U("/o=ORG/cn=zoe"),
         mapi.PR_ADDRTYPE: U("EX"), mapi.PR_SMTP_ADDRESS: U("zoe@example.fr"), mapi.PR_RECIPIENT_TYPE: I32(1)},
        {mapi.PR_DISPLAY_NAME: U("Équipe, Ventes"), mapi.PR_EMAIL_ADDRESS: U("ventes@example.fr"),
         mapi.PR_ADDRTYPE: U("SMTP"), mapi.PR_RECIPIENT_TYPE: I32(2)},
        {mapi.PR_DISPLAY_NAME: U("caché@example.fr"), mapi.PR_EMAIL_ADDRESS: U("bcc@example.fr"),
         mapi.PR_ADDRTYPE: U("SMTP"), mapi.PR_RECIPIENT_TYPE: I32(3)},
    ]
    props = {
        mapi.PR_MESSAGE_CLASS: U("IPM.Note"),
        mapi.PR_SUBJECT: U("\x01\x05RE: Réunion « budget » à 10h – ok ?"),
        mapi.PR_HTML: BIN(html), mapi.PR_INTERNET_CPID: I32(1252),
        mapi.PR_BODY: U("Voir l'image ci-dessous, merci à vous ç"),
        mapi.PR_SENDER_NAME: U("Jérôme Lefèvre"), mapi.PR_SENDER_EMAIL: U("/o=ORG/cn=jerome"),
        mapi.PR_SENDER_ADDRTYPE: U("EX"), mapi.PR_SENDER_SMTP_ADDRESS: U("jerome@example.fr"),
        mapi.PR_CLIENT_SUBMIT_TIME: FT(datetime(2024, 3, 1, 9, 30, tzinfo=timezone.utc)),
        mapi.PR_MESSAGE_DELIVERY_TIME: FT(datetime(2024, 3, 1, 9, 31, tzinfo=timezone.utc)),
        mapi.PR_INTERNET_MESSAGE_ID: U("<orig-123@example.fr>"),
        mapi.PR_MESSAGE_FLAGS: I32(1), mapi.PR_FLAG_STATUS: I32(2), mapi.PR_LAST_VERB_EXECUTED: I32(102),
    }
    return FakeMsg(props, recips, atts)


def test_rich_message_rebuilt_from_mapi():
    src = _sent_mail()
    built = MessageBuilder("deadbeef-42-1234").build_raw(src)
    assert check_raw(built.raw, built.expect) == [], built.warnings
    assert "fallback" not in built.warnings
    raw = built.raw
    assert raw.split(b"\r\n\r\n", 1)[0].isascii()
    assert raw.startswith(b"X-PST-Import-Id: deadbeef-42-1234\r\n")
    m = _parse(raw)
    assert m["subject"] == "RE: Réunion « budget » à 10h – ok ?"
    assert m["from"].addresses[0].display_name == "Jérôme Lefèvre"
    assert m["from"].addresses[0].addr_spec == "jerome@example.fr"
    assert [a.addr_spec for a in m["to"].addresses] == ["zoe@example.fr"]
    assert m["cc"].addresses[0].display_name == "Équipe, Ventes"
    assert m["bcc"].addresses[0].addr_spec == "bcc@example.fr"
    assert m["message-id"] == "<orig-123@example.fr>"
    assert m["date"].datetime == datetime(2024, 3, 1, 9, 30, tzinfo=timezone.utc)
    # structure: mixed[alternative[plain, related[html, png]], pdf, txt, rfc822]
    assert m.get_content_type() == "multipart/mixed"
    html_part = m.get_body(("html",))
    html = html_part.get_content()
    assert "merci à vous ç" in html and html_part.get_content_charset() == "utf-8"
    related = [p for p in m.walk() if p.get_content_type() == "multipart/related"]
    assert related, "inline image must be in multipart/related"
    img = [p for p in related[0].iter_parts() if p.get_content_type() == "image/png"][0]
    assert img["Content-ID"] == "<image001.png@01D9AB12.34567890>"
    assert img.get_content_disposition() == "inline"
    names = {p.get_filename(): p for p in m.iter_attachments()}
    assert set(names) == {"Devis n°2024 – révisé.pdf", "RÉSUMÉ~1.TXT", "Pièce jointe embarquée.eml"}
    assert names["Devis n°2024 – révisé.pdf"].get_content_type() == "application/pdf"  # detected
    assert names["Devis n°2024 – révisé.pdf"].get_payload(decode=True) == PDF
    assert names["RÉSUMÉ~1.TXT"].get_content_type() == "text/plain"
    emb = names["Pièce jointe embarquée.eml"]
    assert emb.get_content_type() == "message/rfc822"
    inner = emb.get_payload(0)
    assert inner["subject"] == "Pièce jointe embarquée"
    assert [p.get_filename() for p in inner.iter_attachments()] == ["intérieur.pdf"]
    assert built.parts.subject.startswith("RE: Réunion")
    assert message_flags(src.bag) == ["\\Seen", "\\Flagged", "\\Answered"]
    assert "�" not in raw.decode("ascii")


def test_cid_unresolved_is_warning_and_cid_by_filename():
    html = '<img src="cid:logo.png@x"><img src="cid:missing.gif">'
    atts = [FakeAtt({mapi.PR_ATTACH_METHOD: I32(1), mapi.PR_ATTACH_LONG_FILENAME: U("logo.png")}, PNG)]
    src = FakeMsg({mapi.PR_MESSAGE_CLASS: U("IPM.Note"), mapi.PR_HTML: BIN(html.encode())}, attachments=atts)
    built = MessageBuilder("x-1").build_raw(src)
    assert check_raw(built.raw, built.expect) == []
    assert "cid-unresolved:missing.gif" in built.warnings
    m = _parse(built.raw)
    img = [p for p in m.walk() if p.get_content_type() == "image/png"][0]
    assert img["Content-ID"] == "<logo.png@x>"


def test_rtf_only_body_and_missing_everything():
    rtf = b"{\\rtf1\\ansi\\ansicpg1252 Bonjour \\'e0 tous\\par}"
    comp = (len(rtf) + 12).to_bytes(4, "little") + len(rtf).to_bytes(4, "little") + b"MELA" + b"\0" * 4 + rtf
    src = FakeMsg({mapi.PR_MESSAGE_CLASS: U("IPM.Note"), mapi.PR_RTF_COMPRESSED: BIN(comp)})
    built = MessageBuilder("x-2").build_raw(src)
    assert "rtf-converted" in built.warnings
    m = _parse(built.raw)
    assert "Bonjour à tous" in m.get_body(("plain",)).get_content()
    assert m["message-id"].endswith("@pst-import.invalid>")
    # fully empty item still gives a valid message
    built = MessageBuilder("x-3").build_raw(FakeMsg({}))
    assert check_raw(built.raw, built.expect) == []


def test_fallback_when_rich_fails(monkeypatch):
    src = _sent_mail()
    b = MessageBuilder("x-4")
    orig = b.mime_tree

    def broken(parts, simple=False):
        if not simple:
            raise RuntimeError("boom")
        return orig(parts, simple=True)
    monkeypatch.setattr(b, "mime_tree", broken)
    built = b.build_raw(src)
    assert "fallback" in built.warnings
    assert check_raw(built.raw, built.expect) == []
    m = _parse(built.raw)
    assert m.get_body(("plain",)).get_content_charset() == "utf-8"
    assert "Devis n°2024 – révisé.pdf" in [p.get_filename() for p in m.iter_attachments()]


# --- contacts ---------------------------------------------------------------
A, AD, C = mapi.PSETID_APPOINTMENT, mapi.PSETID_ADDRESS, mapi.PSETID_COMMON


def test_contact_vcard():
    nm = namemap_with({(AD, mapi.LID_EMAIL1_ADDRESS): 0x8001, (AD, mapi.LID_EMAIL1_TYPE): 0x8002,
                       (AD, mapi.LID_EMAIL2_ADDRESS): 0x8003, (AD, mapi.LID_WORK_STREET): 0x8004,
                       (AD, mapi.LID_WORK_CITY): 0x8005, (AD, mapi.LID_WORK_POSTAL): 0x8006,
                       (AD, mapi.LID_BIRTHDAY_LOCAL): 0x8007})
    photo = b"\xff\xd8\xff\xe0" + bytes(300)
    src = FakeMsg({
        mapi.PR_MESSAGE_CLASS: U("IPM.Contact"), mapi.PR_DISPLAY_NAME: U("Zoé Lefèvre-Émond"),
        mapi.PR_GIVEN_NAME: U("Zoé"), mapi.PR_SURNAME: U("Lefèvre-Émond"),
        mapi.PR_COMPANY_NAME: U("Société Générale; Paris"), mapi.PR_TITLE: U("Directrice générale"),
        0x8001: U("zoe@example.fr"), 0x8002: U("SMTP"), 0x8003: U("z.l@example.com"),
        mapi.PR_MOBILE_TELEPHONE: U("+33 6 12 34 56 78"), mapi.PR_BUSINESS_TELEPHONE: U("01 23 45 67 89"),
        0x8004: U("12, rue de l'Église\nBâtiment B"), 0x8005: U("Créteil"), 0x8006: U("94000"),
        0x8007: FT(datetime(1985, 7, 14, tzinfo=timezone.utc)),
        mapi.PR_BODY: U("Notes : préfère être appelée le matin."),
    }, attachments=[FakeAtt({mapi.PR_ATTACH_METHOD: I32(1), mapi.PR_ATTACHMENT_CONTACTPHOTO: BOOL(True),
                             mapi.PR_ATTACH_LONG_FILENAME: U("ContactPicture.jpg")}, photo)], namemap=nm)
    summary, text, warnings = build_contact(src, "pst-vcf-1")
    v = vobject.readOne(text)
    assert v.fn.value == "Zoé Lefèvre-Émond"
    assert v.n.value.family == "Lefèvre-Émond" and v.n.value.given == "Zoé"
    assert [e.value for e in v.email_list] == ["zoe@example.fr", "z.l@example.com"]
    assert v.org.value[0] == "Société Générale; Paris"
    assert {t.value for t in v.tel_list} == {"+33 6 12 34 56 78", "01 23 45 67 89"}
    adr = v.adr.value
    assert adr.city == "Créteil" and "Église" in adr.street
    assert v.bday.value == "1985-07-14"
    assert "préfère" in v.note.value
    assert v.photo.value == photo
    assert v.uid.value == "pst-vcf-1"
    for line in text.split("\r\n"):
        assert len(line.encode()) <= 75


# --- events -----------------------------------------------------------------
def _minutes(dt):
    return int((dt - datetime(1601, 1, 1)).total_seconds() // 60)


def _recur_weekly(start_day, count, deleted, start_off, end_off):
    b = struct.pack("<HHHHHIII", 0x3004, 0x3004, 0x200B, 1, 0, 0, 1, 0)
    b += struct.pack("<I", 0b0001010)          # MO, WE
    b += struct.pack("<IIII", 0x2022, count, 1, len(deleted))
    b += b"".join(struct.pack("<I", _minutes(d)) for d in deleted)
    b += struct.pack("<I", 0)
    b += struct.pack("<II", _minutes(start_day), _minutes(start_day + timedelta(days=60)))
    b += struct.pack("<IIIIH", 0x3006, 0x3008, start_off, end_off, 0)
    b += struct.pack("<II", 0, 0)
    return b


def _tzdef(key):
    k = key.encode("utf-16-le")
    hdr = struct.pack("<BBHHH", 2, 1, 6 + len(k), 2, len(key)) + k + struct.pack("<H", 1)
    rule = struct.pack("<BBHHH", 2, 1, 0x3E, 2, 2023) + b"\0" * 14 + struct.pack("<iii", -60, 0, -60)
    rule += struct.pack("<8H", 0, 10, 0, 5, 3, 0, 0, 0) + struct.pack("<8H", 0, 3, 0, 5, 2, 0, 0, 0)
    return hdr + rule


def _event(tzkey="Romance Standard Time"):
    nm = namemap_with({(A, mapi.LID_START_WHOLE): 0x8101, (A, mapi.LID_END_WHOLE): 0x8102,
                       (A, mapi.LID_LOCATION): 0x8103, (A, mapi.LID_RECUR): 0x8104,
                       (A, mapi.LID_RECURRING): 0x8105, (A, mapi.LID_TZDEF_RECUR): 0x8106,
                       (A, mapi.LID_TZDEF_START): 0x8107, (C, mapi.LID_REMINDER_SET): 0x8108,
                       (C, mapi.LID_REMINDER_DELTA): 0x8109, (A, mapi.LID_SUBTYPE): 0x810A,
                       (A, mapi.LID_BUSY_STATUS): 0x810B})
    start = datetime(2024, 3, 4, 9, 0, tzinfo=timezone.utc)   # 10:00 Paris (Monday)
    props = {
        mapi.PR_MESSAGE_CLASS: U("IPM.Appointment"), mapi.PR_SUBJECT: U("Comité de pilotage"),
        0x8101: FT(start), 0x8102: FT(start + timedelta(hours=1)), 0x8103: U("Salle « Évasion »"),
        0x8104: BIN(_recur_weekly(datetime(2024, 3, 4), 10, [datetime(2024, 3, 11)], 600, 660)),
        0x8105: BOOL(True), 0x8106: BIN(_tzdef(tzkey)), 0x8107: BIN(_tzdef(tzkey)),
        0x8108: BOOL(True), 0x8109: I32(15), 0x810A: BOOL(False), 0x810B: I32(2),
        mapi.PR_BODY: U("Ordre du jour : sécurité"),
        mapi.PR_SENT_REPRESENTING_NAME: U("Hélène"), mapi.PR_SENT_REPRESENTING_EMAIL: U("helene@example.fr"),
        mapi.PR_SENT_REPRESENTING_ADDRTYPE: U("SMTP"),
    }
    recips = [{mapi.PR_DISPLAY_NAME: U("Hélène"), mapi.PR_EMAIL_ADDRESS: U("helene@example.fr"),
               mapi.PR_ADDRTYPE: U("SMTP"), mapi.PR_RECIPIENT_TYPE: I32(1), mapi.PR_RECIPIENT_FLAGS: I32(3)},
              {mapi.PR_DISPLAY_NAME: U("Noël"), mapi.PR_EMAIL_ADDRESS: U("noel@example.fr"),
               mapi.PR_ADDRTYPE: U("SMTP"), mapi.PR_RECIPIENT_TYPE: I32(2), mapi.PR_RECIPIENT_TRACKSTATUS: I32(3)}]
    return FakeMsg(props, recips, namemap=nm)


def test_event_recurring_with_timezone():
    summary, text, warnings = build_event(_event(), "pst-ics-1")
    assert warnings == []
    cal = icalendar.Calendar.from_ical(text)
    ev = [c for c in cal.walk("VEVENT")][0]
    assert str(ev["summary"]) == "Comité de pilotage"
    assert str(ev["location"]) == "Salle « Évasion »"
    dt = ev["dtstart"].dt
    assert str(dt.tzinfo) == "Europe/Paris" and dt.hour == 10
    rr = ev["rrule"]
    assert rr["FREQ"] == ["WEEKLY"] and rr["BYDAY"] == ["MO", "WE"] and rr["COUNT"] == [10]
    ex = ev["exdate"]
    exd = (ex[0] if isinstance(ex, list) else ex).dts[0].dt
    assert exd.date().isoformat() == "2024-03-11" and exd.hour == 10
    att = ev["attendee"]
    att = att if isinstance(att, list) else [att]
    assert len(att) == 1 and att[0].params["CN"] == "Noël"
    assert att[0].params["ROLE"] == "OPT-PARTICIPANT" and att[0].params["PARTSTAT"] == "ACCEPTED"
    assert att[0].params["SCHEDULE-AGENT"] == "CLIENT"
    assert "helene@example.fr" in str(ev["organizer"])
    alarm = [c for c in ev.walk("VALARM")][0]
    assert alarm["trigger"].dt == timedelta(minutes=-15)
    assert any(c.name == "VTIMEZONE" for c in cal.subcomponents)
    vobject.readOne(text)  # second parser


def test_event_unknown_timezone_uses_custom_vtimezone():
    summary, text, warnings = build_event(_event("Planet Mars Standard Time"), "pst-ics-2")
    cal = icalendar.Calendar.from_ical(text)
    ev = [c for c in cal.walk("VEVENT")][0]
    assert ev["dtstart"].params["TZID"] == "Planet Mars Standard Time"
    tz = [c for c in cal.walk("VTIMEZONE")][0]
    assert str(tz["tzid"]) == "Planet Mars Standard Time"
    vobject.readOne(text)


def test_event_bad_recurrence_still_gives_occurrence():
    src = _event()
    src.bag.props[0x8104] = mapi.Prop(0x8104, mapi.PT_BINARY, data=b"\x04\x30garbage")
    summary, text, warnings = build_event(src, "pst-ics-3")
    assert any(w.startswith("recurrence-unreadable") for w in warnings)
    ev = [c for c in icalendar.Calendar.from_ical(text).walk("VEVENT")][0]
    assert "rrule" not in ev and ev["dtstart"].dt.hour == 10


def test_allday_event():
    src = _event()
    src.bag.props[0x810A] = mapi.Prop(0x810A, mapi.PT_BOOLEAN, data=b"\x01")
    del src.bag.props[0x8104]
    del src.bag.props[0x8105]
    # all day 2024-03-04 Paris = 2024-03-03T23:00Z .. 2024-03-04T23:00Z
    s = datetime(2024, 3, 3, 23, 0, tzinfo=timezone.utc)
    src.bag.props[0x8101] = mapi.Prop(0x8101, mapi.PT_SYSTIME, data=FT(s)[1])
    src.bag.props[0x8102] = mapi.Prop(0x8102, mapi.PT_SYSTIME, data=FT(s + timedelta(days=1))[1])
    _, text, _ = build_event(src, "pst-ics-4")
    ev = [c for c in icalendar.Calendar.from_ical(text).walk("VEVENT")][0]
    assert ev["dtstart"].dt.isoformat() == "2024-03-04" and ev["dtend"].dt.isoformat() == "2024-03-05"


def test_long_subject_token_is_encoded_and_lines_short():
    subj = "A" * 3000
    src = FakeMsg({mapi.PR_MESSAGE_CLASS: U("IPM.Note"), mapi.PR_SUBJECT: U(subj), mapi.PR_BODY: U("x")})
    built = MessageBuilder("x-long1").build_raw(src)
    assert check_raw(built.raw, built.expect) == []
    assert max(len(ln) for ln in built.raw.split(b"\r\n")) <= 998
    assert _parse(built.raw)["subject"] == subj


def test_long_references_token_dropped():
    src = FakeMsg({mapi.PR_MESSAGE_CLASS: U("IPM.Note"), mapi.PR_BODY: U("x"),
                   mapi.PR_INTERNET_REFERENCES: U("<ok@x> <" + "b" * 2000 + "@x>"),
                   mapi.PR_IN_REPLY_TO_ID: U("<" + "c" * 2000 + "@x>"),
                   mapi.PR_INTERNET_MESSAGE_ID: U("<" + "d" * 2000 + "@x>")})
    built = MessageBuilder("x-long2").build_raw(src)
    assert check_raw(built.raw, built.expect) == []
    m = _parse(built.raw)
    assert str(m["references"]).strip() == "<ok@x>"
    assert m["in-reply-to"] is None
    assert m["message-id"].endswith("@pst-import.invalid>")


def test_check_raw_flags_overlong_line():
    raw = b"X-PST-Import-Id: a\r\nSubject: " + b"A" * 1000 + b"\r\n\r\nbody\r\n"
    assert "line longer than 998 octets" in check_raw(raw, None)
