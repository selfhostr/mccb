"""IMAP as a source (read-only: EXAMINE + BODY.PEEK, the source is never altered) and as a target."""
from __future__ import annotations

import hashlib
import imaplib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.parser import BytesHeaderParser
from typing import Iterator

from ..server.imap import (SPECIAL_FLAGS, SPECIAL_NAMES, ImapConn, ImapError, ImapParams, Mailbox, _text, parse_list,
                           personal_prefix, quote, wrap_exc)
from . import oauth, registry
from .base import (CAP_FLAGS, CAP_FOLDERS, CAP_INCREMENTAL, CAP_INTERNAL_DATE, CAP_PARALLEL, CAP_QUOTA, CAP_READBACK,
                   IMPORT_HEADER, MAIL, Changes, ConnectorError, FetchedItem, FolderPlan, OutgoingItem, Source,
                   SourceFolder, SourceItem, Target, TargetSession, subject_of)

FETCH_CHUNK = 500  # UIDs per listing FETCH
LIST_ITEMS = "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
# virtual folders showing messages that live elsewhere: copying them would duplicate the mailbox
VIRTUAL_FLAGS = {"\\all", "\\flagged", "\\important"}
SYSTEM_FLAGS = {"\\seen", "\\answered", "\\flagged", "\\draft"}  # \Recent is refused by APPEND, \Deleted not copied
KEYWORD_RE = re.compile(r"^[A-Za-z0-9$_.+-]+$")
MONTHS = {m: i + 1 for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
                                           "Nov", "Dec"))}
INTERNALDATE_RE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})")
_KINDS = {"temp": ConnectorError.TRANSIENT, "perm": ConnectorError.REJECTED, "fatal": ConnectorError.QUOTA,
          "auth": ConnectorError.AUTH, "trycreate": ConnectorError.FOLDER_MISSING}


def convert(e: BaseException, connecting: bool = False) -> ConnectorError:
    err = wrap_exc(e)
    kind = _KINDS.get(err.kind, ConnectorError.REJECTED)
    if connecting and kind == ConnectorError.TRANSIENT:
        kind = ConnectorError.UNREACHABLE
    return ConnectorError(str(err), kind)


@contextmanager
def _guard(connecting: bool = False):
    try:
        yield
    except ConnectorError:
        raise
    except Exception as e:  # noqa: BLE001 - classified by wrap_exc
        raise convert(e, connecting) from e


def login_of(config: dict) -> str:
    """Dovecot-style master user: "<user><separator><master>", with the password of the master. The login alone names
    the mailbox: changing the master keeps the migration state."""
    login = config.get("login") or ""
    master = config.get("master_user") or ""
    return f"{login}{config.get('master_separator') or '*'}{master}" if master else login


def params_of(config: dict, secrets: dict) -> ImapParams:
    return ImapParams(host=config["host"], port=int(config.get("port") or 993),
                      security=config.get("security") or "ssl", verify_tls=bool(config.get("verify_tls", True)),
                      login=login_of(config), password=secrets.get("password") or "")


def oauth_params_of(config: dict, secrets: dict) -> ImapParams:
    """XOAUTH2: the access token is refreshed by `token`, and asked for at each connection."""
    source = oauth.OAuthToken(config.get("token_url") or "", config.get("client_id") or "",
                              secrets.get("client_secret") or "", config.get("scope") or "",
                              secrets.get("refresh_token") or "", bool(config.get("verify_tls", True)))
    params = params_of(config, secrets)
    params.password = ""
    params.token_source = source.token
    return params


def identity(config: dict) -> str:
    return f"{str(config.get('host', '')).lower()}:{config.get('port', 993)}:{config.get('login', '')}"


# ------------------------------------------------------------------ pure helpers (unit tested)
def parse_internaldate(text: str) -> datetime | None:
    m = INTERNALDATE_RE.search(text or "")
    if not m or m.group(2).capitalize() not in MONTHS:
        return None
    off = timedelta(hours=int(m.group(8)), minutes=int(m.group(9)))
    try:
        return datetime(int(m.group(3)), MONTHS[m.group(2).capitalize()], int(m.group(1)), int(m.group(4)),
                        int(m.group(5)), int(m.group(6)), tzinfo=timezone(off if m.group(7) == "+" else -off))
    except ValueError:
        return None


