"""Multilingual character survival: every script, every place a string can travel.

Synthetic MAPI items (like test_synthetic.py) are built in each script and
codepage, rebuilt with the real builders and re-parsed; text must come back
character-for-character identical (no NFC normalisation, no U+FFFD, no
mojibake).  Covers: subject, display names, plain/HTML/RTF bodies, transport
headers (RFC 2047, ISO-2022-JP), attachment file names (RFC 2231), vCard,
iCalendar, IMAP folder names (modified UTF-7) and the accounts CSV import.
"""
import base64
import email
import email.header
import email.policy
import email.utils
import struct
from datetime import datetime, timedelta, timezone

import icalendar
import pytest
import vobject

from app.engine import mapi
from app.engine import charset
from app.engine.charset import (codec_for_cpid, decode_bytes, decode_html, fix_mojibake, normalize,
                                rtf_fcharset_codec)
from app.engine.contact import build_contact
from app.engine.event import build_event
from app.engine.mapi import PropBag
from app.engine.message import MessageBuilder, decode_header_value
from app.engine.rtf import rtf_to_html, rtf_to_text
from app.engine.verify import check_raw
from app.server import csvimport
from app.server.imap import utf7_decode, utf7_encode

from .conftest import BIN, FT, I32, U, FakeAtt, namemap_with

PDF = b"%PDF-1.4\n" + bytes(range(256)) * 4

# name -> (text, Windows/MAPI codepage, Python codec, MIME charset label)
VIET = "Xin chào, Việt Nam, đường phố"  # cp1258 = combining tones
SAMPLES = {
    "german": ("Grüße aus München – Äpfel, Öl, Übermaß, Straße", 1252, "cp1252", "windows-1252"),
    "french": ("Réunion à 10h « budget » – œuvre, ça, Noël €", 1252, "cp1252", "windows-1252"),
    "latin1": ("Señor Muñoz, ¿qué tal? Ångström, Øresund", 28591, "latin-1", "iso-8859-1"),
    "latin9": ("Prix : 15 € - Œuvre, Šárka, Žofie", 28605, "iso8859_15", "iso-8859-15"),
    "polish": ("Zażółć gęślą jaźń – Łódź, Świętokrzyskie", 1250, "cp1250", "windows-1250"),
    "czech": ("Příliš žluťoučký kůň úpěl ďábelské ódy", 1250, "cp1250", "windows-1250"),
    "hungarian": ("Árvíztűrő tükörfúrógép – Őrség, Győr", 1250, "cp1250", "windows-1250"),
    "polish_iso": ("Zażółć gęślą jaźń - Łódź, Świętokrzyskie", 28592, "iso8859_2", "iso-8859-2"),
    "russian": ("Привет, как дела? Встреча завтра в 10:00. Ёлка, №5", 1251, "cp1251", "windows-1251"),
    "russian_koi8": ("Привет, как дела? Встреча завтра в 10:00", 20866, "koi8_r", "koi8-r"),
    "ukrainian_koi8u": ("Привіт, Україно! Їжак, ґанок, Євген", 21866, "koi8_u", "koi8-u"),
    "russian_iso": ("Привет, как дела? Встреча завтра", 28595, "iso8859_5", "iso-8859-5"),
    "greek": ("Καλημέρα σας, τι κάνετε; Ώρα συνάντησης: 10:00", 1253, "cp1253", "windows-1253"),
    "greek_iso": ("Καλημέρα σας, τι κάνετε;", 28597, "iso8859_7", "iso-8859-7"),
    "turkish": ("Günaydın, nasılsınız? Şükrü İstanbul'da, ğ", 1254, "cp1254", "windows-1254"),
    "hebrew": ("שלום עולם, מה שלומך? פגישה מחר", 1255, "cp1255", "windows-1255"),
    "arabic": ("مرحبا بالعالم، كيف حالك؟ اجتماع غدا", 1256, "cp1256", "windows-1256"),
    "baltic": ("Labdien, kā jums klājas? Ūdens, Šiauliai, ačiū, ąžuolas", 1257, "cp1257", "windows-1257"),
    "vietnamese": (VIET, 1258, "cp1258", "windows-1258"),
    "thai": ("สวัสดีครับ ภาษาไทย ประชุมพรุ่งนี้", 874, "cp874", "windows-874"),
    "japanese_sjis": ("日本語のテストです。会議は明日の１０時です。ｶﾀｶﾅ ①～", 932, "cp932", "shift_jis"),
    "japanese_eucjp": ("日本語のテストです。会議は明日の十時です。", 51932, "euc_jp", "euc-jp"),
    "japanese_jis": ("日本語のテストです。会議は明日の十時です。", 50220, "iso2022_jp", "iso-2022-jp"),
    "chinese_gbk": ("你好世界，明天开会。请确认收到。镕", 936, "gbk", "gbk"),
    "chinese_gb2312": ("你好世界，明天开会。请确认收到。", 20936, "gb2312", "gb2312"),
    "chinese_gb18030": ("你好世界，明天开会。𠮷野家 €", 54936, "gb18030", "gb18030"),
    "chinese_big5": ("你好世界，明天開會。請確認收到。", 950, "cp950", "big5"),
    "korean": ("안녕하세요. 내일 회의가 있습니다. 확인 부탁드립니다.", 949, "cp949", "ks_c_5601-1987"),
    "korean_euckr": ("안녕하세요. 내일 회의가 있습니다.", 51949, "euc_kr", "euc-kr"),
    "utf8": ("Grüße Привет 日本語 שלום مرحبا 😀 👍🏽 𝄞", 65001, "utf-8", "utf-8"),
}
EMOJI = "Emoji 😀👍🏽 👨‍👩‍👧 🇫🇷 𝄞 𠮷 ✅ — fin"
UNICODE_ONLY = dict((k, v[0]) for k, v in SAMPLES.items())
UNICODE_ONLY["emoji"] = EMOJI
UNICODE_ONLY["mixed"] = "Ünïcödé Ελληνικά Кириллица עברית العربية ไทย 中文 日本語 한국어 😀"

