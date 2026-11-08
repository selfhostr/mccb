"""Repair of damaged messages already in a mailbox (no source file needed): analysis and repair of ONE message.

Damage this module knows, all left behind by other migration tools:
  replacement_chars  U+FFFD in a text part: accents lost on the way. Rebuilt word by word from the other
                     text part of the same message (the plain text often survives when the HTML is broken,
                     and the reverse).
  mojibake           UTF-8 read as Windows-1252 ("Ã©" for "é", "â€™" for "’"): decoded back. Subject too.
  attachment_names   Attachments with a placeholder name and no extension ("piece_jointe_3", "ATT00001"):
                     the real type is read from the content, the extension and MIME type are set.
  detached_images    The HTML shows cid:<id> images that the message no longer carries, while images without
                     a Content-ID sit in it as attachments: they are put back in line, in order.
  unreadable         The message cannot be parsed: reported, never changed.

Guarantees: a message is changed only through repair(), which returns None when nothing applies. The result is
re-parsed and checked (verify) before anyone writes it: same addresses, date and Message-ID, same attachments
byte for byte, no new U+FFFD. Forwarded messages (message/rfc822) are kept as they are. A repaired message
carries MARK, so a second pass leaves it alone, and the hash of the original, so a restore can find it.
"""
from __future__ import annotations

import hashlib
import html as htmlmod
import io
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

MARK = "X-MCCB-Repair"
LEGACY_MARKS = ("X-Repare-Import",)   # the repair script used before MCCB
ISSUES = ("replacement_chars", "mojibake", "attachment_names", "detached_images", "unreadable")
FFFD = "�"
OUT_POLICY = policy.SMTP              # CRLF, 7-bit safe headers

PLACEHOLDER_NAME = re.compile(r"^(?:piece_jointe|pi[eè]ce[ _-]?jointe|attachment|att|part|unnamed|noname|"
                              r"untitled|file|fichier|image)[ _-]?\d*$", re.I)


def orig_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


# ------------------------------------------------------------------ real type of a file, from its content
def detect(data: bytes) -> tuple[str, str, str]:
    """(maintype, subtype, extension); extension '' when unknown."""
    h = data[:16]
    if h.startswith(b"\x89PNG"):
        return "image", "png", "png"
    if h.startswith(b"\xff\xd8\xff"):
        return "image", "jpeg", "jpg"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "gif", "gif"
    if h.startswith(b"BM") and len(data) > 26:
        return "image", "bmp", "bmp"
    if h[:4] in (b"II*\x00", b"MM\x00*"):
        return "image", "tiff", "tif"
    if h[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", "webp", "webp"
    if h.startswith(b"%PDF"):
        return "application", "pdf", "pdf"
    if h.startswith(b"{\\rtf"):
        return "application", "rtf", "rtf"
    if h.startswith(b"PK\x03\x04"):
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except Exception:
            names = []
        o = "application"
        if any(n.startswith("word/") for n in names):
            return o, "vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"
        if any(n.startswith("xl/") for n in names):
            return o, "vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
        if any(n.startswith("ppt/") for n in names):
            return o, "vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"
        if "mimetype" in names:
            try:
                kind = zipfile.ZipFile(io.BytesIO(data)).read("mimetype").decode("ascii", "replace").strip()
            except Exception:
                kind = ""
            for suffix, ext in (("opendocument.text", "odt"), ("opendocument.spreadsheet", "ods"),
                                ("opendocument.presentation", "odp")):
                if kind.endswith(suffix):
                    return o, kind.split("/", 1)[-1], ext
        return o, "zip", "zip"
    if h.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):  # OLE: older Office formats and Outlook .msg
        u = data[:200000]
        if "Workbook".encode("utf-16-le") in u or "Book".encode("utf-16-le") in u:
            return "application", "vnd.ms-excel", "xls"
        if "WordDocument".encode("utf-16-le") in u:
            return "application", "msword", "doc"
        if "PowerPoint Document".encode("utf-16-le") in u:
            return "application", "vnd.ms-powerpoint", "ppt"
        if "__substg1.0_".encode("utf-16-le") in u:
            return "application", "vnd.ms-outlook", "msg"
        return "application", "octet-stream", ""
    if h.startswith(b"BEGIN:VCALENDAR"):
        return "text", "calendar", "ics"
    if h.startswith(b"BEGIN:VCARD"):
        return "text", "vcard", "vcf"
    return "application", "octet-stream", ""


