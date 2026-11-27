"""Items an earlier plan put in the collection of ANOTHER folder of the source (the cards of the default "Contacts"
in the collection of "Contacts suggérés", next to the target's own "Contacts" left empty) go where they belong on the
next run, normal or mirror: written into the collection of their folder, read back there, then deleted from the
other one once mccb shows the copy there is its own, with a copy kept under mirror-trash/. Anything mccb cannot show
is its own stays where it is. The items of the other folder given up meanwhile are written into it.

End to end against Radicale (TEST_DAV_URL, docker-compose.test.yml)."""
from pathlib import Path
from urllib.parse import unquote

import pytest

from tests.server.test_dav_default_collections import (account, client_of, home, needs_dav, run, tree,  # noqa: F401
                                                       users, vcf)

SUGG = "Contacts suggérés"


def _setup(client, src, dst, n=5, m=2):
    """The source: "Contacts" (n cards) and "Contacts suggérés" (m cards); the target its own "Contacts". A first
    run puts each where it belongs; then the state an earlier plan left: the cards of "Contacts" in the collection
    of "Contacts suggérés" (recorded there, without the etag of the write), the target's "Contacts" empty, the
    suggested cards given up and not on the target."""
    from app.server import db
    c = client_of(src)
    try:
        book = c.ensure_collection("carddav", home(src) + "book/", "VEVENT", "Contacts")
        sugg = c.ensure_collection("carddav", home(src) + "suggested/", "VEVENT", SUGG)
        for i in range(n):
            c.put(book, f"c{i}.vcf", vcf(f"c{i}", f"Contact {i}"), "carddav")
        for i in range(m):
            c.put(sugg, f"s{i}.vcf", vcf(f"s{i}", f"Suggested {i}"), "carddav")
    finally:
        c.close()
    t = client_of(dst)
    try:
        own = t.ensure_collection("carddav", home(dst) + "default-book/", "VEVENT", "Contacts")
    finally:
        t.close()
    aid = account(client, src, dst, "carddav")
    job, _rep, log = run(client, aid)
    assert job["status"] == "done", log
    rows = {r["hash"]: dict(r) for r in db.q("SELECT * FROM items WHERE account_id=?", (aid,))}
    there = rows["s0.vcf"]["target"]
    assert "mccb-contacts-suggeres-" in there and unquote(rows["c0.vcf"]["target"]) == unquote(own)
    t = client_of(dst)
    try:
        for i in range(n):  # the cards of "Contacts" in the collection of "Contacts suggérés"
            t.put(there, f"c{i}.vcf", t.get(own, f"c{i}.vcf"), "carddav")
            t.delete_item(own, f"c{i}.vcf")
        for i in range(m):
            t.delete_item(there, f"s{i}.vcf")
        t.put(there, "user-1.vcf", vcf("user-1", "Added in the webmail"), "carddav")
    finally:
        t.close()
    db.ex("UPDATE items SET target=?, target_etag=NULL WHERE account_id=? AND folder='Contacts'", (there, aid))
    db.ex("UPDATE items SET status='skipped', error='no longer at the source' WHERE account_id=? AND folder=?",
          (aid, SUGG))
    assert sorted(tree(dst, "carddav")["Contacts"]) == []
    return aid, own, there


