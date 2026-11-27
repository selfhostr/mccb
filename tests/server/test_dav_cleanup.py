"""The collections earlier versions of mccb left on a DAV target, deleted at the end of a run: the second "Contacts"
whose items went to the account's own one, the copies of collections shared by other mailboxes (no longer migrated).
Only one mccb created, holding nothing but copies known to be needed no more; anything else keeps it, said in the
log. SmarterMail keeps one object per UID: the copies in the old collection are moved first.

Unit tests of the decision, a fake SmarterMail (httpx.MockTransport) end to end, Radicale end to end (TEST_DAV_URL,
docker-compose.test.yml)."""
from urllib.parse import unquote

import httpx
import pytest

from app.connectors import dav as cdav
from app.connectors.base import ConnectorError
from app.server.davcleanup import judge
from tests.server.test_dav_default_collections import (account as radicale_account, client_of, home, needs_dav,
                                                       run as radicale_run, targets, tree, users)  # noqa: F401
from tests.server.test_dav_smartermail import account, items, run, sm, vcf  # noqa: F401

H = "https://dav.test/u/"
OLD = H + cdav.collection_slug("carddav", "VEVENT", "Contacts")  # a second "Contacts" an earlier version made
OWN = H + "contacts/"
SHARED = H + cdav.collection_slug("carddav", "VEVENT", "[boss@x.test] - Contacts")


def row(name, status="ok", target=OWN, fk="Contacts", kind="contact", error=None, rid=1):
    return {"id": rid, "kind": kind, "fk": fk, "status": status, "target": target, "error": error, "hash": name}


def decide(url, resources, rows=(), *, evidence=frozenset({"Contacts"}), present=None, live_other=0, shared=(),
           include_shared=False, plan=None):
    rows = list(rows)
    by_name = {}
    for r in rows:
        by_name.setdefault(r["hash"].casefold(), []).append(r)
    there = {OWN: {"c0.vcf", "c1.vcf", "c2.vcf"}} if present is None else present
    return judge(url, {n: '"e"' for n in resources}, rows_by_name=by_name,
                 rows_in=[r for r in rows if cdav.url_key(r["target"] or "") == cdav.url_key(url)],
                 live_other=live_other, evidence=set(evidence),
                 plan_of={("contact", "Contacts"): OWN} if plan is None else plan, shared=set(shared),
                 include_shared=include_shared, present=lambda t: there.get(t))


# ------------------------------------------------------------------ unit: the decision
def test_a_copy_whose_items_are_all_in_the_new_collection_goes():
    v = decide(OLD, ["c0.vcf", "C1.VCF"], [row("c0.vcf"), row("c1.vcf", rid=2)])
    assert v.delete and v.reasons == ["moved"] and not v.moves


def test_only_a_collection_mccb_created():
    v = decide(OWN.replace("contacts/", "dup/"), ["c0.vcf"], [row("c0.vcf")])
    assert not v.delete and v.quiet and "not created by mccb" in v.why


def test_anything_else_in_it_keeps_it():
    v = decide(OLD, ["c0.vcf", "made-in-the-webmail.vcf"], [row("c0.vcf")])
    assert not v.delete and not v.quiet and "'made-in-the-webmail.vcf' was not written there by mccb" in v.why
    assert "1 of its 2 item(s)" in v.why


def test_an_item_not_yet_in_the_new_collection_keeps_it():
    v = decide(OLD, ["c0.vcf", "c9.vcf"], [row("c0.vcf"), row("c9.vcf", rid=2)])
    assert not v.delete and f"'c9.vcf' is not in {OWN} yet" in v.why
    v = decide(OLD, ["c0.vcf"], [row("c0.vcf")], present={})  # the new one cannot be listed
    assert not v.delete and "could not be listed" in v.why


def test_without_a_record_of_the_move_it_is_kept():
    v = decide(OLD, ["c0.vcf"], [row("c0.vcf")], evidence=())
    assert not v.delete and not v.quiet and "no record that 'c0.vcf' was moved out of it" in v.why
    # nothing of this mailbox in it at all (another migration's, the user's): an info line, no warning
    v = decide(OLD, ["x.vcf"], [], evidence=())
    assert not v.delete and v.quiet


def test_items_still_recorded_in_it_keep_it():
    v = decide(OLD, ["c0.vcf"], [row("c0.vcf", target=OLD)])  # gone from the source since: the only copy
    assert not v.delete and "still recorded in it" in v.why
    v = decide(OLD, ["c0.vcf"], [row("c0.vcf")], live_other=3)
    assert not v.delete and v.quiet and "another migration" in v.why


