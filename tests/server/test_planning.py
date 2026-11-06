"""Planning tools: folder mapping, policy and filters, allowed hours, bandwidth, oversized messages, delta
resync, dry run, comparison, batches, queue order, CSV, API tokens. Runs without any network service (in-memory
connectors); the IMAP side is in test_planning_imap.py."""
import datetime as dt
import io
import time
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from app.connectors import base, registry
from app.server import csvimport, foldermap, policy
from tests.server import connector_fakes as fakes
from tests.server.test_connectors import SRV, log_of, mem, memory_account, run_job, start  # noqa: F401


def F(*path, special=None, kind="mail", count=1):
    return NS(path=list(path), kind=kind, special=special, count=count)


# ================================================================== folder mapping
@pytest.mark.parametrize("name,role", [
    ("Sent Items", "sent"), ("Éléments envoyés", "sent"), ("Gesendete Elemente", "sent"),
    ("Elementos enviados", "sent"), ("Posta inviata", "sent"), ("Itens enviados", "sent"),
    ("Отправленные", "sent"), ("已发送邮件", "sent"),
    ("Deleted Items", "trash"), ("Éléments supprimés", "trash"), ("Papierkorb", "trash"),
    ("Elementos eliminados", "trash"), ("Cestino", "trash"), ("Lixeira", "trash"), ("Корзина", "trash"),
    ("已删除邮件", "trash"),
    ("Junk E-mail", "junk"), ("Courrier indésirable", "junk"), ("Correo no deseado", "junk"),
    ("Posta indesiderata", "junk"), ("Lixo eletrônico", "junk"), ("Нежелательная почта", "junk"),
    ("垃圾邮件", "junk"),
    ("Drafts", "drafts"), ("Brouillons", "drafts"), ("Entwürfe", "drafts"), ("Borradores", "drafts"),
    ("Bozze", "drafts"), ("Rascunhos", "drafts"), ("Черновики", "drafts"), ("草稿", "drafts"),
    ("Projects", None)])
def test_default_names_in_the_nine_languages(name, role):
    assert foldermap.default_role([name]) == role


def test_default_names_only_at_the_top_or_under_the_inbox():
    assert foldermap.default_role(["Projects", "Sent"]) is None
    assert foldermap.default_role(["INBOX", "Sent"]) == "sent"


def test_defaults_do_not_steal_a_role_the_source_flagged():
    out = foldermap.Mapper().resolve([F("Sent", special="sent"), F("Sent Items"), F("Gelöschte Elemente")])
    assert [m.special for m in out] == ["sent", None, "trash"]


def test_rules_rename_and_flag():
    m = foldermap.Mapper.of([{"source": "Old", "target": "Archive/Old"}, {"source": "Mes envois", "target": "@sent"}])
    out = m.resolve([F("Old"), F("Old", "2019"), F("Mes envois"), F("Other")])
    assert [x.path for x in out] == [["Archive", "Old"], ["Archive", "Old", "2019"], ["Mes envois"], ["Other"]]
    assert [x.special for x in out] == [None, None, "sent", None]
    assert out[1].orig.path == ["Old", "2019"]  # the key of the items is the one of the source folder


def test_plan_lands_special_folders_on_the_targets_own_names():
    from app.server.imap import Mailbox
    from app.server.runner import folder_key, plan_folders
    boxes = [Mailbox("INBOX", [], "."), Mailbox("Sent", ["\\Sent"], "."), Mailbox("Trash", ["\\Trash"], ".")]
    folders = [F("Boîte de réception", special="inbox"), F("Éléments envoyés"), F("Éléments supprimés"),
               F("Courrier indésirable"), F("Archive"), F("Archive", "2019")]
    mp = foldermap.Mapper.of([{"source": "Archive", "target": "Stock"}])
    plan = foldermap.plan_mapped(lambda fs: plan_folders(fs, boxes, ".", None, ""), folders, mp)
    assert plan[folder_key(["Boîte de réception"])] == ("INBOX", None)
    assert plan[folder_key(["Éléments envoyés"])] == ("Sent", None)
    assert plan[folder_key(["Éléments supprimés"])] == ("Trash", None)
    assert plan[folder_key(["Courrier indésirable"])] == ("Junk", "\\Junk")  # not on the target: created flagged
    assert plan[folder_key(["Archive", "2019"])] == ("Stock.2019", None)


def test_exclusion_patterns():
    assert foldermap.excluded(["Junk*"], ["Junk Email"])
    assert foldermap.excluded(["archive"], ["Archive", "2019"])  # a folder excludes its sub-folders
    assert not foldermap.excluded(["Archive"], ["Archives"])
    assert not foldermap.excluded([], ["Archive"])


def test_bad_rules_are_refused():
    for bad in ([{"source": "", "target": "@sent"}], [{"source": "a", "target": "@nope"}], [{"source": "a"}], "x"):
        with pytest.raises(ValueError):
            foldermap.check_rules(bad)