# ------------------------------------------------------------------ tree
def _leaves(part, parent=None):
    """(part, parent) of every non-multipart part; a forwarded message (message/*) is one opaque leaf."""
    if part.is_multipart() and part.get_content_maintype() != "message":
        for sub in part.get_payload():
            yield from _leaves(sub, part)
    else:
        yield part, parent


def _is_body_text(part) -> bool:
    return part.get_content_type() in ("text/plain", "text/html") and part.get_filename() is None \
        and part.get_content_disposition() != "attachment"


def _text_of(part) -> str | None:
    try:
        return part.get_content()
    except Exception:
        return None


def _cid(part) -> str:
    v = part.get("Content-ID") or ""
    return v.strip().strip("<>").strip()


def _payload_digest(part) -> str:
    if part.get_content_maintype() == "message":
        data = part.as_bytes(policy=OUT_POLICY) if not isinstance(part.get_payload(), str) else \
            (part.get_payload(decode=True) or b"")
        # message/* sub-structure: compare the serialized inner message, independent of folding differences
        return hashlib.sha256(b"msg:" + re.sub(rb"\s+", b"", data)).hexdigest()
    return hashlib.sha256(part.get_payload(decode=True) or b"").hexdigest()


# ------------------------------------------------------------------ mojibake (UTF-8 read as Windows-1252)
_CP1252_EXTRA = "€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ"
_CONT = "[\u0080-¿" + _CP1252_EXTRA + "]"
MOJIBAKE = re.compile("[Â-ô]" + _CONT + "{1,3}")


def _cp1252_bytes(s: str) -> bytes | None:
    out = bytearray()
    for ch in s:
        try:
            out += ch.encode("cp1252")
        except UnicodeEncodeError:
            if ord(ch) < 256:
                out.append(ord(ch))   # bytes 0x81, 0x8d, 0x8f, 0x90, 0x9d: undefined in cp1252, kept as latin-1
            else:
                return None
    return bytes(out)


def fix_mojibake(text: str) -> tuple[str, int]:
    count = 0

    def sub(m):
        nonlocal count
        b = _cp1252_bytes(m.group(0))
        if b is None:
            return m.group(0)
        try:
            fixed = b.decode("utf-8")
        except UnicodeDecodeError:
            return m.group(0)
        if fixed == m.group(0) or not fixed:
            return m.group(0)
        count += 1
        return fixed

    text = MOJIBAKE.sub(sub, text)
    if count:
        # "à" is C3 A0: the A0 (no-break space) often becomes a plain space on the way, so "à Genève" reads
        # "Ã  Genève" (that space, then the real one). Only in a text that is mojibake already: "Ã" before a
        # space is legitimate in Portuguese capitals ("IRMÃ ").
        text, n = re.subn("\u00c3 (?=\\s)", "à", text)
        count += n
    return text, count


def has_mojibake(text: str) -> bool:
    return fix_mojibake(text)[1] > 0


# ------------------------------------------------------------------ replacement characters
WORD = re.compile(r"\w+")
BROKEN_WORD = re.compile(r"[\w�]*�[\w�]*")
_TAGS = re.compile(r"<[^>]*>")


def _words_of(text: str, is_html: bool) -> Counter:
    if is_html:
        text = htmlmod.unescape(_TAGS.sub(" ", text))
    return Counter(WORD.findall(text))


