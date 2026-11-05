"""Fast paths of the builder/verifier: same bytes and same verdicts as the stdlib paths they replace."""
import email.contentmanager
import email.generator
import io
import random

import pytest

from app.engine import mapi, verify
from app.engine.message import POLICY, MessageBuilder, _b64_lines, _FastGenerator, is_word_artefact
from app.engine.pst import BuiltMessage
from app.engine.verify import check_raw, verify_message

from .conftest import BIN, BOOL, I32, U, FakeAtt, FakeMsg
from .test_synthetic import _sent_mail

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _html_mail(refs, atts=()):
    html = "<html><body><p>Réponse</p>%s</body></html>" % "".join('<img src="cid:%s">' % r for r in refs)
    props = {mapi.PR_MESSAGE_CLASS: U("IPM.Note"), mapi.PR_SUBJECT: U("RE: test"),
             mapi.PR_HTML: BIN(html.encode("utf-8")), mapi.PR_INTERNET_CPID: I32(65001),
             mapi.PR_SENDER_EMAIL: U("a@example.fr"), mapi.PR_SENDER_ADDRTYPE: U("SMTP")}
    return FakeMsg(props, attachments=list(atts))


@pytest.mark.parametrize("n", [0, 1, 2, 3, 56, 57, 58, 113, 114, 115, 1000, 4096])
def test_b64_lines_identical_to_stdlib(n):
    data = random.Random(n).randbytes(n)
    assert _b64_lines(data).decode("ascii") == email.contentmanager._encode_base64(data, 78)


def test_fast_generator_identical_output():
    built = MessageBuilder("<id-1>").build_raw(_sent_mail())
    msg = email.message_from_bytes(built.raw, policy=POLICY)
    a, b = io.BytesIO(), io.BytesIO()
    email.generator.BytesGenerator(a, policy=POLICY, mangle_from_=False).flatten(msg)
    _FastGenerator(b, policy=POLICY, mangle_from_=False).flatten(msg)
    assert a.getvalue() == b.getvalue()
    # mixed line endings in a payload: normalised like Generator._write_lines
    g = io.BytesIO()
    gen = _FastGenerator(g, policy=POLICY)
    gen._fp = g
    gen._NL = "\r\n"
    gen._write_lines("a\nb\r\nc\rd")
    assert g.getvalue() == b"a\r\nb\r\nc\r\nd"


def _mutations(raw):
    yield raw
    yield raw.replace(b"\r\n", b"\n", 3)
    i = raw.find(b"\r\n\r\n")
    yield raw[:i] + b"\r\nX-Long: " + b"a" * 1200 + raw[i:]
    yield raw[: len(raw) // 2]
    yield raw.replace(b"base64", b"quoted-printable", 1)
    yield raw.replace(b"Content-ID: <", b"Content-ID: <x", 1)
    yield raw.replace(b'filename="', b'filename="Z', 1)
    j = raw.find(b'boundary="')
    if j > 0:
        b = raw[j + 10:raw.find(b'"', j + 10)]
        yield raw.replace(b"--" + b + b"--", b"", 1)
        yield raw.replace(b'boundary="' + b, b'boundary="zz', 1)


def test_fast_walker_same_verdicts_as_full_parser(monkeypatch):
    srcs = [_sent_mail(), _html_mail(["image001.png@01D9"], [FakeAtt(
        {mapi.PR_ATTACH_METHOD: I32(1), mapi.PR_ATTACH_LONG_FILENAME: U("image001.png"),
         mapi.PR_ATTACH_CONTENT_ID: U("image001.png@01D9"), mapi.PR_ATTACHMENT_HIDDEN: BOOL(True)}, PNG)])]
    cases = []
    for src in srcs:
        built = MessageBuilder("<id-2>").build_raw(src)
        assert "fallback" not in built.warnings
        cases += [(m, built.expect) for m in _mutations(built.raw)]
    fast = [check_raw(m, e) for m, e in cases]

    def no_fast(raw):
        raise verify._Unrecognised("forced")
    monkeypatch.setattr(verify, "_walk_fast", no_fast)
    full = [check_raw(m, e) for m, e in cases]
    assert fast == full
    assert fast[0] == [] and any(fast)  # the original is clean, mutations are caught


def test_check_raw_runs_once_per_mail(monkeypatch):
    calls = []
    real = verify.check_raw
    monkeypatch.setattr(verify, "check_raw", lambda raw, exp: calls.append(1) or real(raw, exp))
    built = MessageBuilder("<id-3>").build_raw(_sent_mail())
    bm = BuiltMessage(raw=built.raw, internaldate=None, flags=[], subject="", attachments=len(built.parts.atts),
                      size=len(built.raw), warnings=list(built.warnings))
    bm._expect = built.expect
    bm._checked = (built.raw, built.checked)
    n_build = len(calls)  # outer message + its embedded message
    assert n_build == 2
    assert verify_message(bm) == [] and len(calls) == n_build  # verification reuses the build's check
    # another raw (e.g. modified after the build): checked again, never a stale verdict
    bm.raw = built.raw.replace(b"\r\n\r\n", b"\r\nX-Long: " + b"a" * 1200 + b"\r\n\r\n", 1)
    bm.size = len(bm.raw)
    assert "line longer than 998 octets" in verify_message(bm) and len(calls) == n_build + 1


def test_word_html_artefacts_are_not_warnings():
    assert is_word_artefact("filelist.xml@01D9AB12.34567890") and is_word_artefact("Msg_files/editdata.mso")
    assert not is_word_artefact("image001.png@01D9AB12.34567890")
    refs = ["filelist.xml@01D9", "editdata.mso@01D9", "oledata.mso@01D9", "themedata.thmx@01D9",
            "colorschememapping.xml@01D9", "header.htm@01D9", "image002.jpg@01D9"]
    built = MessageBuilder("<id-4>").build_raw(_html_mail(refs))
    cid_w = [w for w in built.warnings if w.startswith("cid-unresolved:")]
    assert cid_w == ["cid-unresolved:image002.jpg@01D9"]
    assert "fallback" not in built.warnings and check_raw(built.raw, built.expect) == []
    assert {r.lower() for r in refs} == built.expect["cid_unresolved"]
