"""File sources: mbox, Maildir, .eml, .msg, .ics, .vcf and .zip of those. Formats are generated here; the upload,
the analysis and a whole job go through the same API and runner as a PST."""
import hashlib
import io
import zipfile
from datetime import timezone

import pytest

from app.connectors import files
from app.connectors.base import CALENDAR, CONTACTS, MAIL
from tests.server import connector_fakes as fakes
from tests.server.msgfixture import make_msg

MBOX = (b"From a@example.test Mon Jan  1 10:00:00 2024\n"
        b"From: a@example.test\nSubject: one\nDate: Mon, 1 Jan 2024 10:00:00 +0000\nMessage-ID: <1@x>\nStatus: RO\n\n"
        b"body one\n>From the desk\n\n"
        b"From b@example.test Tue Jan  2 10:00:00 2024\n"
        b"From: b@example.test\nSubject: two\nMessage-ID: <2@x>\nX-Status: AF\n\nbody two\n\n"
        b"From c@example.test Wed Jan  3 10:00:00 2024\n"
        b"From: c@example.test\nSubject: gone\nX-Mozilla-Status: 0008\n\nbody three\n")
ICS = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//EN\r\nX-WR-CALNAME:Work\r\n"
       "BEGIN:VTIMEZONE\r\nTZID:Europe/Paris\r\nEND:VTIMEZONE\r\n"
       "BEGIN:VEVENT\r\nUID:u1\r\nDTSTART;TZID=Europe/Paris:20240101T100000\r\nSUMMARY:First\r\nEND:VEVENT\r\n"
       "BEGIN:VEVENT\r\nUID:u1\r\nRECURRENCE-ID;TZID=Europe/Paris:20240108T100000\r\nSUMMARY:Moved\r\nEND:VEVENT\r\n"
       "BEGIN:VEVENT\r\nUID:u2\r\nDTSTART:20240102T100000Z\r\nSUMMARY:Second\r\nEND:VEVENT\r\n"
       "BEGIN:VTODO\r\nUID:t1\r\nSUMMARY:task\r\nEND:VTODO\r\nEND:VCALENDAR\r\n")
VCF = ("BEGIN:VCARD\r\nVERSION:3.0\r\nUID:c1\r\nFN:Ann\r\nEND:VCARD\r\n"
       "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:No uid\r\nEND:VCARD\r\n")


def eml(subject, mid="m@x"):
    return f"From: q@x.test\nSubject: {subject}\nMessage-ID: <{mid}>\nDate: Thu, 04 Jan 2024 08:00:00 +0000\n\nhi\n".encode()


def make_zip(path, members):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return str(path)


def build(path, name):
    import tempfile
    tmp = tempfile.mkdtemp()
    b = files._Builder(str(path), name, lambda: tmp)
    return b, b.build()


# ------------------------------------------------------------------ pure helpers
def test_classify():
    c = files.classify
    assert c("a.EML", b"") == "eml" and c("x/y.mbox", b"") == "mbox" and c("k.msg", b"") == "msg"
    assert c("cal.ics", b"") == "ics" and c("cards.vcf", b"") == "vcf"
    assert c("Inbox", MBOX[:100]) == "mbox"                       # Thunderbird: no extension
    assert c("noext", b"BEGIN:VCALENDAR\r\n") == "ics" and c("noext", b"\xef\xbb\xbfBEGIN:VCARD") == "vcf"
    assert c("notes.txt", b"hello") is None and c("a.pst", b"!BDN") is None
    assert files.is_maildir_file(["Maildir", "cur", "1.M1"]) and not files.is_maildir_file(["cur"])


def test_mbox_scan_flags_and_unquote():
    msgs = files.scan_mbox(io.BytesIO(MBOX))
    assert len(msgs) == 3
    assert MBOX[msgs[0]["start"]:msgs[0]["end"]].startswith(b"From: a@example.test") \
        and MBOX[msgs[0]["start"]:msgs[0]["end"]].endswith(b">From the desk\n")
    assert msgs[0]["flags"] == ["\\Seen"] and msgs[1]["flags"] == ["\\Answered", "\\Flagged"] and msgs[2]["flags"] is None
    assert msgs[0]["mid"] == "<1@x>" and msgs[0]["date"].isoformat() == "2024-01-01T10:00:00+00:00"
    assert files.unquote_mboxrd(b"a\n>From x\n>>From y\n>Fromage\n") == b"a\nFrom x\n>From y\n>Fromage\n"
    assert files.scan_mbox(io.BytesIO(b"")) == []
    crlf = MBOX.replace(b"\n", b"\r\n")
    again = files.scan_mbox(io.BytesIO(crlf))
    assert len(again) == 3 and crlf[again[1]["start"]:again[1]["end"]].endswith(b"body two\r\n")


