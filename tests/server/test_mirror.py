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
    assert acc["mirror"] is False and acc["options"]["mirror"] is False
    assert client.put(f"/api/accounts/{aid}", json={"options": {"mirror": True}}).status_code == 200
    assert client.get(f"/api/accounts/{aid}").json()["options"]["mirror"] is True
    assert client.put(f"/api/accounts/{aid}", json={"mirror": False}).status_code == 200
    assert client.get(f"/api/accounts/{aid}").json()["options"]["mirror"] is False


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


def test_a_copy_that_did_not_end_well_still_counts_as_a_prior_run(client, box):
    """A first copy stopped or failed half-way wrote items: the mirror may run on them (it copies the rest first)."""
    from app.server import db
    aid, src, dst = make(client)
    job = full(client, aid)
    db.ex("UPDATE jobs SET status='stopped' WHERE id=?", (job["id"],))
    assert "no_prior_run" not in codes(preview(client, aid))
    src.add("INBOX", "late@x")
    src.remove("in0@x")
    job, rep = mirror(client, aid)
    assert rep["mirror"]["mail"]["added"] == 1 and rep["mirror"]["mail"]["deleted"] == 1


def test_preview_of_several_mailboxes_keeps_the_order_asked(client, box):
    from app.server import mirror as mm
    aid, _, _ = make(client)
    full(client, aid)
    out = mm.preview_accounts([aid, 999999, aid], mm.Options(), parallel=3)
    assert [a["account_id"] for a in out] == [aid, 999999, aid]
    assert out[0]["ready"] is True and out[2]["ready"] is True
    assert "unknown_account" in codes(out[1])
    r = client.post("/api/runs", json={"account_ids": [aid, 999999], "mode": "mirror"})
    assert [a["account_id"] for a in r.json()["accounts"]] == [aid, 999999]


def test_a_job_over_the_limit_deletes_nothing_and_says_why(client, box):
    """Started without a comparison first: the job checks the limit itself."""
    aid, src, dst = make(client)
    full(client, aid)
    for i in range(4):
        src.remove(f"in{i}@x")
    src.add("INBOX", "new@x")
    job, rep = mirror(client, aid, min_delete_count=0)
    assert rep["mirror"]["mail"]["deleted"] == 0 and rep["mirror"]["mail"]["skipped_by_safety"] == 4
    assert rep["mirror"]["mail"]["added"] == 1           # the catch-up is done all the same
    assert "too_many_deletes" in {b["code"] for b in rep["mirror"]["blocked"]}
    assert job["status"] == "done_with_errors", job
    log = client.get(f"/api/jobs/{job['id']}/log").text
    assert "deletions held back" in log


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
    for i in range(4):                       # 4 of 14 mails = 28 % > 20 % (floor of the threshold lowered to 0)
        src.remove(f"in{i}@x")
    acc = preview(client, aid, min_delete_count=0)
    assert "too_many_deletes" in codes(acc)
    assert "max_delete_percent" in str(acc["blocked"]) and "allow_large_delete" in str(acc["blocked"])  # the remedy
    assert preview(client, aid, max_delete_percent=30, min_delete_count=0)["ready"] is True
    job, rep = mirror(client, aid, min_delete_count=0)
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


def test_target_marking_by_import_id_with_source_resource_names(client, box):
    """Contacts named by the source (c3.vcf, not derived from the import id) on a target that knows its items only
    by import id (EWS, Graph, JMAP): found again, updated and deleted, never copied twice."""
    from app.server import db
    aid, src, dst = make(client)
    dst.by_import_id = True
    full(client, aid)
    assert db.q1("SELECT hash FROM items WHERE kind='contact' LIMIT 1")["hash"].endswith(".vcf")

    def copies(uid):
        return [c for c in ours(dst, "Contacts") if f"UID:{uid}\r".encode() in c.raw]
    src.get("c3").version = "2"
    src.get("c3").body = "new phone"
    src.remove("c4")
    acc = preview(client, aid)
    assert acc["to_update"]["contacts"] == 1 and acc["to_delete"]["contacts"] == 1, acc
    job, rep = mirror(client, aid)
    assert rep["mirror"]["contacts"] == {"added": 0, "updated": 1, "deleted": 1, "skipped_by_safety": 0, "errors": 0}
    assert len(copies("c3")) == 1 and b"new phone" in copies("c3")[0].raw
    assert copies("c4") == []
    assert all(len(copies(f"c{i}")) == 1 for i in range(4))
    acc = preview(client, aid)
    assert acc["to_add"]["contacts"] == acc["to_update"]["contacts"] == acc["to_delete"]["contacts"] == 0


