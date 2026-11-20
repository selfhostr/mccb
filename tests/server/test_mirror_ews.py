"""Mirror mode of exchange_ews, against the simulated EWS server of fake_exchange.py, extended here (MirrorEws): change
log for SyncFolderItems (Create, Update, Delete, ReadFlagChange), ChangeKey, UpdateItem and DeleteItem that apply and
record what they are asked. The source server raises on any call that is not a read. These tests prove the logic of
the connector, not that real Exchange behaves like the simulation (see docs/exchange.md)."""
import base64
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

import httpx
import pytest

from app.connectors import ews, oauth, pim, registry
from app.connectors import exchange_base as xb
from app.connectors.base import (CALENDAR, CONTACTS, DELETE_SOFT, DELETE_TRASH, MAIL, MIRROR_DELETED_FLAG,
                                 READ_ONLY_SOURCE_METHODS, ConnectorError, OutgoingItem)
from tests.server import fake_exchange as fx
from tests.server.test_connectors_exchange import (CONTACT_XML, EXCEPTION_XML, MIME, RECURRING_XML, folder_of, mail_xml,
                                                   series)

UTC = timezone.utc
SRC = {"url": "https://src.example.test/EWS/Exchange.asmx", "login": "me@example.test", "auth": "basic"}
DST = {"url": "https://dst.example.test/EWS/Exchange.asmx", "login": "me@example.test", "auth": "basic"}
READ_OPS = {"GetFolder", "FindFolder", "FindItem", "GetItem", "SyncFolderItems", "SyncFolderHierarchy", "GetInboxRules"}


