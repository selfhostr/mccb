"""IMAP client (imaplib): connection, modified UTF-7, folders, APPEND, search, quota."""
from __future__ import annotations

import base64
import imaplib
import re
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime

imaplib._MAXLINE = 10_000_000  # large LIST responses

SPECIAL_FLAGS = {"sent": "\\Sent", "drafts": "\\Drafts", "trash": "\\Trash", "junk": "\\Junk"}
SPECIAL_NAMES = {
    "sent": ["Sent", "Sent Items", "Sent Messages", "Envoyés", "Éléments envoyés", "INBOX.Sent"],
    "drafts": ["Drafts", "Brouillons", "INBOX.Drafts"],
    "trash": ["Trash", "Deleted Items", "Deleted Messages", "Corbeille", "Éléments supprimés", "INBOX.Trash"],
    "junk": ["Junk", "Spam", "Junk E-mail", "Courrier indésirable", "INBOX.Junk", "INBOX.Spam"],
}
SPECIAL_DEFAULT = {"sent": "Sent", "drafts": "Drafts", "trash": "Trash", "junk": "Junk"}


# ---------------------------------------------------------------- modified UTF-7 (RFC 3501 §5.1.3)
def _b64enc(s: str) -> str:
    return base64.b64encode(s.encode("utf-16-be")).decode("ascii").rstrip("=").replace("/", ",")


def utf7_encode(s: str) -> str:
    out, buf = [], []
    for ch in s:
        o = ord(ch)
        if 0x20 <= o <= 0x7E:
            if buf:
                out.append("&" + _b64enc("".join(buf)) + "-")
                buf = []
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    if buf:
        out.append("&" + _b64enc("".join(buf)) + "-")
    return "".join(out)


def utf7_decode(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        ch = s[i]
        if ch == "&":
            j = s.index("-", i)
            if j == i + 1:
                out.append("&")
            else:
                b = s[i + 1:j].replace(",", "/")
                b += "=" * (-len(b) % 4)
                out.append(base64.b64decode(b).decode("utf-16-be"))
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _login(M: imaplib.IMAP4, login: str, password: str) -> None:
    """AUTHENTICATE PLAIN in UTF-8 if advertised, otherwise LOGIN with properly
    quoted strings (imaplib.login does not quote and forces ASCII)."""
    caps = {c.upper() for c in getattr(M, "capabilities", ())}
    if "AUTH=PLAIN" in caps:
        blob = b"\0".join((b"", login.encode("utf-8"), password.encode("utf-8")))
        M.authenticate("PLAIN", lambda _challenge: blob)
        return
    # imaplib encodes commands in ASCII by default: switch to UTF-8
    old = M._encoding
    M._encoding = "utf-8"
    try:
        typ, data = M._simple_command("LOGIN", quote(login), quote(password))
        if typ != "OK":
            raise imaplib.IMAP4.error(_text(data) or "LOGIN refused")
        M.state = "AUTH"
    finally:
        M._encoding = old


# ---------------------------------------------------------------- errors
class ImapError(Exception):
    """IMAP error. kind: 'temp' (retry), 'perm' (permanent for the item), 'fatal' (stop the mailbox),
    'auth' (credentials), 'trycreate' (folder missing)."""

    def __init__(self, msg: str, kind: str = "perm"):
        super().__init__(msg)
        self.kind = kind

    @property
    def temporary(self) -> bool:
        return self.kind == "temp"


TEMP_CODES = ("[UNAVAILABLE]", "[INUSE]", "[LIMIT]", "[SERVERBUG]", "[CONTACTADMIN]", "TRY AGAIN", "TEMPORAR")
FATAL_CODES = ("[OVERQUOTA]", "QUOTA EXCEEDED", "OVER QUOTA")


def classify_response(text: str) -> str:
    up = text.upper()
    if "[TRYCREATE]" in up:
        return "trycreate"
    if any(c in up for c in FATAL_CODES):
        return "fatal"
    if "[AUTHENTICATIONFAILED]" in up:
        return "auth"
    if any(c in up for c in TEMP_CODES):
        return "temp"
    return "perm"


def _text(data) -> str:
    parts = []
    for d in data or []:
        if isinstance(d, tuple):
            d = d[0]
        if isinstance(d, bytes):
            d = d.decode("utf-8", "replace")
        if d is not None:
            parts.append(str(d))
    return " ".join(parts)


def wrap_exc(e: BaseException) -> ImapError:
    if isinstance(e, ImapError):
        return e
    if isinstance(e, (imaplib.IMAP4.abort, OSError, socket.timeout, ssl.SSLError, EOFError)):
        return ImapError(f"IMAP connection: {e}", "temp")
    if isinstance(e, imaplib.IMAP4.error):
        return ImapError(str(e), classify_response(str(e)))
    return ImapError(f"{type(e).__name__}: {e}", "perm")


# ---------------------------------------------------------------- LIST
LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?P<delim>NIL|"(?:[^"\\]|\\.)*")\s+(?P<name>.*)$')