# codepage samples a last-resort detection must recognise without any codepage property
DETECTABLE = ["german", "french", "polish", "czech", "hungarian", "russian", "russian_koi8", "greek",
              "turkish", "hebrew", "arabic", "thai", "japanese_sjis", "japanese_eucjp", "chinese_gbk",
              "chinese_big5", "korean", "utf8"]
# Windows ANSI codepages (what Outlook really uses for PT_STRING8)
ANSI = [k for k, v in SAMPLES.items() if v[1] in (874, 932, 936, 949, 950, 1250, 1251, 1252, 1253,
                                                    1254, 1255, 1256, 1257, 1258, 65001)]


def _parse(raw):
    return email.message_from_bytes(raw, policy=email.policy.default)


def S8(s, codec):
    return (mapi.PT_STRING8, s.encode(codec) + b"\x00")


class Msg:
    """FakeMsg whose recipients / attachments inherit the message codepage (as source.PffMessage)."""

    def __init__(self, props, recipients=(), attachments=(), namemap=None, default_codepage="cp1252"):
        self.bag = PropBag.from_dict(props, namemap, default_codepage=default_codepage)
        self._r = [PropBag.from_dict(r, namemap, codepage=self.bag.codepage,
                                     default_codepage=default_codepage) for r in recipients]
        self._a = []
        for a in attachments:
            a.bag = PropBag.from_dict({t: (p.vtype, p.data) for t, p in a.bag.props.items()}, namemap,
                                      codepage=self.bag.codepage, default_codepage=default_codepage)
            self._a.append(a)
        self.identifier = 1

    def recipients(self):
        return self._r

    def attachments(self):
        yield from self._a


def _assert_clean_utf8(raw: bytes):
    head = raw.split(b"\r\n\r\n", 1)[0]
    assert head.isascii(), "non-ASCII header bytes"
    m = _parse(raw)
    for part in m.walk():
        if part.get_content_maintype() == "text":
            assert part.get_content_charset() == "utf-8"
            payload = part.get_payload(decode=True)
            payload.decode("utf-8")           # strict
            assert "�".encode() not in payload
            if part.get_content_subtype() == "html":
                low = payload.lower()
                assert b"charset=utf-8" in low.replace(b'"', b"") or b"charset" not in low
    return m


def _build(src):
    built = MessageBuilder("ml-1").build_raw(src)
    assert check_raw(built.raw, built.expect) == [], built.warnings
    assert "fallback" not in built.warnings, built.warnings
    m = _assert_clean_utf8(built.raw)
    m.raw_bytes = built.raw
    return built, m


def _name(m, header):
    """First display name of an address header, decoded as RFC 2047 says.

    CPython's structured-header parser keeps the folding whitespace between two
    adjacent encoded words of a phrase, which RFC 2047 section 6.2 says to drop
    (and real clients drop): decode the raw header with email.header instead."""
    v = email.message_from_bytes(m.raw_bytes)[header]
    name = email.utils.getaddresses([v])[0][0]
    return str(email.header.make_header(email.header.decode_header(name)))


def _plain(m):
    return m.get_body(("plain",)).get_content().replace("\r\n", "\n")


def _htmlpart(m):
    return m.get_body(("html",)).get_content().replace("\r\n", "\n")


def _short(t, n=40):
    return t[:n].strip(" ,.;:–")


# =============================================================================
# codepage table
CPID_EXPECT = {
    874: "cp874", 932: "cp932", 936: "gb18030", 949: "cp949", 950: "cp950", 1250: "cp1250",
    1251: "cp1251", 1252: "cp1252", 1253: "cp1253", 1254: "cp1254", 1255: "cp1255", 1256: "cp1256",
    1257: "cp1257", 1258: "cp1258", 20866: "koi8-r", 21866: "koi8-u", 20932: "euc_jp", 20936: "gb18030",
    28591: "cp1252", 28592: "iso8859-2", 28593: "iso8859-3", 28594: "iso8859-4", 28595: "iso8859-5",
    28596: "iso8859-6", 28597: "iso8859-7", 28598: "iso8859-8", 28599: "cp1254", 28600: "iso8859-10",
    28601: "cp874", 28603: "iso8859-13", 28604: "iso8859-14", 28605: "iso8859-15", 28606: "iso8859-16",
    50220: "iso2022_jp", 50221: "iso2022_jp_ext", 50222: "iso2022_jp", 50225: "iso2022_kr",
    51932: "euc_jp", 51936: "gb18030", 51949: "cp949", 52936: "hz", 54936: "gb18030", 65000: "utf-7",
    65001: "utf-8", 1200: "utf-16-le", 1201: "utf-16-be", 10000: "mac-roman", 10007: "mac-cyrillic",
}


