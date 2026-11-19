"""Mirror mode of m365_graph, against two simulated mailboxes of one tenant: a source that fails the test on any
request other than GET, and a target that records every change. The guards: the source is never written, nothing is
ever permanently deleted, an absence is confirmed by a GET before a deletion, only items mccb wrote are touched,
events are left alone when Graph would mail their attendees, a move is not a deletion, 429 is waited out."""
from __future__ import annotations

import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.connectors import m365, oauth, pim
from app.connectors.base import (CALENDAR, CAP_DELETE, CAP_INVENTORY, CAP_MIRROR, CAP_MOVE, CAP_SET_FLAGS, CAP_UPDATE,
                                 CONTACTS, DELETE_HARD, DELETE_SOFT, DELETE_TRASH, MAIL, MIRROR_DELETED_FLAG, TASKS,
                                 ConnectorError, OutgoingItem, SourceFolder)
from tests.server import fake_exchange as fx

UTC = timezone.utc
TENANT = {"tenant_id": "contoso", "client_id": "app-1"}
SRC, DST = "src@contoso.test", "dst@contoso.test"
NS = "ns1"
MIME = (b"From: a@example.test\r\nTo: b@example.test\r\nSubject: Hello\r\nMessage-ID: <1@example.test>\r\n"
        b"Date: Fri, 01 Mar 2024 12:00:00 +0000\r\n\r\nbody\r\n")


class MirrorGraph(fx.FakeGraph):
    """FakeGraph plus what the mirror uses: DELETE (to Deleted Items) and PATCH of every kind, message move, JSON
    batches, @removed in message delta rounds. read_only: any request but GET fails the test (a source)."""

    def __init__(self, mailbox: str, issued: set[str], read_only: bool = False):
        super().__init__(mailbox)
        self.issued = issued              # tokens of the tenant, whichever fake issued them
        self.read_only = read_only
        self.mutations: list[tuple[str, str]] = []
        self.trash: list[tuple[str, dict]] = []   # (kind, item) of the events, contacts, tasks deleted
        self.purged: list[str] = []       # messages deleted out of Deleted Items (would be the end of them)
        self.batches: list[list[tuple[str, str]]] = []
        self.sub_throttle: list[int] = []  # Retry-After of the next sub-requests of a batch
        self.gate = None
        self.gate_free: list[int] = []    # free slots of `gate` seen while a batch runs
        self.journal: list[tuple[str, str, str]] = []  # (folder id, message id, "add" | "remove") for delta rounds
        self.after: list = []             # callables run after each request (concurrent changes)
        self.change = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("login."):
            r = super().__call__(request)
            if r.status_code == 200:
                self.issued.add(self.last_token)
            return r
        tok = request.headers.get("authorization", "").removeprefix("Bearer ")
        if tok in self.issued:
            self.last_token = tok
        path = urllib.parse.unquote(request.url.path)
        if request.method != "GET":
            self.mutations.append((request.method, path))
            if self.read_only:
                raise AssertionError(f"the source was written: {request.method} {path}")
        if path.endswith("/permanentDelete"):
            raise AssertionError(f"permanentDelete called: {path}")
        r = self._batch(request) if path == "/v1.0/$batch" else super().__call__(request)
        for hook in list(self.after):
            hook(request)
        return r

    def _batch(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        self.requests.append(request)
        if self.throttle:
            return httpx.Response(429, headers={"Retry-After": str(self.throttle.pop(0))},
                                  json={"error": {"code": "ApplicationThrottled", "message": "too many requests"}})
        if request.headers.get("authorization") != f"Bearer {self.last_token}":
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken", "message": "expired"}})
        reqs = json.loads(request.content)["requests"]
        assert 0 < len(reqs) <= 20, "documented: 20 requests per batch at most"
        self.batches.append([(r["method"], r["url"]) for r in reqs])
        if self.gate is not None:
            self.gate_free.append(self.gate._value)
        out = []
        for r in reqs:
            if self.sub_throttle:
                out.append({"id": r["id"], "status": 429, "headers": {"Retry-After": str(self.sub_throttle.pop(0))},
                            "body": {"error": {"code": "ApplicationThrottled", "message": "too many"}}})
                continue
            headers = {"authorization": request.headers["authorization"], **(r.get("headers") or {})}
            content = json.dumps(r["body"]).encode() if "body" in r else b""
            sub = httpx.Request(r["method"], f"https://graph.microsoft.com/v1.0{r['url']}", headers=headers,
                                content=content)
            resp = self(sub)
            try:
                body = resp.json()
            except ValueError:
                body = None
            out.append({"id": r["id"], "status": resp.status_code, "headers": {}, "body": body})
        return httpx.Response(200, json={"responses": out})

    def _bump(self, item: dict) -> None:
        self.change += 1
        item["changeKey"] = f"ck{self.change}"

    def _route(self, request, path, params, version):
        method = request.method
        body = json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith(
            "application/json") else None
        if (g := re.match(r"^/messages/([^/]+)$", path)) and method == "DELETE":
            m = self.messages.get(g.group(1))
            if m is None:
                return self._err(404, "ErrorItemNotFound")
            if m["parentFolderId"] == "mf-deleteditems":
                del self.messages[m["id"]]
                self.purged.append(m["id"])
            else:
                m["parentFolderId"] = "mf-deleteditems"
                self._bump(m)
            return httpx.Response(204)
        if (g := re.match(r"^/messages/([^/]+)/move$", path)) and method == "POST":
            m = self.messages.get(g.group(1))
            dest = self._mf(body["destinationId"])
            if m is None or dest is None:
                return self._err(404, "ErrorItemNotFound")
            m["parentFolderId"] = dest
            return httpx.Response(201, json={"id": m["id"], "parentFolderId": dest})  # ImmutableId: the same id
        if (g := re.match(r"^/messages/([^/]+)$", path)) and method == "PATCH":
            r = super()._route(request, path, params, version)
            if g.group(1) in self.messages:
                self._bump(self.messages[g.group(1)])
            return r
        if (g := re.match(r"^(?:/contactFolders/[^/]+)?/contacts/([^/]+)$", path)) and method != "POST" \
                and g.group(1) != "delta":
            c = self.contacts.get(g.group(1))
            if c is None:
                return self._err(404, "ErrorItemNotFound")
            if method == "DELETE":
                self.trash.append(("contact", self.contacts.pop(c["id"])))
                return httpx.Response(204)
            if method == "PATCH":
                for k, v in body.items():
                    if v is None or v == []:
                        c.pop(k, None)
                    else:
                        c[k] = v
                self._bump(c)
                return httpx.Response(200, json=c)
            return httpx.Response(200, json=dict(c, parentFolderId=c.get("folder") or "cf-default"))
        if (g := re.match(r"^/todo/lists/([^/]+)/tasks/([^/]+)$", path)) and method in ("DELETE", "PATCH"):
            t = self.tasks.get(g.group(2))
            if t is None:
                return self._err(404, "ErrorItemNotFound")
            if method == "DELETE":
                self.trash.append(("task", self.tasks.pop(t["id"])))
                return httpx.Response(204)
            for k, v in body.items():
                if v is None:
                    t.pop(k, None)
                else:
                    t[k] = v
            return httpx.Response(200, json=t)
        if (g := re.match(r"^/events/([^/]+)$", path)) and "-occ" not in g.group(1) and method in ("DELETE", "PATCH"):
            e = self.events.get(g.group(1))
            if e is None:
                return self._err(404, "ErrorItemNotFound")
            if method == "DELETE":
                self.trash.append(("event", e))
            else:
                e.update(body)
                self._bump(e)
                return httpx.Response(200, json=e)
        return super()._route(request, path, params, version)

    # -- message delta rounds that report deletions and moves
    def remove_message(self, mid: str) -> None:
        m = self.messages.pop(mid)
        self.journal.append((m["parentFolderId"], mid, "remove"))

    def move_message(self, mid: str, to: str) -> None:
        m = self.messages[mid]
        self.journal += [(m["parentFolderId"], mid, "remove"), (to, mid, "add")]
        m["parentFolderId"] = to

    def _delta(self, request, params, ref):
        if "$expand" in params:
            return self._err(400, "BadRequest", "Parsing OData Select and Expand failed")
        fid = self._mf(ref)
        clean = lambda m: {k: v for k, v in m.items() if k not in ("mime", "singleValueExtendedProperties")}  # noqa: E731
        if "$deltatoken" not in params:
            rows = [clean(m) for m in self.messages.values() if m["parentFolderId"] == fid]
        else:
            rows = []
            for f, mid, what in self.journal[int(params["$deltatoken"]):]:
                if f != fid:
                    continue
                if what == "remove":
                    rows.append({"id": mid, "@removed": {"reason": "deleted"}})
                elif mid in self.messages:
                    rows.append(clean(self.messages[mid]))
        keep = {k: v for k, v in params.items() if k != "$deltatoken"}
        link = str(request.url.copy_with(params={**keep, "$deltatoken": str(len(self.journal))}))
        return httpx.Response(200, json={"value": rows, "@odata.deltaLink": link})


class Tenant:
    """Two mailboxes behind one transport: the source (read only) and the target."""

    def __init__(self):
        self.issued: set[str] = set()
        self.src = MirrorGraph(SRC, self.issued, read_only=True)
        self.dst = MirrorGraph(DST, self.issued)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("login."):
            return self.dst(request)
        path = urllib.parse.unquote(request.url.path)
        if path == "/v1.0/$batch":
            urls = [r["url"] for r in json.loads(request.content)["requests"]]
            return (self.src if any(u.startswith(f"/users/{SRC}") for u in urls) else self.dst)(request)
        return self.src(request) if f"/users/{SRC}/" in path else self.dst(request)


@pytest.fixture
def waits(monkeypatch):
    out: list[float] = []
    monkeypatch.setattr(m365, "_sleep", out.append)
    return out


@pytest.fixture
def tenant(monkeypatch, waits):
    t = Tenant()
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(t))
    return t