def keep_flags(flags: list[str]) -> list[str]:
    """Flags worth copying: the system ones an APPEND accepts and well-formed keywords ($Forwarded, NonJunk...)."""
    return [f for f in flags if f.lower() in SYSTEM_FLAGS or (not f.startswith("\\") and KEYWORD_RE.match(f))]


def parse_fetch(data) -> list[dict]:
    """Records {uid, flags, date, size, message_id} of a FETCH response. Servers order the attributes freely, and
    imaplib splits a response around its literal: what follows the literal may hold UID or FLAGS."""
    recs: list[dict] = []
    for d in data or []:
        if isinstance(d, tuple):
            meta, literal = d[0] or b"", d[1] if len(d) > 1 else None
            fresh = True
        elif isinstance(d, bytes):
            meta, literal = d, None
            fresh = bool(re.match(rb"\d+ \(", d))
        else:
            continue
        if fresh or not recs:
            recs.append({"meta": meta, "literal": literal})
        else:
            recs[-1]["meta"] += b" " + meta
    out = []
    for r in recs:
        meta = r["meta"].decode("utf-8", "replace")
        uid = re.search(r"\bUID (\d+)", meta)
        if not uid:
            continue  # unsolicited FETCH (flag update)
        flags = re.search(r"\bFLAGS \(([^)]*)\)", meta)
        size = re.search(r"\bRFC822\.SIZE (\d+)", meta)
        date = re.search(r'\bINTERNALDATE "([^"]+)"', meta)
        mid = None
        if isinstance(r["literal"], bytes):
            mid = " ".join(str(BytesHeaderParser().parsebytes(r["literal"]).get("Message-ID") or "").split()) or None
        out.append({"uid": int(uid.group(1)), "flags": flags.group(1).split() if flags else [],
                    "size": int(size.group(1)) if size else None,
                    "date": parse_internaldate(date.group(1)) if date else None, "message_id": mid})
    return out


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:20]  # noqa: S324 - identifier, not security


def folder_hash(path: list[str]) -> str:
    return hashlib.sha1("\0".join(path).encode("utf-8")).hexdigest()[:8]  # noqa: S324 - identifier, not security


def alt_key_of(rec: dict) -> str:
    """Identity that survives a UIDVALIDITY change: the Message-ID, else the date and size."""
    if rec["message_id"]:
        return "m:" + _digest(rec["message_id"])
    return "d:" + _digest(f"{rec['date']}|{rec['size']}")


def item_keys(path: list[str], uidvalidity: int, recs: list[dict]) -> list[str]:
    """Stable keys, scoped by folder (two mailboxes may share UIDVALIDITY and UIDs): UIDVALIDITY + UID, or for a
    server without UIDVALIDITY the Message-ID digest (else date + size), with a counter for identical ones."""
    fh = folder_hash(path)
    if uidvalidity:
        return [f"{fh}-{uidvalidity}-{r['uid']}" for r in recs]
    seen: dict[str, int] = {}
    keys = []
    for r in recs:
        alt = alt_key_of(r).replace(":", "")
        seen[alt] = seen.get(alt, 0) + 1
        keys.append(f"{fh}-{alt}" + (f"-{seen[alt]}" if seen[alt] > 1 else ""))
    return keys