@pytest.mark.parametrize("cpid,codec", sorted(CPID_EXPECT.items()))
def test_codepage_to_codec(cpid, codec):
    assert codec_for_cpid(cpid) == codec


@pytest.mark.parametrize("label,codec", [
    ("windows-1251", "cp1251"), ("WINDOWS-1256", "cp1256"), ("iso-8859-2", "iso8859-2"),
    ("ISO-8859-8-I", "iso8859-8"), ("koi8-r", "koi8-r"), ("koi8-u", "koi8-u"), ("shift_jis", "cp932"),
    ("x-sjis", "cp932"), ("Shift-JIS", "cp932"), ("euc-jp", "euc_jp"), ("x-euc-jp", "euc_jp"),
    ("iso-2022-jp", "iso2022_jp"), ("gb2312", "gb18030"), ("GBK", "gb18030"), ("gb18030", "gb18030"),
    ("big5", "cp950"), ("ks_c_5601-1987", "cp949"), ("euc-kr", "cp949"), ("tis-620", "cp874"),
    ("windows-874", "cp874"), ("utf8", "utf-8"), ("UTF-8", "utf-8"), ("iso-8859-1", "cp1252"),
    ("us-ascii", "cp1252"), ("iso-8859-9", "cp1254"), ("x-mac-cyrillic", "mac-cyrillic"),
])
def test_charset_label_normalisation(label, codec):
    assert normalize(label) == codec


@pytest.mark.parametrize("fcharset,codec", [(128, "cp932"), (129, "cp949"), (134, "gb18030"),
                                            (136, "cp950"), (161, "cp1253"), (162, "cp1254"),
                                            (163, "cp1258"), (177, "cp1255"), (178, "cp1256"),
                                            (186, "cp1257"), (204, "cp1251"), (222, "cp874"),
                                            (238, "cp1250")])
def test_rtf_fcharset_table(fcharset, codec):
    assert rtf_fcharset_codec(fcharset) == codec


def test_cp950_vendor_characters():
    # 0xA145 is U+2027 in Windows cp950 (big5hkscs says U+2022); 0xA3E1 is the euro sign
    assert decode_bytes(b"\xa1\x45\xa3\xe1", ["windows-950"]) == "‧€"
    assert decode_bytes(b"\xa1\x45", ["big5"]) == "‧"


# =============================================================================
# Unicode properties: everything must be byte exact
@pytest.mark.parametrize("name", sorted(UNICODE_ONLY))
def test_unicode_message_every_field(name):
    text = UNICODE_ONLY[name]
    fname = _short(text, 30) + ".pdf"
    html = "<html><head><meta charset=\"utf-16\"></head><body><p>%s</p></body></html>" % text
    src = Msg({
        mapi.PR_MESSAGE_CLASS: U("IPM.Note"), mapi.PR_SUBJECT: U(text),
        mapi.PR_BODY: U(text + "\n" + text), mapi.PR_HTML: (mapi.PT_UNICODE, html.encode("utf-16-le")),
        mapi.PR_SENDER_NAME: U(_short(text, 25)), mapi.PR_SENDER_EMAIL: U("from@example.org"),
        mapi.PR_SENDER_ADDRTYPE: U("SMTP"),
    }, recipients=[{mapi.PR_DISPLAY_NAME: U(_short(text, 20)), mapi.PR_EMAIL_ADDRESS: U("to@example.org"),
                    mapi.PR_ADDRTYPE: U("SMTP"), mapi.PR_RECIPIENT_TYPE: I32(1)},
                   {mapi.PR_DISPLAY_NAME: U(_short(text, 15) + ", Jr"), mapi.PR_EMAIL_ADDRESS: U("cc@example.org"),
                    mapi.PR_ADDRTYPE: U("SMTP"), mapi.PR_RECIPIENT_TYPE: I32(2)}],
        attachments=[FakeAtt({mapi.PR_ATTACH_METHOD: I32(1), mapi.PR_ATTACH_LONG_FILENAME: U(fname)}, PDF)])
    built, m = _build(src)
    assert m["subject"] == text
    assert _name(m, "from") == _short(text, 25)
    assert _name(m, "to") == _short(text, 20)
    assert _name(m, "cc") == _short(text, 15) + ", Jr"
    assert _plain(m).rstrip("\n") == text + "\n" + text
    assert text in _htmlpart(m)
    att = list(m.iter_attachments())
    assert [a.get_filename() for a in att] == [fname]
    assert att[0].get_content() == PDF


# =============================================================================
# PT_STRING8 + PR_MESSAGE_CODEPAGE
def _string8_msg(text, codec, props_cp):
    fname = _short(text, 30) + ".txt"
    props = {
        mapi.PR_MESSAGE_CLASS: S8("IPM.Note", "ascii"), mapi.PR_SUBJECT: S8(text, codec),
        mapi.PR_BODY: S8(text, codec), mapi.PR_SENDER_NAME: S8(_short(text, 25), codec),
        mapi.PR_SENDER_EMAIL: S8("from@example.org", "ascii"), mapi.PR_SENDER_ADDRTYPE: S8("SMTP", "ascii"),
    }
    props.update(props_cp)
    rec = [{mapi.PR_DISPLAY_NAME: S8(_short(text, 20), codec), mapi.PR_EMAIL_ADDRESS: S8("to@example.org", "ascii"),
            mapi.PR_ADDRTYPE: S8("SMTP", "ascii"), mapi.PR_RECIPIENT_TYPE: I32(1)}]
    att = [FakeAtt({mapi.PR_ATTACH_METHOD: I32(1), mapi.PR_ATTACH_LONG_FILENAME: S8(fname, codec)}, PDF)]
    return Msg(props, rec, att), fname


