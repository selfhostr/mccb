"""JMAP source and target against a fake server speaking RFC 8620 / 8621 / 9610 (tests/server/http_fakes.py)."""
import pytest

from app.connectors import convert
from app.connectors.base import CONTACTS, MAIL, ConnectorError, OutgoingItem, SourceFolder, stamp_import_id
from app.connectors.jmap import JmapSource, JmapTarget
from tests.server.http_fakes import JmapServer

SECRETS = {"password": "pw"}


@pytest.fixture
def srv():
    s = JmapServer()
    yield s
    s.close()


def cfg(s, **extra):
    return {"url": s.url, "auth": "basic", "login": "me", "verify_tls": True, **extra}


def raw(subject, mid):
    return (f"From: a@example.test\r\nTo: b@example.test\r\nSubject: {subject}\r\nMessage-ID: <{mid}>\r\n"
            f"Date: Fri, 01 Mar 2024 12:00:00 +0000\r\n\r\nbody {subject}\r\n").encode()


def opened(s, **extra):
    src = JmapSource(cfg(s, **extra), SECRETS)
    src.open()
    return src


def fill(s):
    s.add_email(raw("one", "1@x"), ["mb-inbox"], {"$seen": True}, "2024-03-01T10:00:00Z", eid="e1")
    s.add_email(raw("two", "2@x"), ["mb-work", "mb-inbox"], {}, "2024-03-02T10:00:00Z", eid="e2")   # a label, not a copy
    s.add_email(raw("three", "3@x"), ["mb-work"], {"$flagged": True, "$forwarded": True}, "2024-03-03T10:00:00Z", eid="e3")
    s.add_email(raw("four", "4@x"), ["mb-sent"], {}, "2024-03-04T10:00:00Z", eid="e4")
    for i in range(5, 9):
        s.add_email(raw(f"m{i}", f"{i}@x"), ["mb-inbox"], {}, f"2024-03-{i:02d}T10:00:00Z", eid=f"e{i}")


# ------------------------------------------------------------------ helpers
def test_jmap_pure_helpers():
    from app.connectors import jmap
    assert jmap.expand("/dl/{accountId}/{blobId}/{name}", accountId="A 1", blobId="b/2", name="m.eml") == "/dl/A%201/b%2F2/m.eml"
    assert jmap.flags_of({"$seen": True, "$flagged": True, "$forwarded": True, "$x": False, "bad kw": True}) == [
        "\\Seen", "\\Flagged", "$Forwarded"]
    assert jmap.keywords_of(["\\Seen", "\\Recent", "$Forwarded", "bad kw", "\\Answered"]) == {
        "$seen": True, "$forwarded": True, "$answered": True}
    assert jmap.format_utc(jmap.parse_utc("2024-03-01T12:00:00Z")) == "2024-03-01T12:00:00Z"
    assert jmap.key_of("e", "abc") == "eabc" and len(jmap.key_of("e", "x" * 300)) < 60
    boxes = [{"id": "1", "name": "Inbox"}, {"id": "2", "name": "Sub", "parentId": "1"}, {"id": "3", "name": "Loop", "parentId": "3"}]
    assert jmap.mailbox_paths(boxes) == {"1": ["Inbox"], "2": ["Inbox", "Sub"], "3": ["Loop"]}


# ------------------------------------------------------------------ source
def test_source_folders_items_and_fetch(srv):
    fill(srv)
    src = opened(srv)
    folders = {tuple(f.path): f for f in src.folders()}
    assert set(folders) == {("INBOX",), ("Sent",), ("Work",), ("Personal",)}  # the "all" role is a view, not a folder
    assert folders[("INBOX",)].special == "inbox" and folders[("Sent",)].special == "sent"
    assert folders[("Personal",)].kind == CONTACTS and folders[("INBOX",)].kind == MAIL
    inbox = list(src.iter_items(folders[("INBOX",)]))
    work = list(src.iter_items(folders[("Work",)]))
    assert [i.key for i in inbox] == ["ee8", "ee7", "ee6", "ee5", "ee2", "ee1"]  # newest first, e2 once (inbox owns it)
    assert [i.key for i in work] == ["ee3"]
    assert inbox[-1].flags == ["\\Seen"] and work[0].flags == ["\\Flagged", "$Forwarded"]
    assert inbox[-1].alt_key.startswith("m:") and inbox[-1].date.year == 2024
    got = src.fetch(folders[("INBOX",)], inbox[-1], "<ns-e1>")
    assert got.raw == raw("one", "1@x") and got.subject == "one" and got.flags == ["\\Seen"]
    # a rerun gives the same keys
    assert [i.key for i in src.iter_items(folders[("INBOX",)])] == [i.key for i in inbox]
    # paging: maxObjectsInGet is 3 in the fake, so 6 mails took several requests
    assert sum(1 for r in srv.http.requests if r.path == "/api") >= 3
    src.close()


