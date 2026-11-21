"""Mirror mode, JMAP connector: the source is only read and lists completely or says it is not sure; the target changes
only what mccb wrote (soft delete to the trash, never a destroy of a mail), re-checking the mark at each call."""
import pytest

from app.connectors import convert, jmap
from app.connectors.base import (CAP_DELETE, CAP_INVENTORY, CAP_MIRROR, CAP_MOVE, CAP_SET_FLAGS, CAP_UPDATE, CONTACTS, DELETE_HARD,
                                 DELETE_SOFT, DELETE_TRASH, MAIL, MIRROR_DELETED_FLAG, ConnectorError, OutgoingItem,
                                 SourceFolder, stamp_import_id)
from app.connectors.jmap import JmapSource, JmapTarget
from tests.server.http_fakes import JmapServer

SECRETS = {"password": "pw"}
NS = "src-aa"


@pytest.fixture
def srv():
    s = JmapServer()
    yield s
    s.close()


def cfg(s):
    return {"url": s.url, "auth": "basic", "login": "me", "verify_tls": True}


def raw(subject, mid):
    return (f"From: a@example.test\r\nTo: b@example.test\r\nSubject: {subject}\r\nMessage-ID: <{mid}>\r\n"
            f"Date: Fri, 01 Mar 2024 12:00:00 +0000\r\n\r\nbody {subject}\r\n").encode()


def source(s):
    src = JmapSource(cfg(s), SECRETS)
    src.open()
    return src


def folder_of(src, *path):
    return next(f for f in src.folders() if f.path == list(path))


def fill(s, n=7):
    for i in range(1, n + 1):
        s.add_email(raw(f"m{i}", f"{i}@x"), ["mb-inbox"], {"$seen": True} if i == 1 else {}, f"2024-03-{i:02d}T10:00:00Z", eid=f"e{i}")


# ------------------------------------------------------------------ source
def test_source_declares_the_mirror_and_target_its_writes():
    from app.connectors import registry
    cat = {c["kind"]: c for c in registry.catalogue()}
    assert CAP_MIRROR in cat["jmap"]["source"]["capabilities"]
    assert {CAP_INVENTORY, CAP_DELETE, CAP_UPDATE, CAP_SET_FLAGS, CAP_MOVE} <= set(cat["jmap"]["target"]["capabilities"])


def test_mirror_listing_is_complete_and_versions(srv):
    fill(srv)
    srv.add_card({"uid": "urn:uuid:1", "name": {"full": "Ann Lee"}}, ["ab-1"])
    src = source(srv)
    lst = src.mirror_listing(folder_of(src, "INBOX"))
    assert lst.complete and lst.problem is None and len(lst.items) == 7        # paged by 3 in the fake
    assert lst.items[-1].flags == ["\\Seen"] and all(i.version is None for i in lst.items)   # a mail never changes
    cards = src.mirror_listing(folder_of(src, "Personal"))
    assert cards.complete and len(cards.items) == 1 and cards.items[0].version.startswith("2024-01-01")


def test_destroyed_mail_leaves_the_listing_and_the_cursor_survives_it(srv):
    fill(srv)
    src = source(srv)
    inbox = folder_of(src, "INBOX")
    first = src.changed_since(inbox, None)
    srv.destroy_email("e3")
    assert "ee3" not in [i.key for i in src.mirror_listing(inbox).items]
    more = src.changed_since(inbox, first.cursor)       # `destroyed` is reported as removed keys, nothing else
    assert not more.reset and more.items == [] and more.removed == ["ee3"]
    assert src.changed_since(inbox, more.cursor).removed == []


def test_listing_that_shifts_under_paging_is_read_again(srv):
    fill(srv)
    src = source(srv)
    inbox = folder_of(src, "INBOX")
    fired = []

    def hook(s, n):                                      # after the first page of the first pass: a mail is destroyed
        if n == 1 and not fired:
            fired.append(1)
            s.destroy_email("e7")
    srv.query_hook = hook
    lst = src.mirror_listing(inbox)
    assert fired and lst.complete                       # the shifted pass was thrown away, the second one is consistent
    assert sorted(i.key for i in lst.items) == sorted(f"ee{i}" for i in range(1, 7))


def test_listing_that_never_settles_is_not_complete(srv):
    fill(srv, 12)
    src = source(srv)
    n = []

    def hook(s, _):                                      # a mail is destroyed after every page: no pass is ever consistent
        n.append(1)
        s.destroy_email(next(iter(s.emails)))
    srv.query_hook = hook
    lst = src.mirror_listing(folder_of(src, "INBOX"))
    assert not lst.complete and "changed while" in lst.problem