def source():
    src = m365.GraphSource({**TENANT, "mailbox": SRC}, {"client_secret": fx.FakeGraph.SECRET})
    src.open()
    return src


def target(**config):
    t = m365.GraphTarget({**TENANT, "mailbox": DST, **config}, {"client_secret": fx.FakeGraph.SECRET})
    s = t.session()
    s.connect()
    return s


def item(kind, raw, key="k1", flags=()):
    return OutgoingItem(kind, raw, f"<{NS}-{key}>", list(flags))


def single(subject="One", attendees=()):
    start = datetime(2024, 1, 1, 9, tzinfo=UTC)
    return pim.Event(uid="s", start=start, end=start + timedelta(hours=1), subject=subject, tz="UTC",
                     attendees=list(attendees), body="text", location="Room")


def weekly(subject="Weekly"):
    start = datetime(2024, 3, 4, 8, tzinfo=UTC)
    return pim.Event(uid="w", start=start, end=start + timedelta(hours=1), subject=subject, tz="UTC",
                     recurrence=pim.Recurrence("WEEKLY", 1, ["MO"], count=5))


def seed_source(fake: MirrorGraph) -> None:
    fake.mail_folders["sub"] = {"id": "mf-sub", "displayName": "Work", "parent": "mf-inbox"}
    for i in range(4):
        fake.add_message("mf-inbox", MIME, id=f"msg-{i}", internetMessageId=f"<{i}@x.test>",
                         receivedDateTime="2024-03-01T12:00:00Z", isRead=bool(i % 2), changeKey=f"src-ck{i}")
    fake.add_message("mf-drafts", MIME, id="draft-1", isDraft=True, changeKey="d-ck1")
    fake.events["ev-1"] = {"id": "ev-1", "calendar": "cal-1", "type": "singleInstance", "subject": "S", "changeKey": "e-ck1",
                           "start": {"dateTime": "2024-03-04T08:00:00", "timeZone": "UTC"},
                           "end": {"dateTime": "2024-03-04T09:00:00", "timeZone": "UTC"}}
    fake.contact_folders["cf-1"] = {"id": "cf-1", "displayName": "Friends"}
    fake.contacts["c1"] = {"id": "c1", "folder": None, "displayName": "Ann", "changeKey": "c-ck1"}
    fake.tasks["t1"] = {"id": "t1", "listId": "list-1", "title": "Plan", "@odata.etag": 'W/"t-1"'}


