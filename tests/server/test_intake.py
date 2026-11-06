"""Server-side intake: drop folder, remote fetch, auto chain, notifications."""
import errno
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.server.fake_engine import make_fake_pst


# ---------------------------------------------------------------------------------- fake HTTP server
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        srv.requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
        if self.path in srv.redirects:
            self.send_response(302)
            self.send_header("Location", srv.redirects[self.path])
            self.end_headers()
            return
        data = srv.files.get(self.path.split("?")[0])
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        self.server.requests.append({"method": "POST", "path": self.path, "headers": dict(self.headers),
                                     "json": body})
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


@pytest.fixture
def web_srv():
    s = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    s.requests, s.files, s.redirects = [], {}, {}
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    s.url = f"http://127.0.0.1:{s.server_address[1]}"
    yield s
    s.shutdown()
    s.server_close()


def _worker(settle=0):
    from app.server import intake_settings
    from app.server.worker import Worker
    intake_settings.update({"inbox": {"settle_seconds": settle}})
    w = Worker()
    w.intake.scan_interval = 0
    return w


def _until(w, cond, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        w.tick()
        for t in list(w.intake.threads.values()) + list(w.validating.values()):
            t.join(0.5)
        if cond():
            return
        time.sleep(0.1)
    raise AssertionError("condition not reached")


def _psts():
    from app.server import db
    return db.q("SELECT * FROM psts ORDER BY id")


@pytest.fixture
def inbox(env, monkeypatch, tmp_path):
    d = tmp_path / "inbox"
    d.mkdir()
    monkeypatch.setenv("INBOX_DIR", str(d))
    env.reload()
    return d


# ---------------------------------------------------------------------------------- drop folder
def test_candidate_names():
    from app.server.intake import is_candidate
    assert is_candidate("jane.pst") and is_candidate("JANE.OST")
    assert not is_candidate(".jane.pst.Xa12Bc")      # rsync temp name
    assert not is_candidate(".hidden.pst")
    assert not is_candidate("jane.pst.part") and not is_candidate("jane.pst.tmp")
    assert not is_candidate("notes.txt")


def test_partial_file_not_picked_up(env, inbox):
    from app.server.intake import Inbox
    f = inbox / "big.pst"
    f.write_bytes(b"!BDN" + b"0" * 1000)
    ib = Inbox()
    t0 = 1000.0
    assert ib.stable(30, now=t0) == []              # first sighting
    assert ib.stable(30, now=t0 + 10) == []         # not yet settled
    with open(f, "ab") as fh:                       # still being written
        fh.write(b"1" * 500)
    os.utime(f, ns=(time.time_ns(), time.time_ns() + 5_000_000))
    assert ib.stable(30, now=t0 + 35) == []         # changed: the clock restarts
    assert ib.stable(30, now=t0 + 70) == [str(f)]   # unchanged for 30 s
    (inbox / ".big2.pst.AbC123").write_bytes(b"x" * 10)
    assert ib.stable(0, now=t0 + 80) == [str(f)]    # temp name never listed


def test_inbox_to_valid_pst(env, inbox):
    make_fake_pst(str(inbox / "jane.doe@example.com.pst"), 120_000)
    w = _worker()
    _until(w, lambda: any(r["status"] == "valid" for r in _psts()))
    rows = _psts()
    assert len(rows) == 1 and rows[0]["source"] == "inbox" and rows[0]["pst_sha"]
    assert os.path.exists(rows[0]["path"]) and not (inbox / "jane.doe@example.com.pst").exists()


def test_inbox_duplicate_not_registered_twice(env, inbox):
    data = make_fake_pst(str(inbox / "a.pst"), 100_000)
    w = _worker()
    _until(w, lambda: any(r["status"] == "valid" for r in _psts()))
    (inbox / "copy-of-a.pst").write_bytes(data)
    for _ in range(6):
        w.tick()
        for t in list(w.intake.threads.values()):
            t.join(1)
        time.sleep(0.1)
    assert len(_psts()) == 1
    assert (inbox / "copy-of-a.pst").exists()       # never deleted


def test_inbox_cross_filesystem_copy(env, inbox, monkeypatch):
    make_fake_pst(str(inbox / "x.pst"), 100_000)
    from app.server import intake

    def boom(a, b):
        raise OSError(errno.EXDEV, "cross-device")
    monkeypatch.setattr(intake.os, "rename", boom)
    w = _worker()
    _until(w, lambda: any(r["status"] == "valid" for r in _psts()))
    assert not (inbox / "x.pst").exists()
    assert os.path.getsize(_psts()[0]["path"]) == 100_000


def test_inbox_survives_worker_restart(env, inbox, monkeypatch):
    make_fake_pst(str(inbox / "r.pst"), 100_000)
    from app.server import intake

    def crash(*a, **k):
        raise intake.Cancelled()                    # worker "dies" after the row was created
    with monkeypatch.context() as m:
        m.setattr(intake, "move_into_place", crash)
        with pytest.raises(intake.Cancelled):
            intake.register_inbox_file(str(inbox / "r.pst"))
    rows = _psts()
    assert len(rows) == 1 and rows[0]["status"] == "fetching" and (inbox / "r.pst").exists()
    w = _worker()                                   # new worker: resumes the row
    _until(w, lambda: _psts()[0]["status"] == "valid")
    assert len(_psts()) == 1 and not (inbox / "r.pst").exists()


# ---------------------------------------------------------------------------------- remote fetch
def test_fetch_http_and_secrets_never_returned(client, env, web_srv):
    data = make_fake_pst(str(env.data_dir / "src.pst"), 200_000)
    web_srv.files["/dl/jane.pst"] = data
    r = client.post("/api/psts/fetch", json={"kind": "http", "url": web_srv.url + "/dl/jane.pst?sig=SEKRETQUERY",
                                             "token": "SEKRETTOKEN"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["status"] == "fetching" and r.json()["source"] == "fetch"
    for txt in (r.text, client.get("/api/psts").text, client.get(f"/api/psts/{pid}").text):
        assert "SEKRETTOKEN" not in txt and "SEKRETQUERY" not in txt
    from app.server import db
    assert "SEKRET" not in db.q1("SELECT fetch_spec FROM psts WHERE id=?", (pid,))["fetch_spec"]
    w = _worker()
    _until(w, lambda: client.get(f"/api/psts/{pid}").json()["status"] == "valid")
    p = client.get(f"/api/psts/{pid}").json()
    assert p["size"] == len(data) and p["received_bytes"] == len(data) and p["filename"] == "jane.pst"
    assert open(env.pst_dir / f"{pid}.pst", "rb").read() == data
    assert web_srv.requests[0]["headers"]["Authorization"] == "Bearer SEKRETTOKEN"


def test_fetch_resumes_with_range(client, env, web_srv):
    data = make_fake_pst(str(env.data_dir / "src.pst"), 200_000)
    web_srv.files["/a.pst"] = data
    pid = client.post("/api/psts/fetch", json={"url": web_srv.url + "/a.pst"}).json()["id"]
    env.ensure_dirs()
    (env.pst_dir / f"{pid}.pst").write_bytes(data[:80_000])   # a previous attempt got this far
    w = _worker()
    _until(w, lambda: client.get(f"/api/psts/{pid}").json()["status"] == "valid")
    assert web_srv.requests[0]["headers"]["Range"] == "bytes=80000-"
    assert open(env.pst_dir / f"{pid}.pst", "rb").read() == data


def test_fetch_cancel(client, env, web_srv):
    pid = client.post("/api/psts/fetch", json={"url": web_srv.url + "/a.pst"}).json()["id"]
    assert client.post(f"/api/psts/{pid}/cancel").json()["state"] == "cancelling"
    w = _worker()
    _until(w, lambda: client.get(f"/api/psts/{pid}").status_code == 404)
    assert client.post(f"/api/psts/{pid}/cancel").status_code == 404


def test_fetch_failure_then_retry(client, env, web_srv):
    pid = client.post("/api/psts/fetch", json={"url": web_srv.url + "/missing.pst"}).json()["id"]
    w = _worker()
    _until(w, lambda: client.get(f"/api/psts/{pid}").json()["status"] == "fetch_failed")
    assert "404" in client.get(f"/api/psts/{pid}").json()["error"]
    web_srv.files["/missing.pst"] = make_fake_pst(str(env.data_dir / "s.pst"), 90_000)
    assert client.post(f"/api/psts/{pid}/retry-fetch").json()["status"] == "fetching"
    _until(w, lambda: client.get(f"/api/psts/{pid}").json()["status"] == "valid")


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data/", "http://[fe80::1]/x.pst",
                                 "http://100.100.100.200/x", "http://[::ffff:169.254.169.254]/x"])
def test_fetch_refuses_link_local_and_metadata(client, url):
    r = client.post("/api/psts/fetch", json={"url": url})
    assert r.status_code == 422 and "refused" in r.text
    assert client.get("/api/psts").json() == []


def test_fetch_input_validation(client):
    assert client.post("/api/psts/fetch", json={"url": "ftp://x/y.pst"}).status_code == 422
    assert client.post("/api/psts/fetch", json={"kind": "sftp", "host": "h", "path": "/p"}).status_code == 422
    assert client.post("/api/psts/fetch", json={"kind": "s3", "endpoint": "http://127.0.0.1:1"}).status_code == 422


def test_fetch_redirect_to_metadata_refused(client, env, web_srv):
    web_srv.redirects["/r.pst"] = "http://169.254.169.254/latest/"
    pid = client.post("/api/psts/fetch", json={"url": web_srv.url + "/r.pst"}).json()["id"]
    w = _worker()
    _until(w, lambda: client.get(f"/api/psts/{pid}").json()["status"] == "fetch_failed")
    assert "refused" in client.get(f"/api/psts/{pid}").json()["error"]


def test_private_blocked_when_configured(monkeypatch, env):
    from app.server import netsafe
    monkeypatch.setenv("FETCH_BLOCK_PRIVATE", "1")
    env.reload()
    with pytest.raises(netsafe.BlockedAddress):
        netsafe.resolve("127.0.0.1", 80)
    monkeypatch.delenv("FETCH_BLOCK_PRIVATE")
    env.reload()
    assert netsafe.resolve("192.168.1.10", 80) == ["192.168.1.10"]


def test_sigv4_known_vector():
    from app.server.intake import _sigv4
    spec = {"region": "us-east-1", "access_key": "AKIAIOSFODNN7EXAMPLE",
            "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}
    h = _sigv4(spec, "https://examplebucket.s3.amazonaws.com/test.txt", "examplebucket.s3.amazonaws.com",
               "bytes=0-9", now=1369353600)
    assert h["Authorization"].endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in h["Authorization"]


def test_fetch_s3_signed_request(client, env, web_srv):
    data = make_fake_pst(str(env.data_dir / "s3.pst"), 90_000)
    web_srv.files["/bkt/dir/mail%20box.pst"] = data
    r = client.post("/api/psts/fetch", json={"kind": "s3", "endpoint": web_srv.url, "bucket": "bkt",
                                             "key": "dir/mail box.pst", "access_key": "AKTEST",
                                             "secret_key": "S3SECRETVALUE"})
    assert r.status_code == 201 and "S3SECRETVALUE" not in r.text
    pid = r.json()["id"]
    w = _worker()
    _until(w, lambda: client.get(f"/api/psts/{pid}").json()["status"] == "valid")
    auth = web_srv.requests[0]["headers"]["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKTEST/") and "S3SECRETVALUE" not in auth


# ---------------------------------------------------------------------------------- auto chain
def _seed(emails, domain_server=True):
    from app.server import db
    now = time.time()
    sid = db.ex("INSERT INTO servers(name, imap_host, created_at) VALUES ('S','imap.test',?)", (now,)).lastrowid
    ids = {}
    for e in emails:
        ids[e] = db.ex("INSERT INTO accounts(server_id,email,password_enc,created_at) VALUES (?,?,?,?)",
                       (sid, e, "x", now)).lastrowid
    return sid, ids


def _valid_pst(name):
    from app.server import db
    now = time.time()
    return db.ex("INSERT INTO psts(filename,size,chunk_size,status,path,created_at,updated_at) "
                 "VALUES (?,1,1,'valid','',?,?)", (name, now, now)).lastrowid


def test_chain_off_by_default(env):
    from app.server import db, intake
    _seed(["jane@example.com"])
    pid = _valid_pst("jane@example.com.pst")
    intake.run_chain()
    assert db.kv_get(f"chain:{pid}") is None


def test_chain_assign_and_start(env):
    from app.server import db, intake, intake_settings
    sid, ids = _seed(["jane.doe@example.com", "bob@example.com"])
    intake_settings.update({"auto_chain": {"enabled": True, "start_import": True,
                                           "default_domains": {str(sid): "example.com"}}})
    time.sleep(0.01)
    p1, p2 = _valid_pst("jane.doe@example.com.pst"), _valid_pst("bob.pst")
    intake.run_chain()
    a = db.q1("SELECT pst_id FROM accounts WHERE id=?", (ids["jane.doe@example.com"],))
    assert a["pst_id"] == p1
    assert db.q1("SELECT pst_id FROM accounts WHERE id=?", (ids["bob@example.com"],))["pst_id"] == p2
    assert db.q1("SELECT COUNT(*) n FROM jobs")["n"] == 2
    assert json.loads(db.kv_get(f"chain:{p1}"))["result"] == "assigned"
    intake.run_chain()                                   # idempotent
    assert db.q1("SELECT COUNT(*) n FROM jobs")["n"] == 2


def test_chain_never_overwrites_and_reports_ambiguity(env):
    from app.server import db, intake, intake_settings
    sid, ids = _seed(["jane@example.com"])
    sid2 = db.ex("INSERT INTO servers(name, imap_host, created_at) VALUES ('S2','imap2.test',?)",
                 (time.time(),)).lastrowid
    db.ex("INSERT INTO accounts(server_id,email,created_at) VALUES (?,?,?)", (sid2, "jane@example.com", time.time()))
    intake_settings.update({"auto_chain": {"enabled": True}})
    time.sleep(0.01)
    p = _valid_pst("jane@example.com.pst")
    intake.run_chain()
    assert json.loads(db.kv_get(f"chain:{p}"))["result"] == "ambiguous"
    assert db.q1("SELECT COUNT(*) n FROM accounts WHERE pst_id IS NOT NULL")["n"] == 0
    # unambiguous but already assigned: left alone
    db.ex("DELETE FROM accounts WHERE server_id=?", (sid2,))
    other = _valid_pst("other.pst")
    db.ex("UPDATE accounts SET pst_id=? WHERE id=?", (other, ids["jane@example.com"]))
    p2 = _valid_pst("Jane@Example.com.PST")
    intake.run_chain()
    assert json.loads(db.kv_get(f"chain:{p2}"))["result"] == "account_has_pst"
    assert db.q1("SELECT pst_id FROM accounts WHERE id=?", (ids["jane@example.com"],))["pst_id"] == other


# ---------------------------------------------------------------------------------- notifications
def _enable_ntfy(srv, **extra):
    from app.server import intake_settings
    intake_settings.update({"notifications": {"enabled": True, "group_window": 0, **extra, "ntfy": {
        "enabled": True, "url": srv.url, "topic": "mccb-test", "token": "tk_secret"}}})


def _finished_job(email, status="done", finished=None):
    from app.server import db
    now = time.time()
    sid = db.q1("SELECT id FROM servers LIMIT 1")
    sid = sid["id"] if sid else db.ex("INSERT INTO servers(name, imap_host, created_at) VALUES ('S','h',?)",
                                      (now,)).lastrowid
    aid = db.ex("INSERT INTO accounts(server_id,email,created_at) VALUES (?,?,?)", (sid, email, now)).lastrowid
    prog = json.dumps({"mail": {"done": 10, "total": 10, "errors": 0}, "calendar": {"done": 0, "total": 0, "errors": 0},
                       "contacts": {"done": 0, "total": 0, "errors": 0}})
    return db.ex("INSERT INTO jobs(account_id,status,phase,progress,created_at,finished_at) VALUES (?,?,?,?,?,?)",
                 (aid, status, "verify", prog, now, finished or now + 1)).lastrowid


def test_settings_secrets_roundtrip(client):
    r = client.put("/api/intake/settings", json={"notifications": {"ntfy": {"topic": "abc", "token": "tk_plain"}}})
    assert r.status_code == 200, r.text
    assert "tk_plain" not in r.text and r.json()["notifications"]["ntfy"]["has_token"] is True
    assert "token" not in r.json()["notifications"]["ntfy"]
    from app.server import db
    assert "tk_plain" not in db.kv_get("intake_settings")
    r = client.put("/api/intake/settings", json={"notifications": {"ntfy": {"priority": 4}}})
    assert r.json()["notifications"]["ntfy"]["has_token"] is True            # omitted = kept
    r = client.put("/api/intake/settings", json={"notifications": {"ntfy": {"token": ""}}})
    assert r.json()["notifications"]["ntfy"]["has_token"] is False           # empty = cleared
    assert client.put("/api/intake/settings", json={"notifications": {"events": {"nope": True}}}).status_code == 422
    assert client.put("/api/intake/settings", json={"inbox": {"settle_seconds": -1}}).status_code == 422
    assert client.get("/api/intake/settings").json()["inbox"]["path"].endswith("inbox")


def test_ntfy_job_notification_masked(client, env, web_srv):
    from app.server import notify
    _enable_ntfy(web_srv, mask_addresses=True)
    n = notify.Notifier()
    jid = _finished_job("jane.doe@example.com", "done_with_errors")
    n.tick()
    n.dispatcher.drain()
    posts = [r for r in web_srv.requests if r["method"] == "POST"]
    assert len(posts) == 1
    body, hdr = posts[0]["json"], posts[0]["headers"]
    assert posts[0]["path"] == "/" and body["topic"] == "mccb-test" and hdr["Authorization"] == "Bearer tk_secret"
    assert "j***@e***.com" in body["message"] and "jane.doe" not in json.dumps(body)
    n.tick()                                              # not sent twice
    n.dispatcher.drain()
    assert len([r for r in web_srv.requests if r["method"] == "POST"]) == 1
    assert jid


def test_batch_is_grouped_into_one_message(client, env, web_srv):
    from app.server import notify
    _enable_ntfy(web_srv)
    n = notify.Notifier()
    for i in range(4):
        _finished_job(f"user{i}@example.com")
    n.tick()
    n.dispatcher.drain()
    posts = [r["json"] for r in web_srv.requests if r["method"] == "POST"]
    assert len(posts) == 1 and posts[0]["title"].startswith("Batch finished: 4")


def test_grouping_when_batch_event_off(client, env, web_srv):
    from app.server import notify
    _enable_ntfy(web_srv, events={"batch_finished": False})
    n = notify.Notifier()
    for i in range(3):
        _finished_job(f"u{i}@example.com")
    n.tick()
    n.dispatcher.drain()
    posts = [r["json"] for r in web_srv.requests if r["method"] == "POST"]
    assert len(posts) == 1 and posts[0]["title"] == "3 migrations"


def test_event_toggle_off_sends_nothing(client, env, web_srv):
    from app.server import notify
    _enable_ntfy(web_srv, events={"job_done": False})
    n = notify.Notifier()
    _finished_job("a@example.com")
    n.tick()
    n.dispatcher.drain()
    assert not [r for r in web_srv.requests if r["method"] == "POST"]


def test_webhook_payload_and_test_endpoint(client, env, web_srv):
    r = client.put("/api/intake/settings", json={"notifications": {"webhook": {
        "enabled": True, "url": web_srv.url + "/hook", "token": "whtoken"}}})
    assert r.status_code == 200, r.text
    t = client.post("/api/intake/notifications/test", json={"channel": "webhook"}).json()
    assert t["ok"] is True and t["results"]["webhook"]["ok"] is True
    post = [x for x in web_srv.requests if x["path"] == "/hook"][0]
    assert post["json"]["event"] == "test" and post["headers"]["Authorization"] == "Bearer whtoken"
    assert client.post("/api/intake/notifications/test", json={"channel": "email"}).status_code == 200
    assert client.post("/api/intake/notifications/test", json={"channel": "sms"}).status_code == 422


def test_unreachable_channel_is_reported_not_raised(client, monkeypatch):
    from app.server import notify
    monkeypatch.setattr(notify, "RETRY_DELAYS", (0.0, 0.0))
    client.put("/api/intake/settings", json={"notifications": {"webhook": {"enabled": True,
                                                                          "url": "http://127.0.0.1:9/x"}}})
    t = client.post("/api/intake/notifications/test", json={"channel": "webhook"})
    assert t.status_code == 200 and t.json()["ok"] is False and t.json()["results"]["webhook"]["error"]


def test_notification_failure_does_not_touch_jobs(client, env, monkeypatch):
    from app.server import db, notify
    monkeypatch.setattr(notify, "RETRY_DELAYS", (0.0, 0.0))
    client.put("/api/intake/settings", json={"notifications": {"enabled": True, "group_window": 0, "webhook": {
        "enabled": True, "url": "http://127.0.0.1:9/x"}}})
    n = notify.Notifier()
    jid = _finished_job("z@example.com", "done")
    before = dict(db.q1("SELECT * FROM jobs WHERE id=?", (jid,)))
    t0 = time.time()
    n.tick()                                   # returns at once: delivery happens on the background thread
    assert time.time() - t0 < 2
    n.dispatcher.drain()
    assert dict(db.q1("SELECT * FROM jobs WHERE id=?", (jid,))) == before
    # a notifier that explodes cannot break the worker loop
    from app.server.worker import Worker
    w = Worker()

    def explode():
        raise RuntimeError("boom")
    monkeypatch.setattr(w.notifier, "tick", explode)
    w.tick()
    assert db.kv_get("worker_heartbeat")


def test_disk_low_and_repeated_crash(client, env, web_srv, monkeypatch):
    from app.server import db, notify
    from collections import namedtuple
    _enable_ntfy(web_srv, disk_low_mb=1000)
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(notify.shutil, "disk_usage", lambda p: Usage(10 ** 12, 10 ** 12 - 5 * 2 ** 20, 5 * 2 ** 20))
    n = notify.Notifier()
    jid = _finished_job("c@example.com", "running", finished=0)
    db.ex("UPDATE jobs SET finished_at=NULL WHERE id=?", (jid,))
    db.kv_set(f"job:{jid}:crashes", "2")
    n.tick()
    n.dispatcher.drain()
    kinds = sorted(r["json"]["title"] for r in web_srv.requests if r["method"] == "POST")
    assert kinds == ["Disk space low", "Repeated crash"]
    n._disk_t = 0
    n.tick()
    n.dispatcher.drain()
    assert len([r for r in web_srv.requests if r["method"] == "POST"]) == 2   # cooldown / claimed


def test_pst_events_and_mask(client, env, web_srv):
    from app.server import db, notify
    _enable_ntfy(web_srv, events={"upload_finished": True, "analysis_valid": True, "analysis_invalid": True},
                 mask_addresses=True)
    n = notify.Notifier()
    now = time.time() + 1
    db.ex("INSERT INTO psts(filename,size,chunk_size,status,path,error,created_at,updated_at) "
          "VALUES ('bad@example.com.pst',1,1,'invalid','','not a PST',?,?)", (now, now))
    n.tick()
    n.dispatcher.drain()
    msgs = [r["json"]["message"] for r in web_srv.requests if r["method"] == "POST"]
    assert len(msgs) == 1 or len(msgs) == 2
    assert all("bad@example.com" not in m for m in msgs)
