"""Duplicates tab: the strict criteria (app/server/dedup.py), the runs against fake IMAP / DAV servers
(app/server/dedup_runs.py) and the API (app/server/api_dedup.py)."""
from __future__ import annotations

import time

import pytest

from app.server import dedup
from tests.server.dedup_fakes import FakeDav, FakeImap, ics, mail, vcf


# ====================================================================== criteria
def _key(raw: bytes) -> str | None:
    head = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
    return dedup.mail_info(len(raw), head).key


def test_mail_same_message_id_and_size_is_a_duplicate():
    assert _key(mail("Offre")) == _key(mail("Offre")) is not None


def test_mail_same_message_id_other_size_is_kept():
    assert _key(mail("Offre", body="hello")) != _key(mail("Offre", body="hello, with a footer"))


def test_mail_same_message_id_other_subject_or_date_is_kept():
    assert _key(mail("Offre")) != _key(mail("Offre 2"))
    assert _key(mail("Offre")) != _key(mail("Offre", date="Wed, 06 Mar 2024 10:00:00 +0100"))


def test_mail_without_message_id():
    a, b = mail("Sans id", mid=None), mail("Sans id", mid=None)
    assert _key(a) == _key(b) and _key(a).startswith("h:")
    assert _key(a) != _key(mail("Sans id", mid=None, sender="other@example.com"))
    # neither Message-ID nor Date: never a duplicate
    raw = b"From: zoe@example.com\r\nSubject: x\r\n\r\nbody\r\n"
    assert dedup.mail_info(len(raw), raw).key is None


def test_mail_header_folding_does_not_matter():
    folded = mail("Offre").replace(b"Subject: Offre", b"Subject:\r\n Offre")
    assert dedup.mail_info(100, folded.split(b"\r\n\r\n")[0]).key == \
        dedup.mail_info(100, mail("Offre").split(b"\r\n\r\n")[0]).key


def test_event_keys():
    same_uid = dedup.event_info(ics("u1", summary="Autre titre"))
    a, b = dedup.event_info(ics("u1")), dedup.event_info(ics("u2"))
    assert set(a.keys) & set(same_uid.keys)                     # same UID
    assert set(a.keys) & set(b.keys)                            # other UID, same title, start and end
    other_start = dedup.event_info(ics("u3", start="20240305T100500"))
    assert not set(a.keys) & set(other_start.keys)
    utc = dedup.event_info(ics("u4").replace("DTSTART;TZID=Europe/Zurich:", "DTSTART:"))
    assert not set(a.keys) & set(utc.keys)                      # same digits, another time zone
    # an alarm inside the event does not lend its SUMMARY to the event
    alarm = dedup.event_info(ics("u5", summary="Réunion", extra="BEGIN:VALARM\r\nACTION:DISPLAY\r\n"
                                                         "SUMMARY:rappel\r\nTRIGGER:-PT15M\r\nEND:VALARM\r\n"))
    assert set(a.keys) & set(alarm.keys) and alarm.label == "Réunion"
    assert dedup.event_info("BEGIN:VCALENDAR\r\nBEGIN:VTODO\r\nUID:t\r\nEND:VTODO\r\nEND:VCALENDAR\r\n") is None
    # an exception of a recurring event is not the same as the series alone
    series = ics("r1", extra="RRULE:FREQ=WEEKLY\r\n")
    with_exc = series.replace("END:VCALENDAR", "BEGIN:VEVENT\r\nUID:r1\r\nRECURRENCE-ID;TZID=Europe/Zurich:"
                                               "20240312T100000\r\nDTSTART;TZID=Europe/Zurich:20240312T120000\r\n"
                                               "SUMMARY:Réunion\r\nEND:VEVENT\r\nEND:VCALENDAR")
    assert not set(dedup.event_info(series).keys) & set(dedup.event_info(with_exc).keys)


def test_contact_keys():
    a = dedup.contact_info(vcf("c1"))
    b = dedup.contact_info(vcf("c2", emails=("ZOE@Example.com",), tels=("+41221234567",)))
    assert a.keys == b.keys and a.keys
    assert a.keys != dedup.contact_info(vcf("c3", emails=("zoe@example.com", "z@example.com"))).keys
    assert a.keys != dedup.contact_info(vcf("c4", fn="Zoé Muller")).keys
    assert a.keys != dedup.contact_info(vcf("c5", tels=("022 123 45 67",))).keys
    assert dedup.contact_info(vcf("c6", emails=(), tels=())).keys == []          # a name alone: never
    assert dedup.contact_info(vcf("c7", extra="KIND:group\r\n")).keys == []