# ------------------------------------------------------------------ review fixes (docs/planning.md "Mirror", safety)
def key_of_subject(subject, kind="mail"):
    from app.server import db
    return db.q1("SELECT item_key FROM items WHERE kind=? AND subject=?", (kind, subject))["item_key"]


def test_exists_leaving_a_key_out_keeps_the_item(client, box):
    """An answer of exists() without the key is not a "gone": the item stays on the target."""
    aid, src, dst = make(client)
    full(client, aid)
    src.exists_omit.add(key_of_subject("in8@x"))
    src.remove("in8@x")
    job, rep = mirror(client, aid)
    assert rep["mirror"]["mail"]["deleted"] == 0 and rep["mirror"]["mail"]["skipped_by_safety"] == 1
    assert [c for c in ours(dst, "INBOX") if "in8@x" in c.raw.decode()]


def test_confirm_gone_without_the_source_folder_keeps_the_item():
    """A deletion whose folder the pass does not know (no folder to ask exists() in) is held."""
    from app.server import mirror as mm
    an = mm.Analysis()
    row = {"id": 1, "item_key": "k1", "folder": "Elsewhere", "src_ref": None, "status": "ok"}
    an.actions["mail"] = [mm.Action("delete", "mail", row=row)]
    an.skipped["mail"] = 0

    class Src:
        def exists(self, folder, keys, refs=None):
            return {k: False for k in keys}
    mm._confirm_gone(an, Src(), [], lambda fn: fn(), lambda *a, **k: None)
    assert an.of("mail", "delete") == [] and an.skipped["mail"] == 1


def test_one_resource_shared_by_two_source_folders_is_not_deleted_while_one_has_it(client, box):
    """C1: the same contact (same UID, so the same resource name) in two source folders mapped to one target
    collection: one resource on the target. Removed from one folder only, it must stay on the target."""
    from app.server import db
    aid, src, dst = make(client)
    src.add("Team", "u1", kind=CONTACTS, subject="Shared")
    src.add("Team", "t2", kind=CONTACTS, subject="Team only")
    src.add("Contacts", "u1", kind=CONTACTS, subject="Shared")
    dst.plan = {"Team": "Contacts"}
    full(client, aid)
    rows = db.q("SELECT folder, target, hash, status FROM items WHERE kind='contact' AND hash='u1.vcf'")
    assert len(rows) == 2 and {r["target"] for r in rows} == {"Contacts"}
    src.remove("u1", "Team")
    acc = preview(client, aid)
    assert acc["to_delete"]["contacts"] == 0, acc
    job, rep = mirror(client, aid)
    assert rep["mirror"]["contacts"]["deleted"] == 0
    assert [c for c in ours(dst, "Contacts") if b"UID:u1\r" in c.raw]
    # gone from both: deleted
    src.remove("u1", "Contacts")
    job, rep = mirror(client, aid)
    assert not [c for c in ours(dst, "Contacts") if b"UID:u1\r" in c.raw]


def test_threshold_counts_only_the_folders_mirrored(client, box):
    """M2: an excluded folder holding most of the mailbox does not dilute the threshold."""
    aid, src, dst = make(client)
    for i in range(60):
        src.add("Archive", f"ar{i}@x")
    full(client, aid)
    r = client.put(f"/api/accounts/{aid}/policy", json={"exclude_folders": ["Archive"]})
    assert r.status_code == 200, r.text
    src.folders[(MAIL, "INBOX")].clear()          # an index incident: INBOX lists nothing
    acc = preview(client, aid)
    assert {"source_empty", "too_many_deletes"} & codes(acc) and acc["ready"] is False, acc
    job, rep = mirror(client, aid)
    assert len(ours(dst, "INBOX")) == 10 and rep["mirror"]["mail"]["deleted"] == 0


def test_threshold_denominator_leaves_out_excluded_folders(client, box):
    """M2: 3 of the 14 mails mirrored (21 %) is over 20 %, whatever an excluded folder holds."""
    aid, src, dst = make(client)
    for i in range(60):
        src.add("Archive", f"ar{i}@x")
    full(client, aid)
    assert client.put(f"/api/accounts/{aid}/policy", json={"exclude_folders": ["Archive"]}).status_code == 200
    for i in range(3):
        src.remove(f"in{i}@x")
    assert "too_many_deletes" in codes(preview(client, aid, min_delete_count=0))