def rebuild_accents(text: str, words: Counter, is_html: bool) -> tuple[str, int, int]:
    """U+FFFD replaced from the words of the other version of the message. (text, fixed, left)."""
    fixed = left = 0
    lone = Counter({w: n for w, n in words.items() if len(w) == 1 and not w.isascii()})

    def sub(m):
        nonlocal fixed, left
        word = m.group(0)
        if set(word) == {FFFD}:
            if len(word) > 1:      # a run without letters: usually the no-break spaces of a signature
                fixed += 1
                return ("&nbsp;" if is_html else " ") * len(word)
            if lone:               # one alone: usually a one-letter word ("à" in French)
                fixed += 1
                return lone.most_common(1)[0][0]
            left += 1
            return word
        pattern = re.compile("^" + "".join(r"[^\W\d_a-zA-Z]" if c == FFFD else re.escape(c) for c in word) + "$")
        found = Counter({w: n for w, n in words.items() if pattern.match(w)})
        if found:
            fixed += 1
            return found.most_common(1)[0][0]
        loose = re.compile(pattern.pattern, re.I)
        for w, _ in words.most_common():
            if len(w) == len(word) and loose.match(w):
                fixed += 1
                return "".join(o if c == FFFD else c for c, o in zip(word, w))
        left += 1
        return word

    return BROKEN_WORD.sub(sub, text), fixed, left


# ------------------------------------------------------------------ analysis
@dataclass
class Analysis:
    issues: list[str] = field(default_factory=list)
    subject: str = ""
    date: str = ""
    already: bool = False      # carries MARK (or the mark of the earlier script)
    error: str | None = None


