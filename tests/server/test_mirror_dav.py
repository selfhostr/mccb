"""Mirror mode CalDAV / CardDAV to DAV against Radicale (TEST_DAV_URL): events, tasks and contacts added, updated by
etag and deleted (a copy kept first), native content of the target never touched, the source only read."""
import os
from urllib.parse import urljoin

import pytest

from app.server.dav import DavClient
from tests.svcusers import create_user

DAV_URL = os.environ.get("TEST_DAV_URL")
needs_dav = pytest.mark.skipif(not DAV_URL, reason="TEST_DAV_URL not set")


def ics(uid, summary, comp="VEVENT"):
    when = "DTSTART:20240305T100000Z\r\nDTEND:20240305T110000Z\r\n" if comp == "VEVENT" else "DUE:20240305T100000Z\r\n"
    return (f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//t//EN\r\nBEGIN:{comp}\r\nUID:{uid}\r\n"
            f"DTSTAMP:20240301T000000Z\r\n{when}SUMMARY:{summary}\r\nEND:{comp}\r\nEND:VCALENDAR\r\n")


def vcf(uid, fn):
    return f"BEGIN:VCARD\r\nVERSION:3.0\r\nUID:{uid}\r\nFN:{fn}\r\nN:{fn};;;;\r\nEND:VCARD\r\n"


def client_of(user):
    return DavClient(user, "pw", False)


def home(user):
    return urljoin(DAV_URL, f"/{user}/")


def contents(user, kind):
    """{collection url: {name: text}} of every collection of this kind."""
    c = client_of(user)
    try:
        out = {}
        for url, _label, _comps in c.collections(kind, home(user), user, ""):
            out[url] = {n: c.get(url, n) for n in sorted(c.names(url))}
        return out
    finally:
        c.close()


def etags(user, kind):
    c = client_of(user)
    try:
        return {url: c.etags(url) for url, _l, _c in c.collections(kind, home(user), user, "")}
    finally:
        c.close()


@pytest.fixture
def dav(client):
    src, dst = create_user(prefix="dsrc"), create_user(prefix="ddst")
    c = client_of(src)
    try:
        cal = c.ensure_collection("caldav", home(src) + "work/", "VEVENT", "Work")
        todo = c.ensure_collection("caldav", home(src) + "todo/", "VTODO", "Todo")
        book = c.ensure_collection("carddav", home(src) + "book/", "VEVENT", "Contacts")
        for i in range(5):
            c.put(cal, f"e{i}.ics", ics(f"e{i}", f"Event {i}"), "caldav")
            c.put(book, f"c{i}.vcf", vcf(f"c{i}", f"Contact {i}"), "carddav")
        for i in range(2):
            c.put(todo, f"t{i}.ics", ics(f"t{i}", f"Task {i}", "VTODO"), "caldav")
    finally:
        c.close()
    accounts = {}
    for kind in ("caldav", "carddav"):
        sid = client.post("/api/servers", json={
            "name": f"dst-{kind}", "imap_host": "127.0.0.1", "imap_port": 1, "imap_security": "none",
            "verify_tls": False, "caldav_url": DAV_URL, "carddav_url": DAV_URL, "dav_enabled": True, "workers": 1,
            "tuning": "manual", "notes": ""}).json()["id"]
        aid = client.post("/api/accounts", json={"server_id": sid, "email": dst, "password": "pw",
                                                 "options": {"mail": False}}).json()["id"]
        r = client.put(f"/api/accounts/{aid}/source", json={
            "kind": kind, "config": {"url": home(src), "login": src, "verify_tls": False},
            "secrets": {"password": "pw"}})
        assert r.status_code == 200, r.text
        assert client.put(f"/api/accounts/{aid}", json={"mirror": True}).status_code == 200
        job = run(client, client.post("/api/runs", json={"account_ids": [aid]}).json()[0]["id"])
        assert job["status"] == "done", client.get(f"/api/jobs/{job['id']}/log").json()
        accounts[kind] = aid
    return accounts, src, dst, (cal, todo, book)


def run(client, jid):
    from app.server import runner
    runner.run_job(jid, "test:inproc")
    return client.get(f"/api/jobs/{jid}").json()


def mirror(client, aid):
    r = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror", "dry_run": False})
    assert r.status_code == 200, r.text
    job = run(client, r.json()["jobs"][0]["id"])
    return job, client.get(f"/api/jobs/{job['id']}/report").json()