def test_maildir_names():
    assert files.maildir_flags("1700000000.M1P2.host:2,RS") == ["\\Answered", "\\Seen"]
    assert files.maildir_flags("1700000000.M1P2.host") == []
    assert files.maildir_flags("x:2,ST") is None
    assert files.maildir_flags("x:2,DFP") == ["\\Draft", "\\Flagged", "$Forwarded"]
    assert files.maildir_folder(["Maildir"], "Maildir") == ["INBOX"]
    assert files.maildir_folder(["Maildir", ".Archive.2020"], "Maildir") == ["Archive", "2020"]
    assert files.maildir_folder(["Inbox"], "") == ["Inbox"]


def test_split_ics_and_vcf():
    name, events = files.split_ics(ICS)
    assert name == "Work" and [u for u, _t in events] == ["u1", "u2"]            # the task is left out
    u1 = events[0][1]
    assert u1.count("BEGIN:VEVENT") == 2 and "BEGIN:VTIMEZONE" in u1 and "SUMMARY:Second" not in u1
    assert u1.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0") and u1.endswith("END:VCALENDAR\r\n")
    cards = files.split_vcf(VCF)
    assert [u for u, _t in cards] == ["c1", "#2"] and cards[0][1].startswith("BEGIN:VCARD")


# ------------------------------------------------------------------ .msg
def test_msg_conversion():
    raw, warns = files.msg_to_eml(make_msg())
    from email import message_from_bytes, policy
    m = message_from_bytes(raw, policy=policy.default)
    assert str(m["Subject"]) == "Quarterly report" and "alice@example.test" in str(m["From"])
    assert "bob@example.test" in str(m["To"]) and str(m["Message-ID"]) == "<msg-1@example.test>"
    assert m["Date"].datetime.isoformat() == "2024-03-01T12:00:00+00:00"
    assert m.get_body(("plain",)).get_content().startswith("Hello Bob")
    att = list(m.iter_attachments())
    assert len(att) == 1 and att[0].get_filename() == "report.txt" and att[0].get_content() == "A" * 5000
    assert warns == []
    with pytest.raises(ValueError):
        files.msg_to_eml(b"not an ole file")


# ------------------------------------------------------------------ containers
def test_single_mbox(tmp_path):
    p = tmp_path / "alice.mbox"
    p.write_bytes(MBOX)
    b, listing = build(p, "alice.mbox")
    assert [(f.path, f.kind, f.special, f.count) for f in listing.folders] == [(["INBOX"], MAIL, "inbox", 2)]
    items = listing.items["INBOX\0mail"]
    assert [i.flags for i in items] == [["\\Seen"], ["\\Answered", "\\Flagged"]]        # the deleted one is skipped
    assert items[0].date.tzinfo is not None and items[0].alt_key == "m:" + files._h("<1@x>")
    _b2, again = build(p, "alice.mbox")
    assert [i.key for i in again.items["INBOX\0mail"]] == [i.key for i in items]        # same file, same keys
    src = files.FilesSource({"pst_id": 1}, {})
    src._builder = b
    got = src.fetch(listing.folders[0], items[0], "<ns-x>")
    assert got.raw == b"From: a@example.test\nSubject: one\nDate: Mon, 1 Jan 2024 10:00:00 +0000\nMessage-ID: <1@x>\n" \
                      b"Status: RO\n\nbody one\nFrom the desk\n"
    assert got.flags == ["\\Seen"] and got.subject == "one" and got.internal_date.astimezone(timezone.utc).hour == 10