def test_groups_keep_one_and_are_transitive():
    groups = dedup.group([("3", ["k"], (3,)), ("1", ["k"], (1,)), ("2", ["x"], (2,)), ("4", ["k", "y"], (4,)),
                          ("5", ["y"], (5,)), ("6", ["z"], (6,))])
    assert len(groups) == 1
    assert groups[0].keep == "1" and sorted(groups[0].remove) == ["3", "4", "5"]


# ====================================================================== runs (fake servers)
@pytest.fixture
def box(env):
    from app.server import db
    from app.server.security import encrypt
    now = time.time()
    sid = db.ex("INSERT INTO servers(name, imap_host, imap_port, imap_security, verify_tls, caldav_url, carddav_url, "
                "dav_enabled, created_at) VALUES ('srv','imap.test',993,'ssl',1,'http://dav.test/cal/',"
                "'http://dav.test/book/',1,?)", (now,)).lastrowid
    aid = db.ex("INSERT INTO accounts(server_id, email, password_enc, created_at) VALUES (?,?,?,?)",
                (sid, "zoe@example.com", encrypt("pw"), now)).lastrowid
    return {"key": f"account:{aid}", "server_id": sid, "account_id": aid}


@pytest.fixture
def servers(box, monkeypatch):
    """A fake IMAP and DAV behind the run: (imap, dav). Collections: /cal/ and /book/."""
    from app.server import dav as davmod
    from app.server import dedup_runs
    imap = FakeImap({"INBOX": {}, "Archive": {}})
    dav = FakeDav()
    dav.collections = {"/cal/": "caldav", "/book/": "carddav"}
    monkeypatch.setattr(dedup_runs, "ImapConn", imap)
    real = davmod.DavClient
    monkeypatch.setattr(dedup_runs, "DavClient", lambda u, p, v: real(u, p, v, transport=dav.transport))
    monkeypatch.setattr(real, "collections", lambda self, kind, *a: [
        ("http://dav.test/cal/" if kind == "caldav" else "http://dav.test/book/", "Agenda" if kind == "caldav"
         else "Contacts", ["VEVENT"] if kind == "caldav" else [])])
    return imap, dav


def _seed(imap: FakeImap, dav: FakeDav):
    inbox = imap.folders["INBOX"]
    inbox[1] = [mail("Offre"), set()]
    inbox[2] = [mail("Offre", body="hello with a footer"), set()]      # same Message-ID, other size: kept
    inbox[3] = [mail("Offre"), {"\\Seen"}]                               # copy of 1
    inbox[4] = [mail("Sans id", mid=None), set()]
    inbox[5] = [mail("Sans id", mid=None), set()]                        # copy of 4
    inbox[6] = [mail("Offre"), {"\\Deleted"}]                            # flagged by the owner: not looked at
    imap.folders["Archive"][1] = [mail("Offre"), set()]                  # same message, other folder: kept
    dav.add("/cal/", "a.ics", ics("u1"))
    dav.add("/cal/", "b.ics", ics("u2"))                                 # same title and times
    dav.add("/cal/", "c.ics", ics("u3", extra="DESCRIPTION:ordre du jour\r\nLOCATION:Salle 2\r\n"))  # most complete
    dav.add("/cal/", "d.ics", ics("u4", start="20240306T100000", end="20240306T110000"))
    dav.add("/book/", "z1.vcf", vcf("c1"))
    dav.add("/book/", "z2.vcf", vcf("c2", emails=("ZOE@example.com",)))
    dav.add("/book/", "y.vcf", vcf("c3", fn="Yann"))


def _run(key: str, mode: str, kinds=None) -> dict:
    from app.server import db, dedup_runs
    resolved = dedup_runs.resolve(key)
    run = dedup_runs.start([resolved], mode, list(kinds or dedup.KINDS))[0]
    db.ex("UPDATE dedup_runs SET status='running' WHERE id=?", (run["id"],))
    dedup_runs.execute(run["id"])
    return dedup_runs.run_view(db.q1("SELECT * FROM dedup_runs WHERE id=?", (run["id"],)), full=True)