def folders_of(boxes: list[Mailbox], delim: str, prefix: str) -> list[SourceFolder]:
    """Source folders of a LIST: namespace prefix removed, virtual and unselectable folders dropped, special
    folders from SPECIAL-USE, then from the usual names for servers that do not have it."""
    out: list[SourceFolder] = []
    taken: set[str] = set()
    seen: set[str] = set()
    for b in sorted(boxes, key=lambda x: x.name):
        flags = {f.lower() for f in b.flags}
        if b.name in seen or flags & {"\\noselect", "\\nonexistent"} or flags & VIRTUAL_FLAGS:
            continue
        seen.add(b.name)
        d = b.delim or delim
        name = b.decoded
        if name.upper() == "INBOX":
            path = ["INBOX"]
        else:
            if prefix and name.startswith(prefix):
                name = name[len(prefix):]
            path = [p for p in (name.split(d) if d else [name]) if p] or [name]
        f = SourceFolder(path=path, kind=MAIL, ref=b.name)
        if path == ["INBOX"]:
            f.special = "inbox"
        else:
            for sp, flag in SPECIAL_FLAGS.items():
                if flag.lower() in flags and sp not in taken:
                    f.special = sp
            if f.special is None and "\\spam" in flags and "junk" not in taken:
                f.special = "junk"
            if f.special:
                taken.add(f.special)
        out.append(f)
    for f in out:  # no SPECIAL-USE: by name, without stealing a role a flag already gave
        if f.special is not None or (len(f.path) > 1 and not (len(f.path) == 2 and f.path[0].upper() == "INBOX")):
            continue
        low = f.path[-1].lower()
        for sp, names in SPECIAL_NAMES.items():
            if sp not in taken and low in {n.lower() for n in names}:
                f.special = sp
                taken.add(sp)
                break
    return out


# ------------------------------------------------------------------ source
class ImapSource(Source):
    make_params = staticmethod(params_of)

    def __init__(self, config: dict, secrets: dict, ctx: dict | None = None):
        self.params = self.make_params(config, secrets)
        self.conn: ImapConn | None = None
        self._uv: dict[str, int] = {}

    def open(self) -> None:
        conn = ImapConn(self.params)
        with _guard(connecting=True):
            conn.connect()
        self.conn, self._uv = conn, {}

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
        self.conn = None

    def _c(self) -> ImapConn:
        if self.conn is None:
            raise ConnectorError("IMAP source not connected", ConnectorError.TRANSIENT)
        return self.conn

    def _list_boxes(self) -> list[Mailbox]:
        c = self._c()
        if "SPECIAL-USE" in c.caps:  # some servers only give the flags when asked
            try:
                typ, data = c._m()._simple_command("LIST", '""', '"*"', "RETURN", "(SPECIAL-USE)")
                typ, data = c._m()._untagged_response(typ, data, "LIST")
                boxes = parse_list(data) if typ == "OK" else []
                if boxes:
                    return boxes
            except imaplib.IMAP4.error:
                pass
        return c.list()

    def folders(self) -> list[SourceFolder]:
        c = self._c()
        with _guard():
            boxes = self._list_boxes()
            delim = c.delimiter()
            prefix = personal_prefix(c)
            out = folders_of(boxes, delim, prefix if prefix.endswith(delim) else "")
            for f in out:
                try:
                    f.count = c.status_messages(f.ref)
                except ImapError as e:
                    if e.kind == "temp":
                        raise
                    f.count = None  # refined by the listing
            return out

    def _uidvalidity(self, name: str) -> int:
        c = self._c()
        typ, data = c._m().response("UIDVALIDITY")
        m = re.search(r"\d+", _text(data))
        if not m:
            typ, data = c._m().status(quote(name), "(UIDVALIDITY)")
            m = re.search(r"UIDVALIDITY\s+(\d+)", _text(data), re.I) if typ == "OK" else None
            return int(m.group(1)) if m else 0
        return int(m.group(0))

    def _select(self, folder: SourceFolder) -> int:
        c, name = self._c(), folder.ref
        if c.selected != name or name not in self._uv:
            c.select(name, readonly=True)
            self._uv[name] = self._uidvalidity(name)
        return self._uv[name]

    def _uids(self, criteria: tuple[str, ...]) -> list[int]:
        typ, data = self._c()._m().uid("SEARCH", *criteria)
        t = _text(self._c()._check(typ, data, "SEARCH"))
        return sorted(int(u) for u in t.split() if u.isdigit())

    def _records(self, uids: list[int]) -> list[dict]:
        c = self._c()
        recs: list[dict] = []
        for i in range(0, len(uids), FETCH_CHUNK):
            chunk = ",".join(str(u) for u in uids[i:i + FETCH_CHUNK])
            typ, data = c._m().uid("FETCH", chunk, LIST_ITEMS)
            recs.extend(parse_fetch(c._check(typ, data, "FETCH")))
        return [r for r in recs if "\\deleted" not in {f.lower() for f in r["flags"]}]

    def _items(self, folder: SourceFolder, uv: int, recs: list[dict]) -> list[SourceItem]:
        keys = item_keys(folder.path, uv, recs)
        return [SourceItem(key=k, alt_key=alt_key_of(r), date=r["date"], size=r["size"], flags=keep_flags(r["flags"]),
                           ref=(uv, r["uid"])) for k, r in zip(keys, recs)]

    def iter_items(self, folder: SourceFolder) -> Iterator[SourceItem]:
        with _guard():
            uv = self._select(folder)
            items = self._items(folder, uv, self._records(self._uids(("ALL",))))
        yield from items

    def changed_since(self, folder: SourceFolder, cursor: str | None) -> Changes:
        """New messages since the cursor (UIDs are ascending). Flag changes and deletions are not reported."""
        state = None
        try:
            state = json.loads(cursor) if cursor else None
        except ValueError:
            pass
        with _guard():
            uv = self._select(folder)
            valid = isinstance(state, dict) and bool(uv) and state.get("uv") == uv
            last = int(state.get("uid") or 0) if valid else 0
            # "n:*" always returns the highest UID, even below n
            uids = [u for u in self._uids(("UID", f"{last + 1}:*")) if u > last] if valid else self._uids(("ALL",))
            recs = self._records(uids)
        high = max(uids, default=last)
        return Changes(self._items(folder, uv, recs), json.dumps({"uv": uv, "uid": high}), reset=not valid)

    def fetch(self, folder: SourceFolder, item: SourceItem, import_id: str) -> FetchedItem | None:
        uv, uid = item.ref
        with _guard():
            cur = self._select(folder)
            if uv and cur != uv:
                raise ConnectorError(f"UIDVALIDITY of '{folder.ref}' changed during the job: rerun to list it again",
                                     ConnectorError.REJECTED)
            raw = self._c().fetch_raw(folder.ref, str(uid))
        if raw is None:
            return None  # expunged since the listing
        return FetchedItem(raw=raw, internal_date=item.date, flags=list(item.flags), subject=subject_of(raw))