# ================================================================== policy
def test_policy_validation():
    assert policy.normalize({}, False) is None and policy.normalize(None, True) is None
    good = policy.normalize({"date_from": "2020-01-01", "max_size_mb": "25", "exclude_folders": [" Junk "]}, False)
    assert good == {"exclude_folders": ["Junk"], "date_from": "2020-01-01", "max_size_mb": 25.0}
    for bad, server in (({"date_from": "yesterday"}, False), ({"date_from": "2024-02-01", "date_to": "2024-01-01"}, False),
                        ({"max_size_mb": 0}, False), ({"window": {"start": "25:00", "end": "06:00"}}, True),
                        ({"window": {"start": "20:00", "end": "06:00", "tz": "Mars/Base"}}, True),
                        ({"window": {"start": "20:00", "end": "06:00"}}, False),  # server only
                        ({"rate_mb_s": -1}, True), ({"nonsense": 1}, False)):
        with pytest.raises(ValueError):
            policy.normalize(bad, server)


def test_effective_policy_merges_account_over_server(env):
    from app.server import db
    now = time.time()
    db.ex("INSERT INTO servers(name, imap_host, created_at, policy) VALUES ('s','h',?,?)",
          (now, db.jdump({"max_size_mb": 10, "exclude_folders": ["Junk"], "rate_mb_s": 2,
                          "window": {"start": "20:00", "end": "06:00", "tz": "UTC"},
                          "folder_map": [{"source": "A", "target": "B"}]})))
    db.ex("INSERT INTO accounts(server_id, email, created_at, policy) VALUES (1,'a@b',?,?)",
          (now, db.jdump({"max_size_mb": 5, "exclude_folders": ["Spam"], "date_to": "2023-12-31",
                          "folder_map": [{"source": "C", "target": "D"}]})))
    p = policy.effective(db.q1("SELECT * FROM accounts"), db.q1("SELECT * FROM servers"))
    assert p.max_size == 5 * 2 ** 20 and p.exclude == ["Spam", "Junk"]
    assert [r["source"] for r in p.rules] == ["C", "A"]  # the account's rules first
    assert p.rate == 2 * 2 ** 20 and p.window["start"] == "20:00"
    assert p.reason(dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc), 10) == "date"
    assert p.reason(dt.datetime(2023, 12, 31, 23, 0, tzinfo=dt.timezone.utc), 10) is None  # the last day is inside
    assert p.reason(None, 6 * 2 ** 20) == "size"
    assert p.reason(None, None) is None  # unknown date and size: never judged
    none = policy.effective(None, None)
    assert not none.has_filters and none.window is None and none.rate is None and none.summary() == {}


def paris(y, m, d, h, mi):
    return dt.datetime(y, m, d, h, mi, tzinfo=ZoneInfo("Europe/Paris")).timestamp()


def test_window_over_midnight_belongs_to_the_day_it_starts():
    w = {"days": [0, 1, 2, 3, 4], "start": "20:00", "end": "06:00", "tz": "Europe/Paris"}
    assert policy.window_open(w, paris(2026, 11, 2, 21, 0))       # Monday evening
    assert policy.window_open(w, paris(2026, 11, 3, 5, 0))        # Tuesday 05:00 is Monday's night
    assert not policy.window_open(w, paris(2026, 11, 3, 12, 0))
    assert not policy.window_open(w, paris(2026, 11, 7, 22, 0))   # Saturday evening
    assert policy.window_open(w, paris(2026, 11, 7, 2, 0))        # Saturday 02:00 is Friday's night
    assert not policy.window_open(w, paris(2026, 11, 9, 3, 0))    # Monday 03:00 is Sunday's night
    assert policy.next_open(w, paris(2026, 11, 7, 22, 0)) == paris(2026, 11, 9, 20, 0)
    assert policy.window_open(None) and policy.window_open({"start": "00:00", "end": "00:00", "tz": "UTC"})


def test_limiter_paces_and_shares_between_jobs(env):
    from app.server import db
    lim = policy.Limiter(1, 1000.0)
    t0 = time.monotonic()
    for _ in range(6):
        lim.wait(500, lambda: False)  # 3000 bytes at 1000 B/s, one second of credit at the start
    assert 1.7 < time.monotonic() - t0 < 3.5
    assert lim.active() and lim.waited > 1
    stop = time.monotonic()
    lim.wait(10_000, lambda: True)  # a stop request ends the wait at once
    assert time.monotonic() - stop < 1
    now = time.time()
    db.ex("INSERT INTO servers(name, imap_host, created_at) VALUES ('s','h',?)", (now,))
    db.ex("INSERT INTO accounts(server_id, email, created_at) VALUES (1,'a@b',?), (1,'c@d',?)", (now, now))
    db.ex("INSERT INTO jobs(account_id, status, created_at) VALUES (1,'running',?), (2,'running',?)", (now, now))
    shared = policy.Limiter(1, 1000.0)
    shared.delay(0)
    assert shared.jobs == 2 and shared.share == 500.0


