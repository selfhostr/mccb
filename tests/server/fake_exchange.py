"""Simulated EWS and Microsoft Graph servers, at HTTP level (httpx.MockTransport), built from the payloads documented on
Microsoft Learn. They prove the logic of the connectors, not that real Exchange behaves like them: see docs/exchange.md."""
from __future__ import annotations

import base64
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

NS_M = "http://schemas.microsoft.com/exchange/services/2006/messages"
NS_T = "http://schemas.microsoft.com/exchange/services/2006/types"
NS_S = "http://schemas.xmlsoap.org/soap/envelope/"
ET.register_namespace("m", NS_M)
ET.register_namespace("t", NS_T)
ET.register_namespace("s", NS_S)


def t(name: str) -> str:
    return f"{{{NS_T}}}{name}"


def m(name: str) -> str:
    return f"{{{NS_M}}}{name}"


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def utc_text(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ EWS
WELL_KNOWN = ("msgfolderroot", "inbox", "sentitems", "drafts", "deleteditems", "junkemail", "outbox", "calendar", "contacts",
              "tasks")
CLASS_OF = {"inbox": "IPF.Note", "sentitems": "IPF.Note", "drafts": "IPF.Note", "deleteditems": "IPF.Note",
            "junkemail": "IPF.Note", "outbox": "IPF.Note", "calendar": "IPF.Appointment", "contacts": "IPF.Contact",
            "tasks": "IPF.Task"}
FOLDER_TAG = {"IPF.Appointment": "CalendarFolder", "IPF.Contact": "ContactsFolder", "IPF.Task": "TasksFolder"}


class FakeEws:
    """One mailbox. Items are kept as the XML of the protocol: seeded from documented answers, or as received."""

    def __init__(self, password: str = "pw"):
        self.password = password
        self.folders: dict[str, dict] = {}
        self.items: dict[str, dict] = {}   # id -> {folder, el}
        self.calls: list[str] = []
        self.requests: list[ET.Element] = []
        self.headers: list[httpx.Headers] = []
        self.busy_next = 0
        self.busy_http500 = False
        self.page_limit = 2
        self.rules_xml = ""
        self.fail_op: dict[str, str] = {}
        self.deleted_occurrences: list[str] = []
        self.updated_occurrences: dict[str, ET.Element] = {}
        self.sync_invalid = False
        self.next_id = 1
        for name in WELL_KNOWN:
            self.add_folder(name, "Top of Information Store" if name == "msgfolderroot" else name.capitalize(),
                            None if name == "msgfolderroot" else "msgfolderroot", CLASS_OF.get(name, "IPF"), fid=f"id-{name}",
                            well_known=name)

    def add_folder(self, key, name, parent, klass="IPF.Note", fid=None, well_known=None, hidden=False):
        fid = fid or f"id-{key}"
        self.folders[fid] = {"id": fid, "name": name, "parent": f"id-{parent}" if parent and not parent.startswith("id-") else parent,
                             "class": klass, "well_known": well_known, "hidden": hidden}
        return fid

    def add_item(self, folder_id: str, xml: str, rid: str | None = None) -> str:
        rid = rid or f"item{self.next_id}"
        self.next_id += 1
        el = ET.fromstring(f'<wrap xmlns:t="{NS_T}">{xml}</wrap>')[0]
        ident = ET.Element(t("ItemId"), {"Id": rid, "ChangeKey": "ck"})
        el.insert(0, ident)
        self.items[rid] = {"folder": folder_id, "el": el}
        return rid

    def in_folder(self, fid: str) -> list[dict]:
        return [dict(i, id=k) for k, i in self.items.items() if i["folder"] == fid]

    # -- HTTP
    def __call__(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        want = "Basic " + base64.b64encode(f"me@example.test:{self.password}".encode()).decode()
        if auth != want:
            return httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="ews"'})
        root = ET.fromstring(request.content)
        self.headers.append(request.headers)
        self.requests.append(root)
        body = root.find(f"{{{NS_S}}}Body")[0]
        op = body.tag.rsplit("}", 1)[-1]
        self.calls.append(op)
        if self.busy_next > 0:
            self.busy_next -= 1
            status = 500 if self.busy_http500 else 200
            return self._xml(self._busy(), status)
        if op in self.fail_op:
            return self._xml(self._messages(op, [self._error(self.fail_op.pop(op))]))
        return self._xml(getattr(self, f"_op_{op}")(body))

    def _xml(self, inner: str, status: int = 200) -> httpx.Response:
        return httpx.Response(status, content=(
            f'<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="{NS_S}" xmlns:m="{NS_M}" xmlns:t="{NS_T}">'
            f"<s:Body>{inner}</s:Body></s:Envelope>").encode("utf-8"), headers={"Content-Type": "text/xml"})

    @staticmethod
    def _busy() -> str:
        return ("<s:Fault><faultcode>a:ErrorServerBusy</faultcode><faultstring>The server cannot service this request right "
                "now. Try again later.</faultstring><detail><m:ResponseCode>ErrorServerBusy</m:ResponseCode>"
                '<m:MessageXml><t:Value Name="BackOffMilliseconds">1500</t:Value></m:MessageXml></detail></s:Fault>')

    @staticmethod
    def _error(code: str, text: str = "refused") -> str:
        return f'<m:Message ResponseClass="Error"><m:MessageText>{text}</m:MessageText><m:ResponseCode>{code}</m:ResponseCode></m:Message>'

    @staticmethod
    def _messages(op: str, parts: list[str]) -> str:
        return f"<m:{op}Response><m:ResponseMessages>" + "".join(parts) + f"</m:ResponseMessages></m:{op}Response>"

    def _folder_id(self, ref: ET.Element) -> str:
        if ref.tag == t("DistinguishedFolderId"):
            return f"id-{ref.get('Id')}"
        return ref.get("Id") or ""

    @staticmethod
    def _serial(el: ET.Element, drop=("MimeContent",)) -> str:
        copy = ET.fromstring(ET.tostring(el))
        for name in drop:
            for x in copy.findall(t(name)):
                copy.remove(x)
        return ET.tostring(copy, encoding="unicode")

    def _folder_xml(self, f: dict) -> str:
        tag = FOLDER_TAG.get(f["class"], "Folder")
        hidden = ('<t:ExtendedProperty><t:ExtendedFieldURI PropertyTag="0x10f4" PropertyType="Boolean"/>'
                  "<t:Value>true</t:Value></t:ExtendedProperty>") if f["hidden"] else ""
        count = sum(1 for i in self.items.values() if i["folder"] == f["id"])
        return (f'<t:{tag}><t:FolderId Id="{f["id"]}"/><t:ParentFolderId Id="{f["parent"] or ""}"/><t:FolderClass>{f["class"]}'
                f"</t:FolderClass><t:DisplayName>{f['name']}</t:DisplayName><t:TotalCount>{count}</t:TotalCount>{hidden}</t:{tag}>")

    def _op_GetFolder(self, body) -> str:
        parts = []
        for ref in body.find(m("FolderIds")):
            f = self.folders.get(self._folder_id(ref))
            if f is None:
                parts.append(self._error("ErrorFolderNotFound"))
                continue
            parts.append('<m:GetFolderResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>'
                         f"<m:Folders>{self._folder_xml(f)}</m:Folders></m:GetFolderResponseMessage>")
        return self._messages("GetFolder", parts)

    def _descendants(self, fid: str, deep: bool) -> list[dict]:
        out = []
        for f in self.folders.values():
            if f["parent"] == fid:
                out.append(f)
                if deep:
                    out.extend(self._descendants(f["id"], True))
        return out

    def _op_FindFolder(self, body) -> str:
        parent = self._folder_id(body.find(m("ParentFolderIds"))[0])
        if parent not in self.folders:
            return self._messages("FindFolder", [self._error("ErrorFolderNotFound")])
        rows = self._descendants(parent, body.get("Traversal") == "Deep")
        view = body.find(m("IndexedPageFolderView"))
        offset = int(view.get("Offset"))
        page = rows[offset:offset + min(int(view.get("MaxEntriesReturned")), self.page_limit)]
        last = "true" if offset + len(page) >= len(rows) else "false"
        return self._messages("FindFolder", ['<m:FindFolderResponseMessage ResponseClass="Success"><m:ResponseCode>NoError'
                                             f'</m:ResponseCode><m:RootFolder TotalItemsInView="{len(rows)}" '
                                             f'IncludesLastItemInRange="{last}"><t:Folders>'
                                             + "".join(self._folder_xml(f) for f in page)
                                             + "</t:Folders></m:RootFolder></m:FindFolderResponseMessage>"])

    def _op_CreateFolder(self, body) -> str:
        parent = self._folder_id(body.find(m("ParentFolderId"))[0])
        el = body.find(m("Folders"))[0]
        name = el.findtext(t("DisplayName"))
        if parent not in self.folders:
            return self._messages("CreateFolder", [self._error("ErrorParentFolderNotFound")])
        for f in self.folders.values():
            if f["parent"] == parent and f["name"].lower() == name.lower():
                return self._messages("CreateFolder", [self._error("ErrorFolderExists")])
        tag = el.tag.rsplit("}", 1)[-1]
        klass = {"CalendarFolder": "IPF.Appointment", "ContactsFolder": "IPF.Contact", "TasksFolder": "IPF.Task"}.get(
            tag, el.findtext(t("FolderClass")) or "IPF.Note")
        fid = f"new-{len(self.folders)}"
        self.folders[fid] = {"id": fid, "name": name, "parent": parent, "class": klass, "well_known": None, "hidden": False}
        return self._messages("CreateFolder", ['<m:CreateFolderResponseMessage ResponseClass="Success"><m:ResponseCode>NoError'
                                               f'</m:ResponseCode><m:Folders><t:{tag}><t:FolderId Id="{fid}"/></t:{tag}></m:Folders>'
                                               "</m:CreateFolderResponseMessage>"])

    @staticmethod
    def _ext_of(el: ET.Element, name: str) -> str | None:
        for p in el.findall(t("ExtendedProperty")):
            uri = p.find(t("ExtendedFieldURI"))
            if uri is not None and uri.get("PropertyName") == name:
                return p.findtext(t("Value"))
        return None

    def _matches(self, el: ET.Element, restriction: ET.Element | None) -> bool:
        if restriction is None:
            return True
        test = restriction[0]
        name = test.find(t("ExtendedFieldURI")).get("PropertyName")
        value = self._ext_of(el, name)
        if test.tag == t("IsEqualTo"):
            wanted = test.find(t("FieldURIOrConstant")).find(t("Constant")).get("Value")
            return value is not None and value.lower() == wanted.lower()  # documented: IsEqualTo ignores case
        wanted = test.find(t("Constant")).get("Value")
        return value is not None and value.lower().startswith(wanted.lower())

    def _occurrences(self, master_id: str, el: ET.Element) -> list[dict]:
        rec = el.find(t("Recurrence"))
        start, end = utc(el.findtext(t("Start"))), utc(el.findtext(t("End")))
        step = timedelta(days=7) if rec.find(t("WeeklyRecurrence")) is not None else timedelta(days=1)
        number = rec.find(t("NumberedRecurrence"))
        count = int(number.findtext(t("NumberOfOccurrences"))) if number is not None else 20
        return [{"id": f"occ-{master_id}-{i}", "start": start + i * step, "end": end + i * step} for i in range(count)]

    def _calendar_view(self, parent: str, view: ET.Element) -> str:
        begin, finish = utc(view.get("StartDate")), utc(view.get("EndDate"))
        rows = []
        for i in self.in_folder(parent):
            el = i["el"]
            if el.tag != t("CalendarItem"):
                continue
            uid = el.findtext(t("UID"))
            if el.find(t("Recurrence")) is None:
                continue
            for occ in self._occurrences(i["id"], el):
                if begin <= occ["start"] <= finish and occ["id"] not in self.deleted_occurrences:
                    rows.append(f'<t:CalendarItem><t:ItemId Id="{occ["id"]}"/><t:UID>{uid}</t:UID><t:Start>'
                                f'{utc_text(occ["start"])}</t:Start><t:OriginalStart>{utc_text(occ["start"])}</t:OriginalStart>'
                                f"<t:CalendarItemType>Occurrence</t:CalendarItemType></t:CalendarItem>")
        return self._messages("FindItem", ['<m:FindItemResponseMessage ResponseClass="Success"><m:ResponseCode>NoError'
                                           f'</m:ResponseCode><m:RootFolder TotalItemsInView="{len(rows)}" IncludesLastItemInRange'
                                           f'="true"><t:Items>' + "".join(rows) + "</t:Items></m:RootFolder></m:FindItemResponseMessage>"])

    def _op_FindItem(self, body) -> str:
        parent = self._folder_id(body.find(m("ParentFolderIds"))[0])
        if parent not in self.folders:
            return self._messages("FindItem", [self._error("ErrorFolderNotFound")])
        if body.find(m("CalendarView")) is not None:
            return self._calendar_view(parent, body.find(m("CalendarView")))
        restriction = body.find(m("Restriction"))
        rows = [i for i in self.in_folder(parent) if self._matches(i["el"], restriction)]
        view = body.find(m("IndexedPageItemView"))
        offset = int(view.get("Offset"))
        page = rows[offset:offset + min(int(view.get("MaxEntriesReturned")), self.page_limit)]
        last = "true" if offset + len(page) >= len(rows) else "false"
        return self._messages("FindItem", ['<m:FindItemResponseMessage ResponseClass="Success"><m:ResponseCode>NoError'
                                           f'</m:ResponseCode><m:RootFolder TotalItemsInView="{len(rows)}" '
                                           f'IncludesLastItemInRange="{last}"><t:Items>'
                                           + "".join(self._serial(i["el"], ("MimeContent", "Body")) for i in page)
                                           + "</t:Items></m:RootFolder></m:FindItemResponseMessage>"])

    def _op_GetItem(self, body) -> str:
        shape = body.find(m("ItemShape"))
        mime = shape.findtext(t("IncludeMimeContent")) == "true"
        parts = []
        for ref in body.find(m("ItemIds")):
            item = self.items.get(ref.get("Id"))
            if item is None:
                parts.append(self._error("ErrorItemNotFound"))
                continue
            xml = self._serial(item["el"], () if mime else ("MimeContent",))
            parts.append('<m:GetItemResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>'
                         f"<m:Items>{xml}</m:Items></m:GetItemResponseMessage>")
        return self._messages("GetItem", parts)

    def _op_CreateItem(self, body) -> str:
        parent = self._folder_id(body.find(m("SavedItemFolderId"))[0])
        if parent not in self.folders:
            return self._messages("CreateItem", [self._error("ErrorFolderNotFound")])
        parts = []
        for el in body.find(m("Items")):
            rid = f"created{self.next_id}"
            self.next_id += 1
            el.insert(0, ET.Element(t("ItemId"), {"Id": rid, "ChangeKey": "ck"}))
            self.items[rid] = {"folder": parent, "el": el, "attrs": dict(body.attrib)}
            tag = el.tag.rsplit("}", 1)[-1]
            parts.append('<m:CreateItemResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>'
                         f'<m:Items><t:{tag}><t:ItemId Id="{rid}" ChangeKey="ck"/></t:{tag}></m:Items></m:CreateItemResponseMessage>')
        return self._messages("CreateItem", parts)

    def _op_UpdateItem(self, body) -> str:
        change = body.find(m("ItemChanges"))[0]
        self.updated_occurrences[change.find(t("ItemId")).get("Id")] = change
        self.last_update_attrs = dict(body.attrib)
        return self._messages("UpdateItem", ['<m:UpdateItemResponseMessage ResponseClass="Success"><m:ResponseCode>NoError'
                                             "</m:ResponseCode></m:UpdateItemResponseMessage>"])

    def _op_DeleteItem(self, body) -> str:
        for ref in body.find(m("ItemIds")):
            rid = ref.get("Id")
            if rid.startswith("occ-"):
                self.deleted_occurrences.append(rid)
            self.items.pop(rid, None)
        return self._messages("DeleteItem", ['<m:DeleteItemResponseMessage ResponseClass="Success"><m:ResponseCode>NoError'
                                             "</m:ResponseCode></m:DeleteItemResponseMessage>"])

    def _op_SyncFolderItems(self, body) -> str:
        fid = self._folder_id(body.find(m("SyncFolderId"))[0])
        state = body.findtext(m("SyncState"))
        if state and self.sync_invalid:
            self.sync_invalid = False
            return self._messages("SyncFolderItems", [self._error("ErrorInvalidSyncStateData")])
        offset = int(state.split("-")[1]) if state else 0
        rows = self.in_folder(fid)
        page = rows[offset:offset + self.page_limit]
        new = offset + len(page)
        last = "true" if new >= len(rows) else "false"
        changes = "".join(f"<t:Create>{self._serial(i['el'], ('MimeContent', 'Body'))}</t:Create>" for i in page)
        return self._messages("SyncFolderItems", [
            '<m:SyncFolderItemsResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>'
            f"<m:SyncState>state-{new}</m:SyncState><m:IncludesLastItemInRange>{last}</m:IncludesLastItemInRange>"
            f"<m:Changes>{changes}</m:Changes></m:SyncFolderItemsResponseMessage>"])

    def _op_GetInboxRules(self, body) -> str:
        return ('<m:GetInboxRulesResponse ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>'
                f"<m:OutlookRuleBlobExists>false</m:OutlookRuleBlobExists><m:InboxRules>{self.rules_xml}</m:InboxRules>"
                "</m:GetInboxRulesResponse>")


# ------------------------------------------------------------------ Graph
class FakeGraph:
    """One mailbox of one tenant. The documented paths the connector uses, nothing else: any other path is an error."""
    SECRET = "s3cret"

    def __init__(self, mailbox: str = "me@contoso.test"):
        self.mailbox = mailbox
        self.mail_folders = {name: {"id": f"mf-{name}", "displayName": name.capitalize(), "parent": None}
                             for name in ("inbox", "sentitems", "drafts", "deleteditems", "junkemail", "outbox")}
        self.messages: dict[str, dict] = {}
        self.calendars = {"cal-1": {"id": "cal-1", "name": "Calendar", "isDefaultCalendar": True}}
        self.events: dict[str, dict] = {}
        self.instances: dict[str, list[dict]] = {}
        self.contact_folders: dict[str, dict] = {}
        self.contacts: dict[str, dict] = {}
        self.lists = {"list-1": {"id": "list-1", "displayName": "Tasks", "wellknownListName": "defaultList"}}
        self.tasks: dict[str, dict] = {}
        self.page_limit = 2
        self.seq = 1
        self.tokens = 0
        self.token_requests: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.throttle: list[int] = []  # Retry-After of the next answers
        self.expire_next_token = False
        self.fail: dict[str, tuple[int, str]] = {}  # "POST:/calendar/events" -> (status, code)
        self.delta_expired = False
        self.delta_calls: list[dict] = []
        self.forbid: set[str] = set()
        self.base_versions: list[str] = []
        # tenant directory: users as Graph returns them, the userPurpose of each ("shared", 404, 403; absent: "user")
        self.directory: list[dict] = []
        self.purposes: dict[str, str | int] = {}
        self.roles: list[str] | None = None   # None: the access token is not a JWT
        self.org_name: str | None = "Contoso Ltd"
        self.last_token = ""

    def _access_token(self) -> str:
        if self.roles is None:
            return f"tok{self.tokens}"
        enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
        return f"{enc({'alg': 'none'})}.{enc({'roles': self.roles})}.tok{self.tokens}"

    def new_id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}{self.seq}"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host.startswith("login."):
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            self.token_requests.append(form)
            if form.get("client_secret") != self.SECRET:
                return httpx.Response(401, json={"error": "invalid_client", "error_description": "AADSTS7000215: wrong secret"})
            self.tokens += 1
            self.last_token = self._access_token()
            return httpx.Response(200, json={"access_token": self.last_token, "expires_in": 3599, "token_type": "Bearer"})
        self.requests.append(request)
        if self.throttle:
            return httpx.Response(429, headers={"Retry-After": str(self.throttle.pop(0))}, json={
                "error": {"code": "ApplicationThrottled", "message": "too many requests"}})
        if request.headers.get("authorization") != f"Bearer {self.last_token}" or self.expire_next_token:
            self.expire_next_token = False
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken", "message": "expired"}})
        upath = urllib.parse.unquote(url.path)
        if (d := re.match(r"^/(?:v1\.0|beta)(/users|/organization)$", upath)) and request.method == "GET":
            if d.group(1) == "/users":
                return self._page(request, self.directory, dict(url.params.multi_items()))
            return httpx.Response(200, json={"value": [{"displayName": self.org_name}] if self.org_name else []})
        m_ = re.match(r"^/(v1\.0|beta)/users/([^/]+)(/.*)$", upath)
        if not m_:
            return self._err(404, "ResourceNotFound")
        self.base_versions.append(m_.group(1))
        ref = m_.group(2)
        known = next((u for u in self.directory if u["id"] == ref), None)
        if known is not None and m_.group(3) == "/mailboxSettings":
            purpose = self.purposes.get(ref, "user")
            if isinstance(purpose, int):
                return self._err(purpose, "ErrorInvalidUser" if purpose == 404 else "ErrorAccessDenied")
            return httpx.Response(200, json={"userPurpose": purpose})
        if known is not None and m_.group(3).startswith("/mailFolders/inbox") and known.get("mail", "") != self.mailbox:
            return httpx.Response(200, json={"id": "mf-inbox"})
        if known is not None and known.get("mail"):
            ref = known["mail"]  # a user id stands for the address
        if ref.lower() != self.mailbox.lower():
            return self._err(404, "ErrorInvalidUser")
        path = m_.group(3)
        key = f"{request.method}:{path}"
        if key in self.fail:
            status, code = self.fail.pop(key)
            return self._err(status, code)
        for prefix in self.forbid:
            if path.startswith(prefix):
                return self._err(403, "ErrorAccessDenied")
        return self._route(request, path, dict(url.params.multi_items()), m_.group(1))

    @staticmethod
    def _err(status: int, code: str, message: str = "refused") -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": message}})

    def _page(self, request: httpx.Request, rows: list[dict], params: dict) -> httpx.Response:
        skip = int(params.get("$skip") or 0)
        top = min(int(params.get("$top") or 10), self.page_limit)
        page = [{k: v for k, v in r.items() if k != "mime"} for r in rows[skip:skip + top]]
        body: dict = {"value": page}
        if skip + top < len(rows):
            next_params = {**params, "$skip": str(skip + top)}
            body["@odata.nextLink"] = str(request.url.copy_with(params=next_params))
        return httpx.Response(200, json=body)

    @staticmethod
    def _filter(rows: list[dict], flt: str | None) -> list[dict]:
        if not flt:
            return rows
        got = re.match(r"^singleValueExtendedProperties/Any\(ep: ep/id eq '([^']+)' and "
                       r"(?:ep/value eq '(.*)'|startswith\(ep/value, '(.*)'\))\)$", flt)
        assert got, f"filter not supported by the simulated server: {flt}"
        prop, equal, prefix = got.groups()
        out = []
        for r in rows:
            for p in r.get("singleValueExtendedProperties") or []:
                value = str(p.get("value"))
                if p.get("id", "").lower() == prop.lower() and (
                        (equal is not None and value.lower() == equal.replace("''", "'").lower())  # documented: case-insensitive
                        or (prefix is not None and value.lower().startswith(prefix.replace("''", "'").lower()))):
                    out.append(r)
                    break
        return out

    def _collection(self, request, path, params, rows, prefix, parent_key=None):
        return self._page(request, self._filter(rows, params.get("$filter")), params)

    def _route(self, request, path, params, version) -> httpx.Response:
        method = request.method
        body = json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith(
            "application/json") else None

        if (g := re.match(r"^/mailFolders/([^/]+)/messageRules$", path)):
            return self._page(request, getattr(self, "rules", []), params)
        if (g := re.match(r"^/mailFolders/([^/]+)/importMime$", path)) and method == "POST":
            assert version == "beta" and request.headers.get("content-type") == "text/plain"
            return self._create_mime(self._mf(g.group(1)), request.content, draft=False)
        if (g := re.match(r"^/mailFolders/([^/]+)/messages/delta$", path)):
            return self._delta(request, params, g.group(1))
        if (g := re.match(r"^/mailFolders/([^/]+)/messages$", path)):
            fid = self._mf(g.group(1))
            if fid is None:
                return self._err(404, "ErrorItemNotFound")
            if method == "POST":
                assert request.headers.get("content-type") == "text/plain"
                return self._create_mime(fid, request.content, draft=True)
            return self._collection(request, path, params, [x for x in self.messages.values() if x["parentFolderId"] == fid], "")
        if (g := re.match(r"^/mailFolders/([^/]+)/childFolders$", path)):
            fid = self._mf(g.group(1))
            if method == "POST":
                return self._create_mail_folder(body, fid)
            return self._page(request, [f for f in self.mail_folders.values() if f["parent"] == fid], params)
        if path == "/mailFolders":
            if method == "POST":
                return self._create_mail_folder(body, None)
            return self._page(request, [dict(f, childFolderCount=sum(1 for x in self.mail_folders.values() if x["parent"] == f["id"]))
                                        for f in self.mail_folders.values() if f["parent"] is None], params)
        if (g := re.match(r"^/mailFolders/([^/]+)$", path)):
            fid = self._mf(g.group(1))
            return httpx.Response(200, json={"id": fid}) if fid else self._err(404, "ErrorItemNotFound")
        if (g := re.match(r"^/messages/([^/]+)/\$value$", path)):
            m2 = self.messages.get(g.group(1))
            return httpx.Response(200, content=m2["mime"]) if m2 else self._err(404, "ErrorItemNotFound")
        if (g := re.match(r"^/messages/([^/]+)$", path)) and method == "GET":
            m2 = self.messages.get(g.group(1))
            if m2 is None:
                return self._err(404, "ErrorItemNotFound")
            out = {k: v for k, v in m2.items() if k != "mime"}
            if "singleValueExtendedProperties" not in (params.get("$expand") or ""):
                out.pop("singleValueExtendedProperties", None)
            return httpx.Response(200, json=out)
        if (g := re.match(r"^/messages/([^/]+)$", path)) and method == "PATCH":
            m2 = self.messages.get(g.group(1))
            if m2 is None:
                return self._err(404, "ErrorItemNotFound")
            props = {p["id"]: p for p in m2.get("singleValueExtendedProperties") or []}
            for p in body.pop("singleValueExtendedProperties", []):
                props[p["id"]] = p
            m2.update(body, singleValueExtendedProperties=list(props.values()))
            return httpx.Response(200, json={"id": m2["id"]})
        if path == "/calendars":
            if method == "POST":
                cid = self.new_id("cal-")
                self.calendars[cid] = {"id": cid, "name": body["name"]}
                return httpx.Response(201, json=self.calendars[cid])
            return self._page(request, list(self.calendars.values()), params)
        if path == "/calendar/events" or (g := re.match(r"^/calendars/([^/]+)/events$", path)):
            cid = "cal-1" if path == "/calendar/events" else g.group(1)
            if cid not in self.calendars:
                return self._err(404, "ErrorItemNotFound")
            if method == "POST":
                return self._create_event(cid, body)
            return self._collection(request, path, params, [e for e in self.events.values() if e["calendar"] == cid], "")
        if (g := re.match(r"^/events/([^/]+)/instances$", path)):
            return self._page(request, self.instances.get(g.group(1)) or [], params)
        if (g := re.match(r"^/events/([^/]+)$", path)):
            return self._event_item(method, g.group(1), body, params)
        if path == "/contacts" or (g := re.match(r"^/contactFolders/([^/]+)/contacts$", path)):
            fid = None if path == "/contacts" else g.group(1)
            if method == "POST":
                cid = self.new_id("c")
                self.contacts[cid] = dict(body, id=cid, folder=fid)
                return httpx.Response(201, json={"id": cid})
            return self._collection(request, path, params, [c for c in self.contacts.values() if c.get("folder") == fid], "")
        if (g := re.match(r"^/contactFolders/([^/]+)/contacts/delta$", path)):  # no /contacts/delta in v1.0
            return self._tracked(request, [c for c in self.contacts.values() if c.get("folder") == g.group(1)], params)
        if (g := re.match(r"^(?:/contactFolders/[^/]+)?/contacts/([^/]+)$", path)):
            c = self.contacts.get(g.group(1))
            return httpx.Response(200, json=c) if c else self._err(404, "ErrorItemNotFound")
        if path == "/contactFolders":
            if method == "POST":
                fid = self.new_id("cf-")
                self.contact_folders[fid] = {"id": fid, "displayName": body["displayName"]}
                return httpx.Response(201, json=self.contact_folders[fid])
            return self._page(request, list(self.contact_folders.values()), params)
        if (g := re.match(r"^/contactFolders/([^/]+)/childFolders$", path)):
            return self._page(request, [], params)
        if path == "/todo/lists":
            if method == "POST":
                lid = self.new_id("list-")
                self.lists[lid] = {"id": lid, "displayName": body["displayName"]}
                return httpx.Response(201, json=self.lists[lid])
            return self._page(request, list(self.lists.values()), params)
        if (g := re.match(r"^/todo/lists/([^/]+)/tasks$", path)):
            if method == "POST":
                tid = self.new_id("t")
                self.tasks[tid] = dict(body, id=tid, listId=g.group(1))
                return httpx.Response(201, json={"id": tid})
            return self._page(request, [x for x in self.tasks.values() if x["listId"] == g.group(1)], params)
        if (g := re.match(r"^/todo/lists/([^/]+)/tasks/delta$", path)):
            return self._tracked(request, [x for x in self.tasks.values() if x["listId"] == g.group(1)], params)
        if (g := re.match(r"^/todo/lists/([^/]+)/tasks/([^/]+)$", path)):
            x = self.tasks.get(g.group(2))
            return httpx.Response(200, json=x) if x else self._err(404, "ErrorItemNotFound")
        return self._err(400, "BadRequest", f"simulated server: no route for {method} {path}")

    def _mf(self, ref: str) -> str | None:
        if ref in self.mail_folders:
            return self.mail_folders[ref]["id"]
        return ref if any(f["id"] == ref for f in self.mail_folders.values()) else None

    def _create_mail_folder(self, body: dict, parent: str | None) -> httpx.Response:
        name = body["displayName"]
        for f in self.mail_folders.values():
            if f["parent"] == parent and f["displayName"].lower() == name.lower():
                return self._err(409, "ErrorFolderExists")
        fid = self.new_id("mf-")
        self.mail_folders[fid] = {"id": fid, "displayName": name, "parent": parent}
        return httpx.Response(201, json=self.mail_folders[fid])

    def _create_mime(self, fid: str | None, content: bytes, draft: bool) -> httpx.Response:
        if fid is None:
            return self._err(404, "ErrorItemNotFound")
        try:
            mime = base64.b64decode(content, validate=True)
        except ValueError:
            return self._err(400, "ErrorMimeContentInvalidBase64String", "Invalid base64 string for MIME content.")
        if len(content) > getattr(self, "max_body", 1 << 30):
            return self._err(413, "RequestEntityTooLarge")
        rid = self.new_id("msg")
        now = "2026-11-06T10:00:00Z"
        self.messages[rid] = {"id": rid, "parentFolderId": fid, "mime": mime, "isDraft": draft, "isRead": True,
                              "receivedDateTime": now, "flag": {"flagStatus": "notFlagged"}}
        return httpx.Response(201, json={k: v for k, v in self.messages[rid].items() if k != "mime"})

    def add_message(self, fid: str, mime: bytes, **fields) -> str:
        rid = fields.pop("id", None) or self.new_id("msg")
        self.messages[rid] = {"id": rid, "parentFolderId": fid, "mime": mime, "isRead": False, "isDraft": False,
                              "flag": {"flagStatus": "notFlagged"}, **fields}
        return rid

    def _delta(self, request, params, ref) -> httpx.Response:
        if "$deltatoken" in params and self.delta_expired:
            self.delta_expired = False
            return self._err(410, "SyncStateNotFound")
        fid = self._mf(ref)
        rows = [{k: v for k, v in x.items() if k != "mime"} for x in self.messages.values() if x["parentFolderId"] == fid]
        if "$deltatoken" in params:
            rows = rows[int(params["$deltatoken"]):]
        return self._simple_delta(request, rows, params, base=int(params.get("$deltatoken") or 0))

    def _tracked(self, request, rows, params) -> httpx.Response:
        base = int(params.get("$deltatoken") or 0)
        return self._simple_delta(request, rows[base:], params, base=base)

    def _simple_delta(self, request, rows, params, base=0) -> httpx.Response:
        if "$expand" in params:  # Exchange Online, whatever the documentation of message delta says
            return self._err(400, "BadRequest", "Parsing OData Select and Expand failed: Value cannot be null. "
                                                "(Parameter 'initialState')")
        if "$filter" in params or "$orderby" in params and params["$orderby"] != "receivedDateTime desc":
            return self._err(400, "BadRequest", "simulated server: unsupported query on a delta")
        self.delta_calls.append(dict(params))
        rows = [{k: v for k, v in r.items() if k != "singleValueExtendedProperties"} for r in rows]
        skip = int(params.get("$skiptoken") or 0)
        page = rows[skip:skip + self.page_limit]
        body: dict = {"value": page}
        clean = {k: v for k, v in params.items() if k not in ("$skiptoken", "$deltatoken")}
        if skip + self.page_limit < len(rows):
            body["@odata.nextLink"] = str(request.url.copy_with(params={**clean, "$skiptoken": str(skip + self.page_limit),
                                                                        **({"$deltatoken": str(base)} if base else {})}))
        else:
            body["@odata.deltaLink"] = str(request.url.copy_with(params={**clean, "$deltatoken": str(base + len(rows))}))
        return httpx.Response(200, json=body)

    def _create_event(self, cid: str, body: dict) -> httpx.Response:
        for e in self.events.values():
            if body.get("transactionId") and e.get("transactionId") == body["transactionId"]:
                return httpx.Response(201, json={"id": e["id"]})
        eid = self.new_id("ev")
        self.events[eid] = dict(body, id=eid, calendar=cid, type="seriesMaster" if body.get("recurrence") else "singleInstance")
        if body.get("recurrence"):
            self.instances[eid] = self._expand(eid, body)
        return httpx.Response(201, json={"id": eid})

    def _expand(self, eid: str, body: dict) -> list[dict]:
        pattern = body["recurrence"]["pattern"]
        step = timedelta(days=7) if pattern["type"] == "weekly" else timedelta(days=1)
        count = body["recurrence"]["range"].get("numberOfOccurrences") or 10
        from zoneinfo import ZoneInfo
        from app.engine.tzwin import iana_for
        tz = body["start"]["timeZone"]
        z = timezone.utc if tz == "UTC" else ZoneInfo(iana_for(tz))
        start = datetime.fromisoformat(body["start"]["dateTime"]).replace(tzinfo=z)
        out = []
        for i in range(count):
            when = (start.replace(tzinfo=None) + i * step).replace(tzinfo=z).astimezone(timezone.utc)
            out.append({"id": f"{eid}-occ{i}", "seriesMasterId": eid, "type": "occurrence", "originalStart": utc_text(when),
                        "start": {"dateTime": when.strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"}})
        return out

    def _event_item(self, method, eid, body, params) -> httpx.Response:
        if "-occ" in eid:
            master = eid.split("-occ")[0]
            rows = self.instances.get(master) or []
            if method == "DELETE":
                self.instances[master] = [x for x in rows if x["id"] != eid]
                return httpx.Response(204)
            if method == "PATCH":
                for x in rows:
                    if x["id"] == eid:
                        x.update(body, type="exception")
                return httpx.Response(200, json={"id": eid})
        e = self.events.get(eid)
        if e is None:
            return self._err(404, "ErrorItemNotFound")
        if method == "DELETE":
            del self.events[eid]
            self.instances.pop(eid, None)
            return httpx.Response(204)
        return httpx.Response(200, json=e)


# ------------------------------------------------------------------ Entra (provisioning of the tenant app)
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


# the delegated scopes each endpoint needs (any one of them), as Microsoft Graph enforces them on the `scp` claim
ADMIN_SCOPES = {
    ("GET", "organization"): ("User.Read", "Organization.Read.All", "Directory.Read.All"),
    ("GET", "servicePrincipals"): ("Application.Read.All", "Application.ReadWrite.All"),
    ("POST", "applications"): ("Application.ReadWrite.All",),
    ("POST", "servicePrincipals"): ("Application.ReadWrite.All",),
    ("POST", "addPassword"): ("Application.ReadWrite.All",),
    ("DELETE", "applications"): ("Application.ReadWrite.All",),
    ("POST", "appRoleAssignedTo"): ("AppRoleAssignment.ReadWrite.All",),
    ("POST", "appRoleAssignments"): ("AppRoleAssignment.ReadWrite.All",),
}


def _jwt(claims: dict, signature: str) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc(claims)}.{signature}"


class FakeEntra(FakeGraph):
    """FakeGraph plus what "Connect with Microsoft" uses: the device code flow of the bootstrap app (delegated token:
    a JWT whose `scp` holds the scopes asked at the devicecode endpoint, enforced per endpoint), the application
    objects of the tenant, and a propagation delay of the new credentials."""

    def __init__(self, mailbox: str = "me@contoso.test"):
        super().__init__(mailbox)
        self.tenant_id = "tid-contoso"
        self.device_code = "dev-code-1"
        self.pending = 2               # authorization_pending answers before the sign-in completes
        self.device_error: tuple[str, str] | None = None   # (error, error_description) instead of the token
        self.device_requests: list[dict] = []
        self.scopes: list[str] = []    # the delegated scopes asked at the devicecode endpoint, resource prefix removed
        self.delegated: list[str] = []  # the delegated tokens issued
        self.upn = "admin@contoso.test"
        self.org_forbidden = False     # True: GET /organization is 403 whatever the scopes
        self.refused: list[tuple[str, str, int]] = []   # (method, path, status) of the delegated calls refused
        self.admin = True              # False: POST /applications is 403 (not an administrator)
        self.consent_forbidden = False  # True: the app role assignments are 403
        self.propagation = 2           # client-credential tokens of a new app refused before it works
        self.applications: dict[str, dict] = {}
        self.passwords: dict[str, str] = {}   # appId -> secret
        self.assignments: list[dict] = []
        self.deleted: list[str] = []
        # the Graph service principal of the tenant: role ids are per role, not to be hardcoded
        self.graph_sp = {"id": "sp-graph", "appId": GRAPH_APP_ID, "appRoles": [
            {"id": f"role-{i}-{v.lower()}", "value": v, "allowedMemberTypes": ["Application"]}
            for i, v in enumerate(["Mail.ReadWrite", "User.Read.All", "Mail.Read", "Calendars.Read", "Contacts.Read",
                                   "MailboxSettings.Read", "Tasks.Read.All", "Directory.Read.All"])]}
        self.service_principals: dict[str, dict] = {"sp-graph": self.graph_sp}

    def role_value(self, role_id: str) -> str:
        return next(r["value"] for r in self.graph_sp["appRoles"] if r["id"] == role_id)

    @property
    def DELEGATED(self) -> str:  # noqa: N802 - the last delegated token issued
        return self.delegated[-1] if self.delegated else "delegated-token-none"

    def _delegated_token(self) -> str:
        tok = _jwt({"tid": self.tenant_id, "upn": self.upn, "scp": " ".join(self.scopes)},
                   f"delegated-token-XYZ{len(self.delegated)}")
        self.delegated.append(tok)
        return tok

    def _scp(self, request: httpx.Request) -> set[str]:
        claims = json.loads(base64.urlsafe_b64decode(
            request.headers["authorization"].split()[1].split(".")[1] + "=="))
        return set(claims.get("scp", "").split())

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host.startswith("login."):
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            if url.path.endswith("/devicecode"):
                self.device_requests.append(form)
                self.scopes = [x.rsplit("/", 1)[-1] for x in form.get("scope", "").split()]
                return httpx.Response(200, json={
                    "device_code": self.device_code, "user_code": "ABCD-EFGH",
                    "verification_uri": "https://microsoft.com/devicelogin", "expires_in": 900, "interval": 5,
                    "message": "To sign in, use a web browser to open https://microsoft.com/devicelogin and enter "
                               "the code ABCD-EFGH to authenticate."})
            if form.get("grant_type") == DEVICE_GRANT:
                self.device_requests.append(form)
                if form.get("device_code") != self.device_code:
                    return httpx.Response(400, json={"error": "bad_verification_code"})
                if self.pending > 0:
                    self.pending -= 1
                    return httpx.Response(400, json={"error": "authorization_pending",
                                                     "error_description": "AADSTS70016: pending"})
                if self.device_error:
                    return httpx.Response(400, json={"error": self.device_error[0],
                                                     "error_description": self.device_error[1]})
                return httpx.Response(200, json={"access_token": self._delegated_token(), "token_type": "Bearer",
                                                 "expires_in": 3599})
            app = next((a for a in self.applications.values() if a["appId"] == form.get("client_id")), None)
            if app is not None:
                self.token_requests.append(form)
                if self.propagation > 0 or form.get("client_secret") != self.passwords.get(app["appId"]):
                    self.propagation = max(0, self.propagation - 1)
                    return httpx.Response(401, json={"error": "invalid_client",
                                                     "error_description": "AADSTS7000215: Invalid client secret."})
                sp = next(s for s in self.service_principals.values() if s.get("appId") == app["appId"])
                self.roles = [self.role_value(a["appRoleId"]) for a in self.assignments if a["principalId"] == sp["id"]]
                self.tokens += 1
                self.last_token = self._access_token()
                return httpx.Response(200, json={"access_token": self.last_token, "expires_in": 3599})
            return super().__call__(request)
        if request.headers.get("authorization", "").removeprefix("Bearer ") in self.delegated:
            self.requests.append(request)
            path = urllib.parse.unquote(url.path)
            r = self._scope_check(request, path) or self._admin(request, path, dict(url.params.multi_items()))
            if r.status_code in (401, 403):
                self.refused.append((request.method, path, r.status_code))
            return r
        return super().__call__(request)

    def _scope_check(self, request: httpx.Request, path: str) -> httpx.Response | None:
        parts = path.strip("/").split("/")
        key = (request.method, parts[-1] if parts[-1] in ("addPassword", "appRoleAssignedTo", "appRoleAssignments") else parts[1])
        needed = ADMIN_SCOPES.get(key)
        assert needed, f"delegated scope of {key} not simulated"
        if not self._scp(request) & set(needed):
            return self._err(403, "Authorization_RequestDenied", "Insufficient privileges to complete the operation.")
        if key == ("GET", "organization") and self.org_forbidden:
            return self._err(403, "Authorization_RequestDenied", "Insufficient privileges to complete the operation.")
        return None

    def _admin(self, request: httpx.Request, path: str, params: dict) -> httpx.Response:
        method = request.method
        body = json.loads(request.content) if request.content else {}
        if path == "/v1.0/organization" and method == "GET":
            return httpx.Response(200, json={"value": [{"id": self.tenant_id, "displayName": self.org_name}]})
        if path == "/v1.0/servicePrincipals" and method == "GET":
            got = re.match(r"^appId eq '([^']+)'$", params.get("$filter", ""))
            assert got, f"filter not supported by the simulated server: {params.get('$filter')}"
            return httpx.Response(200, json={"value": [s for s in self.service_principals.values()
                                                       if s.get("appId") == got.group(1)]})
        if path == "/v1.0/applications" and method == "POST":
            if not self.admin:
                return self._err(403, "Authorization_RequestDenied", "Insufficient privileges to complete the operation.")
            oid = self.new_id("app-obj-")
            self.applications[oid] = {**body, "id": oid, "appId": self.new_id("app-id-")}
            return httpx.Response(201, json=self.applications[oid])
        if path == "/v1.0/servicePrincipals" and method == "POST":
            if not any(a["appId"] == body.get("appId") for a in self.applications.values()):
                return self._err(400, "Request_BadRequest", "unknown appId")
            sid = self.new_id("sp-")
            self.service_principals[sid] = {"id": sid, "appId": body["appId"]}
            return httpx.Response(201, json=self.service_principals[sid])
        if (g := re.match(r"^/v1\.0/servicePrincipals/([^/]+)/(appRoleAssignedTo|appRoleAssignments)$", path)) \
                and method == "POST":
            if self.consent_forbidden:
                return self._err(403, "Authorization_RequestDenied", "Insufficient privileges to complete the operation.")
            assert body["resourceId"] == "sp-graph" and body["principalId"] in self.service_principals
            assert any(r["id"] == body["appRoleId"] for r in self.graph_sp["appRoles"])
            self.assignments.append(dict(body))
            return httpx.Response(201, json={**body, "id": self.new_id("asg-")})
        if (g := re.match(r"^/v1\.0/applications/([^/]+)/addPassword$", path)) and method == "POST":
            app = self.applications.get(g.group(1))
            if app is None:
                return self._err(404, "Request_ResourceNotFound")
            secret = f"app-secret-{self.new_id('')}-S3CR3T"
            self.passwords[app["appId"]] = secret
            return httpx.Response(200, json={"keyId": "k1", "secretText": secret,
                                             "displayName": body["passwordCredential"]["displayName"],
                                             "endDateTime": body["passwordCredential"].get("endDateTime")})
        if (g := re.match(r"^/v1\.0/applications/([^/]+)$", path)) and method == "DELETE":
            app = self.applications.pop(g.group(1), None)
            if app is None:
                return self._err(404, "Request_ResourceNotFound")
            self.deleted.append(g.group(1))
            for sid in [s for s, v in self.service_principals.items() if v.get("appId") == app["appId"]]:
                del self.service_principals[sid]
            return httpx.Response(204)
        return self._err(404, "Request_ResourceNotFound", f"not simulated: {method} {path}")
