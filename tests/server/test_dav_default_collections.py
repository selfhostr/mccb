"""Which collection of a CalDAV / CardDAV target a source folder goes to: the default address book, calendar or task
list of the source to the one the target already has (never a second "Contacts"), the others to the one of the same
name (whatever the case), the lists a server keeps as address books (SmarterMail "Allowed Senders") to the target's
own list under its local name, else a new collection. A plan that changed since an earlier run (the second
"Contacts" an earlier version made) copies the items into the right collection, never a third time, and leaves the
earlier copies where they are.

Unit tests without services, end to end against Radicale (TEST_DAV_URL, docker-compose.test.yml)."""
import os
from urllib.parse import unquote, urljoin

import httpx
import pytest

from app.connectors import dav as cdav
from app.connectors.base import CALENDAR, CONTACTS, TASKS, ConnectorError, SourceFolder
from app.connectors.dav import Collection, choose_collection, default_index

DAV_URL = os.environ.get("TEST_DAV_URL")
needs_dav = pytest.mark.skipif(not DAV_URL, reason="TEST_DAV_URL not set")
H = "https://dav.test/u/"


def coll(path, label, writable=True):
    return Collection(H + path, label, writable)


# ------------------------------------------------------------------ unit: the default one of the source
def test_default_index_by_name_then_first():
    assert default_index(CONTACTS, ["Allowed Senders", "Contacts", "Blocked Senders"]) == 1
    assert default_index(CONTACTS, ["Allowed Senders"]) == 0  # the only one
    assert default_index(CALENDAR, ["Travail", "Calendrier"]) == 1
    assert default_index(CALENDAR, ["Projets", "Kalender"]) == 1
    assert default_index(CALENDAR, ["Projets", "Travail"]) == 0  # no default name: the first
    assert default_index(TASKS, ["Courses", "Tâches"]) == 1
    assert default_index(CONTACTS, ["  CONTACTS "]) == 0
    assert default_index(CONTACTS, []) is None


def test_dav_source_marks_its_default_collection_by_name(monkeypatch):
    class Fake:
        def collections(self, kind, *a):
            if kind == "carddav":
                return [(H + "allowed/", "Allowed Senders", []), (H + "book/", "Contacts", []),
                        (H + "work/", "Work", [])]
            return [(H + "proj/", "Projets", ["VEVENT"]), (H + "cal/", "Calendrier", ["VEVENT", "VTODO"]),
                    (H + "todo/", "Courses", ["VTODO"])]

        def source_collections(self, kind, *a):
            return self.collections(kind), {}

    for kind, cls in (("carddav", cdav.CardDavSource), ("caldav", cdav.CalDavSource)):
        src = cls({"url": H, "login": "u"}, {"password": "p"})
        src.client = Fake()
        got = {(f.kind, f.path[0]): f.special for f in src.folders()}
        if kind == "carddav":
            assert got == {(CONTACTS, "Allowed Senders"): None, (CONTACTS, "Contacts"): "default",
                           (CONTACTS, "Work"): None}
        else:  # by name for the calendars; no task list has a default name: the first one listed
            assert got == {(CALENDAR, "Projets"): None, (CALENDAR, "Calendrier"): "default",
                           (TASKS, "Calendrier"): "default", (TASKS, "Courses"): None}


# ------------------------------------------------------------------ unit: the collection of the target
SMARTERMAIL_FR = [coll("a1/", "Expéditeurs autorisés"), coll("a2/", "Contacts"), coll("a3/", "Expéditeurs bloqués")]


def test_default_folder_goes_to_the_default_collection_not_the_first_listed():
    hit, why = choose_collection(CONTACTS, "Contacts", True, SMARTERMAIL_FR, H + "a1/")
    assert hit.url == H + "a2/" and why == "same name"
    # another language at the source: the collection with a default name, not the first the discovery found
    hit, why = choose_collection(CONTACTS, "Kontakte", True, SMARTERMAIL_FR, H + "a1/")
    assert hit.url == H + "a2/" and why == "default collection"
    # no default name at all on the target: the one the discovery found
    hit, why = choose_collection(CALENDAR, "Calendar", True, [coll("x/", "Perso"), coll("y/", "Work")], H + "x/")
    assert hit.url == H + "x/" and why == "first collection of the server"