def test_oversize_detection():
    assert policy.is_oversize("NO [TOOBIG] Message too large")
    assert policy.is_oversize("APPEND failed: message size exceeds the limit")
    assert not policy.is_oversize("NO [OVERQUOTA] Quota exceeded")
    assert not policy.is_oversize("connection reset")
    assert policy.append_limit({"IMAP4REV1", "APPENDLIMIT=26214400"}) == 26214400
    assert policy.append_limit({"IMAP4REV1"}) is None
    assert policy.oversize_message(30 * 2 ** 20, 25 * 2 ** 20).startswith(policy.OVERSIZE_PREFIX)


# ================================================================== filters in a run
def set_policy(client, aid, doc):
    r = client.put(f"/api/accounts/{aid}/policy", json=doc)
    assert r.status_code == 200, r.text
    return r.json()


def test_policy_endpoints(client):
    sid = client.post("/api/servers", json=SRV).json()["id"]
    assert client.get(f"/api/servers/{sid}/policy").json() == {"policy": None}
    r = client.put(f"/api/servers/{sid}/policy", json={"rate_mb_s": 3, "window": {"start": "22:00", "end": "05:00"}})
    assert r.status_code == 200 and r.json()["policy"]["window"]["tz"] == "UTC"
    assert client.put(f"/api/servers/{sid}/policy", json={"window": {"start": "99:00"}}).status_code == 422
    aid = client.post("/api/accounts", json={"server_id": sid, "email": "a@example.test", "password": "pw"}).json()["id"]
    assert client.put(f"/api/accounts/{aid}/policy", json={"rate_mb_s": 1}).status_code == 422  # server only
    got = set_policy(client, aid, {"date_from": "2020-01-01", "max_size_mb": 20})
    assert got["policy"]["max_size_mb"] == 20.0 and got["effective"]["date_from"] == "2020-01-01"
    assert got["effective"]["rate_mb_s"] == 3.0  # inherited from the server
    assert set_policy(client, aid, None)["policy"] is None
    assert client.put("/api/accounts/999/policy", json={}).status_code == 404


def test_size_filter_is_not_an_error_and_widening_it_sends_the_rest_once(client, mem):
    aid, _box, store = memory_account(client)
    set_policy(client, aid, {"max_size_mb": 0.00005})  # 52 bytes: every memory mail has 157
    jid = start(client, aid)
    j = run_job(client, jid)
    assert j["status"] == "done", log_of(client, jid)
    assert j["progress"]["mail"] == {"done": 12, "total": 12, "errors": 0} and not store.items
    rep = client.get(f"/api/jobs/{jid}/report").json()
    assert rep["totals"]["mail"]["filtered"] == 12 and rep["totals"]["mail"]["errors"] == 0
    assert rep["filtered"]["by_reason"] == {"size": 12} and rep["policy"]["max_size_mb"] == 0.00005
    assert client.get(f"/api/jobs/{jid}/errors").json() == []
    listed = client.get(f"/api/jobs/{jid}/filtered").json()
    assert len(listed) == 12 and listed[0]["reason"] == "size"
    set_policy(client, aid, {"max_size_mb": 1})
    assert client.post(f"/api/jobs/{jid}/retry").status_code == 200
    j = run_job(client, jid)
    assert j["status"] == "done" and len(store.copies()) == 12 and set(store.copies().values()) == {1}
    assert client.get(f"/api/jobs/{jid}/report").json()["totals"]["mail"]["filtered"] == 0


def test_date_filter_and_excluded_folders(client, mem):
    aid, _box, store = memory_account(client, {"INBOX": 8, "Junk Email": 5, "Projects": 4})
    # memory mails are dated 2024-03-01 12:00 + i hours: 0..11 hours on the first day, 12.. on the second
    set_policy(client, aid, {"date_from": "2024-03-01", "date_to": "2024-03-01", "exclude_folders": ["Junk*"]})
    jid = start(client, aid)
    j = run_job(client, jid)
    assert j["status"] == "done", log_of(client, jid)
    assert len(store.items) == 12 and {i[0] for i in store.items} == {"INBOX", "Projects"}  # all on the first day
    rep = client.get(f"/api/jobs/{jid}/report").json()
    assert rep["filtered"]["folders"] == {"Junk Email": 5} and rep["totals"]["mail"]["source"] == 12
    set_policy(client, aid, {"date_from": "2024-03-01", "date_to": "2024-03-01", "exclude_folders": ["Junk*"],
                             "folder_map": [{"source": "Projects", "target": "Work/Projects"}]})
    # items already imported stay where they are: a new mapping creates no duplicate
    assert client.post(f"/api/jobs/{jid}/retry").status_code == 200
    run_job(client, jid)
    assert set(store.copies().values()) == {1} and {i[0] for i in store.items} == {"INBOX", "Projects"}


