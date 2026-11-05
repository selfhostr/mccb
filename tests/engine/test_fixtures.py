"""Real public PST/OST samples: every item converted, re-parsed and checked."""
import email
import email.policy
import hashlib
import os
import shutil
import subprocess
import sys
from collections import Counter

import icalendar
import pytest
import vobject

from app.engine import (BuiltMessage, DavObject, ItemError, PstError, PstFile, check_pst_signature,
                        verify_message)
from app.engine import mapi
from app.engine.mapi import PropBag

from .conftest import FIXTURES, ROOT


def _source_attachments(item):
    """Independent read of the source attachments straight from pypff."""
    out = []
    n = getattr(item, "number_of_attachments", 0) or 0
    for i in range(n):
        a = item.get_attachment(i)
        b = PropBag.from_item(a)
        meth = b.int(mapi.PR_ATTACH_METHOD, 1)
        name = (b.string(mapi.PR_ATTACH_LONG_FILENAME) or b.string(mapi.PR_ATTACH_FILENAME)
                or b.string(mapi.PR_DISPLAY_NAME))
        out.append((meth, name, a.size))
    return out


def _leaves(msg):
    res = []
    for p in msg.walk():
        if p.get_content_type() == "message/rfc822":
            res.append(("embedded", p.get_filename(), None))
            continue
        if p.is_multipart() or p.get_content_maintype() == "message":
            continue
        if p.get_filename():
            res.append(("file", p.get_filename(), len(p.get_payload(decode=True) or b"")))
    # do not count parts nested inside embedded messages
    return res


def test_signature(pst_path):
    assert check_pst_signature(pst_path)
    assert not check_pst_signature(os.path.join(FIXTURES, "SOURCES.txt"))


def test_scan_and_folders(pst_path):
    with PstFile(pst_path) as p:
        s = p.scan()
        assert set(s["totals"]) == {"mail", "contacts", "calendar"}
        assert all(isinstance(x, str) and x for f in s["folders"] for x in f["path"])
        names = [f["path"][0] for f in s["folders"]]
        assert "Top of Personal Folders" not in names and "IPM_SUBTREE" not in names
        assert sum(s["totals"].values()) > 0
        specials = Counter(f.special for f in p.folders() if f.special)
        assert all(v == 1 for v in specials.values())


def test_item_keys_stable_and_ascii(pst_path):
    def keys():
        with PstFile(pst_path) as p:
            return [k for f in p.folders() for _, k, _ in p.iter_items(f)]
    k1, k2 = keys(), keys()
    assert k1 == k2 and len(set(k1)) == len(k1)
    assert all(k.isascii() and " " not in k for k in k1)


def test_all_mails_build_and_verify(pst_path):
    sha = hashlib.sha256(pst_path.encode()).hexdigest()[:16]
    n_mail = n_err = 0
    with PstFile(pst_path) as p:
        for f in p.folders():
            if f.kind != "mail":
                continue
            for i, key, h in p.iter_items(f):
                try:
                    b = p.build_message(h, "%s-%s" % (sha, key))
                except ItemError:
                    n_err += 1
                    continue
                if b is None:
                    continue
                n_mail += 1
                assert isinstance(b, BuiltMessage)
                assert verify_message(b, h) == [], (f.path, i, b.warnings)
                assert "fallback" not in b.warnings, (f.path, i, b.warnings)
                assert b.internaldate.tzinfo is not None
                assert b.size == len(b.raw)
                hdr = b.raw[:65536].split(b"\r\n\r\n", 1)[0]
                assert ("X-PST-Import-Id: %s-%s" % (sha, key)).encode() in hdr
                m = email.message_from_bytes(b.raw, policy=email.policy.default)
                # no U+FFFD introduced
                txt = str(m["subject"] or "")
                for part in m.walk():
                    if part.get_content_type() in ("text/plain", "text/html") and not part.get_filename():
                        txt += part.get_content()
                src = h.source().bag
                src_fffd = sum((src.string(t) or "").count("\ufffd") for t in (mapi.PR_SUBJECT, mapi.PR_BODY))
                assert txt.count("\ufffd") <= src_fffd, (f.path, i)
                # attachment names and sizes identical to the source
                src_atts = _source_attachments(h.item)
                leaves = _leaves(m)
                exp_files = Counter((n, s) for meth, n, s in src_atts if meth in (1, 6) and n)
                got_files = Counter((n, s) for k, n, s in leaves if k == "file")
                assert exp_files - got_files == Counter(), (f.path, i)
                n_emb = sum(1 for meth, _, _ in src_atts if meth == 5)
                got_emb = sum(1 for k, *_ in leaves if k == "embedded")
                assert got_emb + sum(w.startswith("embedded-unreadable") for w in b.warnings) >= n_emb
                assert b.attachments >= len(src_atts) - sum(meth in (0, 2, 3, 4, 7) for meth, *_ in src_atts) - \
                    sum(w.startswith(("embedded-unreadable", "attachment-unreadable")) for w in b.warnings)
                # every cid: of the HTML resolves (except those missing in the source)
                cids = {str(p["Content-ID"]).strip("<> ").lower() for p in m.walk() if p["Content-ID"]}
                html = m.get_body(("html",))
                if html is not None:
                    from app.engine.message import html_cids
                    for ref in html_cids(html.get_content()):
                        assert ref.lower() in cids or "cid-unresolved:%s" % ref in b.warnings
    expected_errors = 1 if os.path.basename(pst_path) == "aspose-test-part0.pst" else 0
    assert n_err == expected_errors
    if os.path.basename(pst_path) in ("enron.pst", "aspose-outlook.pst", "aspose-sample.ost"):
        assert n_mail > 10


