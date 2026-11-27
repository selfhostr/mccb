"""DAV errors seen migrating SmarterMail to SmarterMail: a 500 the server gives every time for some objects (an object
of the same UID elsewhere in the account, content it does not take) fails that object in a few seconds with its
reason, or gets it written in a plainer form, never after a minute of retries; a collection refusing every item trips
a breaker; the collections other accounts share and the public ones are left to their owner; a collection the target
will not create is one folder error; an empty one is neither created nor checked; two objects of one UID that the
server keeps as one are not a verification problem.

Against a fake SmarterMail (httpx.MockTransport), end to end through the runner; and Radicale for the parts a real
server shows (TEST_DAV_URL, docker-compose.test.yml)."""
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote

import httpx
import pytest

from app.connectors import dav as cdav
from app.connectors import davfix
from app.connectors.base import CONTACTS, ConnectorError, OutgoingItem

DAV_URL = os.environ.get("TEST_DAV_URL")
needs_dav = pytest.mark.skipif(not DAV_URL, reason="TEST_DAV_URL not set")
ME = "/principals/me/"


def vcf(uid, fn, extra=""):
    return f"BEGIN:VCARD\r\nVERSION:3.0\r\nUID:{uid}\r\nFN:{fn}\r\nN:{fn};;;;\r\n{extra}END:VCARD\r\n"


def ics(uid, summary, comp="VEVENT"):
    return (f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//t//EN\r\nBEGIN:{comp}\r\nUID:{uid}\r\n"
            f"DTSTAMP:20240301T000000Z\r\nDTSTART:20240305T100000Z\r\nSUMMARY:{summary}\r\nEND:{comp}\r\nEND:VCALENDAR\r\n")


def _uid(body: bytes) -> str | None:
    return davfix.object_uid(body)