@dataclass
class Mailbox:
    name: str          # raw name (modified UTF-7)
    flags: list[str]
    delim: str | None

    @property
    def decoded(self) -> str:
        try:
            return utf7_decode(self.name)
        except Exception:
            return self.name


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return re.sub(r"\\(.)", r"\1", s[1:-1])
    return s


def parse_list(data) -> list[Mailbox]:
    out = []
    items = list(data or [])
    i = 0
    while i < len(items):
        d = items[i]
        i += 1
        if d is None:
            continue
        if isinstance(d, tuple):  # name as literal
            line = d[0].decode("utf-8", "replace") if isinstance(d[0], bytes) else d[0]
            literal = d[1].decode("utf-8", "replace") if isinstance(d[1], bytes) else d[1]
            m = LIST_RE.match(line.strip())
            if not m:
                continue
            name = literal
        else:
            line = d.decode("utf-8", "replace") if isinstance(d, bytes) else d
            m = LIST_RE.match(line.strip())
            if not m:
                continue
            name = _unquote(m.group("name"))
        delim = m.group("delim")
        delim = None if delim == "NIL" else _unquote(delim)
        out.append(Mailbox(name=name, flags=m.group("flags").split(), delim=delim))
    return out


# ---------------------------------------------------------------- connection
@dataclass
class ImapParams:
    host: str
    port: int
    security: str  # ssl|starttls|none
    verify_tls: bool
    login: str
    password: str
    timeout: float = 120.0