def test_copies_of_a_shared_folder_left_out_go_unless_shared_ones_are_migrated():
    fk = "[boss@x.test] - Contacts"
    rows = [row("b0.vcf", status="skipped", target=SHARED, fk=fk, error="not migrated: shared from boss@x.test")]
    v = decide(SHARED, ["b0.vcf"], rows, evidence=(), shared={("contact", fk)})
    assert v.delete and v.reasons == ["shared"]
    v = decide(SHARED, ["b0.vcf"], rows, evidence=(), shared={("contact", fk)}, include_shared=True)
    assert not v.delete
    v = decide(SHARED, ["b0.vcf"], rows, evidence=())  # skipped for another reason: not one of these
    assert not v.delete


def test_an_item_its_copy_keeps_out_is_moved():
    err = f"PUT {OWN}c2.vcf -> HTTP 500: a contact with the UID 'c2' already exists in the address book 'Contacts' " \
          f"({OLD}c2.vcf): the server keeps one contact per UID in the account"
    v = decide(OLD, ["c0.vcf", "c2.vcf"], [row("c0.vcf"), row("c2.vcf", status="error", error=err, rid=2)],
               present={OWN: {"c0.vcf"}})
    assert v.delete and [(n, r["id"], t) for n, r, t in v.moves] == [("c2.vcf", 2, OWN)]
    # an error that does not name this collection: not moved, kept
    v = decide(OLD, ["c2.vcf"], [row("c2.vcf", status="error", error="refuses this contact")], present={OWN: set()})
    assert not v.delete and not v.moves


def test_an_empty_one_goes_only_when_it_held_items_of_this_mailbox():
    assert decide(OLD, [], []).delete
    v = decide(OLD, [], [], evidence=())
    assert not v.delete and v.quiet


# ------------------------------------------------------------------ unit: the DELETE of a collection
def session_with(handler):
    from app.server.dav import DavClient
    s = cdav.CardDavTarget({"url": H, "login": "u"}, {"password": "p"}).session()
    s.client = DavClient("u", "p", transport=httpx.MockTransport(handler))
    return s


def book_props(href):
    return httpx.Response(207, text=(
        '<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav"><d:response>'
        f"<d:href>{href}</d:href><d:propstat><d:prop><d:resourcetype><d:collection/><card:addressbook/>"
        "</d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"))


@pytest.mark.parametrize("answers, want", [
    ([204], "deleted"),
    ([404], "gone"),
    ([500], "deleted"),  # SmarterMail: 500 to a request it carried out; not sent again
    ([500, 204], "deleted"),
])
def test_delete_collection(monkeypatch, answers, want):
    monkeypatch.setattr(cdav.DavSession, "ITEM_RETRY_DELAY", 0.0)
    state = {"there": True, "deletes": 0}
    answers = list(answers)

    def handler(req):
        if req.method == "DELETE":
            state["deletes"] += 1
            sc = answers.pop(0)
            if sc == 204 or (sc == 500 and not answers):
                state["there"] = False  # done, whatever the answer says
            return httpx.Response(sc)
        assert req.method == "PROPFIND"
        return book_props(req.url.path) if state["there"] else httpx.Response(404)

    assert session_with(handler).delete_collection(OLD) == want
    assert not answers or want == "gone"


def test_delete_collection_never_takes_a_collection_still_there_for_done():
    def handler(req):
        if req.method == "DELETE":
            return httpx.Response(207, text='<d:multistatus xmlns:d="DAV:"/>')  # members it could not delete
        return book_props(req.url.path)

    with pytest.raises(ConnectorError) as e:
        session_with(handler).delete_collection(OLD)
    assert e.value.kind == ConnectorError.REJECTED and "still there" in str(e.value)
    with pytest.raises(ConnectorError) as e:
        session_with(lambda req: httpx.Response(403)).delete_collection(OLD)
    assert e.value.kind == ConnectorError.REJECTED and "refuses to delete it" in str(e.value)


# ------------------------------------------------------------------ fake SmarterMail, end to end
DUP = "mccb-contacts-0123abcd"


def earlier_copy(client, src, dst, n=3):
    """As after an earlier version: every contact in a second "Contacts" mccb made, then the account's own book."""
    from app.server import db
    src.add("b64=Q29udGFjdHM", "Contacts", items={f"c{i}.vcf": vcf(f"c{i}", f"Contact {i}") for i in range(n)})
    dup = dst.add(DUP, "Contacts")
    aid = account(client)
    job, _rep, log = run(client, aid)  # the only "Contacts" then: everything there
    assert job["status"] == "done", log
    assert sorted(dst.cols[dup]["items"]) == [f"c{i}.vcf" for i in range(n)]
    db.ex("DELETE FROM kv WHERE key LIKE 'dav-collections:%'")  # an earlier version kept no choice
    own = dst.add("b64=Q29udGFjdHM", "Contacts")
    return aid, dup, own