class FakeSmarterMail:
    """One account per host: /dav/me/ is the home of its collections (address books and calendars alike).

    Like SmarterMail as seen in a real migration: one object per UID in the whole account (a second one, even in
    another collection, gets 500); 500 to If-None-Match over an existing resource (`inm_500`); 500 to a vCard 4.0 and
    to a "X-BOOM" property; 500 to "POISON" whatever its form; 409 to a collection name with "/" in it. `down`: every
    request gets 500. `collapse`: a second object of a UID in one calendar replaces the first (kept once). `refuse`:
    paths whose PUT gets 500. `ctype`: the Content-Type of a GET of an object. DELETE of an object or of a collection."""

    HOME = "/dav/me/"

    def __init__(self):
        self.lock = threading.Lock()
        self.cols: dict[str, dict] = {}
        self.inm_500 = True
        self.down = False
        self.collapse = False
        self.methods: list[tuple[str, str]] = []
        self.etag = 0
        self.refuse: set[str] = set()  # paths whose PUT gets 500, whatever is sent
        self.ctype: str | None = None  # Content-Type of a GET of an object (None: none, as SmarterMail may do)
        self.query_all = False  # REPORT: every object of the collection, whatever the filter (MDaemon)
        self.refuse_url: str | None = None  # "any": 500 to a vCard with a URL; "params": to a URL with parameters
        self.drop: set[str] = set()  # UIDs whose PUT gets 201 and is not kept

    def add(self, slug, name, kind="card", comp=None, owner=ME, shared=False, items=None):
        path = self.HOME + slug + "/"
        self.cols[path] = {"name": name, "kind": kind, "comp": comp, "owner": owner, "shared": shared, "items": {}}
        for n, body in (items or {}).items():
            self.cols[path]["items"][n] = body.encode() if isinstance(body, str) else body
        return path

    def puts(self, path=None):
        return [p for m, p in self.methods if m == "PUT" and (path is None or p.startswith(path))]

    def by_name(self, name):
        return next(c["items"] for c in self.cols.values() if c["name"] == name)

    def names(self):
        return sorted(c["name"] for c in self.cols.values())

    # ---- protocol
    @staticmethod
    def _ms(rows):
        return httpx.Response(207, text='<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
                                        'xmlns:a="urn:ietf:params:xml:ns:carddav" xmlns:cs="http://calendarserver.org/ns/">'
                                        + "".join(rows) + "</d:multistatus>")

    @staticmethod
    def _row(href, props):
        return (f"<d:response><d:href>{quote(href)}</d:href><d:propstat><d:prop>{props}</d:prop>"
                "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")

    def _col_props(self, path):
        c = self.cols[path]
        kind = "<c:calendar/>" if c["kind"] == "cal" else "<a:addressbook/>"
        shared = "<cs:shared/>" if c["shared"] else ""
        comp = (f'<c:supported-calendar-component-set><c:comp name="{c["comp"] or "VEVENT"}"/>'
                "</c:supported-calendar-component-set>") if c["kind"] == "cal" else ""
        owner = f"<d:owner><d:href>{c['owner']}</d:href></d:owner>" if c["owner"] else ""
        return (f"<d:resourcetype><d:collection/>{kind}{shared}</d:resourcetype><d:displayname>{c['name']}"
                f"</d:displayname>{comp}{owner}")

    def _holder(self, kind, uid):
        """(collection path, name) of the object of this UID in the account, None if none."""
        for p, c in self.cols.items():
            if c["kind"] != kind:
                continue
            for n, body in c["items"].items():
                if _uid(body) == uid:
                    return p, n
        return None

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = unquote(req.url.path)
        with self.lock:
            self.methods.append((req.method, path))
            if self.down:
                return httpx.Response(500)
            if req.method == "PROPFIND":
                depth = req.headers.get("depth")
                if path == self.HOME:
                    rows = [self._row(self.HOME, "<d:resourcetype><d:collection/></d:resourcetype>"
                                      f"<c:calendar-home-set><d:href>{self.HOME}</d:href></c:calendar-home-set>"
                                      f"<a:addressbook-home-set><d:href>{self.HOME}</d:href></a:addressbook-home-set>"
                                      f"<d:current-user-principal><d:href>{ME}</d:href></d:current-user-principal>"
                                      f"<d:owner><d:href>{ME}</d:href></d:owner>")]
                    if depth == "1":
                        rows += [self._row(p, self._col_props(p)) for p in self.cols]
                    return self._ms(rows)
                if path in self.cols:
                    rows = [self._row(path, self._col_props(path))]
                    if depth == "1":
                        rows += [self._row(path + n, f'<d:resourcetype/><d:getetag>"{hash(b)}"</d:getetag>')
                                 for n, b in self.cols[path]["items"].items()]
                    return self._ms(rows)
                folder, _, name = path.rpartition("/")
                col = self.cols.get(folder + "/")
                if col and name in col["items"]:
                    return self._ms([self._row(path, f'<d:resourcetype/><d:getetag>"{hash(col["items"][name])}"'
                                                     "</d:getetag>")])
                return httpx.Response(404)
            if req.method in ("MKCOL", "MKCALENDAR"):
                if path in self.cols:
                    return httpx.Response(405)
                name = ET.fromstring(req.content).find(".//{DAV:}displayname").text
                if "/" in name:
                    return httpx.Response(409)  # SmarterMail: a path, its parent folder is not there
                comp = ET.fromstring(req.content).find(".//{urn:ietf:params:xml:ns:caldav}comp")
                self.cols[path] = {"name": name, "kind": "cal" if "caldav" in req.content.decode() else "card",
                                   "comp": comp.get("name") if comp is not None else None, "owner": ME,
                                   "shared": False, "items": {}}
                return httpx.Response(201)
            if req.method == "REPORT":
                col = self.cols.get(path)
                m = re.search(rb"text-match[^>]*>([^<]*)<", req.content)
                if col is None or m is None:
                    return httpx.Response(400)
                uid = m.group(1).decode()
                return self._ms([self._row(path + n, '<d:getetag>"1"</d:getetag>')
                                 for n, b in col["items"].items() if self.query_all or _uid(b) == uid])
            folder, _, name = path.rpartition("/")
            col = self.cols.get(folder + "/")
            if req.method == "DELETE":
                if path in self.cols:
                    del self.cols[path]
                    return httpx.Response(204)
                if col and name in col["items"]:
                    del col["items"][name]
                    return httpx.Response(204)
                return httpx.Response(404)
            if req.method == "GET":
                body = col["items"].get(name) if col else None
                if body is None:
                    return httpx.Response(404)
                return httpx.Response(200, content=body, headers={"Content-Type": self.ctype} if self.ctype else {})
            if req.method == "PUT":
                if col is None:
                    return httpx.Response(409)
                body = req.content
                if path in self.refuse:
                    return httpx.Response(500)
                if req.headers.get("if-none-match") == "*" and name in col["items"]:
                    return httpx.Response(500 if self.inm_500 else 412)
                if b"POISON" in body or b"VERSION:4.0" in body or b"X-BOOM" in body:
                    return httpx.Response(500)
                if self.refuse_url and re.search(rb"^URL[;:]" if self.refuse_url == "any" else rb"^URL;", body, re.M):
                    return httpx.Response(500)
                uid = _uid(body)
                if uid in self.drop:
                    return httpx.Response(201, headers={"ETag": '"dropped"'})
                held = self._holder(col["kind"], uid) if uid else None
                if held is not None and held != (folder + "/", name):
                    if self.collapse and held[0] == folder + "/":
                        del col["items"][held[1]]  # one object per UID: the last one written is kept
                    else:
                        return httpx.Response(500)
                col["items"][name] = body
                return httpx.Response(201, headers={"ETag": f'"{hash(body)}"'})
        return httpx.Response(405)


