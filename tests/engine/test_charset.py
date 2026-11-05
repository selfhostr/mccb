from app.engine import mapi
from app.engine.charset import decode_html, fix_mojibake, force_html_utf8, normalize
from app.engine.mapi import PropBag
from app.engine.message import decode_header_value, MessageBuilder
from app.engine.verify import check_raw

from .conftest import FakeMsg, I32, S8, U, BIN


def test_html_cp1252_accents_come_out_utf8():
    html = "<html><head><meta http-equiv='Content-Type' content='text/html; charset=windows-1252'>" \
           "</head><body>é à è ç — œ €</body></html>"
    raw = html.encode("cp1252")
    text, cs = decode_html(raw, 1252)
    assert "é à è ç — œ €" in text and cs == "cp1252"
    # full message: HTML in cp1252 with PR_INTERNET_CPID=1252 -> UTF-8 MIME part
    msg = FakeMsg({mapi.PR_SUBJECT: U("Réunion à Évry"), mapi.PR_HTML: BIN(raw),
                   mapi.PR_INTERNET_CPID: I32(1252), mapi.PR_MESSAGE_CLASS: U("IPM.Note")})
    built = MessageBuilder("abc-1").build_raw(msg)
    assert check_raw(built.raw, built.expect) == []
    import email, email.policy
    m = email.message_from_bytes(built.raw, policy=email.policy.default)
    assert m["subject"] == "Réunion à Évry"
    part = m.get_body(("html",))
    assert part.get_content_charset() == "utf-8"
    body = part.get_content()
    assert "é à è ç — œ €" in body
    assert "charset=utf-8" in body and "windows-1252" not in body
    assert "�" not in body


def test_html_without_cpid_uses_meta_then_cp1252():
    raw = "<meta charset=iso-8859-15><p>ça coûte 5 €</p>".encode("iso8859_15")
    assert decode_html(raw, None)[0].endswith("ça coûte 5 €</p>")
    raw = "<p>déjà</p>".encode("cp1252")
    assert decode_html(raw, None)[0] == "<p>déjà</p>"


def test_utf8_html_mislabelled_ascii():
    raw = "<p>Grüße é</p>".encode("utf-8")
    assert decode_html(raw, 20127)[0] == "<p>Grüße é</p>"


def test_string8_uses_message_codepage():
    bag = PropBag.from_dict({mapi.PR_SUBJECT: S8("Привет", "cp1251"), mapi.PR_MESSAGE_CODEPAGE: I32(1251)})
    assert bag.string(mapi.PR_SUBJECT) == "Привет"
    bag = PropBag.from_dict({mapi.PR_SUBJECT: S8("Électricité")})
    assert bag.string(mapi.PR_SUBJECT) == "Électricité"


def test_rfc2047_redecoding():
    assert decode_header_value("=?iso-8859-1?Q?R=E9union?= =?iso-8859-1?Q?_=E0_10h?=") == "Réunion à 10h"
    # mislabelled: declared utf-8 but bytes are cp1252
    assert decode_header_value("=?utf-8?Q?caf=E9?=") == "café"
    assert decode_header_value("Re: =?UTF-8?B?w6ljaGFuZ2U=?= ok") == "Re: échange ok"
    assert fix_mojibake("CafÃ© Ã\xa0 Paris") == "Café à Paris"
    assert fix_mojibake("Ça va très bien") == "Ça va très bien"


def test_transport_headers_reencoded_and_kept():
    hdrs = ("Received: from mx.example.org by mail.example.org; Tue, 1 Mar 2022 10:00:00 +0100\r\n"
            "From: =?iso-8859-1?Q?J=E9r=F4me_M=FCller?= <jerome@example.org>\r\n"
            "To: \"Dupont, Zoé\" <zoe@example.org>\r\n"
            "Subject: =?windows-1252?Q?=93Devis=94_r=E9vis=E9?=\r\n"
            "Date: Tue, 1 Mar 2022 10:00:00 +0100\r\n"
            "Message-ID: <abc@example.org>\r\n"
            "Content-Type: text/plain; charset=iso-8859-1\r\n\r\n")
    msg = FakeMsg({mapi.PR_TRANSPORT_MESSAGE_HEADERS: U(hdrs), mapi.PR_BODY: U("Bonjour Zoé"),
                   mapi.PR_MESSAGE_CLASS: U("IPM.Note")})
    built = MessageBuilder("id-1").build_raw(msg)
    assert check_raw(built.raw, built.expect) == []
    head = built.raw.split(b"\r\n\r\n", 1)[0]
    assert head.isascii()
    assert b"Received: from mx.example.org" in head
    assert head.count(b"Content-Type") == 1  # original one dropped
    import email, email.policy
    m = email.message_from_bytes(built.raw, policy=email.policy.default)
    assert m["from"].addresses[0].display_name == "Jérôme Müller"
    assert m["to"].addresses[0].display_name == "Dupont, Zoé"
    assert m["subject"] == "“Devis” révisé"
    assert m["message-id"] == "<abc@example.org>"
    assert m.get_body(("plain",)).get_content().strip() == "Bonjour Zoé"


def test_normalize_labels():
    assert normalize("ISO-8859-1") == "cp1252"
    assert normalize("windows-1250") == "cp1250"
    assert normalize("ks_c_5601-1987") == "cp949"
    assert normalize("bogus-charset") is None
    assert 'charset="utf-8"' in force_html_utf8('<meta charset="iso-8859-1">')
