"""Contacts of a PST: contact groups (IPM.DistList), folders without a container class, values servers refuse."""
import os
import struct

import vobject

from app.engine import PstFile, mapi
from app.engine.contact import build_contact
from app.engine.pst import sniffed_kind

from .conftest import FIXTURES, U, FakeMsg, namemap_with

AD = mapi.PSETID_ADDRESS
WRAPPED = bytes.fromhex("00000000c091add3519dcf11a4a900aa0047faa4c3")


def _one_off(name, addr, atype="SMTP", unicode=True):
    flags = 0x8001 if unicode else 0x0001
    enc = (lambda s: s.encode("utf-16-le") + b"\x00\x00") if unicode else (lambda s: s.encode("cp1252") + b"\x00")
    return b"\x00" * 4 + mapi.ONE_OFF_PROVIDER + b"\x00\x00" + struct.pack("<H", flags) + enc(name) + enc(atype) + enc(addr)


def _mv(values):
    head = struct.pack("<I", len(values))
    off = 4 + 4 * len(values)
    offs, data = [], b""
    for v in values:
        offs.append(off + len(data))
        data += v
    return (mapi.PT_MV_BINARY, head + struct.pack("<%dI" % len(values), *offs) + data)


def _distlist(one_offs, members, name="Équipe ventes"):
    nm = namemap_with({(AD, mapi.LID_DISTLIST_ONE_OFF_MEMBERS): 0x8101, (AD, mapi.LID_DISTLIST_MEMBERS): 0x8102})
    props = {mapi.PR_MESSAGE_CLASS: U("IPM.DistList"), mapi.PR_DISPLAY_NAME: U(name)}
    if one_offs is not None:
        props[0x8101] = _mv(one_offs)
    if members is not None:
        props[0x8102] = _mv(members)
    return FakeMsg(props, namemap=nm)


def test_parse_one_off_entryid_unicode_and_ansi():
    assert mapi.parse_one_off_entryid(_one_off("Zoé Lefèvre", "zoe@example.fr")) == ("Zoé Lefèvre", "SMTP", "zoe@example.fr")
    assert mapi.parse_one_off_entryid(_one_off("Zoé", "z@example.fr", unicode=False)) == ("Zoé", "SMTP", "z@example.fr")
    assert mapi.parse_one_off_entryid(WRAPPED + b"\x00" * 24) is None
    assert mapi.parse_one_off_entryid(b"") is None


def test_distlist_becomes_a_contact_group():
    """Before: build_contact returned None for an IPM.DistList, the group was recorded as skipped in silence."""
    src = _distlist(
        [_one_off("Zoé Lefèvre", "zoe@example.fr"), _one_off("Marc", "marc@example.fr"), _one_off("", "ZOE@example.fr")],
        [WRAPPED + b"\x00" * 24, _one_off("Marc", "marc@example.fr"), _one_off("", "ZOE@example.fr")])
    r = build_contact(src, "pst-vcf-group")
    assert r is not None
    summary, text, warnings = r
    v = vobject.readOne(text)
    assert summary == v.fn.value == "Équipe ventes"
    assert v.contents["x-addressbookserver-kind"][0].value == "group"
    assert [m.value for m in v.contents["x-addressbookserver-member"]] == ["mailto:zoe@example.fr", "mailto:marc@example.fr"]
    assert [e.value for e in v.email_list] == ["zoe@example.fr", "marc@example.fr"]
    assert "Zoé Lefèvre <zoe@example.fr>" in v.note.value
    assert v.uid.value == "pst-vcf-group" and not warnings


def test_distlist_member_without_one_off_is_a_warning():
    src = _distlist(None, [WRAPPED + b"\x00" * 24, _one_off("Marc", "marc@example.fr")])
    summary, text, warnings = build_contact(src, "pst-vcf-g2")
    v = vobject.readOne(text)
    assert [e.value for e in v.email_list] == ["marc@example.fr"]
    assert warnings == ["distlist-member-without-address:1"]


def test_empty_distlist_still_a_card():
    summary, text, warnings = build_contact(_distlist(None, None, name="Vide"), "pst-vcf-g3")
    v = vobject.readOne(text)
    assert v.fn.value == "Vide" and not hasattr(v, "email")


