"""Minimal CalDAV / CardDAV client (sync, httpx + xml.etree)."""
from __future__ import annotations

import codecs
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urljoin

import httpx

NS_DAV = "DAV:"
NS_CAL = "urn:ietf:params:xml:ns:caldav"
NS_CARD = "urn:ietf:params:xml:ns:carddav"
NS_CS = "http://calendarserver.org/ns/"

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


class DavIncomplete(DavError):
    """A multistatus that says itself it is not whole: a <response> with an error status of its own, such as 507
    (RFC 4918 §9.1: a server cutting a Depth 1 PROPFIND short). `partial`: what it did list."""

    def __init__(self, msg: str, partial=None):
        super().__init__(msg, temporary=False, status=207)
        self.partial = partial


_STATUS = re.compile(r"\s([1-5]\d\d)(\s|$)")


def _status_code(text: str) -> int | None:
    m = _STATUS.search(" " + text)
    return int(m.group(1)) if m else None


def expand_template(template: str, email: str, login: str) -> str:
    user, _, domain = email.partition("@")
    return (
        template.replace("{email}", email)
        .replace("{user}", user)
        .replace("{domain}", domain)
        .replace("{login}", login)
    )


_CHARSET_PARAM = re.compile(rb";\s*CHARSET=\"?([A-Za-z0-9_.:-]+)\"?", re.I)
_QP_PARAM = re.compile(rb";\s*(?:ENCODING=)?QUOTED-PRINTABLE\b", re.I)


def _codec(name: bytes) -> str | None:
    try:
        return codecs.lookup(name.decode("ascii")).name
    except (LookupError, UnicodeDecodeError):
        return None


def _cp1252(raw: bytes) -> str:
    """cp1252, its 5 undefined bytes kept as latin-1 (nothing is lost)."""
    return "".join(chr(b) if b in (0x81, 0x8D, 0x8F, 0x90, 0x9D) else bytes((b,)).decode("cp1252") for b in raw)