def test_dav_objects_valid(pst_path):
    seen = set()
    with PstFile(pst_path) as p:
        for f in p.folders():
            if f.kind not in ("calendar", "contacts"):
                continue
            for i, key, h in p.iter_items(f):
                o = p.build_event(h) if f.kind == "calendar" else p.build_contact(h)
                if o is None:
                    continue
                assert isinstance(o, DavObject)
                assert o.filename == o.uid + (".ics" if f.kind == "calendar" else ".vcf")
                assert o.uid not in seen
                seen.add(o.uid)
                assert "�" not in o.content
                if f.kind == "calendar":
                    cal = icalendar.Calendar.from_ical(o.content)
                    evs = list(cal.walk("VEVENT"))
                    assert evs and all(str(e["uid"]) == o.uid for e in evs)
                    vobject.readOne(o.content)
                else:
                    v = vobject.readOne(o.content)
                    assert v.version.value == "3.0" and v.fn.value
    with PstFile(pst_path) as p2:  # deterministic uids across opens
        again = set()
        for f in p2.folders():
            for i, key, h in p2.iter_items(f):
                o = p2.build_event(h) if f.kind == "calendar" else (
                    p2.build_contact(h) if f.kind == "contacts" else None)
                if o is not None:
                    again.add(o.uid)
        assert again == seen


def test_recurring_and_contacts_content():
    with PstFile(os.path.join(FIXTURES, "mtnman1965.ost")) as p:
        cal = [f for f in p.folders() if f.path == ["Calendar"]][0]
        evs = [p.build_event(h) for _, _, h in p.iter_items(cal)]
        workout = [e for e in evs if e.summary == "workout"][0]
        ev = icalendar.Calendar.from_ical(workout.content).walk("VEVENT")[0]
        assert ev["rrule"]["FREQ"] == ["DAILY"]
        assert str(ev["dtstart"].dt.tzinfo) == "America/Denver"
        con = [f for f in p.folders() if f.path == ["Contacts"]][0]
        cards = [p.build_contact(h) for _, _, h in p.iter_items(con)]
        ed = vobject.readOne([c for c in cards if c and c.summary == "Ed Pfromer"][0].content)
        assert ed.email.value == "epfromer@gmail.com"
        assert ed.org.value[0] == "Klonzo, LLC"
        assert hasattr(ed, "photo")


def test_embedded_message_rfc822():
    with PstFile(os.path.join(FIXTURES, "aspose-outlook.pst")) as p:
        found = 0
        for f in p.folders():
            if f.kind != "mail":
                continue
            for _, key, h in p.iter_items(f):
                b = p.build_message(h, "t-" + key)
                if b is None:
                    continue
                m = email.message_from_bytes(b.raw, policy=email.policy.default)
                for part in m.walk():
                    if part.get_content_type() == "message/rfc822":
                        inner = part.get_payload(0)
                        assert inner.get_body(("plain", "html")) is not None
                        found += 1
        assert found >= 4


def test_special_folders_detected():
    with PstFile(os.path.join(FIXTURES, "mtnman1965.ost")) as p:
        sp = {f.special: f.path for f in p.folders() if f.special}
    assert sp["inbox"] == ["Inbox"] and sp["sent"] == ["Sent Items"] and sp["trash"] == ["Deleted Items"]
    with PstFile(os.path.join(FIXTURES, "dist-list.pst")) as p:
        kinds = {tuple(f.path): f.kind for f in p.folders()}
        sp = {f.special for f in p.folders()}
    assert kinds[("Calendar",)] == "calendar" and kinds[("Contacts",)] == "contacts"
    assert kinds[("Tasks",)] == "tasks" and kinds[("Notes",)] == "notes" and kinds[("Journal",)] == "journal"
    assert {"inbox", "sent", "trash", "outbox", "drafts", "junk"} <= sp