# ------------------------------------------------------------------ target
class ImapSession(TargetSession):
    def __init__(self, params: ImapParams, root: str | None):
        self.conn = ImapConn(params)
        self.root = root
        self._existing: set[str] = set()

    @property
    def connected(self) -> bool:
        return self.conn.M is not None

    def connect(self) -> None:
        with _guard(connecting=True):
            self.conn.connect()

    def close(self) -> None:
        self.conn.close()

    def plan_folders(self, folders: list[SourceFolder]) -> dict[str, FolderPlan]:
        from ..server.runner import plan_folders  # the PST path uses the same plan
        with _guard():
            boxes = self.conn.list()
            delim = self.conn.delimiter()
            prefix = personal_prefix(self.conn)
            if prefix and not prefix.endswith(delim):
                prefix = ""
            self._existing = {b.name for b in boxes}
            plan = plan_folders(folders, boxes, delim, self.root, prefix)
        return {k: FolderPlan(name, flag) for k, (name, flag) in plan.items()}

    def ensure_folder(self, name: str, flag: str | None = None) -> None:
        with _guard():
            if not self._existing:
                self._existing = {b.name for b in self.conn.list()}
            if name.upper() == "INBOX" or name in self._existing:
                return
            delim = self.conn.delimiter()
            parts = name.split(delim)
            for i in range(1, len(parts)):
                parent = delim.join(parts[:i])
                if parent and parent not in self._existing and parent.upper() != "INBOX":
                    self.conn.create(parent)
                    self._existing.add(parent)
            self.conn.create(name, flag)
            self._existing.add(name)

    def put(self, folder: str, item: OutgoingItem) -> str | None:
        with _guard():
            try:
                return self.conn.append(folder, item.raw, item.flags, item.internal_date)
            except ImapError as e:
                system = [f for f in item.flags if f.startswith("\\")]
                if e.kind != "perm" or len(system) == len(item.flags):
                    raise
                # some servers refuse unknown keywords: the message matters more than its keywords
                return self.conn.append(folder, item.raw, system, item.internal_date)

    def find(self, folder: str, item: OutgoingItem) -> list[str]:
        with _guard():
            return self.conn.find_import_id(folder, IMPORT_HEADER, item.import_id)

    def count_ours(self, folder: str, namespace: str, names: set[str] | None = None) -> int:
        with _guard():
            return self.conn.count_import_prefix(folder, IMPORT_HEADER, f"{namespace}-")

    def read(self, folder: str, remote_id: str) -> bytes | None:
        with _guard():
            return self.conn.fetch_raw(folder, str(remote_id))

    def quota(self) -> dict | None:
        with _guard():
            return self.conn.quota()

    def info(self) -> dict:
        from ..server.policy import append_limit
        out = {"capabilities": sorted(self.conn.caps)[:50]}
        limit = append_limit(self.conn.caps)  # RFC 7889: the largest message the server accepts
        if limit:
            out["append_limit"] = limit
        return out

    def has_folder(self, name: str) -> bool | None:
        return name.upper() == "INBOX" or name in self._existing