class MirrorEws(fx.FakeEws):
    """FakeEws with what the mirror needs. read_only: any operation that is not a read fails the test."""

    def __init__(self, read_only: bool = False):
        self.read_only = read_only
        self.mutations: list[str] = []
        self.log: list[tuple[str, str, str, str]] = []   # (folder, Create|Update|Delete|ReadFlagChange, item id, value)
        self.deletes: list[tuple[dict, list[str]]] = []
        self.updates: list[tuple[dict, list[ET.Element]]] = []
        self.conflict_next = False
        self.on_sync = None  # called before each SyncFolderItems answer (to change the folder meanwhile)
        super().__init__()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        op = ET.fromstring(request.content).find(f"{{{fx.NS_S}}}Body")[0].tag.rsplit("}", 1)[-1]
        if self.read_only and op not in READ_OPS:
            self.mutations.append(op)
            raise AssertionError(f"the source was asked to change something: {op}")
        return super().__call__(request)

    # -- seeding and changes "made by the user"
    def add_item(self, folder_id, xml, rid=None):
        rid = super().add_item(folder_id, xml, rid)
        self._ident(rid).set("ChangeKey", "ck1")
        self.log.append((folder_id, "Create", rid, ""))
        return rid

    def _ident(self, rid: str) -> ET.Element:
        return self.items[rid]["el"].find(fx.t("ItemId"))

    def change_key(self, rid: str) -> str:
        return self._ident(rid).get("ChangeKey")

    def touch(self, rid: str, subject: str | None = None) -> None:
        el = self.items[rid]["el"]
        if subject is not None:
            el.find(fx.t("Subject")).text = subject
        self._bump(rid)
        self.log.append((self.items[rid]["folder"], "Update", rid, ""))

    def _bump(self, rid: str) -> None:
        ident = self._ident(rid)
        ident.set("ChangeKey", f"ck{int((ident.get('ChangeKey') or 'ck0')[2:] or 0) + 1}")

    def mark_read(self, rid: str, read: bool) -> None:
        self.items[rid]["el"].find(fx.t("IsRead")).text = "true" if read else "false"
        self._bump(rid)
        self.log.append((self.items[rid]["folder"], "ReadFlagChange", rid, "true" if read else "false"))

    def remove(self, rid: str) -> None:
        folder = self.items.pop(rid)["folder"]
        self.log.append((folder, "Delete", rid, ""))

    # -- protocol
    def _op_SyncFolderItems(self, body) -> str:
        if self.on_sync is not None:
            self.on_sync()
        fid = self._folder_id(body.find(fx.m("SyncFolderId"))[0])
        state = body.findtext(fx.m("SyncState")) or ""
        if state and self.sync_invalid:
            self.sync_invalid = False
            return self._messages("SyncFolderItems", [self._error("ErrorInvalidSyncStateData")])
        if not state or state.startswith("full-"):  # first synchronization: everything there now, as Create
            offset, mark = (int(x) for x in state.split("-")[1:]) if state else (0, len(self.log))
            rows = self.in_folder(fid)
            page = rows[offset:offset + self.page_limit]
            done = offset + len(page) >= len(rows)
            changes = "".join(f"<t:Create>{self._serial(i['el'], ('MimeContent', 'Body'))}</t:Create>" for i in page)
            new = f"log-{mark}" if done else f"full-{offset + len(page)}-{mark}"
        else:
            start = int(state.split("-")[1])
            events = [(n, e) for n, e in enumerate(self.log) if n >= start and e[0] == fid]
            born = {e[2] for _n, e in events if e[1] == "Create"}  # created and deleted since: nothing to say
            events = [(n, e) for n, e in events if not (e[1] == "Delete" and e[2] in born)]
            page = events[:self.page_limit]
            done = len(page) == len(events)
            parts = []
            for _n, (_f, kind, rid, value) in page:
                if kind in ("Create", "Update"):
                    if rid in self.items and self.items[rid]["folder"] == fid:
                        parts.append(f"<t:{kind}>{self._serial(self.items[rid]['el'], ('MimeContent', 'Body'))}</t:{kind}>")
                elif kind == "Delete":
                    parts.append(f'<t:Delete><t:ItemId Id="{rid}"/></t:Delete>')
                else:
                    parts.append(f'<t:ReadFlagChange><t:ItemId Id="{rid}"/><t:IsRead>{value}</t:IsRead></t:ReadFlagChange>')
            changes = "".join(parts)
            new = f"log-{(page[-1][0] + 1) if page else start}" if not done else f"log-{len(self.log)}"
        return self._messages("SyncFolderItems", [
            '<m:SyncFolderItemsResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>'
            f"<m:SyncState>{new}</m:SyncState><m:IncludesLastItemInRange>{'true' if done else 'false'}"
            f"</m:IncludesLastItemInRange><m:Changes>{changes}</m:Changes></m:SyncFolderItemsResponseMessage>"])

    def _op_UpdateItem(self, body) -> str:
        changes = list(body.find(fx.m("ItemChanges")))
        self.updates.append((dict(body.attrib), changes))
        self.last_update_attrs = dict(body.attrib)
        parts = []
        for change in changes:
            ident = change.find(fx.t("ItemId"))
            rid = ident.get("Id")
            if rid.startswith("occ-"):
                self.updated_occurrences[rid] = change
            elif rid not in self.items:
                parts.append(self._error("ErrorItemNotFound"))
                continue
            else:
                if self.conflict_next or (body.get("ConflictResolution") != "AlwaysOverwrite"
                                          and ident.get("ChangeKey") != self.change_key(rid)):
                    self.conflict_next = False
                    parts.append(self._error("ErrorIrresolvableConflict"))
                    continue
                self._apply(self.items[rid]["el"], change.find(fx.t("Updates")))
                self._bump(rid)
            parts.append('<m:UpdateItemResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>'
                         "</m:UpdateItemResponseMessage>")
        return self._messages("UpdateItem", parts)

    @staticmethod
    def _apply(el: ET.Element, updates: ET.Element) -> None:
        for u in updates:
            uri = u.find(fx.t("FieldURI"))
            ext = u.find(fx.t("ExtendedFieldURI"))
            if ext is not None:
                tag = ext.get("PropertyTag")
                for p in el.findall(fx.t("ExtendedProperty")):
                    if p.find(fx.t("ExtendedFieldURI")).get("PropertyTag", "").lower() == tag.lower():
                        el.remove(p)
                if u.tag == fx.t("SetItemField"):
                    el.append(u[1].find(fx.t("ExtendedProperty")))
                continue
            name = uri.get("FieldURI").split(":")[1]
            for old in el.findall(fx.t(name)):
                el.remove(old)
            if u.tag == fx.t("SetItemField"):
                el.append(u[1].find(fx.t(name)))

    def _op_DeleteItem(self, body) -> str:
        ids = [ref.get("Id") for ref in body.find(fx.m("ItemIds"))]
        self.deletes.append((dict(body.attrib), ids))
        parts = []
        for rid in ids:
            if rid.startswith("occ-"):
                self.deleted_occurrences.append(rid)
            elif rid not in self.items:
                parts.append(self._error("ErrorItemNotFound"))
                continue
            elif body.get("DeleteType") == "MoveToDeletedItems":  # a moved item gets another id in Exchange
                item = self.items.pop(rid)
                self.log.append((item["folder"], "Delete", rid, ""))
                self.items[f"trashed-{rid}"] = dict(item, folder="id-deleteditems", was=rid)
            else:
                self.items.pop(rid)
            parts.append('<m:DeleteItemResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>'
                         "</m:DeleteItemResponseMessage>")
        return self._messages("DeleteItem", parts)


class Router:
    def __init__(self, **servers):
        self.servers = servers

    def __call__(self, request: httpx.Request) -> httpx.Response:
        return self.servers[request.url.host.split(".")[0]](request)


@pytest.fixture
def waits(monkeypatch):
    out: list[float] = []
    monkeypatch.setattr(ews, "_sleep", out.append)
    return out


@pytest.fixture
def servers(monkeypatch, waits):
    src, dst = MirrorEws(read_only=True), MirrorEws()
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(Router(src=src, dst=dst)))
    return src, dst


def source():
    s = ews.EwsSource(dict(SRC), {"password": "pw"})
    s.open()
    return s


def session(**config):
    s = ews.EwsTarget({**DST, **config}, {"password": "pw"}).session()
    s.connect()
    return s


def out_item(kind, raw, import_id="<src-1-a>", flags=()):
    return OutgoingItem(kind, raw, import_id, list(flags))


