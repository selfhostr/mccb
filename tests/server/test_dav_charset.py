"""Accents in contacts, events and tasks survive a DAV migration: the objects are read as bytes (never in the charset
a Content-Type says or leaves out: "é" would come out "Ã©"), copied as they are when they are UTF-8, and written in
UTF-8 when they are not (vCard 2.1 in its CHARSET, else cp1252), never with U+FFFD in place of a character.

Units; a fake SmarterMail (tests/server/test_dav_smartermail.py) end to end through the runner; Radicale for a real
server (TEST_DAV_URL, docker-compose.test.yml)."""
import httpx
import pytest

from app.connectors import dav as cdav
from app.connectors import davfix
from app.connectors.base import CONTACTS, OutgoingItem
from app.server import dav as sdav
from app.server.dav import DavClient
from tests.server.test_dav_smartermail import DAV_URL, account, items, needs_dav, run, sm  # noqa: F401 - sm: fixture

NAME = "Laetitia Loehlé"
CARD = ("BEGIN:VCARD\r\nVERSION:3.0\r\nUID:c-1\r\nFN:Laetitia Loehlé\r\n"
        'N;SORT-AS="Loehlé, Laetitia":Loehlé;Laetitia;;;\r\nORG:Société générale;Comptabilité\r\n'
        "ADR;TYPE=WORK:;;Rue des Fougères 2;Genève;;1204;Suisse\r\nNOTE:Déjà vu – à rappeler € 20\r\nEND:VCARD\r\n")
EVENT = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//t//EN\r\nBEGIN:VEVENT\r\nUID:e-1\r\n"
         "DTSTAMP:20240301T000000Z\r\nDTSTART:20240305T100000Z\r\nSUMMARY:Réunion d’équipe à Zürich\r\n"
         "LOCATION:Salle « Mont-Blanc »\\, 2ᵉ étage\r\nDESCRIPTION:Ordre du jour : budget\\, congés\\nÀ préparer\r\n"
         "END:VEVENT\r\nEND:VCALENDAR\r\n")
TASK = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//t//EN\r\nBEGIN:VTODO\r\nUID:t-1\r\n"
        "DTSTAMP:20240301T000000Z\r\nSUMMARY:Préparer les clés\r\nDESCRIPTION:Après la réunion\r\n"
        "END:VTODO\r\nEND:VCALENDAR\r\n")
# vCard 2.1 as an old Windows client wrote it: 8-bit cp1252 with a CHARSET, and a quoted-printable value
CARD21 = ("BEGIN:VCARD\r\nVERSION:2.1\r\nUID:c-21\r\nN;CHARSET=ISO-8859-1:Müller;Hélène\r\n"
          "FN;CHARSET=ISO-8859-1:Hélène Müller\r\nNOTE;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:Caf=C3=A9\r\n"
          "TITLE:Chargée d'affaires\r\nEND:VCARD\r\n").encode("cp1252")


# ------------------------------------------------------------------ units
def test_decode_object_is_utf8_whatever_else():
    decode_object = sdav.decode_object
    assert decode_object(CARD.encode()) == CARD
    assert decode_object(b"\xef\xbb\xbf" + CARD.encode()) == CARD  # a BOM is not part of the object
    text = decode_object(CARD21)
    assert "FN:Hélène Müller\r\n" in text and "N:Müller;Hélène\r\n" in text  # its CHARSET honoured, then dropped
    assert "NOTE;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:Caf=C3=A9\r\n" in text  # ASCII: as it is
    assert "TITLE:Chargée d'affaires\r\n" in text  # no CHARSET: cp1252
    # one line not UTF-8 in a UTF-8 object: only that line is read otherwise
    mixed = "FN:Zoë\r\n".encode() + "NOTE:Noël – €\r\n".encode("cp1252")
    assert decode_object(mixed) == "FN:Zoë\r\nNOTE:Noël – €\r\n"
    assert decode_object(b"NOTE:\x81\x9d\r\n") == "NOTE:\x81\x9d\r\n"  # bytes cp1252 lacks: kept, nothing lost
    assert "�" not in decode_object(CARD21)


@pytest.mark.parametrize("ctype", [None, "text/vcard", "text/vcard; charset=iso-8859-1", "text/x-vcard; charset=windows-1252",
                                   "text/plain"])
def test_a_get_reads_the_bytes_not_the_charset_the_server_says(ctype):
    body = CARD.encode()

    def answer(req):
        return httpx.Response(200, content=body, headers={"Content-Type": ctype} if ctype else {})

    c = DavClient("u", "p", transport=httpx.MockTransport(answer))
    try:
        assert c.get("https://d.test/book/", "c-1.vcf") == CARD
        assert c.get_bytes("https://d.test/book/", "c-1.vcf") == body
    finally:
        c.close()


def test_the_second_forms_keep_the_accents():
    (_n, normalized, _), (_m, minimal, _) = davfix.forms("carddav", CARD.replace("VERSION:3.0", "VERSION:4.0")
                                                         .replace("END:VCARD", "X-BOOM:é\r\nEND:VCARD"))
    for t in (normalized, minimal):
        assert "FN:Laetitia Loehlé\r\n" in t and "Comptabilité" in t and "Genève" in t and "Ã" not in t
        assert all(len(ln.encode()) <= 75 for ln in t.split("\r\n"))
    long = EVENT.replace("SUMMARY:", "SUMMARY:" + "é" * 60).replace("DTSTAMP:20240301T000000Z\r\n", "")
    ev = next(t for label, t, _notes in davfix.forms("caldav", long) if label == "normalized")
    assert all(len(ln.encode()) <= 75 for ln in ev.split("\r\n")) and "Ã" not in ev  # folded between characters
    lines = davfix.unfold(ev)
    assert "SUMMARY:" + "é" * 60 + "Réunion d’équipe à Zürich" in lines
    assert "LOCATION:Salle « Mont-Blanc »\\, 2ᵉ étage" in lines