class ImapTarget(Target):
    capabilities = frozenset({CAP_FOLDERS, CAP_FLAGS, CAP_INTERNAL_DATE, CAP_QUOTA, CAP_PARALLEL, CAP_READBACK})
    make_params = staticmethod(params_of)

    def __init__(self, config: dict, secrets: dict, ctx: dict | None = None):
        self.params = self.make_params(config, secrets)
        self.root = (config.get("root_folder") or "").strip() or None

    def session(self) -> ImapSession:
        return ImapSession(self.params, self.root)


class ImapOAuthSource(ImapSource):
    make_params = staticmethod(oauth_params_of)


class ImapOAuthTarget(ImapTarget):
    make_params = staticmethod(oauth_params_of)


_SERVER = (
    registry.Field("host", "Server", required=True),
    registry.Field("port", "Port", "number", default=993),
    registry.Field("security", "Security", "select", default="ssl", options=("ssl", "starttls", "none")),
    registry.Field("verify_tls", "Verify the TLS certificate", "boolean", default=True),
    registry.Field("login", "Login", required=True),
)
_CONNECTION = _SERVER + (
    registry.Field("password", "Password", "password", required=True, secret=True),
    registry.Field("master_user", "Master user",
                   help="Dovecot-style admin access: the password above is the master's, the login is the mailbox to reach."),
    registry.Field("master_separator", "Master user separator", help="Between the login and the master user. Empty: *"),
)
_OAUTH_CONNECTION = _SERVER + (
    registry.Field("token_url", "Token endpoint", "url", required=True,
                   help="For Microsoft 365: https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token"),
    registry.Field("client_id", "Client ID", required=True),
    registry.Field("scope", "Scope", help="For Microsoft 365 app-only: https://outlook.office365.com/.default"),
    registry.Field("client_secret", "Client secret", "password", secret=True),
    registry.Field("refresh_token", "Refresh token", "password", secret=True,
                   help="Empty: the client credentials grant, for app-only access."),
)

registry.register(registry.ConnectorSpec(
    kind="imap", label="IMAP", description="Any IMAP mailbox, read without altering it or written to.",
    source=registry.Role(ImapSource, (MAIL,), frozenset({CAP_FOLDERS, CAP_FLAGS, CAP_INTERNAL_DATE, CAP_INCREMENTAL}),
                         _CONNECTION, identity),
    target=registry.Role(ImapTarget, (MAIL,), ImapTarget.capabilities,
                         _CONNECTION + (registry.Field("root_folder", "Root folder",
                                                       help="Put everything under this folder."),), identity),
))
registry.register(registry.ConnectorSpec(
    kind="imap_oauth2", label="IMAP (OAuth2)", description="An IMAP mailbox reached with an OAuth2 token (XOAUTH2).",
    source=registry.Role(ImapOAuthSource, (MAIL,), frozenset({CAP_FOLDERS, CAP_FLAGS, CAP_INTERNAL_DATE, CAP_INCREMENTAL}),
                         _OAUTH_CONNECTION, identity),
    target=registry.Role(ImapOAuthTarget, (MAIL,), ImapOAuthTarget.capabilities,
                         _OAUTH_CONNECTION + (registry.Field("root_folder", "Root folder",
                                                             help="Put everything under this folder."),), identity),
))