def seed(fake):
    fake.add_folder("work", "Work", "inbox")
    for i in range(5):
        fake.add_item("id-inbox", mail_xml(i, "false"), rid=f"m{i}")
    for i in range(3):
        fake.add_item("id-work", mail_xml(10 + i), rid=f"w{i}")
    fake.add_item("id-calendar", RECURRING_XML.format(t=fx.NS_T), rid="rec1")
    fake.add_item("id-calendar", EXCEPTION_XML.format(t=fx.NS_T), rid="exc-1")
    fake.items["exc-1"]["folder"] = "elsewhere"  # exceptions are not items of the calendar folder
    fake.add_item("id-contacts", CONTACT_XML.format(t=fx.NS_T), rid="ct1")


def ids_of(fake, folder):
    return sorted(i["id"] for i in fake.in_folder(folder))


# ------------------------------------------------------------------ catalogue
def test_catalogue_declares_the_mirror():
    cat = {c["kind"]: c for c in registry.catalogue()}["exchange_ews"]
    assert "mirror" in cat["source"]["capabilities"]
    target = set(cat["target"]["capabilities"])
    assert {"inventory", "delete", "update", "set_flags"} <= target
    assert "move" not in target and "readback" not in target


# ------------------------------------------------------------------ source: read only
def test_the_source_is_never_written(servers):
    src_fake, _ = servers
    seed(src_fake)
    s = source()
    folders = s.folders()
    keys = {}
    for f in folders:
        listing = s.mirror_listing(f)
        assert listing.complete, (f.path, listing.problem)
        changes = s.changed_since(f, None)
        for it in listing.items:
            keys[it.key] = it.ref
            s.fetch(f, it, "<x>")
        if changes.cursor:
            s.changed_since(f, changes.cursor)
    src_fake.touch("rec1", "Renamed")
    src_fake.mark_read("m1", True)
    src_fake.remove("m2")
    inbox = folder_of(folders, "Inbox")
    s.changed_since(inbox, "log-0")
    s.exists(inbox, list(keys), dict(keys))
    for key in keys:
        s.key_epoch(key)
    s.close()
    assert src_fake.mutations == []
    assert set(src_fake.calls) <= READ_OPS
    # what the mirror may call on a source, and what this source implements of it
    assert {"mirror_listing", "key_epoch"} <= READ_ONLY_SOURCE_METHODS


def test_the_read_only_fake_does_catch_a_write(servers):
    src_fake, _ = servers
    s = ews.EwsSession(dict(SRC), {"password": "pw"}, None)
    s.connect()
    with pytest.raises(AssertionError):
        s.put("mail:@inbox", out_item(MAIL, MIME))
    assert src_fake.mutations == ["CreateItem"]


# ------------------------------------------------------------------ source: listing, versions, changes
def test_mirror_listing_is_complete_and_versioned(servers):
    src_fake, _ = servers
    seed(src_fake)
    s = source()
    folders = s.folders()
    inbox, cal = folder_of(folders, "Inbox"), folder_of(folders, "Calendar")
    listing = s.mirror_listing(inbox)
    assert listing.complete and listing.problem is None and listing.epoch == xb.KEY_EPOCH
    assert sorted(i.ref for i in listing.items) == [f"m{i}" for i in range(5)]  # three pages of two
    assert "FindItem" not in src_fake.calls and src_fake.calls.count("SyncFolderItems") == 3
    assert {i.key for i in listing.items} == {i.key for i in s.iter_items(inbox)}  # same keys as the copy
    assert all(i.version is None and "\\Flagged" in i.flags for i in listing.items)  # mail: flags, no version
    events = s.mirror_listing(cal)
    assert [(i.ref, i.version) for i in events.items] == [("rec1", "ck1")]  # the master, with its ChangeKey
    src_fake.touch("rec1", "Changed")
    assert [i.version for i in s.mirror_listing(cal).items] == ["ck2"]
    src_fake.mark_read("m1", True)
    seen = {i.ref: i.flags for i in s.mirror_listing(inbox).items}
    assert seen["m1"][0] == "\\Seen" and "\\Seen" not in seen["m0"]


def test_listing_not_complete_when_the_folder_changes_meanwhile(servers):
    src_fake, _ = servers
    seed(src_fake)
    s = source()
    inbox = folder_of(s.folders(), "Inbox")
    added = []

    def arrive():
        if not added:
            added.append(src_fake.add_item("id-inbox", mail_xml(50)))
    src_fake.on_sync = arrive
    listing = s.mirror_listing(inbox)
    assert not listing.complete and "changed during the listing" in listing.problem
    src_fake.on_sync = None
    assert s.mirror_listing(inbox).complete


def test_listing_not_complete_when_the_count_differs(servers, monkeypatch):
    src_fake, _ = servers
    seed(src_fake)
    s = source()
    inbox = folder_of(s.folders(), "Inbox")
    monkeypatch.setattr(s, "_total", lambda folder: 6)
    listing = s.mirror_listing(inbox)
    assert not listing.complete and "5 items listed, the folder holds 6" in listing.problem


