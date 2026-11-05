"""PST message -> RFC 5322 (UTF-8 clean, QP/base64, RFC 2047/2231)."""
from __future__ import annotations

import base64
import binascii
import email.header
import email.policy
import email.utils
import hashlib
import html as htmlmod
import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.generator import BytesGenerator
from email.message import MIMEPart
from urllib.parse import unquote

from . import mapi
from .charset import decode_bytes, decode_html, fix_mojibake, force_html_utf8, normalize
from .detect import detect, extension_for
from .mapi import PT_UNICODE, decode_utf16
from .rtf import decompress, rtf_to_html, rtf_to_text

POLICY = email.policy.default.clone(linesep="\r\n", max_line_length=78)
FOLD_POLICY = email.policy.SMTP.clone(max_line_length=78)
MAX_DEPTH = 6

_NON_MAIL = ("ipm.contact", "ipm.distlist", "ipm.appointment", "ipm.stickynote", "ipm.activity",
             "ipm.abchperson", "ipm.microsoft.", "ipm.configuration", "ipm.network",
             "ipm.aggregation", "ipm.conversationaction", "ipm.extendedrule", "ipm.rule",
             "ipm.olemessage", "ipm.document", "ipm.skypeteams")


def message_class(bag) -> str:
    return (bag.string(mapi.PR_MESSAGE_CLASS) or "").strip()


def is_mail_class(cls: str) -> bool:
    c = (cls or "").lower()
    if c == "ipm.task" or c.startswith("ipm.task."):
        return False
    return not c.startswith(_NON_MAIL)


class BuildError(Exception):
    pass


@dataclass
class _Att:
    name: str
    mime: str
    data: bytes | None = None       # by-value bytes
    raw_message: bytes | None = None  # embedded message (method 5), built recursively
    cid: str | None = None
    location: str | None = None
    hidden: bool = False
    inline_flag: bool = False
    related: bool = False
    kind: str = "file"              # file | embedded | ole


@dataclass
class _Parts:
    subject: str = ""
    text: str | None = None
    html: str | None = None
    atts: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    source_fffd: int = 0
    cid_unresolved: set = field(default_factory=set)


# ---------------------------------------------------------------------------
# bodies
def clean_subject(s: str | None) -> str:
    if not s:
        return ""
    if s[0] == "\x01" and len(s) >= 2:   # MAPI subject prefix marker
        s = s[2:]
    return s.replace("\r", " ").replace("\n", " ").strip()


def extract_bodies(bag, warnings: list) -> tuple[str | None, str | None]:
    text = html = None
    icp = bag.int(mapi.PR_INTERNET_CPID)
    mcp = bag.int(mapi.PR_MESSAGE_CODEPAGE)
    hraw = bag.raw(mapi.PR_HTML)
    if hraw:
        if bag.vtype(mapi.PR_HTML) == PT_UNICODE:
            html = decode_utf16(hraw)
        else:
            html, _cs = decode_html(bytes(hraw).rstrip(b"\x00"), icp or mcp, bag.codepage)
        if not html.strip():
            html = None
    braw = bag.raw(mapi.PR_BODY)
    if braw:
        vt = bag.vtype(mapi.PR_BODY)
        if vt == PT_UNICODE:
            text = decode_utf16(braw)
        else:
            from .charset import codec_for_cpid
            text = decode_bytes(bytes(braw).split(b"\x00", 1)[0],
                                [bag.codepage, codec_for_cpid(icp), bag.default_codepage])
        if not text.strip():
            text = None
    if text is not None:
        text = text.replace("\x00", "")
    if html is not None:
        html = html.replace("\x00", "")
    if html is None:
        rtfc = bag.raw(mapi.PR_RTF_COMPRESSED)
        if rtfc:
            try:
                rtf = decompress(bytes(rtfc))
                h = rtf_to_html(rtf, bag.codepage)
                if h and h.strip():
                    html = h
                    warnings.append("rtf-converted")
                elif text is None:
                    t = rtf_to_text(rtf, bag.codepage)
                    if t.strip():
                        text = t
                        warnings.append("rtf-converted")
            except Exception as ex:  # noqa: BLE001
                warnings.append("rtf-unreadable:%s" % ex)
    return text, html