def _parse(raw: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(raw)


def _header(msg, name: str) -> str:
    try:
        return str(msg.get(name) or "")
    except Exception:
        return ""


def _placeholder(part) -> bool:
    fn = part.get_filename()
    if not fn:
        return False
    stem = fn.rsplit(".", 1)[0] if "." in fn else fn
    if "." in fn and fn.rsplit(".", 1)[1].strip():
        return False          # has an extension: its name is not the problem
    return bool(PLACEHOLDER_NAME.match(stem.strip()))


def _wrong_name(part) -> bool:
    """A placeholder name without an extension, whose content tells its real type."""
    if not _placeholder(part):
        return False
    return bool(detect(part.get_payload(decode=True) or b"")[2])


def _cids_in(html: str) -> list[str]:
    out: list[str] = []
    for c in re.findall(r"""cid:([^"'\s>)]+)""", html or "", re.I):
        c = htmlmod.unescape(c)
        if c not in out:
            out.append(c)
    return out


def analyze(raw: bytes) -> Analysis:
    a = Analysis()
    try:
        msg = _parse(raw)
        a.subject = _header(msg, "Subject")
        a.date = _header(msg, "Date")
    except Exception as e:  # pragma: no cover: the parser of policy.default is lenient
        a.issues.append("unreadable")
        a.error = str(e)
        return a
    if msg.get(MARK) or any(msg.get(h) for h in LEGACY_MARKS):
        a.already = True
        return a
    try:
        issues = _issues(msg)
    except Exception as e:
        a.issues.append("unreadable")
        a.error = f"{type(e).__name__}: {e}"
        return a
    if has_mojibake(a.subject):
        issues.add("mojibake")
    a.issues = [i for i in ISSUES if i in issues]
    return a


def _issues(msg) -> set[str]:
    issues: set[str] = set()
    leaves = list(_leaves(msg))
    cids_present = {_cid(p) for p, _ in leaves if _cid(p)}
    html_cids: list[str] = []
    loose_images = 0
    for part, _parent in leaves:
        if _is_body_text(part):
            text = _text_of(part)
            if text is None:
                continue
            if FFFD in text:
                issues.add("replacement_chars")
            if has_mojibake(text):
                issues.add("mojibake")
            if part.get_content_type() == "text/html":
                html_cids += _cids_in(text)
        elif part.get_content_maintype() != "message":
            if _wrong_name(part):
                issues.add("attachment_names")
            data = None
            if part.get_content_maintype() == "image" or _placeholder(part):
                data = part.get_payload(decode=True) or b""
            if not _cid(part) and data is not None and detect(data)[0] == "image":
                loose_images += 1
    missing = [c for c in html_cids if c not in cids_present]
    if missing and loose_images:
        issues.add("detached_images")
    return issues


# ------------------------------------------------------------------ repair
@dataclass
class Repaired:
    raw: bytes
    fixes: list[str]          # issues actually fixed
    left: list[str]           # issues found but not (fully) fixed
    notes: list[str]


def _set_text(part, text: str, subtype: str) -> None:
    if subtype == "html":
        text = re.sub(r"""(<meta[^>]+charset\s*=\s*["']?\s*)[A-Za-z0-9_\-:.]+""", r"\1utf-8", text,
                      count=1, flags=re.I)
    part.set_content(text, subtype=subtype, charset="utf-8", cte="quoted-printable")


def repair(raw: bytes) -> Repaired | None:
    """The repaired message, or None when there is nothing this module can safely fix."""
    a = analyze(raw)
    if a.already or not a.issues or a.issues == ["unreadable"]:
        return None
    msg = _parse(raw)
    fixes: set[str] = set()
    left: set[str] = set()
    notes: list[str] = []
    leaves = list(_leaves(msg))
    texts = [(p, _text_of(p)) for p, _ in leaves if _is_body_text(p)]
    texts = [(p, t) for p, t in texts if t is not None]

    # 1. mojibake first: a part that was read with the wrong charset is fixed as a whole
    for i, (part, text) in enumerate(texts):
        new, n = fix_mojibake(text)
        if n:
            _set_text(part, new, part.get_content_subtype())
            texts[i] = (part, new)
            fixes.add("mojibake")
    subject = _header(msg, "Subject")
    new_subject, n = fix_mojibake(subject)
    if n:
        del msg["Subject"]
        msg["Subject"] = new_subject
        fixes.add("mojibake")

    # 2. U+FFFD, from the words of the other text parts
    for i, (part, text) in enumerate(texts):
        if FFFD not in text:
            continue
        is_html = part.get_content_subtype() == "html"
        words = Counter()
        for other, ot in texts:
            if other is not part and FFFD not in ot:
                words += _words_of(ot, other.get_content_subtype() == "html")
        if not words:
            for other, ot in texts:      # every version is damaged: the intact words of all of them
                if other is not part:
                    words += Counter(w for w in WORD.findall(ot) if FFFD not in w)
        if not words:
            left.add("replacement_chars")
            notes.append("no intact text version to rebuild the accents from")
            continue
        new, n_fixed, n_left = rebuild_accents(text, words, is_html)
        if n_fixed:
            _set_text(part, new, part.get_content_subtype())
            texts[i] = (part, new)
            fixes.add("replacement_chars")
        if n_left:
            left.add("replacement_chars")
            notes.append(f"{n_left} damaged word(s) could not be rebuilt")

    # 3. attachment names and types
    for part, _parent in leaves:
        if _is_body_text(part) or part.get_content_maintype() == "message" or not _wrong_name(part):
            continue
        mt, st, ext = detect(part.get_payload(decode=True) or b"")
        name = f"{part.get_filename()}.{ext}"
        part.set_type(f"{mt}/{st}")
        part.set_param("name", name, header="Content-Type")
        if part.get("Content-Disposition") is not None:
            part.set_param("filename", name, header="Content-Disposition")
        else:
            part["Content-Disposition"] = "attachment"
            part.set_param("filename", name, header="Content-Disposition")
        fixes.add("attachment_names")

    # 4. images back in line: the cid: references the HTML shows but the message lacks
    if "detached_images" in a.issues:
        n = _reattach_images(msg)
        if n:
            fixes.add("detached_images")
            notes.append(f"{n} image(s) put back in line")
        else:
            left.add("detached_images")

    if not fixes:
        return None
    msg[MARK] = f"1; orig={orig_hash(raw)}; fixes={','.join(i for i in ISSUES if i in fixes)}"
    out = msg.as_bytes(policy=OUT_POLICY)
    problems = verify(raw, out)
    if problems:
        raise RepairCheckError("; ".join(problems))
    return Repaired(out, [i for i in ISSUES if i in fixes], [i for i in ISSUES if i in left], notes)


class RepairCheckError(Exception):
    """The repaired message did not pass verify(): nothing must be written."""


def _reattach_images(msg) -> int:
    leaves = list(_leaves(msg))
    present = {_cid(p) for p, _ in leaves if _cid(p)}
    html_parts = [p for p, _ in leaves if _is_body_text(p) and p.get_content_type() == "text/html"]
    if not html_parts:
        return 0
    html_part = html_parts[0]
    missing = [c for c in _cids_in(_text_of(html_part) or "") if c not in present]
    loose = [(p, parent) for p, parent in leaves
             if parent is not None and not _is_body_text(p) and p.get_content_maintype() != "message"
             and not _cid(p) and detect(p.get_payload(decode=True) or b"")[0] == "image"]
    pairs = list(zip(missing, loose))
    if not pairs:
        return 0
    for cid, (part, parent) in pairs:
        data = part.get_payload(decode=True) or b""
        mt, st, ext = detect(data)
        stem = cid.split("@")[0]
        name = stem if "." in stem else (part.get_filename() or "image") + (f".{ext}" if ext else "")
        parent.get_payload().remove(part)
        # turns html_part into multipart/related on the first call: the HTML becomes its first sub-part
        html_part.add_related(data, mt, st, cid=f"<{cid}>", filename=name, disposition="inline")
    # a multipart left with no part (all its images moved) would be invalid
    _drop_empty(msg)
    return len(pairs)


def _drop_empty(part) -> None:
    if not part.is_multipart() or part.get_content_maintype() == "message":
        return
    subs = part.get_payload()
    for s in list(subs):
        _drop_empty(s)
        if s.is_multipart() and s.get_content_maintype() != "message" and not s.get_payload():
            subs.remove(s)


# ------------------------------------------------------------------ verification
KEPT_HEADERS = ("From", "To", "Cc", "Date", "Message-ID", "In-Reply-To", "References")


def _attachment_digests(msg) -> Counter:
    return Counter(_payload_digest(p) for p, _ in _leaves(msg) if not _is_body_text(p))


def _fffd(msg) -> int:
    n = 0
    for p, _ in _leaves(msg):
        if _is_body_text(p):
            n += (_text_of(p) or "").count(FFFD)
    return n


def verify(original: bytes, repaired: bytes) -> list[str]:
    """What the repaired message lost compared to the original; empty when it may be written."""
    problems = []
    try:
        a, b = _parse(original), _parse(repaired)
    except Exception as e:  # pragma: no cover
        return [f"unreadable result: {e}"]
    for h in KEPT_HEADERS:
        if " ".join(_header(a, h).split()) != " ".join(_header(b, h).split()):
            problems.append(f"header {h} changed")
    if not b.get(MARK):
        problems.append("mark missing")
    if _attachment_digests(a) != _attachment_digests(b):
        problems.append("attachments differ")
    if _fffd(b) > _fffd(a):
        problems.append("more replacement characters than before")
    if not any(_is_body_text(p) for p, _ in _leaves(b)) and any(_is_body_text(p) for p, _ in _leaves(a)):
        problems.append("text body lost")
    return problems


# ------------------------------------------------------------------ preview (before / after, for a person)
def summary(raw: bytes, limit: int = 1200) -> dict:
    msg = _parse(raw)
    text = ""
    for p, _ in _leaves(msg):
        if _is_body_text(p):
            t = _text_of(p) or ""
            if p.get_content_subtype() == "html":
                t = htmlmod.unescape(_TAGS.sub(" ", re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", t)))
            t = re.sub(r"[ \t ]+", " ", t)
            t = re.sub(r"\s*\n\s*", "\n", t).strip()
            if t and (not text or FFFD in t or has_mojibake(t)):
                text = t
                if FFFD in t or has_mojibake(t):
                    break
    parts = []
    for p, _ in _leaves(msg):
        if _is_body_text(p):
            continue
        parts.append({"name": p.get_filename() or "", "type": p.get_content_type(),
                      "inline": bool(_cid(p)), "size": len(p.get_payload(decode=True) or b"")
                      if p.get_content_maintype() != "message" else None})
    return {"subject": _header(msg, "Subject"), "text": text[:limit], "parts": parts}