def test_zip_with_everything(tmp_path):
    z = make_zip(tmp_path / "all.zip", {
        "Maildir/cur/1700000000.M1.h:2,S": eml("m1", "1@m"), "Maildir/new/1700000001.M2.h": eml("m2", "2@m"),
        "Maildir/cur/1700000002.M3.h:2,ST": eml("trashed", "3@m"), "Maildir/.Sent.2020/cur/1700000003.M4.h:2,RS": eml("m4", "4@m"),
        "loose/a.eml": eml("loose"), "Work.mbox": MBOX, "cal/work.ics": ICS, "people.vcf": VCF,
        "note.msg": make_msg(attachment=None), "readme.txt": b"skip me", "__MACOSX/._a.eml": b"x", "dir/": b"",
        "nested.zip": b"PK\x03\x04junk"})
    b, listing = build(z, "all.zip")
    got = {(tuple(f.path), f.kind): f for f in listing.folders}
    assert set(got) == {(("INBOX",), MAIL), (("Sent", "2020"), MAIL), (("loose",), MAIL), (("Work",), MAIL),
                        (("cal", "Work"), CALENDAR), (("people",), CONTACTS)}
    assert got[(("INBOX",), MAIL)].count == 3          # m1, m2 and the .msg at the root; the trashed one is gone
    assert got[(("Work",), MAIL)].special is None and got[(("Work",), MAIL)].count == 2
    assert got[(("cal", "Work"), CALENDAR)].count == 2 and got[(("people",), CONTACTS)].count == 2
    assert sorted(listing.skipped) == ["nested.zip", "readme.txt"]
    keys = [i.key for items in listing.items.values() for i in items]
    assert len(keys) == len(set(keys)) and all(k.isascii() and len(k) <= 200 for k in keys)
    assert keys == [i.key for items in build(z, "all.zip")[1].items.values() for i in items]
    src = files.FilesSource({"pst_id": 1}, {})
    src._builder = b
    inbox = got[(("INBOX",), MAIL)]
    by_subject = {}
    for it in listing.items["INBOX\0mail"]:
        f = src.fetch(inbox, it, "<x>")
        by_subject[f.subject] = (it, f)
    assert set(by_subject) == {"m1", "m2", "Quarterly report"}
    assert by_subject["m1"][1].flags == ["\\Seen"] and by_subject["m2"][1].flags == []
    work = got[(("Work",), MAIL)]
    one = src.fetch(work, listing.items["Work\0mail"][0], "<x>")                 # mbox inside a zip: extracted on demand
    assert one.subject == "one" and one.flags == ["\\Seen"]
    cal = src.fetch(got[(("cal", "Work"), CALENDAR)], listing.items["cal\x1fWork\0calendar"][0], "<x>")
    assert cal.raw.startswith(b"BEGIN:VCALENDAR") and cal.subject == "First"
    card = src.fetch(got[(("people",), CONTACTS)], listing.items["people\0contacts"][1], "<x>")
    assert b"FN:No uid" in card.raw


def test_scan_upload_refusals(tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_bytes(b"just text")
    assert not files.looks_like_files(str(notes), "notes.txt")
    empty_zip = make_zip(tmp_path / "e.zip", {"readme.txt": b"x"})
    with pytest.raises(ValueError, match="nothing recognized"):
        files.scan_upload(empty_zip, "e.zip")
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"PK\x03\x04 not really")
    with pytest.raises(ValueError):
        files.scan_upload(str(bad), "bad.zip")
    pst = tmp_path / "x.pst"
    pst.write_bytes(b"!BDN" + b"0" * 100)
    assert not files.looks_like_files(str(pst), "x.pst")                          # a PST is never read here
    scan = files.scan_upload(make_zip(tmp_path / "ok.zip", {"a.eml": eml("a")}), "ok.zip")
    assert scan["format"] == "files" and scan["totals"] == {"mail": 1, "calendar": 0, "contacts": 0}
    assert scan["folders"] == [{"path": ["INBOX"], "kind": "mail", "count": 1}] and scan["contents"] == ["eml"]


def test_zip_bomb_is_skipped(tmp_path):
    z = tmp_path / "b.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("ok.eml", eml("ok"))
        zf.writestr("big.eml", b"A" * (80 * 2**20))      # 80 MB of one byte: ratio far above the limit
    names = [e.path for e in files.entries_of(str(z), "b.zip")]
    assert names == ["ok.eml"]


def test_intake_names():
    from app.server.intake import is_candidate, stem_of
    for n in ("a.mbox", "b.EML", "c.msg", "d.ics", "e.vcf", "f.zip", "g.mbx", "h.pst", "i.ost"):
        assert is_candidate(n), n
    assert not is_candidate(".a.mbox") and not is_candidate("a.mbox.part") and not is_candidate("a.txt")
    assert stem_of("jane@example.test.mbox") == "jane@example.test" and stem_of("jane.pst") == "jane"