def test_listing_without_query_state_or_with_a_wrong_total_is_not_complete(srv, monkeypatch):
    fill(srv)
    src = source(srv)
    inbox = folder_of(src, "INBOX")
    orig = JmapServer.m_Email_query
    monkeypatch.setattr(JmapServer, "m_Email_query", lambda self, a: {**orig(self, a), "queryState": None})
    assert not src.mirror_listing(inbox).complete
    monkeypatch.setattr(JmapServer, "m_Email_query", lambda self, a: {**orig(self, a), "total": 99})
    lst = src.mirror_listing(inbox)
    assert not lst.complete and "total" in lst.problem


def test_absence_is_confirmed_by_reading_the_id(srv):
    fill(srv)
    card = srv.add_card({"uid": "urn:uuid:1", "name": {"full": "Ann"}}, ["ab-1"])
    src = source(srv)
    inbox = folder_of(src, "INBOX")
    srv.destroy_email("e2")
    srv.add_email(raw("moved", "m@x"), ["mb-sent"], eid="emv")           # exists, just not in this folder
    got = src.exists(inbox, ["ee1", "ee2", "eemv", "eh" + "0" * 20])
    assert got == {"ee1": True, "ee2": False, "eemv": True, "eh" + "0" * 20: True}   # unreadable hashed key: kept
    book = folder_of(src, "Personal")
    srv.destroy_card(card)
    assert src.exists(book, ["c" + card]) == {"c" + card: False}


def test_exists_never_says_gone_without_notfound(srv, monkeypatch):
    fill(srv)
    src = source(srv)
    monkeypatch.setattr(JmapServer, "m_Email_get", lambda self, a: {"state": "s", "list": [], "notFound": []})
    assert src.exists(folder_of(src, "INBOX"), ["ee1"]) == {"ee1": True}     # empty answer is not an absence


def test_the_source_never_writes(srv, monkeypatch):
    fill(srv)
    srv.add_card({"uid": "urn:uuid:1", "name": {"full": "Ann"}}, ["ab-1"])
    real = jmap.JmapClient.api

    def strict(self, calls, using):
        for name, _a, _i in calls:
            assert name.endswith(("/get", "/query", "/changes")), f"mutating call {name} from the source"
        return real(self, calls, using)

    def no_upload(self, *a, **k):
        raise AssertionError("upload from the source")
    monkeypatch.setattr(jmap.JmapClient, "api", strict)
    monkeypatch.setattr(jmap.JmapClient, "upload", no_upload)
    src = source(srv)
    for f in src.folders():
        lst = src.mirror_listing(f)
        assert lst.complete
        cur = src.changed_since(f, None).cursor
        src.changed_since(f, cur)
        src.exists(f, [i.key for i in lst.items][:3])
        for it in lst.items[:2]:
            src.fetch(f, it, "<x>")
    assert not [c for c in srv.calls if c.endswith(("/set", "/import", "/copy"))]
    assert not [r for r in srv.http.requests if r.path.startswith("/upload")]
    assert srv.set_args == []


# ------------------------------------------------------------------ target: mail
def target(s):
    t = JmapTarget(cfg(s), SECRETS).session()
    t.connect()
    return t


def put(t, mid, folder="Inbox", flags=None, ns=NS):
    iid = f"<{ns}-{mid}>"
    return iid, t.put(folder, OutgoingItem(MAIL, stamp_import_id(raw(f"s{mid}", f"{mid}@x"), iid), iid, flags or []))


def test_inventory_lists_only_ours(srv):
    t = target(srv)
    iid1, e1 = put(t, "1", flags=["\\Seen"])
    iid2, e2 = put(t, "2")
    _o, other = put(t, "3", ns="other-ns")                                     # another namespace
    mine = srv.add_email(raw("hand", "h@x"), ["mb-inbox"])                       # arrived by itself: no header at all
    done = srv.add_email(stamp_import_id(raw("old", "o@x"), f"<{NS}-9>"), ["mb-inbox"], {"$mccbmirrordeleted": True})
    inv = t.inventory("Inbox", NS)
    assert set(inv) == {iid1, iid2}
    assert inv[iid1][0].ref == e1 and inv[iid1][0].flags == ["\\Seen"]
    assert {e1, e2}.isdisjoint({other, mine, done})
    assert t.inventory("Nowhere", NS) == {}


