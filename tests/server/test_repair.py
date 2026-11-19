"""Repair of damaged messages (app/server/repair.py): detection, repair, verification, idempotence."""
from __future__ import annotations

import io
import re
import zipfile
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest

from app.server import repair

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40 + b"png-bytes"
PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"


def _docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document/>")
    return buf.getvalue()


def _msg(text: str | None, html: str | None, subject: str = "Réunion", **headers) -> EmailMessage:
    m = EmailMessage()
    m["From"] = "Zoé Müller <zoe@example.com>"
    m["To"] = "team@example.com"
    m["Subject"] = subject
    m["Date"] = "Tue, 05 Mar 2024 10:00:00 +0100"
    m["Message-ID"] = "<abc@example.com>"
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    if text is not None:
        m.set_content(text)
        if html is not None:
            m.add_alternative(html, subtype="html")
    elif html is not None:
        m.set_content(html, subtype="html")
    return m


def _raw(m: EmailMessage) -> bytes:
    return m.as_bytes(policy=policy.SMTP)


def _parse(raw: bytes):
    return BytesParser(policy=policy.default).parsebytes(raw)


def _html(raw: bytes) -> str:
    return _parse(raw).get_body(preferencelist=("html",)).get_content()


def test_clean_message_is_left_alone():
    raw = _raw(_msg("Déjà vu à l'école, Noël « café » Ça va ? Élève.", "<p>Déjà vu à l'école, Noël « café »</p>"))
    a = repair.analyze(raw)
    assert a.issues == [] and not a.already
    assert repair.repair(raw) is None


def test_replacement_chars_rebuilt_from_the_text_version():
    text = "Bonjour, déjà reçu à Genève. Merci pour l'été."
    html = "<html><head><meta charset=\"windows-1252\"></head><body><p>Bonjour, d�j� re�u � " \
           "Gen�ve. Merci pour l'�t�.</p></body></html>"
    raw = _raw(_msg(text, html))
    assert repair.analyze(raw).issues == ["replacement_chars"]
    fixed = repair.repair(raw)
    assert fixed and fixed.fixes == ["replacement_chars"] and fixed.left == []
    out = _html(fixed.raw)
    assert "déjà reçu à Genève" in out and "l'été" in out and "�" not in out
    assert 'charset="utf-8"' in out
    m = _parse(fixed.raw)
    assert m[repair.MARK].startswith(f"1; orig={repair.orig_hash(raw)}")
    assert m["Message-ID"] == "<abc@example.com>" and m["Date"] == "Tue, 05 Mar 2024 10:00:00 +0100"
    # a second pass changes nothing
    assert repair.analyze(fixed.raw).already and repair.repair(fixed.raw) is None


def test_replacement_chars_in_the_text_rebuilt_from_the_html():
    raw = _raw(_msg("Le probl�me est r�gl�.", "<p>Le problème est réglé.</p>"))
    fixed = repair.repair(raw)
    assert fixed and _parse(fixed.raw).get_body(preferencelist=("plain",)).get_content().strip() == \
        "Le problème est réglé."


def test_replacement_chars_without_any_intact_version_are_reported_not_invented():
    raw = _raw(_msg(None, "<p>Le probl�me</p>"))
    assert repair.analyze(raw).issues == ["replacement_chars"]
    assert repair.repair(raw) is None


def test_mojibake_fixed_in_body_and_subject():
    raw = _raw(_msg("Ã‰tÃ© Ã  GenÃ¨ve, câ€™est lâ€™heure.", "<p>RÃ©union prÃ©vue</p>", subject="RÃ©union dâ€™Ã©quipe"))
    assert repair.analyze(raw).issues == ["mojibake"]
    fixed = repair.repair(raw)
    m = _parse(fixed.raw)
    assert m["Subject"] == "Réunion d’équipe"
    assert m.get_body(preferencelist=("plain",)).get_content().strip() == "Été à Genève, c’est l’heure."
    assert "Réunion prévue" in _html(fixed.raw)


def test_legit_text_is_not_taken_for_mojibake():
    for text in ("« Noël » à Genève", "café» et thé·", "Ärger über Öl", "Ça coûte 5 €", "ﬁn — “quote”", "Россия",
                 "日本語のテキスト", "Âme sœur"):
        assert not repair.has_mojibake(text), text