def preview(client, aid):
    r = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror"})
    return r.json()["accounts"][0]


def find(tree, needle):
    return [(url, n) for url, items in tree.items() for n, text in items.items() if needle in (text or "")]


@needs_dav
def test_dav_mirror(client, env, dav):
    accounts, src, dst, (cal, todo, book) = dav
    c = client_of(src)
    try:
        c.put(cal, "e1.ics", ics("e1", "Event 1 moved to Friday"), "caldav")   # changed: new etag
        c._request("DELETE", cal + "e2.ics")                                    # deleted
        c.put(cal, "e9.ics", ics("e9", "Event 9"), "caldav")                    # new
        c.put(todo, "t0.ics", ics("t0", "Task 0 done", "VTODO"), "caldav")
        c.put(book, "c3.vcf", vcf("c3", "Contact 3 new name"), "carddav")
        c._request("DELETE", book + "c4.vcf")
    finally:
        c.close()
    # native content of the target, next to ours
    t = client_of(dst)
    try:
        target_cal = [u for u, items in contents(dst, "caldav").items() if find({u: items}, "Event 0")][0]
        t.put(target_cal, "native.ics", ics("native", "Lunch booked on the target"), "caldav")
    finally:
        t.close()
    src_before = etags(src, "caldav"), etags(src, "carddav")
    acc = preview(client, accounts["caldav"])
    assert acc["ready"] is True, acc
    assert acc["to_add"]["calendar"] == 1 and acc["to_update"]["calendar"] == 1 and acc["to_delete"]["calendar"] == 1
    assert acc["to_update"]["tasks"] == 1
    job, rep = mirror(client, accounts["caldav"])
    assert job["status"] == "done", client.get(f"/api/jobs/{job['id']}/log").json()
    assert rep["mirror"]["calendar"] == {"added": 1, "updated": 1, "deleted": 1, "skipped_by_safety": 0, "errors": 0}
    assert rep["mirror"]["tasks"]["updated"] == 1
    tree = contents(dst, "caldav")
    assert find(tree, "Event 1 moved to Friday") and not find(tree, "SUMMARY:Event 1\r")
    assert not find(tree, "UID:e2") and find(tree, "UID:e9") and find(tree, "Task 0 done")
    assert find(tree, "Lunch booked on the target")
    copies = list((env.data_dir / "mirror-trash").rglob("*.ics"))
    assert len(copies) == 1 and "UID:e2" in copies[0].read_text()
    job, rep = mirror(client, accounts["carddav"])
    assert job["status"] == "done"
    assert rep["mirror"]["contacts"] == {"added": 0, "updated": 1, "deleted": 1, "skipped_by_safety": 0, "errors": 0}
    books = contents(dst, "carddav")
    assert find(books, "Contact 3 new name") and not find(books, "UID:c4")
    assert (etags(src, "caldav"), etags(src, "carddav")) == src_before  # the source was only read
    # idempotent
    for aid in accounts.values():
        acc = preview(client, aid)
        assert sum(acc["to_add"].values()) + sum(acc["to_update"].values()) + sum(acc["to_delete"].values()) == 0, acc


@needs_dav
def test_dav_rows_without_recorded_name_are_never_deleted(client, env, dav):
    """M1: without the recorded resource name, exists() cannot read the item by itself; a second listing proves
    nothing (the same PROPFIND as the first): kept."""
    from app.server import db
    accounts, src, dst, (cal, todo, book) = dav
    db.ex("UPDATE items SET src_ref=NULL WHERE kind='contact'")
    c = client_of(src)
    try:
        c._request("DELETE", book + "c4.vcf")
    finally:
        c.close()
    job, rep = mirror(client, accounts["carddav"])
    assert rep["mirror"]["contacts"]["deleted"] == 0 and rep["mirror"]["contacts"]["skipped_by_safety"] == 1
    assert find(contents(dst, "carddav"), "UID:c4")


def dav_session(user):
    from app.connectors.dav import DavSession
    return DavSession("caldav", {"url": home(user), "login": user, "verify_tls": False}, {"password": "pw"})


def changed_on_the_target(user, coll, name):
    c = client_of(user)
    try:
        c.put(coll, name, ics("x", "changed by hand on the target"), "caldav")
    finally:
        c.close()


