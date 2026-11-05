"""PR_RTF_COMPRESSED support: LZFu decompression (MS-OXRTFCP), HTML
de-encapsulation (MS-OXRTFEX, ``\\fromhtml1``) and RTF -> plain text."""
from __future__ import annotations

import re
import struct

from .charset import codec_for_cpid, rtf_fcharset_codec, decode_bytes

_PREBUF = (
    b"{\\rtf1\\ansi\\mac\\deff0\\deftab720{\\fonttbl;}{\\f0\\fnil \\froman "
    b"\\fswiss \\fmodern \\fscript \\fdecor MS Sans SerifSymbolArialTimes New "
    b"RomanCourier{\\colortbl\\red0\\green0\\blue0\r\n\\par "
    b"\\pard\\plain\\f0\\fs20\\b\\i\\u\\tab\\tx"
)
_COMPRESSED = 0x75465A4C  # 'LZFu'
_UNCOMPRESSED = 0x414C454D  # 'MELA'


class RtfError(ValueError):
    pass


def decompress(data: bytes) -> bytes:
    if data is None or len(data) < 16:
        raise RtfError("RTF data too short")
    comp_size, raw_size, magic, crc = struct.unpack_from("<IIII", data)
    if magic == _UNCOMPRESSED:
        return bytes(data[16:16 + raw_size])
    if magic != _COMPRESSED:
        raise RtfError("unknown RTF compression 0x%08x" % magic)
    src = memoryview(data)[16:4 + comp_size] if comp_size + 4 <= len(data) else memoryview(data)[16:]
    dic = bytearray(4096)
    dic[:len(_PREBUF)] = _PREBUF
    wpos = len(_PREBUF)
    out = bytearray()
    i, n = 0, len(src)
    limit = raw_size if raw_size else 1 << 31
    while i < n and len(out) < limit:
        ctrl = src[i]
        i += 1
        for bit in range(8):
            if i >= n:
                break
            if ctrl & (1 << bit):
                if i + 1 >= n:
                    i = n
                    break
                ref = (src[i] << 8) | src[i + 1]
                i += 2
                off, ln = ref >> 4, (ref & 0xF) + 2
                if off == wpos:
                    return bytes(out)
                for k in range(ln):
                    c = dic[(off + k) & 0xFFF]
                    out.append(c)
                    dic[wpos] = c
                    wpos = (wpos + 1) & 0xFFF
            else:
                c = src[i]
                i += 1
                out.append(c)
                dic[wpos] = c
                wpos = (wpos + 1) & 0xFFF
    return bytes(out)


# --------------------------------------------------------------------------
_TOKEN = re.compile(
    rb"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|(\r\n|\r|\n)|([^\\{}\r\n]+)",
    re.S,
)

# destinations whose content is never text
_SKIP_DEST = {
    b"fonttbl", b"colortbl", b"stylesheet", b"info", b"pict", b"object", b"objdata",
    b"header", b"headerl", b"headerr", b"headerf", b"footer", b"footerl", b"footerr",
    b"footerf", b"fldinst", b"listtable", b"listoverridetable", b"rsidtbl", b"generator",
    b"xmlnstbl", b"themedata", b"colorschememapping", b"latentstyles", b"datastore",
    b"pgdsctbl", b"filetbl", b"revtbl", b"bkmkstart", b"bkmkend", b"pnseclvl",
    b"pntext", b"pntxta", b"pntxtb", b"listtext", b"xe", b"tc", b"template", b"author",
    b"operator", b"title", b"subject", b"comment", b"keywords", b"doccomm", b"nonshppict",
    b"shppict", b"blipuid", b"falt", b"panose", b"mmathPr", b"wgrffmtfilter", b"passwordhash",
    b"fchars", b"lchars", b"userprops", b"docvar", b"ftnsep", b"ftnsepc", b"aftnsep",
}
_SPECIAL = {b"par": "\n", b"line": "\n", b"tab": "\t", b"cell": "\t", b"row": "\n",
            b"sect": "\n", b"page": "\n", b"emdash": "\u2014", b"endash": "\u2013",
            b"emspace": "\u2003", b"enspace": "\u2002", b"qmspace": "\u2005",
            b"bullet": "\u2022", b"lquote": "\u2018", b"rquote": "\u2019",
            b"ldblquote": "\u201c", b"rdblquote": "\u201d", b"nbsp": "\u00a0"}


class _State:
    __slots__ = ("skip", "uc", "font", "htmltag", "htmlrtf", "dest", "mhtml")

    def __init__(self):
        self.skip = False
        self.uc = 1
        self.font = None
        self.htmltag = False
        self.htmlrtf = False
        self.dest = None
        self.mhtml = False

    def copy(self):
        s = _State()
        for k in self.__slots__:
            setattr(s, k, getattr(self, k))
        return s


def is_html_rtf(rtf: bytes) -> bool:
    return b"\\fromhtml" in rtf[:1024]


_DBCS = {"cp932", "cp936", "gbk", "gb18030", "cp949", "cp950", "big5hkscs", "johab", "shift_jis",
         "euc_jp", "euc_kr", "gb2312"}