def test_delete_goes_to_the_trash_and_is_never_a_destroy(srv):
    trash = srv.add_trash()
    t = target(srv)
    iid, eid = put(t, "1", flags=["\\Seen"])
    assert t.delete_mode("Inbox") == DELETE_TRASH
    assert t.delete("Inbox", eid) == DELETE_TRASH
    e = srv.emails[eid]                                                          # still there: recoverable
    assert e["mailboxIds"] == {trash: True} and e["keywords"][jmap.MIRROR_KEYWORD] is True and e["keywords"]["$seen"] is True
    assert MIRROR_DELETED_FLAG.lower() == jmap.MIRROR_KEYWORD
    assert all("destroy" not in a for a in srv.set_args) and "destroyed" not in [k for _s, k, _i in srv.log]
    assert srv.set_args[-1]["ifInState"].startswith("s")
    assert t.inventory("Inbox", NS) == {}                                        # no longer ours to manage
    assert t.delete("Inbox", eid) == DELETE_TRASH                                # already gone from the folder: not an error
    assert len(srv.set_args) == 1


def test_delete_without_a_trash_only_marks(srv):
    t = target(srv)
    _iid, eid = put(t, "1")
    assert t.delete_mode("Inbox") == DELETE_SOFT
    assert t.delete("Inbox", eid) == DELETE_SOFT
    assert srv.emails[eid]["mailboxIds"] == {"mb-inbox": True} and srv.emails[eid]["keywords"] == {jmap.MIRROR_KEYWORD: True}


def test_nothing_is_deleted_inside_the_trash_or_junk(srv):
    trash, junk = srv.add_trash(), srv.add_junk()
    t = target(srv)
    for folder, box in (("Trash", trash), ("Junk", junk)):
        iid = f"<{NS}-{folder}>"
        eid = t.put(folder, OutgoingItem(MAIL, stamp_import_id(raw("x", f"{folder}@x"), iid), iid))
        assert t.delete_mode(folder) == DELETE_SOFT
        assert t.delete(folder, eid) == DELETE_SOFT
        assert srv.emails[eid]["mailboxIds"] == {box: True}                      # not moved, not destroyed
        assert srv.emails[eid]["keywords"] == {jmap.MIRROR_KEYWORD: True}


def test_a_mail_mccb_did_not_write_is_never_touched(srv):
    srv.add_trash()
    t = target(srv)
    foreign = srv.add_email(raw("mine", "f@x"), ["mb-inbox"], {"$seen": True})
    before = dict(srv.emails[foreign])
    for call in (lambda: t.delete("Inbox", foreign), lambda: t.set_flags("Inbox", foreign, ["\\Flagged"], []),
                 lambda: t.move("Inbox", foreign, "Sent")):
        with pytest.raises(ConnectorError) as e:
            call()
        assert e.value.kind == ConnectorError.REJECTED
    assert srv.emails[foreign] == before and srv.set_args == []
    # a mail of ours that somebody moved to another mailbox is left where it is
    _iid, eid = put(t, "1")
    srv.emails[eid]["mailboxIds"] = {"mb-work": True}
    t.delete("Inbox", eid)
    assert srv.emails[eid]["mailboxIds"] == {"mb-work": True} and srv.set_args == []


def test_state_change_between_check_and_write_redoes_the_check(srv):
    srv.add_trash()
    t = target(srv)
    _iid, eid = put(t, "1")
    orig, hit = JmapServer.m_Email_get, []

    def get_then_change(self, a):
        res = orig(self, a)
        if not hit:                                       # somebody else writes right after our read
            hit.append(1)
            self.add_email(raw("late", "l@x"), ["mb-sent"])
        return res
    srv.m_Email_get = lambda a: get_then_change(srv, a)
    t.delete("Inbox", eid)
    assert len(srv.set_args) == 2 and srv.emails[eid]["mailboxIds"] == {"mb-trash": True}   # first refused (stateMismatch)


def test_set_flags_and_move(srv):
    t = target(srv)
    _iid, eid = put(t, "1", flags=["\\Seen", "$Forwarded"])
    t.set_flags("Inbox", eid, ["\\Flagged"], ["\\Seen"])
    assert srv.emails[eid]["keywords"] == {"$forwarded": True, "$flagged": True}
    t.set_flags("Inbox", eid, [], [])                                            # nothing to do: nothing sent
    assert len(srv.set_args) == 1
    assert t.move("Inbox", eid, "Sent") == eid
    assert srv.emails[eid]["mailboxIds"] == {"mb-sent": True}
    with pytest.raises(ConnectorError) as e:
        t.move("Sent", eid, "Nowhere")
    assert e.value.kind == ConnectorError.FOLDER_MISSING
    assert srv.emails[eid]["mailboxIds"] == {"mb-sent": True}


def test_a_mail_cannot_be_updated_in_place(srv):
    t = target(srv)
    iid, eid = put(t, "1")
    with pytest.raises(ConnectorError) as e:
        t.update("Inbox", eid, OutgoingItem(MAIL, b"x", iid))
    assert e.value.kind == ConnectorError.CONFIG


