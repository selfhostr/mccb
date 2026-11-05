"""Codepage / charset handling.

Rules (see SPEC "Lessons" 1):
* PT_UNICODE is UTF-16-LE, PT_STRING8 is in the message codepage;
* the HTML body is in PR_INTERNET_CPID, fallback ``<meta charset>``, then cp1252;
* we never introduce U+FFFD: the last resort is a lossless cp1252/latin-1 mix.
"""
from __future__ import annotations

import codecs
import re
import unicodedata

_CPID = {
    37: "cp037", 437: "cp437", 500: "cp500", 708: "iso8859_6", 720: "cp720", 737: "cp737",
    775: "cp775", 850: "cp850", 852: "cp852", 855: "cp855", 857: "cp857", 858: "cp858",
    860: "cp860", 861: "cp861", 862: "cp862", 863: "cp863", 864: "cp864", 865: "cp865",
    866: "cp866", 869: "cp869", 874: "cp874", 875: "cp875", 932: "cp932", 936: "gb18030",
    949: "cp949", 950: "cp950", 1026: "cp1026", 1140: "cp1140",
    1200: "utf-16-le", 1201: "utf-16-be",
    1250: "cp1250", 1251: "cp1251", 1252: "cp1252", 1253: "cp1253", 1254: "cp1254",
    1255: "cp1255", 1256: "cp1256", 1257: "cp1257", 1258: "cp1258", 1361: "johab",
    10000: "mac_roman", 10001: "shift_jis", 10002: "big5", 10003: "euc_kr", 10004: "mac_arabic",
    10008: "gb2312", 10006: "mac_greek", 10007: "mac_cyrillic", 10029: "mac_latin2",
    10079: "mac_iceland", 10081: "mac_turkish",
    12000: "utf-32-le", 12001: "utf-32-be",
    20127: "us-ascii", 20866: "koi8_r", 21866: "koi8_u", 20932: "euc_jp", 20936: "gb2312", 20949: "cp949",
    28591: "iso8859_1", 28592: "iso8859_2", 28593: "iso8859_3", 28594: "iso8859_4",
    28595: "iso8859_5", 28596: "iso8859_6", 28597: "iso8859_7", 28598: "iso8859_8",
    28599: "iso8859_9", 28600: "iso8859_10", 28601: "iso8859_11", 28603: "iso8859_13",
    28604: "iso8859_14", 28605: "iso8859_15", 28606: "iso8859_16", 38598: "iso8859_8",
    50220: "iso2022_jp", 50221: "iso2022_jp_ext", 50222: "iso2022_jp", 50225: "iso2022_kr",
    51932: "euc_jp", 51936: "gb2312", 51949: "euc_kr", 52936: "hz", 54936: "gb18030",
    65000: "utf-7", 65001: "utf-8",
}

# Labels whose real-world content is a superset (WHATWG encoding standard).
_SUPERSET = {
    "ascii": "cp1252", "us-ascii": "cp1252", "iso8859-1": "cp1252", "latin-1": "cp1252",
    "iso8859-9": "cp1254", "iso8859-11": "cp874", "tis-620": "cp874",
    "gb2312": "gb18030", "gbk": "gb18030", "euc_kr": "cp949", "shift_jis": "cp932",
    "big5": "cp950",
}