def folder(src, *path, kind=None):
    return next(f for f in src.folders() if f.path == list(path) and (kind is None or f.kind == kind))


# ------------------------------------------------------------------ capabilities
def test_capabilities_are_declared():
    from app.connectors import registry
    role = registry.role_of("m365_graph", "source")
    assert CAP_MIRROR in role.capabilities
    caps = m365.GraphTarget.capabilities
    assert {CAP_INVENTORY, CAP_DELETE, CAP_UPDATE, CAP_SET_FLAGS, CAP_MOVE} <= caps


# ------------------------------------------------------------------ source: read only
def test_source_is_never_written(tenant):
    """Everything the mirror may call on the source, against a source that fails the test on any write."""
    seed_source(tenant.src)
    src = source()
    folders = src.folders()
    for f in folders:
        if f.skip:
            continue
        listing = src.mirror_listing(f)
        assert listing.complete
        for it in listing.items[:2]:
            src.fetch(f, it, "<x>")
        keys = [it.key for it in listing.items] + ["nope-key"]
        src.exists(f, keys, {**{it.key: it.ref for it in listing.items}, "nope-key": "nope"})
    inbox = folder(src, "Inbox")
    cur = src.changed_since(inbox, None).cursor
    tenant.src.remove_message("msg-0")
    src.changed_since(inbox, cur)
    sent = len(tenant.src.requests)
    for method in ("POST", "PATCH", "DELETE", "PUT"):
        with pytest.raises(ConnectorError) as err:
            src._c().request(method, src._c().url("/messages/msg-1"))
        assert err.value.kind == ConnectorError.CONFIG and "only reads" in str(err.value)
    with pytest.raises(ConnectorError):
        src._c().batch([{"id": "1", "method": "GET", "path": "/messages/msg-1"}])  # a batch is a POST
    assert len(tenant.src.requests) == sent  # refused before leaving
    assert tenant.src.mutations == []


def test_mirror_listing_versions(tenant):
    seed_source(tenant.src)
    src = source()
    inbox = src.mirror_listing(folder(src, "Inbox")).items
    assert {i.ref: i.version for i in inbox} == {f"msg-{i}": m365.MAIL_SEALED for i in range(4)}
    drafts = src.mirror_listing(folder(src, "Drafts")).items
    assert [i.version for i in drafts] == ["d-ck1"]  # a draft can change: its changeKey
    assert [i.version for i in src.mirror_listing(folder(src, "Calendar")).items] == ["e-ck1"]
    contacts = next(f for f in src.folders() if f.kind == CONTACTS and not f.ref)
    assert [i.version for i in src.mirror_listing(contacts).items] == ["c-ck1"]
    assert [i.version for i in src.mirror_listing(folder(src, "Tasks")).items] == ['W/"t-1"']
    assert inbox[1].flags == ["\\Seen"]


def test_mirror_listing_survives_a_deletion_during_paging(tenant):
    """Graph pages by $skip: msg-0 deleted after the first page shifts msg-2 to the first page, the first pass misses
    it. The second pass has it: no item still at the source is ever missing from the listing."""
    seed_source(tenant.src)
    src = source()
    inbox = folder(src, "Inbox")

    def delete_once(request):
        if request.url.path.endswith("/mailFolders/mf-inbox/messages") and "$skip" not in str(request.url) \
                and "msg-0" in tenant.src.messages:
            tenant.src.remove_message("msg-0")
    tenant.src.after.append(delete_once)
    first = [i.ref for i in src.iter_items(inbox)]
    assert "msg-2" not in first  # what one pass alone would miss
    tenant.src.messages.clear()
    seed_source(tenant.src)  # the same order again, msg-0 first
    listing = src.mirror_listing(inbox)
    assert {"msg-1", "msg-2", "msg-3"} <= {i.ref for i in listing.items} and listing.complete


def test_listing_error_raises(tenant):
    seed_source(tenant.src)
    src = source()
    tenant.src.fail["GET:/mailFolders/mf-inbox/messages"] = (500, "InternalServerError")
    with pytest.raises(ConnectorError):
        src.mirror_listing(folder(src, "Inbox"))


# ------------------------------------------------------------------ source: absence confirmed, moves
def test_exists_tells_gone_from_moved(tenant):
    seed_source(tenant.src)
    src = source()
    inbox = folder(src, "Inbox")
    tenant.src.remove_message("msg-0")
    tenant.src.move_message("msg-1", "mf-sub")
    assert src.probe(inbox, ["msg-0", "msg-1", "msg-2"]) == {"msg-0": m365.GONE, "msg-1": m365.MOVED,
                                                               "msg-2": m365.HERE}
    contacts = next(f for f in src.folders() if f.kind == CONTACTS and not f.ref)
    assert src.probe(contacts, ["c1"]) == {"c1": m365.HERE}
    tenant.src.contacts["c1"]["folder"] = "cf-1"  # moved to Friends
    assert src.probe(contacts, ["c1"]) == {"c1": m365.MOVED}
    assert src.probe(folder(src, "Calendar"), ["ev-1", "ev-x"]) == {"ev-1": m365.HERE, "ev-x": m365.GONE}
    assert src.probe(folder(src, "Tasks"), ["t1", "t9"]) == {"t1": m365.HERE, "t9": m365.GONE}