def _check_fixed(dst, aid, own, there, rep, log, n=5, m=2):
    from app.server import db
    got = tree(dst, "carddav")
    assert sorted(got) == ["Contacts", SUGG], got
    assert sorted(got["Contacts"]) == [f"c{i}.vcf" for i in range(n)], got
    assert sorted(got[SUGG]) == [f"s{i}.vcf" for i in range(m)] + ["user-1.vcf"], got  # the user's card stays
    assert "Added in the webmail" in got[SUGG]["user-1.vcf"]
    assert f"Contacts: {n} card(s) moved from '{SUGG}' to 'Contacts' where they belong" in log, log
    assert "collection_changed" not in log and "misplaced:" not in log, log
    rows = [dict(r) for r in db.q("SELECT folder, status, target FROM items WHERE account_id=?", (aid,))]
    assert all(r["status"] == "ok" for r in rows), rows
    assert {unquote(r["target"]) for r in rows if r["folder"] == "Contacts"} == {unquote(own)}
    assert {unquote(r["target"]) for r in rows if r["folder"] == SUGG} == {unquote(there)}
    [mv] = [x for x in rep["dav_misplaced"] if x["moved"]]
    assert (mv["folder"], mv["moved"], mv["copied"], mv["failed"]) == ("Contacts", n, 0, 0)
    cols = {unquote(x["url"]): x for x in rep["dav"]["contacts"]["collections"]}
    assert (cols[unquote(own)]["sent"], cols[unquote(own)]["present"]) == (n, n), cols
    assert (cols[unquote(there)]["sent"], cols[unquote(there)]["present"]) == (m, m), cols
    backup = log.split("a copy of each is kept in ", 1)[1].split(")", 1)[0]
    assert "mirror-trash" in backup and len(list(Path(backup).glob("*.vcf"))) == n


@needs_dav
def test_cards_in_the_collection_of_another_folder_are_moved_by_a_normal_run(client, users):
    src, dst = users
    aid, own, there = _setup(client, src, dst)
    job, rep, log = run(client, aid)
    assert job["status"] == "done", log
    _check_fixed(dst, aid, own, there, rep, log)
    # again: nothing moves, nothing twice
    got = tree(dst, "carddav")
    job, rep, log = run(client, aid)
    assert job["status"] == "done", log
    assert tree(dst, "carddav") == got and "where they belong" not in log and "misplaced" not in log, log


@needs_dav
def test_cards_in_the_collection_of_another_folder_are_moved_by_the_mirror(client, users):
    src, dst = users
    aid, own, there = _setup(client, src, dst)
    assert client.put(f"/api/accounts/{aid}", json={"mirror": True}).status_code == 200
    job, rep, log = run(client, aid, mode="mirror", dry_run=False)
    assert job["status"] == "done", log
    _check_fixed(dst, aid, own, there, rep, log)
    assert rep["contacts"]["skipped"] == 0 and rep["mirror"]["contacts"]["deleted"] == 0, rep
    job, rep, log = run(client, aid, mode="mirror", dry_run=False)
    assert job["status"] == "done", log
    assert "where they belong" not in log and rep["mirror"]["contacts"]["deleted"] == 0, log


@needs_dav
def test_a_copy_mccb_cannot_show_is_its_own_is_never_deleted(client, users):
    """The object under the name of a card in the other collection holds another UID (not what mccb wrote there):
    the card is written from the source where it belongs, that object is left as it is."""
    from app.server import db
    src, dst = users
    aid, own, there = _setup(client, src, dst, n=2, m=1)
    t = client_of(dst)
    try:
        t.delete_item(there, "c1.vcf")  # Radicale refuses another UID over a resource
        t.put(there, "c1.vcf", vcf("someone-else", "Not the card mccb wrote"), "carddav")
    finally:
        t.close()
    job, rep, log = run(client, aid)
    assert job["status"] == "done", log
    got = tree(dst, "carddav")
    assert sorted(got["Contacts"]) == ["c0.vcf", "c1.vcf"] and "Contact 1" in got["Contacts"]["c1.vcf"], got
    assert "Not the card mccb wrote" in got[SUGG]["c1.vcf"]  # left there
    assert "c0.vcf" not in got[SUGG] and "s0.vcf" in got[SUGG], got
    assert "Contacts: 1 card(s) moved from" in log, log
    assert "'c1.vcf' cannot be shown to be mccb's copy" in log, log
    assert {unquote(r["target"]) for r in db.q("SELECT target FROM items WHERE account_id=? AND folder='Contacts'",
                                               (aid,))} == {unquote(own)}