@pytest.fixture
def sm(monkeypatch):
    """Two fake SmarterMail servers: src.test (the source) and dst.test (the target); no wait before a retry."""
    from app.server import dav as sdav
    servers = {"src.test": FakeSmarterMail(), "dst.test": FakeSmarterMail()}
    real = sdav.httpx.Client

    def route(req):
        return servers[req.url.host](req)

    monkeypatch.setattr(sdav.httpx, "Client", lambda **kw: real(**{**kw, "transport": httpx.MockTransport(route)}))
    monkeypatch.setattr(cdav.DavSession, "ITEM_RETRY_DELAY", 0.01)
    return servers["src.test"], servers["dst.test"]


def account(client, kind="carddav"):
    sid = client.post("/api/servers", json={
        "name": f"dst-{kind}", "imap_host": "127.0.0.1", "imap_port": 1, "imap_security": "none",
        "verify_tls": False, "caldav_url": "https://dst.test/dav/me/", "carddav_url": "https://dst.test/dav/me/",
        "dav_enabled": True, "workers": 1, "tuning": "manual", "notes": ""}).json()["id"]
    r = client.post("/api/accounts", json={"server_id": sid, "email": "me@kmy.ch", "password": "pw",
                                           "options": {"mail": False}})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    r = client.put(f"/api/accounts/{aid}/source", json={
        "kind": kind, "config": {"url": "https://src.test/dav/me/", "login": "me@kmy.ch", "email": "me@kmy.ch"},
        "secrets": {"password": "pw"}})
    assert r.status_code == 200, r.text
    return aid


def run(client, aid, **kw):
    from app.server import runner
    r = client.post("/api/runs", json={"account_ids": [aid], **kw})
    assert r.status_code == 200, r.text
    body = r.json()
    jid = (body["jobs"] if isinstance(body, dict) else body)[0]["id"]
    runner.run_job(jid, "test:inproc")
    job = client.get(f"/api/jobs/{jid}").json()
    log = "\n".join(x["msg"] for x in client.get(f"/api/jobs/{jid}/log").json()["lines"])
    return job, client.get(f"/api/jobs/{jid}/report").json(), log


def items(aid):
    from app.server import db
    return {r["subject"]: dict(r) for r in db.q("SELECT subject, status, error, warnings, folder FROM items "
                                               "WHERE account_id=?", (aid,))}


# ------------------------------------------------------------------ unit
def test_collections_of_other_accounts_are_found_by_the_server_then_by_their_name():
    H = "https://s.test/dav/me/"
    rows = [(H + "a/", "Contacts", []), (H + "b/", "[compta@kmy.ch] - Contacts", []),
            (H + "c/", "[Public] - kmy.ch/Contacts", []), (H + "d/", "Agenda de Paul", []),
            (H + "e/", "Partagé", []), (H + "f/", "[me@kmy.ch] - Notes", [])]
    owners = {"principals": {"https://s.test" + ME},
              "collections": {H + "a/": {"owner": "https://s.test" + ME, "shared": False},
                              H + "d/": {"owner": "https://s.test/principals/paul/", "shared": False},
                              H + "e/": {"owner": None, "shared": True}}}
    got = cdav.foreign_collections(rows, owners, "me@kmy.ch")
    assert got[H + "b/"] == "shared from compta@kmy.ch: migrated with that mailbox"
    assert got[H + "c/"].startswith("public folder of kmy.ch")
    assert "owned by another account (https://s.test/principals/paul/)" in got[H + "d/"]
    assert "shared with this mailbox" in got[H + "e/"]
    assert H + "a/" not in got and H + "f/" not in got  # its own, whatever its name
    # owners the server gives in another form than its principal: not believed (nothing would be migrated)
    odd = {"principals": {"https://s.test" + ME},
           "collections": {u: {"owner": "/users/me", "shared": False} for u, _l, _c in rows}}
    assert set(cdav.foreign_collections(rows, odd, "me@kmy.ch")) == {H + "b/", H + "c/"}