def test_names_match_whatever_the_case_and_lists_by_their_role():
    colls = SMARTERMAIL_FR + [coll("w/", "work")]
    assert choose_collection(CONTACTS, "WORK ", False, colls, H + "a1/")[0].url == H + "w/"
    assert choose_collection(CONTACTS, "Allowed Senders", False, colls, H + "a1/")[0].url == H + "a1/"
    assert choose_collection(CONTACTS, "Blocked Senders", False, colls, H + "a1/")[0].url == H + "a3/"
    hit, why = choose_collection(CONTACTS, "Family", False, colls, H + "a1/")  # nothing for it: created
    assert hit is None and why


def test_a_duplicate_made_earlier_is_never_chosen():
    # a second "Contacts" mccb created (its own slug) next to the server's own
    made = coll(cdav.collection_slug("carddav", "VEVENT", "Contacts"), "Contacts")
    assert cdav.made_by_mccb(made.url) and not cdav.made_by_mccb(H + "a2/")
    colls = [made] + SMARTERMAIL_FR
    hit, _ = choose_collection(CONTACTS, "Contacts", True, colls, made.url, previous={cdav.url_key(made.url)})
    assert hit.url == H + "a2/"
    # the same at an URL of the server: the one holding the items of earlier runs is the extra one
    dup = coll("dup/", "Contacts")
    hit, _ = choose_collection(CONTACTS, "Contacts", True, [dup] + SMARTERMAIL_FR, dup.url,
                               previous={cdav.url_key(dup.url)})
    assert hit.url == H + "a2/"
    # "Allowed Senders" an earlier run created next to the server's "Expéditeurs autorisés"
    mine = coll(cdav.collection_slug("carddav", "VEVENT", "Allowed Senders"), "Allowed Senders")
    hit, _ = choose_collection(CONTACTS, "Allowed Senders", False, SMARTERMAIL_FR + [mine], H + "a1/",
                               previous={cdav.url_key(mine.url)})
    assert hit.url == H + "a1/"
    # alone, a collection mccb created is the one of its folder (every run after the first one)
    work = coll(cdav.collection_slug("carddav", "VEVENT", "Work"), "Work")
    assert choose_collection(CONTACTS, "Work", False, SMARTERMAIL_FR + [work], H + "a1/",
                             previous={cdav.url_key(work.url)})[0] is work


def test_earlier_choices_are_kept():
    # the collection earlier runs used for the default folder stays theirs (no copy of everything elsewhere)
    colls = [coll("perso/", "Perso"), coll("cal/", "Calendar")]
    hit, _ = choose_collection(CALENDAR, "Calendar", True, colls, H + "perso/", previous={H + "perso/"})
    assert hit.url == H + "perso/"
    # the choice of the last run is kept while the collection is there: no back and forth between two "Contacts"
    dup = coll("dup/", "Contacts")
    colls = [dup] + SMARTERMAIL_FR
    hit, why = choose_collection(CONTACTS, "Contacts", True, colls, dup.url, previous={H + "a2/"}, pinned=H + "a2")
    assert hit.url == H + "a2/" and why == "chosen by an earlier run"
    hit, _ = choose_collection(CONTACTS, "Contacts", True, colls, dup.url, pinned=H + "gone/")
    assert hit.url in (H + "a2/", dup.url)