def test_an_object_not_in_utf8_is_written_in_utf8(sm):
    """A PST or a file may hand an object in cp1252: written in UTF-8 (it said what it is), never with U+FFFD."""
    _src, dst = sm
    col = "https://dst.test" + dst.add("b64=Q29udGFjdHM", "Contacts")
    s = cdav.CardDavTarget({"url": "https://dst.test/dav/me/", "login": "me"}, {"password": "p"}).session()
    s.connect()
    s.put(col, OutgoingItem(CONTACTS, CARD21, "<x>", name="c-21.vcf"))
    got = dst.by_name("Contacts")["c-21.vcf"].decode("utf-8")
    assert "FN:Hélène Müller\r\n" in got and "TITLE:Chargée d'affaires" in got and "�" not in got
    assert s.read(col, "c-21.vcf") == got.encode()


# ------------------------------------------------------------------ end to end (fake SmarterMail)
@pytest.mark.parametrize("ctype", [None, "text/vcard; charset=iso-8859-1"])
def test_contacts_events_and_tasks_keep_their_accents(client, sm, ctype):
    src, dst = sm
    src.ctype = ctype
    boom = CARD.replace("UID:c-1", "UID:c-2").replace("FN:Laetitia Loehlé", "FN:Zoë Noël").replace("NOTE:", "X-BOOM:")
    src.add("b64=Q29udGFjdHM", "Contacts", items={"c-1.vcf": CARD, "c-21.vcf": CARD21, "c-2.vcf": boom})
    dst.add("b64=Q29udGFjdHM", "Contacts")
    aid = account(client)
    job, _rep, log = run(client, aid)
    assert job["status"] == "done", log
    got = dst.by_name("Contacts")
    assert got["c-1.vcf"] == CARD.encode()  # the very bytes of the source
    assert "FN:Hélène Müller\r\n" in got["c-21.vcf"].decode() and "TITLE:Chargée d'affaires" in got["c-21.vcf"].decode()
    # refused by the server as it is (X-BOOM): written in its minimal form, accents kept
    assert "FN:Zoë Noël\r\n" in got["c-2.vcf"].decode() and "Genève" in got["c-2.vcf"].decode()
    rows = items(aid)
    assert set(rows) == {NAME, "Hélène Müller", "Zoë Noël"} and "minimal form" in rows["Zoë Noël"]["warnings"]
    assert all("Ã" not in b.decode("utf-8") and "�" not in b.decode("utf-8") for b in got.values())

    src.add("b64=Q2FsZW5kYXI", "Calendrier", kind="cal", items={"e-1.ics": EVENT})
    src.add("b64=VMOiY2hlcw", "Tâches", kind="cal", comp="VTODO", items={"t-1.ics": TASK})
    dst.add("b64=Q2FsZW5kYXI", "Calendar", kind="cal")
    dst.add("b64=VGFza3M", "Tasks", kind="cal", comp="VTODO")
    aid = account(client, "caldav")
    job, _rep, log = run(client, aid)
    assert job["status"] == "done", log
    assert dst.by_name("Calendar")["e-1.ics"] == EVENT.encode()
    assert dst.by_name("Tasks")["t-1.ics"] == TASK.encode()
    assert set(items(aid)) == {"Réunion d’équipe à Zürich", "Préparer les clés"}


# ------------------------------------------------------------------ Radicale
@needs_dav
def test_radicale_keeps_the_accents_of_contacts_events_and_tasks(client):
    from tests.server.test_dav_default_collections import account as radicale_account
    from tests.server.test_dav_default_collections import home, run as radicale_run, tree
    from tests.svcusers import create_user
    for kind, objects in (("carddav", {"c-1.vcf": CARD}),
                          ("caldav", {"e-1.ics": EVENT, "t-1.ics": TASK})):
        src, dst = create_user(prefix="chsrc"), create_user(prefix="chdst")
        c = DavClient(src, "pw", False)
        try:
            if kind == "carddav":
                book = c.ensure_collection("carddav", home(src) + "book/", "VEVENT", "Contacts")
                cols = {n: book for n in objects}
            else:
                cal = c.ensure_collection("caldav", home(src) + "cal/", "VEVENT", "Calendrier")
                todo = c.ensure_collection("caldav", home(src) + "todo/", "VTODO", "Tâches")
                cols = {"e-1.ics": cal, "t-1.ics": todo}
            for n, body in objects.items():
                c.put(cols[n], n, body, kind)
        finally:
            c.close()
        aid = radicale_account(client, src, dst, kind)
        job, _rep, log = radicale_run(client, aid)
        assert job["status"] == "done", log
        got = {n: t for coll in tree(dst, kind).values() for n, t in coll.items()}
        assert set(got) == set(objects), got
        for n, body in objects.items():
            assert sorted(davfix.unfold(got[n])) == sorted(davfix.unfold(body)), n  # Radicale reorders, may fold
            assert "Ã" not in got[n] and "�" not in got[n]
