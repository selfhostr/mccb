"""Mirror mode against the in-memory connectors of mirror_fakes.py: what is added, updated and deleted on the target,
and every safety rule (read-only source, only our items, thresholds, complete listings, dry run, idempotence)."""
import pytest

from app.connectors.base import CONTACTS, MAIL, MIRROR_DELETED_FLAG, ConnectorError
from tests.server import mirror_fakes as mf

SRV = {"name": "Prod", "imap_host": "imap.example.test", "imap_port": 993, "imap_security": "ssl", "verify_tls": True,
       "dav_enabled": False, "workers": 1, "tuning": "manual", "notes": ""}


@pytest.fixture
def box():
    mf.register()
    yield mf
    mf.unregister()


def make(client, mirror=True, seed=True):
    src = mf.SOURCES["s"] = mf.Box()
    dst = mf.TARGETS["t"] = mf.Store()
    if seed:
        for i in range(10):
            src.add("INBOX", f"in{i}@x", flags=["\\Seen"] if i % 2 else [])
        for i in range(4):
            src.add("Projects", f"pr{i}@x")
        for i in range(5):
            src.add("Contacts", f"c{i}", kind=CONTACTS, subject=f"Contact {i}")
    sid = client.post("/api/servers", json=SRV).json()["id"]
    r = client.post("/api/accounts", json={"server_id": sid, "email": "me@example.test", "password": "pw"})
    aid = r.json()["id"]
    assert client.put(f"/api/accounts/{aid}/source", json={"kind": "boxmirror", "config": {"name": "s"}}).status_code == 200
    assert client.put(f"/api/accounts/{aid}/target", json={"kind": "boxmirror", "config": {"name": "t"}}).status_code == 200
    if mirror:
        r = client.put(f"/api/accounts/{aid}", json={"mirror": True})
        assert r.status_code == 200, r.text
    return aid, src, dst


def run(client, jid):
    from app.server import runner
    runner.run_job(jid, "test:inproc")
    return client.get(f"/api/jobs/{jid}").json()


def full(client, aid):
    r = client.post("/api/runs", json={"account_ids": [aid]})
    assert r.status_code == 200, r.text
    job = run(client, r.json()[0]["id"])
    assert job["status"] == "done", job
    return job


def preview(client, aid, **kw):
    r = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror", **kw})
    assert r.status_code == 200, r.text
    acc = r.json()["accounts"]
    assert len(acc) == 1
    return acc[0]


def mirror(client, aid, **kw):
    r = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror", "dry_run": False, **kw})
    assert r.status_code == 200, r.text
    body = r.json()
    jobs = body["jobs"]
    assert len(jobs) == 1, body
    job = run(client, jobs[0]["id"])
    rep = client.get(f"/api/jobs/{job['id']}/report").json()
    return job, rep


def codes(acc):
    return {b["code"] for b in acc["blocked"]}


def ours(dst, folder):
    return [c for c in dst.all(folder) if c.ours]


# ------------------------------------------------------------------ account option
def test_mirror_option_on_account(client, box):
    aid, _, _ = make(client, mirror=False)
    acc = client.get(f"/api/accounts/{aid}").json()
    assert acc["mirror"] is False and "mirror" not in acc["options"]
    assert client.put(f"/api/accounts/{aid}", json={"options": {"mirror": True}}).status_code == 200
    assert client.get(f"/api/accounts/{aid}").json()["mirror"] is True
    assert client.put(f"/api/accounts/{aid}", json={"mirror": False}).status_code == 200
    assert client.get(f"/api/accounts/{aid}").json()["mirror"] is False


def test_mirror_off_is_blocked(client, box):
    aid, _, _ = make(client, mirror=False)
    full(client, aid)
    acc = preview(client, aid)
    assert acc["ready"] is False and "mirror_off" in codes(acc)
    r = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror", "dry_run": False})
    assert r.status_code == 200
    assert r.json()["jobs"] == [] and "mirror_off" in codes(r.json()["accounts"][0])


def test_no_prior_run_is_blocked(client, box):
    aid, _, _ = make(client)
    acc = preview(client, aid)
    assert acc["ready"] is False and "no_prior_run" in codes(acc)