def test_a_folder_listing_nothing_is_refused_by_itself(client, box):
    """M2: one folder emptied at the source is refused (source_empty), however small it is in the mailbox."""
    aid, src, dst = make(client)
    for i in range(30):
        src.add("INBOX", f"more{i}@x")
    full(client, aid)
    src.folders[(MAIL, "Projects")].clear()        # 4 of 44 mails: under the threshold
    acc = preview(client, aid)
    assert "source_empty" in codes(acc) and "Projects" in str(acc["blocked"]), acc
    job, rep = mirror(client, aid)
    assert len(ours(dst, "Projects")) == 4 and rep["mirror"]["mail"]["skipped_by_safety"] == 4
    job, rep = mirror(client, aid, allow_large_delete=True)
    assert len(ours(dst, "Projects")) == 0 and rep["mirror"]["mail"]["deleted"] == 4


def test_update_of_an_item_gone_from_the_target_writes_it_again(client, box):
    """M3: an item to update that the target no longer has (an update that failed half-way, a deletion by hand) is
    added again, not forgotten."""
    aid, src, dst = make(client)
    full(client, aid)
    src.get("c3").version = "2"
    src.get("c3").body = "new phone"
    for ref, c in list(dst.folders["Contacts"].items()):
        if b"UID:c3\r" in c.raw:
            del dst.folders["Contacts"][ref]
    acc = preview(client, aid)
    assert acc["to_add"]["contacts"] == 1, acc
    job, rep = mirror(client, aid)
    got = [c for c in ours(dst, "Contacts") if b"UID:c3\r" in c.raw]
    assert len(got) == 1 and b"new phone" in got[0].raw
    assert rep["mirror"]["contacts"]["added"] == 1


def test_unfinished_item_still_at_the_source_is_not_given_up(client, box):
    """M4: an item in error that a listing misses is only given up when exists() says it is gone."""
    from app.server import db
    aid, src, dst = make(client)
    full(client, aid)
    db.ex("UPDATE items SET status='error', error='target: boom', hash=NULL WHERE subject='in6@x'")
    src.hidden.add("in6@x")                       # missed by the listing, still there
    mirror(client, aid)
    assert db.q1("SELECT status FROM items WHERE subject='in6@x'")["status"] == "error"
    src.hidden.clear()
    src.remove("in6@x")                           # really gone: given up
    mirror(client, aid)
    assert db.q1("SELECT status FROM items WHERE subject='in6@x'")["status"] == "skipped"


def test_unfinished_item_is_not_given_up_when_a_listing_failed(client, box):
    """M4: one listing error in the pass and nothing is given up either."""
    from app.server import db
    aid, src, dst = make(client)
    full(client, aid)
    db.ex("UPDATE items SET status='error', error='target: boom', hash=NULL WHERE subject='in6@x'")
    src.remove("in6@x")
    src.list_errors["Projects"] = ConnectorError("server said BAD", ConnectorError.REJECTED)
    mirror(client, aid)
    assert db.q1("SELECT status FROM items WHERE subject='in6@x'")["status"] == "error"


def test_slash_in_a_folder_name_does_not_mix_two_folders(client, box):
    """m1: the folder "A/B" (one name) and the folder B inside A have the same legacy key "A/B": the rows of one are
    never taken for rows of the other, so nothing is deleted."""
    aid, src, dst = make(client, seed=False)
    for i in range(5):
        src.add("A|B", f"y{i}@x")
    for i in range(2):
        src.add("A/B", f"x{i}@x")
    full(client, aid)
    acc = preview(client, aid)  # the 5 rows of B inside A are not taken for deletions of "A/B" (nor counted so)
    assert acc["ready"] is True and acc["blocked"] == [], acc
    acc = preview(client, aid, allow_large_delete=True)
    assert acc["to_delete"]["mail"] == 0, acc
    job, rep = mirror(client, aid, allow_large_delete=True)
    assert rep["mirror"]["mail"]["deleted"] == 0
    assert len([c for c in dst.all("A/B") if c.ours]) == 7