def test_placeholder_attachments_get_their_real_type_and_bytes_stay_identical():
    m = _msg("voir pièces jointes", None)
    m.add_attachment(PDF, maintype="application", subtype="octet-stream", filename="piece_jointe_1")
    m.add_attachment(_docx(), maintype="application", subtype="octet-stream", filename="piece_jointe_2")
    m.add_attachment(b"just text", maintype="application", subtype="octet-stream", filename="piece_jointe_3")
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename="contrat.pdf")
    raw = _raw(m)
    assert repair.analyze(raw).issues == ["attachment_names"]
    fixed = repair.repair(raw)
    atts = {p.get_filename(): (p.get_content_type(), p.get_payload(decode=True)) for p in _parse(fixed.raw).iter_attachments()}
    assert atts["piece_jointe_1.pdf"] == ("application/pdf", PDF)
    assert atts["piece_jointe_2.docx"][0].endswith("wordprocessingml.document")
    assert atts["piece_jointe_3"] == ("application/octet-stream", b"just text")   # unknown content: untouched
    assert atts["contrat.pdf"] == ("application/pdf", PDF)


def test_detached_signature_images_go_back_in_line():
    html = '<p>Cordialement</p><img src="cid:image001.png@01DA1234.56789AB0"><img src="cid:image002.png@01DA1234.56789AB0">'
    m = _msg("Cordialement", html)
    m.add_attachment(PNG, maintype="application", subtype="octet-stream", filename="piece_jointe_1")
    m.add_attachment(PNG + b"2", maintype="image", subtype="png", filename="piece_jointe_2")
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename="offre.pdf")
    raw = _raw(m)
    assert set(repair.analyze(raw).issues) == {"attachment_names", "detached_images"}
    fixed = repair.repair(raw)
    assert "detached_images" in fixed.fixes
    out = _parse(fixed.raw)
    related = [p for p in out.walk() if p.get_content_type() == "multipart/related"]
    assert len(related) == 1
    inline = {p["Content-ID"]: p.get_payload(decode=True) for p in related[0].iter_parts() if p["Content-ID"]}
    assert inline == {"<image001.png@01DA1234.56789AB0>": PNG, "<image002.png@01DA1234.56789AB0>": PNG + b"2"}
    names = [p.get_filename() for p in out.iter_attachments()]
    assert names == ["offre.pdf"]
    assert repair.analyze(fixed.raw).already


def test_forwarded_message_is_kept_as_it_is():
    inner = _msg("Le probl�me", None, subject="inner")
    outer = _msg("Bonjour, déjà vu.", "<p>Bonjour, d�j� vu.</p>")
    outer.add_attachment(inner)
    raw = _raw(outer)
    assert repair.analyze(raw).issues == ["replacement_chars"]   # the inner message is not ours to change
    fixed = repair.repair(raw)
    inner_after = [p for p in _parse(fixed.raw).iter_attachments() if p.get_content_type() == "message/rfc822"][0]
    assert "probl�me" in inner_after.get_payload()[0].get_content()


def test_legacy_mark_of_the_older_script_is_respected():
    raw = _raw(_msg("déjà", "<p>d�j�</p>", X_Repare_Import="1"))
    assert repair.analyze(raw).already and repair.repair(raw) is None


def test_verify_refuses_a_result_that_lost_something():
    m = _msg("x", None)
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename="a.pdf")
    raw = _raw(m)
    other = _msg("x", None)
    other[repair.MARK] = "1"
    assert "attachments differ" in repair.verify(raw, _raw(other))
    changed = _msg("x", None)
    del changed["From"]
    changed["From"] = "someone@else.example"
    assert any("From" in p for p in repair.verify(_raw(_msg("x", None)), _raw(changed)))


def test_detect():
    assert repair.detect(PNG)[2] == "png"
    assert repair.detect(PDF)[2] == "pdf"
    assert repair.detect(_docx())[2] == "docx"
    assert repair.detect(b"\xff\xd8\xff\xe0jpeg")[2] == "jpg"
    assert repair.detect(b"hello")[2] == ""


def test_summary_shows_text_and_parts():
    m = _msg("Déjà", "<style>p{}</style><p>D�j�</p>")
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename="a.pdf")
    s = repair.summary(_raw(m))
    assert s["text"] == "D�j�" and s["parts"][0]["name"] == "a.pdf"


# ------------------------------------------------------------------ address headers of sent items (non-regression)
_DAMAGED_BODY = (b"MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary=\"b\"\r\n\r\n"
                 b"--b\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
                 + "Déjà reçu à Genève.\r\n".encode() +
                 b"--b\r\nContent-Type: text/html; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
                 + "<p>D�j� re�u</p>\r\n".encode() + b"--b--\r\n")