def test_second_forms_of_a_vcard_and_an_event():
    card = ("BEGIN:VCARD\nVERSION:4.0\nUID:u1\nTEL;VALUE=uri;TYPE=cell:tel:+41791234567\n"
            "PHOTO:data:image/png;base64," + "A" * 400_000 + "\nitem1.EMAIL:a@b.test\nX-BOOM:" + "é" * 80
            + "\nEND:VCARD\n")
    (n1, t1, notes1), (n2, t2, notes2) = davfix.forms("carddav", card)
    assert (n1, n2) == ("normalized", "minimal")
    assert "VERSION:3.0\r\n" in t1 and "VERSION:4.0" not in t1 and "TEL;TYPE=cell:+41791234567\r\n" in t1
    assert "FN:a@b.test\r\n" in t1 and "N:a@b.test;;;;\r\n" in t1 and "\nEMAIL:a@b.test" in t1
    assert "PHOTO" not in t1 and any("photo of" in n for n in notes1) and "vCard 4.0 written as 3.0" in notes1
    assert all(len(ln.encode()) <= 75 for ln in t1.split("\r\n")) and t1.endswith("END:VCARD\r\n")
    assert "X-BOOM" in t1 and "X-BOOM" not in t2 and "left out: PHOTO, X-BOOM" in notes2
    assert davfix.object_uid(t2) == "u1"
    event = "BEGIN:VCALENDAR\nBEGIN:VTODO\nUID:t1\nCREATED:20240101T000000Z\nX-A:1\nEND:VTODO\nEND:VCALENDAR\n"
    (_n, e1, en1), (_m, e2, _en2) = davfix.forms("caldav", event)
    assert "DTSTAMP:20240101T000000Z\r\n" in e1 and "VERSION:2.0" in e1 and "PRODID" in e1 and "X-A" not in e2
    assert "DTSTAMP added" in en1
    assert davfix.forms("carddav", vcf("ok", "Fine")) == []  # already in its plainest form: nothing else to try


def test_breaker_trips_lets_a_probe_through_and_closes_on_a_success(monkeypatch):
    monkeypatch.setattr(cdav.Breaker, "LIMIT", 3)
    monkeypatch.setattr(cdav.Breaker, "PROBE", 4)
    b = cdav.Breaker()
    col = "https://d.test/c/"
    assert [b.failed(col, "HTTP 500") for _ in range(3)] == [False, False, True]
    held = [b.blocked(col) for _ in range(8)]
    assert sum(h is None for h in held) == 2 and all(h.startswith("not_sent: ") for h in held if h)  # 1 in 4 goes
    assert b.summary() == [(col, 6, "HTTP 500")] and b.summary() == []
    b.ok(col)
    assert b.blocked(col) is None and b.blocked("https://d.test/other/") is None


# ------------------------------------------------------------------ one session
def session_on(dst, kind="carddav"):
    cls = {"carddav": cdav.CardDavTarget, "caldav": cdav.CalDavTarget, "tasks": cdav.CalDavTasksTarget}[kind]
    s = cls({"url": "https://dst.test/dav/me/", "login": "me"}, {"password": "p"}).session()
    s.connect()
    return s


def put(s, col, uid, body):
    return s.put(col, OutgoingItem(CONTACTS, body.encode(), "<x>", name=f"{uid}.vcf"))


