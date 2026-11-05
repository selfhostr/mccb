"""Import order newest -> oldest (app/server/ordering.py): date indexing in builder processes, global sort
across folders, no-date items last, ties by item_key, crash isolation of the date read, resume order,
DAV order (calendar start desc, contacts last modification desc)."""
import time

import pytest

from tests.server import fake_engine
from tests.server.test_runner import (DEFAULT_FOLDERS, IMAP_HOST, WorkerThread, _log, imap_conn, run,
                                      server_import_ids, setup_account, wait_job)

needs_imap = pytest.mark.skipif(not IMAP_HOST, reason="TEST_IMAP_HOST not set")

INBOX = (["Boîte de réception"], "mail", "IPF.Note", "inbox", 5)
SUB = (["Boîte de réception", "Projets"], "mail", "IPF.Note", None, 4)
SENT = (["Éléments envoyés"], "mail", "IPF.Note", "sent", 3)
CAL = (["Calendrier"], "calendar", "IPF.Appointment", None, 4)
CONTACTS = (["Contacts"], "contacts", "IPF.Contact", None, 3)
T0 = 1_700_000_000


def _ref(folder, i):
    return f"{'/'.join(folder[0])}#{i}"


# interleaved dates across folders, one item without date, two items with the same date (tie -> item_key)
DATES = {
    _ref(INBOX, 0): T0 + 100, _ref(INBOX, 1): T0 + 900, _ref(INBOX, 2): None, _ref(INBOX, 3): T0 + 400,
    _ref(INBOX, 4): T0 + 50,
    _ref(SUB, 0): T0 + 800, _ref(SUB, 1): T0 + 400, _ref(SUB, 2): T0 + 10, _ref(SUB, 3): T0 + 600,
    _ref(SENT, 0): T0 + 700, _ref(SENT, 1): T0 + 1000, _ref(SENT, 2): T0 + 300,
    _ref(CAL, 0): T0 + 5, _ref(CAL, 1): T0 + 500, _ref(CAL, 2): None, _ref(CAL, 3): T0 + 90,
    _ref(CONTACTS, 0): T0 + 1, _ref(CONTACTS, 1): T0 + 3, _ref(CONTACTS, 2): T0 + 2,
}


def expected_order(folders):
    """item keys newest first, no date last, ties by item_key."""
    rows = []
    for fo in folders:
        for i in range(fo[4]):
            ts = fake_engine.CONFIG["dates"].get(_ref(fo, i), "default")
            assert ts != "default"
            key = fake_engine.item_key(fo[0], i)
            rows.append(((-ts if ts is not None else float("inf")), key))
    return [k for _, k in sorted(rows)]


@pytest.fixture(autouse=True)
def reset_fake():
    fake_engine.CONFIG.update(folders=list(DEFAULT_FOLDERS), delay=0.0, fail_keys=set(), reject_keys=set(),
                              unreadable=set(), warnings={}, dates={})
    yield
    fake_engine.CONFIG.update(folders=list(DEFAULT_FOLDERS), delay=0.0, fail_keys=set(), reject_keys=set(),
                              unreadable=set(), warnings={}, dates={})


# ------------------------------------------------------------------ ordering.build_index (unit, real processes)
def _index(path, kind="mail", procs=2, in_child=True):
    from app.server import ordering
    from app.server.builders import BuilderPool
    pst = fake_engine.PstFile(path)
    specs = [ordering.FolderSpec(fi, f, "/".join(f.path)) for fi, f in enumerate(pst.folders())
             if {"mail": "mail", "event": "calendar", "contact": "contacts"}[kind] == f.kind]
    crashes, seen_progress = [], []
    pool = BuilderPool(path, procs, in_child=in_child, engine_module="tests.server.fake_engine")
    try:
        entries, st = ordering.build_index(
            pool, specs, kind, keep=lambda s, i, k: True, check=lambda: None,
            progress=lambda d, t: seen_progress.append((d, t)),
            on_fetch_crash=lambda s, i, rc: crashes.append((s.fkey, i, rc)), chunk=2)
    finally:
        pool.close()
    return entries, st, crashes, seen_progress