def test_changed_since_reports_read_flags_and_confirmed_deletions(servers):
    src_fake, _ = servers
    seed(src_fake)
    s = source()
    inbox = folder_of(s.folders(), "Inbox")
    first = s.changed_since(inbox, None)
    assert first.reset and len(first.items) == 5 and first.removed == []
    src_fake.mark_read("m1", True)
    src_fake.remove("m2")
    src_fake.add_item("id-inbox", mail_xml(9), rid="m9")
    src_fake.add_item("id-inbox", mail_xml(8), rid="m8")
    src_fake.remove("m8")  # created then deleted between two passes: neither copied nor reported
    src_fake.log.append(("id-inbox", "Delete", "m3", ""))  # a deletion the server reports but GetItem still finds
    calls = len(src_fake.calls)
    src_fake.page_limit = 10  # the simulation collapses a creation and a deletion only within one answer
    second = s.changed_since(inbox, first.cursor)
    assert not second.reset
    got = {i.ref: i for i in second.items}
    assert set(got) == {"m1", "m9"} and got["m1"].flags[0] == "\\Seen"  # re-read: a full line
    assert second.removed == [xb.item_key(inbox.path, "m2")]  # m3 still there: not removed
    assert "GetItem" in src_fake.calls[calls:]


def test_exists_reads_each_item_with_its_recorded_id(servers):
    src_fake, _ = servers
    seed(src_fake)
    s = source()
    inbox = folder_of(s.folders(), "Inbox")
    k0, k1, k2 = (xb.item_key(inbox.path, r) for r in ("m0", "m1", "m2"))
    src_fake.remove("m1")
    other = xb.item_key(inbox.path, "m3")
    calls = len(src_fake.calls)
    got = s.exists(inbox, [k0, k1, k2, other], {k0: "m0", k1: "m1", k2: None, other: "m4"})
    # m1: ErrorItemNotFound -> gone. No id recorded, or an id that does not give back the key: kept
    assert got == {k0: True, k1: False, k2: True, other: True}
    assert src_fake.calls[calls:] == ["GetItem"]  # one batch, read only
    assert s.exists(inbox, [k1], {k1: ["m1"]}) == {k1: True}  # not an id: cannot tell
    assert s.exists(inbox, [k1], None) == {k1: True}
    work = folder_of(s.folders(), "Inbox", "Work")
    assert s.exists(work, [k1], {k1: "m1"}) == {k1: True}  # a key of another folder: cannot tell
    src_fake.fail_op["GetItem"] = "ErrorInternalServerError"
    with pytest.raises(ConnectorError):
        s.exists(inbox, [k1], {k1: "m1"})
    src_fake.fail_op["GetItem"] = "ErrorInvalidIdMalformed"
    with pytest.raises(ConnectorError):  # not "not found": no deletion on a doubt
        s.exists(inbox, [k1], {k1: "m1"})
    assert src_fake.mutations == []


# ------------------------------------------------------------------ source: renamed or moved folders
def tracked(s, folder):
    return [i.key for i in s.mirror_listing(folder).items]


def test_renamed_folder_suspends_its_deletions(servers):
    src_fake, _ = servers
    seed(src_fake)
    s = source()
    work = folder_of(s.folders(), "Inbox", "Work")
    keys = tracked(s, work)
    inbox_keys = tracked(s, folder_of(s.folders(), "Inbox"))
    s.close()
    # the user renames Work, then makes a new, empty, folder Work
    src_fake.folders["id-work"]["name"] = "Work 2023"
    src_fake.add_folder("work2", "Work", "inbox")
    src_fake.remove("w2")  # this one is really gone
    s = source()
    folders = s.folders()
    listing = s.mirror_listing(folder_of(folders, "Inbox", "Work"))
    assert listing.complete and listing.items == []  # complete and empty: without the epoch, 3 deletions
    epochs = {k: s.key_epoch(k) for k in keys}
    renamed = [k for k in keys if epochs[k] != listing.epoch]
    assert len(renamed) == 2 and all("renamed-or-moved" in epochs[k] for k in renamed)
    assert epochs[xb.item_key(["Inbox", "Work"], "w2")] == listing.epoch  # not seen anywhere: really gone
    assert {s.key_epoch(k) for k in inbox_keys} == {xb.KEY_EPOCH}  # other folders are not concerned
    assert src_fake.mutations == []


def test_moved_folder_suspends_its_deletions_even_listed_last(servers):
    src_fake, _ = servers
    seed(src_fake)
    s = source()
    keys = tracked(s, folder_of(s.folders(), "Inbox", "Work"))
    src_fake.folders["id-work"]["parent"] = "id-msgfolderroot"  # moved out of the inbox, nothing at the old place
    s = source()
    s.folders()
    assert all("renamed-or-moved" in s.key_epoch(k) for k in keys)  # the index lists the folders not seen yet


def test_unverifiable_rename_blocks_deletions(servers):
    src_fake, _ = servers
    seed(src_fake)
    s = source()
    keys = tracked(s, folder_of(s.folders(), "Inbox", "Work"))
    s = source()
    s.folders()
    src_fake.busy_next = 100  # the folders cannot be listed: no way to tell
    assert s.key_epoch(keys[0]) == f"{xb.KEY_EPOCH}:unverified"
    src_fake.busy_next = 0
    assert s.key_epoch(keys[0]) == f"{xb.KEY_EPOCH}:unverified"  # for the whole run
    assert s.key_epoch("not-an-ews-key") == xb.KEY_EPOCH