def test_a_500_the_server_gives_again_fails_the_item_in_a_few_requests(sm):
    _src, dst = sm
    col = "https://dst.test" + dst.add("b64=Q29udGFjdHM", "Contacts")
    s = session_on(dst)
    t0 = time.monotonic()
    with pytest.raises(ConnectorError) as e:
        put(s, col, "p1", vcf("p1", "POISON"))
    assert time.monotonic() - t0 < 2
    assert e.value.kind == ConnectorError.REJECTED and not e.value.retryable
    assert "refuses this contact (HTTP 500 again a moment later) while the collection answers" in str(e.value)
    assert len(dst.puts()) == 2  # once more a moment later, and no more (its plainer forms are the same text)
    with pytest.raises(ConnectorError) as e:
        put(s, col, "p2", vcf("p2", "POISON", "X-BOOM:1\r\n"))
    assert "(HTTP 500 again a moment later, and for its minimal form)" in str(e.value)
    assert len(dst.puts()) == 2 + 3
    with pytest.raises(ConnectorError) as e:
        put(s, col, "p3", vcf("p3", "POISON", "X-BOOM:1\r\n").replace("VERSION:3.0", "VERSION:4.0"))
    assert "(HTTP 500 again a moment later, and for its normalized and minimal form)" in str(e.value)
    assert len(dst.puts()) == 2 + 3 + 4


def test_a_500_from_a_server_in_trouble_stays_temporary(sm):
    _src, dst = sm
    col = "https://dst.test" + dst.add("b64=Q29udGFjdHM", "Contacts")
    s = session_on(dst)
    s._discovery()
    dst.down = True
    with pytest.raises(ConnectorError) as e:
        put(s, col, "c1", vcf("c1", "Someone"))
    assert e.value.retryable  # the runner retries as before


def test_a_uid_already_in_another_address_book_is_named(sm):
    _src, dst = sm
    col = "https://dst.test" + dst.add("b64=Q29udGFjdHM", "Contacts")
    dst.add("mccb-contacts-0123abcd", "Contacts", items={"c1.vcf": vcf("c1", "Old copy")})
    dst.add("perso", "Perso", items={"other-name.vcf": vcf("c2", "Elsewhere")})
    s = session_on(dst)
    with pytest.raises(ConnectorError) as e:
        put(s, col, "c1", vcf("c1", "Someone"))
    msg = str(e.value)
    assert "a contact with the UID 'c1' already exists in the address book 'Contacts' " \
           "(https://dst.test/dav/me/mccb-contacts-0123abcd/c1.vcf)" in msg
    assert "looks like a copy an earlier run made: check it, delete that address book in the webmail, then rerun" in msg
    with pytest.raises(ConnectorError) as e:  # another name: found by an addressbook-query
        put(s, col, "c2", vcf("c2", "Someone else"))
    assert "already exists in the address book 'Perso' (https://dst.test/dav/me/perso/other-name.vcf)" in str(e.value)
    assert not any(c["name"] == "Contacts" and "c1.vcf" in c["items"] and b"Someone" in c["items"]["c1.vcf"]
                   for c in dst.cols.values())


def test_a_server_answering_every_query_with_the_whole_collection_blames_no_card(sm):
    """MDaemon answers an addressbook-query with every card of the collection: the first one was named as holding
    the UID of every refused card. A query for a UID no object has shows it; nothing is named then."""
    _src, dst = sm
    dst.query_all = True
    col = "https://dst.test" + dst.add("b64=Q29udGFjdHM", "Contacts", items={"first.vcf": vcf("first", "First")})
    s = session_on(dst)
    for i in range(3):
        with pytest.raises(ConnectorError) as e:
            put(s, col, f"p{i}", vcf(f"p{i}", "POISON"))
        assert "first.vcf" not in str(e.value) and "already" not in str(e.value)
        assert "refuses this contact (HTTP 500 again a moment later) while the collection answers" in str(e.value)
    assert sum(m == "REPORT" for m, _p in dst.methods) == 1  # the collection is tried once, then not asked
    dst.query_all = False
    dst.add("perso", "Perso", items={"other-name.vcf": vcf("c2", "Elsewhere")})
    s = session_on(dst)
    with pytest.raises(ConnectorError) as e:  # a server that filters: the card is read before it is named
        put(s, col, "c2", vcf("c2", "Someone else"))
    assert "already exists in the address book 'Perso' (https://dst.test/dav/me/perso/other-name.vcf)" in str(e.value)
    assert ("GET", "/dav/me/perso/other-name.vcf") in dst.methods