def test_date_filter_leaves_out_old_mail(client, mem):
    aid, box_, store = memory_account(client, {"INBOX": 14})
    set_policy(client, aid, {"date_from": "2024-03-02"})  # mails 12..13 are dated the 2nd
    jid = start(client, aid)
    j = run_job(client, jid)
    assert len(store.items) == 2 and j["progress"]["mail"] == {"done": 14, "total": 14, "errors": 0}
    assert client.get(f"/api/jobs/{jid}/report").json()["filtered"]["by_reason"] == {"date": 12}


# ================================================================== oversized messages
def test_refused_for_size_is_reported_once_and_not_retried(client, mem, monkeypatch):
    aid, _box, store = memory_account(client, {"INBOX": 6})
    real = fakes.MemorySession.put
    calls = []

    def put(self, folder, item):
        if b"INBOX 3" in item.raw:
            calls.append(item.import_id)
            raise base.ConnectorError("NO [TOOBIG] Message too large", base.ConnectorError.REJECTED)
        return real(self, folder, item)

    monkeypatch.setattr(fakes.MemorySession, "put", put)
    jid = start(client, aid)
    j = run_job(client, jid)
    assert j["status"] == "done_with_errors" and j["progress"]["mail"]["errors"] == 1 and len(store.items) == 5
    assert len(calls) == 1  # no retry storm
    rep = client.get(f"/api/jobs/{jid}/report").json()
    assert rep["oversized"]["count"] == 1 and rep["oversized"]["items"][0]["item_key"].endswith("-3")
    assert rep["oversized"]["items"][0]["error"].startswith(policy.OVERSIZE_PREFIX)
    assert [x["item_key"] for x in client.get(f"/api/jobs/{jid}/oversized").json()] == [rep["oversized"]["items"][0]["item_key"]]
    run_job_again = client.post(f"/api/jobs/{jid}/retry")
    assert run_job_again.status_code == 200
    run_job(client, jid)
    assert len(calls) == 1 and set(store.copies().values()) == {1}  # a full rerun does not send it again
    assert client.post(f"/api/jobs/{jid}/retry-errors").status_code == 200
    run_job(client, jid)
    assert len(calls) == 2  # only when the administrator asks for the errors again


def test_known_target_limit_stops_the_message_before_sending(client, mem, monkeypatch):
    aid, _box, store = memory_account(client, {"INBOX": 4})
    monkeypatch.setattr(fakes.MemorySession, "info", lambda self: {"append_limit": 100}, raising=False)
    jid = start(client, aid)
    j = run_job(client, jid)
    assert j["status"] == "done_with_errors" and not store.items
    rep = client.get(f"/api/jobs/{jid}/report").json()
    assert rep["oversized"]["count"] == 4 and "over the limit of the target" in rep["oversized"]["items"][0]["error"]


# ================================================================== delta resync
@pytest.fixture
def inc(mem):
    class IncSource(fakes.MemorySource):
        def changed_since(self, folder, cursor):
            n = self.box.folders[folder.ref]
            start_at = int(cursor) if cursor else 0
            return base.Changes([it for it in self.iter_items(folder) if it.ref >= start_at], str(n),
                                reset=cursor is None)

    field = registry.Field("name", "Name", required=True)
    token = registry.Field("token", "Token", "password", required=True, secret=True)
    registry.register(registry.ConnectorSpec(
        kind="inc", label="Incremental", source=registry.Role(IncSource, ("mail",), frozenset({base.CAP_INCREMENTAL}),
                                                              (field, token), lambda c: c["name"])))
    yield
    registry.unregister("inc")


def test_delta_lists_only_what_changed(client, inc):
    aid, box_, store = memory_account(client, {"INBOX": 5})
    assert client.put(f"/api/accounts/{aid}/source", json={"kind": "inc", "config": {"name": "s"},
                                                           "secrets": {"token": "ok"}}).status_code == 200
    r = client.post("/api/runs", json={"account_ids": [aid], "mode": "delta", "name": "cutover"})
    assert r.status_code == 200 and r.json()[0]["batch_id"]
    jid = r.json()[0]["id"]
    assert run_job(client, jid)["status"] == "done" and len(store.items) == 5  # no cursor yet: full listing
    box_.folders["INBOX"] = 8  # three messages arrived
    assert client.post(f"/api/jobs/{jid}/delta").status_code == 200
    j = run_job(client, jid)
    assert j["status"] == "done", log_of(client, jid)
    assert len(store.items) == 8 and set(store.copies().values()) == {1}
    assert "3 item(s) changed since the previous pass" in log_of(client, jid)
    box_.fetches.clear()
    assert client.post(f"/api/jobs/{jid}/delta").status_code == 200
    run_job(client, jid)
    assert box_.fetches == [] and len(store.items) == 8  # nothing new, nothing read