def test_scan_counts_and_writes_nothing(box, servers):
    imap, dav = servers
    _seed(imap, dav)
    before = {f: dict(v) for f, v in imap.folders.items()}, dict(dav.items)
    r = _run(box["key"], "scan")
    assert r["status"] == "done", r
    assert r["totals"]["mail"] == {"items": 6, "groups": 2, "duplicates": 2, "deleted": 0, "skipped": 0}
    assert r["totals"]["calendar"]["groups"] == 1 and r["totals"]["calendar"]["duplicates"] == 2
    assert r["totals"]["contacts"]["groups"] == 1 and r["totals"]["contacts"]["duplicates"] == 1
    assert imap.writes == [] and all(ro for _, ro in imap.selects)
    assert ({f: dict(v) for f, v in imap.folders.items()}, dict(dav.items)) == before and dav.deleted == []
    inbox = next(f for f in r["report"]["folders"] if f["name"] == "INBOX")
    assert inbox["groups"] == 2 and inbox["duplicates"] == 2
    ex = [e for e in r["report"]["examples"] if e["kind"] == "mail"]
    assert {e["label"] for e in ex} == {"Offre", "Sans id"} and ex[0]["keep"] == "UID 1"
    cal = next(e for e in r["report"]["examples"] if e["kind"] == "calendar")
    assert cal["keep"] == "c.ics" and cal["copies"] == 3
    assert r["report"]["log"]


def test_dedup_deletes_only_copies_and_keeps_the_first(box, servers):
    imap, dav = servers
    _seed(imap, dav)
    r = _run(box["key"], "dedup")
    assert r["status"] == "done", r
    assert sorted(imap.folders["INBOX"]) == [1, 2, 4, 6]
    assert imap.folders["INBOX"][6][1] == {"\\Deleted"}            # the owner's \Deleted mail is not expunged
    assert ("UID EXPUNGE", "INBOX", ("3", "5")) in imap.writes
    assert sorted(imap.folders["Archive"]) == [1]
    assert sorted(dav.items) == ["/book/y.vcf", "/book/z1.vcf", "/cal/c.ics", "/cal/d.ics"]
    assert r["totals"]["mail"]["deleted"] == 2 and r["totals"]["calendar"]["deleted"] == 2
    assert r["totals"]["contacts"]["deleted"] == 1 and r["progress"]["deleted"] == 5
    again = _run(box["key"], "scan")
    assert again["progress"]["duplicates"] == 0


def test_only_the_kinds_asked(box, servers):
    imap, dav = servers
    _seed(imap, dav)
    r = _run(box["key"], "dedup", kinds=["contacts"])
    assert set(r["totals"]) == {"contacts"} and r["totals"]["contacts"]["deleted"] == 1
    assert len(imap.folders["INBOX"]) == 6 and "/cal/a.ics" in dav.items


def test_the_kept_message_changed_meanwhile_the_group_stays(box, servers):
    imap, dav = servers
    _seed(imap, dav)

    def gone(conn):           # another client removes the oldest copy between analysis and deletion
        conn.folders["INBOX"].pop(1, None)
        conn.folders["INBOX"][5][0] = mail("Sans id", mid=None, body="edited")
    imap.on_recheck = gone
    r = _run(box["key"], "dedup", kinds=["mail"])
    assert 3 in imap.folders["INBOX"], "the last copy must never be deleted"
    assert 5 in imap.folders["INBOX"], "a copy that changed since the analysis is not deleted"
    assert r["totals"]["mail"]["deleted"] == 0 and r["totals"]["mail"]["skipped"] == 2


def test_dav_change_during_run_is_skipped(box, servers):
    imap, dav = servers
    _seed(imap, dav)
    calls = []

    def edit(fake, path):     # z2 edited by its owner right before its DELETE: If-Match refuses it
        calls.append(path)
        if path == "/book/z2.vcf":
            fake.change(path)
    dav.on_delete = edit
    r = _run(box["key"], "dedup", kinds=["contacts"])
    assert "/book/z2.vcf" in dav.items and "/book/z1.vcf" in dav.items
    assert r["totals"]["contacts"]["deleted"] == 0 and r["totals"]["contacts"]["skipped"] == 1