def test_no_move_when_a_listing_failed(client, box):
    """m5: a move takes the mail out of its old folder on the target: not while any listing failed."""
    aid, src, dst = make(client)
    src.add("Other", "o1@x")
    full(client, aid)
    src.move("pr0@x", "INBOX")
    src.list_errors["Other"] = ConnectorError("server said BAD", ConnectorError.REJECTED)
    mirror(client, aid)
    assert [c for c in ours(dst, "Projects") if "pr0@x" in c.raw.decode()]


def test_no_move_when_the_old_copy_is_still_at_the_source(client, box):
    """m5: same Message-ID new in INBOX while the listing of Projects misses it (still there): a copy, not a move."""
    aid, src, dst = make(client)
    full(client, aid)
    src.add("INBOX", "pr0@x")                     # a copy of it in INBOX
    src.hidden_in.add(("Projects", "pr0@x"))      # the listing of Projects misses the original, still there
    mirror(client, aid)
    assert [c for c in ours(dst, "Projects") if "pr0@x" in c.raw.decode()]
    assert [c for c in ours(dst, "INBOX") if "pr0@x" in c.raw.decode()]


def test_mirror_on_a_mailbox_with_an_active_job_is_blocked(client, box):
    """m7: a job already queued or running: refused explicitly (job_active), its options would be lost otherwise."""
    aid, src, dst = make(client)
    full(client, aid)
    r = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror", "dry_run": False})
    assert len(r.json()["jobs"]) == 1
    r = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror", "dry_run": False,
                                       "allow_large_delete": True})
    body = r.json()
    assert body["jobs"] == [] and "job_active" in codes(body["accounts"][0])
    assert "job_id" not in body["accounts"][0] and body["accounts"][0]["ready"] is False
    acc = preview(client, aid)
    assert "job_active" in codes(acc) and acc["ready"] is False


def test_real_mirror_run_is_audited(client, box, caplog):
    """m8: one line of the application log per real mirror request, with its options and who asked (no secret)."""
    import logging
    aid, src, dst = make(client)
    full(client, aid)
    with caplog.at_level(logging.INFO):
        client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror", "dry_run": False,
                                       "max_delete_percent": 7, "allow_large_delete": True})
    lines = [r.getMessage() for r in caplog.records if "audit mirror run requested" in r.getMessage().lower()]
    assert len(lines) == 1, [r.getMessage() for r in caplog.records]
    line = lines[0]
    assert f"accounts=[{aid}]" in line and "allow_large_delete=True" in line and "max_delete_percent=7" in line
    assert "via=session" in line
    caplog.clear()
    with caplog.at_level(logging.INFO):  # a dry run is not audited
        client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror"})
    assert not [r for r in caplog.records if "audit" in r.getMessage().lower()]


def test_soft_delete_without_trash_is_announced(client, box):
    """m3: a target without a trash only marks the mails: the dry run says so (notes), it is no block."""
    aid, src, dst = make(client)
    full(client, aid)
    dst.trash = "INBOX"          # deleting in the trash itself: only marked
    src.remove("in3@x")
    acc = preview(client, aid)
    assert acc["ready"] is True and "soft_delete" in {n["code"] for n in acc["notes"]}, acc
    job, rep = mirror(client, aid)
    assert "soft_delete" in {n["code"] for n in rep["mirror"]["notes"]}


def test_hard_delete_without_read_back_is_announced(client, box, monkeypatch):
    """m6: a target that deletes for good and cannot give a copy back never deletes: said in the dry run."""
    from app.connectors.base import DELETE_HARD
    aid, src, dst = make(client)
    full(client, aid)
    monkeypatch.setattr(mf.StoreSession, "delete_mode", lambda self, folder: DELETE_HARD)
    src.remove("c2")
    acc = preview(client, aid)
    assert "no_copy_kept" in {n["code"] for n in acc["notes"]}, acc
    job, rep = mirror(client, aid)
    assert rep["mirror"]["contacts"]["skipped_by_safety"] == 1 and rep["mirror"]["contacts"]["deleted"] == 0
    assert "no_copy_kept" in {n["code"] for n in rep["mirror"]["notes"]}


