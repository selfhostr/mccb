"""Verification of a built message (re-parse, charset, attachments, cid).

check_raw() splits the MIME structure itself (bytes.find on the boundaries, C speed) and parses only
the header block of each part with the email package; leaf bodies are decoded with binascii (C). The
full pure-Python email re-parse (feedparser: one Python iteration per line) is kept as a fallback for
any structure the fast walker does not recognise, so the checks and their results are unchanged.
"""
from __future__ import annotations

import binascii
import email.policy
from collections import Counter
from email.parser import BytesParser

from .message import html_cids

FFFD = "�"
_POLICY = email.policy.default
_HEADER_PARSER = BytesParser(policy=_POLICY)


class _Unrecognised(Exception):
    """Structure not handled by the fast walker: use the full email parser."""


# ------------------------------------------------------------------ leaf model shared by both walkers
class _Leaf:
    """What the checks need from a MIME part: header accessors + payload."""
    __slots__ = ("h", "body", "container")

    def __init__(self, h, body: bytes | None, container: bool = False):
        self.h = h                  # email.message.EmailMessage (headers; payload only with the full walker)
        self.body = body            # raw (still transfer-encoded) body bytes, fast walker only
        self.container = container  # multipart container: nothing to check itself


def _decode_body(h, body: bytes) -> bytes:
    """= EmailMessage.get_payload(decode=True) for a non-multipart part (our own, well-formed output)."""
    cte = str(h.get("content-transfer-encoding", "")).lower()
    if cte == "base64":
        return binascii.a2b_base64(body)
    if cte == "quoted-printable":
        return binascii.a2b_qp(body)
    if cte in ("x-uuencode", "uuencode", "uue", "x-uue"):
        raise _Unrecognised("uuencode")
    return body


def _parse_headers(block: bytes):
    return _HEADER_PARSER.parsebytes(block + b"\r\n", headersonly=True)


def _fast_parts(data: bytes, depth: int = 0):
    """Yields (headers, body bytes or None for multipart/message containers) in document order,
    with the same part boundaries as email.feedparser for our generated messages."""
    if depth > 50:
        raise _Unrecognised("too deep")
    if data.startswith(b"\r\n"):
        raise _Unrecognised("part without headers")
    sep = data.find(b"\r\n\r\n")
    if sep < 0:
        raise _Unrecognised("no header/body separator in part")
    h = _parse_headers(data[:sep + 2])
    body = data[sep + 4:]
    if h.get_content_maintype() != "multipart":
        yield h, body
        return
    boundary = h.get_boundary()
    if not boundary:
        raise _Unrecognised("multipart without boundary")
    delim = b"\r\n--" + boundary.encode("ascii", "surrogateescape")
    text = b"\r\n" + body
    pos = text.find(delim)
    if pos < 0:
        raise _Unrecognised("no first boundary")
    yield h, None
    while True:
        after = pos + len(delim)
        if text.startswith(b"--", after):
            return  # close delimiter: the epilogue is ignored
        eol = text.find(b"\r\n", after)
        if eol < 0 or text[after:eol].strip(b" \t"):
            raise _Unrecognised("malformed boundary line")
        start = eol + 2
        nxt = start
        while True:
            nxt = text.find(delim, nxt)
            if nxt < 0:
                raise _Unrecognised("missing close boundary")
            tail = text[nxt + len(delim):nxt + len(delim) + 2]
            # a boundary line is "--b" followed by "--", blanks or CRLF (feedparser's boundary regex)
            if tail[:2] == b"--" or tail[:1] in (b"\r", b" ", b"\t"):
                break
            nxt += 1
        yield from _fast_parts(text[start:nxt], depth + 1)
        pos = nxt


def _walk_fast(raw: bytes):
    """(top-level headers, [_Leaf] in document order)."""
    top = None
    out = []
    for h, body in _fast_parts(raw):
        if top is None:
            top = h
        out.append(_Leaf(h, body, container=body is None))
    return top, out


def _walk_full(raw: bytes):
    msg = BytesParser(policy=_POLICY).parsebytes(raw)
    out = []

    def walk(part):
        if part.get_content_type() == "message/rfc822":
            out.append(_Leaf(part, None))
            return
        if part.is_multipart():
            out.append(_Leaf(part, None, container=True))
            for sp in part.iter_parts():
                walk(sp)
            return
        out.append(_Leaf(part, None))

    walk(msg)
    return msg, out