# ------------------------------------------------------------------ target: only ours
def test_inventory_lists_only_our_items(servers):
    _, dst = servers
    t = session()
    a = t.put("mail:@inbox", out_item(MAIL, MIME, "<src-1-a>", ["\\Seen"]))
    b = t.put("mail:@inbox", out_item(MAIL, MIME, "<src-1-b>", ["\\Flagged"]))
    c = t.put("mail:@inbox", out_item(MAIL, MIME, "<src-1-c>"))
    t.put("mail:@inbox", out_item(MAIL, MIME, "<SRC-1-d>"))  # the server ignores case, the inventory does not
    t.put("mail:@inbox", out_item(MAIL, MIME, "<src-2-a>"))
    dst.add_item("id-inbox", mail_xml(1), rid="theirs")
    inv = t.inventory("mail:@inbox", "src-1")
    assert set(inv) == {"<src-1-a>", "<src-1-b>", "<src-1-c>"}  # three pages of two
    assert [r.ref for r in inv["<src-1-a>"]] == [a] and inv["<src-1-b>"][0].ref == b and inv["<src-1-c>"][0].ref == c
    assert inv["<src-1-a>"][0].flags == ["\\Seen"] and inv["<src-1-b>"][0].flags == ["\\Flagged"]
    assert inv["<src-1-a>"][0].version == "ck"
    assert t.inventory("calendar:@default", "src-1") == {}


def test_inventory_refuses_a_folder_changing_meanwhile(servers, monkeypatch):
    _, dst = servers
    t = session()
    for i in range(3):
        t.put("mail:@inbox", out_item(MAIL, MIME, f"<src-1-{i}>"))
    real = dst._op_FindItem
    calls = []

    def find(body):
        calls.append(1)
        if len(calls) == 2:
            dst.add_item("id-inbox", mail_xml(1).replace("<t:IsDraft>", ews._ext(ews.IMPORT_URI, "<src-1-9>") + "<t:IsDraft>"))
        return real(body)
    monkeypatch.setattr(dst, "_op_FindItem", find)
    with pytest.raises(ConnectorError) as err:
        t.inventory("mail:@inbox", "src-1")
    assert err.value.retryable


def test_never_deletes_what_mccb_did_not_write(servers):
    _, dst = servers
    t = session()
    ours = t.put("mail:@inbox", out_item(MAIL, MIME))
    dst.add_item("id-inbox", mail_xml(1), rid="theirs")
    t.inventory("mail:@inbox", "src-1")
    with pytest.raises(ConnectorError) as err:
        t.delete("mail:@inbox", "theirs")
    assert err.value.kind == ConnectorError.REJECTED
    assert t.delete_many("mail:@inbox", ["theirs"]) == {}  # left out of the answer: a failure, nothing done
    assert t.delete_many("mail:@inbox", [ours], {ours: "<src-1-zzz>"}) == {}  # not the expected copy
    with pytest.raises(ConnectorError):
        t.delete("mail:@inbox", ours, "<src-1-zzz>")
    with pytest.raises(ConnectorError):
        t.update("mail:@inbox", "theirs", out_item(MAIL, MIME))
    with pytest.raises(ConnectorError):
        t.set_flags("mail:@inbox", "theirs", ["\\Seen"], [])
    assert ids_of(dst, "id-inbox") == sorted([ours, "theirs"])  # nothing deleted
    assert dst.deletes == [] and dst.updates == [] and dst.items["theirs"]["el"].findtext(fx.t("IsRead")) == "true"
    # an item whose import id changed since the inventory is left alone too
    for p in dst.items[ours]["el"].findall(fx.t("ExtendedProperty")):
        if p.find(fx.t("ExtendedFieldURI")).get("PropertyName") == xb.IMPORT_PROP:
            p.find(fx.t("Value")).text = "<src-1-other>"
    with pytest.raises(ConnectorError):
        t.delete("mail:@inbox", ours)
    assert ours in dst.items


def test_delete_many_does_ours_and_leaves_the_others(servers):
    _, dst = servers
    t = session()
    a = t.put("mail:@inbox", out_item(MAIL, MIME, "<src-1-a>"))
    b = t.put("mail:@inbox", out_item(MAIL, MIME, "<src-1-b>"))
    dst.add_item("id-inbox", mail_xml(1), rid="theirs")
    t.inventory("mail:@inbox", "src-1")
    got = t.delete_many("mail:@inbox", [a, "theirs", b, "gone"], {a: "<src-1-a>", b: "<src-1-x>"})
    assert got == {a: DELETE_TRASH, "gone": DELETE_TRASH}  # b: not the expected copy, theirs: not ours
    assert ids_of(dst, "id-inbox") == sorted([b, "theirs"]) and dst.deletes[-1][1] == [a]