def _ssl_ctx(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class ImapConn:
    def __init__(self, p: ImapParams):
        self.p = p
        self.M: imaplib.IMAP4 | None = None
        self.caps: set[str] = set()
        self.selected: str | None = None
        self._delim: str | None = None

    # -- lifecycle
    def connect(self) -> "ImapConn":
        self.close()
        p = self.p
        try:
            if p.security == "ssl":
                M = imaplib.IMAP4_SSL(p.host, p.port, ssl_context=_ssl_ctx(p.verify_tls), timeout=p.timeout)
            else:
                M = imaplib.IMAP4(p.host, p.port, timeout=p.timeout)
                if p.security == "starttls":
                    M.starttls(ssl_context=_ssl_ctx(p.verify_tls))
        except Exception as e:
            raise ImapError(f"cannot connect to {p.host}:{p.port}: {e}", "temp") from e
        try:
            _login(M, p.login, p.password)
        except imaplib.IMAP4.error as e:
            try:
                M.logout()
            except Exception:
                pass
            k = classify_response(str(e))
            raise ImapError(f"authentication refused: {e}", "temp" if k == "temp" else "auth") from e
        except Exception as e:
            raise wrap_exc(e) from e
        self.M = M
        try:
            typ, data = M.capability()
            self.caps = set(_text(data).upper().split())
        except Exception:
            self.caps = {c.upper() for c in getattr(M, "capabilities", ())}
        self.selected = None
        return self

    def close(self) -> None:
        if self.M is not None:
            try:
                self.M.logout()
            except Exception:
                pass
        self.M = None
        self.selected = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *a):
        self.close()

    def _m(self) -> imaplib.IMAP4:
        if self.M is None:
            self.connect()
        return self.M  # type: ignore[return-value]

    def _check(self, typ, data, what: str):
        if typ != "OK":
            t = _text(data)
            raise ImapError(f"{what}: {typ} {t}", classify_response(t))
        return data

    # -- folders
    def list(self) -> list[Mailbox]:
        try:
            typ, data = self._m().list('""', '"*"')
        except Exception as e:
            raise wrap_exc(e) from e
        return parse_list(self._check(typ, data, "LIST"))

    def delimiter(self) -> str:
        if self._delim is None:
            try:
                typ, data = self._m().list('""', '""')
                boxes = parse_list(data) if typ == "OK" else []
            except Exception as e:
                raise wrap_exc(e) from e
            d = boxes[0].delim if boxes and boxes[0].delim else None
            if not d:
                for b in self.list():
                    if b.delim:
                        d = b.delim
                        break
            self._delim = d or "/"
        return self._delim

    def create(self, name: str, special_flag: str | None = None) -> None:
        M = self._m()
        try:
            if special_flag and "CREATE-SPECIAL-USE" in self.caps:
                typ, data = M._simple_command("CREATE", quote(name), f"(USE ({special_flag}))")
                if typ != "OK":
                    typ, data = M.create(quote(name))
            else:
                typ, data = M.create(quote(name))
        except imaplib.IMAP4.error as e:
            if "ALREADYEXISTS" in str(e).upper() or "EXISTS" in str(e).upper():
                return
            raise wrap_exc(e) from e
        except Exception as e:
            raise wrap_exc(e) from e
        if typ != "OK":
            t = _text(data).upper()
            if "ALREADYEXISTS" in t or "EXISTS" in t:
                return
            raise ImapError(f"CREATE {name}: {_text(data)}", classify_response(t))
        try:
            M.subscribe(quote(name))
        except Exception:
            pass

    def select(self, name: str, readonly: bool = True) -> int:
        try:
            typ, data = self._m().select(quote(name), readonly=readonly)
        except Exception as e:
            self.selected = None
            raise wrap_exc(e) from e
        self._check(typ, data, f"SELECT {name}")
        self.selected = name
        try:
            return int(data[0])
        except Exception:
            return 0

    def status_messages(self, name: str) -> int:
        try:
            typ, data = self._m().status(quote(name), "(MESSAGES)")
        except Exception as e:
            raise wrap_exc(e) from e
        t = _text(self._check(typ, data, f"STATUS {name}"))
        m = re.search(r"MESSAGES\s+(\d+)", t, re.I)
        return int(m.group(1)) if m else 0

    # -- messages
    def append(self, name: str, raw: bytes, flags: list[str], internaldate: datetime | None) -> str | None:
        """Returns the UID (APPENDUID) if available."""
        M = self._m()
        fl = "(" + " ".join(flags) + ")" if flags else None
        dt = imaplib.Time2Internaldate(internaldate) if internaldate else None
        try:
            typ, data = M.append(quote(name), fl, dt, raw)
        except Exception as e:
            raise wrap_exc(e) from e
        if typ != "OK":
            t = _text(data)
            raise ImapError(f"APPEND: {typ} {t}", classify_response(t))
        m = re.search(r"APPENDUID\s+\d+\s+(\d+)", _text(data) + " " + _text(M.untagged_responses.get("APPENDUID", [])))
        return m.group(1) if m else None

    def search_header(self, name: str, header: str, value: str) -> list[str]:
        if self.selected != name:
            self.select(name, readonly=True)
        M = self._m()
        try:
            if value.isascii():
                typ, data = M.uid("SEARCH", "HEADER", header, quote(value))
            else:
                M.literal = value.encode("utf-8")
                typ, data = M.uid("SEARCH", "CHARSET", "UTF-8", "HEADER", header)
        except Exception as e:
            raise wrap_exc(e) from e
        t = _text(self._check(typ, data, "SEARCH"))
        return [u for u in t.split() if u.isdigit()]

    def fetch_header_values(self, name: str, uids: list[str], header: str) -> dict[str, list[str]]:
        """{uid: [unfolded header values]} via UID FETCH BODY.PEEK[HEADER.FIELDS (header)]."""
        if not uids:
            return {}
        if self.selected != name:
            self.select(name, readonly=True)
        from email.parser import BytesHeaderParser
        out: dict[str, list[str]] = {}
        for i in range(0, len(uids), 200):
            chunk = uids[i:i + 200]
            try:
                typ, data = self._m().uid("FETCH", ",".join(chunk), f"(UID BODY.PEEK[HEADER.FIELDS ({header})])")
            except Exception as e:
                raise wrap_exc(e) from e
            self._check(typ, data, "FETCH")
            for d in data or []:
                if not (isinstance(d, tuple) and len(d) >= 2 and isinstance(d[1], bytes)):
                    continue
                m = re.search(rb"UID\s+(\d+)", d[0] or b"")
                if not m:
                    continue
                msg = BytesHeaderParser().parsebytes(d[1])
                vals = [re.sub(r"\s+", "", str(v)) for v in (msg.get_all(header) or [])]
                out.setdefault(m.group(1).decode(), []).extend(vals)
        return out

    def find_import_id(self, name: str, header: str, import_id: str) -> list[str]:
        """UIDs whose `header` header equals EXACTLY import_id. SEARCH HEADER is a
        substring search (`sha-f-12` would find `sha-f-123`): the candidates are re-read and compared.
        import_id is delimited (`<sha-key>`); the legacy undelimited form (`sha-key`) is also accepted."""
        bare = import_id.strip()
        if bare.startswith("<") and bare.endswith(">"):
            bare = bare[1:-1]
        wanted = {bare, f"<{bare}>"}
        cands = self.search_header(name, header, bare)
        if not cands:
            return []
        vals = self.fetch_header_values(name, cands, header)
        return [u for u in cands if any(v in wanted for v in vals.get(u, []))]

    def count_import_prefix(self, name: str, header: str, prefix: str) -> int:
        """Number of messages in the folder whose `header` header contains prefix (PST fingerprint + "-"):
        counts only our own messages, not the folder's pre-existing content."""
        return len(self.search_header(name, header, prefix))

    def fetch_raw(self, name: str, uid: str) -> bytes | None:
        if self.selected != name:
            self.select(name, readonly=True)
        try:
            typ, data = self._m().uid("FETCH", uid, "(BODY.PEEK[])")
        except Exception as e:
            raise wrap_exc(e) from e
        self._check(typ, data, "FETCH")
        for d in data or []:
            if isinstance(d, tuple) and len(d) >= 2 and isinstance(d[1], bytes):
                return d[1]
        return None

    def quota(self) -> dict | None:
        """{'used': bytes, 'limit': bytes} or None if unsupported."""
        if "QUOTA" not in self.caps:
            return None
        M = self._m()
        try:
            typ, data = M.getquotaroot("INBOX")
        except Exception:
            return None
        if typ != "OK":
            return None
        flat = []
        for part in data or []:
            if isinstance(part, list):
                flat.extend(part)
            else:
                flat.append(part)
        m = re.search(r"STORAGE\s+(\d+)\s+(\d+)", _text(flat), re.I)
        if not m:
            return {"used": None, "limit": None}
        return {"used": int(m.group(1)) * 1024, "limit": int(m.group(2)) * 1024}

    def noop(self) -> None:
        try:
            self._m().noop()
        except Exception as e:
            raise wrap_exc(e) from e