def test_index_sorts_across_folders_newest_first(env, tmp_path):
    path = str(tmp_path / "f.pst")
    fake_engine.make_fake_pst(path, 1000)
    fake_engine.CONFIG.update(folders=[INBOX, SUB, SENT, CAL], dates=dict(DATES))
    entries, st, crashes, prog = _index(path)
    assert [e[1] for e in entries] == expected_order([INBOX, SUB, SENT])
    # compact entries: (sort value, item_key, folder index, item index) — nothing else retained
    assert all(len(e) == 4 and isinstance(e[2], int) and isinstance(e[3], int) for e in entries)
    assert entries[-1][1] == fake_engine.item_key(INBOX[0], 2) and entries[-1][0] == float("inf")
    # same date (T0 + 400): tie broken by item_key
    tie = [e[1] for e in entries if e[0] == -(T0 + 400)]
    assert tie == sorted([fake_engine.item_key(INBOX[0], 3), fake_engine.item_key(SUB[0], 1)])
    assert st.seen == st.kept == 12 and st.undated == 1 and st.date_crashes == 0 and not crashes
    assert prog[0] == (0, 12) and prog[-1] == (12, 12)


def test_index_date_crash_means_no_date_fetch_crash_reported(env, tmp_path, monkeypatch):
    """A builder dying while reading a date: the item is kept without date (last); one dying while
    fetching the item is reported (index skipped by the caller). Other items of the chunk are unaffected."""
    path = str(tmp_path / "f.pst")
    fake_engine.make_fake_pst(path, 1000)
    fake_engine.CONFIG.update(folders=[INBOX], dates={k: v for k, v in DATES.items() if k.startswith("Boîte")})
    bad_date = fake_engine.item_key(INBOX[0], 1)  # newest of the folder
    monkeypatch.setenv("PSTMIG_TEST_CRASH", f"date:{bad_date},fetch:3")
    entries, st, crashes, _ = _index(path)
    keys = [e[1] for e in entries]
    assert crashes == [("Boîte de réception", 3, -11)]
    assert fake_engine.item_key(INBOX[0], 3) not in keys and len(keys) == 4
    assert st.date_crashes == 1 and st.undated == 2
    # no date -> last (with the item that has no date at all), ties by key
    assert set(keys[-2:]) == {bad_date, fake_engine.item_key(INBOX[0], 2)}
    assert keys[:2] == [fake_engine.item_key(INBOX[0], 0), fake_engine.item_key(INBOX[0], 4)]


def test_index_dav_kinds(env, tmp_path):
    path = str(tmp_path / "f.pst")
    fake_engine.make_fake_pst(path, 1000)
    fake_engine.CONFIG.update(folders=[INBOX, CAL, CONTACTS], dates=dict(DATES))
    ev, _, _, _ = _index(path, "event", procs=1)
    ct, _, _, _ = _index(path, "contact", procs=1)
    assert [e[1] for e in ev] == expected_order([CAL])
    assert [e[1] for e in ct] == expected_order([CONTACTS])


# ------------------------------------------------------------------ end to end (worker + GreenMail + Radicale)
def _mail_keys_by_id(aid, kind="mail"):
    from app.server import db
    return [r["item_key"] for r in db.q("SELECT item_key FROM items WHERE account_id=? AND kind=? ORDER BY id",
                                        (aid, kind))]


def _folder_uid_order(email):
    """{folder: [import ids in UID order]}"""
    import email as em
    c = imap_conn(email)
    out = {}
    try:
        for b in c.list():
            if "\\Noselect" in b.flags or not c.select(b.name):
                continue
            typ, data = c.M.uid("FETCH", "1:*", "(UID BODY.PEEK[HEADER.FIELDS (X-PST-Import-Id)])")
            got = []
            for d in data:
                if isinstance(d, tuple):
                    uid = int(d[0].split(b"UID ")[1].split()[0])
                    got.append((uid, em.message_from_bytes(d[1]).get("X-PST-Import-Id")))
            if got:
                out[b.name] = [v for _, v in sorted(got)]
    finally:
        c.close()
    return out