def decode_object(raw: bytes) -> str:
    """The text of a vCard or an iCalendar object from its bytes, whatever the Content-Type of the answer says (a
    charset there is often wrong or missing, and a default such as ISO-8859-1 turns "é" into "Ã©"). UTF-8 when it is
    valid UTF-8 (RFC 6350 / 5545: the only charset of these formats). Otherwise each line that is not, in its own
    CHARSET parameter (vCard 2.1; the parameter is then dropped: the text goes on in UTF-8), else in cp1252, the
    charset older Windows clients wrote these objects in. A quoted-printable value is ASCII: left as it is, with its
    CHARSET (it says what its bytes are)."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    out = []
    for line in raw.splitlines(keepends=True):
        try:
            out.append(line.decode("utf-8"))
            continue
        except UnicodeDecodeError:
            pass
        head = line.split(b":", 1)[0]
        m = _CHARSET_PARAM.search(head)
        codec = _codec(m.group(1)) if m else None
        if codec is not None and not _QP_PARAM.search(head):
            try:
                out.append(line.decode(codec).replace(m.group(0).decode("ascii"), "", 1))
                continue
            except UnicodeDecodeError:
                pass
        out.append(_cp1252(line))
    return "".join(out)


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

    def _propfind(self, url: str, props: list[tuple[str, str]], depth: int, strict: bool = False
                  ) -> list[tuple[str, dict[str, ET.Element]]]:
        """[(absolute href, {prop tag: element with a 200 status})]. A <response> with an error status of its own is
        left out. `strict`: a cut-short answer raises DavIncomplete instead (its `partial` holds the rest): a 5xx
        status (507: the server cut the listing short, RFC 4918) on any response, or an error status on the
        collection itself. A 4xx on one member (gone since, not readable) is only that member left out."""
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
        problems = []
        for r in root.findall(_q(NS_DAV, "response")):
            href_el = r.find(_q(NS_DAV, "href"))
            status = (r.findtext(_q(NS_DAV, "status")) or "").strip()  # the response's own, not a propstat's
            code = _status_code(status) if status else None
            if code is not None and not 200 <= code < 300:
                raw = (href_el.text or "").strip() if href_el is not None else ""
                itself = not raw or _slash(urljoin(base, raw)) == _slash(urljoin(base, url))
                if code >= 500 or itself:
                    problems.append(f"{raw or '?'}: {status}")
                continue
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
        if strict and problems:
            raise DavIncomplete(f"PROPFIND {url}: the server says its answer is incomplete ("
                                + "; ".join(problems[:3]) + ")", partial=out)
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

    def _mkcol_body(self, method: str, kind: str, component: str, display: str) -> bytes:
        if method == "MKCALENDAR":
            root = ET.Element(_q(NS_CAL, "mkcalendar"))
        else:
            root = ET.Element(_q(NS_DAV, "mkcol"))
        setp = ET.SubElement(ET.SubElement(root, _q(NS_DAV, "set")), _q(NS_DAV, "prop"))
        if method == "MKCOL":  # extended MKCOL (RFC 5689): the resource type says what to create
            rt = ET.SubElement(setp, _q(NS_DAV, "resourcetype"))
            ET.SubElement(rt, _q(NS_DAV, "collection"))
            ET.SubElement(rt, _q(NS_CAL, "calendar") if kind == "caldav" else _q(NS_CARD, "addressbook"))
        ET.SubElement(setp, _q(NS_DAV, "displayname")).text = display
        if kind == "caldav":
            comps = ET.SubElement(setp, _q(NS_CAL, "supported-calendar-component-set"))
            ET.SubElement(comps, _q(NS_CAL, "comp")).set("name", component)
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    # answers to a creation that another form of the request may get past (not a 401, not a server in trouble)
    _MKCOL_RETRY = frozenset({400, 403, 405, 409, 412, 415, 422, 501})

    def _mkcol(self, kind: str, url: str, component: str = "VEVENT", display: str | None = None) -> str:
        """Creates a collection at `url` ("PST Import" unless `display` names it). CalDAV: MKCALENDAR, then an
        extended MKCOL (RFC 5689) when the server refuses it; CardDAV: extended MKCOL. A name with "/" (a sub-list at
        the source, "Tâches/24me") is tried again with " - " in its place: SmarterMail reads it as a path and
        answers 409. Server refusal -> explicit DavError (non-temporary except 5xx/429). `last_display`: the name
        the collection was created under."""
        display = display or IMPORT_DISPLAY
        names = [display]
        flat = re.sub(r"\s*[/\\]\s*", " - ", display).strip(" -") or display
        if flat != display:
            names.append(flat)
        methods = ["MKCALENDAR", "MKCOL"] if kind == "caldav" else ["MKCOL"]
        first: DavError | None = None
        tried = []
        for name, method in [(n, m) for n in names for m in methods]:
            try:
                self._request(method, url, ok=(200, 201, 204), content=self._mkcol_body(method, kind, component, name),
                              headers={"Content-Type": "application/xml; charset=utf-8"})
                self.last_display = name
                return url
            except DavError as e:
                if e.status == 405 and self._is_collection(kind, url):
                    self.last_display = name
                    return url  # already exists (created in the meantime)
                first = first or e
                tried.append(f"{method} '{name}' -> {f'HTTP {e.status}' if e.status else e}")
                if e.status not in self._MKCOL_RETRY:
                    first = e  # a server in trouble, a refused login: said as it is, not asked again
                    break
        assert first is not None
        what = "the calendar" if kind == "caldav" else "the address book"
        raise DavError(f"creation of {what} '{display}' refused by the server ({url}: {'; '.join(tried)})",
                       temporary=first.temporary, status=first.status) from first

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

    def ensure_collection(self, kind: str, url: str, component: str = "VEVENT", display: str | None = None,
                          create: bool = True) -> str:
        """The collection `url` exists (otherwise it is created, named `display`). A collection the server lists but
        will not open (MDaemon: "Invalid Path") is never created again over itself: an explicit error instead.
        `create` False: a collection the server listed earlier, never created over itself either."""
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
        if not create:
            what = "calendar" if kind == "caldav" else "address book"
            raise DavError(f"the {what} '{display or url}' ({url}) was listed by the server but cannot be opened now "
                           f"(PROPFIND -> HTTP {status}): not created again, check it on the server",
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

    def source_collections(self, kind: str, template_url: str | None, email: str, imap_host: str
                           ) -> tuple[list[tuple[str, str, list[str]]], dict]:
        """`collections`, with what the server says of who owns each one (`ownership`)."""
        coll, home = self._discover(kind, template_url, email, imap_host, False)
        return self.collections_at(kind, coll, home), self.ownership(kind, home, coll)

    def collections_at(self, kind: str, coll: str | None, home: str | None) -> list[tuple[str, str, list[str]]]:
        """As `collections`, from a discovery already made: the collections of `home`, else `coll` alone."""
        return [(url, label, comps) for url, label, comps, _w in self.listing(kind, coll, home)]

    WRITE_PRIVILEGES = frozenset({"all", "write", "write-content", "bind"})

    def listing(self, kind: str, coll: str | None, home: str | None, privileges: bool = False
                ) -> list[tuple[str, str, list[str], bool]]:
        """[(url, display name, accepted components, writable)] of the collections of `home`, else of `coll` alone.
        `privileges`: DAV:current-user-privilege-set is asked too, and a collection that says it grants no write
        (a global address list...) is not writable; without that property (or not asked), every one is."""
        props = [(NS_DAV, "resourcetype"), (NS_DAV, "displayname"), (NS_CAL, "supported-calendar-component-set")]
        if privileges:
            props.append((NS_DAV, "current-user-privilege-set"))
        if home is None:
            rows = [e for s in ([coll] if coll else []) for e in self._propfind(s, props, 0)]
        else:
            rows = self._propfind(home, props, 1)
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
            privs = p.get(_q(NS_DAV, "current-user-privilege-set"))
            writable = True
            if privs is not None:
                granted = {child.tag.rsplit("}", 1)[-1].lower() for pr in privs.findall(_q(NS_DAV, "privilege"))
                           for child in pr}
                writable = bool(granted & self.WRITE_PRIVILEGES)
            url = _slash(href)
            out.append((url, label or url.rstrip("/").rsplit("/", 1)[-1], comps, writable))
        return out

    def ownership(self, kind: str, home: str | None, coll: str | None = None) -> dict:
        """What says, for each collection of `home`, whether this account owns it: {"principals": hrefs of the account
        (DAV:current-user-principal, DAV:owner of the home), "collections": {url: {"owner": href or None, "shared":
        bool}}}. `shared`: the server marks it as shared with this account (calendarserver.org <shared/> resource
        type or CS:shared-url). Best effort: an error gives what is known (nothing)."""
        props = [(NS_DAV, "resourcetype"), (NS_DAV, "owner"), (NS_DAV, "current-user-principal"),
                 (NS_CS, "shared-url")]
        out: dict = {"principals": set(), "collections": {}}
        try:
            rows = self._propfind(home, props, 1) if home else self._propfind(coll, props, 0) if coll else []
        except DavError:
            return out
        for href, p in rows:
            me = self._href_of(p.get(_q(NS_DAV, "current-user-principal")))
            if me:
                out["principals"].add(_slash(urljoin(href, me)))
            owner = self._href_of(p.get(_q(NS_DAV, "owner")))
            owner = _slash(urljoin(href, owner)) if owner else None
            if home is not None and _slash(href) == home:
                if owner:
                    out["principals"].add(owner)
                continue
            rt = p.get(_q(NS_DAV, "resourcetype"))
            shared = (rt is not None and rt.find(_q(NS_CS, "shared")) is not None) or \
                p.get(_q(NS_CS, "shared-url")) is not None
            out["collections"][_slash(href)] = {"owner": owner, "shared": shared}
        return out

    def find_uid(self, kind: str, collection_url: str, uid: str, component: str = "VEVENT") -> list[str]:
        """Absolute hrefs of the objects of this collection whose UID is `uid` (addressbook-query / calendar-query,
        RFC 6352 and 4791). Raises DavError when the server does not do the query."""
        if kind == "caldav":
            body = ET.Element(_q(NS_CAL, "calendar-query"))
            ET.SubElement(ET.SubElement(body, _q(NS_DAV, "prop")), _q(NS_DAV, "getetag"))
            f = ET.SubElement(ET.SubElement(body, _q(NS_CAL, "filter")), _q(NS_CAL, "comp-filter"), name="VCALENDAR")
            f = ET.SubElement(f, _q(NS_CAL, "comp-filter"), name=component)
            m = ET.SubElement(ET.SubElement(f, _q(NS_CAL, "prop-filter"), name="UID"), _q(NS_CAL, "text-match"),
                              collation="i;octet")
        else:
            body = ET.Element(_q(NS_CARD, "addressbook-query"))
            ET.SubElement(ET.SubElement(body, _q(NS_DAV, "prop")), _q(NS_DAV, "getetag"))
            f = ET.SubElement(body, _q(NS_CARD, "filter"))
            m = ET.SubElement(ET.SubElement(f, _q(NS_CARD, "prop-filter"), name="UID"), _q(NS_CARD, "text-match"),
                              collation="i;octet")
            m.set("match-type", "equals")
        m.text = uid
        resp = self._request("REPORT", _slash(collection_url), ok=(207,),
                             content=ET.tostring(body, encoding="utf-8", xml_declaration=True),
                             headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            raise DavError(f"REPORT {collection_url}: invalid XML ({e})") from e
        base = str(resp.url)
        out = []
        for r in root.findall(_q(NS_DAV, "response")):
            href = (r.findtext(_q(NS_DAV, "href")) or "").strip()
            if href and _slash(urljoin(base, href)) != _slash(collection_url):
                out.append(urljoin(base, href))
        return out

    # ---- objects -----------------------------------------------------
    @staticmethod
    def _item_url(collection_url: str, filename: str) -> str:
        return _slash(collection_url) + quote(filename, safe="")

    def put(self, collection_url: str, filename: str, content: str, kind: str, if_match: str | None = None,
            if_none_match: bool = False) -> int:
        """PUT of one resource. `if_match`: only over the resource that still has this etag (412 otherwise, raised
        as a temporary DavError: nothing is overwritten, the next pass lists it again). `if_none_match`: only if
        no resource has this name yet (412 otherwise, raised as DavError status 412, not temporary). The ETag the
        server gives back is kept in `last_etag` ('' when it gives none)."""
        ctype = "text/calendar" if kind == "caldav" else "text/vcard"
        headers = {"Content-Type": f"{ctype}; charset=utf-8"}
        if if_match:
            headers["If-Match"] = if_match
        if if_none_match:
            headers["If-None-Match"] = "*"
        self.last_etag = ""
        try:
            resp = self._request("PUT", self._item_url(collection_url, filename), content=content.encode("utf-8"),
                                 headers=headers)
        except DavError as e:
            if if_match and e.status == 412:
                raise DavError(f"PUT {filename}: changed on the server since it was listed (HTTP 412), not "
                               "overwritten", temporary=True, status=412) from e
            raise
        self.last_etag = (resp.headers.get("etag") or "").strip()
        return resp.status_code

    def state_of(self, collection_url: str, filename: str) -> str | None:
        """The etag of one resource ('' when the server gives none), None when there is no such resource."""
        try:
            return self.etag_of(collection_url, filename)
        except DavError as e:
            if e.status == 404:
                return None
            raise

    def etag_of(self, collection_url: str, filename: str) -> str:
        """The etag of one resource (PROPFIND Depth 0), '' when the server gives none."""
        url = self._item_url(collection_url, filename)
        for _href, p in self._propfind(url, [(NS_DAV, "getetag")], 0):
            et = p.get(_q(NS_DAV, "getetag"))
            return (et.text or "").strip() if et is not None else ""
        return ""

    def exists(self, collection_url: str, filename: str) -> bool:
        return self.get(collection_url, filename) is not None

    def get(self, collection_url: str, filename: str) -> str | None:
        """The text of one resource (decode_object of its bytes: never the charset its Content-Type says), None when
        there is no such resource."""
        raw = self.get_bytes(collection_url, filename)
        return decode_object(raw) if raw is not None else None

    def get_bytes(self, collection_url: str, filename: str) -> bytes | None:
        """One resource exactly as the server gives it, None when there is no such resource."""
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
        return resp.content

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

    def names_etags(self, collection_url: str, strict: bool = False) -> dict[str, str]:
        """{name (as names() gives it): etag, '' when the server gives none} of the non-collection resources.
        `strict`: an answer the server says is incomplete raises DavIncomplete (`partial` in this form)."""
        def named(tags: dict[str, str]) -> dict[str, str]:
            from urllib.parse import unquote, urlparse
            out: dict[str, str] = {}
            for href, et in tags.items():
                seg = (urlparse(href).path or href).rstrip("/").rsplit("/", 1)[-1]
                if seg:
                    out[unquote(seg)] = et
            return out
        try:
            return named(self.etags(collection_url, strict))
        except DavIncomplete as e:
            raise DavIncomplete(str(e), partial=named(e.partial or {})) from e

    def delete_item(self, collection_url: str, filename: str, etag: str | None = None) -> int:
        """DELETE of one resource, with If-Match when the etag is known. 2xx deleted, 404 gone already, 412 changed
        since it was listed; any other answer raises."""
        url = self._item_url(collection_url, filename)
        if etag:
            return self.delete_if_match(url, etag)
        try:
            resp = self._send("DELETE", url)
        except httpx.HTTPError as e:
            raise DavError(f"DELETE {url}: {e.__class__.__name__}: {e}", temporary=True) from e
        sc = resp.status_code
        if 200 <= sc < 300 or sc == 404:
            return sc
        raise DavError(f"DELETE {url} -> HTTP {sc}", temporary=(sc >= 500 or sc == 429), status=sc,
                       retry_after=_retry_after(resp))

    def delete_collection(self, collection_url: str) -> int:
        """DELETE of a whole collection and what it holds (RFC 4918: always Depth infinity). The HTTP status: 2xx
        (207 included: a multistatus may name members it could not delete, the caller checks the collection is gone),
        404 gone already; any other answer raises."""
        url = _slash(collection_url)
        try:
            resp = self._send("DELETE", url)
        except httpx.HTTPError as e:
            raise DavError(f"DELETE {url}: {e.__class__.__name__}: {e}", temporary=True) from e
        sc = resp.status_code
        if 200 <= sc < 300 or sc == 404:
            return sc
        raise DavError(f"DELETE {url} -> HTTP {sc}", temporary=(sc >= 500 or sc == 429), status=sc,
                       retry_after=_retry_after(resp))

    # ---- duplicate search (dedup_runs.py) -------------------------------
    def etags(self, collection_url: str, strict: bool = False) -> dict[str, str]:
        """{absolute href: etag ('' when the server gives none)} of the non-collection resources. `strict`: see
        _propfind (DavIncomplete, `partial` in this form)."""
        url = _slash(collection_url)
        props = [(NS_DAV, "resourcetype"), (NS_DAV, "getetag")]
        try:
            rows = self._propfind(url, props, 1, strict)
        except DavIncomplete as e:
            raise DavIncomplete(str(e), partial=self._etags_of(url, e.partial or [])) from e
        return self._etags_of(url, rows)

    @staticmethod
    def _etags_of(url: str, rows: list) -> dict[str, str]:
        out: dict[str, str] = {}
        for href, p in rows:
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