def test_smartermail_second_contacts_is_moved_into_the_own_one_then_deleted(client, sm):
    src, dst = sm
    aid, dup, own = earlier_copy(client, src, dst)
    before = dict(dst.cols[dup]["items"])
    job, rep, log = run(client, aid)
    assert job["status"] == "done", log
    assert dup not in dst.cols, dst.names()  # deleted
    assert dst.cols[own]["items"] == before  # every contact in the account's own book, as it was
    assert "collection_changed: 3 item(s) of 'Contacts'" in log and "deleted: 3 item(s)" in log, log
    [gone] = rep["dav_removed"]
    assert (gone["url"], gone["name"], gone["items"], gone["reason"], gone["moved"]) == (
        "https://dst.test" + dup, "Contacts", 3, "moved", 3)
    assert gone["backup"] and "mirror-trash" in gone["backup"]
    got = items(aid)
    assert all(r["status"] == "ok" for r in got.values()), got
    assert rep["dav"]["contacts"]["ok"] and rep["dav"]["contacts"]["present"] == 3
    from app.server import db
    assert db.q1("SELECT value FROM kv WHERE key LIKE 'dav-moved:%'")["value"] == "{}"
    # again: nothing to delete, nothing written
    puts = len(dst.puts())
    job, rep, log = run(client, aid)
    assert job["status"] == "done" and "dav_removed" not in rep and len(dst.puts()) == puts, log


def test_smartermail_a_move_the_new_book_refuses_is_written_back_and_the_old_one_kept(client, sm):
    src, dst = sm
    aid, dup, own = earlier_copy(client, src, dst)
    before = dict(dst.cols[dup]["items"])
    dst.refuse = {own + "c1.vcf"}
    job, rep, log = run(client, aid)
    assert job["status"] == "done_with_errors", log
    assert dup in dst.cols and set(dst.cols[dup]["items"]) == {"c1.vcf", "c2.vcf"}
    assert dst.cols[dup]["items"]["c1.vcf"] == before["c1.vcf"]  # written back as it was
    assert set(dst.cols[own]["items"]) == {"c0.vcf"}
    assert "collection_kept: 'Contacts' (https://dst.test/dav/me/mccb-contacts-0123abcd/)" in log, log
    assert "still refuses 'c1.vcf' once its copy here is deleted" in log
    assert "dav_removed" not in rep and rep["dav_kept"][0]["name"] == "Contacts"
    got = items(aid)
    assert got["Contact 0"]["status"] == "ok" and got["Contact 1"]["status"] == "error"
    # the cause gone, the next run finishes it
    dst.refuse = set()
    job, rep, log = run(client, aid)
    assert job["status"] == "done", log
    assert dup not in dst.cols and dst.cols[own]["items"] == before
    assert rep["dav_removed"][0]["moved"] == 2


def test_smartermail_a_run_of_an_earlier_version_is_known_from_its_report(client, sm):
    """The run that moved the items (an earlier version) kept no record of it but its report (`dav_moved`); an object
    the user added in the old book kept it then. Once that object is gone, the next run deletes the book."""
    from app.server import db
    src, dst = sm
    aid, dup, own = earlier_copy(client, src, dst)
    dst.cols[dup]["items"]["user.vcf"] = vcf("user-1", "Added in the webmail").encode()
    job, rep, log = run(client, aid)
    assert "'user.vcf' was not written there by mccb for this mailbox" in log, log
    assert dup in dst.cols and rep["dav_moved"]
    del dst.cols[dup]["items"]["user.vcf"]
    db.ex("DELETE FROM kv WHERE key LIKE 'dav-moved:%'")
    job, rep, log = run(client, aid)
    assert job["status"] == "done", log
    assert dup not in dst.cols and len(dst.cols[own]["items"]) == 3


def test_smartermail_nothing_deleted_on_a_rerun_of_the_errors_only(client, sm):
    from app.server import runner
    src, dst = sm
    aid, dup, own = earlier_copy(client, src, dst)
    dst.cols[dup]["items"]["user.vcf"] = vcf("user-1", "Added in the webmail").encode()
    job, rep, log = run(client, aid)
    assert dup in dst.cols, log
    del dst.cols[dup]["items"]["user.vcf"]
    assert client.post(f"/api/jobs/{job['id']}/retry-errors").status_code == 200
    runner.run_job(job["id"], "test:inproc")
    assert dup in dst.cols and len(dst.cols[dup]["items"]) == 3  # errors only: nothing deleted, nothing moved
    assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 200  # a normal rerun of the same job
    runner.run_job(job["id"], "test:inproc")
    assert dup not in dst.cols and len(dst.cols[own]["items"]) == 3