def test_delta_on_a_source_without_changes_feed_is_a_full_listing(client, mem):
    aid, box_, store = memory_account(client, {"INBOX": 5})
    jid = start(client, aid)
    run_job(client, jid)
    box_.folders["INBOX"] = 7
    assert client.post(f"/api/jobs/{jid}/delta").status_code == 200
    j = run_job(client, jid)
    assert j["status"] == "done" and len(store.items) == 7 and set(store.copies().values()) == {1}
    assert "full listing filtered by the items already migrated" in log_of(client, jid)


def test_changing_the_target_forgets_the_cursors(client, inc):
    from app.server import db
    aid, _box, _store = memory_account(client, {"INBOX": 2})
    db.kv_set(f"delta:{aid}:src-x:INBOX", "2")
    fakes.TARGETS["t2"] = fakes.MemoryStore()
    assert client.put(f"/api/accounts/{aid}/target", json={"kind": "memory", "config": {"name": "t2"}}).status_code == 200
    assert db.kv_get(f"delta:{aid}:src-x:INBOX") is None


# ================================================================== dry run
def test_dry_run_writes_nothing_and_counts(client, mem):
    from app.server import planning
    aid, _box, store = memory_account(client, {"INBOX": 8, "Junk Email": 3, "Projects": 4})
    set_policy(client, aid, {"exclude_folders": ["Junk*"]})
    r = client.post("/api/runs/plan", json={"account_ids": [aid]})
    assert r.status_code == 202 and r.json()["status"] == "queued"
    pid = r.json()["id"]
    planning.run_plan(pid)
    p = client.get(f"/api/plans/{pid}").json()
    assert p["status"] == "done" and p["result"]["done"] == 1
    a = p["result"]["accounts"][0]
    assert a["status"] == "ok" and a["source"]["ok"] and a["targets"]["mail"]["ok"]
    k = a["kinds"]["mail"]
    assert (k["items"], k["to_migrate"], k["excluded_folders"], k["excluded_items"]) == (15, 12, 1, 3)
    assert k["bytes"] == 1200 and a["excluded_folders"] == ["Junk Email"]
    assert {tuple(f["path"]): f["excluded"] for f in a["folders"]}[("Junk Email",)] is True
    assert p["result"]["totals"]["to_migrate"] == 12
    assert not store.items and client.get("/api/jobs").json() == []  # nothing written, no job
    assert client.post("/api/runs/plan", json={"account_ids": [999]}).status_code == 422
    assert client.get("/api/plans/999").status_code == 404


def test_dry_run_reports_a_refused_connection_and_oversized_mail(client, mem, monkeypatch):
    from app.server import planning
    aid, _box, _store = memory_account(client, {"INBOX": 4})
    monkeypatch.setattr(fakes.MemorySession, "info", lambda self: {"append_limit": 50}, raising=False)
    monkeypatch.setattr(fakes.MemorySession, "quota", lambda self: {"used": 0, "limit": 300}, raising=False)
    a = planning.plan_account(aid)
    assert a["status"] == "warning" and a["target_limit"] == 50
    assert a["kinds"]["mail"]["oversized"] == 4 and a["quota"]["enough"] is False
    assert any("quota" in w for w in a["warnings"]) and any("over the limit" in w for w in a["warnings"])
    monkeypatch.setattr(fakes.MemorySource, "open", lambda self: (_ for _ in ()).throw(
        base.ConnectorError("bad token", base.ConnectorError.AUTH)))
    b = planning.plan_account(aid)
    assert b["status"] == "error" and b["source"]["ok"] is False and b["source"]["error_kind"] == "auth"


# ================================================================== comparison
def test_compare_finds_what_is_missing(client, mem):
    from app.server import planning
    aid, _box, store = memory_account(client, {"INBOX": 6})
    jid = start(client, aid)
    run_job(client, jid)
    assert client.get(f"/api/jobs/{jid}/compare").json()["status"] == "none"
    r = client.post(f"/api/jobs/{jid}/compare", json={"detail": True})
    assert r.status_code == 202
    planning.run_compare(r.json()["id"])
    c = client.get(f"/api/jobs/{jid}/compare").json()
    assert c["status"] == "done" and c["result"]["ok"] is True
    assert c["result"]["totals"]["expected"] == 6 and c["result"]["totals"]["on_target"] == 6
    gone = store.items.pop(0)  # someone deleted a message on the target
    cid = client.get(f"/api/jobs/{jid}/compare?run=1&detail=1").json()["id"]
    planning.run_compare(cid)
    res = client.get(f"/api/jobs/{jid}/compare").json()["result"]
    assert res["ok"] is False and res["totals"]["missing"] == 1
    assert res["folders"][0]["target"] == "INBOX" and res["folders"][0]["missing_items"][0]["subject"]
    assert gone[1] not in store.copies()
    text = client.get(f"/api/jobs/{jid}/compare/csv").content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("kind;source_folders;target_folder;expected;on_target;missing")
    assert "mail;INBOX;INBOX;6;5;1" in text
    rep = client.get(f"/api/jobs/{jid}/report").json()
    assert rep["compare"]["totals"]["missing"] == 1 and "generated_at" in rep["compare"]
    assert client.get("/api/jobs/999/compare").status_code == 404


