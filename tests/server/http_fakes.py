"""A real HTTP server on a loopback port for the protocol tests, plus a JMAP server that answers with the payloads of
RFC 8620, 8621 and 9610 (session resource, method calls with result references, blobs, state strings)."""
from __future__ import annotations

import base64
import json
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit


class FakeHttp:
    """`handler(req)` returns (status, headers, body). `req` has method, path, query, headers, body, json()."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[SimpleNamespace] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                u = urlsplit(self.path)
                req = SimpleNamespace(method=self.command, path=u.path, query={k: v[0] for k, v in parse_qs(u.query).items()},
                                      headers=self.headers, body=body, json=lambda: json.loads(body or b"null"))
                outer.requests.append(req)
                status, headers, out = outer.handler(req)
                if isinstance(out, (dict, list)):
                    out, headers = json.dumps(out).encode(), {"Content-Type": "application/json", **headers}
                elif isinstance(out, str):
                    out = out.encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_GET = do_POST = do_PUT = do_PROPFIND = do_REPORT = do_MKCOL = do_MKCALENDAR = do_DELETE = _do

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def basic_ok(req, user="me", password="pw") -> bool:
    got = req.headers.get("Authorization", "")
    return got == "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# ------------------------------------------------------------------ JMAP
class JmapServer:
    """Account A1 with mail and contacts. Knobs: `no_header_filter`, `cannot_calculate`, `over_quota`, `other_host`."""

    def __init__(self):
        self.http = FakeHttp(self.handle)
        self.mailboxes = {"mb-inbox": {"id": "mb-inbox", "name": "Inbox", "parentId": None, "role": "inbox"},
                          "mb-sent": {"id": "mb-sent", "name": "Sent", "parentId": None, "role": "sent"},
                          "mb-work": {"id": "mb-work", "name": "Work", "parentId": None, "role": None},
                          "mb-all": {"id": "mb-all", "name": "All", "parentId": None, "role": "all"}}
        self.emails: dict[str, dict] = {}
        self.blobs: dict[str, bytes] = {}
        self.books = {"ab-1": {"id": "ab-1", "name": "Personal", "sortOrder": 0}}
        self.cards: dict[str, dict] = {}
        self.state = 1
        self.log: list[tuple[int, str, str]] = []  # (state after, "created" | "updated" | "destroyed", id)
        self.no_header_filter = self.cannot_calculate = self.over_quota = False
        self.other_host = False
        self.seq = 0
        self.calls: list[str] = []        # every JMAP method called, in order ("Email/set", ...)
        self.set_args: list[dict] = []    # arguments of every Email/set and ContactCard/set
        self.query_hook = None            # called as hook(server, n) after each Email/query (n counts from 1): changes under paging
        self.queries = 0

    def add_trash(self) -> str:
        self.mailboxes["mb-trash"] = {"id": "mb-trash", "name": "Trash", "parentId": None, "role": "trash"}
        return "mb-trash"

    def add_junk(self) -> str:
        self.mailboxes["mb-junk"] = {"id": "mb-junk", "name": "Junk", "parentId": None, "role": "junk"}
        return "mb-junk"

    def destroy_email(self, eid: str) -> None:
        del self.emails[eid]
        self.state += 1
        self.log.append((self.state, "destroyed", eid))

    def destroy_card(self, cid: str) -> None:
        del self.cards[cid]
        self.state += 1
        self.log.append((self.state, "destroyed", cid))

    def close(self):
        self.http.close()

    @property
    def url(self) -> str:
        return self.http.url + "/.well-known/jmap"

    def next_id(self, prefix="x") -> str:
        self.seq += 1
        return f"{prefix}{self.seq}"

    def add_email(self, raw: bytes, boxes: list[str], keywords=None, received="2024-03-01T12:00:00Z", eid=None) -> str:
        eid = eid or self.next_id("e")
        blob = self.next_id("b")
        self.blobs[blob] = raw
        mid = re.search(rb"(?im)^Message-ID:\s*(.+)$", raw)
        self.emails[eid] = {"id": eid, "blobId": blob, "mailboxIds": {b: True for b in boxes}, "keywords": keywords or {},
                            "size": len(raw), "receivedAt": received,
                            "messageId": [mid.group(1).decode().strip()] if mid else None}
        self.state += 1
        self.log.append((self.state, "created", eid))
        return eid

    # -- HTTP
    def handle(self, req):
        if not basic_ok(req):
            return 401, {}, {"type": "about:blank", "status": 401}
        base = self.http.url
        if req.path == "/.well-known/jmap":
            api_host = "http://localhost:1" if self.other_host else base
            return 200, {}, {
                "capabilities": {"urn:ietf:params:jmap:core": {"maxSizeUpload": 50_000_000, "maxConcurrentUpload": 4,
                                                              "maxSizeRequest": 10_000_000, "maxCallsInRequest": 16,
                                                              "maxObjectsInGet": 3, "maxObjectsInSet": 50},
                                 "urn:ietf:params:jmap:mail": {}, "urn:ietf:params:jmap:contacts": {}},
                "accounts": {"A1": {"name": "me@example.test", "isPersonal": True, "isReadOnly": False,
                                    "accountCapabilities": {"urn:ietf:params:jmap:mail": {},
                                                            "urn:ietf:params:jmap:contacts": {}}}},
                "primaryAccounts": {"urn:ietf:params:jmap:mail": "A1", "urn:ietf:params:jmap:contacts": "A1"},
                "username": "me@example.test", "apiUrl": api_host + "/api",
                "downloadUrl": base + "/download/{accountId}/{blobId}/{name}?type={type}",
                "uploadUrl": base + "/upload/{accountId}/", "state": "s0"}
        if req.path == "/api" and req.method == "POST":
            return 200, {}, {"methodResponses": self.methods(req.json()["methodCalls"]), "sessionState": "s0"}
        if req.path.startswith("/upload/") and req.method == "POST":
            blob = self.next_id("b")
            self.blobs[blob] = req.body
            return 201, {}, {"accountId": "A1", "blobId": blob, "type": req.headers.get("Content-Type"), "size": len(req.body)}
        m = re.match(r"^/download/A1/([^/]+)/", req.path)
        if m:
            data = self.blobs.get(m.group(1))
            return (200, {"Content-Type": "message/rfc822"}, data) if data is not None else (404, {}, b"")
        return 404, {}, b""

    # -- methods
    def methods(self, calls):
        done: dict[str, dict] = {}
        out = []
        for name, args, cid in calls:
            args = dict(args)
            broken = False
            for k in [k for k in args if k.startswith("#")]:
                ref = args.pop(k)
                prior = done.get(ref["resultOf"])
                if prior is None:     # the call it refers to failed
                    broken = True
                else:
                    args[k[1:]] = prior["ids"]
            if broken:
                out.append(["error", {"type": "invalidResultReference"}, cid])
                continue
            self.calls.append(name)
            fn = getattr(self, "m_" + name.replace("/", "_"), None)
            if fn is None:
                out.append(["error", {"type": "unknownMethod"}, cid])
                continue
            try:
                res = fn(args)
            except JmapError as e:
                out.append(["error", {"type": e.type, "description": e.args[0] if e.args else ""}, cid])
                continue
            done[cid] = res
            out.append([name, {"accountId": "A1", **res}, cid])
        return out

    def m_Mailbox_get(self, a):
        return {"state": "mbs", "list": [{**b, "totalEmails": sum(b["id"] in e["mailboxIds"] for e in self.emails.values())}
                                         for b in self.mailboxes.values()], "notFound": []}

    def m_Mailbox_set(self, a):
        created, notc = {}, {}
        for cid, args in (a.get("create") or {}).items():
            if any(b["name"].lower() == args["name"].lower() and b["parentId"] == args.get("parentId")
                   for b in self.mailboxes.values()):
                notc[cid] = {"type": "invalidProperties", "properties": ["name"]}
                continue
            if args.get("role") == "junk" and getattr(self, "refuse_roles", False):
                notc[cid] = {"type": "invalidProperties", "properties": ["role"]}
                continue
            mid = self.next_id("mb")
            self.mailboxes[mid] = {"id": mid, "name": args["name"], "parentId": args.get("parentId"), "role": args.get("role")}
            created[cid] = {"id": mid}
        return {"oldState": "mbs", "newState": "mbs2", "created": created or None, "notCreated": notc or None}

    def _header(self, e, name):
        raw = self.blobs[e["blobId"]].split(b"\r\n\r\n")[0].split(b"\n\n")[0].decode("utf-8", "replace")
        return [m.group(1).strip() for m in re.finditer(rf"(?im)^{re.escape(name)}:(.*)$", raw)]

    def m_Email_query(self, a):
        f = a.get("filter") or {}
        ids = [e for e in self.emails.values() if f.get("inMailbox") in (None, *e["mailboxIds"])]
        if "header" in f:
            if self.no_header_filter:
                raise JmapError("unsupportedFilter")
            name, *val = f["header"]
            ids = [e for e in ids if any((not val) or val[0] in h for h in self._header(e, name))]
        ids.sort(key=lambda e: e["receivedAt"], reverse=True)
        pos, lim = int(a.get("position") or 0), int(a.get("limit") or 1000)
        page = [e["id"] for e in ids[pos:pos + lim]]
        res = {"queryState": f"q{self.state}", "canCalculateChanges": False, "position": pos, "ids": page, "total": len(ids)}
        self.queries += 1
        if self.query_hook:
            self.query_hook(self, self.queries)    # a change AFTER this page was read: the next one sees another state
        return res

    def m_Email_get(self, a):
        if a.get("ids") is not None and len(a["ids"]) > 3:
            raise JmapError("requestTooLarge")
        if a.get("ids") == []:
            return {"state": f"s{self.state}", "list": [], "notFound": []}
        out, nf = [], []
        for i in a.get("ids") or []:
            e = self.emails.get(i)
            if e is None:
                nf.append(i)
                continue
            props = a.get("properties")
            rec = {}
            for p in props or e:
                if p.startswith("header:"):
                    rec[p] = self._header(e, p.split(":")[1])
                else:
                    rec[p] = e.get(p)
            out.append(rec)
        return {"state": f"s{self.state}", "list": out, "notFound": nf}

    def m_Email_changes(self, a):
        if self.cannot_calculate:
            raise JmapError("cannotCalculateChanges")
        since = int(a["sinceState"][1:])
        created = [i for st, k, i in self.log if st > since and k == "created" and i in self.emails]
        updated = [i for st, k, i in self.log if st > since and k == "updated" and i in self.emails and i not in created]
        destroyed = [i for st, k, i in self.log if st > since and k == "destroyed" and i not in self.emails]
        return {"oldState": a["sinceState"], "newState": f"s{self.state}", "hasMoreChanges": False, "created": created,
                "updated": updated, "destroyed": destroyed}

    def m_Email_set(self, a):
        """update (patches: "keywords/$x", "mailboxIds/<id>") and destroy, with ifInState. No create (Email/import does)."""
        self.set_args.append(a)
        if a.get("ifInState") is not None and a["ifInState"] != f"s{self.state}":
            raise JmapError("stateMismatch")
        old, updated, notu, destroyed, notd = f"s{self.state}", {}, {}, [], {}
        for eid, patch in (a.get("update") or {}).items():
            e = self.emails.get(eid)
            if e is None:
                notu[eid] = {"type": "notFound"}
                continue
            bad = [k for k in patch if k.split("/")[0] not in ("keywords", "mailboxIds")]
            new_boxes = {k.split("/")[1] for k, v in patch.items() if k.startswith("mailboxIds/") and v}
            if bad or any(b not in self.mailboxes for b in new_boxes):
                notu[eid] = {"type": "invalidProperties", "properties": bad or ["mailboxIds"]}
                continue
            for k, v in patch.items():
                prop, _, sub = k.partition("/")
                if v is None or v is False:
                    e[prop].pop(sub, None)
                else:
                    e[prop][sub] = True
            if not e["mailboxIds"]:
                e["mailboxIds"] = {}
                notu[eid] = {"type": "invalidProperties", "properties": ["mailboxIds"]}
                continue
            self.state += 1
            self.log.append((self.state, "updated", eid))
            updated[eid] = None
        for eid in a.get("destroy") or []:
            if eid not in self.emails:
                notd[eid] = {"type": "notFound"}
                continue
            self.destroy_email(eid)
            destroyed.append(eid)
        return {"oldState": old, "newState": f"s{self.state}", "updated": updated or None, "notUpdated": notu or None,
                "destroyed": destroyed or None, "notDestroyed": notd or None}

    def m_Email_import(self, a):
        created, notc = {}, {}
        for cid, imp in a["emails"].items():
            if self.over_quota:
                notc[cid] = {"type": "overQuota"}
                continue
            bad = [b for b in imp["mailboxIds"] if b not in self.mailboxes]
            if bad:
                notc[cid] = {"type": "invalidProperties", "properties": ["mailboxIds"]}
                continue
            eid = self.add_email(self.blobs[imp["blobId"]], list(imp["mailboxIds"]), imp.get("keywords"),
                                 imp.get("receivedAt") or "2024-03-01T12:00:00Z")
            created[cid] = {"id": eid, "blobId": self.emails[eid]["blobId"], "threadId": "t", "size": self.emails[eid]["size"]}
        return {"oldState": "s", "newState": f"s{self.state}", "created": created or None, "notCreated": notc or None}

    def m_AddressBook_get(self, a):
        return {"state": "abs", "list": list(self.books.values()), "notFound": []}

    def m_AddressBook_set(self, a):
        created = {}
        for cid, args in (a.get("create") or {}).items():
            bid = self.next_id("ab")
            self.books[bid] = {"id": bid, "name": args["name"], "sortOrder": 0}
            created[cid] = {"id": bid}
        return {"oldState": "abs", "newState": "abs2", "created": created, "notCreated": None}

    def add_card(self, card: dict, books: list[str]) -> str:
        cid = self.next_id("c")
        self.cards[cid] = {"id": cid, "addressBookIds": {b: True for b in books}, "uid": card.get("uid") or str(uuid.uuid4()),
                           **{k: v for k, v in card.items() if k != "uid"}}
        self.state += 1
        self.cards[cid]["updated"] = (datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self.state)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        self.log.append((self.state, "created", cid))
        return cid

    def m_ContactCard_query(self, a):
        f = a.get("filter") or {}
        ids = [c["id"] for c in self.cards.values()
               if f.get("inAddressBook") in (None, *c["addressBookIds"]) and f.get("uid") in (None, c["uid"])]
        pos, lim = int(a.get("position") or 0), int(a.get("limit") or 1000)
        return {"queryState": f"q{self.state}", "position": pos, "ids": ids[pos:pos + lim], "total": len(ids)}

    def m_ContactCard_get(self, a):
        if a.get("ids") == []:
            return {"state": f"s{self.state}", "list": [], "notFound": []}
        out, nf = [], []
        for i in a.get("ids") or []:
            c = self.cards.get(i)
            if c is None:
                nf.append(i)
                continue
            out.append({k: v for k, v in c.items() if not a.get("properties") or k in a["properties"]})
        return {"state": f"s{self.state}", "list": out, "notFound": nf}

    def m_ContactCard_changes(self, a):
        since = int(a["sinceState"][1:])
        created = [i for st, k, i in self.log if st > since and k == "created" and i in self.cards]
        updated = [i for st, k, i in self.log if st > since and k == "updated" and i in self.cards and i not in created]
        destroyed = [i for st, k, i in self.log if st > since and k == "destroyed" and i not in self.cards]
        return {"oldState": a["sinceState"], "newState": f"s{self.state}", "hasMoreChanges": False, "created": created,
                "updated": updated, "destroyed": destroyed}

    def m_ContactCard_set(self, a):
        self.set_args.append(a)
        if a.get("ifInState") is not None and a["ifInState"] != f"s{self.state}":
            raise JmapError("stateMismatch")
        created, notc = {}, {}
        for cid, card in (a.get("create") or {}).items():
            if any(c["uid"] == card.get("uid") for c in self.cards.values()):
                notc[cid] = {"type": "invalidProperties", "properties": ["uid"]}
                continue
            created[cid] = {"id": self.add_card(card, list(card["addressBookIds"]))}
        updated, notu, destroyed, notd = {}, {}, [], {}
        for cid, patch in (a.get("update") or {}).items():
            c = self.cards.get(cid)
            if c is None:
                notu[cid] = {"type": "notFound"}
                continue
            for k, v in patch.items():
                if v is None:
                    c.pop(k, None)
                else:
                    c[k] = v
            self.state += 1
            c["updated"] = (datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self.state)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.log.append((self.state, "updated", cid))
            updated[cid] = None
        for cid in a.get("destroy") or []:
            if cid not in self.cards:
                notd[cid] = {"type": "notFound"}
                continue
            self.destroy_card(cid)
            destroyed.append(cid)
        return {"oldState": "s", "newState": f"s{self.state}", "created": created or None, "notCreated": notc or None,
                "updated": updated or None, "notUpdated": notu or None, "destroyed": destroyed or None,
                "notDestroyed": notd or None}


class JmapError(Exception):
    def __init__(self, type_: str):
        super().__init__(type_)
        self.type = type_