def test_backup_copies_are_private_and_purged(client, env):
    """m2: copies kept before a final deletion are readable by mccb only, and purged after the retention."""
    import os
    import time
    from app.server import db
    from app.server import mirror as mm
    p = mm.backup_copy(7, "contacts", "x.vcf", b"BEGIN:VCARD\r\nEND:VCARD\r\n")
    assert os.stat(p).st_mode & 0o777 == 0o600
    d = env.data_dir / "mirror-trash"
    for sub in (d, d / "job-7", d / "job-7" / "contacts"):
        assert os.stat(sub).st_mode & 0o777 == 0o700, sub
    assert mm.purge_backups(days=30) == 0                     # too recent
    old = time.time() - 31 * 86400
    for x in [d / "job-7", *(d / "job-7").rglob("*")]:
        os.utime(x, (old, old))
    sid = client.post("/api/servers", json=SRV).json()["id"]
    aid = client.post("/api/accounts", json={"server_id": sid, "email": "p@example.test", "password": "pw"}).json()["id"]
    db.ex("INSERT INTO jobs(account_id, status, phase, mode, created_at) VALUES (?, 'running', 'mirror', 'mirror', ?)",
          (aid, time.time()))
    assert mm.purge_backups(days=30) == 0                     # never during a job
    db.ex("UPDATE jobs SET status='done'")
    assert mm.purge_backups(days=30) == 1 and not (d / "job-7").exists()
    assert mm.purge_backups(days=0) == 0                      # 0: kept for ever


# ------------------------------------------------------------------ second review
def copies_of(dst, folder, uid):
    return [c for c in ours(dst, folder) if f"UID:{uid}\r".encode() in c.raw]


def test_update_by_replacement_converges_to_one_copy(client, box):
    """M1: an Exchange-like target writes the new copy, then trashes the old one. That second step fails once: the
    update is not replayed (no third copy), it is an error, and the next pass leaves exactly one up-to-date copy."""
    aid, src, dst = make(client)
    dst.by_import_id = True
    dst.update_replaces = True
    full(client, aid)
    src.get("c3").version = "2"
    src.get("c3").body = "new phone"
    dst.fail_trash = 1
    job, rep = mirror(client, aid)
    assert dst.updates == 1, "update() replayed"
    assert rep["mirror"]["contacts"]["errors"] == 1 and rep["mirror"]["contacts"]["updated"] == 0
    job, rep = mirror(client, aid)
    got = copies_of(dst, "Contacts", "c3")
    assert len(got) == 1 and b"new phone" in got[0].raw, [c.raw for c in got]
    assert rep["mirror"]["contacts"]["updated"] == 1
    acc = preview(client, aid)
    assert acc["to_update"]["contacts"] == 0 and acc["to_delete"]["contacts"] == 0, acc


def test_update_with_two_copies_updates_one_and_trashes_the_other(client, box):
    """M1: two copies of one item on the target (a trash that failed earlier): one is updated, the other goes to the
    trash only once the update has succeeded."""
    aid, src, dst = make(client)
    dst.by_import_id = True
    dst.update_replaces = True
    full(client, aid)
    [(ref, c)] = [(r, c) for r, c in dst.folders["Contacts"].items() if b"UID:c3\r" in c.raw]
    dst.folders["Contacts"]["dup"] = mf.Copy("dup", c.ident, c.raw, [])
    src.get("c3").version = "2"
    src.get("c3").body = "new phone"
    mirror(client, aid)
    got = copies_of(dst, "Contacts", "c3")
    assert len(got) == 1 and b"new phone" in got[0].raw and dst.updates == 1


def test_update_is_never_replayed(client, box, monkeypatch):
    """M1: update() goes through no replay at all: a transient failure is one error, tried again at the next pass."""
    aid, src, dst = make(client)
    full(client, aid)
    src.get("c3").version = "2"
    calls = []

    def flaky(self, folder, ref, item):
        calls.append(ref)
        raise ConnectorError("timeout", ConnectorError.TRANSIENT)
    monkeypatch.setattr(mf.StoreSession, "update", flaky)
    job, rep = mirror(client, aid)
    assert len(calls) == 1 and rep["mirror"]["contacts"]["errors"] == 1


def test_small_collection_is_not_blocked_by_the_percentage(client, box):
    """minor 1: the percentage applies only above min_delete_count deletions (10 by default)."""
    aid, src, dst = make(client)
    full(client, aid)
    src.remove("c1")
    src.remove("c2")                              # 2 of 5 contacts: 40 %, but only 2 deletions
    acc = preview(client, aid)
    assert acc["ready"] is True and acc["to_delete"]["contacts"] == 2, acc
    for i in range(10):                           # 11 deletions of 30: 37 % above the floor
        src.add("Big", f"b{i}@x")
    for i in range(20):
        src.add("Big", f"k{i}@x")
    full(client, aid)
    for i in range(10):
        src.remove(f"b{i}@x")
    src.remove("in0@x")
    acc = preview(client, aid, max_delete_percent=20)
    assert "too_many_deletes" in codes(acc), acc