# ================================================================== batches, queue order, allowed hours
def test_batches_group_and_act(client, mem):
    from app.server import db
    sid = client.post("/api/servers", json=SRV).json()["id"]
    ids = []
    for i in range(3):
        fakes.SOURCES[f"s{i}"] = fakes.MemoryMailbox({"INBOX": 2})
        fakes.TARGETS[f"t{i}"] = fakes.MemoryStore()
        aid = client.post("/api/accounts", json={"server_id": sid, "email": f"u{i}@example.test",
                                                 "password": "pw"}).json()["id"]
        client.put(f"/api/accounts/{aid}/source", json={"kind": "memory", "config": {"name": f"s{i}"},
                                                        "secrets": {"token": "ok"}})
        client.put(f"/api/accounts/{aid}/target", json={"kind": "memory", "config": {"name": f"t{i}"}})
        ids.append(aid)
    r = client.post("/api/runs", json={"account_ids": ids, "name": "wave 1"})
    jobs = r.json()
    bid = jobs[0]["batch_id"]
    assert bid and {j["batch_id"] for j in jobs} == {bid}
    b = client.get(f"/api/batches/{bid}").json()
    assert b["name"] == "wave 1" and b["jobs"] == 3 and b["state"] == "queued" and len(b["job_list"]) == 3
    assert client.post(f"/api/batches/{bid}/pause").json()["by_status"] == {"paused": 3}
    assert client.post(f"/api/batches/{bid}/resume").json()["by_status"] == {"queued": 3}
    run_job(client, jobs[0]["id"])
    assert client.post(f"/api/batches/{bid}/stop").json()["by_status"] == {"done": 1, "stopped": 2}
    db.ex("UPDATE jobs SET status='failed', error='x' WHERE id=?", (jobs[1]["id"],))
    out = client.post(f"/api/batches/{bid}/retry-failed").json()
    assert out["changed"] == 1 and out["by_status"] == {"done": 1, "queued": 1, "stopped": 1}
    listing = client.get("/api/batches").json()
    assert listing[0]["id"] == bid and listing[0]["items"]["mail"]["total"] >= 2
    assert client.post(f"/api/batches/{bid}/explode").status_code == 404
    assert client.get("/api/batches/999").status_code == 404
    assert client.post("/api/runs", json={"account_ids": [ids[2]]}).json()[0]["batch_id"] is None  # no name, no batch


def test_named_batch_is_announced_once(env):
    from app.server import db, notify
    now = time.time()
    db.ex("INSERT INTO servers(name, imap_host, created_at) VALUES ('s','h',?)", (now,))
    db.ex("INSERT INTO batches(name, created_at) VALUES ('wave 1', ?)", (now,))
    for i, st in enumerate(("done", "failed")):
        db.ex("INSERT INTO accounts(server_id, email, created_at) VALUES (1, ?, ?)", (f"u{i}@x", now))
        db.ex("INSERT INTO jobs(account_id, status, batch_id, error, created_at, finished_at) VALUES (?,?,1,?,?,?)",
              (i + 1, st, "boom" if st == "failed" else None, now, now))

    class Sink:
        def __init__(self):
            self.sent = []

        def submit(self, m):
            self.sent.append(m)
            return True

    n = notify.Notifier(dispatcher=Sink())
    cfg = {"events": {"batch_finished": True}}
    n._poll_batches(cfg, now - 1)
    n._poll_batches(cfg, now - 1)
    assert len(n.dispatcher.sent) == 1
    m = n.dispatcher.sent[0]
    assert m.kind == "batch_finished" and "wave 1" in m.title and "u1@x" in m.text and m.priority == 4
    db.ex("UPDATE jobs SET status='running' WHERE id=1")  # a batch with a job still active is not finished
    db.ex("INSERT INTO batches(name, created_at) VALUES ('wave 2', ?)", (now,))
    db.ex("INSERT INTO jobs(account_id, status, batch_id, created_at) VALUES (1,'running',2,?)", (now,))
    n._poll_batches(cfg, now - 1)
    assert len(n.dispatcher.sent) == 1


def test_queue_order_is_honoured_by_the_worker(env):
    from app.server import db, planning, worker
    now = time.time()
    db.ex("INSERT INTO servers(name, imap_host, created_at) VALUES ('s','h',?)", (now,))
    for i in range(4):
        db.ex("INSERT INTO accounts(server_id, email, created_at) VALUES (1, ?, ?)", (f"u{i}@x", now))
        db.ex("INSERT INTO jobs(account_id, status, created_at) VALUES (?, 'queued', ?)", (i + 1, now))
    assert planning.reorder([4, 3, 99]) == [4, 3, 1, 2]
    w = worker.Worker(concurrency=1)
    claimed = []
    for _ in range(4):
        claimed.append(w.claim())
        db.ex("UPDATE jobs SET status='done' WHERE id=?", (claimed[-1],))
    assert claimed == [4, 3, 1, 2]
    planning.set_priority(1, 50)
    assert db.q1("SELECT priority FROM jobs WHERE id=1")["priority"] == 50