def _check_string8(text, src, fname):
    _, m = _build(src)
    assert m["subject"] == text
    assert _name(m, "from") == _short(text, 25)
    assert _name(m, "to") == _short(text, 20)
    assert _plain(m).rstrip("\n") == text
    assert [a.get_filename() for a in m.iter_attachments()] == [fname]


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_string8_with_message_codepage(name):
    text, cpid, codec, _ = SAMPLES[name]
    src, fname = _string8_msg(text, codec, {mapi.PR_MESSAGE_CODEPAGE: I32(cpid)})
    _check_string8(text, src, fname)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_string8_with_internet_cpid_only(name):
    """No PR_MESSAGE_CODEPAGE: PR_INTERNET_CPID gives the ANSI codepage (iso-2022-jp -> cp932...)."""
    text, cpid, codec, _ = SAMPLES[name]
    ansi = {50220: "cp932", 51932: "cp932", 51949: "cp949", 20866: "cp1251", 21866: None, 28595: "cp1251",
            28592: "cp1250", 28597: "cp1253", 20936: "gb18030", 28591: "cp1252", 28605: "cp1252"}.get(cpid, codec)
    if ansi is None:
        pytest.skip("koi8-u has no Windows ANSI equivalent")
    try:
        text.encode(ansi)
    except UnicodeEncodeError:
        pytest.skip("sample not representable in the ANSI codepage")
    src, fname = _string8_msg(text, ansi, {mapi.PR_INTERNET_CPID: I32(cpid)})
    _check_string8(text, src, fname)


@pytest.mark.parametrize("name", DETECTABLE)
def test_string8_without_any_codepage_is_detected(name):
    """Neither PR_MESSAGE_CODEPAGE nor PR_INTERNET_CPID: the cp1252 store default must not win
    over an obviously Cyrillic / Greek / CJK... item."""
    text, cpid, codec, _ = SAMPLES[name]
    src, fname = _string8_msg(text, codec, {})
    _check_string8(text, src, fname)


@pytest.mark.parametrize("name", ["russian", "greek", "japanese_sjis", "chinese_gbk", "korean"])
def test_store_default_codepage_is_used(name):
    text, cpid, codec, _ = SAMPLES[name]
    props = {mapi.PR_SUBJECT: S8(text[:6], codec), mapi.PR_MESSAGE_CLASS: S8("IPM.Note", "ascii")}
    bag = PropBag.from_dict(props, default_codepage=codec)
    assert bag.string(mapi.PR_SUBJECT) == text[:6]


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_guess_codec_never_breaks_explicit_default(name):
    text, cpid, codec, _ = SAMPLES[name]
    data = text.encode(codec)
    g = charset.guess_codec(data, [codec], "cp1252")
    if g is None:
        assert data.isascii() and decode_bytes(data, [codec]) == text
    else:
        assert data.decode(g) == text


def test_unicode_codepage_ignored_for_string8():
    bag = PropBag.from_dict({mapi.PR_MESSAGE_CODEPAGE: I32(1200),
                             mapi.PR_SUBJECT: S8("Привет мир, как дела", "cp1251")})
    assert bag.string(mapi.PR_SUBJECT) == "Привет мир, как дела"


# =============================================================================
# HTML bodies
def _html_msg(body_bytes, cpid=None, mcp=None):
    props = {mapi.PR_MESSAGE_CLASS: U("IPM.Note"), mapi.PR_SUBJECT: U("html"), mapi.PR_HTML: BIN(body_bytes)}
    if cpid:
        props[mapi.PR_INTERNET_CPID] = I32(cpid)
    if mcp:
        props[mapi.PR_MESSAGE_CODEPAGE] = I32(mcp)
    return Msg(props)


def _html(text, label):
    meta = '<meta http-equiv="Content-Type" content="text/html; charset=%s">' % label if label else ""
    return "<html><head>%s</head><body><p>%s</p><p>%s</p></body></html>" % (meta, text, text)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_html_meta_and_cpid_agree(name):
    text, cpid, codec, label = SAMPLES[name]
    _, m = _build(_html_msg(_html(text, label).encode(codec), cpid))
    h = _htmlpart(m)
    assert h.count(text) == 2 and "charset=utf-8" in h


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_html_meta_only(name):
    text, cpid, codec, label = SAMPLES[name]
    _, m = _build(_html_msg(_html(text, label).encode(codec)))
    assert _htmlpart(m).count(text) == 2


@pytest.mark.parametrize("name", [k for k in sorted(SAMPLES) if SAMPLES[k][1] != 1252])
def test_html_meta_beats_weak_cp1252_cpid(name):
    """PR_INTERNET_CPID says 1252 but the HTML declares (and is) koi8-r / Shift_JIS / ..."""
    text, cpid, codec, label = SAMPLES[name]
    _, m = _build(_html_msg(_html(text, label).encode(codec), 1252))
    assert _htmlpart(m).count(text) == 2


