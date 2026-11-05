"""Codepage / charset handling.

Rules (see SPEC « Leçons » 1):
* PT_UNICODE is UTF-16-LE, PT_STRING8 is in the message codepage;
* the HTML body is in PR_INTERNET_CPID, fallback ``<meta charset>``, then cp1252;
* we never introduce U+FFFD: the last resort is a lossless cp1252/latin-1 mix.
"""
from __future__ import annotations

import codecs
import re

_CPID = {
    37: "cp037", 437: "cp437", 500: "cp500", 708: "iso8859_6", 720: "cp720", 737: "cp737",
    775: "cp775", 850: "cp850", 852: "cp852", 855: "cp855", 857: "cp857", 858: "cp858",
    860: "cp860", 861: "cp861", 862: "cp862", 863: "cp863", 864: "cp864", 865: "cp865",
    866: "cp866", 869: "cp869", 874: "cp874", 875: "cp875", 932: "cp932", 936: "gb18030",
    949: "cp949", 950: "big5hkscs", 1026: "cp1026", 1140: "cp1140",
    1200: "utf-16-le", 1201: "utf-16-be",
    1250: "cp1250", 1251: "cp1251", 1252: "cp1252", 1253: "cp1253", 1254: "cp1254",
    1255: "cp1255", 1256: "cp1256", 1257: "cp1257", 1258: "cp1258", 1361: "johab",
    10000: "mac_roman", 10006: "mac_greek", 10007: "mac_cyrillic", 10029: "mac_latin2",
    10079: "mac_iceland", 10081: "mac_turkish",
    12000: "utf-32-le", 12001: "utf-32-be",
    20127: "us-ascii", 20866: "koi8_r", 21866: "koi8_u", 20932: "euc_jp", 20936: "gb2312",
    28591: "iso8859_1", 28592: "iso8859_2", 28593: "iso8859_3", 28594: "iso8859_4",
    28595: "iso8859_5", 28596: "iso8859_6", 28597: "iso8859_7", 28598: "iso8859_8",
    28599: "iso8859_9", 28603: "iso8859_13", 28605: "iso8859_15", 38598: "iso8859_8",
    50220: "iso2022_jp", 50221: "iso2022_jp_ext", 50222: "iso2022_jp", 50225: "iso2022_kr",
    51932: "euc_jp", 51936: "gb2312", 51949: "euc_kr", 52936: "hz", 54936: "gb18030",
    65000: "utf-7", 65001: "utf-8",
}

# Labels whose real-world content is a superset (WHATWG encoding standard).
_SUPERSET = {
    "ascii": "cp1252", "us-ascii": "cp1252", "iso8859-1": "cp1252", "latin-1": "cp1252",
    "iso8859-9": "cp1254", "iso8859-11": "cp874", "tis-620": "cp874",
    "gb2312": "gb18030", "gbk": "gb18030", "euc_kr": "cp949", "shift_jis": "cp932",
    "big5": "big5hkscs",
}

_RTF_FCHARSET = {
    0: None, 1: None, 2: "symbol", 77: "mac_roman", 128: "cp932", 129: "cp949", 130: "johab",
    134: "gb18030", 136: "big5hkscs", 161: "cp1253", 162: "cp1254", 163: "cp1258",
    177: "cp1255", 178: "cp1256", 186: "cp1257", 204: "cp1251", 222: "cp874",
    238: "cp1250", 254: "cp437", 255: "cp850",
}


def normalize(name: str | None) -> str | None:
    """Python codec name for a charset label, with WHATWG supersets applied."""
    if not name:
        return None
    name = name.strip().strip("\"'").lower()
    if not name:
        return None
    if name in ("utf8", "unicode-1-1-utf-8", "x-unicode20utf8"):
        name = "utf-8"
    if name in ("ks_c_5601-1987", "ks_c_5601", "x-windows-949", "windows-949"):
        name = "cp949"
    if name in ("x-sjis", "sjis", "ms_kanji", "windows-31j"):
        name = "cp932"
    if name.startswith("windows-") and name[8:].isdigit():
        name = "cp" + name[8:]
    if name.startswith("x-mac-"):
        name = "mac_" + name[6:]
    try:
        canon = codecs.lookup(name).name
    except LookupError:
        return None
    return _SUPERSET.get(canon, canon)