def test_allowed_hours_pause_and_resume_by_themselves(client, mem):
    from app.server import db, planning
    aid, _box, store = memory_account(client, {"INBOX": 4})
    sid = db.q1("SELECT server_id FROM accounts WHERE id=?", (aid,))["server_id"]
    closed = {"window": {"start": "00:00", "end": "00:01", "tz": "UTC"}}
    opened = {"window": {"start": "00:00", "end": "00:00", "tz": "UTC"}}  # start == end: all day
    if policy.window_open(closed["window"]):
        pytest.skip("run during the one minute of the test window")
    assert client.put(f"/api/servers/{sid}/policy", json=closed).status_code == 200
    jid = start(client, aid)
    planning.window_tick()  # still queued: the worker does not start it
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "paused" and j["pause_reason"] == "window"
    assert "allowed hours" in log_of(client, jid)
    # a job that is already running pauses itself at its next check
    db.ex("UPDATE jobs SET status='queued', pause_reason=NULL WHERE id=?", (jid,))
    j = run_job(client, jid)
    assert j["status"] == "paused" and j["pause_reason"] == "window" and not store.items
    assert "Paused: outside the allowed hours" in log_of(client, jid)
    assert client.put(f"/api/servers/{sid}/policy", json=opened).status_code == 200
    planning.window_tick()
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "queued" and j["pause_reason"] is None
    j = run_job(client, jid)
    assert j["status"] == "done" and len(store.items) == 4
    assert client.post(f"/api/jobs/{jid}/retry").status_code == 200
    assert client.post(f"/api/jobs/{jid}/pause").json()["pause_reason"] is None
    planning.window_tick()
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "paused"


def test_bandwidth_limit_is_applied_without_failing_the_job(client, mem):
    from app.server import db
    aid, _box, store = memory_account(client, {"INBOX": 6})
    sid = db.q1("SELECT server_id FROM accounts WHERE id=?", (aid,))["server_id"]
    # 6 mails of ~157 bytes at 0.0005 MB/s (524 B/s): one second of credit, then the rest paced
    assert client.put(f"/api/servers/{sid}/policy", json={"rate_mb_s": 0.0005}).status_code == 200
    t0 = time.monotonic()
    jid = start(client, aid)
    j = run_job(client, jid)
    took = time.monotonic() - t0
    assert j["status"] == "done" and len(store.items) == 6
    assert took >= 0.6  # about 940 bytes in total: more than the credit of the first second


# ================================================================== CSV
def test_old_csv_files_parse_exactly_as_before():
    rows, errs = csvimport.parse(b"a@b.fr;pw1\nc@d.fr;pw2;login2\n")
    assert rows == [{"line": 1, "email": "a@b.fr", "password": "pw1", "login": ""},
                    {"line": 2, "email": "c@d.fr", "password": "pw2", "login": "login2"}] and errs == []


def test_extended_columns_parse():
    data = ("email;password;pst;mail;calendar;contacts;source_kind;source_host;source_password\n"
            "a@b.fr;pw;box.pst;1;non;;imap;src.example;secret\n"
            "c@d.fr;pw;;;;;;;\n"
            "e@f.fr;pw;;maybe;;;;;\n").encode()
    rows, errs, ext = csvimport.parse_ext(data)
    assert ext is True
    assert rows[0]["pst"] == "box.pst" and rows[0]["options"] == {"mail": True, "calendar": False}
    assert rows[0]["source"] == {"kind": "imap", "config": {"host": "src.example", "password": "secret"}}
    assert "pst" not in rows[1] and "options" not in rows[1] and "source" not in rows[1]
    assert errs == [{"line": 4, "msg": "mail: expected 1/0, yes/no or true/false, got 'maybe'"}]