@pytest.mark.parametrize("name", [k for k in sorted(SAMPLES) if SAMPLES[k][2] not in ("cp1252", "latin-1", "utf-8")])
def test_html_cpid_beats_stale_meta(name):
    """The body was re-encoded by Outlook to PR_INTERNET_CPID; the meta still says iso-8859-1."""
    text, cpid, codec, label = SAMPLES[name]
    _, m = _build(_html_msg(_html(text, "iso-8859-1").encode(codec), cpid))
    assert _htmlpart(m).count(text) == 2


@pytest.mark.parametrize("name", DETECTABLE)
def test_html_without_any_declaration(name):
    text, cpid, codec, label = SAMPLES[name]
    _, m = _build(_html_msg(_html(text, None).encode(codec)))
    assert _htmlpart(m).count(text) == 2


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_html_message_codepage_fallback(name):
    text, cpid, codec, label = SAMPLES[name]
    assert decode_html(_html(text, None).encode(codec), None, normalize(codec))[0].count(text) == 2


def test_html_iso2022jp_is_ascii_but_decoded():
    text = SAMPLES["japanese_jis"][0]
    data = _html(text, "iso-2022-jp").encode("iso2022_jp")
    assert data.isascii()
    assert decode_html(data, None)[0].count(text) == 2
    assert decode_html(data, 50220)[0].count(text) == 2


# =============================================================================
# RTF bodies
def _rtf_escape_bytes(b: bytes) -> bytes:
    out = bytearray()
    for x in b:
        if x >= 0x80:
            out += b"\\'%02x" % x
        elif x in (0x5C, 0x7B, 0x7D):
            out += b"\\" + bytes([x])
        else:
            out.append(x)
    return bytes(out)