_RTF_FCHARSET = {
    0: None, 1: None, 2: "symbol", 77: "mac_roman", 128: "cp932", 129: "cp949", 130: "johab",
    134: "gb18030", 136: "cp950", 161: "cp1253", 162: "cp1254", 163: "cp1258",
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
    if name in ("iso-8859-8-i", "iso-8859-8-e", "iso_8859-8-i", "csiso88598i", "logical"):
        name = "iso8859_8"
    if name.startswith("x-") and name[2:] in ("euc-jp", "euc", "gbk", "cp1251", "cp1250", "cp1252"):
        name = name[2:]
    if name in ("cp-850", "cp-866"):
        name = name.replace("-", "")
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


def _lenient_cp1252_encode(s: str) -> bytes | None:
    """Inverse of _lenient_cp1252; None if a character is outside cp1252/latin-1."""
    out = bytearray()
    for ch in s:
        try:
            out += ch.encode("cp1252")
        except UnicodeEncodeError:
            if ord(ch) > 0xFF:
                return None
            out.append(ord(ch))
    return bytes(out)


# 7-bit stateful encodings: the bytes are pure ASCII but the text is not.
_STATEFUL = {"iso2022_jp", "iso2022_jp_1", "iso2022_jp_2", "iso2022_jp_2004", "iso2022_jp_3",
             "iso2022_jp_ext", "iso2022_kr", "hz", "utf-7"}
_ESC_DEFAULTS = ("iso2022_jp_2", "iso2022_jp_ext", "iso2022_kr")
# strict decoders that reject a few vendor code points: retry with a sibling
_SIBLING = {"cp950": ("big5hkscs",), "euc_jp": ("euc_jis_2004", "cp932"), "shift_jis": ("cp932",)}
_UNICODE_CODECS = {"utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be", "utf-16", "utf-32"}


def decode_ascii(data: bytes, candidates=()) -> str:
    """ASCII bytes: honour ISO-2022 escape sequences / HZ / UTF-7 when relevant."""
    stateful = [c for c in candidates if c in _STATEFUL]
    if b"\x1b" in data:
        stateful += [c for c in _ESC_DEFAULTS if c not in stateful]
    for c in stateful:
        try:
            return data.decode(c)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("ascii")


def _try_decode(data: bytes, c: str) -> str | None:
    for cc in (c,) + _SIBLING.get(c, ()):
        try:
            return data.decode(cc)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def decode_bytes(data: bytes, candidates) -> str:
    """Decode with the first candidate that decodes strictly; lossless fallback."""
    if not data:
        return ""
    cands: list[str] = []
    for c in candidates:
        c = normalize(c) if c else None
        if c and c not in cands:
            cands.append(c)
    if data.isascii():
        return decode_ascii(data, cands)
    for c in cands:
        t = _try_decode(data, c)
        if t is not None:
            return t
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return _lenient_cp1252(data)


# ---------------------------------------------------------------- detection (last resort)
_GUESS_ORDER = ("cp1252", "cp1250", "cp1251", "koi8-r", "cp1253", "cp1254", "cp1256", "cp1255",
                "cp1257", "cp874", "cp1258", "cp932", "cp949", "gb18030", "cp950", "euc_jp")
_COMMON_PUNCT = set(" «»–—‘’‚“”„…€°·•×÷§©®™±¿¡　、。「」『』【】〈〉《》・ー，．！？：；（）～‏‎")
_SCRIPT_CACHE: dict[str, str] = {}


def _script(ch: str) -> str:
    r = _SCRIPT_CACHE.get(ch)
    if r is None:
        w = unicodedata.name(ch, "?").split(" ", 1)[0]
        if w in ("CJK", "HIRAGANA", "KATAKANA", "IDEOGRAPHIC", "FULLWIDTH", "HALFWIDTH"):
            w = "CJK"
        r = _SCRIPT_CACHE[ch] = w
    return r


def _cjk_score(ch: str, codec: str) -> float:
    """Score of one CJK character (it stands for two bytes, hence the weights)."""
    o = ord(ch)
    if 0xFF61 <= o <= 0xFF9F:          # half-width katakana: typical of a wrong DBCS guess
        return -1.0
    if 0x3040 <= o <= 0x30FF:
        return 2.0 if codec in ("cp932", "euc_jp") else 0.0
    if 0xAC00 <= o <= 0xD7A3:
        try:
            ch.encode("euc_kr")
            return 2.0
        except UnicodeEncodeError:
            return -0.5
    if 0x4E00 <= o <= 0x9FFF:
        try:
            if codec == "gb18030":
                return 2.0 if 0xB0 <= ch.encode("gb2312")[0] <= 0xD7 else 0.0
            if codec == "cp950":
                return 2.0 if 0xA4 <= ch.encode("cp950")[0] <= 0xC6 else 0.0
            if codec in ("cp932", "euc_jp"):
                return 2.0 if 0x88 <= ch.encode("cp932")[0] <= 0x98 else 0.0
        except UnicodeEncodeError:
            return 0.0
        return -0.5
    return 0.0


# letters that real text in the codepage's languages (almost) never uses
_RARE = set("ðÐþÞõÕûÛýÝ" "ฃฅฤฦฯ")
_HEB_FINAL = set("ךםןףץ")
_THAI_LEAD = set("เแโใไ")
_RU_VOWELS = set("аеёиоуыэюяАЕЁИОУЫЭЮЯ")


def _word_score(w: list[str], codec: str) -> float:
    na = [c for c in w if ord(c) >= 0x80]
    if not na:
        return 0.0
    scripts = {_script(c) for c in w if unicodedata.category(c)[0] == "L"}
    score = 0.0
    if len(scripts - {"LATIN"}) > 1 or ("LATIN" in scripts and scripts & {"CYRILLIC", "GREEK", "HEBREW", "ARABIC", "THAI"}):
        score -= 2.0 * len(na)
    if unicodedata.category(w[0])[0] == "M":
        score -= 2.0
    if scripts == {"LATIN"} and len(w) >= 2 and len(na) == len(w):
        score -= 1.5 * len(w)       # "ÕÅÎ": a Latin word made only of accented letters
    if scripts == {"CYRILLIC"} and len(w) >= 4 and not (_RU_VOWELS & set(w)):
        score -= 2.0
    if w[-1] in _THAI_LEAD:
        score -= 1.0
    run = marks = 0
    prev_lower = False
    for i, c in enumerate(w):
        cat = unicodedata.category(c)
        marks = marks + 1 if cat[0] == "M" else 0
        if marks > 2:
            score -= 1.0
        if ord(c) >= 0x80 and _script(c) == "LATIN":
            run += 1
            if run >= 3:
                score -= 2.0
        else:
            run = 0
        if cat == "Lu" and prev_lower and ord(c) >= 0x80:
            score -= 1.5
        prev_lower = cat == "Ll"
        if c in _RARE:
            score -= 2.0
        if c in _HEB_FINAL:
            score += 0.5 if i == len(w) - 1 else -2.0
    for c in na:
        sc = _script(c)
        if sc in ("CJK", "HANGUL"):
            score += _cjk_score(c, codec)
        elif unicodedata.category(c)[0] in "LM":
            score += 1.0
    return score


def plausibility(text: str, codec: str = "") -> float:
    """Rough score of how much a decoded text looks like real text (higher is better)."""
    score = 0.0
    word: list[str] = []
    text = text[:6000]
    n = len(text)
    for i, ch in enumerate(text):
        cat = unicodedata.category(ch)
        if cat[0] in "LM":
            word.append(ch)
            continue
        if word:
            score += _word_score(word, codec)
        if ord(ch) < 0x80:
            word = []
            if cat == "Cc" and ch not in "\t\r\n":
                score -= 3.0
            continue
        if cat in ("Cc", "Co", "Cn", "Cs") or ch == "�":
            score -= 4.0
        elif word and i + 1 < n and unicodedata.category(text[i + 1])[0] == "L" \
                and ch not in " 　‎‏":
            score -= 2.0            # symbol glued inside a word: "Za¿ó³æ"
        elif ch not in _COMMON_PUNCT and cat[0] != "N":
            score -= 1.0
        word = []
    if word:
        score += _word_score(word, codec)
    return score


def _decode_sample(pieces, codec: str) -> str | None:
    out = []
    for p, truncated in pieces:
        for cut in ((0, 1, 2, 3) if truncated else (0,)):
            t = _try_decode(p[:len(p) - cut] if cut else p, codec)
            if t is not None:
                out.append(t)
                break
        else:
            return None
    return "\n".join(out)


def guess_codec(data, preferred=(), default: str | None = "cp1252", margin: float = 3.0) -> str | None:
    """Best legacy codec for ``data`` (bytes or a list of bytes samples).

    Valid UTF-8 wins; otherwise the preferred codecs (then ``default``) are kept
    unless another codepage gives a clearly more plausible text.  Returns None
    for pure ASCII input."""
    pieces = [data] if isinstance(data, (bytes, bytearray)) else list(data)
    pieces = [(bytes(p[:8192]), len(p) > 8192) for p in pieces if p and not bytes(p).isascii()]
    if not pieces:
        return None
    if _decode_sample(pieces, "utf-8") is not None:
        return "utf-8"
    pref, bonus = [], {}
    for i, c in enumerate(list(preferred) + [default]):
        c = normalize(c) if c else None
        if c and c not in pref and c not in _UNICODE_CODECS and c != "utf-8":
            pref.append(c)
            # an explicit hint (PR_INTERNET_CPID...) outranks the store default
            bonus[c] = margin * (2 if i < len(preferred) else 1)
    order = pref + [c for c in (normalize(x) for x in _GUESS_ORDER) if c not in pref]
    best = None
    for c in order:
        t = _decode_sample(pieces, c)
        if t is None:
            continue
        sc = plausibility(t, c) + bonus.get(c, 0.0)
        if best is None or sc > best[0]:
            best = (sc, c)
    if best is None:
        return pref[0] if pref else None
    return best[1]


_ANSI = {}
for _k, _v in (("iso2022_jp", "cp932"), ("iso2022_jp_ext", "cp932"), ("iso2022_jp_2", "cp932"),
               ("euc_jp", "cp932"), ("shift_jis", "cp932"), ("euc_kr", "cp949"), ("iso2022_kr", "cp949"),
               ("gb2312", "gb18030"), ("hz", "gb18030"), ("koi8_r", "cp1251"), ("koi8_u", "cp1251"),
               ("iso8859_5", "cp1251"), ("iso8859_2", "cp1250"), ("iso8859_7", "cp1253"),
               ("iso8859_8", "cp1255"), ("iso8859_6", "cp1256"), ("iso8859_9", "cp1254"),
               ("iso8859_13", "cp1257"), ("iso8859_4", "cp1257"), ("iso8859_15", "cp1252"),
               ("iso8859_11", "cp874"), ("big5hkscs", "cp950")):
    _ANSI[codecs.lookup(_k).name] = _v


def ansi_for(codec: str | None) -> str | None:
    """Windows ANSI codepage an Outlook client writing ``codec`` mail would use for PT_STRING8."""
    if not codec or codec in _UNICODE_CODECS or codec in ("utf-8", "utf-7"):
        return None
    return _ANSI.get(codec, codec)


def is_unicode_codec(codec: str | None) -> bool:
    return codec in _UNICODE_CODECS or codec == "utf-7"


_META_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_:.\-]+)""", re.I)
_XML_RE = re.compile(rb"""<\?xml[^>]+encoding\s*=\s*["']([A-Za-z0-9_:.\-]+)""", re.I)