def _join_surrogates(s: str) -> str:
    """\\uN escapes of a non-BMP character come as a UTF-16 surrogate pair."""
    if not any(0xD800 <= ord(c) <= 0xDFFF for c in s):
        return s
    return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _parse(rtf: bytes, html_mode: bool, default_codec: str | None = None) -> str:
    out: list[str] = []
    pending = bytearray()          # \'hh bytes awaiting decode
    pending_codec = [None]
    ansicpg = default_codec or "cp1252"
    fonts: dict[int, str | None] = {}
    deff = None
    st = _State()
    stack: list[_State] = []
    uc_skip = 0
    font_num = None               # inside \fonttbl: current \fN being defined
    expect_dest = False           # just after \*
    last_mhtml_param = None
    skip_next_htmltag = None

    def emit_ok(state):
        if state.skip:
            return False
        if html_mode:
            return state.htmltag or not state.htmlrtf
        return True

    def codec_of(state):
        f = state.font if state.font is not None else deff
        c = fonts.get(f) if f is not None else None
        return c or ansicpg

    def flush():
        if pending:
            out.append(decode_bytes(bytes(pending), [pending_codec[0], ansicpg]))
            pending.clear()

    for m in _TOKEN.finditer(rtf):
        word, param, hexb, sym, brace, nl, text = m.groups()
        if brace is not None:
            flush()
            if brace == b"{":
                stack.append(st)
                st = st.copy()
                st.dest = None
            else:
                if stack:
                    st = stack.pop()
            expect_dest = False
            uc_skip = 0
            continue
        if nl is not None:
            continue
        if word is not None:
            p = int(param) if param is not None else None
            if uc_skip and word not in (b"par",):
                # a control word counts as one skipped char after \u
                uc_skip -= 1
                continue
            if expect_dest:
                expect_dest = False
                if html_mode and word in (b"htmltag", b"mhtmltag"):
                    flush()
                    if word == b"mhtmltag":
                        st.mhtml = True
                        last_mhtml_param = p
                    elif skip_next_htmltag is not None and p == skip_next_htmltag:
                        st.skip = True
                        skip_next_htmltag = None
                        continue
                    else:
                        skip_next_htmltag = None
                    st.htmltag = True
                    st.htmlrtf = False
                    st.dest = word
                    continue
                st.skip = True
                st.dest = word
                continue
            if word in _SKIP_DEST:
                if word == b"fonttbl":
                    st.dest = b"fonttbl"
                    st.skip = True
                    continue
                st.skip = True
                st.dest = word
                continue
            if st.dest == b"fonttbl" or (stack and any(s.dest == b"fonttbl" for s in stack[-2:])):
                if word == b"f" and p is not None:
                    font_num = p
                    fonts.setdefault(p, None)
                elif word == b"fcharset" and font_num is not None and p is not None:
                    fonts[font_num] = rtf_fcharset_codec(p)
                elif word == b"cpg" and font_num is not None and p is not None:
                    fonts[font_num] = codec_for_cpid(p) or fonts.get(font_num)
                continue
            if word == b"ansicpg" and p:
                ansicpg = codec_for_cpid(p) or ansicpg
                continue
            if word == b"deff" and p is not None:
                deff = p
                continue
            if word == b"f" and p is not None:
                flush()
                st.font = p
                continue
            if word == b"uc" and p is not None:
                st.uc = p
                continue
            if word == b"htmlrtf":
                st.htmlrtf = not (p == 0)
                continue
            if word == b"u" and p is not None:
                flush()
                if emit_ok(st):
                    out.append(chr(p + 65536 if p < 0 else p) if 0 <= (p + 65536 if p < 0 else p) < 0x110000 else "")
                uc_skip = st.uc
                continue
            if word == b"fromhtml" or word == b"fromtext":
                continue
            if word in _SPECIAL:
                flush()
                if emit_ok(st):
                    if html_mode and word in (b"par", b"line"):
                        out.append("\r\n")
                    else:
                        out.append(_SPECIAL[word])
                continue
            if word == b"bin" and p:
                # skip binary data
                continue
            continue
        if hexb is not None:
            if uc_skip:
                uc_skip -= 1
                continue
            if emit_ok(st):
                c = codec_of(st)
                if pending and pending_codec[0] != c:
                    flush()
                pending_codec[0] = c
                pending.append(int(hexb, 16))
            continue
        if sym is not None:
            if uc_skip:
                uc_skip -= 1
                continue
            if sym in (b"\\", b"{", b"}") and pending and pending_codec[0] in _DBCS \
                    and emit_ok(st) and codec_of(st) == pending_codec[0]:
                # escaped trail byte of a double-byte character (0x5C / 0x7B / 0x7D)
                pending.append(sym[0])
                continue
            flush()
            if sym == b"*":
                expect_dest = True
                continue
            if not emit_ok(st):
                continue
            if sym in (b"\\", b"{", b"}"):
                out.append(sym.decode())
            elif sym == b"~":
                out.append("\u00a0")
            elif sym == b"_":
                out.append("\u2011")
            elif sym in (b"\r", b"\n"):
                out.append("\r\n" if html_mode else "\n")
            elif sym == b"-":
                pass
            elif sym == b"|" or sym == b":":
                pass
            continue
        if text is not None:
            if uc_skip:
                k = min(uc_skip, len(text))
                text = text[k:]
                uc_skip -= k
                if not text:
                    continue
            if st.mhtml and st.htmltag and not st.skip:
                skip_next_htmltag = last_mhtml_param
            if emit_ok(st):
                c = codec_of(st)
                if pending and pending_codec[0] != c:
                    flush()
                pending_codec[0] = c
                pending.extend(text)
            continue
    flush()
    return _join_surrogates("".join(out))


def rtf_to_html(rtf: bytes, default_codec: str | None = None) -> str | None:
    """Extract HTML encapsulated in RTF (\\fromhtml1); None if not encapsulated.

    ``default_codec`` (the message codepage) is used when the RTF has no \\ansicpg."""
    if not is_html_rtf(rtf):
        return None
    return _parse(rtf, html_mode=True, default_codec=default_codec).replace("\x00", "")


def rtf_to_text(rtf: bytes, default_codec: str | None = None) -> str:
    text = _parse(rtf, html_mode=False, default_codec=default_codec).replace("\x00", "")
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n") + "\n" if text.strip() else ""