def test_dav_kept_item_changed_group_stays(box, servers, monkeypatch):
    imap, dav = servers
    _seed(imap, dav)
    from app.server.dav import DavClient
    real = DavClient.multiget
    seen = []

    def multiget(self, kind, url, hrefs):
        seen.append(1)
        if len(seen) == 2:        # the re-check: the most complete event was edited meanwhile
            dav.change("/cal/c.ics", ics("u3", summary="Réunion déplacée"))
        return real(self, kind, url, hrefs)
    monkeypatch.setattr(DavClient, "multiget", multiget)
    r = _run(box["key"], "dedup", kinds=["calendar"])
    assert {"/cal/a.ics", "/cal/b.ics", "/cal/c.ics"} <= set(dav.items)
    assert r["totals"]["calendar"]["deleted"] == 0 and r["totals"]["calendar"]["skipped"] == 2


def test_no_uidplus_refuses_the_folder(box, servers):
    imap, dav = servers
    _seed(imap, dav)
    imap.caps.discard("UIDPLUS")
    r = _run(box["key"], "dedup", kinds=["mail"])
    assert imap.writes == [] and len(imap.folders["INBOX"]) == 6
    assert "no_uidplus" in r["report"]["notes"]
    assert next(f for f in r["report"]["folders"] if f["name"] == "INBOX")["refused"] == "no_uidplus"
    assert r["totals"]["mail"]["duplicates"] == 2 and r["totals"]["mail"]["deleted"] == 0


def test_no_etag_refuses_the_collection(box, servers):
    imap, dav = servers
    _seed(imap, dav)
    dav.etags = False
    r = _run(box["key"], "dedup", kinds=["calendar"])
    assert dav.deleted == [] and len(dav.items) == 7
    assert r["report"]["folders"][0]["refused"] == "no_etag"


def test_dav_disabled_is_noted(box, servers):
    from app.server import db
    imap, dav = servers
    db.ex("UPDATE servers SET dav_enabled=0")
    r = _run(box["key"], "scan")
    assert r["status"] == "done" and "dav_disabled" in r["report"]["notes"]


def test_dedup_refused_while_a_migration_writes(box, servers):
    from app.server import db
    db.ex("INSERT INTO jobs(account_id, status, created_at) VALUES (?, 'running', ?)", (box["account_id"], time.time()))
    imap, dav = servers
    _seed(imap, dav)
    r = _run(box["key"], "dedup")
    assert r["status"] == "failed" and "migration" in r["error"] and len(imap.folders["INBOX"]) == 6