def test_exists_answers_by_key_with_the_recorded_refs(tenant):
    """The interface: exists(folder, keys, refs) -> {key: still there}; refs are the SourceItem.ref recorded in
    items.src_ref (JSON round trip: a str stays a str). Moved is True, no ref (or not an id) is True: kept."""
    seed_source(tenant.src)
    src = source()
    inbox = folder(src, "Inbox")
    keys = {it.ref: it.key for it in src.mirror_listing(inbox).items}
    tenant.src.remove_message("msg-0")
    tenant.src.move_message("msg-1", "mf-sub")
    refs = {keys["msg-0"]: "msg-0", keys["msg-1"]: "msg-1", keys["msg-2"]: None, keys["msg-3"]: ["not", "an", "id"]}
    assert src.exists(inbox, list(refs) + ["unknown"], refs) == {
        keys["msg-0"]: False, keys["msg-1"]: True, keys["msg-2"]: True, keys["msg-3"]: True, "unknown": True}
    assert src.exists(inbox, [keys["msg-0"]]) == {keys["msg-0"]: True}  # no refs at all: cannot tell
    assert "exists" in __import__("app.connectors.base", fromlist=["x"]).READ_ONLY_SOURCE_METHODS


def test_exists_never_says_gone_on_another_failure(tenant):
    seed_source(tenant.src)
    src = source()
    inbox = folder(src, "Inbox")
    for status, code in ((404, "ErrorInvalidUser"), (500, "InternalServerError"), (403, "ErrorAccessDenied"),
                         (400, "ErrorInvalidIdMalformed")):
        tenant.src.fail["GET:/messages/msg-2"] = (status, code)
        with pytest.raises(ConnectorError):
            src.exists(inbox, ["k2"], {"k2": "msg-2"})
    # an item 404 while the mailbox itself no longer answers: not a deletion
    tenant.src.remove_message("msg-0")
    tenant.src.fail["GET:/mailFolders/inbox"] = (404, "ErrorInvalidUser")
    with pytest.raises(ConnectorError):
        src.exists(inbox, ["k0"], {"k0": "msg-0"})


def test_delta_reports_removed_and_a_move_is_not_a_deletion(tenant):
    seed_source(tenant.src)
    src = source()
    inbox, work = folder(src, "Inbox"), folder(src, "Inbox", "Work")
    first = src.changed_since(inbox, None)
    assert first.reset and first.removed == []
    before = {i.ref: i.key for i in first.items}
    tenant.src.remove_message("msg-0")
    tenant.src.move_message("msg-1", "mf-sub")
    second = src.changed_since(inbox, first.cursor)
    # @removed: a hint (keys), a move says it too
    assert not second.reset and second.removed == [before["msg-0"], before["msg-1"]]
    refs = {before[r]: r for r in ("msg-0", "msg-1")}
    assert src.exists(inbox, second.removed, refs) == {before["msg-0"]: False, before["msg-1"]: True}
    moved = [i for i in src.mirror_listing(work).items if i.ref == "msg-1"][0]
    assert moved.key != before["msg-1"]  # the key holds the folder: the ref (ImmutableId) tells the move


def test_core_flow_deletes_only_what_a_get_says_gone(tenant):
    """What the mirror does with a tracked item missing from a complete listing: GET it at the source first, delete
    at the target only when gone, never when moved, nothing at all when the probe fails."""
    seed_source(tenant.src)
    src = source()
    s = target()
    inbox = folder(src, "Inbox")
    tracked = {}
    for it in src.mirror_listing(inbox).items:
        tracked[it.key] = (it.ref, s.put("mail:@inbox", item(MAIL, MIME, it.ref)))
    tenant.src.remove_message("msg-0")
    tenant.src.move_message("msg-1", "mf-sub")

    def mirror_pass():
        listed = {i.key for i in src.mirror_listing(inbox).items}
        missing = {k: v for k, v in tracked.items() if k not in listed}
        there = src.exists(inbox, list(missing), {k: ref for k, (ref, _rid) in missing.items()})
        s.inventory("mail:@inbox", NS)
        gone = [(ref, rid) for k, (ref, rid) in missing.items() if not there[k]]
        done = s.delete_many("mail:@inbox", [rid for _ref, rid in gone], {rid: f"<{NS}-{ref}>" for ref, rid in gone})
        return {ref: done[rid] for ref, rid in gone}

    tenant.src.fail["GET:/messages/msg-0"] = (500, "InternalServerError")  # the probe fails: nothing is deleted
    with pytest.raises(ConnectorError):
        mirror_pass()
    assert not any(m == "DELETE" for m, _p in tenant.dst.mutations)
    assert mirror_pass() == {"msg-0": DELETE_TRASH}
    rid0, rid1 = tracked[[k for k, v in tracked.items() if v[0] == "msg-0"][0]][1], \
        tracked[[k for k, v in tracked.items() if v[0] == "msg-1"][0]][1]
    assert tenant.dst.messages[rid0]["parentFolderId"] == "mf-deleteditems"
    assert tenant.dst.messages[rid1]["parentFolderId"] == "mf-inbox"  # moved at the source: not deleted
    assert tenant.src.mutations == []