def test_one_empty_folder_can_be_allowed_by_itself(client, box):
    """minor 1: an emptied folder is lifted by its key (allow_empty_folders), named in the block, not for all."""
    aid, src, dst = make(client)
    src.add("Solo", "solo@x")
    for i in range(30):
        src.add("INBOX", f"more{i}@x")
    full(client, aid)
    src.folders[(MAIL, "Solo")].clear()
    src.folders[(MAIL, "Projects")].clear()
    acc = preview(client, aid)
    empty = [b for b in acc["blocked"] if b["code"] == "source_empty"]
    assert {b.get("folder") for b in empty} == {"Solo", "Projects"}, acc
    assert all("allow_empty_folders" in b["message"] for b in empty)
    acc = preview(client, aid, allow_empty_folders=["Solo"])
    assert [b.get("folder") for b in acc["blocked"] if b["code"] == "source_empty"] == ["Projects"], acc
    job, rep = mirror(client, aid, allow_empty_folders=["Solo"])
    assert ours(dst, "Solo") == [] and len(ours(dst, "Projects")) == 4


def test_move_of_a_mail_gone_from_the_target_adds_it(client, box):
    """minor 2: a mail deleted by hand on the target, then moved at the source: added to its new folder."""
    aid, src, dst = make(client)
    full(client, aid)
    for ref, c in list(dst.folders["Projects"].items()):
        if "pr0@x" in c.raw.decode():
            del dst.folders["Projects"][ref]
    src.move("pr0@x", "INBOX")
    job, rep = mirror(client, aid)
    assert [c for c in ours(dst, "INBOX") if "pr0@x" in c.raw.decode()]


def test_shared_resource_counts_rows_of_a_skipped_folder(client, box):
    """minor 3: a row whose folder is no longer migrated (skipped) still holds the shared resource."""
    from app.server import db
    aid, src, dst = make(client)
    src.add("Team", "u1", kind=CONTACTS, subject="Shared")
    src.add("Team", "t2", kind=CONTACTS, subject="Team only")
    src.add("Contacts", "u1", kind=CONTACTS, subject="Shared")
    dst.plan = {"Team": "Contacts"}
    full(client, aid)
    db.ex("UPDATE items SET status='skipped', error='folder not migrated' WHERE folder='Team'")
    src.remove("u1", "Contacts")
    mirror(client, aid)
    assert copies_of(dst, "Contacts", "u1")


def test_shared_resource_names_without_case(client, box):
    """minor 3: "U1.vcf" and "u1.vcf" are one resource on a server that ignores case: kept while one is listed,
    and the second is never written over the first."""
    aid, src, dst = make(client)
    dst.casefold = True
    src.add("Team", "U1", kind=CONTACTS, subject="Shared upper")
    src.add("Team", "t2", kind=CONTACTS, subject="Team only")
    src.add("Contacts", "u1", kind=CONTACTS, subject="Shared lower")
    dst.plan = {"Team": "Contacts"}
    full(client, aid)
    puts = [w for w in dst.writes if w[0] == "put" and w[2].casefold() == "u1.vcf"]
    assert len(puts) == 1, dst.writes
    from app.server import db  # rows of an earlier version: each with its own case
    db.ex("UPDATE items SET hash='U1.vcf' WHERE folder='Team' AND hash='u1.vcf'")
    src.remove("U1", "Team")
    mirror(client, aid)
    assert [c for c in ours(dst, "Contacts") if c.ident.casefold() == "u1.vcf"]


def test_purge_survives_a_broken_link(client, env):
    """minor 5: a dangling symbolic link in mirror-trash does not stop the purge."""
    import os
    import time
    from app.server import mirror as mm
    mm.backup_copy(8, "contacts", "y.vcf", b"x")
    d = env.data_dir / "mirror-trash" / "job-8"
    os.symlink(d / "nowhere", d / "contacts" / "dangling")
    old = time.time() - 40 * 86400
    for x in [d, *d.rglob("*")]:
        os.utime(x, (old, old), follow_symlinks=False)
    assert mm.purge_backups(days=30) == 1 and not d.exists()