def codec_for_cpid(cpid: int | None) -> str | None:
    if not cpid:
        return None
    name = _CPID.get(cpid)
    if name is None:
        try:
            name = codecs.lookup("cp%d" % cpid).name
        except LookupError:
            return None
    return normalize(name)


def rtf_fcharset_codec(fcharset: int) -> str | None:
    return _RTF_FCHARSET.get(fcharset)


def _lenient_cp1252(data: bytes) -> str:
    """cp1252 where defined, latin-1 for the 5 undefined bytes: never fails."""
    out = []
    for chunk in re.split(rb"([\x81\x8d\x8f\x90\x9d])", data):
        if len(chunk) == 1 and chunk in b"\x81\x8d\x8f\x90\x9d":
            out.append(chunk.decode("latin-1"))
        elif chunk:
            out.append(chunk.decode("cp1252"))
    return "".join(out)


def decode_bytes(data: bytes, candidates) -> str:
    """Decode with the first candidate that decodes strictly; lossless fallback."""
    if not data:
        return ""
    if data.isascii():
        return data.decode("ascii")
    tried = set()
    for c in candidates:
        c = normalize(c) if c else None
        if not c or c in tried:
            continue
        tried.add(c)
        try:
            return data.decode(c)
        except (UnicodeDecodeError, LookupError):
            continue
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return _lenient_cp1252(data)


_META_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_:.\-]+)""", re.I)
_XML_RE = re.compile(rb"""<\?xml[^>]+encoding\s*=\s*["']([A-Za-z0-9_:.\-]+)""", re.I)


def sniff_html_charset(data: bytes) -> str | None:
    head = data[:8192]
    m = _META_RE.search(head) or _XML_RE.search(head)
    return normalize(m.group(1).decode("ascii", "ignore")) if m else None


_SINGLE_BYTE_WEAK = {"cp1252", "us-ascii", "ascii", "iso8859-1", "latin-1", None}


def decode_html(data: bytes, cpid: int | None) -> tuple[str, str]:
    """Decode an HTML body. Returns (text, charset used)."""
    if data.startswith(codecs.BOM_UTF8):
        return _strict_or(data[3:], "utf-8"), "utf-8"
    if data.startswith(codecs.BOM_UTF16_LE):
        return data[2:].decode("utf-16-le", "replace"), "utf-16-le"
    if data.startswith(codecs.BOM_UTF16_BE):
        return data[2:].decode("utf-16-be", "replace"), "utf-16-be"
    if data.isascii():
        return data.decode("ascii"), "us-ascii"
    cp = codec_for_cpid(cpid)
    meta = sniff_html_charset(data)
    # Valid UTF-8 with non-ASCII bytes is (practically) never meant as a
    # single-byte codepage: prefer it when the declared cp is weak.
    if cp in _SINGLE_BYTE_WEAK or meta == "utf-8":
        try:
            return data.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            pass
    for c in (cp, meta, "cp1252"):
        if not c:
            continue
        try:
            return data.decode(c), c
        except (UnicodeDecodeError, LookupError):
            continue
    return _lenient_cp1252(data), "cp1252"


def _strict_or(data: bytes, enc: str) -> str:
    try:
        return data.decode(enc)
    except UnicodeDecodeError:
        return decode_bytes(data, ["cp1252"])


_META_REWRITE = re.compile(
    r"""(<meta[^>]+charset\s*=\s*["']?\s*)([A-Za-z0-9_:.\-]+)""", re.I)
_XML_REWRITE = re.compile(r"""(<\?xml[^>]+encoding\s*=\s*["'])([A-Za-z0-9_:.\-]+)""", re.I)


def force_html_utf8(html: str) -> str:
    """Rewrite in-document charset declarations to utf-8 (we emit UTF-8)."""
    html = _META_REWRITE.sub(lambda m: m.group(1) + "utf-8", html, count=3)
    html = _XML_REWRITE.sub(lambda m: m.group(1) + "utf-8", html, count=1)
    return html


_MOJIBAKE_HINT = re.compile("[ÂÃÅâ][\u0080-¿ŒœŠšŸŽžƒˆ˜–-›€™]")


def fix_mojibake(s: str) -> str:
    """Repair UTF-8 decoded as cp1252/latin-1 ("Ã©" -> "é"), conservatively."""
    if not s or not _MOJIBAKE_HINT.search(s):
        return s
    for enc in ("cp1252", "latin-1"):
        try:
            fixed = s.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed != s and "�" not in fixed:
            return fixed
    return s