_TAG_BLOCK = re.compile(r"<(script|style|head)\b.*?</\1\s*>", re.I | re.S)
_TAG_BR = re.compile(r"<(br|/p|/div|/tr|/h[1-6]|/li|/table)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")


def html_to_text(h: str) -> str:
    h = _TAG_BLOCK.sub("", h)
    h = re.sub(r"<!--.*?-->", "", h, flags=re.S)
    h = _TAG_BR.sub("\n", h)
    h = _TAG.sub("", h)
    h = htmlmod.unescape(h)
    h = re.sub(r"[ \t\r\f\v]+", " ", h)
    h = re.sub(r"\n\s*\n\s*\n+", "\n\n", h)
    return h.strip() + "\n"


# Word "Save as HTML" companions referenced from mail HTML but never shipped as attachments: not a defect
WORD_ARTEFACTS = frozenset({"filelist.xml", "editdata.mso", "oledata.mso", "themedata.thmx",
                            "colorschememapping.xml", "header.htm"})


def is_word_artefact(ref: str) -> bool:
    name = ref.split("@", 1)[0].replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    return name in WORD_ARTEFACTS


_CID_RE = re.compile(r"""cid:([^"'\s>)]+)""", re.I)


def html_cids(h: str | None) -> list[str]:
    if not h:
        return []
    seen, out = set(), []
    for m in _CID_RE.finditer(h):
        c = unquote(m.group(1)).strip().strip("<>")
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# addresses / headers
_ADDR_HEADERS = {"from", "to", "cc", "bcc", "reply-to", "sender", "resent-from", "resent-to",
                 "resent-cc", "resent-sender", "disposition-notification-to", "return-receipt-to"}
_DROP_HEADERS = {"content-type", "content-transfer-encoding", "mime-version", "content-disposition",
                 "content-id", "content-length", "content-description", "content-location",
                 "x-pst-import-id", "lines", "content-class"}
_HNAME = re.compile(r"^[!-9;-~]+$")


def parse_transport_headers(s: str | None) -> list[list[str]]:
    if not s:
        return []
    out: list[list[str]] = []
    for line in s.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip():
            if out:
                break
            continue
        if line[0] in " \t":
            if out:
                out[-1][1] += " " + line.strip()
            continue
        if ":" not in line:
            continue
        name, val = line.split(":", 1)
        name = name.strip()
        if not _HNAME.match(name):
            continue
        out.append([name, val.strip()])
    return out


_EW = re.compile(r"=\?([^?\s]+)\?([QqBb])\?([^?]*)\?=")


