"""Mirror mode CalDAV / CardDAV to DAV against Radicale (TEST_DAV_URL): events, tasks and contacts added, updated by
etag and deleted (a copy kept first), native content of the target never touched, the source only read."""
import os
from urllib.parse import urljoin

import pytest

from app.server.dav import DavClient
from tests.svcusers import create_user

DAV_URL = os.environ.get("TEST_DAV_URL")
pytestmark = pytest.mark.skipif(not DAV_URL, reason="TEST_DAV_URL not set")


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