# ------------------------------------------------------------------ checks
def check_raw(raw: bytes, expect: dict | None) -> list[str]:
    errors: list[str] = []
    sep = raw.find(b"\r\n\r\n")
    if sep < 0:
        return ["no header/body separator"]
    header = raw[:sep + 2]
    if not header.isascii():
        errors.append("non-ASCII bytes in header block")
    if max(map(len, raw.split(b"\r\n"))) > 998:
        errors.append("line longer than 998 octets")
    if b"\nX-PST-Import-Id:" not in b"\n" + header:
        errors.append("missing X-PST-Import-Id")
    # bare LF <=> some "\n" not preceded by "\r" (= b"\n" in raw.replace(b"\r\n", b""), without the copy)
    if raw.count(b"\n") != raw.count(b"\r\n"):
        errors.append("bare LF line ending")
    fast = True
    try:
        msg, leaves = _walk_fast(raw)
    except _Unrecognised:
        fast = False
    except Exception:  # noqa: BLE001 - let the full parser report it
        fast = False
    if not fast:
        try:
            msg, leaves = _walk_full(raw)
        except Exception as ex:  # noqa: BLE001
            return errors + ["re-parse failed: %s" % ex]
    try:
        return _check_parts(errors, msg, leaves, expect, fast)
    except _Unrecognised:
        msg, leaves = _walk_full(raw)
        return _check_parts(errors, msg, leaves, expect, False)


def _check_parts(errors: list[str], msg, leaves, expect: dict | None, fast: bool) -> list[str]:
    errors = list(errors)
    fffd = 0
    try:
        fffd += str(msg.get("subject", "")).count(FFFD)
    except Exception as ex:  # noqa: BLE001
        errors.append("subject undecodable: %s" % ex)
    atts: list[tuple] = []
    cids: set[str] = set()
    htmls: list[str] = []
    # neither walker descends into message/rfc822: the embedded message counts as one attachment
    for leaf in leaves:
        part = leaf.h
        ct = part.get_content_type()
        if ct == "message/rfc822":
            atts.append((part.get_filename(), None, "embedded"))
            continue
        if leaf.container:
            continue
        disp = part.get_content_disposition()
        fname = part.get_filename()
        cid = part.get("Content-ID")
        if cid:
            cids.add(str(cid).strip().strip("<>").lower())

        def payload() -> bytes:
            if fast:
                return _decode_body(part, leaf.body)
            return part.get_payload(decode=True) or b""

        if ct in ("text/plain", "text/html") and disp is None and fname is None and not cid:
            data = payload()
            cs = part.get_content_charset() or "us-ascii"
            try:
                txt = data.decode(cs)
            except (UnicodeDecodeError, LookupError):
                errors.append("%s body not decodable as %s" % (ct, cs))
                continue
            fffd += txt.count(FFFD)
            if ct == "text/html":
                htmls.append(txt)
            continue
        data = payload()
        if fname:
            fffd += fname.count(FFFD)
        atts.append((fname, len(data), "file"))
    if expect is None:
        return errors
    if fffd > expect.get("fffd", 0):
        errors.append("U+FFFD introduced (%d > %d in source)" % (fffd, expect.get("fffd", 0)))
    exp_atts = expect.get("attachments", [])
    if len(atts) != len(exp_atts):
        errors.append("attachment count %d != source %d" % (len(atts), len(exp_atts)))
    exp_sizes = Counter((n, s) for n, s, k in exp_atts if k != "embedded")
    got_sizes = Counter((n, s) for n, s, k in atts if k != "embedded")
    if exp_sizes != got_sizes:
        missing = exp_sizes - got_sizes
        extra = got_sizes - exp_sizes
        errors.append("attachment names/sizes differ: missing=%s extra=%s" % (
            sorted(missing)[:3], sorted(extra)[:3]))
    unresolved = expect.get("cid_unresolved", set())
    for h in htmls:
        for ref in html_cids(h):
            if ref.lower() not in cids and ref.lower() not in unresolved:
                errors.append("cid not resolved: %s" % ref)
    return errors


def verify_message(built, handle=None) -> list[str]:
    """Blocking errors for a BuiltMessage (empty list = OK).

    The expectations (`_expect`) come from the builder's own parts: the source item is not re-read.
    When build_raw already ran check_raw on this exact raw (`_checked`), its result is reused."""
    expect = getattr(built, "_expect", None)
    if expect is None and handle is not None:
        owner = getattr(handle, "pst", None)
        if owner is not None:
            try:
                expect = owner._expectations(handle)
            except Exception as ex:  # noqa: BLE001
                return ["cannot read source for verification: %s" % ex]
    pre = getattr(built, "_checked", None)
    if pre is not None and pre[0] is built.raw:
        errors = list(pre[1])
    else:
        errors = check_raw(built.raw, expect)
    if built.size != len(built.raw):
        errors.append("size mismatch")
    if expect is not None and built.attachments != len(expect.get("attachments", [])):
        errors.append("attachments counter mismatch")
    return errors