# ------------------------------------------------------------------ Radicale, end to end
@needs_dav
def test_radicale_second_contacts_deleted_once_its_items_are_in_the_own_one(client, users):
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
        dup = t.ensure_collection("carddav", home(dst) + cdav.collection_slug("carddav", "VEVENT", "Contacts"),
                                  "VEVENT", "Contacts")
    finally:
        t.close()
    assert cdav.made_by_mccb(dup)
    aid = radicale_account(client, src, dst, "carddav")
    job, rep, log = radicale_run(client, aid)
    assert job["status"] == "done", log
    db.ex("DELETE FROM kv WHERE key LIKE 'dav-collections:%'")
    t = client_of(dst)
    try:
        own = t.ensure_collection("carddav", home(dst) + "own-contacts/", "VEVENT", "Contacts")
        t.put(own, "c1.vcf", vcf("c1", "Contact 1 kept as it is"), "carddav")
        t.put(dup, "webmail.vcf", vcf("w1", "Added in the second book"), "carddav")  # keeps it, first
    finally:
        t.close()
    job, rep, log = radicale_run(client, aid)
    assert job["status"] == "done", log
    got = tree(dst, "carddav")
    assert len(got) == 2 and "'webmail.vcf' was not written there by mccb" in log, log
    assert unquote(rep["dav_kept"][0]["url"]) == unquote(dup)
    t = client_of(dst)
    try:
        t.delete_item(dup, "webmail.vcf")
    finally:
        t.close()
    job, rep, log = radicale_run(client, aid)
    assert job["status"] == "done", log
    got = tree(dst, "carddav")
    assert list(got) == ["Contacts"] and sorted(got["Contacts"]) == ["c0.vcf", "c1.vcf", "c2.vcf"], got
    assert "kept as it is" in got["Contacts"]["c1.vcf"]
    [gone] = rep["dav_removed"]
    assert (unquote(gone["url"]), gone["items"], gone["reason"], gone["moved"]) == (unquote(dup), 3, "moved", 0)
    assert targets(aid) == {unquote(own)}
    job, rep, log = radicale_run(client, aid)
    assert job["status"] == "done" and "dav_removed" not in rep and tree(dst, "carddav") == got


@needs_dav
def test_radicale_copy_of_a_shared_book_deleted_once_it_is_no_longer_migrated(client, users, monkeypatch):
    from app.server import db
    from app.server.config import settings
    src, dst = users
    c = client_of(src)
    try:
        book = c.ensure_collection("carddav", home(src) + "book/", "VEVENT", "Contacts")
        boss = c.ensure_collection("carddav", home(src) + "boss/", "VEVENT", "[boss@example.test] - Contacts")
        c.put(book, "c0.vcf", vcf("c0", "Mine"), "carddav")
        c.put(boss, "b0.vcf", vcf("b0", "Theirs"), "carddav")
        c.put(boss, "b1.vcf", vcf("b1", "Theirs too"), "carddav")
    finally:
        c.close()
    t = client_of(dst)
    try:
        t.ensure_collection("carddav", home(dst) + "default-book/", "VEVENT", "Contacts")
    finally:
        t.close()
    aid = radicale_account(client, src, dst, "carddav")
    monkeypatch.setattr(settings, "dav_include_shared", True)  # as an earlier version did
    job, rep, log = radicale_run(client, aid)
    assert job["status"] == "done", log
    assert sorted(tree(dst, "carddav")) == ["Contacts", "[boss@example.test] - Contacts"]
    job, rep, log = radicale_run(client, aid)  # still migrated: nothing deleted
    assert "dav_removed" not in rep and len(tree(dst, "carddav")) == 2
    monkeypatch.setattr(settings, "dav_include_shared", False)
    job, rep, log = radicale_run(client, aid)
    assert job["status"] == "done", log
    got = tree(dst, "carddav")
    assert list(got) == ["Contacts"] and list(got["Contacts"]) == ["c0.vcf"], got
    [gone] = rep["dav_removed"]
    assert gone["name"] == "[boss@example.test] - Contacts" and gone["reason"] == "shared" and gone["items"] == 2
    assert "not migrated: shared from boss@example.test" in log
    assert db.q1("SELECT COUNT(*) n FROM items WHERE account_id=? AND subject LIKE 'Theirs%'", (aid,))["n"] == 0
    job, rep, log = radicale_run(client, aid)
    assert job["status"] == "done" and "dav_removed" not in rep, log