def sniff_html_charset(data: bytes) -> str | None:
    head = data[:8192]
    m = _META_RE.search(head) or _XML_RE.search(head)
    return normalize(m.group(1).decode("ascii", "ignore")) if m else None


_SINGLE_BYTE_WEAK = {"cp1252", "us-ascii", "ascii", "iso8859-1", "latin-1", None}


def decode_html(data: bytes, cpid: int | None, fallback: str | None = None) -> tuple[str, str]:
    """Decode an HTML body. Returns (text, charset used).

    Order: BOM; valid UTF-8 (when the declared codepage is weak or says UTF-8);
    PR_INTERNET_CPID vs ``<meta charset>`` (when both decode but disagree, the
    more plausible text wins, the codepage being preferred unless it is the
    weak cp1252 default); then ``fallback`` (message codepage) / detection."""
    if data.startswith(codecs.BOM_UTF8):
        return _strict_or(data[3:], "utf-8"), "utf-8"
    if data.startswith(codecs.BOM_UTF16_LE):
        return data[2:].decode("utf-16-le", "replace"), "utf-16-le"
    if data.startswith(codecs.BOM_UTF16_BE):
        return data[2:].decode("utf-16-be", "replace"), "utf-16-be"
    cp = codec_for_cpid(cpid)
    if cp in _UNICODE_CODECS:
        cp = None
    meta = sniff_html_charset(data)
    if meta in _UNICODE_CODECS:
        meta = None             # an 8-bit body cannot be UTF-16: the declaration is stale
    if data.isascii():
        cands = [c for c in (cp, meta, fallback) if c]
        return decode_ascii(data, cands), next((c for c in cands if c in _STATEFUL), "us-ascii")
    # Valid UTF-8 with non-ASCII bytes is (practically) never meant as a
    # single-byte codepage: prefer it when the declared cp is weak.
    if cp in _SINGLE_BYTE_WEAK or meta == "utf-8" or cp == "utf-8":
        try:
            return data.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            pass
    decoded = {}
    for c in (cp, meta):
        if c and c != "utf-8" and c not in decoded:
            t = _try_decode(data, c)
            if t is not None:
                decoded[c] = t
    if decoded:
        if len(decoded) == 2 and decoded[cp] != decoded[meta]:
            pref, other = (cp, meta) if cp not in _SINGLE_BYTE_WEAK else (meta, cp)
            if plausibility(decoded[other], other) > plausibility(decoded[pref], pref) + 3.0:
                pref = other
            return decoded[pref], pref
        c = next(iter(decoded))
        return decoded[c], c
    g = guess_codec(data, [fallback] if fallback else [], "cp1252") or "cp1252"
    t = _try_decode(data, g)
    if t is not None:
        return t, g
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