def test_source_owner_prefers_trash_over_labels(srv):
    srv.mailboxes["mb-trash"] = {"id": "mb-trash", "name": "Trash", "parentId": None, "role": "trash"}
    srv.add_email(raw("t", "t@x"), ["mb-trash", "mb-work", "mb-inbox"], eid="et")
    src = opened(srv)
    by = {tuple(f.path): f for f in src.folders()}
    assert [i.key for i in src.iter_items(by[("Trash",)])] == ["eet"]
    assert list(src.iter_items(by[("INBOX",)])) == [] and list(src.iter_items(by[("Work",)])) == []


def test_source_refusals(srv):
    with pytest.raises(ConnectorError) as e:
        JmapSource(cfg(srv), {"password": "wrong"}).open()
    assert e.value.kind == ConnectorError.AUTH
    with pytest.raises(ConnectorError) as e:
        JmapSource({"url": srv.http.url + "/nothing", "login": "me"}, SECRETS).open()
    assert e.value.kind == ConnectorError.CONFIG
    with pytest.raises(ConnectorError) as e:
        JmapSource({"url": srv.url, "auth": "basic"}, SECRETS).open()  # basic without login
    assert e.value.kind == ConnectorError.CONFIG
    srv.other_host = True  # credentials must not follow the apiUrl to another host
    with pytest.raises(ConnectorError) as e:
        JmapSource(cfg(srv), SECRETS).open()
    assert e.value.kind == ConnectorError.CONFIG and "another host" in str(e.value)


def test_source_changed_since(srv):
    fill(srv)
    src = opened(srv)
    inbox = next(f for f in src.folders() if f.path == ["INBOX"])
    first = src.changed_since(inbox, None)
    assert first.reset and len(first.items) == 6 and first.cursor
    none = src.changed_since(inbox, first.cursor)
    assert not none.reset and none.items == []
    srv.add_email(raw("new", "9@x"), ["mb-inbox"], eid="e9")
    srv.add_email(raw("other", "10@x"), ["mb-sent"], eid="e10")
    more = src.changed_since(inbox, first.cursor)
    assert not more.reset and [i.key for i in more.items] == ["ee9"] and more.cursor != first.cursor
    srv.cannot_calculate = True
    again = src.changed_since(inbox, more.cursor)
    assert again.reset and len(again.items) == 7


def test_source_contacts_become_vcards(srv):
    srv.add_card({"uid": "urn:uuid:1", "name": {"components": [{"kind": "given", "value": "Joe"},
                                                             {"kind": "surname", "value": "Bloggs"}], "isOrdered": True},
                  "emails": {"e1": {"address": "joe@example.test", "contexts": {"work": True}}},
                  "phones": {"p1": {"number": "tel:+33123", "features": {"mobile": True}}},
                  "organizations": {"o1": {"name": "ACME"}}}, ["ab-1"])
    srv.add_card({"uid": "urn:uuid:g", "kind": "group", "name": {"full": "Team"}}, ["ab-1"])
    src = opened(srv)
    book = next(f for f in src.folders() if f.kind == CONTACTS)
    items = list(src.iter_items(book))
    assert len(items) == 2
    got = src.fetch(book, items[0], "<x>")
    text = got.raw.decode()
    assert "FN:Joe Bloggs" in text and "N:Bloggs;Joe;;;" in text and "EMAIL;TYPE=work:joe@example.test" in text
    assert "TEL;TYPE=cell:+33123" in text and "ORG:ACME" in text and "UID:urn:uuid:1" in text
    assert src.fetch(book, items[1], "<x>") is None  # a group is no person


# ------------------------------------------------------------------ target
def target(s, **extra):
    return JmapTarget(cfg(s, **extra), SECRETS).session()


def folders_to_plan():
    return [SourceFolder(["INBOX"], MAIL, "inbox"), SourceFolder(["Sent"], MAIL, "sent"),
            SourceFolder(["Drafts"], MAIL, "drafts"), SourceFolder(["Projects", "A/B"], MAIL)]


def test_target_plan_and_ensure(srv):
    t = target(srv)
    t.connect()
    plan = t.plan_folders(folders_to_plan())
    assert {k: (p.name, p.flag) for k, p in plan.items()} == {
        "INBOX": ("Inbox", None), "Sent": ("Sent", None), "Drafts": ("Drafts", "drafts"),
        "Projects/A\\/B": ("Projects/A_B", None)}
    for p in plan.values():
        t.ensure_folder(p.name, p.flag)
        t.ensure_folder(p.name, p.flag)  # idempotent
    names = {(b["name"], b["parentId"] is None, b.get("role")) for b in srv.mailboxes.values()}
    assert ("Drafts", True, "drafts") in names and ("Projects", True, None) in names
    child = next(b for b in srv.mailboxes.values() if b["name"] == "A_B")
    assert srv.mailboxes[child["parentId"]]["name"] == "Projects"
    root = target(srv, root_folder="Migrated")
    root.connect()
    assert root.plan_folders(folders_to_plan()[:2])["INBOX"].name == "Migrated/INBOX"