# ------------------------------------------------------------------ dry run
def test_dry_run_counts_and_writes_nothing(client, box):
    aid, src, dst = make(client)
    full(client, aid)
    src.remove("in0@x")
    src.remove("c1")
    src.add("INBOX", "new@x")
    src.get("in2@x").flags = ["\\Seen", "\\Flagged"]
    src.get("c2").version = "2"
    src.move("pr1@x", "INBOX")
    before = len(dst.writes)
    snapshot = {k: [(c.ref, c.ident, list(c.flags), c.raw) for c in v.values()] for k, v in dst.folders.items()}
    acc = preview(client, aid)
    assert acc["ready"] is True, acc
    assert acc["email"] == "me@example.test"
    assert acc["to_add"] == {"mail": 1, "calendar": 0, "contacts": 0, "tasks": 0}
    assert acc["to_delete"] == {"mail": 1, "calendar": 0, "contacts": 1, "tasks": 0}
    assert acc["to_update"] == {"mail": 2, "calendar": 0, "contacts": 1, "tasks": 0}  # flags + move, changed contact
    assert len(dst.writes) == before
    assert {k: [(c.ref, c.ident, list(c.flags), c.raw) for c in v.values()] for k, v in dst.folders.items()} == snapshot
    deletes = [s for s in acc["sample"] if s["action"] == "delete"]
    assert {(s["kind"], s["folder"]) for s in deletes} == {("mail", "INBOX"), ("contacts", "Contacts")}
    assert all(len(s["subject"]) <= 80 for s in acc["sample"]) and len(acc["sample"]) <= 20
    # the dry run is the default and creates no job
    from app.server import db
    assert db.q1("SELECT COUNT(*) n FROM jobs WHERE mode='mirror'")["n"] == 0


def test_sample_is_capped_and_subjects_truncated(client, box):
    aid, src, dst = make(client, seed=False)
    for i in range(30):
        src.add("INBOX", f"m{i}@x", subject="S" * 300)
    full(client, aid)
    for i in range(25):
        src.remove(f"m{i}@x")
    acc = preview(client, aid, allow_large_delete=True)
    assert len(acc["sample"]) == 20
    assert all(len(s["subject"]) <= 80 for s in acc["sample"])


# ------------------------------------------------------------------ execution
def test_delete_propagated_to_trash_with_mark(client, box):
    aid, src, dst = make(client)
    full(client, aid)
    src.remove("in3@x")
    job, rep = mirror(client, aid)
    assert job["status"] == "done", job
    assert job["mode"] == "mirror"
    assert not [c for c in ours(dst, "INBOX") if "in3@x" in c.raw.decode()]
    trashed = [c for c in dst.all("Trash") if "in3@x" in c.raw.decode()]
    assert len(trashed) == 1 and MIRROR_DELETED_FLAG in trashed[0].flags
    assert rep["mirror"]["mail"]["deleted"] == 1
    assert rep["totals"]["mail"]["mirror"]["deleted"] == 1
    assert job["mirror"]["mail"]["deleted"] == 1
    log = "\n".join(x["msg"] for x in client.get(f"/api/jobs/{job['id']}/log").json()["lines"])
    assert "delete" in log and "trash" in log and "in3@x" not in log  # key and action, no content


def test_mail_arrived_on_target_is_never_deleted(client, box):
    aid, src, dst = make(client)
    full(client, aid)
    dst.native("INBOX", b"Subject: arrived later\r\n\r\nhello\r\n")
    dst.native("INBOX", b"Subject: no header\r\n\r\nx\r\n", ident="<other-namespace-1>")
    src.remove("in1@x")
    job, rep = mirror(client, aid)
    assert job["status"] == "done"
    natives = [c for c in dst.all("INBOX") if not c.ours]
    assert len(natives) == 2
    assert rep["mirror"]["mail"]["deleted"] == 1