_CONT = "\u0080-¿ŒœŠšŸŽžƒˆ˜–-›€™"
# UTF-8 lead byte (as cp1252) + continuation byte: Latin / punctuation mojibake ("Ã©", "â€™")
_MOJIBAKE_HINT = re.compile("[ÂÃÅâ][%s]" % _CONT)
# Cyrillic, Greek, Hebrew, Arabic (2-byte) and CJK (3-byte) mojibake: needs two hits
_MOJIBAKE_WIDE = re.compile("[ÐÑÎÏ×ØÙÚÛ][%s]|[ãäåæçèéêëìíîï][%s]{2}" % (_CONT, _CONT))


def fix_mojibake(s: str) -> str:
    """Repair UTF-8 decoded as cp1252/latin-1 ("Ã©" -> "é"), conservatively.

    The whole string must round-trip (cp1252 bytes forming valid UTF-8), and
    the result must not contain control / unassigned / private characters."""
    if not s or not (_MOJIBAKE_HINT.search(s) or len(_MOJIBAKE_WIDE.findall(s)) >= 2):
        return s
    b = _lenient_cp1252_encode(s)
    if b is None:
        return s
    try:
        fixed = b.decode("utf-8")
    except UnicodeDecodeError:
        return s
    if fixed == s or "�" in fixed:
        return s
    if any(unicodedata.category(c) in ("Cc", "Co", "Cn") and c not in "\t\r\n" for c in fixed):
        return s
    return fixed