# ------------------------------------------------------------------ target: contacts
VCARD = ["BEGIN:VCARD", "VERSION:3.0", "UID:abc", "FN:Ann Lee", "N:Lee;Ann;;;", "EMAIL;TYPE=work:ann@example.test",
         "ORG:ACME", "END:VCARD"]


def card_item(name="deadbeef.vcf", lines=VCARD):
    return OutgoingItem(CONTACTS, convert.join_lines(lines).encode(), f"<{NS}-{name}>", name=name)


def book_target(srv):
    t = target(srv)
    folder = t.plan_folders([SourceFolder(["Contacts"], CONTACTS)])["Contacts"].name
    t.ensure_folder(folder)
    return t, folder


def test_contacts_update_inventory_delete(srv):
    t, folder = book_target(srv)
    cid = t.put(folder, card_item())
    other = srv.add_card({"uid": "urn:uuid:not-ours", "name": {"full": "Bob"}}, [t._book("Contacts")])
    fresh = target(srv)                                                          # another session: knows nothing yet
    inv = fresh.inventory(folder, NS, {"deadbeef.vcf", "absent.vcf"})
    assert list(inv) == ["deadbeef.vcf"] and inv["deadbeef.vcf"][0].ref == cid and inv["deadbeef.vcf"][0].version
    assert fresh.delete_mode(folder) == DELETE_HARD
    # update: new content, properties that disappeared are removed, uid kept
    new = [x for x in VCARD if not x.startswith("ORG")]
    new[new.index("FN:Ann Lee")] = "FN:Ann Smith"
    assert fresh.update(folder, cid, card_item(lines=new)) == cid
    c = srv.cards[cid]
    assert c["name"]["full"] == "Ann Smith" and "organizations" not in c and c["uid"] == t._card_uid(card_item())
    assert srv.cards[other]["name"]["full"] == "Bob"
    # the vCard is readable before the hard delete (the runner keeps it)
    assert b"FN:Ann Smith" in fresh.read(folder, cid)
    assert fresh.delete(folder, cid) == DELETE_HARD
    assert cid not in srv.cards and other in srv.cards
    assert fresh.delete(folder, cid) == DELETE_HARD                              # already gone: not an error


def test_a_card_mccb_did_not_write_is_never_touched(srv):
    t, folder = book_target(srv)
    other = srv.add_card({"uid": "urn:uuid:not-ours", "name": {"full": "Bob"}}, [t._book("Contacts")])
    before = dict(srv.cards[other])
    with pytest.raises(ConnectorError) as e:
        t.delete(folder, other)                                                  # never listed by inventory
    assert e.value.kind == ConnectorError.REJECTED
    with pytest.raises(ConnectorError) as e:
        t.update(folder, other, card_item())                                     # uid is not the one derived from the name
    assert e.value.kind == ConnectorError.REJECTED
    t.inventory(folder, NS, {"deadbeef.vcf"})
    with pytest.raises(ConnectorError):
        t.delete(folder, other)
    assert srv.cards[other] == before and srv.set_args == []


def test_delete_checks_the_expected_import_id(srv):
    srv.add_trash()
    t = target(srv)
    iid, eid = put(t, "1")
    with pytest.raises(ConnectorError) as e:
        t.delete("Inbox", eid, f"<{NS}-other>")                                  # the mail does not carry this id any more
    assert e.value.kind == ConnectorError.REJECTED and srv.emails[eid]["mailboxIds"] == {"mb-inbox": True}
    assert srv.set_args == []
    assert t.delete("Inbox", eid, iid) == DELETE_TRASH
    assert srv.emails[eid]["mailboxIds"] == {"mb-trash": True}
    # delete_many (default of the interface) goes through delete() one by one
    _i2, e2 = put(t, "2")
    _i3, e3 = put(t, "3")
    assert t.delete_many("Inbox", [e2, e3]) == {e2: DELETE_TRASH, e3: DELETE_TRASH}


def test_card_delete_with_the_resource_name_needs_no_inventory(srv):
    t, folder = book_target(srv)
    cid = t.put(folder, card_item())
    other = srv.add_card({"uid": "urn:uuid:not-ours", "name": {"full": "Bob"}}, [t._book("Contacts")])
    fresh = target(srv)
    with pytest.raises(ConnectorError) as e:
        fresh.delete(folder, other, "deadbeef.vcf")                              # uid is not the one derived from that name
    assert e.value.kind == ConnectorError.REJECTED and other in srv.cards
    assert fresh.delete(folder, cid, "deadbeef.vcf") == DELETE_HARD and cid not in srv.cards
