"""Verification of a built message (re-parse, charset, attachments, cid)."""
from __future__ import annotations

import email.policy
from collections import Counter
from email.parser import BytesParser

from .message import html_cids

FFFD = "�"


def check_raw(raw: bytes, expect: dict | None) -> list[str]:
    errors: list[str] = []
    sep = raw.find(b"\r\n\r\n")
    if sep < 0:
        return ["no header/body separator"]
    header = raw[:sep + 2]
    if not header.isascii():
        errors.append("non-ASCII bytes in header block")
    if any(len(ln) > 998 for ln in raw.split(b"\r\n")):
        errors.append("line longer than 998 octets")
    if b"\nX-PST-Import-Id:" not in b"\n" + header:
        errors.append("missing X-PST-Import-Id")
    if b"\n" in raw.replace(b"\r\n", b""):
        errors.append("bare LF line ending")
    try:
        msg = BytesParser(policy=email.policy.default).parsebytes(raw)
    except Exception as ex:  # noqa: BLE001
        return errors + ["re-parse failed: %s" % ex]
    fffd = 0
    try:
        fffd += str(msg.get("subject", "")).count(FFFD)
    except Exception as ex:  # noqa: BLE001
        errors.append("subject undecodable: %s" % ex)
    atts: list[tuple] = []
    cids: set[str] = set()
    htmls: list[str] = []

    def walk(part, top=False):
        nonlocal fffd
        ct = part.get_content_type()
        if ct == "message/rfc822":
            atts.append((part.get_filename(), None, "embedded"))
            return
        if part.is_multipart():
            for sp in part.iter_parts():
                walk(sp)
            return
        disp = part.get_content_disposition()
        fname = part.get_filename()
        cid = part.get("Content-ID")
        if cid:
            cids.add(str(cid).strip().strip("<>").lower())
        if ct in ("text/plain", "text/html") and disp is None and fname is None and not cid:
            data = part.get_payload(decode=True) or b""
            cs = part.get_content_charset() or "us-ascii"
            try:
                txt = data.decode(cs)
            except (UnicodeDecodeError, LookupError):
                errors.append("%s body not decodable as %s" % (ct, cs))
                return
            fffd += txt.count(FFFD)
            if ct == "text/html":
                htmls.append(txt)
            return
        data = part.get_payload(decode=True) or b""
        if fname:
            fffd += fname.count(FFFD)
        atts.append((fname, len(data), "file"))

    walk(msg, True)
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
    """Blocking errors for a BuiltMessage (empty list = OK)."""
    expect = getattr(built, "_expect", None)
    if expect is None and handle is not None:
        owner = getattr(handle, "pst", None)
        if owner is not None:
            try:
                expect = owner._expectations(handle)
            except Exception as ex:  # noqa: BLE001
                return ["cannot read source for verification: %s" % ex]
    errors = check_raw(built.raw, expect)
    if built.size != len(built.raw):
        errors.append("size mismatch")
    if expect is not None and built.attachments != len(expect.get("attachments", [])):
        errors.append("attachments counter mismatch")
    return errors