def test_cards_with_a_website_refused_are_written_with_it_in_the_note_then_at_once(sm):
    """The target answers 500 to every vCard with a URL (seen on MDaemon): the URL goes into the note, and the
    next cards of the collection get that form at once (one PUT more, no wait, no other form tried)."""
    _src, dst = sm
    dst.refuse_url = "any"
    col = "https://dst.test" + dst.add("b64=Q29udGFjdHM", "Contacts")
    s = session_on(dst)
    put(s, col, "w1", vcf("w1", "Web One",
                          "NOTE:hello\r\nURL;PREF=1;TYPE=WORK:www.one.test\r\nPHOTO;ENCODING=b;TYPE=JPEG:AAAA\r\n"))
    assert "written in a URL-in-note form" in s.last_note and "URL moved into the note (1)" in s.last_note
    body = dst.cols[dst.HOME + "b64=Q29udGFjdHM/"]["items"]["w1.vcf"].decode()
    assert "NOTE:hello\\nwww.one.test\r\n" in body and "\nURL" not in body and "PHOTO" in body  # photo kept
    n = len(dst.methods)
    t0 = time.monotonic()
    for i in range(2, 6):
        put(s, col, f"w{i}", vcf(f"w{i}", f"Web {i}", f"URL;TYPE=HOME:https://w{i}.test/a,b\r\n"))
        assert "URL-in-note" in s.last_note
    assert time.monotonic() - t0 < 1  # no moment waited
    assert [m for m, _p in dst.methods[n:]].count("PUT") == 4 * 2  # as it is, then the form learnt: nothing else
    body = dst.cols[dst.HOME + "b64=Q29udGFjdHM/"]["items"]["w5.vcf"].decode()
    assert "NOTE:https://w5.test/a\\,b\r\n" in body
    put(s, col, "plain", vcf("plain", "No website"))  # a card the server takes as it is: as it is
    assert s.last_note == "" and "URL" not in dst.cols[dst.HOME + "b64=Q29udGFjdHM/"]["items"]["plain.vcf"].decode()
    dst.refuse_url = "params"  # a server refusing only the parameters: the URL itself is kept
    put(s, dst_col := "https://dst.test" + dst.add("other", "Other"), "p1",
        vcf("p1", "Params", "URL;TYPE=WORK:www.p.test\r\n"))
    assert "plain-URL form (URL parameters left out)" in s.last_note
    assert "\r\nURL:www.p.test\r\n" in dst.cols[dst.HOME + "other/"]["items"]["p1.vcf"].decode(), dst_col


def test_contacts_with_a_website_end_to_end(client, sm):
    src, dst = sm
    dst.refuse_url = "any"
    src.add("b64=Q29udGFjdHM", "Contacts", items={
        **{f"w{i}.vcf": vcf(f"w{i}", f"Web {i}", f"URL;TYPE=WORK:www.w{i}.test\r\n") for i in range(30)},
        **{f"n{i}.vcf": vcf(f"n{i}", f"Plain {i}") for i in range(5)}})
    dst.add("b64=Q29udGFjdHM", "Contacts")
    aid = account(client)
    job, rep, log = run(client, aid)
    assert job["status"] == "done", log
    got = dst.by_name("Contacts")
    assert len(got) == 35 and all(b"www.w" in got[f"w{i}.vcf"] for i in range(30))
    assert "already" not in log and "not sent" not in log


def test_an_event_sent_but_dropped_by_the_server_is_sent_again_on_the_next_run(client, sm):
    src, dst = sm
    src.add("b64=Q2FsZW5kYXI", "Calendrier", kind="cal", items={
        "a.ics": ics("ev-a", "Kept"), "b.ics": ics("ev-b", "Web Meeting: test")})
    dst.add("b64=Q2FsZW5kYXI", "Calendrier", kind="cal")
    dst.drop = {"ev-b"}  # 201 to its PUT, not kept
    aid = account(client, "caldav")
    job, rep, log = run(client, aid)
    assert "1 present < 2 sent" in log and "missing on the target: 'Web Meeting: test'" in log, log
    assert items(aid)["Web Meeting: test"]["status"] == "ok"
    dst.drop = set()
    job, rep, log = run(client, aid)
    assert "1 item(s) recorded as sent are not on the target (dropped by the server or deleted there), sent again: " \
           "'ev-b.ics'" in log, log
    assert job["status"] == "done", log
    assert sorted(dst.by_name("Calendrier")) == ["ev-a.ics", "ev-b.ics"]
    job, rep, log = run(client, aid)  # nothing missing any more: nothing sent again
    assert "sent again" not in log and job["status"] == "done", log