# ------------------------------------------------------------------ upload, analysis, job
def _put_all(client, name, data):
    meta = {"filename": name, "size": len(data), "last_modified": 1700000000000}
    up = client.post("/api/uploads", json=meta).json()
    cs = up["chunk_size"]
    for n, i in enumerate(range(0, len(data), cs)):
        chunk = data[i:i + cs]
        r = client.put(f"/api/uploads/{up['id']}/chunks/{n}", content=chunk,
                       headers={"X-Chunk-Sha256": hashlib.sha256(chunk).hexdigest(), "Content-Type": "application/octet-stream"})
        assert r.status_code == 200, r.text
    assert client.post(f"/api/uploads/{up['id']}/complete").json()["status"] == "validating"
    from app.server.worker import Worker
    w = Worker()
    w.tick()
    for t in list(w.validating.values()):
        t.join(10)
    return up["id"]


@pytest.fixture
def mem():
    fakes.register()
    yield fakes
    fakes.unregister()


def _account_with_target(client, pst_id):
    from tests.server.test_connectors import make_account
    fakes.TARGETS["t"] = fakes.MemoryStore()
    aid = make_account(client)
    assert client.put(f"/api/accounts/{aid}/target", json={"kind": "memory", "config": {"name": "t"}}).status_code == 200
    r = client.put(f"/api/accounts/{aid}/pst", json={"pst_id": pst_id})
    assert r.status_code == 200, r.text
    return aid, fakes.TARGETS["t"]


def test_upload_analysis_and_job(client, mem, tmp_path):
    from tests.server.test_connectors import run_job, start
    data = make_zip_bytes({"Inbox/a.eml": eml("a", "a@x"), "Inbox/b.eml": eml("b", "b@x"), "Sent/c.eml": eml("c", "c@x"),
                           "x.vcf": VCF})
    pid = _put_all(client, "export.zip", data)
    pst = client.get(f"/api/psts/{pid}").json()
    assert pst["status"] == "valid", pst
    assert pst["scan"]["format"] == "files" and pst["scan"]["totals"] == {"mail": 3, "calendar": 0, "contacts": 2}
    assert client.get(f"/api/psts/{pid}/preview").status_code == 409              # preview is for PST files

    aid, store = _account_with_target(client, pid)
    src = client.get(f"/api/accounts/{aid}/source").json()
    assert src["kind"] == "files" and src["config"] == {"pst_id": pid}             # set when the file was linked
    job = run_job(client, start(client, aid))
    assert job["status"] == "done", job
    assert sorted(i[0] for i in store.items) == ["Inbox", "Inbox", "Sent"]         # mail only: no target for contacts
    first = store.copies()
    assert len(first) == 3 and set(first.values()) == {1}
    again = run_job(client, start(client, aid))
    assert again["status"] == "done" and store.copies() == first                   # a rerun adds nothing

    # a real PST goes back to the historical path, and a file switches the account again
    from tests.server.fake_engine import make_fake_pst
    pid2 = _put_all(client, "real.pst", make_fake_pst(str(tmp_path / "t.pst"), 70_000))
    assert client.get(f"/api/psts/{pid2}").json()["scan"]["totals"]["mail"] == 11
    for want, pst_id in (("pst", pid2), ("files", pid), ("pst", pid2)):
        assert client.put(f"/api/accounts/{aid}/pst", json={"pst_id": pst_id}).status_code == 200
        assert client.get(f"/api/accounts/{aid}/source").json()["kind"] == want


def make_zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_unrecognized_upload_is_invalid(client, env):
    pid = _put_all(client, "notes.txt", b"just some text, nothing to migrate" * 10)
    p = client.get(f"/api/psts/{pid}").json()
    assert p["status"] == "invalid" and "not a PST" in p["error"]


def test_a_file_linked_before_it_was_analysed_follows(client, env):
    """Account linked to an upload still being analysed: the source switches when the analysis ends."""
    from tests.server.test_connectors import make_account
    aid = make_account(client)
    data = MBOX
    meta = {"filename": "jane.mbox", "size": len(data), "last_modified": 1}
    up = client.post("/api/uploads", json=meta).json()
    assert client.put(f"/api/accounts/{aid}/pst", json={"pst_id": up["id"]}).status_code == 200
    assert client.get(f"/api/accounts/{aid}/source").json()["kind"] == "pst"     # unknown yet
    client.put(f"/api/uploads/{up['id']}/chunks/0", content=data,
               headers={"X-Chunk-Sha256": hashlib.sha256(data).hexdigest(), "Content-Type": "application/octet-stream"})
    client.post(f"/api/uploads/{up['id']}/complete")
    from app.server.worker import Worker
    w = Worker()
    w.tick()
    for t in list(w.validating.values()):
        t.join(10)
    assert client.get(f"/api/psts/{up['id']}").json()["status"] == "valid"
    assert client.get(f"/api/accounts/{aid}/source").json()["kind"] == "files"