def test_control_characters_are_dropped_from_the_card():
    """A vertical tab or a form feed copied from a document into a PST contact makes SabreDAV refuse the card."""
    src = FakeMsg({mapi.PR_MESSAGE_CLASS: U("IPM.Contact"), mapi.PR_DISPLAY_NAME: U("Jean\x0bDupont"),
                   mapi.PR_BODY: U("ligne 1\x0cligne 2\x1f\r\nfin")})
    _s, text, _w = build_contact(src, "pst-vcf-ctl")
    assert not any(c in text for c in "\x0b\x0c\x1f")
    v = vobject.readOne(text)
    assert v.fn.value == "JeanDupont" and v.note.value == "ligne 1ligne 2\nfin"


def test_exchange_only_address_is_a_warning():
    nm = namemap_with({(AD, mapi.LID_EMAIL1_ADDRESS): 0x8001, (AD, mapi.LID_EMAIL1_TYPE): 0x8002})
    src = FakeMsg({mapi.PR_MESSAGE_CLASS: U("IPM.Contact"), mapi.PR_DISPLAY_NAME: U("Jean Dupont"),
                   0x8001: U("/o=ExchangeLabs/ou=Exchange Administrative Group/cn=Recipients/cn=jdupont"),
                   0x8002: U("EX")}, namemap=nm)
    _s, text, warnings = build_contact(src, "pst-vcf-ex")
    assert "EMAIL" not in text and warnings == ["email-not-smtp:EX"]


def test_sniffed_kind_counts_contact_groups():
    """A Contacts folder without container class whose first items include a group was taken for a mail folder:
    its contacts were then skipped as 'not a mail'."""
    assert sniffed_kind(["IPM.Contact", "IPM.DistList", "IPM.Contact"]) == "contacts"
    assert sniffed_kind(["IPM.DistList"]) == "contacts"
    assert sniffed_kind(["IPM.Appointment", "IPM.Appointment.Occurrence"]) == "calendar"
    assert sniffed_kind(["IPM.Contact", "IPM.Note"]) is None
    assert sniffed_kind([]) is None


class _Folder:
    def __init__(self, items):
        self._items = items

    @property
    def number_of_sub_messages(self):
        return len(self._items)

    def get_sub_message(self, i):
        it = self._items[i]
        if isinstance(it, Exception):
            raise it
        return it


def test_sniff_kind_skips_an_unreadable_item(monkeypatch):
    """Before, one unreadable item among the first ones made the whole folder a mail folder."""
    pst = PstFile.__new__(PstFile)
    monkeypatch.setattr(PstFile, "_bag", lambda self, item: item.bag)
    contact = FakeMsg({mapi.PR_MESSAGE_CLASS: U("IPM.Contact")})
    group = FakeMsg({mapi.PR_MESSAGE_CLASS: U("IPM.DistList")})
    assert pst._sniff_kind(_Folder([OSError("unreadable"), contact, group])) == "contacts"
    assert pst._sniff_kind(_Folder([OSError("unreadable")])) is None


def test_fixture_distribution_list_is_migrated():
    """dist-list.pst (java-libpst): one contact and one group of three members, two of them one-off addresses."""
    with PstFile(os.path.join(FIXTURES, "dist-list.pst")) as pst:
        [folder] = [f for f in pst.folders() if f.kind == "contacts"]
        built = [pst.build_contact(h) for _i, _k, h in pst.iter_items(folder)]
        classes = sorted(pst.item_class(h) for _i, _k, h in pst.iter_items(folder))
    assert classes == ["IPM.Contact", "IPM.DistList"]
    assert all(b is not None for b in built)
    group = [b for b in built if "X-ADDRESSBOOKSERVER-KIND:group" in b.content][0]
    v = vobject.readOne(group.content)
    assert v.fn.value == "test dist list"
    assert sorted(e.value for e in v.email_list) == ["contact1@rjohnson.id.au", "dist1@rjohnson.id.au",
                                                      "dist2@rjohnson.id.au"]
    assert group.warnings == []