def test_flags_and_move_and_contact_update(client, box):
    aid, src, dst = make(client)
    full(client, aid)
    src.get("in2@x").flags = ["\\Seen", "\\Flagged"]
    src.get("in1@x").flags = []          # read -> unread
    src.move("pr0@x", "INBOX")
    src.get("c3").version = "2"
    src.get("c3").body = "new phone"
    job, rep = mirror(client, aid)
    assert job["status"] == "done", job
    c2 = [c for c in ours(dst, "INBOX") if "in2@x" in c.raw.decode()][0]
    assert set(c2.flags) == {"\\Seen", "\\Flagged"}
    c1 = [c for c in ours(dst, "INBOX") if "in1@x" in c.raw.decode()][0]
    assert "\\Seen" not in c1.flags
    assert [c for c in ours(dst, "INBOX") if "pr0@x" in c.raw.decode()]
    assert not [c for c in ours(dst, "Projects") if "pr0@x" in c.raw.decode()]
    assert not [c for c in dst.all("Trash") if "pr0@x" in c.raw.decode()]  # moved, not deleted + copied
    contact = [c for c in ours(dst, "Contacts") if b"UID:c3" in c.raw][0]
    assert b"new phone" in contact.raw
    assert rep["mirror"]["mail"]["updated"] == 3
    assert rep["mirror"]["contacts"]["updated"] == 1
    assert rep["mirror"]["mail"]["added"] == 0


def test_idempotent(client, box):
    aid, src, dst = make(client)
    full(client, aid)
    src.remove("in4@x")
    src.remove("c0")
    src.add("INBOX", "new@x")
    src.get("c2").version = "2"
    src.move("pr2@x", "INBOX")
    mirror(client, aid)
    writes = len(dst.writes)
    acc = preview(client, aid)
    assert acc["to_add"] == acc["to_update"] == acc["to_delete"] == {"mail": 0, "calendar": 0, "contacts": 0, "tasks": 0}
    job, rep = mirror(client, aid)
    assert job["status"] == "done"
    assert len(dst.writes) == writes
    assert rep["mirror"]["mail"] == {"added": 0, "updated": 0, "deleted": 0, "skipped_by_safety": 0, "errors": 0}
    # a delta pass afterwards does not copy the moved mail again
    r = client.post("/api/runs", json={"account_ids": [aid], "mode": "delta"})
    run(client, r.json()[0]["id"])
    assert len(dst.writes) == writes


def test_new_mail_is_added(client, box):
    aid, src, dst = make(client)
    full(client, aid)
    src.add("Projects", "fresh@x")
    job, rep = mirror(client, aid)
    assert [c for c in ours(dst, "Projects") if "fresh@x" in c.raw.decode()]
    assert rep["mirror"]["mail"]["added"] == 1


# ------------------------------------------------------------------ safety
def test_source_never_written(client, box, monkeypatch):
    """BoxSource raises on every mutating method; the mirror wraps the source so that nothing but reads get through."""
    from app.server import mirror as mm
    aid, src, dst = make(client)
    full(client, aid)
    src.remove("in0@x")
    src.get("in2@x").flags = ["\\Flagged"]
    src.get("c1").version = "9"
    job, rep = mirror(client, aid)
    assert job["status"] == "done", job
    ro = mm.ReadOnlySource(mf.BoxSource({"name": "s"}, {}))
    for name in ("append", "delete", "expunge", "store", "set_flags", "move", "update", "put", "mark_read"):
        with pytest.raises(AttributeError):
            getattr(ro, name)
    ro.folders()  # reads pass


def test_source_empty_refused(client, box):
    aid, src, dst = make(client)
    full(client, aid)
    for (kind, name), items in src.folders.items():
        if kind == MAIL:
            items.clear()
    acc = preview(client, aid)
    assert "source_empty" in codes(acc) and acc["ready"] is False
    job, rep = mirror(client, aid)
    assert len(ours(dst, "INBOX")) == 10 and len(ours(dst, "Projects")) == 4
    assert rep["mirror"]["mail"]["deleted"] == 0 and rep["mirror"]["mail"]["skipped_by_safety"] == 14
    assert job["status"] == "done_with_errors"
    # only an explicit allow_large_delete goes through
    acc = preview(client, aid, allow_large_delete=True)
    assert "source_empty" not in codes(acc)


def test_threshold(client, box):
    aid, src, dst = make(client)
    full(client, aid)
    for i in range(4):                       # 4 of 14 mails = 28 % > 20 %
        src.remove(f"in{i}@x")
    acc = preview(client, aid)
    assert "too_many_deletes" in codes(acc)
    assert preview(client, aid, max_delete_percent=30)["ready"] is True
    job, rep = mirror(client, aid)
    assert rep["mirror"]["mail"]["deleted"] == 0 and rep["mirror"]["mail"]["skipped_by_safety"] == 4
    assert len(ours(dst, "INBOX")) == 10
    job, rep = mirror(client, aid, allow_large_delete=True)
    assert rep["mirror"]["mail"]["deleted"] == 4
    assert len(ours(dst, "INBOX")) == 6