def test_inventory_by_resource_name_for_events_contacts_tasks(servers):
    import hashlib
    _, dst = servers
    t = session()
    rid = t.put("contacts:@default", out_item(CONTACTS, pim.contact_to_vcard(pim.Contact(uid="c", full_name="A")), "<src-1-c>"))
    name = hashlib.sha256(b"<src-1-c>").hexdigest()[:32] + ".vcf"  # what the runner records (items.hash)
    inv = t.inventory("contacts:@default", "src-1", {name, "other.vcf"})
    assert [r.ref for r in inv[name]] == [rid] and "other.vcf" not in inv
    assert t.delete("contacts:@default", rid, name) == DELETE_TRASH  # the name stands for the expected import id
    rid = t.put("contacts:@default", out_item(CONTACTS, pim.contact_to_vcard(pim.Contact(uid="d", full_name="B")), "<src-1-d>"))
    with pytest.raises(ConnectorError):
        t.delete("contacts:@default", rid, name)
    assert rid in dst.items


# ------------------------------------------------------------------ target: deletions
def test_delete_moves_to_deleted_items_never_hard(servers):
    _, dst = servers
    t = session()
    mail = t.put("mail:@inbox", out_item(MAIL, MIME))
    event = t.put("calendar:@default", out_item(CALENDAR, pim.event_to_ics(series()), "<src-1-e>"))
    hard_before = [ids for attrs, ids in dst.deletes if attrs["DeleteType"] == "HardDelete"]
    assert all(i.startswith("occ-") for ids in hard_before for i in ids)  # writing a series: its exdates only
    dst.deletes.clear()
    assert t.delete_mode("mail:@inbox") == DELETE_TRASH
    assert t.delete("mail:@inbox", mail) == DELETE_TRASH
    assert t.delete("calendar:@default", event) == DELETE_TRASH
    assert ids_of(dst, "id-deleteditems") == sorted([f"trashed-{mail}", f"trashed-{event}"])  # recoverable
    assert ids_of(dst, "id-inbox") == [] and ids_of(dst, "id-calendar") == []
    for attrs, _ids in dst.deletes:
        assert attrs == {"DeleteType": "MoveToDeletedItems", "SendMeetingCancellations": "SendToNone",
                         "AffectedTaskOccurrences": "AllOccurrences"}
    cats = [c.text for c in dst.items[f"trashed-{mail}"]["el"].find(fx.t("Categories"))]
    assert cats == [MIRROR_DELETED_FLAG]  # found again to undo
    assert dst.items[f"trashed-{event}"]["el"].find(fx.t("Categories")) is not None  # the series kept its own
    calls = len(dst.deletes)
    assert t.delete("mail:@inbox", mail, "<src-1-a>") == DELETE_TRASH  # already gone: not an error, nothing sent
    assert len(dst.deletes) == calls


def test_in_deleted_items_only_marks(servers):
    _, dst = servers
    t = session()
    rid = t.put("mail:@deleteditems", out_item(MAIL, MIME))
    assert t.delete_mode("mail:@deleteditems") == DELETE_SOFT
    assert t.delete("mail:@deleteditems", rid) == DELETE_SOFT
    assert rid in dst.items and dst.deletes == []  # deleting there would be final
    assert [c.text for c in dst.items[rid]["el"].find(fx.t("Categories"))] == [MIRROR_DELETED_FLAG]


def test_delete_many_goes_by_batches(servers, monkeypatch):
    _, dst = servers
    monkeypatch.setattr(ews, "GET_BATCH", 2)
    t = session()
    refs = [t.put("contacts:@default", out_item(CONTACTS, pim.contact_to_vcard(pim.Contact(uid=f"c{i}", full_name=f"C {i}")),
                                                f"<src-1-c{i}>")) for i in range(5)]
    assert t.delete_many("contacts:@default", refs) == {r: DELETE_TRASH for r in refs}
    assert [len(ids) for _a, ids in dst.deletes] == [2, 2, 1] and ids_of(dst, "id-contacts") == []
    assert dst.updates == []  # contacts are not marked: only mails are


# ------------------------------------------------------------------ target: flags and updates
def flags_of(dst, rid):
    el = dst.items[rid]["el"]
    props = {p.find(fx.t("ExtendedFieldURI")).get("PropertyTag"): p.findtext(fx.t("Value"))
             for p in el.findall(fx.t("ExtendedProperty")) if p.find(fx.t("ExtendedFieldURI")).get("PropertyTag")}
    return el.findtext(fx.t("IsRead")), props.get("0x1090"), props.get("0x1081")


def test_set_flags_with_change_key_and_no_mail_sent(servers):
    _, dst = servers
    t = session()
    rid = t.put("mail:@inbox", out_item(MAIL, MIME))
    t.inventory("mail:@inbox", "src-1")
    assert flags_of(dst, rid) == ("false", None, None)
    t.set_flags("mail:@inbox", rid, ["\\Seen", "\\Flagged", "\\Answered", "$Custom"], [])
    assert flags_of(dst, rid) == ("true", "2", "102")
    attrs, changes = dst.updates[-1]
    assert attrs == {"MessageDisposition": "SaveOnly", "ConflictResolution": "AutoResolve",
                     "SendMeetingInvitationsOrCancellations": "SendToNone"}
    assert changes[0].find(fx.t("ItemId")).get("ChangeKey") == "ck"  # the one just read
    t.set_flags("mail:@inbox", rid, [], ["\\Flagged", "\\Answered", "\\Seen"])
    assert flags_of(dst, rid) == ("false", None, None)
    assert dst.change_key(rid) == "ck2"
    t.set_flags("mail:@inbox", rid, [], [])  # nothing to change: nothing sent
    assert len(dst.updates) == 2
    dst.conflict_next = True
    with pytest.raises(ConnectorError) as err:
        t.set_flags("mail:@inbox", rid, ["\\Seen"], [])
    assert err.value.retryable