@needs_dav
def test_dav_update_and_delete_use_if_match(client):
    """m4: update and delete only touch the resource the inventory saw (If-Match): changed on the target since,
    it is left alone (412), an update as a temporary error, a delete as a refusal."""
    from app.connectors.base import CALENDAR, ConnectorError, OutgoingItem
    user = create_user(prefix="dim")
    c = client_of(user)
    try:
        coll = c.ensure_collection("caldav", home(user) + "cal/", "VEVENT", "Cal")
    finally:
        c.close()
    s = dav_session(user)
    try:
        s.put(coll, OutgoingItem(CALENDAR, ics("x", "first").encode(), "<ns-x>", name="x.ics"))
        assert list(s.inventory(coll, "ns", {"x.ics"})) == ["x.ics"]
        changed_on_the_target(user, coll, "x.ics")
        with pytest.raises(ConnectorError) as err:
            s.update(coll, "x.ics", OutgoingItem(CALENDAR, ics("x", "from the source").encode(), "<ns-x>", name="x.ics"))
        assert err.value.retryable
        assert "changed by hand" in s.read(coll, "x.ics").decode()
        with pytest.raises(ConnectorError) as err:
            s.delete(coll, "x.ics", "x.ics")
        assert err.value.kind == ConnectorError.REJECTED
        assert "changed by hand" in s.read(coll, "x.ics").decode()
        # seen again by an inventory: both go through
        s.inventory(coll, "ns", {"x.ics"})
        s.update(coll, "x.ics", OutgoingItem(CALENDAR, ics("x", "from the source").encode(), "<ns-x>", name="x.ics"))
        assert "from the source" in s.read(coll, "x.ics").decode()
        s.inventory(coll, "ns", {"x.ics"})
        s.delete(coll, "x.ics", "x.ics")
        assert s.read(coll, "x.ics") is None
    finally:
        s.close()


@needs_dav
def test_dav_write_over_an_existing_resource_is_kept_unverified(client):
    """Third review: a normal write goes over a resource already there, as it always did (idempotent, no error).
    The first PUT says If-None-Match: *, so mccb knows when it wrote over something it had not created: that write
    is marked ("!" etag), and the mirror never deletes or updates it on that ground."""
    from app.connectors.base import CALENDAR, OutgoingItem
    user = create_user(prefix="dnm")
    c = client_of(user)
    try:
        coll = c.ensure_collection("caldav", home(user) + "cal/", "VEVENT", "Cal")
        c.put(coll, "x.ics", ics("x", "already there"), "caldav")
    finally:
        c.close()
    s = dav_session(user)
    try:
        assert s.put(coll, OutgoingItem(CALENDAR, ics("x", "from the source").encode(), "<ns-x>", name="x.ics")) == "x.ics"
        assert "from the source" in s.read(coll, "x.ics").decode()
        assert (s.written_version(coll, "x.ics") or "").startswith("!")
        assert s.put(coll, OutgoingItem(CALENDAR, ics("y", "new").encode(), "<ns-y>", name="y.ics")) == "y.ics"
        assert not (s.written_version(coll, "y.ics") or "!").startswith("!")
    finally:
        s.close()


def rerun(client, aid):
    job = run(client, client.post("/api/runs", json={"account_ids": [aid]}).json()[0]["id"])
    assert job["status"] == "done", client.get(f"/api/jobs/{job['id']}/log").json()
    return job


@needs_dav
@pytest.mark.parametrize("how", ["items_reset", "new_namespace", "interrupted", "target_saved_again"])
def test_normal_migration_rewrites_what_it_wrote_as_before(client, env, dav, how):
    """Third review, non-regression (behaviour of 7a68f42): a migration whose items rows are gone, under another
    namespace, or interrupted, writes again over the resources it wrote before: no error, the newer content of the
    source on the target."""
    from app.server import db
    accounts, src, dst, (cal, todo, book) = dav
    aid = accounts["caldav"]
    c = client_of(src)
    try:
        c.put(cal, "e1.ics", ics("e1", "Event 1 newer at the source"), "caldav")
    finally:
        c.close()
    if how == "items_reset":
        db.ex("DELETE FROM items WHERE account_id=?", (aid,))
    elif how == "new_namespace":
        db.ex("UPDATE items SET pst_key=pst_key || '-old' WHERE account_id=?", (aid,))
    elif how == "interrupted":
        db.ex("UPDATE items SET status='pending' WHERE account_id=?", (aid,))
    else:
        srv = client.get(f"/api/accounts/{aid}").json()
        assert srv
        db.ex("DELETE FROM items WHERE account_id=?", (aid,))  # what saving another target does (web.py)
    rerun(client, aid)
    assert find(contents(dst, "caldav"), "Event 1 newer at the source")
    if how == "new_namespace":  # rows of this mailbox under the old namespace prove these resources are its own
        marks = {(r["target_etag"] or "")[:1] for r in db.q(
            "SELECT target_etag FROM items WHERE account_id=? AND kind='event' AND status='ok' AND pst_key NOT LIKE "
            "'%-old'", (aid,))}
        assert "!" not in marks and "" not in marks, marks