# ====================================================================== API
def test_api_boxes_runs_and_history(client, box, servers):
    from app.server import db, dedup_runs
    from app.server.security import encrypt
    imap, dav = servers
    _seed(imap, dav)
    now = time.time()
    # the same mailbox in the repair list is shown once; another one is added; a non-IMAP target is not shown
    db.ex("INSERT INTO repair_boxes(server_id, email, password_enc, created_at) VALUES (?,?,?,?)",
          (box["server_id"], "ZOE@example.com", encrypt("pw"), now))
    rid = db.ex("INSERT INTO repair_boxes(server_id, email, password_enc, created_at) VALUES (?,?,?,?)",
                (box["server_id"], "yann@example.com", encrypt("pw"), now)).lastrowid
    db.ex("INSERT INTO accounts(server_id, email, password_enc, created_at, target_kind) VALUES (?,?,?,?, 'm365')",
          (box["server_id"], "cloud@example.com", encrypt("pw"), now))
    db.ex("INSERT INTO accounts(server_id, email, created_at) VALUES (?,?,?)", (box["server_id"], "nopw@example.com", now))
    boxes = client.get("/api/dedup/boxes").json()
    assert [b["key"] for b in boxes] == [f"account:{box['account_id'] + 2}", f"repair:{rid}", box["key"]]
    nopw = boxes[0]
    assert nopw["usable"] is False and nopw["unusable_reason"] == "no_password"
    assert boxes[2]["dav"] is True and boxes[2]["last_scan"] is None and boxes[2]["busy"] is None

    assert client.post("/api/dedup/runs", json={"box_keys": [box["key"]], "mode": "dedup"}).status_code == 422
    assert client.post("/api/dedup/runs", json={"box_keys": [box["key"]], "mode": "zap"}).status_code == 422
    assert client.post("/api/dedup/runs", json={"box_keys": ["account:999"]}).status_code == 422
    assert client.post("/api/dedup/runs", json={"box_keys": [nopw["key"]]}).status_code == 422
    assert client.post("/api/dedup/runs", json={"box_keys": [box["key"]], "kinds": ["tasks"]}).status_code == 422

    res = client.post("/api/dedup/runs", json={"box_keys": [box["key"], box["key"]], "mode": "scan"})
    assert res.status_code == 202 and len(res.json()["runs"]) == 1
    run_id = res.json()["runs"][0]["id"]
    busy = client.post("/api/dedup/runs", json={"box_keys": [box["key"]]}).json()
    assert busy["runs"] == [] and busy["skipped_busy"] == [box["key"]]
    listed = next(b for b in client.get("/api/dedup/boxes").json() if b["key"] == box["key"])
    assert listed["active_run"]["id"] == run_id

    db.ex("UPDATE dedup_runs SET status='running' WHERE id=?", (run_id,))
    dedup_runs.execute(run_id)
    full = client.get(f"/api/dedup/runs/{run_id}").json()
    assert full["status"] == "done" and full["report"]["folders"] and "totals" not in full["report"]
    assert full["totals"]["mail"]["duplicates"] == 2 and imap.writes == []
    listed = next(b for b in client.get("/api/dedup/boxes").json() if b["key"] == box["key"])
    assert listed["last_scan"]["id"] == run_id and listed["active_run"] is None

    # removal: confirmed, refused while a migration writes to the mailbox
    jid = db.ex("INSERT INTO jobs(account_id, status, created_at) VALUES (?, 'queued', ?)",
                (box["account_id"], now)).lastrowid
    assert next(b for b in client.get("/api/dedup/boxes").json() if b["key"] == box["key"])["busy"] == "migration"
    res = client.post("/api/dedup/runs", json={"box_keys": [box["key"]], "mode": "dedup", "confirm": True}).json()
    assert res["runs"] == [] and res["skipped_busy"] == [box["key"]]
    db.ex("DELETE FROM jobs WHERE id=?", (jid,))
    res = client.post("/api/dedup/runs", json={"box_keys": [box["key"]], "mode": "dedup", "confirm": True}).json()
    second = res["runs"][0]["id"]
    db.ex("UPDATE dedup_runs SET status='running' WHERE id=?", (second,))
    dedup_runs.execute(second)
    listed = next(b for b in client.get("/api/dedup/boxes").json() if b["key"] == box["key"])
    assert listed["last_scan"] is None and listed["last_run"]["id"] == second   # analyse again before removing
    assert len(imap.folders["INBOX"]) == 4
    res = client.post("/api/dedup/runs", json={"box_keys": [box["key"]], "mode": "dedup", "confirm": True}).json()
    second = res["runs"][0]["id"]
    stopped = client.post(f"/api/dedup/runs/{second}/stop").json()
    assert stopped["status"] == "stopped"
    hist = client.get(f"/api/dedup/boxes/{box['key']}/runs").json()
    assert [h["id"] for h in hist] == [second, second - 1, run_id] and "report" not in hist[0]
    assert client.get("/api/dedup/boxes/account:999/runs").status_code == 404
    assert client.get("/api/dedup/runs/999").status_code == 404


def test_recover_requeues(env):
    from app.server import db, dedup_runs
    now = time.time()
    a = db.ex("INSERT INTO dedup_runs(box_key, email, mode, status, created_at) VALUES ('account:1','a@x','scan',"
              "'running',?)", (now,)).lastrowid
    b = db.ex("INSERT INTO dedup_runs(box_key, email, mode, status, created_at) VALUES ('account:2','b@x','scan',"
              "'stopping',?)", (now,)).lastrowid
    dedup_runs.Runs().recover()
    assert db.q1("SELECT status FROM dedup_runs WHERE id=?", (a,))["status"] == "queued"
    assert db.q1("SELECT status FROM dedup_runs WHERE id=?", (b,))["status"] == "stopped"