def test_500_to_if_none_match_over_an_existing_resource_writes_it_over(sm):
    _src, dst = sm
    col = "https://dst.test" + dst.add("b64=Q29udGFjdHM", "Contacts", items={"c1.vcf": vcf("c1", "Before")})
    s = session_on(dst)
    assert put(s, col, "c1", vcf("c1", "After")) == "c1.vcf"
    assert b"After" in dst.by_name("Contacts")["c1.vcf"] and s.last_note == ""
    assert s.written_version(col, "c1.vcf").startswith("!")  # written over: its owner is not proven


def test_a_vcard_4_is_written_as_3_with_a_note(sm):
    _src, dst = sm
    col = "https://dst.test" + dst.add("b64=Q29udGFjdHM", "Contacts")
    s = session_on(dst)
    put(s, col, "v4", vcf("v4", "New format").replace("VERSION:3.0", "VERSION:4.0"))
    assert b"VERSION:3.0" in dst.by_name("Contacts")["v4.vcf"]
    assert s.last_note.startswith("the server answered HTTP 500 to this contact as it is: written in a normalized "
                                  "form (vCard 4.0 written as 3.0)")


def test_a_name_with_a_slash_is_created_flat(sm):
    _src, dst = sm
    dst.add("b64=VGFza3M", "Tâches", kind="cal", comp="VTODO")
    s = session_on(dst, "tasks")
    url = "https://dst.test/dav/me/mccb-taches-24me-048e4f82/"
    s.ensure_folder(url, label="Tâches/24me")
    assert "Tâches - 24me" in dst.names() and s.renamed[url] == "Tâches - 24me"
    # MKCALENDAR, then an extended MKCOL, under the name asked; then MKCALENDAR under the flat name
    assert [m for m, _p in dst.methods if m.startswith("MK")] == ["MKCALENDAR", "MKCOL", "MKCALENDAR"]


# ------------------------------------------------------------------ end to end
def test_smartermail_contacts_end_to_end(client, sm):
    src, dst = sm
    src.add("b64=Q29udGFjdHM", "Contacts", items={
        "c0.vcf": vcf("c0", "Plain"), "c1.vcf": vcf("c1", "Dup UID"),
        "c2.vcf": vcf("c2", "Version four").replace("VERSION:3.0", "VERSION:4.0"),
        "c3.vcf": vcf("c3", "POISON"), "c4.vcf": vcf("c4", "Boom", "X-BOOM:1\r\n")})
    src.add("sugg", "Contacts suggérés")  # empty
    src.add("compta", "[compta@kmy.ch] - Contacts", owner="/principals/compta/", items={"s.vcf": vcf("s1", "Shared")})
    src.add("public", "[Public] - kmy.ch/Contacts", owner=None, items={"p.vcf": vcf("p1", "Public")})
    src.add("sub", "Équipe/Projets", items={"e.vcf": vcf("e1", "Team")})
    dst.add("b64=Q29udGFjdHM", "Contacts")
    dst.add("mccb-contacts-0123abcd", "Contacts", items={"c1.vcf": vcf("c1", "Dup UID")})  # an earlier run's twin
    aid = account(client)
    t0 = time.monotonic()
    job, rep, log = run(client, aid)
    assert time.monotonic() - t0 < 30, log
    assert job["status"] == "done_with_errors", log
    # shared and public collections: left to their owner, said once, in the report
    assert "'[compta@kmy.ch] - Contacts' (contacts) not migrated: shared from compta@kmy.ch: migrated with that " \
           "mailbox" in log
    assert "not migrated: public folder of kmy.ch" in log
    assert {f["reason"][:6] for f in rep["folders_skipped"]} == {"shared", "public"}
    assert not any(b"Shared" in b or b"Public" in b for c in dst.cols.values() for b in c["items"].values())
    # the empty one: not created, not checked
    assert "Contacts suggérés" not in dst.names() and "None present" not in log and "present <" not in log, log
    assert "Folder 'Contacts suggérés': nothing to copy" in log
    # a name with "/": created flat
    assert set(dst.by_name("Équipe - Projets")) == {"e1.vcf"}
    got = items(aid)
    assert got["Plain"]["status"] == "ok"
    assert got["Dup UID"]["status"] == "error" and "already exists in the address book 'Contacts'" in got["Dup UID"]["error"]
    assert got["POISON"]["status"] == "error" and "refuses this contact" in got["POISON"]["error"]
    assert got["Version four"]["status"] == "ok" and "normalized form" in got["Version four"]["warnings"]
    assert got["Boom"]["status"] == "ok" and "minimal form" in got["Boom"]["warnings"]
    assert "Finished: done_with_errors (2 error(s): 2 item error(s))" in log, log


