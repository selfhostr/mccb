"""Planning tools against GreenMail (docker-compose.test.yml): localised special folders, dry run, final resync."""
import pytest

from app.server.imap import utf7_encode
from tests.server.test_connectors_imap import message, messages_of, setup_route
from tests.server.test_idempotence import run_inproc
from tests.server.test_runner import (DAV_URL, IMAP_HOST, _log, imap_conn, run, server_import_ids,  # noqa: F401
                                      wait_services)  # wait_services: autouse fixture of this module

pytestmark = pytest.mark.skipif(not IMAP_HOST or not DAV_URL, reason="TEST_IMAP_HOST / TEST_DAV_URL not set")

LOCALISED = {"Gesendete Elemente": "sent", "Borradores": "drafts", "Корзина": "trash", "Posta indesiderata": "junk"}


def seed_localised(email_addr: str) -> int:
    c = imap_conn(email_addr)
    try:
        n = 0
        for i in range(3):
            c.append("INBOX", message(i, "INBOX"), [], None)
            n += 1
        for k, name in enumerate(LOCALISED):
            c.create(utf7_encode(name))
            for i in range(2):
                c.append(utf7_encode(name), message(i, f"loc{k}"), [], None)
                n += 1
        return n
    finally:
        c.close()


def test_localised_folders_land_in_the_special_folders_of_the_target(client, env):
    aid, src, dst = setup_route(client)
    n = seed_localised(src)
    jid = run(client, aid)
    j = run_inproc(client, jid)
    assert j["status"] == "done", _log(client, jid)
    got = messages_of(dst)
    assert len(got["INBOX"]) == 3
    # whatever the target calls them, there is one folder per role and no folder with the source's name
    assert not any(name in got for name in LOCALISED), sorted(got)
    assert {"Sent", "Drafts", "Trash", "Junk"} <= set(got), sorted(got)
    assert all(len(got[f]) == 2 for f in ("Sent", "Drafts", "Trash", "Junk"))
    ids = server_import_ids(dst)
    assert len(ids) == n and set(ids.values()) == {1}


def test_a_rule_beats_the_built_in_names(client, env):
    aid, src, dst = setup_route(client)
    seed_localised(src)
    r = client.put(f"/api/accounts/{aid}/policy", json={"folder_map": [{"source": "Gesendete Elemente", "target": "Archive/Sent"}],
                                                        "exclude_folders": ["Корзина"]})
    assert r.status_code == 200, r.text
    jid = run(client, aid)
    j = run_inproc(client, jid)
    assert j["status"] == "done", _log(client, jid)
    got = messages_of(dst)
    assert len(got["INBOX"]) == 3 and "Sent" not in got and "Trash" not in got
    assert sum(len(v) for k, v in got.items() if k.startswith("Archive")) == 2
    assert client.get(f"/api/jobs/{jid}/report").json()["filtered"]["folders"] == {"Корзина": 2}


def test_dry_run_counts_and_writes_nothing(client, env):
    from app.server import planning
    aid, src, dst = setup_route(client)
    n = seed_localised(src)
    pid = client.post("/api/runs/plan", json={"account_ids": [aid]}).json()["id"]
    planning.run_plan(pid)
    p = client.get(f"/api/plans/{pid}").json()
    a = p["result"]["accounts"][0]
    assert p["status"] == "done" and a["source"]["ok"] and a["targets"]["mail"]["ok"], a
    k = a["kinds"]["mail"]
    assert k["items"] == n and k["to_migrate"] == n and k["folders_to_create"] >= 4
    created = {f["target"] for f in a["folders"] if f["create"]}
    assert {"Sent", "Drafts", "Trash", "Junk"} <= created
    assert set(messages_of(dst)) <= {"INBOX"}  # nothing created, nothing written
    assert client.get("/api/jobs").json() == []


def test_final_resync_picks_up_what_arrived_since(client, env):
    aid, src, dst = setup_route(client)
    n = seed_localised(src)
    jid = client.post("/api/runs", json={"account_ids": [aid], "mode": "delta", "name": "cutover"}).json()[0]["id"]
    assert run_inproc(client, jid)["status"] == "done"
    c = imap_conn(src)
    try:
        for i in (10, 11):
            c.append("INBOX", message(i, "INBOX"), [], None)
    finally:
        c.close()
    assert client.post(f"/api/jobs/{jid}/delta").status_code == 200
    j = run_inproc(client, jid)
    assert j["status"] == "done", _log(client, jid)
    ids = server_import_ids(dst)
    assert len(ids) == n + 2 and set(ids.values()) == {1}
    assert "changed since the previous pass" in _log(client, jid)
    cid = client.post(f"/api/jobs/{jid}/compare").json()["id"]
    from app.server import planning
    planning.run_compare(cid)
    res = client.get(f"/api/jobs/{jid}/compare").json()["result"]
    assert res["ok"] is True and res["totals"]["missing"] == 0 and res["totals"]["on_target"] == n + 2