def test_rows_without_source_ref_get_it_back(client, box):
    """minor 6: a row migrated before src_ref was kept gets it when the mirror lists its item again."""
    from app.server import db
    aid, src, dst = make(client)
    full(client, aid)
    db.ex("UPDATE items SET src_ref=NULL")
    preview(client, aid)
    assert db.q1("SELECT COUNT(*) n FROM items WHERE src_ref IS NULL")["n"] > 0  # the dry run writes nothing
    mirror(client, aid)
    assert db.q1("SELECT COUNT(*) n FROM items WHERE status='ok' AND src_ref IS NULL")["n"] == 0


def test_audit_is_written_before_and_in_the_job_log(client, box, caplog, monkeypatch):
    """minor 7: the audit line is written before the request runs (also when it fails), and the job log says who
    asked and with which options."""
    import logging
    from app.server import mirror as mm
    aid, src, dst = make(client)
    full(client, aid)
    r = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror", "dry_run": False,
                                       "max_delete_percent": 9})
    jid = r.json()["jobs"][0]["id"]
    log = "\n".join(x["msg"] for x in client.get(f"/api/jobs/{jid}/log").json()["lines"])
    assert "requested by session" in log and "max_delete_percent=9" in log, log
    run(client, jid)

    def boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(mm, "start", boom)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        try:
            client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror", "dry_run": False})
        except RuntimeError:
            pass
    assert [r for r in caplog.records if "AUDIT mirror run requested" in r.getMessage()]


def test_job_started_meanwhile_is_job_active(client, box, monkeypatch):
    """minor 8: a job created between the check and the creation is still reported as job_active, never as the
    mirror job (its options would be lost)."""
    from app.server import mirror as mm
    from app.server import runs
    aid, src, dst = make(client)
    full(client, aid)
    real = runs.create_jobs

    def racing(ids, *a, **kw):
        real(ids, None, "full")       # another request wins the race
        return real(ids, *a, **kw)
    monkeypatch.setattr(runs, "create_jobs", racing)
    body = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror", "dry_run": False}).json()
    acc = body["accounts"][0]
    assert "job_active" in codes(acc) and "job_id" not in acc and body["jobs"] == [], body
    monkeypatch.setattr(runs, "create_jobs", real)
    assert mm  # (imported for the patch target's module)


def test_unknown_mailbox_has_its_own_code(client, box):
    acc = preview(client, 99999)
    assert codes(acc) == {"unknown_account"}
    body = client.post("/api/runs", json={"account_ids": [99999], "mode": "mirror", "dry_run": False}).json()
    assert codes(body["accounts"][0]) == {"unknown_account"}


# ------------------------------------------------------------------ third review
def test_shared_resource_waits_for_the_write_in_progress(client, box):
    """m1: the same UID in two folders: the second is recorded as sharing the resource only once the first is
    written; that write failing, the second is written itself, never "ok" for nothing on the target."""
    from app.server import db
    aid, src, dst = make(client)
    src.add("Team", "u1", kind=CONTACTS, subject="Shared")
    src.add("Contacts", "u1", kind=CONTACTS, subject="Shared")
    dst.plan = {"Team": "Contacts"}
    dst.fail_put.add("u1.vcf")                     # the first write of u1.vcf is refused for good
    r = client.post("/api/runs", json={"account_ids": [aid]})
    run(client, r.json()[0]["id"])
    ok = db.q1("SELECT COUNT(*) n FROM items WHERE kind='contact' AND hash='u1.vcf' AND status='ok'")["n"]
    assert ok >= 1 and copies_of(dst, "Contacts", "u1"), (ok, dst.writes)


def test_update_failure_closes_the_dead_session(client, box):
    """m4: an update that fails on a lost connection closes the session: the next update reconnects."""
    aid, src, dst = make(client)
    full(client, aid)
    src.get("c2").version = "2"
    src.get("c3").version = "2"
    dst.update_kills = 1
    job, rep = mirror(client, aid)
    assert rep["mirror"]["contacts"]["errors"] == 1 and rep["mirror"]["contacts"]["updated"] == 1, rep["mirror"]


def test_allow_empty_folders_entries_are_bounded(client, box):
    """m5: one folder key is at most 1024 characters."""
    aid, src, dst = make(client)
    r = client.post("/api/runs", json={"account_ids": [aid], "mode": "mirror", "allow_empty_folders": ["x" * 1025]})
    assert r.status_code == 422
