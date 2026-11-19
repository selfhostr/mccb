"""Calendar and contact items of the PST route, without servers: every item that is not sent is counted and said
(skipped with its message class, error, log line), one item never stops the phase, DAV switched off is reported."""
import time

import pytest

from tests.server.fake_engine import DavObject, FolderInfo

SHA = "abcd1234"


def _runner(env, dav_enabled=1, opt_contacts=1):
    from app.server import db, runner
    now = time.time()
    db.ex("INSERT INTO servers(name, imap_host, dav_enabled, created_at) VALUES ('srv', 'h', ?, ?)", (dav_enabled, now))
    db.ex("INSERT INTO psts(id, filename, size, chunk_size, status, path, pst_sha, created_at, updated_at) "
          "VALUES (1, 'f', 1, 1, 'valid', '/nowhere', ?, ?, ?)", (SHA, now, now))
    db.ex("INSERT INTO accounts(server_id, email, password_enc, pst_id, opt_contacts, created_at) "
          "VALUES (1, 'a@b', NULL, 1, ?, ?)", (opt_contacts, now))
    db.ex("INSERT INTO jobs(account_id, status, progress, created_at) VALUES (1, 'running', '{}', ?)", (now,))
    return runner.JobRunner(1, "w")


def _vcf(i):
    return DavObject(uid=f"u{i}", filename=f"u{i}.vcf", content=f"BEGIN:VCARD\r\nFN:n{i}\r\nEND:VCARD\r\n", summary=f"n{i}")


class _Dav:
    def __init__(self, fail=None):
        self.puts, self.fail = [], fail or {}

    def put(self, url, filename, content, kind):
        exc = self.fail.get(filename)
        if exc is not None:
            raise exc
        self.puts.append(filename)
        return 201


class _Pst:
    def item_class(self, handle):
        return handle["cls"]


def _row(key):
    from app.server import db
    r = db.q1("SELECT status, error FROM items WHERE item_key=?", (key,))
    return dict(r) if r else None


def _logs():
    from app.server import db
    return [(r["level"], r["msg"]) for r in db.q("SELECT level, msg FROM job_logs ORDER BY id")]


def test_unconverted_contact_is_skipped_with_its_class_and_built_again_later(env):
    r = _runner(env)
    r.pst = _Pst()
    r._dav_item(_Dav(), "https://dav/ab/", "contacts", "contact", "Contacts", 0, "k0", {"cls": "IPM.AbchPerson"},
                lambda h: None)
    row = _row("k0")
    assert row["status"] == "skipped" and row["error"] == "item cannot be converted (message class IPM.AbchPerson)"
    assert r._unconverted == {"IPM.AbchPerson": 1}
    # a newer engine may convert it (contact groups did not convert before): judged again on a normal run only
    rows = r.item_rows("contact", "Contacts")
    assert r.wants(rows["k0"])
    r.mode = "errors"
    assert not r.wants(rows["k0"])
    # an old skipped row of the mail phase stays done
    r.mode = "normal"
    r.upsert_item("mail", "m0", "Inbox", "skipped", error="not a mail")
    assert not r.wants(r.item_rows("mail", "Inbox")["m0"])


def test_one_failing_contact_does_not_stop_the_others(env):
    """An unexpected exception of a PUT used to leave the phase (and the job) with the next contacts never sent."""
    r = _runner(env)
    r.pst = _Pst()
    dav = _Dav(fail={"u1.vcf": UnicodeEncodeError("utf-8", "\udcff", 0, 1, "surrogates not allowed")})
    for i in range(3):
        r._dav_item(dav, "https://dav/ab/", "contacts", "contact", "Contacts", i, f"k{i}", {"cls": "IPM.Contact"},
                    lambda h, i=i: _vcf(i))
    assert dav.puts == ["u0.vcf", "u2.vcf"]
    assert _row("k0")["status"] == "ok" and _row("k2")["status"] == "ok"
    bad = _row("k1")
    assert bad["status"] == "error" and "UnicodeEncodeError" in bad["error"]
    r.recompute_progress()
    assert r.progress["contacts"]["errors"] == 1
    assert any(level == "error" and "n1" in msg for level, msg in _logs())


def test_warnings_of_the_engine_are_recorded(env):
    from app.server import db
    r = _runner(env)
    r.pst = _Pst()
    obj = _vcf(0)
    obj.warnings = ["distlist-member-without-address:1"]
    r._dav_item(_Dav(), "https://dav/ab/", "contacts", "contact", "Contacts", 0, "k0", {"cls": "IPM.DistList"},
                lambda h: obj)
    assert db.jload(db.q1("SELECT warnings w FROM items WHERE item_key='k0'")["w"], []) == obj.warnings


def test_build_failure_is_an_error_and_pause_still_propagates(env):
    from app.server import runner
    r = _runner(env)
    r.pst = _Pst()

    def broken(h):
        raise RuntimeError("corrupted contact")

    r._dav_item(_Dav(), "https://dav/ab/", "contacts", "contact", "Contacts", 0, "k0", {"cls": "IPM.Contact"}, broken)
    assert _row("k0")["status"] == "error" and "corrupted contact" in _row("k0")["error"]

    def pause(h):
        raise runner.PauseRequested()

    with pytest.raises(runner.PauseRequested):
        r._dav_item(_Dav(), "https://dav/ab/", "contacts", "contact", "Contacts", 1, "k1", {"cls": "IPM.Contact"}, pause)


def test_contacts_with_dav_disabled_are_reported(env):
    """DAV off for the server: the contacts of the PST were left out without a word, the job looked complete."""
    r = _runner(env, dav_enabled=0)
    r.folders = [FolderInfo(["Contacts"], "contacts", "IPF.Contact", None, 5),
                 FolderInfo(["Calendrier"], "calendar", "IPF.Appointment", None, 0),
                 FolderInfo(["Inbox"], "mail", "IPF.Note", "inbox", 3)]
    r._warn_dav_disabled()
    assert r.report["warnings"] == [
        "contacts: 5 item(s) in the PST not migrated: CalDAV/CardDAV is disabled for the server 'srv' "
        "(enable DAV in the server settings, then run the job again)"]
    assert ("warning", r.report["warnings"][0]) in _logs()


def test_tasks_and_notes_left_out_are_said(env):
    r = _runner(env)
    r.folders = [FolderInfo(["Tâches"], "tasks", "IPF.Task", None, 4), FolderInfo(["Notes"], "notes", "IPF.StickyNote", None, 2),
                 FolderInfo(["Journal"], "journal", "IPF.Journal", None, 0)]
    r._note_unsupported()
    assert ("warning", "Not migrated from a PST: 2 notes item(s), 4 tasks item(s)") in _logs()


def test_no_warning_when_dav_is_on_or_contacts_not_wanted(env):
    r = _runner(env, dav_enabled=0, opt_contacts=0)
    r.folders = [FolderInfo(["Contacts"], "contacts", "IPF.Contact", None, 5)]
    r._warn_dav_disabled()
    assert r.report["warnings"] == []