# ---------------------------------------------------------------- special folders
def find_special(boxes: list[Mailbox], special: str) -> str | None:
    flag = SPECIAL_FLAGS.get(special)
    if flag:
        for b in boxes:
            if any(f.lower() == flag.lower() for f in b.flags):
                return b.name
        if special == "junk":
            for b in boxes:
                if any(f.lower() == "\\spam" for f in b.flags):
                    return b.name
    names = {b.decoded.lower(): b.name for b in boxes}
    for cand in SPECIAL_NAMES.get(special, []):
        if cand.lower() in names:
            return names[cand.lower()]
    return None


def with_retry(fn, conn: ImapConn, retries: int = 5, base_delay: float = 1.0, should_stop=None):
    """Runs fn(conn) with reconnection + exponential backoff on temporary errors."""
    attempt = 0
    while True:
        try:
            return fn(conn)
        except Exception as e:
            err = wrap_exc(e)
            if err.kind != "temp" or attempt >= retries:
                raise err from e
            attempt += 1
            delay = min(60.0, base_delay * (2 ** (attempt - 1)))
            end = time.time() + delay
            while time.time() < end:
                if should_stop and should_stop():
                    raise err from e
                time.sleep(min(0.5, delay))
            try:
                conn.connect()
            except ImapError as ce:
                if ce.kind != "temp":
                    raise


def personal_prefix(conn: ImapConn) -> str:
    """Personal namespace prefix (e.g. "INBOX." on Courier/Cyrus), otherwise ''."""
    if "NAMESPACE" not in conn.caps:
        return ""
    try:
        typ, data = conn._m().namespace()
    except Exception:
        return ""
    if typ != "OK":
        return ""
    m = re.match(r'\s*\(\("((?:[^"\\]|\\.)*)"', _text(data))
    return m.group(1) if m else ""