def test_import_links_pst_and_source_and_export_never_has_a_password(client):
    from app.server import db
    sid = client.post("/api/servers", json={"name": "S", "imap_host": "h", "imap_port": 143,
                                            "imap_security": "none"}).json()["id"]
    now = time.time()
    db.ex("INSERT INTO psts(filename, size, chunk_size, status, path, pst_sha, created_at, updated_at) "
          "VALUES ('Jane.pst', 1, 1, 'valid', '/x', 'cafe', ?, ?)", (now, now))
    data = ("email;password;pst;mail;calendar;source_kind;source_host;source_login;source_password\n"
            "jane@example.test;Sup3rSecret;jane.PST;1;0;imap;src.example;jane;SourceSecret\n"
            "bob@example.test;pw;nope.pst;;;;;;\n").encode()
    r = client.post("/api/accounts/import-csv", data={"server_id": str(sid)},
                    files={"file": ("a.csv", io.BytesIO(data), "text/csv")})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["created"] == 2 and out["errors"] == [] and "nope.pst" in out["warnings"][0]["msg"]
    jane = [a for a in client.get("/api/accounts").json() if a["email"] == "jane@example.test"][0]
    assert jane["pst_id"] == 1 and jane["options"] == {"mail": True, "calendar": False, "contacts": True}
    assert jane["source"]["kind"] == "imap" and jane["source"]["has_secret"] is True
    assert jane["source"]["config"]["host"] == "src.example"
    exported = client.get(f"/api/accounts/export-csv?server_id={sid}").content.decode("utf-8-sig")
    assert "Sup3rSecret" not in exported and "SourceSecret" not in exported
    assert exported.splitlines()[0].startswith("email;login;server;pst;mail;calendar;contacts;source_kind;source_")
    assert "jane@example.test;;S;Jane.pst;1;0;1;imap;src.example" in exported
    # the export is a valid import: no password asked for existing accounts, secrets kept
    r = client.post("/api/accounts/import-csv", data={"server_id": str(sid)},
                    files={"file": ("e.csv", io.BytesIO(exported.encode("utf-8-sig")), "text/csv")})
    assert r.json()["errors"] == [] and r.json()["updated"] == 2
    again = [a for a in client.get("/api/accounts").json() if a["email"] == "jane@example.test"][0]
    assert again["source"]["has_secret"] is True and again["has_password"] is True


# ================================================================== API tokens
def test_tokens_work_next_to_the_session_and_are_stored_hashed(client):
    from fastapi.testclient import TestClient
    from app.server import db
    from app.server.web import app
    r = client.post("/api/tokens", json={"name": "nas"})
    assert r.status_code == 201
    tok = r.json()
    assert tok["token"].startswith("mccb_") and tok["token"].startswith(tok["prefix"]) and len(tok["prefix"]) == 13
    assert "token" not in client.get("/api/tokens").json()[0]  # shown once
    row = db.q1("SELECT * FROM api_tokens")
    assert tok["token"] not in row["token_hash"] and tok["token"] not in str(dict(row)) and len(row["token_hash"]) == 64
    with TestClient(app) as anon:  # no cookie
        assert anon.get("/api/jobs").status_code == 401
        h = {"Authorization": f"Bearer {tok['token']}"}
        assert anon.get("/api/jobs", headers=h).status_code == 200
        assert anon.get("/api/jobs", headers={"Authorization": "Bearer mccb_00000000_nope"}).status_code == 401
        assert anon.get("/api/jobs", headers={"Authorization": f"Bearer {tok['token']}x"}).status_code == 401
        assert anon.post("/api/tokens", json={"name": "x"}, headers=h).status_code == 403  # no token minting tokens
        assert anon.get("/api/tokens", headers=h).status_code == 403
        assert client.get("/api/tokens").json()[0]["last_used_at"] is not None
        assert client.delete(f"/api/tokens/{tok['id']}").json()["revoked_at"] is not None
        assert anon.get("/api/jobs", headers=h).status_code == 401
    assert client.delete("/api/tokens/999").status_code == 404


# ================================================================== schema
def test_an_old_database_gets_the_planning_columns(env):
    import sqlite3
    from app.server import db
    env.db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(env.db_path)
    c.executescript((Path(__file__).resolve().parents[1] / "fixtures" / "schema-1.0.0.sql").read_text())
    c.execute("INSERT INTO servers(id,name,imap_host,created_at) VALUES (1,'s','h',1)")
    c.execute("INSERT INTO accounts(id,server_id,email,login,password_enc,created_at) VALUES (1,1,'a@x','','tok',1)")
    c.execute("INSERT INTO jobs(id,account_id,created_at) VALUES (1,1,1)")
    c.commit()
    c.close()
    db.reset()
    assert db.q1("SELECT priority, batch_id, pause_reason FROM jobs WHERE id=1")["priority"] == 0
    assert db.q1("SELECT policy FROM servers WHERE id=1")["policy"] is None
    assert db.q1("SELECT policy FROM accounts WHERE id=1")["policy"] is None
    assert db.q1("SELECT COUNT(*) n FROM api_tokens")["n"] == 0


def test_planning_columns_exist_in_a_fresh_database(env):
    from app.server import db
    assert {"batch_id", "priority", "pause_reason"} <= {r[1] for r in db.q("PRAGMA table_info(jobs)")}
    assert "policy" in {r[1] for r in db.q("PRAGMA table_info(servers)")}
    assert "policy" in {r[1] for r in db.q("PRAGMA table_info(accounts)")}
    for t in ("batches", "plans", "compares", "api_tokens"):
        assert db.q1("SELECT name FROM sqlite_master WHERE name=?", (t,))
