"""Minimal CalDAV / CardDAV client (sync, httpx + xml.etree)."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urljoin

import httpx

NS_DAV = "DAV:"
NS_CAL = "urn:ietf:params:xml:ns:caldav"
NS_CARD = "urn:ietf:params:xml:ns:carddav"

IMPORT_NAME = "import-pst"
IMPORT_DISPLAY = "PST Import"


def _q(ns: str, name: str) -> str:
    return f"{{{ns}}}{name}"


class DavError(Exception):
    def __init__(self, msg: str, temporary: bool = False, status: int | None = None, retry_after: float | None = None):
        super().__init__(msg)
        self.temporary = temporary
        self.status = status
        self.retry_after = retry_after  # seconds asked by the server (Retry-After on 429/503)


def expand_template(template: str, email: str, login: str) -> str:
    user, _, domain = email.partition("@")
    return (
        template.replace("{email}", email)
        .replace("{user}", user)
        .replace("{domain}", domain)
        .replace("{login}", login)
    )


def _retry_after(resp: httpx.Response) -> float | None:
    """Retry-After in seconds. The HTTP date form is ignored: the caller then uses its own backoff."""
    v = (resp.headers.get("retry-after") or "").strip()
    return float(v) if v.isdigit() else None


def _slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


class DavClient:
    def __init__(self, username: str, password: str, verify_tls: bool = True,
                 timeout: float = 60.0, transport: httpx.BaseTransport | None = None):
        self.username = username
        self._auth = httpx.BasicAuth(username, password)
        # Redirects handled by hand (see _send): httpx would turn
        # PROPFIND into GET on 301/302 and drop Authorization.
        self._http = httpx.Client(
            verify=verify_tls,
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "DavClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- low level -------------------------------------------------
    MAX_REDIRECTS = 5

    def _send(self, method: str, url: str, **kw) -> httpx.Response:
        """Request with manual redirects: method, body and headers
        preserved (301/302/303/307/308 all treated the same, which is what
        WebDAV expects). Authentication rule: credentials are only sent again
        if the target has the same host as the initial request and does not downgrade
        https to http; otherwise the request follows without Authorization (never
        leaking the password to another host)."""
        origin = httpx.URL(url)
        cur = origin
        for _ in range(self.MAX_REDIRECTS + 1):
            same = (cur.host == origin.host
                    and not (origin.scheme == "https" and cur.scheme != "https"))
            resp = self._http.request(method, cur, auth=self._auth if same else None, **kw)
            loc = resp.headers.get("location")
            if resp.status_code in (301, 302, 303, 307, 308) and loc:
                cur = cur.join(loc)
                continue
            return resp
        raise DavError(f"{method} {url}: too many redirects", temporary=False)

    def _request(self, method: str, url: str, ok: tuple[int, ...] | None = None,
                 **kw) -> httpx.Response:
        try:
            resp = self._send(method, url, **kw)
        except httpx.HTTPError as e:
            raise DavError(f"{method} {url}: {e.__class__.__name__}: {e}",
                           temporary=True) from e
        if ok is not None:
            good = resp.status_code in ok
        else:
            good = 200 <= resp.status_code < 300
        if not good:
            sc = resp.status_code
            raise DavError(f"{method} {url} -> HTTP {sc}",
                           temporary=(sc >= 500 or sc == 429), status=sc, retry_after=_retry_after(resp))
        return resp

    def _propfind(self, url: str, props: list[tuple[str, str]], depth: int
                  ) -> list[tuple[str, dict[str, ET.Element]]]:
        body = ET.Element(_q(NS_DAV, "propfind"))
        prop = ET.SubElement(body, _q(NS_DAV, "prop"))
        for ns, name in props:
            ET.SubElement(prop, _q(ns, name))
        data = ET.tostring(body, encoding="utf-8", xml_declaration=True)
        resp = self._request(
            "PROPFIND", url, ok=(207,), content=data,
            headers={"Depth": str(depth),
                     "Content-Type": "application/xml; charset=utf-8"})
        base = str(resp.url)
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            raise DavError(f"PROPFIND {url}: invalid XML ({e})") from e
        out = []
        for r in root.findall(_q(NS_DAV, "response")):
            href_el = r.find(_q(NS_DAV, "href"))
            if href_el is None or not (href_el.text or "").strip():
                continue
            href = urljoin(base, href_el.text.strip())
            found: dict[str, ET.Element] = {}
            for ps in r.findall(_q(NS_DAV, "propstat")):
                st = ps.findtext(_q(NS_DAV, "status")) or ""
                if " 200" not in st:
                    continue
                p = ps.find(_q(NS_DAV, "prop"))
                if p is not None:
                    for child in p:
                        found[child.tag] = child
            out.append((href, found))
        return out

    @staticmethod
    def _href_of(el: ET.Element | None) -> str | None:
        if el is None:
            return None
        h = el.find(_q(NS_DAV, "href"))
        if h is not None and (h.text or "").strip():
            return h.text.strip()
        return None

    @staticmethod
    def _is_kind(props: dict[str, ET.Element], kind: str) -> bool:
        rt = props.get(_q(NS_DAV, "resourcetype"))
        if rt is None:
            return False
        tag = _q(NS_CAL, "calendar") if kind == "caldav" else _q(NS_CARD, "addressbook")
        return rt.find(tag) is not None

    @staticmethod
    def _accepts(props: dict[str, ET.Element], component: str = "VEVENT") -> bool:
        comp = props.get(_q(NS_CAL, "supported-calendar-component-set"))
        if comp is None or len(comp) == 0:
            return True
        return any(c.get("name", "").upper() == component
                   for c in comp.findall(_q(NS_CAL, "comp")))

    # ---- discovery -------------------------------------------------
    def _resolve(self, kind: str, start: str, create: bool, component: str = "VEVENT"
                 ) -> tuple[str | None, str | None]:
        """Returns (collection, home). collection None if absent."""
        if kind not in ("caldav", "carddav"):
            raise DavError(f"unknown type: {kind}")
        ns = NS_CAL if kind == "caldav" else NS_CARD
        home_prop = "calendar-home-set" if kind == "caldav" else "addressbook-home-set"
        coll_tag = "calendar" if kind == "caldav" else "addressbook"

        try:
            entries = self._propfind(start, [
                (NS_DAV, "resourcetype"), (NS_DAV, "current-user-principal"),
                (ns, home_prop)], 0)
        except DavError as e:
            if e.status != 404:
                raise
            # target URL (server template) does not exist: the collection is created at this address
            if not create:
                return None, _slash(start)
            created = self._mkcol(kind, _slash(start), component)
            # canonical URL (href returned by the server, e.g. encoded "@"): identical to subsequent discoveries
            return self._resolve(kind, created, False, component)[0] or created, None
        if not entries:
            raise DavError(f"PROPFIND {start}: empty response")
        here, props = entries[0]
        if self._is_kind(props, kind):
            return _slash(here), None

        home_href = self._href_of(props.get(_q(ns, home_prop)))
        if home_href:
            home = urljoin(here, home_href)
        else:
            principal_href = self._href_of(props.get(_q(NS_DAV, "current-user-principal")))
            if principal_href:
                principal = urljoin(here, principal_href)
                pe = self._propfind(principal, [(ns, home_prop)], 0)
                h = self._href_of(pe[0][1].get(_q(ns, home_prop))) if pe else None
                home = urljoin(principal, h) if h else principal
            else:
                home = here
        home = _slash(home)

        for href, p in self._propfind(home, [
                (NS_DAV, "resourcetype"), (NS_DAV, "displayname"),
                (NS_CAL, "supported-calendar-component-set")], 1):
            if _slash(href) == home or not self._is_kind(p, kind):
                continue
            if kind == "caldav" and not self._accepts(p, component):
                continue
            return _slash(href), home
        if not create:
            return None, home
        return self._create_collection(kind, home, component), home

    def _create_collection(self, kind: str, home: str, component: str = "VEVENT") -> str:
        # distinct slugs: CalDAV and CardDAV often share the same home (Radicale...), and a calendar may refuse tasks
        slug = "-contacts/" if kind != "caldav" else "-tasks/" if component == "VTODO" else "-agenda/"
        return self._mkcol(kind, home + IMPORT_NAME + slug, component)

    def _mkcol(self, kind: str, url: str, component: str = "VEVENT", display: str | None = None) -> str:
        """Creates a collection at `url` ("PST Import" unless `display` names it): MKCALENDAR (CalDAV) or extended
        MKCOL (CardDAV, RFC 5689). Server refusal -> explicit DavError (non-temporary except 5xx/429)."""
        display = display or IMPORT_DISPLAY
        if kind == "caldav":
            root = ET.Element(_q(NS_CAL, "mkcalendar"))
            setp = ET.SubElement(ET.SubElement(root, _q(NS_DAV, "set")), _q(NS_DAV, "prop"))
            ET.SubElement(setp, _q(NS_DAV, "displayname")).text = display
            comps = ET.SubElement(setp, _q(NS_CAL, "supported-calendar-component-set"))
            ET.SubElement(comps, _q(NS_CAL, "comp")).set("name", component)
            method = "MKCALENDAR"
        else:
            root = ET.Element(_q(NS_DAV, "mkcol"))
            setp = ET.SubElement(ET.SubElement(root, _q(NS_DAV, "set")), _q(NS_DAV, "prop"))
            rt = ET.SubElement(setp, _q(NS_DAV, "resourcetype"))
            ET.SubElement(rt, _q(NS_DAV, "collection"))
            ET.SubElement(rt, _q(NS_CARD, "addressbook"))
            ET.SubElement(setp, _q(NS_DAV, "displayname")).text = display
            method = "MKCOL"
        data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        try:
            self._request(method, url, ok=(200, 201, 204), content=data,
                          headers={"Content-Type": "application/xml; charset=utf-8"})
        except DavError as e:
            if e.status == 405 and self._is_collection(kind, url):
                return url  # already exists (created in the meantime)
            what = "the calendar" if kind == "caldav" else "the address book"
            raise DavError(f"creation of {what} '{display}' refused by the server "
                           f"({method} {url} -> {f'HTTP {e.status}' if e.status else e}): create the collection "
                           f"manually or set its URL in the server configuration",
                           temporary=e.temporary, status=e.status) from e
        return url

    def _is_collection(self, kind: str, url: str) -> bool:
        try:
            entries = self._propfind(url, [(NS_DAV, "resourcetype")], 0)
        except DavError:
            return False
        return bool(entries) and self._is_kind(entries[0][1], kind)

    def _probe(self, kind: str, url: str) -> int | None:
        """None: `url` is a collection of this kind; else the HTTP status of the PROPFIND (0: no answer, 207: an
        answer that is not such a collection)."""
        try:
            entries = self._propfind(url, [(NS_DAV, "resourcetype")], 0)
        except DavError as e:
            return e.status or 0
        return None if entries and self._is_kind(entries[0][1], kind) else 207

    def _listed_name(self, kind: str, url: str) -> str | None:
        """Display name of `url` when its parent lists it as a collection of this kind (None: not listed)."""
        target = unquote(_slash(url))
        parent = target.rstrip("/").rsplit("/", 1)[0] + "/"
        try:
            rows = self._propfind(parent, [(NS_DAV, "resourcetype"), (NS_DAV, "displayname")], 1)
        except DavError:
            return None
        for href, props in rows:
            if unquote(_slash(href)) == target and self._is_kind(props, kind):
                return (props.get(_q(NS_DAV, "displayname")).text or "").strip() if props.get(
                    _q(NS_DAV, "displayname")) is not None else ""
        return None

    def ensure_collection(self, kind: str, url: str, component: str = "VEVENT", display: str | None = None) -> str:
        """The collection `url` exists (otherwise it is created, named `display`). A collection the server lists but
        will not open (MDaemon: "Invalid Path") is never created again over itself: an explicit error instead."""
        status = self._probe(kind, url)
        if status is None:
            return url
        if status == 0 or status == 429 or status >= 500:
            raise DavError(f"PROPFIND {url} -> {f'HTTP {status}' if status else 'no answer'}", temporary=True,
                           status=status or None)
        listed = self._listed_name(kind, url)
        if listed is not None:
            what = "calendar" if kind == "caldav" else "address book"
            raise DavError(f"the server lists the {what} '{listed or url}' ({url}) but refuses to open it "
                           f"(PROPFIND -> HTTP {status}): check this {what} on the server (it may be damaged), or set "
                           f"the {kind.upper()} URL of a working collection in the server configuration",
                           status=status if status >= 400 else 409)
        return self._mkcol(kind, _slash(url), component, display)

    def _starts(self, kind: str, template_url: str | None, email: str,
                imap_host: str) -> list[str]:
        if template_url:
            return [expand_template(template_url, email, self.username)]
        domain = email.partition("@")[2]
        starts = [f"https://{imap_host}/.well-known/{kind}"]
        if domain and domain != imap_host:
            starts.append(f"https://{domain}/.well-known/{kind}")
        return starts

    def _discover(self, kind: str, template_url: str | None, email: str,
                  imap_host: str, create: bool, component: str = "VEVENT") -> tuple[str | None, str | None]:
        last: DavError | None = None
        for start in self._starts(kind, template_url, email, imap_host):
            try:
                return self._resolve(kind, start, create, component)
            except DavError as e:
                last = e
        assert last is not None
        raise last

    def discover(self, kind: str, template_url: str | None, email: str,
                 imap_host: str, create: bool = True, component: str = "VEVENT") -> str:
        coll, _ = self._discover(kind, template_url, email, imap_host, create, component)
        if coll is None:
            raise DavError(f"no {kind} collection found")
        return coll

    def test(self, kind: str, template_url: str | None, email: str,
             imap_host: str) -> tuple[bool, str]:
        try:
            coll, home = self._discover(kind, template_url, email, imap_host, False)
        except DavError as e:
            return False, f"{kind} connection failed: {e}"
        except Exception as e:  # never raises
            return False, f"Unexpected {kind} error: {e}"
        if coll:
            return True, f"{kind} connection successful: {coll}"
        return True, (f"{kind} connection successful; no collection found in "
                      f"{home}, the '{IMPORT_DISPLAY}' collection will be created.")

    def collections(self, kind: str, template_url: str | None, email: str,
                    imap_host: str) -> list[tuple[str, str, list[str]]]:
        """Every collection of this kind, to read them: [(url, display name, accepted components)]. Empty components:
        the server does not say."""
        return self.collections_at(kind, *self._discover(kind, template_url, email, imap_host, False))

    def collections_at(self, kind: str, coll: str | None, home: str | None) -> list[tuple[str, str, list[str]]]:
        """As `collections`, from a discovery already made: the collections of `home`, else `coll` alone."""
        if home is None:
            starts = [coll] if coll else []
            rows = [e for s in starts for e in self._propfind(s, [
                (NS_DAV, "displayname"), (NS_DAV, "resourcetype"),
                (NS_CAL, "supported-calendar-component-set")], 0)]
        else:
            rows = self._propfind(home, [(NS_DAV, "resourcetype"), (NS_DAV, "displayname"),
                                         (NS_CAL, "supported-calendar-component-set")], 1)
        out = []
        for href, p in rows:
            if home is not None and _slash(href) == home:
                continue
            if not self._is_kind(p, kind):
                continue
            name = p.get(_q(NS_DAV, "displayname"))
            label = (name.text or "").strip() if name is not None else ""
            comp = p.get(_q(NS_CAL, "supported-calendar-component-set"))
            comps = [c.get("name", "").upper() for c in comp.findall(_q(NS_CAL, "comp"))] if comp is not None else []
            url = _slash(href)
            out.append((url, label or url.rstrip("/").rsplit("/", 1)[-1], comps))
        return out

    # ---- objects -----------------------------------------------------
    @staticmethod
    def _item_url(collection_url: str, filename: str) -> str:
        return _slash(collection_url) + quote(filename, safe="")

    def put(self, collection_url: str, filename: str, content: str, kind: str) -> int:
        ctype = "text/calendar" if kind == "caldav" else "text/vcard"
        resp = self._request(
            "PUT", self._item_url(collection_url, filename),
            content=content.encode("utf-8"),
            headers={"Content-Type": f"{ctype}; charset=utf-8"})
        return resp.status_code

    def exists(self, collection_url: str, filename: str) -> bool:
        return self.get(collection_url, filename) is not None

    def get(self, collection_url: str, filename: str) -> str | None:
        try:
            # with credentials (like all other requests): otherwise 401 on an authenticated server
            resp = self._send("GET", self._item_url(collection_url, filename))
        except httpx.HTTPError as e:
            raise DavError(f"GET: {e.__class__.__name__}: {e}", temporary=True) from e
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            sc = resp.status_code
            raise DavError(f"GET -> HTTP {sc}", temporary=(sc >= 500 or sc == 429),
                           status=sc)
        return resp.text

    def count(self, collection_url: str) -> int:
        url = _slash(collection_url)
        n = 0
        for href, p in self._propfind(url, [(NS_DAV, "resourcetype")], 1):
            if _slash(href) == url:
                continue
            rt = p.get(_q(NS_DAV, "resourcetype"))
            if rt is not None and rt.find(_q(NS_DAV, "collection")) is not None:
                continue
            n += 1
        return n

    def names(self, collection_url: str) -> set[str]:
        """Names (last decoded segment) of the non-collection resources of the collection."""
        from urllib.parse import unquote, urlparse
        url = _slash(collection_url)
        out: set[str] = set()
        for href, p in self._propfind(url, [(NS_DAV, "resourcetype")], 1):
            if _slash(href) == url:
                continue
            rt = p.get(_q(NS_DAV, "resourcetype"))
            if rt is not None and rt.find(_q(NS_DAV, "collection")) is not None:
                continue
            path = urlparse(href).path or href
            seg = path.rstrip("/").rsplit("/", 1)[-1]
            if seg:
                out.add(unquote(seg))
        return out

    # ---- duplicate search (dedup_runs.py) -------------------------------
    def etags(self, collection_url: str) -> dict[str, str]:
        """{absolute href: etag ('' when the server gives none)} of the non-collection resources."""
        url = _slash(collection_url)
        out: dict[str, str] = {}
        for href, p in self._propfind(url, [(NS_DAV, "resourcetype"), (NS_DAV, "getetag")], 1):
            if _slash(href) == url:
                continue
            rt = p.get(_q(NS_DAV, "resourcetype"))
            if rt is not None and rt.find(_q(NS_DAV, "collection")) is not None:
                continue
            et = p.get(_q(NS_DAV, "getetag"))
            out[href] = (et.text or "").strip() if et is not None else ""
        return out

    def multiget(self, kind: str, collection_url: str, hrefs: list[str]) -> dict[str, tuple[str, str]]:
        """{absolute href: (etag, data)} of these resources (calendar-multiget / addressbook-multiget, RFC 4791 and
        6352). A resource gone since its listing is simply missing from the result."""
        if not hrefs:
            return {}
        ns, report, data_name = ((NS_CAL, "calendar-multiget", "calendar-data") if kind == "caldav"
                                 else (NS_CARD, "addressbook-multiget", "address-data"))
        body = ET.Element(_q(ns, report))
        prop = ET.SubElement(body, _q(NS_DAV, "prop"))
        ET.SubElement(prop, _q(NS_DAV, "getetag"))
        ET.SubElement(prop, _q(ns, data_name))
        for h in hrefs:
            ET.SubElement(body, _q(NS_DAV, "href")).text = httpx.URL(h).raw_path.decode("ascii")
        resp = self._request("REPORT", _slash(collection_url), ok=(207,),
                             content=ET.tostring(body, encoding="utf-8", xml_declaration=True),
                             headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            raise DavError(f"REPORT {collection_url}: invalid XML ({e})") from e
        base = str(resp.url)
        out: dict[str, tuple[str, str]] = {}
        for r in root.findall(_q(NS_DAV, "response")):
            href = (r.findtext(_q(NS_DAV, "href")) or "").strip()
            if not href:
                continue
            for ps in r.findall(_q(NS_DAV, "propstat")):
                if " 200" not in (ps.findtext(_q(NS_DAV, "status")) or ""):
                    continue
                p = ps.find(_q(NS_DAV, "prop"))
                if p is None:
                    continue
                d = p.find(_q(ns, data_name))
                if d is None or not (d.text or "").strip():
                    continue
                out[urljoin(base, href)] = ((p.findtext(_q(NS_DAV, "getetag")) or "").strip(), d.text)
        return out

    def delete_if_match(self, href: str, etag: str) -> int:
        """DELETE only when the resource still has this etag. The HTTP status: 2xx deleted, 412 changed since,
        404 gone already; any other error raises."""
        try:
            resp = self._send("DELETE", href, headers={"If-Match": etag})
        except httpx.HTTPError as e:
            raise DavError(f"DELETE {href}: {e.__class__.__name__}: {e}", temporary=True) from e
        sc = resp.status_code
        if 200 <= sc < 300 or sc in (404, 412):
            return sc
        raise DavError(f"DELETE {href} -> HTTP {sc}", temporary=(sc >= 500 or sc == 429), status=sc,
                       retry_after=_retry_after(resp))