def test_a_collection_refusing_everything_trips_the_breaker(client, sm, monkeypatch):
    monkeypatch.setattr(cdav.Breaker, "LIMIT", 3)
    src, dst = sm
    src.add("b64=Q29udGFjdHM", "Contacts", items={f"c{i}.vcf": vcf(f"c{i}", f"POISON {i}") for i in range(10)})
    col = dst.add("b64=Q29udGFjdHM", "Contacts")
    aid = account(client)
    job, rep, log = run(client, aid)
    assert job["status"] == "done_with_errors", log
    assert len(dst.puts(col)) == 3 * 2  # 3 items (each twice: as it is, again a moment later), the 7 others not sent
    assert log.count("APPEND failure") == 3 and log.count("not sent: the collection refused every item") == 1, log
    assert "7 item(s) for https://dst.test/dav/me/b64=Q29udGFjdHM/ not sent" in log
    errors = [r for r in items(aid).values() if r["status"] == "error"]
    assert len(errors) == 10 and sum(r["error"].startswith("[perm] not sent:") for r in errors) == 7


def test_two_events_of_one_uid_kept_once_by_the_server_are_not_a_verification_problem(client, sm):
    src, dst = sm
    dst.collapse = True
    # a UID that is not safe as a file name: each object keeps its own resource name, the server keeps one
    src.add("b64=Q2FsZW5kYXI", "Calendrier", kind="cal", items={
        "a.ics": ics("weird uid/1", "Master"), "b.ics": ics("weird uid/1", "Exception"), "c.ics": ics("ok-1", "Other")})
    dst.add("b64=Q2FsZW5kYXI", "Calendrier", kind="cal")
    aid = account(client, "caldav")
    job, rep, log = run(client, aid)
    assert job["status"] == "done", log
    assert len(dst.by_name("Calendrier")) == 2
    assert "2 present for 3 sent: 1 object(s) share their UID with another one and the server keeps one per UID " \
           "(UID 'weird uid/1'" in log, log
    assert "present <" not in log


# ------------------------------------------------------------------ Radicale
@needs_dav
def test_radicale_empty_and_shared_collections(client):
    from app.server.dav import DavClient
    from tests.server.test_dav_default_collections import account as radicale_account
    from tests.server.test_dav_default_collections import home, run as radicale_run, tree
    from tests.svcusers import create_user
    src, dst = create_user(prefix="smsrc"), create_user(prefix="smdst")
    t = DavClient(dst, "pw", False)
    try:
        t.ensure_collection("carddav", home(dst) + "default-book/", "VEVENT", "Contacts")
    finally:
        t.close()
    c = DavClient(src, "pw", False)
    try:
        book = c.ensure_collection("carddav", home(src) + "book/", "VEVENT", "Contacts")
        c.ensure_collection("carddav", home(src) + "empty/", "VEVENT", "Vide")
        boss = c.ensure_collection("carddav", home(src) + "boss/", "VEVENT", "[boss@example.test] - Contacts")
        c.put(book, "c0.vcf", vcf("c0", "Mine"), "carddav")
        c.put(boss, "b0.vcf", vcf("b0", "Theirs"), "carddav")
    finally:
        c.close()
    aid = radicale_account(client, src, dst, "carddav")
    job, rep, log = radicale_run(client, aid)
    assert job["status"] == "done", log
    got = tree(dst, "carddav")
    assert list(got) == ["Contacts"] and list(got["Contacts"]) == ["c0.vcf"], got
    assert "not migrated: shared from boss@example.test" in log and "'Vide': nothing to copy" in log
    assert "present <" not in log and "None present" not in log