@needs_dav
def test_mirror_never_touches_what_was_written_over_a_resource_already_there(client, env, dav):
    """Third review M2: a row written over a resource that was already on the target (its owner unknown) is never
    deleted nor updated by the mirror (note unverified_owner); a row of mccb's own write is."""
    from app.server import db
    accounts, src, dst, (cal, todo, book) = dav
    aid = accounts["caldav"]
    db.ex("DELETE FROM items WHERE account_id=?", (aid,))
    rerun(client, aid)                                  # every event written again, over what was there
    marks = {r["target_etag"][:1] for r in db.q("SELECT target_etag FROM items WHERE account_id=? AND kind='event' "
                                                "AND status='ok'", (aid,))}
    assert marks == {"!"}, marks
    c = client_of(src)
    try:
        c._request("DELETE", cal + "e3.ics")
    finally:
        c.close()
    acc = preview(client, aid)
    assert "unverified_owner" in {n["code"] for n in acc["notes"]} and acc["to_delete"]["calendar"] == 0, acc
    job, rep = mirror(client, aid)
    assert find(contents(dst, "caldav"), "UID:e3") and rep["mirror"]["calendar"]["deleted"] == 0
    assert rep["mirror"]["held"]["calendar"]["unverified_owner"] >= 1


@needs_dav
def test_mirror_adopts_legacy_rows_whose_content_matches(client, env, dav):
    """Third review: rows without a kept etag (written before) are adopted by a mirror job when the target holds
    exactly what the source has (same lines, in any order): the next pass may then delete them."""
    from app.server import db
    accounts, src, dst, (cal, todo, book) = dav
    aid = accounts["caldav"]
    db.ex("UPDATE items SET target_etag=NULL WHERE account_id=?", (aid,))
    c = client_of(src)
    try:
        c._request("DELETE", cal + "e3.ics")
    finally:
        c.close()
    job, rep = mirror(client, aid)                      # not verified yet: kept, the others are adopted
    assert find(contents(dst, "caldav"), "UID:e3")
    assert db.q1("SELECT COUNT(*) n FROM items WHERE account_id=? AND kind='event' AND status='ok' AND "
                 "target_etag IS NULL", (aid,))["n"] == 1   # e3 only: gone at the source, nothing to compare
    c = client_of(src)
    try:
        c._request("DELETE", cal + "e4.ics")
    finally:
        c.close()
    job, rep = mirror(client, aid)
    assert not find(contents(dst, "caldav"), "UID:e4") and find(contents(dst, "caldav"), "UID:e3")


@needs_dav
def test_dav_item_changed_on_the_target_is_left_alone(client, env, dav):
    """M2 (second review): the etag of OUR write is kept; an item whose etag on the target differs (changed there
    since mccb wrote it) is neither deleted nor overwritten (note target_modified)."""
    accounts, src, dst, (cal, todo, book) = dav
    tree = contents(dst, "caldav")
    [(tcal, _n)] = find(tree, "SUMMARY:Event 3\r")
    t = client_of(dst)
    try:
        t.put(tcal, "e3.ics", ics("e3", "Event 3 edited on the target"), "caldav")
        t.put(tcal, "e1.ics", ics("e1", "Event 1 edited on the target"), "caldav")
    finally:
        t.close()
    c = client_of(src)
    try:
        c._request("DELETE", cal + "e3.ics")
        c.put(cal, "e1.ics", ics("e1", "Event 1 changed at the source"), "caldav")
    finally:
        c.close()
    acc = preview(client, accounts["caldav"])
    assert "target_modified" in {n["code"] for n in acc["notes"]}, acc
    assert acc["to_delete"]["calendar"] == 0 and acc["to_update"]["calendar"] == 0, acc
    job, rep = mirror(client, accounts["caldav"])
    tree = contents(dst, "caldav")
    assert find(tree, "Event 3 edited on the target") and find(tree, "Event 1 edited on the target")
    assert rep["mirror"]["calendar"]["deleted"] == 0 and rep["mirror"]["calendar"]["skipped_by_safety"] == 2
    assert rep["mirror"]["held"]["calendar"]["target_modified"] == 2