@needs_imap
@pytest.mark.parametrize("procs", ["2", "0"])
def test_append_order_newest_first_across_folders(client, env, monkeypatch, procs):
    monkeypatch.setenv("BUILD_PROCESSES", procs)
    fake_engine.CONFIG.update(folders=[INBOX, SUB, SENT], dates=dict(DATES))
    aid, email = setup_account(client, env, workers=1)  # one IMAP connection: APPEND order = index order
    jid = run(client, aid)
    with WorkerThread():
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"), cond=lambda j: j["finished_at"])
    assert j["status"] == "done", _log(client, jid)
    order = expected_order([INBOX, SUB, SENT])
    assert _mail_keys_by_id(aid) == order  # submission order (inflight recorded just before APPEND)
    from tests.server.test_idempotence import pst_sha_of
    sha = pst_sha_of(aid)
    per_folder = _folder_uid_order(email)
    assert len(per_folder) == 3
    for ids in per_folder.values():
        keys = [v[len(sha) + 2:-1] for v in ids]
        assert keys == [k for k in order if k in set(keys)]  # UIDs ascend with the newest-first order
    log = _log(client, jid)
    assert "Indexed 12 item(s) in " in log and "12 to import, newest first (1 without date" in log
    assert "Indexing mail items (newest first)…" in log


@needs_imap
def test_stop_then_resume_keeps_order_without_duplicates(client, env):
    big_inbox = (INBOX[0], "mail", "IPF.Note", "inbox", 12)
    big_sub = (SUB[0], "mail", "IPF.Note", None, 10)
    dates = {}
    for i in range(12):
        dates[_ref(big_inbox, i)] = T0 + 2 * i * 60
    for i in range(10):
        dates[_ref(big_sub, i)] = T0 + (2 * i + 1) * 60 if i != 7 else None
    fake_engine.CONFIG.update(folders=[big_inbox, big_sub], dates=dates, delay=0.15)
    aid, email = setup_account(client, env, workers=1)
    jid = run(client, aid)
    order = expected_order([big_inbox, big_sub])
    with WorkerThread():
        wait_job(client, jid, ("running",), cond=lambda j: j["progress"]["mail"]["done"] >= 4)
        assert client.post(f"/api/jobs/{jid}/stop").status_code == 200
        j = wait_job(client, jid, ("stopped",), timeout=60)
        done1 = j["progress"]["mail"]["done"]
        assert 4 <= done1 < 22
        from app.server import db
        ok1 = {r["item_key"] for r in db.q("SELECT item_key FROM items WHERE account_id=? AND status='ok'", (aid,))}
        assert ok1 == set(order[:len(ok1)])  # what was imported is the newest part
        fake_engine.CONFIG["delay"] = 0.0
        assert client.post(f"/api/jobs/{jid}/retry").status_code == 200
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"), cond=lambda j: j["finished_at"])
    assert j["status"] == "done", _log(client, jid)
    assert j["progress"]["mail"] == {"done": 22, "total": 22, "errors": 0}
    ids = server_import_ids(email)
    assert len(ids) == 22 and set(ids.values()) == {1}
    # first run = newest prefix, resume = the rest, still newest first
    assert _mail_keys_by_id(aid) == order
    assert f"{22 - len(ok1)} to import, newest first" in _log(client, jid)


@needs_imap
def test_dav_order_calendar_start_and_contacts_mtime(client, env):
    fake_engine.CONFIG.update(folders=[CAL, CONTACTS], dates=dict(DATES))
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    with WorkerThread():
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"), cond=lambda j: j["finished_at"])
    assert j["status"] == "done", _log(client, jid)
    assert _mail_keys_by_id(aid, "event") == expected_order([CAL])
    assert _mail_keys_by_id(aid, "contact") == expected_order([CONTACTS])
    from app.server import db
    st = {r["status"] for r in db.q("SELECT status FROM items WHERE account_id=?", (aid,))}
    assert st == {"ok"}
    log = _log(client, jid)
    assert "Indexed 4 item(s) in " in log and "Indexed 3 item(s) in " in log
