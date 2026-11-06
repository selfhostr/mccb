"""JMAP as a source and as a target: mail (RFC 8620 core, RFC 8621 mail) and contacts (RFC 9610, JSContact cards).

Calendars are not here: JMAP Calendars is still an Internet-Draft. Use the `caldav` connector for the calendars of a
JMAP provider. See docs/protocols.md."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import quote, urlsplit

import httpx

from . import registry
from .base import (CAP_FLAGS, CAP_FOLDERS, CAP_INCREMENTAL, CAP_INTERNAL_DATE, CAP_PARALLEL, CAP_QUOTA, CAP_READBACK,
                   CONTACTS, IMPORT_HEADER, MAIL, Changes, ConnectorError, FetchedItem, FolderPlan, OutgoingItem,
                   Source, SourceFolder, SourceItem, Target, TargetSession, subject_of)
from .convert import jscontact_to_vcard, vcard_to_jscontact

CORE = "urn:ietf:params:jmap:core"
CAP_MAIL = "urn:ietf:params:jmap:mail"
CAP_CONTACTS = "urn:ietf:params:jmap:contacts"
CAP_QUOTA_URI = "urn:ietf:params:jmap:quota"
PAGE = 200                 # objects per Email/get or ContactCard/get (the server may allow fewer: see _page)
EMAIL_PROPS = ["id", "blobId", "mailboxIds", "keywords", "size", "receivedAt", "messageId"]
SKIP_ROLES = {"all", "flagged", "important", "subscribed"}  # views, not containers
ROLE_RANK = {"trash": 0, "junk": 1, "drafts": 2, "sent": 3, "inbox": 4}  # which mailbox owns a mail in several
SPECIAL_ROLE = {"inbox": "inbox", "sent": "sent", "drafts": "drafts", "trash": "trash", "junk": "junk"}
DEFAULT_NAME = {"sent": "Sent", "drafts": "Drafts", "trash": "Trash", "junk": "Junk"}
KEYWORD_OUT = {"$seen": "\\Seen", "$flagged": "\\Flagged", "$answered": "\\Answered", "$draft": "\\Draft",
               "$forwarded": "$Forwarded", "$junk": "$Junk", "$notjunk": "$NotJunk", "$phishing": "$Phishing"}
KEYWORD_IN = {"\\seen": "$seen", "\\flagged": "$flagged", "\\answered": "$answered", "\\draft": "$draft"}
KEYWORD_RE = re.compile(r"^[a-z0-9$_.+-]{1,255}$")
BOOK_PREFIX = "@book:"     # target folder names of address books, apart from the mailbox paths
UPLOAD_SLOTS = 4           # uploads at once per target, whatever the pool size (servers advertise maxConcurrentUpload)


class BearerAuth(httpx.Auth):
    """Access token, sent only on requests httpx lets carry credentials (never after a redirect to another host)."""

    def __init__(self, token: str):
        self.token = token

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self.token}"
        yield request


# ------------------------------------------------------------------ pure helpers
def parse_utc(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def format_utc(d: datetime | None) -> str | None:
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def flags_of(keywords: dict | None) -> list[str]:
    out = []
    for k, v in (keywords or {}).items():
        if v:
            f = KEYWORD_OUT.get(k.lower()) or (k if re.match(r"^[A-Za-z0-9$_.+-]+$", k) else None)
            if f:
                out.append(f)
    return out


def keywords_of(flags: list[str]) -> dict[str, bool]:
    out = {}
    for f in flags:
        k = KEYWORD_IN.get(f.lower())
        if k is None:
            k = f.lower()
            if f.startswith("\\") or not KEYWORD_RE.match(k):
                continue
        out[k] = True
    return out


def expand(template: str, **values: str) -> str:
    """URI template level 1 (RFC 6570): {name} replaced by the percent-encoded value."""
    return re.sub(r"\{(\w+)\}", lambda m: quote(values.get(m.group(1), ""), safe=""), template)


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:20]  # noqa: S324 - identifier, not security


def key_of(prefix: str, ident: str) -> str:
    k = prefix + ident
    return k if len(k) <= 190 else prefix + "h" + _digest(ident)


def mailbox_paths(boxes: list[dict]) -> dict[str, list[str]]:
    """{id: [names from the root]} of a Mailbox/get list. A broken parent chain stops at the missing parent."""
    by = {b["id"]: b for b in boxes}
    out = {}
    for b in boxes:
        path, cur, seen = [], b, set()
        while cur is not None and cur["id"] not in seen:
            seen.add(cur["id"])
            path.append(cur.get("name") or cur["id"])
            cur = by.get(cur.get("parentId"))
        out[b["id"]] = list(reversed(path))
    return out


def owner_of(mailbox_ids: dict, known: dict[str, Any], rank: dict[str, int], paths: dict[str, list[str]]) -> str | None:
    """The one mailbox that carries a mail kept in several (labels): a mail is migrated once. Trash and junk first,
    then drafts, sent, inbox, then the path, so the choice never changes between two runs."""
    ids = [i for i, v in mailbox_ids.items() if v and i in known]
    if not ids:
        return None
    return min(ids, key=lambda i: (rank.get(i, 9), "/".join(paths[i]).lower(), i))


# ------------------------------------------------------------------ client
class JmapClient:
    """One HTTP connection to a JMAP server. Credentials only go to the host of the session URL."""

    def __init__(self, config: dict, secret: str, transport: httpx.BaseTransport | None = None):
        self.url = config["url"]
        self.account_wanted = config.get("account_id") or None
        bearer = config.get("auth") == "bearer"
        if not bearer and not config.get("login"):
            raise ConnectorError("a login is needed for basic authentication", ConnectorError.CONFIG)
        self._auth = BearerAuth(secret) if bearer else httpx.BasicAuth(config.get("login") or "", secret)
        self._http = httpx.Client(verify=bool(config.get("verify_tls", True)), timeout=120.0, follow_redirects=True,
                                  transport=transport)
        self.session: dict = {}
        self.host = ""
        self.account_id = ""
        self.limits: dict = {}

    def close(self) -> None:
        self._http.close()

    # -- HTTP
    def _send(self, method: str, url: str, **kw) -> httpx.Response:
        if self.host and urlsplit(url).hostname != self.host:
            raise ConnectorError(f"the server points at another host ({urlsplit(url).hostname}): credentials are not "
                                 "sent there", ConnectorError.CONFIG)
        try:
            resp = self._http.request(method, url, auth=self._auth, **kw)
        except httpx.HTTPError as e:
            raise ConnectorError(f"{method} {url}: {type(e).__name__}: {e}", ConnectorError.UNREACHABLE) from e
        sc = resp.status_code
        if sc in (401, 403):
            raise ConnectorError(f"{method} {url} -> HTTP {sc}: credentials refused", ConnectorError.AUTH)
        if sc == 429 or sc >= 500:
            raise ConnectorError(f"{method} {url} -> HTTP {sc}", ConnectorError.TRANSIENT)
        return resp

    def connect(self) -> None:
        """Session resource (RFC 8620 section 2): capabilities, accounts, and the URLs for API, upload, download."""
        resp = self._send("GET", self.url, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            raise ConnectorError(f"session {self.url} -> HTTP {resp.status_code}", ConnectorError.CONFIG)
        try:
            s = resp.json()
            api = s["apiUrl"]
            s["accounts"], s["capabilities"]
        except (ValueError, KeyError, TypeError) as e:
            raise ConnectorError(f"{self.url} is not a JMAP session resource", ConnectorError.CONFIG) from e
        self.session = s
        self.host = urlsplit(str(resp.url)).hostname or ""
        if urlsplit(api).hostname != self.host:
            raise ConnectorError("apiUrl is on another host than the session: credentials are not sent there",
                                 ConnectorError.CONFIG)
        self.limits = (s["capabilities"].get(CORE) or {})

    def pick_account(self, capability: str) -> str | None:
        """Account to use for a capability: the configured one, else the primary one."""
        accounts = self.session.get("accounts") or {}
        if self.account_wanted:
            if self.account_wanted not in accounts:
                raise ConnectorError(f"account {self.account_wanted} is not in the session", ConnectorError.CONFIG)
            return self.account_wanted if capability in (accounts[self.account_wanted].get("accountCapabilities") or {}) else None
        primary = (self.session.get("primaryAccounts") or {}).get(capability)
        if primary:
            return primary
        for aid, acc in accounts.items():
            if capability in (acc.get("accountCapabilities") or {}):
                return aid
        return None

    def has_capability(self, capability: str) -> bool:
        return self.pick_account(capability) is not None

    def api(self, calls: list[tuple[str, dict, str]], using: list[str]) -> dict[str, tuple[str, dict]]:
        """One request with several method calls (RFC 8620 section 3), {call id: (name, arguments)}. A method that
        failed raises; calls that use a back-reference ride in the same request."""
        maxc = int(self.limits.get("maxCallsInRequest") or 0)
        if maxc and len(calls) > maxc:
            raise ConnectorError(f"{len(calls)} calls in a request, the server accepts {maxc}", ConnectorError.CONFIG)
        body = {"using": using, "methodCalls": [[n, a, i] for n, a, i in calls]}
        resp = self._send("POST", self.session["apiUrl"], json=body)
        if resp.status_code != 200:
            kind = ConnectorError.CONFIG if resp.status_code in (400, 404, 415) else ConnectorError.REJECTED
            detail = ""
            try:
                detail = f" {resp.json().get('type', '')} {resp.json().get('detail', '')}".rstrip()
            except ValueError:
                pass
            raise ConnectorError(f"JMAP request -> HTTP {resp.status_code}{detail}", kind)
        try:
            out = {}
            for name, args, cid in resp.json()["methodResponses"]:
                if name == "error":
                    raise_method_error(args)
                out[cid] = (name, args)
            return out
        except (ValueError, KeyError, TypeError) as e:
            raise ConnectorError(f"unreadable JMAP response: {e}", ConnectorError.TRANSIENT) from e

    def page_size(self) -> int:
        mo = int(self.limits.get("maxObjectsInGet") or PAGE)
        return max(1, min(PAGE, mo))

    def upload(self, data: bytes, content_type: str) -> str:
        """Blob upload (RFC 8620 section 6.1). Returns the blobId."""
        url = expand(self.session["uploadUrl"], accountId=self.account_id)
        mx = int(self.limits.get("maxSizeUpload") or 0)
        if mx and len(data) > mx:
            raise ConnectorError(f"{len(data)} bytes, the server accepts uploads up to {mx}", ConnectorError.REJECTED)
        resp = self._send("POST", url, content=data, headers={"Content-Type": content_type})
        if resp.status_code == 413:
            raise ConnectorError("upload refused: too large", ConnectorError.REJECTED)
        if resp.status_code == 507:
            raise ConnectorError("upload refused: over quota", ConnectorError.QUOTA)
        if resp.status_code not in (200, 201):
            raise ConnectorError(f"upload -> HTTP {resp.status_code}", ConnectorError.REJECTED)
        try:
            return resp.json()["blobId"]
        except (ValueError, KeyError, TypeError) as e:
            raise ConnectorError("upload answered without a blobId", ConnectorError.TRANSIENT) from e

    def download(self, blob_id: str, content_type: str, name: str) -> bytes | None:
        """Blob download (RFC 8620 section 6.2). None when the blob is gone."""
        url = expand(self.session["downloadUrl"], accountId=self.account_id, blobId=blob_id, type=content_type, name=name)
        resp = self._send("GET", url)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ConnectorError(f"download -> HTTP {resp.status_code}", ConnectorError.REJECTED)
        return resp.content


_METHOD_ERRORS = {
    "serverUnavailable": ConnectorError.TRANSIENT, "serverFail": ConnectorError.TRANSIENT,
    "serverPartialFail": ConnectorError.TRANSIENT, "rateLimit": ConnectorError.TRANSIENT,
    "forbidden": ConnectorError.AUTH, "accountNotFound": ConnectorError.CONFIG,
    "accountNotSupportedByMethod": ConnectorError.CONFIG, "accountReadOnly": ConnectorError.CONFIG,
    "unknownMethod": ConnectorError.CONFIG, "invalidArguments": ConnectorError.REJECTED,
    "overQuota": ConnectorError.QUOTA, "tooLarge": ConnectorError.REJECTED,
}


class MethodError(ConnectorError):
    def __init__(self, msg: str, kind: str, jmap_type: str):
        super().__init__(msg, kind)
        self.jmap_type = jmap_type


def raise_method_error(args: dict) -> None:
    t = str((args or {}).get("type") or "serverFail")
    raise MethodError(f"JMAP error {t}: {(args or {}).get('description') or ''}".strip(),
                      _METHOD_ERRORS.get(t, ConnectorError.REJECTED), t)


def config_secret(secrets: dict) -> str:
    s = secrets.get("password") or ""
    if not s:
        raise ConnectorError("no password or token", ConnectorError.CONFIG)
    return s


def identity(config: dict) -> str:
    return f"{str(config.get('url', '')).lower()}:{config.get('login', '')}:{config.get('account_id', '')}"


# ------------------------------------------------------------------ source
class JmapSource(Source):
    def __init__(self, config: dict, secrets: dict, ctx: dict | None = None):
        self.config = config
        self.secret = secrets.get("password") or ""
        self.transport = (ctx or {}).get("transport")
        self.client: JmapClient | None = None
        self.mail_account = self.contacts_account = None
        self._boxes: dict[str, dict] = {}
        self._paths: dict[str, list[str]] = {}
        self._rank: dict[str, int] = {}
        self._books: dict[str, dict] = {}

    def open(self) -> None:
        self.close()
        c = JmapClient(self.config, config_secret({"password": self.secret}), self.transport)
        try:
            c.connect()
        except BaseException:
            c.close()
            raise
        self.client = c
        self.mail_account = c.pick_account(CAP_MAIL)
        self.contacts_account = c.pick_account(CAP_CONTACTS)
        c.account_id = self.mail_account or self.contacts_account or ""
        if not (self.mail_account or self.contacts_account):
            self.close()
            raise ConnectorError("the account has neither mail nor contacts", ConnectorError.CONFIG)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        self.client = None

    def _c(self) -> JmapClient:
        if self.client is None:
            raise ConnectorError("JMAP source not open", ConnectorError.TRANSIENT)
        return self.client

    # -- folders
    def folders(self) -> list[SourceFolder]:
        out: list[SourceFolder] = []
        if self.mail_account:
            r = self._c().api([("Mailbox/get", {"accountId": self.mail_account, "ids": None}, "m")],
                              [CORE, CAP_MAIL])["m"][1]
            boxes = [b for b in r.get("list") or [] if b.get("role") not in SKIP_ROLES]
            self._boxes = {b["id"]: b for b in boxes}
            self._paths = mailbox_paths(boxes)
            self._rank = {i: ROLE_RANK.get(b.get("role") or "", 9) for i, b in self._boxes.items()}
            for b in sorted(boxes, key=lambda b: "/".join(self._paths[b["id"]]).lower()):
                role = b.get("role")
                path = ["INBOX"] if role == "inbox" else self._paths[b["id"]]
                if self._under_inbox(b):
                    path = ["INBOX"] + self._paths[b["id"]][1:]
                out.append(SourceFolder(path=path, kind=MAIL, special=role if role in SPECIAL_ROLE else None,
                                        count=b.get("totalEmails"), ref=("mail", b["id"])))
        if self.contacts_account:
            r = self._c().api([("AddressBook/get", {"accountId": self.contacts_account, "ids": None}, "a")],
                              [CORE, CAP_CONTACTS])["a"][1]
            self._books = {b["id"]: b for b in r.get("list") or []}
            for b in sorted(self._books.values(), key=lambda b: (b.get("sortOrder") or 0, b.get("name") or "", b["id"])):
                out.append(SourceFolder(path=[b.get("name") or b["id"]], kind=CONTACTS, ref=("book", b["id"])))
        return out

    def _under_inbox(self, box: dict) -> bool:
        """True for a mailbox nested in the inbox: IMAP-like sources name those INBOX/..."""
        cur, seen = box, {box["id"]}
        while cur.get("parentId") and cur["parentId"] not in seen:
            seen.add(cur["parentId"])
            cur = self._boxes.get(cur["parentId"]) or {}
            if cur.get("role") == "inbox":
                return True
        return False

    def _need_folders(self) -> None:
        if not self._boxes and not self._books:
            self.folders()

    # -- listing
    def _list(self, type_name: str, account: str, using: list[str], flt: dict, props: list[str] | None
              ) -> Iterator[dict]:
        """Query + get in one request per page (the ids ride a back-reference), newest first for mail."""
        c = self._c()
        page, position = c.page_size(), 0
        while True:
            q = {"accountId": account, "filter": flt, "position": position, "limit": page, "calculateTotal": True}
            if type_name == "Email":
                q["sort"] = [{"property": "receivedAt", "isAscending": False}]
            g = {"accountId": account, "#ids": {"resultOf": "q", "name": f"{type_name}/query", "path": "/ids"}}
            if props is not None:
                g["properties"] = props
            got = c.api([(f"{type_name}/query", q, "q"), (f"{type_name}/get", g, "g")], using)
            ids = got["q"][1].get("ids") or []
            yield from got["g"][1].get("list") or []
            position += len(ids)
            total = got["q"][1].get("total")
            if not ids or (total is not None and position >= total) or (total is None and len(ids) < page):
                return

    def _mail_item(self, rec: dict) -> SourceItem:
        mid = (rec.get("messageId") or [None])[0]
        date = parse_utc(rec.get("receivedAt"))
        alt = "m:" + _digest(mid) if mid else "d:" + _digest(f"{rec.get('receivedAt')}|{rec.get('size')}")
        return SourceItem(key=key_of("e", rec["id"]), alt_key=alt, date=date, size=rec.get("size"),
                          flags=flags_of(rec.get("keywords")), ref=(rec["blobId"], rec["id"]))

    def _mine(self, folder: SourceFolder, rec: dict) -> bool:
        return owner_of(rec.get("mailboxIds") or {}, self._boxes, self._rank, self._paths) == folder.ref[1]

    def iter_items(self, folder: SourceFolder) -> Iterator[SourceItem]:
        self._need_folders()
        kind, ident = folder.ref
        if kind == "mail":
            for rec in self._list("Email", self.mail_account, [CORE, CAP_MAIL], {"inMailbox": ident}, EMAIL_PROPS):
                if self._mine(folder, rec):
                    yield self._mail_item(rec)
        else:
            for rec in self._list("ContactCard", self.contacts_account, [CORE, CAP_CONTACTS], {"inAddressBook": ident}, None):
                if self._card_owner(rec) == ident:
                    yield self._card_item(rec)

    def _card_owner(self, rec: dict) -> str | None:
        ids = sorted(i for i, v in (rec.get("addressBookIds") or {}).items() if v)
        return ids[0] if ids else None

    def _card_item(self, rec: dict) -> SourceItem:
        uid = rec.get("uid") or ""
        return SourceItem(key=key_of("c", rec["id"]), alt_key=("u:" + _digest(uid)) if uid else None,
                          date=parse_utc(rec.get("updated")), ref=rec)

    # -- incremental
    def changed_since(self, folder: SourceFolder, cursor: str | None) -> Changes:
        """Mail and cards created or updated since the state in `cursor` (Email/changes, ContactCard/changes).
        Destroyed objects are not reported. A state the server cannot calculate from gives a full listing."""
        self._need_folders()
        kind, ident = folder.ref
        tn, account, using = (("Email", self.mail_account, [CORE, CAP_MAIL]) if kind == "mail"
                              else ("ContactCard", self.contacts_account, [CORE, CAP_CONTACTS]))
        c = self._c()
        try:
            state = (json.loads(cursor) or {}).get("state") if cursor else None
        except ValueError:
            state = None
        if state:
            ids: list[str] = []
            try:
                while True:
                    ch = c.api([(f"{tn}/changes", {"accountId": account, "sinceState": state, "maxChanges": 500}, "c")],
                               using)["c"][1]
                    ids += list(ch.get("created") or []) + list(ch.get("updated") or [])
                    state = ch.get("newState") or state
                    if not ch.get("hasMoreChanges"):
                        break
            except MethodError as e:
                if e.jmap_type != "cannotCalculateChanges":
                    raise
                state = None
            if state:
                items = []
                for i in range(0, len(ids), c.page_size()):
                    got = c.api([(f"{tn}/get", {"accountId": account, "ids": ids[i:i + c.page_size()],
                                                **({"properties": EMAIL_PROPS} if tn == "Email" else {})}, "g")],
                                using)["g"][1]
                    for rec in got.get("list") or []:
                        if tn == "Email":
                            if self._mine(folder, rec):
                                items.append(self._mail_item(rec))
                        elif self._card_owner(rec) == ident:
                            items.append(self._card_item(rec))
                return Changes(items, json.dumps({"state": state}), reset=False)
        # first listing, or a state the server forgot: read the state first, so that nothing falls between the two
        st = c.api([(f"{tn}/get", {"accountId": account, "ids": []}, "s")], using)["s"][1].get("state")
        return Changes(list(self.iter_items(folder)), json.dumps({"state": st}) if st else None, reset=True)

    # -- content
    def fetch(self, folder: SourceFolder, item: SourceItem, import_id: str) -> FetchedItem | None:
        if folder.kind == CONTACTS:
            res = jscontact_to_vcard(item.ref)
            if res is None:
                return None
            text, warns = res
            name = (item.ref.get("name") or {}).get("full") or ""
            return FetchedItem(raw=text.encode("utf-8"), subject=name[:500], warnings=warns)
        blob_id, _ident = item.ref
        raw = self._c().download(blob_id, "message/rfc822", "message.eml")
        if raw is None:
            return None
        return FetchedItem(raw=raw, internal_date=item.date, flags=list(item.flags), subject=subject_of(raw))


# ------------------------------------------------------------------ target
class JmapSession(TargetSession):
    def __init__(self, target: "JmapTarget"):
        self.t = target
        self.client: JmapClient | None = None
        self.mail_account = self.contacts_account = None
        self._boxes: dict[str, dict] = {}
        self._books: dict[str, str] = {}

    @property
    def connected(self) -> bool:
        return self.client is not None

    def connect(self) -> None:
        self.close()
        c = JmapClient(self.t.config, config_secret(self.t.secrets), self.t.transport)
        try:
            c.connect()
        except BaseException:
            c.close()
            raise
        self.client = c
        self.mail_account = c.pick_account(CAP_MAIL)
        self.contacts_account = c.pick_account(CAP_CONTACTS)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        self.client = None

    def _c(self) -> JmapClient:
        self.ensure()
        return self.client  # type: ignore[return-value]

    def _mail(self) -> str:
        if not self.mail_account:
            raise ConnectorError("the account has no mail", ConnectorError.CONFIG)
        self._c().account_id = self.mail_account
        return self.mail_account

    def _call(self, calls, using):
        return self._c().api(calls, using)

    # -- mailboxes
    def _load_boxes(self) -> dict[str, list[str]]:
        r = self._call([("Mailbox/get", {"accountId": self._mail(), "ids": None}, "m")], [CORE, CAP_MAIL])["m"][1]
        self._boxes = {b["id"]: b for b in r.get("list") or []}
        return mailbox_paths(list(self._boxes.values()))

    def _find_box(self, parts: list[str], refresh: bool = False) -> str | None:
        if refresh or not self._boxes:
            self._load_boxes()
        paths = mailbox_paths(list(self._boxes.values()))
        low = [p.lower() for p in parts]
        for i, p in paths.items():
            if [x.lower() for x in p] == low:
                return i
        return None

    def plan_folders(self, folders: list[SourceFolder]) -> dict[str, FolderPlan]:
        from ..server.runner import folder_key
        out: dict[str, FolderPlan] = {}
        if folders and folders[0].kind == CONTACTS:
            for f in folders:
                out[folder_key(f.path)] = FolderPlan(BOOK_PREFIX + " - ".join(f.path))
            return out
        paths = self._load_boxes()
        by_role = {b["role"]: i for i, b in self._boxes.items() if b.get("role")}
        root = (self.t.config.get("root_folder") or "").strip().strip("/")

        def clean(parts):
            return [p.replace("/", "_").strip() or "_" for p in parts]

        for f in folders:
            path = clean(f.path)
            name, flag = None, None
            if root:
                name = "/".join([root] + path)
            elif f.special == "inbox" or path == ["INBOX"]:
                name = "/".join(paths[by_role["inbox"]]) if "inbox" in by_role else "Inbox"
            elif path[0].upper() == "INBOX" and "inbox" in by_role:
                name = "/".join(paths[by_role["inbox"]] + path[1:])
            elif f.special in SPECIAL_ROLE:
                role = SPECIAL_ROLE[f.special]
                if role in by_role:
                    name = "/".join(paths[by_role[role]])
                else:
                    name, flag = DEFAULT_NAME.get(role, path[-1]), role
            if name is None:
                name = "/".join(path)
            out[folder_key(f.path)] = FolderPlan(name, flag)
        return out

    def ensure_folder(self, name: str, flag: str | None = None) -> None:
        if name.startswith(BOOK_PREFIX):
            self._ensure_book(name[len(BOOK_PREFIX):])
            return
        parts = [p for p in name.split("/") if p]
        have = self._find_box(parts, refresh=True)
        if have:
            return
        parent = None
        for i, part in enumerate(parts):
            sub = parts[:i + 1]
            bid = self._find_box(sub)
            if bid is None:
                bid = self._create_box(part, parent, flag if i == len(parts) - 1 else None, sub)
            parent = bid

    def _create_box(self, name: str, parent: str | None, role: str | None, full: list[str]) -> str:
        acc = self._mail()
        for use_role in ((True, False) if role else (False,)):
            args: dict[str, Any] = {"name": name, "parentId": parent}
            if role and use_role:
                args["role"] = role
            r = self._call([("Mailbox/set", {"accountId": acc, "create": {"n": args}}, "s")], [CORE, CAP_MAIL])["s"][1]
            made = (r.get("created") or {}).get("n")
            if made:
                self._boxes[made["id"]] = {"id": made["id"], "name": name, "parentId": parent, "role": args.get("role")}
                return made["id"]
            err = (r.get("notCreated") or {}).get("n") or {}
            if err.get("type") == "overQuota":
                raise ConnectorError("cannot create the mailbox: over quota", ConnectorError.QUOTA)
            again = self._find_box(full, refresh=True)  # created by another session meanwhile
            if again:
                return again
        raise ConnectorError(f"mailbox '{name}' refused: {err.get('type')} {err.get('description') or ''}".strip(),
                             ConnectorError.REJECTED)

    # -- address books
    def _load_books(self) -> dict[str, str]:
        acc = self.contacts_account
        if not acc:
            raise ConnectorError("the account has no contacts (urn:ietf:params:jmap:contacts)", ConnectorError.CONFIG)
        r = self._call([("AddressBook/get", {"accountId": acc, "ids": None}, "a")], [CORE, CAP_CONTACTS])["a"][1]
        self._books = {(b.get("name") or "").lower(): b["id"] for b in r.get("list") or []}
        return self._books

    def _ensure_book(self, name: str) -> str:
        books = self._load_books()
        if name.lower() in books:
            return books[name.lower()]
        r = self._call([("AddressBook/set", {"accountId": self.contacts_account, "create": {"b": {"name": name}}}, "s")],
                       [CORE, CAP_CONTACTS])["s"][1]
        made = (r.get("created") or {}).get("b")
        if not made:
            err = (r.get("notCreated") or {}).get("b") or {}
            raise ConnectorError(f"address book '{name}' refused: {err.get('type')}", ConnectorError.REJECTED)
        self._books[name.lower()] = made["id"]
        return made["id"]

    def _book(self, name: str) -> str | None:
        if name.lower() not in self._books:
            self._load_books()
        return self._books.get(name.lower())

    # -- items
    def put(self, folder: str, item: OutgoingItem) -> str | None:
        if item.kind == CONTACTS:
            return self._put_card(folder, item)
        c, acc = self._c(), self._mail()
        parts = [p for p in folder.split("/") if p]
        box = self._find_box(parts) or self._find_box(parts, refresh=True)
        if box is None:
            raise ConnectorError(f"mailbox '{folder}' does not exist", ConnectorError.FOLDER_MISSING)
        with self.t.slots:
            blob = c.upload(item.raw, "message/rfc822")
        imp = {"blobId": blob, "mailboxIds": {box: True}, "keywords": keywords_of(item.flags)}
        if item.internal_date is not None:
            imp["receivedAt"] = format_utc(item.internal_date)
        r = self._call([("Email/import", {"accountId": acc, "emails": {"i": imp}}, "i")], [CORE, CAP_MAIL])["i"][1]
        made = (r.get("created") or {}).get("i")
        if made:
            return made["id"]
        err = (r.get("notCreated") or {}).get("i") or {}
        t = err.get("type")
        if t == "alreadyExists" and err.get("existingId"):
            return err["existingId"]
        if t == "overQuota":
            raise ConnectorError("over quota", ConnectorError.QUOTA)
        if t == "invalidProperties" and "mailboxIds" in (err.get("properties") or []):
            self._boxes = {}
            raise ConnectorError(f"mailbox '{folder}' refused", ConnectorError.FOLDER_MISSING)
        if t in ("serverFail", "serverUnavailable", "rateLimit"):
            raise ConnectorError(f"import: {t}", ConnectorError.TRANSIENT)
        raise ConnectorError(f"import refused: {t} {err.get('description') or ''}".strip(), ConnectorError.REJECTED)

    def _card_uid(self, item: OutgoingItem) -> str:
        return "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, "mccb:" + (item.name or item.import_id)))

    def _put_card(self, folder: str, item: OutgoingItem) -> str | None:
        book = self._book(folder[len(BOOK_PREFIX):])
        if book is None:
            raise ConnectorError(f"address book '{folder}' does not exist", ConnectorError.FOLDER_MISSING)
        try:
            card, _w = vcard_to_jscontact(item.raw.decode("utf-8", "replace"))
        except ValueError as e:
            raise ConnectorError(str(e), ConnectorError.REJECTED) from e
        card["uid"] = self._card_uid(item)
        card["addressBookIds"] = {book: True}
        r = self._call([("ContactCard/set", {"accountId": self.contacts_account, "create": {"k": card}}, "s")],
                       [CORE, CAP_CONTACTS])["s"][1]
        made = (r.get("created") or {}).get("k")
        if made:
            return made["id"]
        err = (r.get("notCreated") or {}).get("k") or {}
        t = err.get("type")
        if t == "overQuota":
            raise ConnectorError("over quota", ConnectorError.QUOTA)
        if t in ("serverFail", "serverUnavailable", "rateLimit"):
            raise ConnectorError(f"contact: {t}", ConnectorError.TRANSIENT)
        found = self._cards_with_uid(self._card_uid(item))  # the server may report a duplicate uid as invalidProperties
        if found:
            return found[0]
        raise ConnectorError(f"contact refused: {t} {err.get('description') or ''} {err.get('properties') or ''}".strip(),
                             ConnectorError.REJECTED)

    def _cards_with_uid(self, uid: str) -> list[str]:
        r = self._call([("ContactCard/query", {"accountId": self.contacts_account, "filter": {"uid": uid}}, "q")],
                       [CORE, CAP_CONTACTS])["q"][1]
        return list(r.get("ids") or [])

    def _header_hits(self, box: str, needle: str) -> Iterator[tuple[str, list[str]]]:
        """(email id, values of our header) for the mails of a mailbox whose header contains `needle`."""
        acc, c = self._mail(), self._c()
        prop = f"header:{IMPORT_HEADER}:asText:all"
        page, position = c.page_size(), 0
        try:
            while True:
                got = c.api([("Email/query", {"accountId": acc, "filter": {"inMailbox": box, "header": [IMPORT_HEADER, needle]},
                                              "position": position, "limit": page}, "q"),
                             ("Email/get", {"accountId": acc, "#ids": {"resultOf": "q", "name": "Email/query", "path": "/ids"},
                                            "properties": ["id", prop]}, "g")], [CORE, CAP_MAIL])
                ids = got["q"][1].get("ids") or []
                for rec in got["g"][1].get("list") or []:
                    yield rec["id"], [str(v).strip() for v in rec.get(prop) or []]
                position += len(ids)
                if len(ids) < page:
                    return
        except MethodError as e:
            if e.jmap_type != "unsupportedFilter":
                raise
        # the server cannot filter on a header: read the header of every mail of the mailbox
        position = 0
        while True:
            got = c.api([("Email/query", {"accountId": acc, "filter": {"inMailbox": box}, "position": position,
                                          "limit": page}, "q"),
                         ("Email/get", {"accountId": acc, "#ids": {"resultOf": "q", "name": "Email/query", "path": "/ids"},
                                        "properties": ["id", prop]}, "g")], [CORE, CAP_MAIL])
            ids = got["q"][1].get("ids") or []
            for rec in got["g"][1].get("list") or []:
                yield rec["id"], [str(v).strip() for v in rec.get(prop) or []]
            position += len(ids)
            if len(ids) < page:
                return

    def find(self, folder: str, item: OutgoingItem) -> list[str]:
        if item.kind == CONTACTS:
            return self._cards_with_uid(self._card_uid(item))
        box = self._find_box([p for p in folder.split("/") if p], refresh=True)
        if box is None:
            return []
        return [i for i, vals in self._header_hits(box, item.import_id) if item.import_id in vals]

    def count_ours(self, folder: str, namespace: str, names: set[str] | None = None) -> int:
        if folder.startswith(BOOK_PREFIX):
            uids = {"urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, "mccb:" + n)) for n in names or ()}
            book = self._book(folder[len(BOOK_PREFIX):])
            if not book:
                return 0
            n, c, page, position = 0, self._c(), self._c().page_size(), 0
            while True:
                got = c.api([("ContactCard/query", {"accountId": self.contacts_account,
                                                    "filter": {"inAddressBook": book}, "position": position,
                                                    "limit": page}, "q"),
                             ("ContactCard/get", {"accountId": self.contacts_account,
                                                  "#ids": {"resultOf": "q", "name": "ContactCard/query", "path": "/ids"},
                                                  "properties": ["id", "uid"]}, "g")], [CORE, CAP_CONTACTS])
                ids = got["q"][1].get("ids") or []
                n += sum(1 for rec in got["g"][1].get("list") or [] if rec.get("uid") in uids)
                position += len(ids)
                if len(ids) < page:
                    return n
        box = self._find_box([p for p in folder.split("/") if p], refresh=True)
        if box is None:
            return 0
        start = f"<{namespace}-"
        return sum(1 for _i, vals in self._header_hits(box, start) if any(v.startswith(start) for v in vals))

    def read(self, folder: str, remote_id: str) -> bytes | None:
        acc, c = self._mail(), self._c()
        r = self._call([("Email/get", {"accountId": acc, "ids": [remote_id], "properties": ["id", "blobId"]}, "g")],
                       [CORE, CAP_MAIL])["g"][1]
        lst = r.get("list") or []
        return c.download(lst[0]["blobId"], "message/rfc822", "message.eml") if lst else None

    def quota(self) -> dict | None:
        """RFC 9425 Quota/get, octets, account scope. None when the server has no quota capability."""
        c = self._c()
        if CAP_QUOTA_URI not in (c.session.get("capabilities") or {}) or not self.mail_account:
            return None
        try:
            r = c.api([("Quota/get", {"accountId": self.mail_account, "ids": None}, "q")],
                      [CORE, CAP_QUOTA_URI])["q"][1]
        except ConnectorError:
            return None
        for q in r.get("list") or []:
            if q.get("resourceType") == "octets" and q.get("scope", "account") == "account" and q.get("hardLimit"):
                return {"used": int(q.get("used") or 0), "limit": int(q["hardLimit"])}
        return None

    def info(self) -> dict:
        c = self._c()
        return {"jmap": {"username": c.session.get("username"), "mail": bool(self.mail_account),
                         "contacts": bool(self.contacts_account)}}


class JmapTarget(Target):
    capabilities = frozenset({CAP_FOLDERS, CAP_FLAGS, CAP_INTERNAL_DATE, CAP_QUOTA, CAP_PARALLEL, CAP_READBACK})

    def __init__(self, config: dict, secrets: dict, ctx: dict | None = None):
        self.config = config
        self.secrets = secrets
        self.transport = (ctx or {}).get("transport")
        self.slots = threading.BoundedSemaphore(UPLOAD_SLOTS)

    def session(self) -> JmapSession:
        return JmapSession(self)


_CONNECTION = (
    registry.Field("url", "Session URL", "url", required=True,
                   help="https://host/.well-known/jmap, or the session URL of the provider."),
    registry.Field("auth", "Authentication", "select", default="basic", options=("basic", "bearer"),
                   help="bearer: the secret below is an access token or an app token."),
    registry.Field("login", "Login", help="Required for basic authentication."),
    registry.Field("account_id", "Account id", help="Empty: the primary account."),
    registry.Field("verify_tls", "Verify the TLS certificate", "boolean", default=True),
    registry.Field("password", "Password or token", "password", required=True, secret=True),
)

registry.register(registry.ConnectorSpec(
    kind="jmap", label="JMAP", description="Mail and contacts of a JMAP server (RFC 8620, 8621, 9610). Calendars: use CalDAV.",
    source=registry.Role(JmapSource, (MAIL, CONTACTS), frozenset({CAP_FOLDERS, CAP_FLAGS, CAP_INTERNAL_DATE, CAP_INCREMENTAL}),
                         _CONNECTION, identity),
    target=registry.Role(JmapTarget, (MAIL, CONTACTS), JmapTarget.capabilities,
                         _CONNECTION + (registry.Field("root_folder", "Root folder",
                                                       help="Put all mail under this mailbox."),), identity)))