# ------------------------------------------------------------------ target: inventory, ownership
def put_all(s, fake):
    ids = {"mail": s.put("mail:@inbox", item(MAIL, MIME, "m1", ["\\Seen"])),
           "event": s.put("calendar:@default", item(CALENDAR, pim.event_to_ics(single()), "e1")),
           "contact": s.put("contacts:@default", item(CONTACTS, pim.contact_to_vcard(pim.Contact(
               uid="c", full_name="Ann Lee", given="Ann", phones=[("mobile", "+336")], note="n")), "c1")),
           "task": s.put("tasks:@default", item(TASKS, pim.task_to_ics(pim.Task(uid="t", subject="Do")), "t1"))}
    # already there before mccb, and written by another job: never ours
    fake.add_message("mf-inbox", MIME, id="theirs")
    fake.add_message("mf-inbox", MIME, id="other-ns",
                     singleValueExtendedProperties=[{"id": m365.PROP_IMPORT, "value": "<ns2-k1>"}])
    fake.add_message("mf-inbox", MIME, id="upper",  # the filter of Graph ignores case, the inventory does not
                     singleValueExtendedProperties=[{"id": m365.PROP_IMPORT, "value": "<NS1-k9>"}])
    fake.events["their-ev"] = {"id": "their-ev", "calendar": "cal-1", "subject": "Theirs"}
    fake.contacts["their-c"] = {"id": "their-c", "folder": None, "displayName": "Theirs"}
    fake.tasks["their-t"] = {"id": "their-t", "listId": "list-1", "title": "Theirs"}
    return ids


def test_inventory_lists_only_ours(tenant):
    s = target()
    ids = put_all(s, tenant.dst)
    tenant.dst.add_message("mf-inbox", MIME, id="lost-patch",  # the PATCH after the import was lost: the header
                           singleValueExtendedProperties=[{"id": m365.PROP_HEADER, "value": f"<{NS}-m2>"}])
    writes = len(tenant.dst.mutations)
    mails = s.inventory("mail:@inbox", NS)
    assert set(mails) == {f"<{NS}-m1>", f"<{NS}-m2>"}
    assert mails[f"<{NS}-m1>"][0].ref == ids["mail"] and "\\Seen" in mails[f"<{NS}-m1>"][0].flags
    assert mails[f"<{NS}-m2>"][0].ref == "lost-patch"
    assert {k: [r.ref for r in v] for k, v in s.inventory("calendar:@default", NS).items()} == {f"<{NS}-e1>": [ids["event"]]}
    assert {k: [r.ref for r in v] for k, v in s.inventory("contacts:@default", NS).items()} == {f"<{NS}-c1>": [ids["contact"]]}
    assert {k: [r.ref for r in v] for k, v in s.inventory("tasks:@default", NS).items()} == {f"<{NS}-t1>": [ids["task"]]}
    assert s.inventory("mail:@inbox", "ns9") == {}
    assert len(tenant.dst.mutations) == writes  # read only


def test_nothing_is_touched_without_the_inventory(tenant):
    s = target()
    ids = put_all(s, tenant.dst)
    with pytest.raises(ConnectorError) as err:  # ours, but not listed by inventory() of the mirror
        s.delete("mail:@inbox", ids["mail"])
    assert "inventory" in str(err.value) and tenant.dst.messages[ids["mail"]]["parentFolderId"] == "mf-inbox"
    other = s.target.session()  # the inventory of one session is good for the others of the target
    other.connect()
    s.inventory("mail:@inbox", NS)
    assert other.delete("mail:@inbox", ids["mail"]) == DELETE_TRASH


def test_items_not_written_by_mccb_are_never_deleted(tenant):
    s = target()
    put_all(s, tenant.dst)
    for spec in ("mail:@inbox", "calendar:@default", "contacts:@default", "tasks:@default"):
        s.inventory(spec, NS)
    for spec, ref in (("mail:@inbox", "theirs"), ("mail:@inbox", "other-ns"), ("calendar:@default", "their-ev"),
                      ("contacts:@default", "their-c"), ("tasks:@default", "their-t")):
        with pytest.raises(ConnectorError) as err:
            s.delete(spec, ref)
        assert err.value.kind == ConnectorError.REJECTED and "left alone" in str(err.value)
    with pytest.raises(ConnectorError):
        s.update("contacts:@default", "their-c", item(CONTACTS, pim.contact_to_vcard(pim.Contact(uid="x", full_name="X"))))
    with pytest.raises(ConnectorError):
        s.set_flags("mail:@inbox", "theirs", ["\\Seen"], [])
    with pytest.raises(ConnectorError):
        s.move("mail:@inbox", "theirs", "mail:@junkemail")
    assert {"theirs", "other-ns"} <= set(tenant.dst.messages) and "their-ev" in tenant.dst.events
    assert "their-c" in tenant.dst.contacts and "their-t" in tenant.dst.tasks
    assert all(tenant.dst.messages[m]["parentFolderId"] == "mf-inbox" for m in ("theirs", "other-ns"))
    assert not any(m != "GET" and any(x in p for x in ("theirs", "other-ns", "their-")) for m, p in tenant.dst.mutations)
    # inventory() listed it but the mark is not that of our namespace: an item written by mccb for another job
    s._marks[("mail:@inbox", "other-ns")] = f"<{NS}-k1>"
    with pytest.raises(ConnectorError):
        s.delete("mail:@inbox", "other-ns")
    assert tenant.dst.messages["other-ns"]["parentFolderId"] == "mf-inbox"


def test_a_mark_changed_since_the_inventory_is_refused(tenant):
    s = target()
    ids = put_all(s, tenant.dst)
    s.inventory("mail:@inbox", NS)
    msg = tenant.dst.messages[ids["mail"]]
    msg["singleValueExtendedProperties"] = [{"id": m365.PROP_IMPORT, "value": f"<{NS}-someone-else>"}]
    with pytest.raises(ConnectorError) as err:
        s.delete("mail:@inbox", ids["mail"])
    assert "not written by mccb" in str(err.value) and msg["parentFolderId"] == "mf-inbox"
    del msg["singleValueExtendedProperties"]  # no mark at all
    with pytest.raises(ConnectorError):
        s.delete("mail:@inbox", ids["mail"])
    with pytest.raises(ConnectorError):  # update knows the import id it expects
        s.update("mail:@inbox", ids["mail"], item(MAIL, MIME, "m1"))