def _rtf_u(text: str) -> bytes:
    out = bytearray()
    for unit in struct.unpack("<%dh" % (len(text.encode("utf-16-le")) // 2), text.encode("utf-16-le")):
        if 0x20 <= unit < 0x80 and unit not in (0x5C, 0x7B, 0x7D):
            out.append(unit)
        else:
            out += b"\\u%d?" % unit       # signed 16-bit, as Word/Outlook write them
    return bytes(out)


def _mela(rtf: bytes) -> bytes:
    return (len(rtf) + 12).to_bytes(4, "little") + len(rtf).to_bytes(4, "little") + b"MELA" + b"\0" * 4 + rtf


WINCP = [k for k in sorted(SAMPLES) if SAMPLES[k][1] in (874, 932, 936, 949, 950, 1250, 1251, 1252, 1253,
                                                         1254, 1255, 1256, 1257, 1258)]


@pytest.mark.parametrize("name", WINCP)
def test_rtf_ansicpg_hex_escapes(name):
    text, cpid, codec, _ = SAMPLES[name]
    rtf = b"{\\rtf1\\ansi\\ansicpg%d\\deff0{\\fonttbl{\\f0\\fswiss Arial;}}\\f0 %s\\par }" % (
        cpid, _rtf_escape_bytes(text.encode(codec)))
    assert rtf_to_text(rtf).strip("\n") == text
    _, m = _build(Msg({mapi.PR_MESSAGE_CLASS: U("IPM.Note"), mapi.PR_RTF_COMPRESSED: BIN(_mela(rtf))}))
    assert _plain(m).strip("\n") == text


@pytest.mark.parametrize("name", WINCP)
def test_rtf_fcharset_font_overrides_ansicpg(name):
    """\\ansicpg1252 document, the text run uses a font declared with \\fcharsetN."""
    text, cpid, codec, _ = SAMPLES[name]
    fch = {874: 222, 932: 128, 936: 134, 949: 129, 950: 136, 1250: 238, 1251: 204, 1252: 0, 1253: 161,
           1254: 162, 1255: 177, 1256: 178, 1257: 186, 1258: 163}[cpid]
    rtf = (b"{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0\\fswiss\\fcharset0 Arial;}"
           b"{\\f1\\fnil\\fcharset%d Font;}}\\f0 A \\f1 %s\\f0  Z\\par }" % (fch, _rtf_escape_bytes(text.encode(codec))))
    assert rtf_to_text(rtf).strip("\n") == "A " + text + " Z"


@pytest.mark.parametrize("name", sorted(UNICODE_ONLY))
def test_rtf_unicode_escapes_with_surrogates(name):
    text = UNICODE_ONLY[name]
    rtf = b"{\\rtf1\\ansi\\ansicpg1252\\uc1 %s\\par }" % _rtf_u(text)
    assert rtf_to_text(rtf).strip("\n") == text
    _, m = _build(Msg({mapi.PR_MESSAGE_CLASS: U("IPM.Note"), mapi.PR_RTF_COMPRESSED: BIN(_mela(rtf))}))
    assert _plain(m).strip("\n") == text


@pytest.mark.parametrize("name", WINCP)
def test_rtf_encapsulated_html(name):
    text, cpid, codec, label = SAMPLES[name]
    esc = _rtf_escape_bytes(text.encode(codec))
    rtf = (b"{\\rtf1\\ansi\\ansicpg%d\\fromhtml1 \\deff0{\\fonttbl{\\f0\\fswiss Arial;}}"
           b"{\\*\\htmltag64 <html><head><meta charset=\"%s\"></head><body>}"
           b"{\\*\\htmltag84 <p>}\\htmlrtf {\\htmlrtf0 %s\\htmlrtf }\\htmlrtf0 {\\*\\htmltag92 </p>}"
           b"{\\*\\htmltag84 <p>}%s{\\*\\htmltag92 </p>}"
           b"{\\*\\htmltag72 </body></html>}}" % (cpid, label.encode(), esc, esc))
    h = rtf_to_html(rtf)
    assert h.count(text) == 2
    _, m = _build(Msg({mapi.PR_MESSAGE_CLASS: U("IPM.Note"), mapi.PR_RTF_COMPRESSED: BIN(_mela(rtf))}))
    out = _htmlpart(m)
    assert out.count(text) == 2 and "charset=\"utf-8\"" in out


@pytest.mark.parametrize("name", ["japanese_sjis", "chinese_gbk", "chinese_big5", "korean"])
def test_rtf_dbcs_unicode_with_dbcs_fallback(name):
    """\\uc2 \\uN followed by the two-byte ANSI fallback (Word style)."""
    text, cpid, codec, _ = SAMPLES[name]
    body = bytearray()
    for ch in text:
        fb = ch.encode(codec)
        if ord(ch) < 0x80:
            body += _rtf_escape_bytes(fb)
            continue
        unit = struct.unpack("<h", ch.encode("utf-16-le")[:2])[0] if ord(ch) <= 0xFFFF else None
        if unit is None or len(fb) != 2:
            body += _rtf_escape_bytes(fb)
            continue
        body += b"{\\uc2\\u%d%s}" % (unit, _rtf_escape_bytes(fb))
    rtf = b"{\\rtf1\\ansi\\ansicpg%d %s\\par }" % (cpid, bytes(body))
    assert rtf_to_text(rtf).strip("\n") == text


def test_rtf_dbcs_trail_byte_escaped():
    # 0x5C / 0x7B / 0x7D are valid Shift_JIS / GBK / Big5 trail bytes, written \\ \{ \}
    for text, codec, cpid in (("表示ソフト能力", "cp932", 932), ("功能", "cp950", 950)):
        rtf = b"{\\rtf1\\ansi\\ansicpg%d %s\\par }" % (cpid, _rtf_escape_bytes(text.encode(codec)))
        assert rtf_to_text(rtf).strip("\n") == text


def test_rtf_without_ansicpg_uses_message_codepage():
    text = SAMPLES["russian"][0]
    rtf = b"{\\rtf1\\ansi %s\\par }" % _rtf_escape_bytes(text.encode("cp1251"))
    assert rtf_to_text(rtf, "cp1251").strip("\n") == text
    _, m = _build(Msg({mapi.PR_MESSAGE_CLASS: U("IPM.Note"), mapi.PR_MESSAGE_CODEPAGE: I32(1251),
                       mapi.PR_RTF_COMPRESSED: BIN(_mela(rtf))}))
    assert _plain(m).strip("\n") == text


# =============================================================================
# transport headers (RFC 2047, stateful encodings, mojibake repair)
def _b_words(text, label, codec, chunk=9):
    """RFC 2047 B words; the bytes are split at arbitrary offsets (mid character)."""
    b = text.encode(codec)
    return " ".join("=?%s?B?%s?=" % (label, base64.b64encode(b[i:i + chunk]).decode())
                    for i in range(0, len(b), chunk))


def _q_word(text, label, codec):
    b = text.encode(codec)
    return "=?%s?Q?%s?=" % (label, "".join(chr(x) if 0x30 <= x < 0x7F and x not in (0x3D, 0x3F, 0x5F)
                                           else ("_" if x == 0x20 else "=%02X" % x) for x in b))


def _hdr_msg(headers):
    return Msg({mapi.PR_MESSAGE_CLASS: U("IPM.Note"), mapi.PR_BODY: U("x"),
                mapi.PR_TRANSPORT_MESSAGE_HEADERS: U(headers)})


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_transport_headers_rfc2047(name):
    text, cpid, codec, label = SAMPLES[name]
    if codec == "iso2022_jp":
        b_subject = "=?%s?B?%s?=" % (label, base64.b64encode(text.encode(codec)).decode())
    else:
        b_subject = _b_words(text, label, codec)
    disp = _short(text, 20)
    hdrs = ("Subject: Re: %s\r\nFrom: %s <from@example.org>\r\nTo: %s <to@example.org>\r\n"
            "Date: Mon, 4 Mar 2024 10:00:00 +0100\r\nMessage-ID: <a@b>\r\n\r\n") % (
        b_subject, _q_word(disp, label, codec), _q_word(disp, label, codec))
    _, m = _build(_hdr_msg(hdrs))
    assert m["subject"] == "Re: " + text
    assert _name(m, "from") == disp
    assert _name(m, "to") == disp


@pytest.mark.parametrize("name", sorted(UNICODE_ONLY))
def test_transport_headers_raw_unicode_next_to_encoded_word(name):
    """Raw (already decoded) text next to an encoded word must not become \\uXXXX escapes."""
    text = UNICODE_ONLY[name]
    v = "%s =?utf-8?B?%s?= fin" % (text, base64.b64encode(text.encode()).decode())
    assert decode_header_value(v) == "%s %s fin" % (text, text)
    _, m = _build(_hdr_msg('Subject: %s\r\nFrom: "%s" <f@example.org>\r\n\r\n' % (v, _short(text, 20))))
    assert m["subject"] == "%s %s fin" % (text, text)
    assert _name(m, "from") == _short(text, 20)


def test_transport_header_raw_iso2022jp():
    text = SAMPLES["japanese_jis"][0]
    raw = text.encode("iso2022_jp").decode("ascii")
    assert decode_header_value(raw) == text
    _, m = _build(_hdr_msg("Subject: %s\r\nFrom: %s <f@example.org>\r\n\r\n" % (raw, raw)))
    assert m["subject"] == text
    assert _name(m, "from") == text


def test_rfc2047_iso2022jp_split_words():
    text = "日本語のテスト"
    enc = text.encode("iso2022_jp")
    # Two words that only make sense together (escape sequence state carried over)
    w = " ".join("=?ISO-2022-JP?B?%s?=" % base64.b64encode(p).decode() for p in (enc[:9], enc[9:]))
    assert decode_header_value(w) == text


@pytest.mark.parametrize("legit", [
    "Gruß“", "Er sagte „Grüße“", "voilà »", "Il a dit café » fin", "SÃO PAULO", "NÃO",
    "Ålesund – Oslo", "Øst–Vest", "Привет, мир", "日本語のテスト", "3×² = 9", "Straße”", "Ä€ 5",
    "Ðóra Ñandú", "Kıbrıs Ç Ş", "Mañana — ¿sí?", "Œuvre ™", "â€ž", "Ï€ ≈ 3.14",
])
def test_mojibake_repair_leaves_legit_text_alone(legit):
    assert fix_mojibake(legit) == legit or legit in ("â€ž", "Ï€ ≈ 3.14")
    assert decode_header_value("=?utf-8?B?%s?=" % base64.b64encode(legit.encode()).decode()) \
        in (legit, fix_mojibake(legit))


@pytest.mark.parametrize("good", ["été à Noël", "Grüße", "Привет, мир", "Καλημέρα", "שלום עולם",
                                  "日本語のテスト", "中文测试", "한국어 테스트", "Ž Š č"])
def test_mojibake_repair_fixes_utf8_read_as_cp1252(good):
    from app.engine.charset import _lenient_cp1252
    broken = _lenient_cp1252(good.encode("utf-8"))
    assert fix_mojibake(broken) == good
    assert fix_mojibake(good) == good


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_rfc2047_legit_text_unchanged(name):
    text, cpid, codec, label = SAMPLES[name]
    assert decode_header_value(_q_word(text, label, codec)) == text


# =============================================================================
# attachment file names (RFC 2231)
@pytest.mark.parametrize("name", sorted(UNICODE_ONLY))
def test_attachment_filename_rfc2231(name):
    text = UNICODE_ONLY[name]
    fname = text.replace(" ", "_")[:60] + ".docx"
    src = Msg({mapi.PR_MESSAGE_CLASS: U("IPM.Note"), mapi.PR_BODY: U("x")},
              attachments=[FakeAtt({mapi.PR_ATTACH_METHOD: I32(1), mapi.PR_ATTACH_LONG_FILENAME: U(fname)}, PDF)])
    built, m = _build(src)
    att = list(m.iter_attachments())
    assert [a.get_filename() for a in att] == [fname]
    assert b"filename*" in built.raw or fname.isascii()


# =============================================================================
# vCard
@pytest.mark.parametrize("name,kind", [(n, "unicode") for n in sorted(UNICODE_ONLY)] +
                         [(n, "string8") for n in sorted(SAMPLES)])
def test_vcard_fields(name, kind):
    AD = mapi.PSETID_ADDRESS
    if kind == "unicode":
        text = UNICODE_ONLY[name]
        enc = U
        cp = {}
    else:
        text, cpid, codec, _ = SAMPLES[name]

        def enc(s):
            return S8(s, codec)
        cp = {mapi.PR_MESSAGE_CODEPAGE: I32(cpid)}
    nm = namemap_with({(AD, mapi.LID_WORK_STREET): 0x8004, (AD, mapi.LID_WORK_CITY): 0x8005,
                       (AD, mapi.LID_EMAIL1_ADDRESS): 0x8001})
    given, family = _short(text, 12), _short(text[12:], 14) or "X"
    props = {mapi.PR_MESSAGE_CLASS: U("IPM.Contact"), mapi.PR_DISPLAY_NAME: enc(given + " " + family),
             mapi.PR_GIVEN_NAME: enc(given), mapi.PR_SURNAME: enc(family), mapi.PR_COMPANY_NAME: enc(text),
             mapi.PR_TITLE: enc(_short(text, 30)), 0x8004: enc(text), 0x8005: enc(_short(text, 10)),
             0x8001: U("c@example.org"), mapi.PR_BODY: enc(text + "\r\n" + text)}
    props.update(cp)
    summary, vcf, _ = build_contact(Msg(props, namemap=nm), "uid-1")
    for line in vcf.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    vcf.encode("utf-8")
    v = vobject.readOne(vcf)
    assert v.fn.value == given + " " + family
    assert v.n.value.given == given and v.n.value.family == family
    assert v.org.value[0] == text
    assert v.title.value == _short(text, 30)
    assert v.adr.value.street == text and v.adr.value.city == _short(text, 10)
    assert v.note.value == text + "\n" + text


# =============================================================================
# iCalendar
@pytest.mark.parametrize("name,kind", [(n, "unicode") for n in sorted(UNICODE_ONLY)] +
                         [(n, "string8") for n in sorted(SAMPLES)])
def test_ics_fields(name, kind):
    A = mapi.PSETID_APPOINTMENT
    if kind == "unicode":
        text = UNICODE_ONLY[name]
        enc = U
        cp = {}
    else:
        text, cpid, codec, _ = SAMPLES[name]

        def enc(s):
            return S8(s, codec)
        cp = {mapi.PR_MESSAGE_CODEPAGE: I32(cpid)}
    nm = namemap_with({(A, mapi.LID_START_WHOLE): 0x8101, (A, mapi.LID_END_WHOLE): 0x8102,
                       (A, mapi.LID_LOCATION): 0x8103})
    start = datetime(2024, 3, 4, 9, 0, tzinfo=timezone.utc)
    cn = _short(text, 18)
    props = {mapi.PR_MESSAGE_CLASS: U("IPM.Appointment"), mapi.PR_SUBJECT: enc(text),
             0x8101: FT(start), 0x8102: FT(start + timedelta(hours=1)), 0x8103: enc(_short(text, 30)),
             mapi.PR_BODY: enc(text + "\r\n" + text),
             mapi.PR_SENT_REPRESENTING_NAME: enc(cn), mapi.PR_SENT_REPRESENTING_EMAIL: U("org@example.org"),
             mapi.PR_SENT_REPRESENTING_ADDRTYPE: U("SMTP")}
    props.update(cp)
    rec = [{mapi.PR_DISPLAY_NAME: enc(cn + ", 2"), mapi.PR_EMAIL_ADDRESS: U("att@example.org"),
            mapi.PR_ADDRTYPE: U("SMTP"), mapi.PR_RECIPIENT_TYPE: I32(1)}]
    summary, ics, _ = build_event(Msg(props, rec, namemap=nm), "uid-ev")
    raw = ics.encode("utf-8") if isinstance(ics, str) else ics
    for line in raw.split(b"\r\n"):
        assert len(line) <= 75
        line.decode("utf-8")              # never folded inside a UTF-8 sequence
    cal = icalendar.Calendar.from_ical(raw)
    ev = [c for c in cal.walk("VEVENT")][0]
    assert str(ev["summary"]) == text
    assert str(ev["location"]) == _short(text, 30)
    assert str(ev["description"]) == text + "\n" + text
    assert ev["organizer"].params["CN"] == cn
    att = ev["attendee"] if not isinstance(ev["attendee"], list) else ev["attendee"][0]
    assert att.params["CN"] == cn + ", 2"


# =============================================================================
# IMAP folder names (RFC 3501 modified UTF-7)
@pytest.mark.parametrize("name,expected", [
    ("~peter/mail/台北/日本語", "~peter/mail/&U,BTFw-/&ZeVnLIqe-"),
    ("日本語", "&ZeVnLIqe-"), ("Привет", "&BB8EQAQ4BDIENQRC-"), ("😀", "&2D3eAA-"),
    ("A&B", "A&-B"), ("Éléments envoyés", "&AMk-l&AOk-ments envoy&AOk-s"),
])
def test_modified_utf7_vectors(name, expected):
    assert utf7_encode(name) == expected
    assert utf7_decode(expected) == name


@pytest.mark.parametrize("name", sorted(UNICODE_ONLY))
def test_modified_utf7_roundtrip(name):
    text = UNICODE_ONLY[name]
    for folder in (text, "INBOX/" + text, text + " & co", "&" + text + "&", text.replace(" ", "")):
        enc = utf7_encode(folder)
        assert enc.isascii() and all(0x20 <= ord(c) <= 0x7E for c in enc)
        assert utf7_decode(enc) == folder


# =============================================================================
# CSV import of accounts
CSV_TEXTS = [v[0] for v in SAMPLES.values()] + [EMOJI]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"])
@pytest.mark.parametrize("idx", range(len(CSV_TEXTS)))
def test_csv_import_unicode(idx, encoding):
    text = CSV_TEXTS[idx].replace(",", " ").replace(";", " ")[:40].strip()
    data = ("email;password;login\r\nuser@example.org;%s;%s\r\n" % (text, text[::-1])).encode(encoding)
    rows, errors = csvimport.parse(data)
    assert errors == []
    assert rows[0]["password"] == text and rows[0]["login"] == text[::-1]


def test_csv_import_quoted_separators_and_tabs():
    data = 'email\tpassword\r\nuser@example.org\t"Пароль; с, разделителями"\r\n'.encode("utf-16")
    rows, errors = csvimport.parse(data)
    assert errors == [] and rows[0]["password"] == "Пароль; с, разделителями"


@pytest.mark.parametrize("name", ["german", "french", "latin1"])
def test_csv_import_cp1252(name):
    text = SAMPLES[name][0].replace(",", " ")[:30].strip()
    try:
        data = ("user@example.org;%s\r\n" % text).encode("cp1252")
    except UnicodeEncodeError:
        pytest.skip("not cp1252")
    rows, errors = csvimport.parse(data)
    assert rows[0]["password"] == text