def test_target_mail_roundtrip(srv):
    from datetime import datetime, timezone
    t = target(srv)
    t.connect()
    item_raw = stamp_import_id(raw("hello", "h@x"), "<src-aa-1>")
    item = OutgoingItem(MAIL, item_raw, "<src-aa-1>", ["\\Seen", "$Forwarded"], datetime(2024, 3, 5, 8, 0, tzinfo=timezone.utc))
    assert t.find("Inbox", item) == []
    eid = t.put("Inbox", item)
    e = srv.emails[eid]
    assert e["mailboxIds"] == {"mb-inbox": True} and e["keywords"] == {"$seen": True, "$forwarded": True}
    assert e["receivedAt"] == "2024-03-05T08:00:00Z"
    assert t.find("Inbox", item) == [eid]
    assert t.find("Inbox", OutgoingItem(MAIL, b"", "<src-aa-2>")) == []   # exact: another id does not match
    assert t.find("Sent", item) == []
    t.put("Inbox", OutgoingItem(MAIL, stamp_import_id(raw("two", "2@x"), "<other-1>"), "<other-1>"))
    assert t.count_ours("Inbox", "src-aa") == 1
    assert t.read("Inbox", eid) == item_raw
    assert t.count_ours("Nowhere", "src-aa") == 0


def test_target_find_without_header_filter(srv):
    srv.no_header_filter = True
    t = target(srv)
    t.connect()
    item = OutgoingItem(MAIL, stamp_import_id(raw("x", "x@x"), "<ns-1>"), "<ns-1>")
    eid = t.put("Inbox", item)
    assert t.find("Inbox", item) == [eid]
    assert t.count_ours("Inbox", "ns") == 1


def test_target_errors(srv):
    t = target(srv)
    t.connect()
    item = OutgoingItem(MAIL, raw("x", "x@x"), "<ns-1>")
    with pytest.raises(ConnectorError) as e:
        t.put("Missing", item)
    assert e.value.kind == ConnectorError.FOLDER_MISSING
    srv.over_quota = True
    with pytest.raises(ConnectorError) as e:
        t.put("Inbox", item)
    assert e.value.kind == ConnectorError.QUOTA
    bad = JmapTarget(cfg(srv), {"password": "no"}).session()
    with pytest.raises(ConnectorError) as e:
        bad.connect()
    assert e.value.kind == ConnectorError.AUTH


def test_target_contacts_are_idempotent(srv):
    t = target(srv)
    t.connect()
    plan = t.plan_folders([SourceFolder(["Contacts"], CONTACTS)])
    name = plan["Contacts"].name
    t.ensure_folder(name)
    t.ensure_folder(name)
    assert sorted(b["name"] for b in srv.books.values()) == ["Contacts", "Personal"]
    vcard = convert.join_lines(["BEGIN:VCARD", "VERSION:3.0", "UID:abc", "FN:Ann Lee", "N:Lee;Ann;;;",
                                "EMAIL;TYPE=work:ann@example.test", "END:VCARD"]).encode()
    item = OutgoingItem(CONTACTS, vcard, "<ns-v1>", name="deadbeef.vcf")
    assert t.find(name, item) == []
    cid = t.put(name, item)
    card = srv.cards[cid]
    assert card["name"]["full"] == "Ann Lee" and card["emails"]["e1"]["address"] == "ann@example.test"
    assert card["uid"].startswith("urn:uuid:")
    assert t.find(name, item) == [cid]
    assert t.put(name, item) == cid                       # same name again: no second card
    assert len(srv.cards) == 1
    assert t.count_ours(name, "ns", {"deadbeef.vcf", "other.vcf"}) == 1


def test_catalogue_of_the_open_protocols():
    from app.connectors import registry
    cat = {c["kind"]: c for c in registry.catalogue()}
    assert cat["jmap"]["source"]["items"] == ["mail", "contacts"] and cat["jmap"]["target"]["items"] == ["mail", "contacts"]
    assert cat["google"]["source"]["items"] == ["mail", "calendar", "contacts"] and cat["google"]["target"] is None
    assert cat["files"]["source"]["items"] == ["mail", "calendar", "contacts"] and cat["files"]["target"] is None
    assert {f["name"] for f in cat["google"]["source"]["fields"] if f["secret"]} == {
        "service_account_key", "client_secret", "refresh_token"}
    assert {f["name"] for f in cat["jmap"]["source"]["fields"] if f["secret"]} == {"password"}
    assert "incremental" in cat["jmap"]["source"]["capabilities"] and "parallel_writes" in cat["jmap"]["target"]["capabilities"]