def _ew_bytes(enc: str, payload: str) -> bytes:
    if enc in "Bb":
        p = re.sub(r"[^A-Za-z0-9+/]", "", payload)
        p = p[:len(p) - len(p) % 4] if len(p) % 4 == 1 else p + "=" * (-len(p) % 4)
        try:
            return base64.b64decode(p)
        except (binascii.Error, ValueError):
            return payload.encode("ascii", "replace")
    raw = payload.replace("_", " ").encode("latin-1", "replace")
    return re.sub(rb"=([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw)


def _decode_plain(seg: str) -> str:
    if "\x1b" in seg and seg.isascii():
        # raw ISO-2022-JP/KR (7-bit, stateful) sent without encoded words
        return decode_bytes(seg.encode("ascii"), [])
    return seg


def decode_header_value(v: str) -> str:
    """RFC 2047 decode with charset repair; never introduces U+FFFD.

    Unencoded text is kept as is (email.header.decode_header would turn
    non-Latin-1 characters into ``\\uXXXX`` escapes); adjacent encoded words
    in the same charset are joined before decoding (a multi-byte or stateful
    sequence may be split across them); whitespace between encoded words is
    dropped (RFC 2047 §6.2)."""
    out: list[str] = []
    pend_cs, pend = None, bytearray()

    def flush():
        nonlocal pend_cs
        if pend_cs is not None:
            out.append(decode_bytes(bytes(pend), [normalize(pend_cs), "utf-8", "cp1252"]))
            pend.clear()
            pend_cs = None

    pos = 0
    prev_ew = False
    for m in _EW.finditer(v):
        gap = v[pos:m.start()]
        if gap and not (prev_ew and not gap.strip()):
            flush()
            out.append(_decode_plain(gap))
        cs = m.group(1).split("*", 1)[0].lower()
        if pend_cs is not None and cs != pend_cs:
            flush()
        pend_cs = cs
        pend += _ew_bytes(m.group(2), m.group(3))
        pos = m.end()
        prev_ew = True
    flush()
    if pos < len(v):
        out.append(_decode_plain(v[pos:]))
    v = fix_mojibake("".join(out))
    return re.sub(r"[\r\n]+", " ", v).strip()


_MAX_TOKEN = 900
_ID_HEADERS = ("message-id", "in-reply-to", "references", "content-id", "resent-message-id")


def _fold_ascii(name: str, value: str) -> str:
    if any(len(t) > _MAX_TOKEN for t in value.split()):
        if name.lower() in _ID_HEADERS:
            # absurd id token: drop it (an id that long is unusable anyway)
            value = " ".join(t for t in value.split() if len(t) <= _MAX_TOKEN)
            if not value:
                return ""
        else:
            h = email.header.Header(value, "utf-8", header_name=name, maxlinelen=76)
            return "%s: %s\r\n" % (name, h.encode(linesep="\r\n"))
    line = "%s: %s" % (name, value)
    if len(line) <= 78:
        return line + "\r\n"
    words = re.split(r"(?<=\S)(?=\s)", line)
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + len(w) > 78:
            lines.append(cur)
            cur = w if w[:1] in (" ", "\t") else " " + w
        else:
            cur += w
    if cur:
        lines.append(cur)
    return "\r\n".join(lines) + "\r\n"


def _fold_unicode(name: str, value: str) -> str:
    try:
        folded = FOLD_POLICY.fold(name, value)
        folded.encode("ascii")
        return folded
    except Exception:  # noqa: BLE001
        h = email.header.Header(value, "utf-8", header_name=name, maxlinelen=78)
        return "%s: %s\r\n" % (name, h.encode(linesep="\r\n"))


def header_line(name: str, raw: str | None, decoded: str | None = None) -> str:
    if raw is not None and raw.isascii() and "=?" not in raw and "\x1b" not in raw:
        return _fold_ascii(name, raw)
    if decoded is not None:
        value = decoded
    elif name.lower() in _ADDR_HEADERS:
        value = decode_address_header(raw or "")
    else:
        value = decode_header_value(raw or "")
    if value.isascii():
        return _fold_ascii(name, value)
    return _fold_unicode(name, value)


def decode_address_header(raw: str) -> str:
    """Decode the display names of an address list one by one.

    Decoding the whole header first would expose a comma (or ``<``, ``"``...)
    hidden inside an encoded word ("=?koi8-r?Q?...=2C...?=") and split one
    address into two; re-quoting each decoded name keeps the structure."""
    if "=?" not in raw and "\x1b" not in raw:
        return decode_header_value(raw)
    words: list[str] = []

    def hide(m):
        words.append(m.group(0))
        return "xEWx%dx" % (len(words) - 1)
    # encoded words may (illegally but commonly) carry specials such as ":" or ","
    masked = _EW.sub(hide, raw)
    try:
        pairs = email.utils.getaddresses([masked])
    except Exception:  # noqa: BLE001
        pairs = []
    if not pairs or any(not a or "@" not in a or "xEWx" in a for _n, a in pairs):
        return decode_header_value(raw)
    unhide = re.compile(r"xEWx(\d+)x")
    return ", ".join(format_addr(decode_header_value(unhide.sub(lambda m: words[int(m.group(1))], n)), a)
                     for n, a in pairs)


_SPECIALS = re.compile(r'[()<>\[\]:;@\\,."]')


def format_addr(name: str | None, addr: str | None) -> str:
    name = (name or "").strip().strip("'\"").strip()
    addr = (addr or "").strip()
    if name and addr and name.lower() == addr.lower():
        name = ""
    if not addr:
        addr = "unknown@unknown.invalid"
    if not name:
        return "<%s>" % addr if " " in addr else addr
    if _SPECIALS.search(name):
        name = '"%s"' % name.replace("\\", "\\\\").replace('"', '\\"')
    return "%s <%s>" % (name, addr)


def _smtp(addrtype, email_addr, smtp):
    addrtype = (addrtype or "").upper()
    email_addr = (email_addr or "").strip()
    if smtp and "@" in smtp:
        return smtp.strip()
    if addrtype in ("SMTP", "") and "@" in email_addr:
        return email_addr
    if "@" in email_addr and addrtype != "EX":
        return email_addr
    return None


def sender_of(bag):
    name = bag.string(mapi.PR_SENDER_NAME) or bag.string(mapi.PR_SENT_REPRESENTING_NAME)
    addr = _smtp(bag.string(mapi.PR_SENDER_ADDRTYPE), bag.string(mapi.PR_SENDER_EMAIL),
                 bag.string(mapi.PR_SENDER_SMTP_ADDRESS))
    if not addr:
        addr = _smtp(bag.string(mapi.PR_SENT_REPRESENTING_ADDRTYPE),
                     bag.string(mapi.PR_SENT_REPRESENTING_EMAIL),
                     bag.string(mapi.PR_SENT_REPRESENTING_SMTP_ADDRESS))
    if not addr:
        c = bag.string(0x5D0A)  # PidTagCreatorSmtpAddress
        if c and "@" in c:
            addr = c.strip()
    if not name and not addr:
        return None
    return name, addr


def recipient_of(rb):
    name = rb.string(mapi.PR_DISPLAY_NAME)
    addr = _smtp(rb.string(mapi.PR_ADDRTYPE), rb.string(mapi.PR_EMAIL_ADDRESS),
                 rb.string(mapi.PR_SMTP_ADDRESS))
    if not addr:
        # PidTagPrimarySmtpAddress-like fallbacks
        for t in (0x39FE, 0x3A56):
            s = rb.string(t)
            if s and "@" in s:
                addr = s
                break
    rtype = rb.int(mapi.PR_RECIPIENT_TYPE, 1) or 1
    return name, addr, rtype & 0x0F


def message_flags(bag) -> list[str]:
    flags = []
    mf = bag.int(mapi.PR_MESSAGE_FLAGS, 0) or 0
    if mf & 0x01:
        flags.append("\\Seen")
    if mf & 0x08:
        flags.append("\\Draft")
    if (bag.int(mapi.PR_FLAG_STATUS, 0) or 0) == 2:
        flags.append("\\Flagged")
    verb = bag.int(mapi.PR_LAST_VERB_EXECUTED, 0) or 0
    icon = bag.int(mapi.PR_ICON_INDEX, 0) or 0
    if verb in (102, 103) or icon in (0x105, 0x106):
        flags.append("\\Answered")
    if verb == 104 or icon == 0x107:
        flags.append("$Forwarded")
    return flags


def message_dates(bag):
    for t in (mapi.PR_MESSAGE_DELIVERY_TIME, mapi.PR_CLIENT_SUBMIT_TIME, mapi.PR_CREATION_TIME,
              mapi.PR_LAST_MODIFICATION_TIME):
        d = bag.time(t)
        if d:
            return d
    return None


# ---------------------------------------------------------------------------
_B64_LINE = 76  # = email.contentmanager._encode_base64(max_line_length=78): 57 bytes per line


def _b64_lines(data: bytes) -> bytes:
    """Byte-identical to email.contentmanager._encode_base64(data, 78), encoded in one C call."""
    if not data:
        return b""
    enc = binascii.b2a_base64(data, newline=False)
    n = _B64_LINE
    return b"\n".join([enc[i:i + n] for i in range(0, len(enc), n)]) + b"\n"


class _FastGenerator(BytesGenerator):
    """BytesGenerator writing a payload in one call instead of one write per line (same output:
    every \\r\\n, \\r or \\n becomes the policy line separator, like Generator._write_lines)."""

    def _write_lines(self, lines):
        if not lines:
            return
        self.write(lines.replace("\r\n", "\n").replace("\r", "\n").replace("\n", self._NL))


class MessageBuilder:
    def __init__(self, import_id: str):
        self.import_id = import_id

    # attachments ---------------------------------------------------------
    def collect_attachments(self, src, parts: _Parts, depth: int):
        n = 0
        for att in src.attachments():
            n += 1
            if isinstance(att, Exception):
                parts.warnings.append("attachment-unreadable:#%d:%s" % (n, att))
                continue
            b = att.bag
            method = b.int(mapi.PR_ATTACH_METHOD, 1)
            if method is None:
                method = 1
            name = (b.string(mapi.PR_ATTACH_LONG_FILENAME) or b.string(mapi.PR_ATTACH_FILENAME)
                    or b.string(mapi.PR_DISPLAY_NAME) or "").strip()
            name = re.sub(r"[\x00-\x1f\x7f]", "", name).replace("/", "_").replace("\\", "_")
            cid = (b.string(mapi.PR_ATTACH_CONTENT_ID) or "").strip().strip("<>").strip() or None
            loc = (b.string(mapi.PR_ATTACH_CONTENT_LOCATION) or "").strip() or None
            hidden = b.bool(mapi.PR_ATTACHMENT_HIDDEN)
            aflags = b.int(mapi.PR_ATTACH_FLAGS, 0) or 0
            if method == 0:
                continue
            if method == 5:
                try:
                    if depth >= MAX_DEPTH:
                        raise BuildError("embedding too deep")
                    inner_src = att.embedded()
                    inner = self.build_raw(inner_src, depth + 1)
                    subj = clean_subject(inner_src.bag.string(mapi.PR_SUBJECT))
                    base = subj or name or "message"
                    base = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", base)[:120].strip() or "message"
                    if not base.lower().endswith(".eml"):
                        base += ".eml"
                    parts.atts.append(_Att(name=base, mime="message/rfc822", raw_message=inner,
                                           kind="embedded", hidden=hidden))
                    parts.warnings.extend("embedded:" + w for w in inner.warnings)
                except Exception as ex:  # noqa: BLE001
                    parts.warnings.append("embedded-unreadable:%s:%s" % (name or "#%d" % n, ex))
                continue
            if method in (2, 3, 4, 7):
                parts.warnings.append("attachment-reference:%s" % (name or "#%d" % n))
                continue
            try:
                data = att.read_data()
            except Exception as ex:  # noqa: BLE001
                parts.warnings.append("attachment-unreadable:%s:%s" % (name or "#%d" % n, ex))
                continue
            mime = detect(data, name, b.string(mapi.PR_ATTACH_MIME_TAG))
            if mime.startswith("multipart/"):
                mime = "application/octet-stream"
            if not name:
                stem = re.sub(r"[^\w.-]", "_", cid.split("@")[0]) if cid else "attachment-%d" % n
                ext = extension_for(mime)
                name = stem if stem.lower().endswith(ext) else stem + ext
            kind = "ole" if method == 6 else "file"
            if kind == "ole":
                parts.warnings.append("ole-attachment:%s" % name)
            parts.source_fffd += name.count("�")
            parts.atts.append(_Att(name=name, mime=mime, data=data, cid=cid, location=loc,
                                   hidden=hidden, inline_flag=bool(aflags & 0x4), kind=kind))

    def resolve_cids(self, parts: _Parts):
        refs = html_cids(parts.html)
        if not refs:
            return
        by_cid = {}
        for a in parts.atts:
            if a.cid and a.data is not None:
                by_cid.setdefault(a.cid.lower(), a)
        for ref in refs:
            a = by_cid.get(ref.lower())
            if a is None:
                local = ref.split("@", 1)[0].lower()
                for cand in parts.atts:
                    if cand.data is None or cand.related:
                        continue
                    nm = cand.name.lower()
                    if nm == local or nm == ref.lower() or (cand.location or "").lower() in (local, ref.lower()):
                        a = cand
                        break
                if a is not None:
                    if a.cid and a.cid.lower() != ref.lower():
                        # one attachment referenced under two ids: keep html in sync
                        parts.html = re.sub("cid:" + re.escape(ref), "cid:" + a.cid, parts.html, flags=re.I)
                    else:
                        a.cid = ref
                    by_cid[ref.lower()] = a
            if a is None:
                parts.cid_unresolved.add(ref)
                if not is_word_artefact(ref):
                    parts.warnings.append("cid-unresolved:%s" % ref)
                continue
            a.related = True
        for a in parts.atts:
            if (not a.related and a.data is not None and (a.hidden or a.inline_flag) and a.cid
                    and a.mime.startswith("image/")):
                a.related = True

    # MIME ----------------------------------------------------------------
    @staticmethod
    def _text_part(s: str, subtype: str) -> MIMEPart:
        p = MIMEPart(policy=POLICY)
        # count of non-ASCII characters, computed in C (a per-character loop was a hotspot on big bodies)
        non_ascii = 0 if s.isascii() else len(s) - len(s.encode("ascii", "ignore"))
        cte = "base64" if non_ascii > len(s) * 0.3 else "quoted-printable"
        p.set_content(s, subtype=subtype, charset="utf-8", cte=cte)
        return p

    @staticmethod
    def _multipart(subtype: str, parts: list, **params) -> MIMEPart:
        m = MIMEPart(policy=POLICY)
        m["Content-Type"] = "multipart/" + subtype
        for k, v in params.items():
            m.set_param(k, v)
        # boundary chosen here: otherwise the generator regex-searches the whole flattened subtree for a
        # collision, once per nesting level (superlinear on big attachments). 128 random bits cannot collide
        # with content in practice; check_raw re-validates the structure anyway.
        m.set_boundary("=_%s" % uuid.uuid4().hex)
        m.set_payload(list(parts))
        return m

    @staticmethod
    def _att_part(a: _Att, inline: bool) -> MIMEPart:
        p = MIMEPart(policy=POLICY)
        disp = "inline" if inline else "attachment"
        if a.raw_message is not None:
            p["Content-Type"] = "message/rfc822"
            p.set_param("name", a.name)
            p["Content-Disposition"] = disp
            p.set_param("filename", a.name, header="Content-Disposition")
            p.set_payload(a.raw_message.raw.decode("ascii"))
            return p
        maintype, subtype = a.mime.split("/", 1)
        if maintype == "message":
            maintype, subtype = "application", "octet-stream"
        kw = {}
        if a.cid:
            kw["cid"] = "<%s>" % re.sub(r"[\s<>]", "", a.cid)
        # same headers and bytes as set_content(cte="base64"), with the base64 body encoded in C
        # (email.contentmanager._encode_base64 loops in Python over 57-byte chunks)
        p.set_content(_b64_lines(a.data), maintype=maintype, subtype=subtype, disposition=disp,
                      filename=a.name, params={"name": a.name}, cte="7bit", **kw)
        p.replace_header("Content-Transfer-Encoding", "base64")
        return p

    def mime_tree(self, parts: _Parts, simple: bool = False) -> MIMEPart:
        if simple:
            text = parts.text or (html_to_text(parts.html) if parts.html else "")
            body = self._text_part(text, "plain")
            atts = [self._att_part(a, False) for a in parts.atts]
            return self._multipart("mixed", [body] + atts) if atts else body
        related = [a for a in parts.atts if a.related] if parts.html else []
        others = [a for a in parts.atts if a not in related]
        if parts.html is not None:
            hp = self._text_part(force_html_utf8(parts.html), "html")
            if related:
                hp = self._multipart("related", [hp] + [self._att_part(a, True) for a in related],
                                     type="text/html")
            body = self._multipart("alternative", [self._text_part(parts.text, "plain"), hp]) \
                if parts.text else hp
        else:
            body = self._text_part(parts.text or "", "plain")
        if others:
            return self._multipart("mixed", [body] + [self._att_part(a, False) for a in others])
        return body

    @staticmethod
    def serialize(root: MIMEPart) -> bytes:
        buf = io.BytesIO()
        _FastGenerator(buf, policy=POLICY, mangle_from_=False).flatten(root)
        return buf.getvalue()

    # headers --------------------------------------------------------------
    def header_block(self, src, parts: _Parts, minimal: bool = False) -> str:
        bag = src.bag
        lines: list[str] = []
        present: set[str] = set()
        if not minimal:
            for name, raw in parse_transport_headers(bag.string(mapi.PR_TRANSPORT_MESSAGE_HEADERS)):
                low = name.lower()
                if low in _DROP_HEADERS or low.startswith("content-"):
                    continue
                if low == "subject":
                    present.add(low)
                    lines.append(("Subject", None))  # placeholder, filled below
                    continue
                if low in _ADDR_HEADERS or low in ("date", "message-id", "in-reply-to", "references"):
                    if low in present and low in ("from", "date", "message-id", "subject", "to", "cc"):
                        continue  # duplicate singletons
                present.add(low)
                try:
                    lines.append(header_line(name, raw))
                except Exception:  # noqa: BLE001
                    lines.append(_fold_unicode(name, decode_header_value(raw)))
            hdr_subject = None
            for name, raw in parse_transport_headers(bag.string(mapi.PR_TRANSPORT_MESSAGE_HEADERS)):
                if name.lower() == "subject":
                    hdr_subject = decode_header_value(raw)
                    break
            if parts.subject and "�" in parts.subject and hdr_subject and "�" not in hdr_subject:
                parts.subject = hdr_subject
            if not parts.subject and hdr_subject:
                parts.subject = hdr_subject
        add = []
        if "from" not in present:
            s = sender_of(bag)
            if s:
                add.append(header_line("From", None, format_addr(*s)))
        rcpts = {1: [], 2: [], 3: []}
        if not ({"to", "cc"} & present):
            for rb in src.recipients():
                try:
                    name, addr, rtype = recipient_of(rb)
                except Exception:  # noqa: BLE001
                    continue
                if rtype in rcpts and (name or addr):
                    rcpts[rtype].append(format_addr(name, addr))
            for rtype, hname in ((1, "To"), (2, "Cc"), (3, "Bcc")):
                if rcpts[rtype]:
                    add.append(header_line(hname, None, ", ".join(rcpts[rtype])))
        if "date" not in present:
            d = bag.time(mapi.PR_CLIENT_SUBMIT_TIME) or message_dates(bag)
            if d:
                add.append("Date: %s\r\n" % email.utils.format_datetime(d.astimezone(timezone.utc)))
        if "message-id" not in present:
            mid = (bag.string(mapi.PR_INTERNET_MESSAGE_ID) or "").strip()
            if not mid or not mid.isascii() or " " in mid or len(mid) > _MAX_TOKEN:
                mid = "<%s@pst-import.invalid>" % hashlib.sha1(self.import_id.strip("<>").encode()).hexdigest()[:32]  # stable regardless of the id format
            add.append(_fold_ascii("Message-ID", mid))
        if "in-reply-to" not in present:
            v = (bag.string(mapi.PR_IN_REPLY_TO_ID) or "").strip()
            if v and v.isascii():
                add.append(_fold_ascii("In-Reply-To", v))
        if "references" not in present:
            v = (bag.string(mapi.PR_INTERNET_REFERENCES) or "").strip()
            if v and v.isascii():
                add.append(_fold_ascii("References", v))
        subj_line = header_line("Subject", None, parts.subject) if parts.subject else None
        out = [_fold_ascii("X-PST-Import-Id", self.import_id)]
        for ln in lines:
            if isinstance(ln, tuple):
                if subj_line:
                    out.append(subj_line)
                    subj_line = None
                continue
            out.append(ln)
        if subj_line:
            add.insert(0, subj_line)
        out.extend(add)
        out.append("MIME-Version: 1.0\r\n")
        return "".join(out)

    # entry points -----------------------------------------------------------
    def gather(self, src, depth: int) -> _Parts:
        parts = _Parts()
        bag = src.bag
        parts.subject = clean_subject(bag.string(mapi.PR_SUBJECT))
        parts.text, parts.html = extract_bodies(bag, parts.warnings)
        for s in (parts.subject, parts.text, parts.html):
            if s:
                parts.source_fffd += s.count("�")
        self.collect_attachments(src, parts, depth)
        self.resolve_cids(parts)
        return parts

    def assemble(self, src, parts: _Parts, simple=False, minimal=False) -> bytes:
        root = self.mime_tree(parts, simple=simple)
        head = self.header_block(src, parts, minimal=minimal)
        return head.encode("ascii") + self.serialize(root)

    def build_raw(self, src, depth: int = 0):
        """Build (rich, falling back to simple) and return a _Built."""
        from .verify import check_raw
        parts = self.gather(src, depth)
        expect = expectations(parts)
        errors = None
        try:
            raw = self.assemble(src, parts)
            errors = check_raw(raw, expect)
        except Exception as ex:  # noqa: BLE001
            errors = ["build failed: %s" % ex]
        checked = errors
        if errors:
            parts.warnings.append("fallback")
            parts.warnings.extend("rich-error:" + e for e in errors[:5])
            expect = expectations(parts, simple=True)
            checked = None  # the simple build is checked by verify_message
            try:
                raw = self.assemble(src, parts, simple=True)
            except Exception:  # noqa: BLE001
                raw = self.assemble(src, parts, simple=True, minimal=True)
        return _Built(raw=raw, parts=parts, expect=expect, checked=checked)


@dataclass
class _Built:
    raw: bytes
    parts: _Parts
    expect: dict
    # check_raw(raw, expect) result already computed for this exact raw (None = not checked yet)
    checked: list | None = None

    @property
    def warnings(self):
        return self.parts.warnings


def expectations(parts: _Parts, simple: bool = False) -> dict:
    return {
        "attachments": [(a.name, len(a.data) if a.data is not None else None, a.kind) for a in parts.atts],
        "fffd": parts.source_fffd,
        "cid_unresolved": {c.lower() for c in parts.cid_unresolved},
        "simple": simple,
    }


def header_date(headers: str | None) -> datetime | None:
    """Date of the transport headers (only that header is parsed), aware UTC; None if absent, unparsable
    or without a timezone."""
    if not headers:
        return None
    for name, raw in parse_transport_headers(headers):
        if name.lower() == "date":
            try:
                d = email.utils.parsedate_to_datetime(raw)
            except Exception:  # noqa: BLE001
                return None
            if d is None or d.tzinfo is None:
                return None
            try:
                return d.astimezone(timezone.utc)
            except Exception:  # noqa: BLE001
                return None
    return None


_MIN_DATE = datetime(1980, 1, 1, tzinfo=timezone.utc)


def mail_date(get_time, get_headers, now: datetime | None = None) -> datetime | None:
    """THE mail date (shared by INTERNALDATE and the newest-first sort key, so they cannot diverge).
    Date header of the transport headers, only if it carries a timezone and lies within
    [1980-01-01, (delivery time or now) + 2 days]; a header more than 30 days after the delivery time gives
    the delivery time; otherwise client submit > delivery > creation > last modification.
    ``get_time(tag)`` -> datetime | None, ``get_headers()`` -> transport headers text | None (both lazy)."""
    from datetime import timedelta
    try:
        d = header_date(get_headers())
    except Exception:  # noqa: BLE001
        d = None
    if d:
        delivery = get_time(mapi.PR_MESSAGE_DELIVERY_TIME)
        limit = (delivery or now or datetime.now(timezone.utc)) + timedelta(days=2)
        if _MIN_DATE <= d <= limit:
            return d
        if delivery and d > delivery + timedelta(days=30):
            return delivery
    for t in (mapi.PR_CLIENT_SUBMIT_TIME, mapi.PR_MESSAGE_DELIVERY_TIME, mapi.PR_CREATION_TIME,
              mapi.PR_LAST_MODIFICATION_TIME):
        d = get_time(t)
        if d:
            return d
    return None


NO_INTERNALDATE = datetime(1980, 1, 1, tzinfo=timezone.utc)


def internaldate_of(bag) -> datetime:
    return mail_date(bag.time, lambda: bag.string(mapi.PR_TRANSPORT_MESSAGE_HEADERS)) or NO_INTERNALDATE