# --- robustness -------------------------------------------------------------
def test_corrupt_folders_reported_not_raised():
    with PstFile(os.path.join(FIXTURES, "aspose-sub-corrupt.pst")) as p:
        s = p.scan()
    assert s["errors"] and s["totals"]["mail"] > 0


def test_corrupt_item_is_item_error():
    with PstFile(os.path.join(FIXTURES, "aspose-test-part0.pst")) as p:
        errs = 0
        for f in p.folders():
            for _, key, h in p.iter_items(f):
                if h.error is not None:
                    with pytest.raises(ItemError):
                        p.build_message(h, "x")
                    with pytest.raises(ItemError):
                        p.build_event(h)
                    errs += 1
        assert errs == 1


def test_broken_files(tmp_path):
    bad = tmp_path / "bad.pst"
    bad.write_bytes(b"not a pst at all" * 100)
    with pytest.raises(PstError):
        PstFile(str(bad))
    with pytest.raises(PstError):
        PstFile(str(tmp_path / "missing.pst"))
    # truncated real file: must either refuse to open or fail per item, never crash the process
    trunc = tmp_path / "trunc.pst"
    with open(os.path.join(FIXTURES, "enron.pst"), "rb") as fh:
        trunc.write_bytes(fh.read(2_000_000))
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from app.engine import PstFile, PstError, ItemError\n"
            "try:\n"
            "    p = PstFile(%r)\n"
            "except PstError:\n"
            "    print('refused'); sys.exit(0)\n"
            "ok = err = 0\n"
            "for f in p.folders():\n"
            "    for i, k, h in p.iter_items(f):\n"
            "        try:\n"
            "            p.build_message(h, 'x-' + k); ok += 1\n"
            "        except ItemError:\n"
            "            err += 1\n"
            "print('done', ok, err, len(p.scan()['errors']))\n") % (ROOT, str(trunc))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.startswith(("refused", "done"))


def test_cli_dump(tmp_path):
    out = tmp_path / "dump"
    r = subprocess.run([sys.executable, "-m", "app.engine", "dump", os.path.join(FIXTURES, "mtnman1965.ost"),
                        str(out), "--limit", "3"], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    files = [os.path.join(d, f) for d, _, fs in os.walk(out) for f in fs]
    assert any(f.endswith(".eml") for f in files)
    assert any(f.endswith(".ics") for f in files)
    assert any(f.endswith(".vcf") for f in files)
    shutil.rmtree(out)


def test_sort_key_equals_internaldate(pst_path):
    """The newest-first sort key is the very date of the built message's INTERNALDATE."""
    sha = hashlib.sha256(pst_path.encode()).hexdigest()[:16]
    n = 0
    with PstFile(pst_path) as p:
        for f in p.folders():
            if f.kind != "mail":
                continue
            for i, key, h in p.iter_items(f):
                try:
                    b = p.build_message(h, "%s-%s" % (sha, key))
                except ItemError:
                    continue
                if b is None:
                    continue
                assert p.item_date(h, "mail") == b.internaldate, (f.path, i)
                n += 1


def _md(date_hdr, delivery=None, submit=None):
    from datetime import datetime, timezone
    from app.engine.message import mail_date
    times = {mapi.PR_MESSAGE_DELIVERY_TIME: delivery, mapi.PR_CLIENT_SUBMIT_TIME: submit}
    hdr = None if date_hdr is None else "Date: %s\r\nSubject: x\r\n" % date_hdr
    return mail_date(times.get, lambda: hdr, now=datetime(2024, 1, 1, tzinfo=timezone.utc))


def test_mail_date_guardrails():
    from datetime import datetime, timezone
    D = lambda *a: datetime(*a, tzinfo=timezone.utc)  # noqa: E731
    deliv, sub = D(2011, 5, 2), D(2011, 5, 1)
    assert _md("Mon, 02 May 2011 10:00:00 +0200", deliv, sub) == D(2011, 5, 2, 8)      # valid
    assert _md("Thu, 01 Jan 1970 00:00:00 +0000", deliv, sub) == sub                    # before 1980 -> fallback
    assert _md("Mon, 02 May 2016 10:00:00 +0000", deliv, sub) == deliv                  # > delivery + 30 d
    assert _md("Sat, 14 May 2011 10:00:00 +0000", deliv, sub) == sub                    # +12 d: fallback
    assert _md("Mon, 02 May 2011 10:00:00", deliv, sub) == sub                          # no timezone
    assert _md("Mon, 02 May 2011 10:00:00 -0000", deliv, sub) == sub                    # no timezone (-0000)
    assert _md("Fri, 01 Jan 2100 00:00:00 +0000", None, sub) == sub                     # future vs now, no delivery
    assert _md("Mon, 02 May 2011 10:00:00 +0000", None, None) == D(2011, 5, 2, 10)      # valid, no MAPI date