# ------------------------------------------------------------------ target: deletion
def test_delete_goes_to_deleted_items_never_permanent(tenant):
    s = target()
    ids = put_all(s, tenant.dst)
    assert s.delete_mode("mail:@inbox") == DELETE_TRASH and s.delete_mode("calendar:@default") == DELETE_TRASH
    assert s.delete_mode("contacts:@default") == DELETE_TRASH and s.delete_mode("tasks:@default") == DELETE_HARD
    assert s.delete_mode("mail:@deleteditems") == DELETE_SOFT
    for spec in ("mail:@inbox", "calendar:@default", "contacts:@default", "tasks:@default"):
        s.inventory(spec, NS)
    assert s.delete("mail:@inbox", ids["mail"]) == DELETE_TRASH
    msg = tenant.dst.messages[ids["mail"]]
    assert msg["parentFolderId"] == "mf-deleteditems" and MIRROR_DELETED_FLAG in msg["categories"]
    assert s.delete("calendar:@default", ids["event"]) == DELETE_TRASH and ids["event"] not in tenant.dst.events
    assert s.delete("contacts:@default", ids["contact"]) == DELETE_TRASH
    assert s.read("tasks:@default", ids["task"]).startswith(b"BEGIN:VCALENDAR")  # the copy kept before a hard delete
    assert s.delete("tasks:@default", ids["task"]) == DELETE_HARD
    assert [k for k, _x in tenant.dst.trash] == ["event", "contact", "task"]
    # already in Deleted Items: marked, never deleted again (that would be the end of it)
    tenant.dst.messages[ids["mail"]]["categories"] = []
    s.inventory("mail:@deleteditems", NS)
    assert s.delete("mail:@deleteditems", ids["mail"]) == DELETE_SOFT
    assert ids["mail"] in tenant.dst.messages and tenant.dst.purged == []
    assert MIRROR_DELETED_FLAG in tenant.dst.messages[ids["mail"]]["categories"]
    every = [r.url.path for r in tenant.dst.requests] + [u for b in tenant.dst.batches for _m, u in b]
    assert not any("permanentDelete" in p for p in every)
    deletes = [u for b in tenant.dst.batches for m, u in b if m == "DELETE"]
    assert deletes and all("/permanentDelete" not in u for u in deletes)


def test_delete_already_gone_is_not_an_error(tenant):
    s = target()
    ids = put_all(s, tenant.dst)
    s.inventory("mail:@inbox", NS)
    s.inventory("calendar:@default", NS)
    tenant.dst.messages.pop(ids["mail"])
    assert s.delete("mail:@inbox", ids["mail"]) == DELETE_TRASH
    tenant.dst.events.pop(ids["event"])
    assert s.delete("calendar:@default", ids["event"]) == DELETE_TRASH


def test_delete_many_in_batches_of_20_and_429(tenant, waits):
    s = target()
    tenant.dst.gate = s.target.gate
    rids = [s.put("mail:@inbox", item(MAIL, MIME, f"m{i}")) for i in range(45)]
    s.inventory("mail:@inbox", NS)
    tenant.dst.batches.clear()
    tenant.dst.throttle = [3]          # the whole batch, once
    tenant.dst.sub_throttle = [7, 7]   # two sub-requests of a batch
    got = s.delete_many("mail:@inbox", rids)
    assert set(got.values()) == {DELETE_TRASH}
    assert all(tenant.dst.messages[r]["parentFolderId"] == "mf-deleteditems" for r in rids)
    assert tenant.dst.batches and all(len(b) <= 20 for b in tenant.dst.batches)
    assert 3.0 in waits and 7.0 in waits
    assert tenant.dst.gate_free and set(tenant.dst.gate_free) == {0}  # 4 at once per mailbox: the batch holds them all
    assert s.target.gate._value == m365.MAX_CONCURRENT


def test_batch_gives_up_when_throttled_for_ever(tenant):
    s = target()
    rid = s.put("mail:@inbox", item(MAIL, MIME))
    s.inventory("mail:@inbox", NS)
    tenant.dst.sub_throttle = [1] * 50
    with pytest.raises(ConnectorError) as err:
        s.delete("mail:@inbox", rid)
    assert err.value.retryable and tenant.dst.messages[rid]["parentFolderId"] == "mf-inbox"


# ------------------------------------------------------------------ target: events and invitations
def test_send_invitations_refuses_event_deletion_and_update(tenant):
    s = target(send_invitations=True)
    rid = s.put("calendar:@default", item(CALENDAR, pim.event_to_ics(single()), "e1"))
    tenant.dst.mutations.clear()
    with pytest.raises(ConnectorError) as err:
        s.delete("calendar:@default", rid)
    assert err.value.kind == ConnectorError.REJECTED and "Send invitations" in str(err.value)
    assert s.delete_many("calendar:@default", [rid]) == {}  # refused: missing from the answer
    with pytest.raises(ConnectorError) as err:
        s.update("calendar:@default", rid, item(CALENDAR, pim.event_to_ics(single("Two")), "e1"))
    assert "Send invitations" in str(err.value)
    assert tenant.dst.mutations == [] and tenant.dst.events[rid]["subject"] == "One"
    # mails and contacts are not concerned
    mid = s.put("mail:@inbox", item(MAIL, MIME, "m1"))
    s.inventory("mail:@inbox", NS)
    assert s.delete("mail:@inbox", mid) == DELETE_TRASH