# ------------------------------------------------------------------ without a server (httpx.MockTransport)
MS_HEAD = '<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">'
COLL_OK = ('<D:response><D:href>/u/c/</D:href><D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype>'
           '</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>')


def res(name, etag):
    return (f'<D:response><D:href>/u/c/{name}</D:href><D:propstat><D:prop><D:resourcetype/><D:getetag>"{etag}"'
            '</D:getetag></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>')


def mocked(handler):
    import httpx
    return DavClient("u", "pw", False, transport=httpx.MockTransport(handler))


def dav_source(handler):
    from app.connectors.dav import CalDavSource
    s = CalDavSource({"url": "http://dav.test/u/", "login": "u"}, {"password": "pw"})
    s.client = mocked(handler)
    return s


def test_truncated_multistatus_makes_an_incomplete_listing():
    """M1: a server that cuts a PROPFIND short says so in the multistatus (507 for the collection, RFC 4918): the
    listing is not complete, so nothing is deleted."""
    import httpx
    from app.connectors.base import CALENDAR, SourceFolder

    def handler(req):
        body = MS_HEAD + COLL_OK + res("a.ics", "1") + (
            '<D:response><D:href>/u/c/</D:href><D:status>HTTP/1.1 507 Insufficient Storage</D:status></D:response>'
            "</D:multistatus>")
        return httpx.Response(207, content=body.encode())
    s = dav_source(handler)
    listing = s.mirror_listing(SourceFolder(path=["Cal"], kind=CALENDAR, ref="http://dav.test/u/c/"))
    assert listing.complete is False and [i.ref for i in listing.items] == ["a.ics"]
    assert "507" in (listing.problem or "")


def listing_with(extra):
    import httpx
    from app.connectors.base import CALENDAR, SourceFolder
    s = dav_source(lambda req: httpx.Response(207, content=(MS_HEAD + COLL_OK + res("a.ics", "1") + extra
                                                            + "</D:multistatus>").encode()))
    return s.mirror_listing(SourceFolder(path=["Cal"], kind=CALENDAR, ref="http://dav.test/u/c/"))


def test_an_error_on_one_child_is_no_incomplete_listing():
    """minor 4: a 404 or 403 on one member is that member left out, not a listing cut short."""
    for status in ("404 Not Found", "403 Forbidden"):
        listing = listing_with(f'<D:response><D:href>/u/c/b.ics</D:href><D:status>HTTP/1.1 {status}</D:status>'
                               "</D:response>")
        assert listing.complete is True and [i.ref for i in listing.items] == ["a.ics"], status


def test_an_error_on_the_collection_or_a_5xx_is_an_incomplete_listing():
    """minor 4: an error status on the collection itself, or a 5xx anywhere: incomplete."""
    listing = listing_with('<D:response><D:href>/u/c/</D:href><D:status>HTTP/1.1 403 Forbidden</D:status>'
                           "</D:response>")
    assert listing.complete is False
    listing = listing_with('<D:response><D:href>/u/c/b.ics</D:href><D:status>HTTP/1.1 503 Busy</D:status>'
                           "</D:response>")
    assert listing.complete is False


def test_complete_multistatus_makes_a_complete_listing():
    import httpx
    from app.connectors.base import CALENDAR, SourceFolder
    s = dav_source(lambda req: httpx.Response(207, content=(MS_HEAD + COLL_OK + res("a.ics", "1")
                                                            + "</D:multistatus>").encode()))
    listing = s.mirror_listing(SourceFolder(path=["Cal"], kind=CALENDAR, ref="http://dav.test/u/c/"))
    assert listing.complete is True and len(listing.items) == 1


def test_exists_without_a_recorded_name_answers_present():
    """M1: no name recorded for a key: answered present (kept), never decided from another listing."""
    import httpx
    from app.connectors.base import CALENDAR, SourceFolder
    asked = []

    def handler(req):
        asked.append((req.method, str(req.url)))
        if req.method == "GET" and req.url.path.endswith("/gone.ics"):
            return httpx.Response(404)
        return httpx.Response(207, content=(MS_HEAD + COLL_OK + "</D:multistatus>").encode())
    s = dav_source(handler)
    f = SourceFolder(path=["Cal"], kind=CALENDAR, ref="http://dav.test/u/c/")
    got = s.exists(f, ["k-unnamed", "k-gone"], {"k-gone": "gone.ics"})
    assert got == {"k-unnamed": True, "k-gone": False}
    assert [m for m, _u in asked] == ["GET"]


