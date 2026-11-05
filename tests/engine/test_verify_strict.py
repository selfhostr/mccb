"""Strict boundary lines and bad base64 in the fast MIME walker: fall back to the full parser."""
import pytest

from app.engine import verify
from app.engine.verify import check_raw

H = b"X-PST-Import-Id: x\r\nSubject: s\r\nMIME-Version: 1.0\r\n"
TXT = (b'Content-Type: text/plain; charset="utf-8"\r\n'
       b"Content-Transfer-Encoding: quoted-printable\r\n\r\nhello")


def _att(data):
    return (b'Content-Type: application/octet-stream; name="a.bin"\r\n'
            b'Content-Disposition: attachment; filename="a.bin"\r\n'
            b"Content-Transfer-Encoding: base64\r\n\r\n" + data)


def _mp(parts):
    out = H + b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
    for p in parts:
        out += b"--B\r\n" + p + b"\r\n"
    return out + b"--B--\r\n"


def _full(raw, expect):
    msg, leaves = verify._walk_full(raw)
    return verify._check_parts([], msg, leaves, expect, False)


EXP = {"attachments": [("a.bin", 3, "file")]}


@pytest.mark.parametrize("junk", [b"--B--x", b"--B\rjunk"])
def test_bad_delimiter_falls_back(junk):
    raw = _mp([TXT, _att(b"AAAA\r\n" + junk + b"\r\nAAAA")])
    with pytest.raises(verify._Unrecognised):
        list(verify._fast_parts(raw))
    assert check_raw(raw, EXP) == _full(raw, EXP)


def test_bad_close_delimiter_falls_back():
    raw = _mp([TXT, _att(b"AAAA")]).replace(b"--B--\r\n", b"--B--x\r\n")
    with pytest.raises(verify._Unrecognised):
        list(verify._fast_parts(raw))
    assert check_raw(raw, EXP) == _full(raw, EXP)


def test_padded_delimiter_stays_fast():
    raw = _mp([TXT, _att(b"AAAA")]).replace(b"--B\r\n", b"--B  \r\n")
    assert len(list(verify._fast_parts(raw))) == 3
    assert check_raw(raw, EXP) == _full(raw, EXP)


def test_invalid_base64_does_not_raise():
    raw = _mp([TXT, _att(b"AAA")])
    with pytest.raises(verify._Unrecognised):
        verify._decode_body({"content-transfer-encoding": "base64"}, b"AAA")
    res = check_raw(raw, EXP)
    assert isinstance(res, list)
    assert res == _full(raw, EXP)