def test_event_with_attendees_added_on_the_target_is_left_alone(tenant):
    s = target()
    rid = s.put("calendar:@default", item(CALENDAR, pim.event_to_ics(single()), "e1"))
    s.inventory("calendar:@default", NS)
    tenant.dst.events[rid]["attendees"] = [{"emailAddress": {"address": "x@y.test"}, "type": "required"}]
    for call in (lambda: s.delete("calendar:@default", rid),
                 lambda: s.update("calendar:@default", rid, item(CALENDAR, pim.event_to_ics(single("Two")), "e1"))):
        with pytest.raises(ConnectorError) as err:
            call()
        assert "attendees" in str(err.value)
    assert rid in tenant.dst.events and tenant.dst.events[rid]["subject"] == "One"


def test_default_target_keeps_attendees_as_text_so_events_can_change(tenant):
    s = target()
    who = [pim.Person("a@example.test", "Ann")]
    rid = s.put("calendar:@default", item(CALENDAR, pim.event_to_ics(single(attendees=who)), "e1"))
    assert "attendees" not in tenant.dst.events[rid]
    assert s.update("calendar:@default", rid, item(CALENDAR, pim.event_to_ics(single("Two", who)), "e1")) == rid
    e = tenant.dst.events[rid]
    assert e["subject"] == "Two" and "attendees" not in e and "transactionId" in e  # PATCH: no new transactionId
    assert s.delete("calendar:@default", rid) == DELETE_TRASH


# ------------------------------------------------------------------ target: updates, flags, moves
def test_update_series_is_replaced(tenant):
    s = target()
    rid = s.put("calendar:@default", item(CALENDAR, pim.event_to_ics(weekly()), "w1"))
    new = s.update("calendar:@default", rid, item(CALENDAR, pim.event_to_ics(weekly("Weekly 2")), "w1"))
    assert new != rid and rid not in tenant.dst.events and tenant.dst.events[new]["subject"] == "Weekly 2"
    assert [x["id"] for k, x in tenant.dst.trash if k == "event"] == [rid]
    assert s.inventory("calendar:@default", "w") == {} and list(s.inventory("calendar:@default", NS)) == [f"<{NS}-w1>"]


def test_update_writes_the_new_copy_before_trashing_the_old(tenant, monkeypatch):
    """M3: a series or a draft is replaced by a new copy written FIRST; when that fails the old one is untouched."""
    s = target()
    ids = put_all(s, tenant.dst)
    rid = s.put("calendar:@default", item(CALENDAR, pim.event_to_ics(weekly()), "w1"))

    def refused(*a, **kw):
        raise ConnectorError("POST refused", ConnectorError.TRANSIENT)
    monkeypatch.setattr(s, "_put_event", refused)
    monkeypatch.setattr(s, "put", refused)
    with pytest.raises(ConnectorError):
        s.update("calendar:@default", rid, item(CALENDAR, pim.event_to_ics(weekly("Weekly 2")), "w1"))
    assert rid in tenant.dst.events and tenant.dst.trash == []
    with pytest.raises(ConnectorError):
        s.update("mail:@inbox", ids["mail"], item(MAIL, MIME.replace(b"body", b"edited"), "m1"))
    assert tenant.dst.messages[ids["mail"]]["parentFolderId"] != "mf-deleteditems"


def test_update_contact_task_and_draft(tenant):
    s = target()
    ids = put_all(s, tenant.dst)
    c2 = pim.Contact(uid="c", full_name="Ann Smith", given="Ann", surname="Smith")
    assert s.update("contacts:@default", ids["contact"], item(CONTACTS, pim.contact_to_vcard(c2), "c1")) == ids["contact"]
    c = tenant.dst.contacts[ids["contact"]]
    assert c["surname"] == "Smith" and "mobilePhone" not in c and "personalNotes" not in c  # removed at the source
    assert m365._ext_value(c, m365.PROP_IMPORT) == f"<{NS}-c1>"
    t2 = pim.Task(uid="t", subject="Done", status="COMPLETED")
    assert s.update("tasks:@default", ids["task"], item(TASKS, pim.task_to_ics(t2), "t1")) == ids["task"]
    t = tenant.dst.tasks[ids["task"]]
    assert t["title"] == "Done" and t["status"] == "completed" and t["linkedResources"][0]["externalId"] == f"<{NS}-t1>"
    new = s.update("mail:@inbox", ids["mail"], item(MAIL, MIME.replace(b"body", b"edited"), "m1"))
    assert new != ids["mail"] and tenant.dst.messages[new]["mime"].endswith(b"edited\r\n")
    assert tenant.dst.messages[ids["mail"]]["parentFolderId"] == "mf-deleteditems"  # MIME cannot change: replaced


def test_set_flags_and_move(tenant):
    s = target()
    ids = put_all(s, tenant.dst)
    s.inventory("mail:@inbox", NS)
    s.set_flags("mail:@inbox", ids["mail"], ["\\Flagged", "\\Answered"], ["\\Seen"])
    msg = tenant.dst.messages[ids["mail"]]
    assert msg["isRead"] is False and msg["flag"] == {"flagStatus": "flagged"}
    assert m365._ext_value(msg, m365.PROP_VERB) == "102"
    assert m365._ext_value(msg, m365.PROP_IMPORT) == f"<{NS}-m1>"
    new = s.move("mail:@inbox", ids["mail"], "mail:@junkemail")
    assert new == ids["mail"] and msg["parentFolderId"] == "mf-junkemail"
    assert [r.ref for r in s.inventory("mail:@junkemail", NS)[f"<{NS}-m1>"]] == [new]
    assert s.delete("mail:@junkemail", new) == DELETE_TRASH