def test_listing_reads_write_privileges():
    def handler(req: httpx.Request) -> httpx.Response:
        def row(href, name, privs):
            p = "".join(f"<d:privilege><d:{x}/></d:privilege>" for x in privs or ())
            ps = f"<d:current-user-privilege-set>{p}</d:current-user-privilege-set>" if privs is not None else ""
            return (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop><d:resourcetype><d:collection/>"
                    f"<card:addressbook/></d:resourcetype><d:displayname>{name}</d:displayname>{ps}</d:prop>"
                    "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
        body = ('<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
                '<d:response><d:href>/u/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>'
                "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                + row("/u/gal/", "Contacts", ["read"]) + row("/u/mine/", "Contacts", ["read", "write"])
                + row("/u/old/", "Old", None) + "</d:multistatus>")
        return httpx.Response(207, text=body)

    from app.server.dav import DavClient
    c = DavClient("u", "p", transport=httpx.MockTransport(handler))
    rows = c.listing("carddav", None, H, privileges=True)
    assert [(u.rsplit("/", 2)[-2], w) for u, _l, _c, w in rows] == [("gal", False), ("mine", True), ("old", True)]
    # the plan never takes a read-only one (a global address list named "Contacts")
    target = cdav.CardDavTarget({"url": H, "login": "u"}, {"password": "p"})
    s = target.session()
    s.client = c
    s._discovery = lambda: (H + "gal/", H)
    plan = s.plan_folders([SourceFolder(["Contacts"], CONTACTS, special="default")])
    assert plan["Contacts"].name == H + "mine/" and plan["Contacts"].label is None


# ------------------------------------------------------------------ end to end (Radicale)
def vcf(uid, fn):
    return f"BEGIN:VCARD\r\nVERSION:3.0\r\nUID:{uid}\r\nFN:{fn}\r\nN:{fn};;;;\r\nEND:VCARD\r\n"


def ics(uid, summary):
    return (f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//t//EN\r\nBEGIN:VEVENT\r\nUID:{uid}\r\n"
            "DTSTAMP:20240301T000000Z\r\nDTSTART:20240305T100000Z\r\nDTEND:20240305T110000Z\r\n"
            f"SUMMARY:{summary}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def client_of(user):
    from app.server.dav import DavClient
    return DavClient(user, "pw", False)


def home(user):
    return urljoin(DAV_URL, f"/{user}/")


def tree(user, kind):
    """{display name: {resource name: text}} of every collection of this kind (two of one name: an error)."""
    c = client_of(user)
    try:
        out = {}
        for url, label, _comps in c.collections(kind, home(user), user, ""):
            key = label if label not in out else f"{label} #2 ({url.rsplit('/', 2)[-2]})"
            out[key] = {n: c.get(url, n) for n in sorted(c.names(url))}
        return out
    finally:
        c.close()


def targets(aid):
    from app.server import db
    return {unquote(r["target"]) for r in db.q("SELECT target FROM items WHERE account_id=?", (aid,))}


def account(client, src, dst, kind):
    sid = client.post("/api/servers", json={
        "name": f"dst-{kind}-{dst}", "imap_host": "127.0.0.1", "imap_port": 1, "imap_security": "none",
        "verify_tls": False, "caldav_url": DAV_URL, "carddav_url": DAV_URL, "dav_enabled": True, "workers": 1,
        "tuning": "manual", "notes": ""}).json()["id"]
    r = client.post("/api/accounts", json={"server_id": sid, "email": dst, "password": "pw", "options": {"mail": False}})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    r = client.put(f"/api/accounts/{aid}/source", json={
        "kind": kind, "config": {"url": home(src), "login": src, "verify_tls": False}, "secrets": {"password": "pw"}})
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


@pytest.fixture
def users():
    from tests.svcusers import create_user
    return create_user(prefix="dfsrc"), create_user(prefix="dfdst")


@needs_dav
def test_contacts_go_to_the_existing_address_books(client, users):
    src, dst = users
    c = client_of(src)
    try:  # the source: its default book, a SmarterMail list, one of its own
        book = c.ensure_collection("carddav", home(src) + "book/", "VEVENT", "Contacts")
        allowed = c.ensure_collection("carddav", home(src) + "allowed/", "VEVENT", "Allowed Senders")
        work = c.ensure_collection("carddav", home(src) + "work/", "VEVENT", "Work")
        fam = c.ensure_collection("carddav", home(src) + "fam/", "VEVENT", "Famille")
        for i in range(3):
            c.put(book, f"c{i}.vcf", vcf(f"c{i}", f"Contact {i}"), "carddav")
        c.put(allowed, "a0.vcf", vcf("a0", "sender@example.test"), "carddav")
        c.put(work, "w0.vcf", vcf("w0", "Colleague"), "carddav")
        c.put(fam, "f0.vcf", vcf("f0", "Cousin"), "carddav")
    finally:
        c.close()
    t = client_of(dst)
    try:  # the target, as a French SmarterMail account: its own book with a contact of the user, its lists
        mine = t.ensure_collection("carddav", home(dst) + "default-book/", "VEVENT", "Contacts")
        t.ensure_collection("carddav", home(dst) + "allowed-list/", "VEVENT", "Expéditeurs autorisés")
        t.ensure_collection("carddav", home(dst) + "blocked-list/", "VEVENT", "Expéditeurs bloqués")
        t.ensure_collection("carddav", home(dst) + "pro/", "VEVENT", "WORK")
        t.put(mine, "user-1.vcf", vcf("user-1", "Made on the target"), "carddav")
    finally:
        t.close()
    aid = account(client, src, dst, "carddav")
    job, rep, log = run(client, aid)
    assert job["status"] == "done", log
    got = tree(dst, "carddav")
    assert sorted(got) == ["Contacts", "Expéditeurs autorisés", "Expéditeurs bloqués", "Famille", "WORK"], got
    assert sorted(got["Contacts"]) == ["c0.vcf", "c1.vcf", "c2.vcf", "user-1.vcf"]
    assert "Made on the target" in got["Contacts"]["user-1.vcf"]
    assert sorted(got["Expéditeurs autorisés"]) == ["a0.vcf"] and sorted(got["WORK"]) == ["w0.vcf"]
    assert sorted(got["Famille"]) == ["f0.vcf"] and got["Expéditeurs bloqués"] == {}
    assert "'Contacts' -> existing 'Contacts'" in log and "'Famille' -> new collection 'Famille'" in log
    # again: nothing created, nothing twice
    job, rep, log = run(client, aid)
    assert job["status"] == "done", log
    assert tree(dst, "carddav") == got and "collection_changed" not in log


@needs_dav
def test_calendar_goes_to_the_existing_calendar_and_mirror_keeps_it(client, users):
    src, dst = users
    c = client_of(src)
    try:
        cal = c.ensure_collection("caldav", home(src) + "cal/", "VEVENT", "Calendrier")
        proj = c.ensure_collection("caldav", home(src) + "proj/", "VEVENT", "Projets")
        for i in range(2):
            c.put(cal, f"e{i}.ics", ics(f"e{i}", f"Event {i}"), "caldav")
        c.put(proj, "p0.ics", ics("p0", "Project 0"), "caldav")
    finally:
        c.close()
    t = client_of(dst)
    try:
        t.ensure_collection("caldav", home(dst) + "default-cal/", "VEVENT", "Calendar")
    finally:
        t.close()
    aid = account(client, src, dst, "caldav")
    job, rep, log = run(client, aid)
    assert job["status"] == "done", log
    got = tree(dst, "caldav")
    assert sorted(got) == ["Calendar", "Projets"], got
    assert sorted(got["Calendar"]) == ["e0.ics", "e1.ics"] and sorted(got["Projets"]) == ["p0.ics"]
    # mirror: dry run, then a pass that adds to the same calendar
    assert client.put(f"/api/accounts/{aid}", json={"mirror": True}).status_code == 200
    c = client_of(src)
    try:
        c.put(cal, "e9.ics", ics("e9", "Event 9"), "caldav")
    finally:
        c.close()
    acc = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror"}).json()["accounts"][0]
    assert acc["ready"] is True, acc
    assert acc["to_add"]["calendar"] == 1 and sum(acc["to_delete"].values()) == 0, acc
    job, rep, log = run(client, aid, mode="mirror", dry_run=False)
    assert job["status"] == "done", log
    got = tree(dst, "caldav")
    assert sorted(got) == ["Calendar", "Projets"] and sorted(got["Calendar"]) == ["e0.ics", "e1.ics", "e9.ics"]


@needs_dav
def test_a_second_contacts_of_an_earlier_run_is_left_and_the_items_go_to_the_right_one(client, users):
    """As after an earlier version: the contacts in a second "Contacts", the items rows saying so. The next run
    copies them into the account's own "Contacts" (one already there left as it is), names the other one in the
    log and the report, deletes nothing; the runs after it change nothing."""
    from app.server import db
    src, dst = users
    c = client_of(src)
    try:
        book = c.ensure_collection("carddav", home(src) + "book/", "VEVENT", "Contacts")
        for i in range(3):
            c.put(book, f"c{i}.vcf", vcf(f"c{i}", f"Contact {i}"), "carddav")
    finally:
        c.close()
    t = client_of(dst)
    try:
        dup = t.ensure_collection("carddav", home(dst) + "dup-contacts/", "VEVENT", "Contacts")
    finally:
        t.close()
    aid = account(client, src, dst, "carddav")
    job, rep, log = run(client, aid)  # the only "Contacts" then: everything there
    assert job["status"] == "done", log
    assert targets(aid) == {unquote(dup)}
    db.ex("DELETE FROM kv WHERE key LIKE 'dav-collections:%'")  # an earlier version kept no choice
    t = client_of(dst)
    try:  # the account's own book, as the server has it, with one of the contacts already in it
        own = t.ensure_collection("carddav", home(dst) + "own-contacts/", "VEVENT", "Contacts")
        t.put(own, "c1.vcf", vcf("c1", "Contact 1 kept as it is"), "carddav")
    finally:
        t.close()
    job, rep, log = run(client, aid)
    assert job["status"] == "done", log
    got = tree(dst, "carddav")
    assert len(got) == 2, got  # no third address book
    own_items = next(v for k, v in got.items() if "c1.vcf" in v and "kept as it is" in v["c1.vcf"])
    assert sorted(own_items) == ["c0.vcf", "c1.vcf", "c2.vcf"]
    assert sum(len(v) for v in got.values()) == 6  # the earlier copies left where they are
    assert "collection_changed: 3 item(s) of 'Contacts'" in log and "dup-contacts" in log, log
    [moved] = rep["dav_moved"]
    assert (unquote(moved["from"]), moved["from_name"], unquote(moved["to"]), moved["items"]) == (
        unquote(dup), "Contacts", unquote(own), 3)
    assert targets(aid) == {unquote(own)}
    # again, and in delta: nothing moves back, nothing twice
    for kw in ({}, {"mode": "delta"}):
        job, rep, log = run(client, aid, **kw)
        assert job["status"] == "done", log
        assert tree(dst, "carddav") == got and "collection_changed" not in log
    # mirror dry run: our items in the collection they are now in, nothing to delete
    assert client.put(f"/api/accounts/{aid}", json={"mirror": True}).status_code == 200
    acc = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror"}).json()["accounts"][0]
    assert acc["ready"] is True and sum(acc["to_delete"].values()) == 0 and sum(acc["to_add"].values()) == 0, acc


@needs_dav
def test_a_name_the_target_has_is_never_created_a_second_time(users):
    _src, dst = users
    t = client_of(dst)
    try:
        t.ensure_collection("carddav", home(dst) + "default-book/", "VEVENT", "Contacts")
    finally:
        t.close()
    target = cdav.CardDavTarget({"url": home(dst), "login": dst, "verify_tls": False}, {"password": "pw"})
    s = target.session()
    s.connect()
    try:
        with pytest.raises(ConnectorError) as e:
            s.ensure_folder(home(dst) + "other/", label="contacts")
        assert e.value.kind == ConnectorError.REJECTED and "already has a collection named 'Contacts'" in str(e.value)
    finally:
        s.close()
    assert sorted(tree(dst, "carddav")) == ["Contacts"]


def test_a_listed_collection_is_never_created_over_itself():
    """A server whose home lists its address book under another path (it is not a child of the home), and that
    does not open it with a PROPFIND of its own: the plan sends the contacts there; ensure_folder used to find it
    missing, its parent not listing it, and made a MKCOL named "Contacts" there (a second "Contacts" on such a
    server). Now: never created, an explicit error instead."""
    methods = []

    def handler(req: httpx.Request) -> httpx.Response:
        methods.append((req.method, req.url.path))
        if req.method == "PROPFIND" and req.url.path == "/u/":
            rows = ['<d:response><d:href>/u/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/>'
                    '</d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>']
            if req.headers.get("depth") == "1":
                rows.append('<d:response><d:href>/books/u/main/</d:href><d:propstat><d:prop><d:resourcetype>'
                            '<d:collection/><card:addressbook/></d:resourcetype><d:displayname>Contacts'
                            '</d:displayname></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
            return httpx.Response(207, text='<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
                                            + "".join(rows) + "</d:multistatus>")
        if req.method == "MKCOL":
            return httpx.Response(201)
        return httpx.Response(404)

    from app.server.dav import DavClient
    target = cdav.CardDavTarget({"url": H, "login": "u"}, {"password": "p"})
    s = target.session()
    s.client = DavClient("u", "p", transport=httpx.MockTransport(handler))
    plan = s.plan_folders([SourceFolder(["Contacts"], CONTACTS, special="default")])
    url = plan["Contacts"].name
    assert url == "https://dav.test/books/u/main/"
    other = target.session()  # the runner's own session, as in _run_phase
    other.client = s.client
    with pytest.raises(ConnectorError) as e:
        other.ensure_folder(url)
    assert e.value.kind == ConnectorError.REJECTED and "not created again" in str(e.value)
    assert not [m for m in methods if m[0] == "MKCOL"]