def test_update_replaces_a_recurring_series(servers):
    _, dst = servers
    t = session()
    old = t.put("calendar:@default", out_item(CALENDAR, pim.event_to_ics(series()), "<src-1-e>"))
    t.inventory("calendar:@default", "src-1")
    e = series()
    e.subject = "Weekly, new room"
    new = t.update("calendar:@default", old, out_item(CALENDAR, pim.event_to_ics(e), "<src-1-e>"))
    assert new != old and f"trashed-{old}" in dst.items  # the old series with all its occurrences, recoverable
    trash = [(a, ids) for a, ids in dst.deletes if ids == [old]]
    assert trash and trash[0][0]["DeleteType"] == "MoveToDeletedItems" and trash[0][0]["SendMeetingCancellations"] == "SendToNone"
    el = dst.items[new]["el"]
    assert el.findtext(fx.t("Subject")) == "Weekly, new room" and dst.items[new]["attrs"]["SendMeetingInvitations"] == "SendToNone"
    assert f"occ-{new}-1" in dst.deleted_occurrences and f"occ-{new}-2" in dst.updated_occurrences  # exceptions again
    # the new series is written while the old one (same UID) is still there: its occurrences are left alone
    assert dst.deleted_occurrences.count(f"occ-{old}-1") == 1
    assert [r.ref for r in t.inventory("calendar:@default", "src-1")["<src-1-e>"]] == [new]
    with pytest.raises(ConnectorError) as err:  # not the copy of this item: left alone
        t.update("calendar:@default", new, out_item(CALENDAR, pim.event_to_ics(e), "<src-1-other>"))
    assert err.value.kind == ConnectorError.REJECTED and new in dst.items


def test_update_writes_the_new_copy_before_trashing_the_old(servers, monkeypatch):
    """M3: the new copy is written first; when that fails the old one stays where it is (not only in the trash)."""
    _, dst = servers
    t = session()
    c = pim.Contact(uid="c", full_name="Ann")
    rid = t.put("contacts:@default", out_item(CONTACTS, pim.contact_to_vcard(c), "<src-1-c>"))
    t.inventory("contacts:@default", "src-1")

    def refused(*a, **kw):
        raise ConnectorError("CreateItem refused", ConnectorError.TRANSIENT)
    monkeypatch.setattr(t, "_create", refused)  # every write of a new item
    c.full_name = "Ann Lee"
    with pytest.raises(ConnectorError):
        t.update("contacts:@default", rid, out_item(CONTACTS, pim.contact_to_vcard(c), "<src-1-c>"))
    assert rid in dst.items and dst.deletes == [] and f"trashed-{rid}" not in dst.items


def test_update_of_an_item_already_gone_writes_it_again(servers):
    _, dst = servers
    t = session()
    c = pim.Contact(uid="c", full_name="Ann")
    rid = t.put("contacts:@default", out_item(CONTACTS, pim.contact_to_vcard(c), "<src-1-c>"))
    dst.items.pop(rid)
    c.full_name = "Ann Lee"
    new = t.update("contacts:@default", rid, out_item(CONTACTS, pim.contact_to_vcard(c), "<src-1-c>"))
    assert dst.items[new]["el"].findtext(fx.t("DisplayName")) == "Ann Lee" and dst.deletes == []


# ------------------------------------------------------------------ throttling
def test_throttling_waits_then_gives_up_without_a_partial_change(servers, waits):
    src_fake, dst = servers
    seed(src_fake)
    s = source()
    inbox = folder_of(s.folders(), "Inbox")
    src_fake.busy_next = 3
    assert s.mirror_listing(inbox).complete and waits == [1.5, 1.5, 1.5]
    t = session()
    rid = t.put("mail:@inbox", out_item(MAIL, MIME))
    waits.clear()
    dst.busy_next = 2
    assert t.delete("mail:@inbox", rid) == DELETE_TRASH and waits == [1.5, 1.5]
    rid = t.put("mail:@inbox", out_item(MAIL, MIME, "<src-1-b>"))
    dst.busy_next = 100
    with pytest.raises(ConnectorError) as err:
        t.delete("mail:@inbox", rid)
    assert err.value.kind == ConnectorError.TRANSIENT
    assert dst.items[rid]["folder"] == "id-inbox" and dst.items[rid]["el"].find(fx.t("Categories")) is None
    src_fake.busy_next = 100
    with pytest.raises(ConnectorError):  # an error, never an empty listing that would delete everything
        s.mirror_listing(inbox)