SENT_HEADERS = {
    "encoded_folded": b"To: =?utf-8?q?Zo=C3=A9_M=C3=BCller?= <zoe@example.com>,\r\n"
                      b" =?iso-8859-1?q?Fran=E7ois?= <f@example.com>\r\n"
                      b"Cc: \"Dupont, Jean\" <j@example.com>, =?utf-8?b?w4lsw6h2ZQ==?= <e@example.com>\r\n",
    "bcc_only": b"Bcc: a@example.com, b@example.com\r\n",
    "undisclosed": b"To: undisclosed-recipients:;\r\nBcc: x@example.com\r\n",
    "long": b"To: " + b", ".join(b"=?utf-8?q?Pers=C3=B6n_%d?= <p%d@example.com>" % (i, i) for i in range(12))
            + b"\r\n",
    "raw_utf8": "To: Zoé Müller <zoe@example.com>\r\nCc: Élève <e@example.com>\r\n".encode(),
    "raw_latin1": "To: Zoé Müller <zoe@example.com>\r\n".encode("latin-1"),
    "two_to": b"To: a@example.com\r\nTo: b@example.com\r\n",
    "group": b"To: Equipe: a@example.com, b@example.com;, c@example.com\r\n",
    "resent": b"To: a@example.com\r\nResent-To: r@example.com\r\nReply-To: rt@example.com\r\nSender: s@example.com\r\n",
}


def _sent(extra: bytes) -> bytes:
    return (b"From: me@example.com\r\nSubject: test\r\nDate: Tue, 05 Mar 2024 10:00:00 +0100\r\n"
            b"Message-ID: <s1@example.com>\r\n" + extra + _DAMAGED_BODY)


@pytest.mark.parametrize("case", sorted(SENT_HEADERS))
def test_sent_item_keeps_its_address_headers_byte_for_byte(case):
    raw = _sent(SENT_HEADERS[case])
    fixed = repair.repair(raw)
    assert fixed and fixed.fixes == ["replacement_chars"]
    assert repair.address_fields(fixed.raw) == repair.address_fields(raw)
    out_head = fixed.raw.split(b"\r\n\r\n", 1)[0]
    assert b"unknown-8bit" not in out_head
    # every original header line other than the MIME ones is in the result unchanged
    for line in re.split(rb"\r\n(?![ \t])", raw.split(b"\r\n\r\n", 1)[0]):
        if not line.lower().startswith((b"content-", b"mime-version")):
            assert line in out_head, line


def test_mojibake_subject_keeps_its_place_and_the_recipients():
    raw = _sent(SENT_HEADERS["raw_utf8"]).replace(b"Subject: test", "Subject: RÃ©union".encode())
    fixed = repair.repair(raw)
    assert fixed and "mojibake" in fixed.fixes
    m = _parse(fixed.raw)
    assert m["Subject"] == "Réunion" and list(m.keys())[:2] == ["From", "Subject"]
    assert repair.address_fields(fixed.raw) == repair.address_fields(raw)


def test_verify_refuses_lost_or_reencoded_recipients():
    raw = _sent(SENT_HEADERS["raw_utf8"] + SENT_HEADERS["bcc_only"])
    good = repair.repair(raw).raw
    assert repair.verify(raw, good) == []
    no_bcc = re.sub(rb"Bcc: [^\r]*\r\n", b"", good)
    assert any("bcc" in p.lower() for p in repair.verify(raw, no_bcc))
    reencoded = good.replace("Zoé Müller".encode(), b"=?unknown-8bit?q?Zo=C3=A9_M=C3=BCller?=")
    assert any(p.lower().startswith("header to") for p in repair.verify(raw, reencoded))
    two = _sent(SENT_HEADERS["two_to"])
    one_to = repair.repair(two).raw.replace(b"To: b@example.com\r\n", b"")
    assert any(p.lower().startswith("header to") for p in repair.verify(two, one_to))


def test_message_without_recipients_is_reported_not_invented():
    raw = b"Subject: x\r\nFrom: me@example.com\r\nDate: Tue, 05 Mar 2024 10:00:00 +0100\r\n" + _DAMAGED_BODY
    a = repair.analyze(raw)
    assert a.no_recipients and a.issues == ["replacement_chars"]
    fixed = repair.repair(raw)
    assert fixed and not any(h in repair.address_fields(fixed.raw) for h in ("to", "cc", "bcc"))
    assert not repair.analyze(_sent(SENT_HEADERS["bcc_only"])).no_recipients
    assert not repair.analyze(_sent(SENT_HEADERS["undisclosed"])).no_recipients
