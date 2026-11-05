import os
import struct
import sys
from datetime import datetime, timezone

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIXTURES = os.path.join(ROOT, "tests", "fixtures")

from app.engine import mapi  # noqa: E402
from app.engine.mapi import PropBag  # noqa: E402


def fixture_paths():
    return sorted(os.path.join(FIXTURES, f) for f in os.listdir(FIXTURES)
                  if f.lower().endswith((".pst", ".ost")))


@pytest.fixture(params=fixture_paths(), ids=os.path.basename)
def pst_path(request):
    return request.param


# --- synthetic MAPI values -------------------------------------------------
def U(s):
    return (mapi.PT_UNICODE, s.encode("utf-16-le"))


def S8(s, cp="cp1252"):
    return (mapi.PT_STRING8, s.encode(cp) + b"\x00")


def I32(v):
    return (mapi.PT_LONG, struct.pack("<i", v))


def BOOL(v):
    return (mapi.PT_BOOLEAN, b"\x01" if v else b"\x00")


def BIN(b):
    return (mapi.PT_BINARY, b)


def FT(dt):
    ticks = int((dt - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds() * 10_000_000)
    return (mapi.PT_SYSTIME, struct.pack("<Q", ticks))


class FakeAtt:
    def __init__(self, props, data=None, embedded=None, namemap=None):
        self.bag = PropBag.from_dict(props, namemap)
        self._data = data
        self._emb = embedded

    @property
    def size(self):
        return len(self._data or b"")

    def read_data(self):
        if self._data is None:
            raise IOError("no data")
        return self._data

    def embedded(self):
        if self._emb is None:
            raise RuntimeError("no embedded message")
        return self._emb


class FakeMsg:
    def __init__(self, props, recipients=(), attachments=(), namemap=None, identifier=1234):
        self.bag = PropBag.from_dict(props, namemap)
        self._r = [PropBag.from_dict(r, namemap) for r in recipients]
        self._a = list(attachments)
        self.identifier = identifier

    def recipients(self):
        return self._r

    def attachments(self):
        yield from self._a


def namemap_with(entries):
    nm = mapi.NameMap()
    for (guid, lid), pid in entries.items():
        nm.by_key[(guid.upper(), lid)] = pid
        nm.by_id[pid] = (guid.upper(), lid)
    return nm