def test_uidvalidity_changed_refused(client, box):
    aid, src, dst = make(client)
    full(client, aid)
    src.epoch[(MAIL, "INBOX")] = 2           # every key of INBOX changes
    acc = preview(client, aid)
    assert "uidvalidity_changed" in codes(acc)
    job, rep = mirror(client, aid, allow_large_delete=True)
    assert len(ours(dst, "INBOX")) == 10     # nothing deleted, nothing copied twice
    assert rep["mirror"]["mail"]["deleted"] == 0 and rep["mirror"]["mail"]["added"] == 0


def test_listing_error_refuses_deletes(client, box):
    aid, src, dst = make(client)
    full(client, aid)
    src.remove("in0@x")
    src.list_errors["Projects"] = ConnectorError("server said BAD", ConnectorError.REJECTED)
    acc = preview(client, aid)
    assert "source_unlistable" in codes(acc)
    job, rep = mirror(client, aid)
    assert len(ours(dst, "INBOX")) == 10     # an error in ANOTHER folder of the pass: no deletion at all
    assert rep["mirror"]["mail"]["deleted"] == 0 and rep["mirror"]["mail"]["skipped_by_safety"] >= 1


def test_incomplete_listing_refuses_deletes(client, box):
    aid, src, dst = make(client)
    full(client, aid)
    src.remove("in0@x")
    src.incomplete.add("INBOX")
    acc = preview(client, aid)
    assert "source_unlistable" in codes(acc)
    mirror(client, aid)
    assert len(ours(dst, "INBOX")) == 10


def test_target_unreachable(client, box, monkeypatch):
    aid, src, dst = make(client)
    full(client, aid)
    from app.server.config import settings
    monkeypatch.setattr(settings, "max_retries", 0)
    src.remove("in0@x")
    dst.inventory_errors.add("INBOX")
    acc = preview(client, aid)
    assert "target_unreachable" in codes(acc) and acc["ready"] is False
    job, rep = mirror(client, aid)
    assert job["status"] == "failed" and "target_unreachable" in job["error"]
    assert len(ours(dst, "INBOX")) == 10


def test_unreachable_target_connection(client, box, monkeypatch):
    aid, src, dst = make(client)
    full(client, aid)

    def refuse(self):
        raise ConnectorError("connection refused", ConnectorError.UNREACHABLE)
    monkeypatch.setattr(mf.StoreSession, "connect", refuse)
    acc = preview(client, aid)
    assert "target_unreachable" in codes(acc) and acc["ready"] is False


def test_deleted_twice_is_a_no_op(client, box):
    aid, src, dst = make(client)
    full(client, aid)
    src.remove("in5@x")
    mirror(client, aid)
    n = len(dst.all("Trash"))
    job, rep = mirror(client, aid)
    assert len(dst.all("Trash")) == n and rep["mirror"]["mail"]["deleted"] == 0


def test_contact_deleted_and_restored(client, box):
    aid, src, dst = make(client)
    full(client, aid)
    m = src.get("c4")
    src.remove("c4")
    job, rep = mirror(client, aid)
    assert rep["mirror"]["contacts"]["deleted"] == 1
    assert not [c for c in ours(dst, "Contacts") if b"UID:c4" in c.raw]
    src.add("Contacts", "c4", kind=CONTACTS, subject=m.subject)   # back at the source
    job, rep = mirror(client, aid)
    assert rep["mirror"]["contacts"]["added"] == 1
    assert [c for c in ours(dst, "Contacts") if b"UID:c4" in c.raw]


def test_rows_without_version_compare_with_the_target(client, box):
    """Items migrated before versions were kept: flags are compared with what the target holds, once."""
    from app.server import db
    aid, src, dst = make(client)
    full(client, aid)
    db.ex("UPDATE items SET version=NULL")
    src.get("in2@x").flags = ["\\Flagged"]
    acc = preview(client, aid)
    assert acc["to_update"]["mail"] == 1, acc
    job, rep = mirror(client, aid)
    assert rep["mirror"]["mail"]["updated"] == 1
    assert db.q1("SELECT COUNT(*) n FROM items WHERE kind='mail' AND version IS NULL")["n"] == 0
    assert preview(client, aid)["to_update"]["mail"] == 0