def test_put_on_a_server_that_ignores_if_none_match():
    """A server that ignores If-None-Match simply writes: no error."""
    import httpx
    from app.connectors.base import CALENDAR, OutgoingItem
    from app.connectors.dav import DavSession
    seen = []

    def handler(req):
        seen.append((req.method, req.headers.get("if-none-match"), req.headers.get("if-match")))
        return httpx.Response(204, headers={"ETag": '"e9"'})
    s = DavSession("caldav", {"url": "http://dav.test/u/", "login": "u"}, {"password": "pw"})
    s.client = mocked(handler)
    assert s.put("http://dav.test/u/c/", OutgoingItem(CALENDAR, b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", "<ns-x>",
                                                       name="x.ics")) == "x.ics"
    assert seen == [("PUT", "*", None)] and s.written_version("http://dav.test/u/c/", "x.ics") == '"e9"'


def test_put_over_a_resource_named_by_its_uid():
    """A server that names objects by their UID (MDaemon) answers 412 to If-None-Match when the object is there:
    written over it with If-Match on the etag just read, as before (marked "!")."""
    import httpx
    from app.connectors.base import CALENDAR, OutgoingItem
    from app.connectors.dav import DavSession
    seen = []

    def handler(req):
        seen.append((req.method, req.headers.get("if-none-match"), req.headers.get("if-match")))
        if req.method == "PUT" and req.headers.get("if-none-match"):
            return httpx.Response(412)
        if req.method == "PROPFIND":
            return httpx.Response(207, content=(MS_HEAD + res("x.ics", "old") + "</D:multistatus>").encode())
        return httpx.Response(204, headers={"ETag": '"new"'})
    s = DavSession("caldav", {"url": "http://dav.test/u/", "login": "u"}, {"password": "pw"})
    s.client = mocked(handler)
    assert s.put("http://dav.test/u/c/", OutgoingItem(CALENDAR, b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", "<ns-x>",
                                                       name="x.ics")) == "x.ics"
    assert [m for m in seen if m[0] == "PUT"] == [("PUT", "*", None), ("PUT", None, '"old"')]
    assert s.written_version("http://dav.test/u/c/", "x.ics") == '!"new"'


def test_no_extra_propfind_outside_the_mirror():
    """Third review m2: a server that gives no ETag to a PUT costs no PROPFIND in a normal migration."""
    import httpx
    from app.connectors.base import CALENDAR, OutgoingItem
    from app.connectors.dav import DavSession
    seen = []

    def handler(req):
        seen.append(req.method)
        return httpx.Response(201)
    s = DavSession("caldav", {"url": "http://dav.test/u/", "login": "u"}, {"password": "pw"})
    s.client = mocked(handler)
    s.put("http://dav.test/u/c/", OutgoingItem(CALENDAR, b"X", "<ns-x>", name="x.ics"))
    assert seen == ["PUT"] and s.written_version("http://dav.test/u/c/", "x.ics") is None


def test_dav_update_sends_if_match_and_a_412_is_temporary():
    """m4: the PUT of an update carries If-Match with the etag of the inventory; 412 is a temporary error."""
    import httpx
    from app.connectors.base import CALENDAR, ConnectorError, OutgoingItem
    from app.connectors.dav import DavSession
    puts = []

    def handler(req):
        if req.method == "PROPFIND":
            return httpx.Response(207, content=(MS_HEAD + COLL_OK + res("x.ics", "e1") + "</D:multistatus>").encode())
        if req.method == "PUT":
            puts.append(req.headers.get("if-match"))
            return httpx.Response(412)
        return httpx.Response(404)
    s = DavSession("caldav", {"url": "http://dav.test/u/", "login": "u"}, {"password": "pw"})
    s.client = mocked(handler)
    s.inventory("http://dav.test/u/c/", "ns", {"x.ics"})
    with pytest.raises(ConnectorError) as err:
        s.update("http://dav.test/u/c/", "x.ics", OutgoingItem(CALENDAR, b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
                                                                "<ns-x>", name="x.ics"))
    assert err.value.retryable and puts == ['"e1"']