# ------------------------------------------------------------------ final interface: names, expected marks
def test_inventory_by_resource_name_and_expected_marks(tenant):
    """The runner records an event, a contact or a task by a resource name derived from its import id (items.hash):
    inventory() answers by that name when `names` is given, delete() checks the expected import id or name."""
    s = target()
    ids = put_all(s, tenant.dst)
    name = m365.resource_name_of(CALENDAR, f"<{NS}-e1>")
    assert name.endswith(".ics") and m365.resource_name_of(CONTACTS, f"<{NS}-c1>").endswith(".vcf")
    inv = s.inventory("calendar:@default", NS, {name})
    assert {k: [r.ref for r in v] for k, v in inv.items()} == {name: [ids["event"]]}
    assert s.inventory("calendar:@default", NS, {"other.ics"}) == {}
    cname = m365.resource_name_of(CONTACTS, f"<{NS}-c1>")
    assert list(s.inventory("contacts:@default", NS, {cname})) == [cname]
    tname = m365.resource_name_of(TASKS, f"<{NS}-t1>")
    assert list(s.inventory("tasks:@default", NS, {tname})) == [tname]
    s.inventory("mail:@inbox", NS)
    with pytest.raises(ConnectorError):  # another import id expected: refused
        s.delete("mail:@inbox", ids["mail"], f"<{NS}-m9>")
    assert s.delete_many("mail:@inbox", [ids["mail"]], {ids["mail"]: f"<{NS}-m9>"}) == {}
    assert tenant.dst.messages[ids["mail"]]["parentFolderId"] == "mf-inbox"
    assert s.delete("mail:@inbox", ids["mail"], f"<{NS}-m1>") == DELETE_TRASH
    assert s.delete_many("contacts:@default", [ids["contact"]], {ids["contact"]: "wrong.vcf"}) == {}
    assert s.delete_many("contacts:@default", [ids["contact"]], {ids["contact"]: cname}) == {ids["contact"]: DELETE_TRASH}


def test_delete_many_answers_only_the_done(tenant):
    s = target()
    ids = put_all(s, tenant.dst)
    s.inventory("mail:@inbox", NS)
    got = s.delete_many("mail:@inbox", [ids["mail"], "theirs", "gone-already"])
    assert got == {ids["mail"]: DELETE_TRASH, "gone-already": DELETE_TRASH}  # "theirs": refused, missing
    assert tenant.dst.messages["theirs"]["parentFolderId"] == "mf-inbox"


# ------------------------------------------------------------------ end to end, through the mirror of the runner
SRV = {"name": "Prod", "imap_host": "imap.example.test", "imap_port": 993, "imap_security": "ssl", "verify_tls": True,
       "dav_enabled": False, "workers": 1, "tuning": "manual", "notes": ""}


def run_job(client, jid):
    from app.server import runner
    runner.run_job(jid, "test:inproc")
    return client.get(f"/api/jobs/{jid}").json()


def test_graph_to_graph_mirror_end_to_end(client, tenant):
    seed_source(tenant.src)
    # an emptied folder and more than 20 % deleted are held back by the mirror: other contacts stay
    for i in range(2, 7):
        tenant.src.contacts[f"c{i}"] = {"id": f"c{i}", "folder": None, "displayName": f"Bob {i}", "changeKey": f"c-ck{i}"}
    sid = client.post("/api/servers", json=SRV).json()["id"]
    aid = client.post("/api/accounts", json={"server_id": sid, "email": DST, "password": "pw"}).json()["id"]
    for role, mailbox in (("source", SRC), ("target", DST)):
        r = client.put(f"/api/accounts/{aid}/{role}", json={"kind": "m365_graph", "config": {**TENANT, "mailbox": mailbox},
                                                            "secrets": {"client_secret": fx.FakeGraph.SECRET}})
        assert r.status_code == 200, r.text
    assert client.put(f"/api/accounts/{aid}", json={"mirror": True}).status_code == 200
    job = run_job(client, client.post("/api/runs", json={"account_ids": [aid]}).json()[0]["id"])
    assert job["status"] == "done", client.get(f"/api/jobs/{job['id']}/log").json()
    dst = tenant.dst
    inbox = lambda: {m["id"]: m for m in dst.messages.values() if m["parentFolderId"] == "mf-inbox"}  # noqa: E731
    assert len(inbox()) == 4 and len(dst.events) == 1 and len(dst.contacts) == 6
    dst.add_message("mf-inbox", MIME, id="native")  # arrived on the target: never ours
    # at the source: one mail deleted for good, one read, one new, the event changed, the contact deleted
    tenant.src.remove_message("msg-0")
    tenant.src.messages["msg-2"]["isRead"] = True
    tenant.src.add_message("mf-inbox", MIME.replace(b"<1@", b"<new@"), id="msg-new", internetMessageId="<new@x.test>")
    tenant.src.events["ev-1"].update(subject="S moved", changeKey="e-ck2")
    del tenant.src.contacts["c1"]
    r = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror", "dry_run": False})
    assert r.status_code == 200, r.text
    job = run_job(client, r.json()["jobs"][0]["id"])
    log = client.get(f"/api/jobs/{job['id']}/log").json()
    assert job["status"] == "done", "\n".join(f"{x['level']} {x['msg']}" for x in log["lines"])
    rep = client.get(f"/api/jobs/{job['id']}/report").json()["mirror"]
    assert rep["mail"]["deleted"] == 1 and rep["mail"]["added"] == 1 and rep["mail"]["errors"] == 0, (rep, log)
    assert rep["calendar"]["updated"] == 1 and rep["contacts"]["deleted"] == 1, (rep, log)
    assert "native" in inbox() and len(inbox()) == 5  # 4 - 1 + 1, and the native one
    trashed = [m for m in dst.messages.values() if m["parentFolderId"] == "mf-deleteditems"]
    assert len(trashed) == 1 and MIRROR_DELETED_FLAG in trashed[0]["categories"]
    assert sum(1 for m in inbox().values() if m["isRead"]) >= 1  # the read flag followed
    assert [e["subject"] for e in dst.events.values()] == ["S moved"]
    assert "Ann" not in [c.get("displayName") for c in dst.contacts.values()] and len(dst.contacts) == 5 and [k for k, _x in dst.trash] == ["contact"]
    assert tenant.src.mutations == [] and dst.purged == []