def test_hard_delete_keeps_a_copy_first(client, box, monkeypatch, env):
    from app.connectors.base import CAP_READBACK, DELETE_HARD
    aid, src, dst = make(client)
    full(client, aid)

    def hard(self, folder, ref, expected_import_id=None):
        with self.s.lock:
            self.s.folders[folder].pop(ref, None)
        return DELETE_HARD
    monkeypatch.setattr(mf.StoreSession, "delete_mode", lambda self, folder: DELETE_HARD)
    monkeypatch.setattr(mf.StoreSession, "delete", hard)
    src.remove("in7@x")
    job, rep = mirror(client, aid)       # no read-back: no copy can be kept, nothing is deleted
    assert rep["mirror"]["mail"]["deleted"] == 0 and rep["mirror"]["mail"]["skipped_by_safety"] == 1
    assert len(ours(dst, "INBOX")) == 10
    monkeypatch.setattr(mf.StoreTarget, "capabilities", mf.StoreTarget.capabilities | {CAP_READBACK})
    job, rep = mirror(client, aid)
    assert rep["mirror"]["mail"]["deleted"] == 1
    copies = list((env.data_dir / "mirror-trash").rglob("*.eml"))
    assert len(copies) == 1 and b"in7@x" in copies[0].read_bytes()


def test_item_missing_from_listing_but_still_readable_is_kept(client, box):
    aid, src, dst = make(client)
    full(client, aid)
    src.hidden.add("in6@x")                  # the listing misses it, a direct read finds it
    src.remove("in8@x")
    job, rep = mirror(client, aid)
    assert rep["mirror"]["mail"]["deleted"] == 1 and rep["mirror"]["mail"]["skipped_by_safety"] == 1
    assert [c for c in ours(dst, "INBOX") if "in6@x" in c.raw.decode()]


def test_exists_error_refuses_every_deletion(client, box):
    aid, src, dst = make(client)
    full(client, aid)
    src.remove("in8@x")
    src.exists_errors["INBOX"] = ConnectorError("timeout", ConnectorError.REJECTED)
    acc = preview(client, aid)
    assert "source_unlistable" in codes(acc)
    mirror(client, aid)
    assert len(ours(dst, "INBOX")) == 10


def test_exists_gets_the_recorded_source_ref(client, box):
    aid, src, dst = make(client)
    full(client, aid)
    uid = src.find("in8@x")[1]
    src.remove("in8@x")
    mirror(client, aid)
    assert list(src.exists_refs.values()) == [uid]


def test_folder_gone_from_the_source_deletes_nothing(client, box):
    aid, src, dst = make(client)
    full(client, aid)
    del src.folders[(MAIL, "Projects")]
    acc = preview(client, aid)
    assert "source_unlistable" in codes(acc) and "Projects" in str(acc["blocked"])
    job, rep = mirror(client, aid)
    assert len(ours(dst, "Projects")) == 4 and rep["mirror"]["mail"]["skipped_by_safety"] == 4


def test_untrusted_epoch_refuses_the_folder(client, box):
    aid, src, dst = make(client)
    full(client, aid)
    src.remove("in8@x")
    src.unstable.add("INBOX")
    acc = preview(client, aid)
    assert "uidvalidity_changed" in codes(acc)
    mirror(client, aid)
    assert len(ours(dst, "INBOX")) == 10


def test_deletes_go_in_batches(client, box, monkeypatch):
    calls = []
    real = mf.StoreSession.delete_many

    def spy(self, folder, refs, expected=None):
        calls.append((folder, len(refs), dict(expected or {})))
        return real(self, folder, refs, expected)
    monkeypatch.setattr(mf.StoreSession, "delete_many", spy)
    aid, src, dst = make(client)
    full(client, aid)
    src.remove("in8@x")
    src.remove("in9@x")
    job, rep = mirror(client, aid)
    assert rep["mirror"]["mail"]["deleted"] == 2
    assert [(f, n) for f, n, _ in calls] == [("INBOX", 2)]
    assert all(v.startswith("<src-") for v in calls[0][2].values())