# ------------------------------------------------------------------ time zones of all-day events
ALL_DAY = ('<t:CalendarItem xmlns:t="{t}"><t:Subject>Off</t:Subject><t:UID>ad</t:UID><t:Start>{start}</t:Start>'
           "<t:End>{end}</t:End><t:IsAllDayEvent>true</t:IsAllDayEvent>{zone}</t:CalendarItem>")


def all_day(start, end, zone=""):
    return ET.fromstring(ALL_DAY.format(t=fx.NS_T, start=start, end=end, zone=zone))


def test_all_day_event_without_a_zone_keeps_its_day():
    for start, end in (("2024-06-30T22:00:00Z", "2024-07-01T22:00:00Z"),   # Paris, summer
                       ("2024-07-01T04:00:00Z", "2024-07-02T04:00:00Z"),   # New York
                       ("2024-07-01T00:00:00Z", "2024-07-02T00:00:00Z")):  # UTC
        e = ews.event_from_xml(all_day(start, end), [])
        assert (e.start, e.end) == (date(2024, 7, 1), date(2024, 7, 2)), start
    e = ews.event_from_xml(all_day("2024-06-30T22:00:00Z", "2024-07-01T22:00:00Z",
                                   '<t:StartTimeZone Id="Romance Standard Time"/>'), [])
    assert (e.start, e.tz) == (date(2024, 7, 1), "Europe/Paris")
    assert ews.all_day_date(datetime(2024, 6, 30, 22, tzinfo=UTC), None) == date(2024, 7, 1)


def test_all_day_event_rewritten_again_and_again_stays_on_its_day(servers):
    _, dst = servers
    t = session(time_zone="Europe/Paris")
    raw = (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:u\r\nDTSTAMP:20240101T000000Z\r\n"
           b"DTSTART;VALUE=DATE:20240701\r\nDTEND;VALUE=DATE:20240702\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    rid = t.put("calendar:@default", out_item(CALENDAR, raw))
    for _ in range(3):  # read back without its zone (what a lossy path gives), written again
        el = dst.items[rid]["el"]
        for z in el.findall(fx.t("StartTimeZone")) + el.findall(fx.t("EndTimeZone")):
            el.remove(z)
        e = ews.event_from_xml(el, [])
        assert (e.start, e.end) == (date(2024, 7, 1), date(2024, 7, 2))
        rid = t.update("calendar:@default", rid, out_item(CALENDAR, pim.event_to_ics(e)))
    assert dst.items[rid]["el"].findtext(fx.t("Start")) == "2024-06-30T22:00:00Z"


# ------------------------------------------------------------------ one pass, both sides
def test_one_mirror_pass_end_to_end(servers):
    """What the mirror does with this connector, step by step: copy, then a deletion and a change at the source."""
    src_fake, dst = servers
    seed(src_fake)
    s, t = source(), session()
    folders = {tuple(f.path): f for f in s.folders()}
    plan = t.plan_folders(list(folders.values()))
    from app.server.runner import folder_key
    copied = {}
    for path in (("Inbox",), ("Calendar",)):
        f = folders[path]
        for it in s.mirror_listing(f).items:
            got = s.fetch(f, it, f"<src-1-{it.key}>")
            kind = f.kind
            copied[it.key] = (it.version, t.put(plan[folder_key(list(path))].name,
                                                OutgoingItem(kind, got.raw, f"<src-1-{it.key}>", got.flags)))
    src_fake.remove("m4")
    src_fake.touch("rec1", "Standup, new time")
    inbox, cal = folders[("Inbox",)], folders[("Calendar",)]
    listing = s.mirror_listing(inbox)
    gone = set(k for k in copied if k.startswith(xb.folder_hash(["Inbox"]))) - {i.key for i in listing.items}
    assert listing.complete and gone == {xb.item_key(["Inbox"], "m4")}
    assert all(s.key_epoch(k) == listing.epoch for k in gone)  # not renamed: the deletion may go on
    assert s.exists(inbox, list(gone), {k: "m4" for k in gone}) == {k: False for k in gone}  # confirmed by GetItem
    inv = t.inventory("mail:@inbox", "src-1")
    for k in gone:
        for r in inv[f"<src-1-{k}>"]:
            t.delete("mail:@inbox", r.ref)
    events = s.mirror_listing(cal).items
    for it in events:
        version, ref = copied[it.key]
        if it.version != version:
            got = s.fetch(cal, it, f"<src-1-{it.key}>")
            t.update("calendar:@default", ref, OutgoingItem(CALENDAR, got.raw, f"<src-1-{it.key}>"))
    assert len(dst.in_folder("id-inbox")) == 4 and len(dst.in_folder("id-deleteditems")) == 2
    assert [i["el"].findtext(fx.t("Subject")) for i in dst.in_folder("id-calendar")] == ["Standup, new time"]
    assert src_fake.mutations == [] and set(src_fake.calls) <= READ_OPS
    assert all(a["DeleteType"] != "HardDelete" or all(i.startswith("occ-") for i in ids) for a, ids in dst.deletes)
    assert base64.b64decode(dst.in_folder("id-inbox")[0]["el"].findtext(fx.t("MimeContent"))) == MIME
