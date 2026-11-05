import pytest

from app.engine.rtf import decompress, rtf_to_html, rtf_to_text, RtfError

V1 = bytes.fromhex("2d0000002b0000004c5a4675f1c5c7a703000a0072637067313235423"
                   "20af32068656c090020627705b06c647d0a800fa0")
V2 = bytes.fromhex("1a0000001c0000004c5a4675e2d44b51410004205758595a0d6e7d010eb0")


def test_lzfu_spec_vectors():
    assert decompress(V1) == b"{\\rtf1\\ansi\\ansicpg1252\\pard hello world}\r\n"
    assert decompress(V2) == b"{\\rtf1 WXYZWXYZWXYZWXYZWXYZ}"


def test_lzfu_uncompressed_and_garbage():
    raw = b"{\\rtf1 abc}"
    data = (len(raw) + 12).to_bytes(4, "little") + len(raw).to_bytes(4, "little") + b"MELA" + b"\0" * 4 + raw
    assert decompress(data) == raw
    with pytest.raises(RtfError):
        decompress(b"\x00" * 20)


def test_rtf_text_accents():
    rtf = (b"{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0\\fswiss Arial;}{\\f1\\fcharset204 Arial Cyr;}}"
           b"\\f0 Caf\\'e9 \\'e0 l\\'e9t\\'e9\\par \\u8364? et \\f1 \\'cf\\'f0\\'e8\\par}")
    t = rtf_to_text(rtf)
    assert "Café à l'été".replace("'", "") in t.replace("'", "") or "Café à lété" in t
    assert "€ et При" in t


def test_rtf_fromhtml_deencapsulation():
    rtf = (b"{\\rtf1\\ansi\\ansicpg1252\\fromhtml1 \\deff0{\\fonttbl{\\f0\\fswiss Arial;}}"
           b"{\\*\\htmltag19 <html>}{\\*\\htmltag34 <body>}\\htmlrtf {\\htmlrtf0 "
           b"{\\*\\htmltag64 <p>}\\htmlrtf {\\htmlrtf0 R\\'e9sum\\'e9 \\u233? "
           b"{\\*\\htmltag84 <img src=\"cid:image001.png@01D0\">}\\htmlrtf }\\htmlrtf0 "
           b"{\\*\\htmltag72 </p>}\\htmlrtf }\\htmlrtf0 {\\*\\htmltag42 </body>}{\\*\\htmltag27 </html>}}")
    h = rtf_to_html(rtf)
    assert h.startswith("<html><body><p>Résumé é")
    assert 'cid:image001.png@01D0' in h and h.endswith("</p></body></html>")
    assert rtf_to_html(b"{\\rtf1 plain}") is None


def test_lzfu_matches_libpff_on_fixtures():
    import os
    from app.engine import PstFile, mapi
    from .conftest import FIXTURES
    n = 0
    for name in ("mtnman1965.ost", "aspose-sample.ost"):
        with PstFile(os.path.join(FIXTURES, name)) as p:
            for f in p.folders():
                for i, k, h in p.iter_items(f):
                    r = h.source().bag.raw(mapi.PR_RTF_COMPRESSED)
                    if not r:
                        continue
                    lib = h.item.rtf_body
                    assert decompress(bytes(r)).rstrip(b"\0") == bytes(lib).rstrip(b"\0")
                    n += 1
    assert n > 100
