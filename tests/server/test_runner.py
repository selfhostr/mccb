"""Intégration : runner + faux engine contre le vrai IMAP (GreenMail/Dovecot) et Radicale de docker-compose.test.yml."""
import hashlib
import os
import threading
import time
import uuid

import pytest

from tests.server import fake_engine
from tests.server.fake_engine import make_fake_pst
from tests.svcusers import create_user

IMAP_HOST = os.environ.get("TEST_IMAP_HOST")
IMAP_PORT = int(os.environ.get("TEST_IMAP_PORT", "3143"))
DAV_URL = os.environ.get("TEST_DAV_URL")

pytestmark = pytest.mark.skipif(not IMAP_HOST or not DAV_URL, reason="TEST_IMAP_HOST / TEST_DAV_URL non définis")

DEFAULT_FOLDERS = list(fake_engine.CONFIG["folders"])
N_MAIL = sum(n for p, k, c, s, n in DEFAULT_FOLDERS if k == "mail")
N_EVENT = sum(n for p, k, c, s, n in DEFAULT_FOLDERS if k == "calendar")
N_CONTACT = sum(n for p, k, c, s, n in DEFAULT_FOLDERS if k == "contacts")


@pytest.fixture(scope="module", autouse=True)
def wait_services():
    """Attend que GreenMail et Radicale acceptent les connexions (démarrage à froid)."""
    import socket
    from urllib.parse import urlparse
    u = urlparse(DAV_URL)
    for host, port in ((IMAP_HOST, IMAP_PORT), (u.hostname, u.port or 80)):
        end = time.time() + 60
        while True:
            try:
                socket.create_connection((host, port), timeout=2).close()
                break
            except OSError:
                if time.time() > end:
                    raise
                time.sleep(0.5)
    time.sleep(1)


@pytest.fixture(autouse=True)
def reset_fake():
    fake_engine.CONFIG.update(folders=list(DEFAULT_FOLDERS), delay=0.0, fail_keys=set(), reject_keys=set(), unreadable=set())
    yield
    fake_engine.CONFIG.update(folders=list(DEFAULT_FOLDERS), delay=0.0, fail_keys=set(), reject_keys=set(), unreadable=set())


class WorkerThread:
    def __init__(self):
        from app.server.worker import Worker
        self.w = Worker(concurrency=2)
        self.t = threading.Thread(target=self.w.run_forever, kwargs={"poll": 0.2}, daemon=True)

    def __enter__(self):
        self.t.start()
        return self.w

    def __exit__(self, *a):
        self.w.stop_event.set()
        self.t.join(30)
        for t in list(self.w.threads.values()):
            t.join(30)


def wait_job(client, jid, statuses, timeout=90, cond=None):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{jid}").json()
        if j["status"] in statuses and (cond is None or cond(j)):
            return j
        time.sleep(0.2)
    log = client.get(f"/api/jobs/{jid}/log").json()["lines"]
    raise AssertionError(f"job {jid} bloqué : {j}\n" + "\n".join(l["msg"] for l in log[-30:]))


def setup_account(client, env, root_folder=None, workers=3):
    email = create_user()
    srv = client.post("/api/servers", json={
        "name": "test", "imap_host": IMAP_HOST, "imap_port": IMAP_PORT, "imap_security": "none",
        "verify_tls": False, "root_folder": root_folder, "caldav_url": DAV_URL, "carddav_url": DAV_URL,
        "dav_enabled": True, "workers": workers, "notes": ""}).json()
    acc = client.post("/api/accounts", json={"server_id": srv["id"], "email": email, "password": "pw"}).json()
    # upload + validation du faux PST
    from app.server.uploads import validate
    data = make_fake_pst(str(env.data_dir / "src.pst"), 200_000)
    up = client.post("/api/uploads", json={"filename": f"{email}.pst", "size": len(data), "last_modified": 1}).json()
    cs = up["chunk_size"]
    for n in range(up["chunks"]):
        c = data[n * cs:(n + 1) * cs]
        assert client.put(f"/api/uploads/{up['id']}/chunks/{n}", content=c,
                          headers={"X-Chunk-Sha256": hashlib.sha256(c).hexdigest()}).status_code == 200
    client.post(f"/api/uploads/{up['id']}/complete")
    validate(up["id"])
    assert client.get(f"/api/psts/{up['id']}").json()["status"] == "valid"
    client.put(f"/api/accounts/{acc['id']}/pst", json={"pst_id": up["id"]})
    return acc["id"], email


def imap_conn(email):
    from app.server.imap import ImapConn, ImapParams
    return ImapConn(ImapParams(IMAP_HOST, IMAP_PORT, "none", False, email, "pw")).connect()


def server_import_ids(email):
    """{import_id: nombre d'exemplaires} sur tous les dossiers."""
    import email as em
    c = imap_conn(email)
    out = {}
    try:
        for b in c.list():
            if "\\Noselect" in b.flags:
                continue
            n = c.select(b.name)
            if not n:
                continue
            typ, data = c.M.fetch("1:*", "(BODY.PEEK[HEADER.FIELDS (X-PST-Import-Id)])")
            for d in data:
                if isinstance(d, tuple):
                    v = em.message_from_bytes(d[1]).get("X-PST-Import-Id")
                    if v:
                        out[v] = out.get(v, 0) + 1
    finally:
        c.close()
    return out


def run(client, aid):
    r = client.post("/api/runs", json={"account_ids": [aid]}).json()
    assert isinstance(r, list) and r, r
    return r[0]["id"]


def test_full_import(client, env):
    aid, email = setup_account(client, env)
    assert client.post(f"/api/accounts/{aid}/test").json()["imap"]["ok"] is True
    jid = run(client, aid)
    with WorkerThread():
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"))
    assert j["status"] == "done", client.get(f"/api/jobs/{jid}/log").json()
    assert j["progress"]["mail"] == {"done": N_MAIL, "total": N_MAIL, "errors": 0}
    assert j["progress"]["calendar"]["done"] == N_EVENT and j["progress"]["contacts"]["done"] == N_CONTACT
    assert j["rate"] is not None and j["rate"] > 0 and j["eta"] is None  # débit moyen conservé en fin de job
    ids = server_import_ids(email)
    assert len(ids) == N_MAIL and set(ids.values()) == {1}
    c = imap_conn(email)
    names = {b.decoded for b in c.list()}
    c.close()
    assert "INBOX" in names and any("Été 2024" in n for n in names) and "Sent" in names
    rep = client.get(f"/api/jobs/{jid}/report").json()
    assert rep["totals"]["mail"]["sent"] == N_MAIL and rep["totals"]["mail"]["errors"] == 0
    assert rep["mail"]["sent"] == N_MAIL and rep["contacts"]["present"] == N_CONTACT
    assert all(f["present"] >= f["sent"] for f in rep["folders"]) and isinstance(rep["folders"][0]["path"], list)
    assert "relu" in rep["sample_check"]
    assert rep["verify"]["sample"]["ok"] == rep["verify"]["sample"]["checked"] > 0
    assert rep["dav"]["calendar"]["present"] == N_EVENT and rep["dav"]["contacts"]["present"] == N_CONTACT
    assert (env.reports_dir / f"job-{jid}.json").exists() and (env.logs_dir / f"job-{jid}.log").exists()
    # relance complète : aucun doublon (items ok sautés, PUT DAV idempotent)
    client.post(f"/api/jobs/{jid}/retry")
    with WorkerThread():
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"))
    assert j["status"] == "done"
    assert set(server_import_ids(email).values()) == {1} and len(server_import_ids(email)) == N_MAIL


def test_root_folder(client, env):
    aid, email = setup_account(client, env, root_folder="Import PST")
    jid = run(client, aid)
    with WorkerThread():
        assert wait_job(client, jid, ("done", "done_with_errors", "failed"))["status"] == "done"
    c = imap_conn(email)
    names = {b.decoded for b in c.list()}
    d = c.delimiter()
    assert c.status_messages("INBOX") == 0
    c.close()
    assert f"Import PST{d}Boîte de réception" in names and f"Import PST{d}Éléments envoyés" in names


def test_pause_resume(client, env):
    fake_engine.CONFIG["folders"] = [(["Boîte de réception"], "mail", "IPF.Note", "inbox", 30)]
    fake_engine.CONFIG["delay"] = 0.1
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    with WorkerThread():
        wait_job(client, jid, ("running",), cond=lambda j: j["progress"]["mail"]["done"] >= 3)
        assert client.post(f"/api/jobs/{jid}/pause").json()["status"] == "paused"
        time.sleep(1.5)
        j1 = client.get(f"/api/jobs/{jid}").json()
        time.sleep(1.5)
        j2 = client.get(f"/api/jobs/{jid}").json()
        assert j2["status"] == "paused" and j1["progress"] == j2["progress"]
        assert j2["progress"]["mail"]["done"] < 30
        assert client.post(f"/api/jobs/{jid}/resume").json()["status"] in ("queued", "running")
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"))
    assert j["status"] == "done"
    ids = server_import_ids(email)
    assert len(ids) == 30 and set(ids.values()) == {1}


def test_stop_then_retry_without_duplicates(client, env):
    fake_engine.CONFIG["folders"] = [(["Boîte de réception"], "mail", "IPF.Note", "inbox", 25),
                                     (["Contacts"], "contacts", "IPF.Contact", None, 3)]
    fake_engine.CONFIG["delay"] = 0.08
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    with WorkerThread():
        wait_job(client, jid, ("running",), cond=lambda j: j["progress"]["mail"]["done"] >= 5)
        assert client.post(f"/api/jobs/{jid}/stop").json()["status"] == "stopping"
        j = wait_job(client, jid, ("stopped",))
        assert j["progress"]["mail"]["done"] < 25 and j["finished_at"]
        fake_engine.CONFIG["delay"] = 0.0
        assert client.post(f"/api/jobs/{jid}/retry").json()["status"] == "queued"
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"))
    assert j["status"] == "done"
    ids = server_import_ids(email)
    assert len(ids) == 25 and set(ids.values()) == {1}


def test_inflight_crash_recovery(client, env):
    """Crash simulé : un mail APPENDé mais resté `inflight`, un autre `inflight` jamais envoyé,
    et le job resté `running` avec un worker mort → reprise sans doublon ni perte."""
    from app.server import db
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    with WorkerThread():
        assert wait_job(client, jid, ("done",))["status"] == "done"
    rows = db.q("SELECT * FROM items WHERE account_id=? AND kind='mail' AND status='ok' ORDER BY id LIMIT 2", (aid,))
    pst_sha = db.q1("SELECT p.pst_sha FROM psts p JOIN accounts a ON a.pst_id=p.id WHERE a.id=?", (aid,))["pst_sha"]
    appended, lost = rows[0], rows[1]
    # « lost » : marqué inflight mais absent du serveur (crash avant la fin de l'APPEND)
    c = imap_conn(email)
    c.select(lost["target"], readonly=False)
    uids = c.search_header(lost["target"], "X-PST-Import-Id", f"{pst_sha}-{lost['item_key']}")
    assert len(uids) == 1
    c.M.uid("STORE", uids[0], "+FLAGS", "(\\Deleted)")
    c.M.expunge()
    c.close()
    db.ex("UPDATE items SET status='inflight' WHERE id IN (?,?)", (appended["id"], lost["id"]))
    # job « running » appartenant à un worker mort
    db.ex("UPDATE jobs SET status='running', worker_id='morte:1:1', heartbeat=0, finished_at=NULL WHERE id=?", (jid,))
    with WorkerThread():
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"), cond=lambda j: j["finished_at"])
    assert j["status"] == "done"
    ids = server_import_ids(email)
    assert len(ids) == N_MAIL and set(ids.values()) == {1}
    st = {r["id"]: r["status"] for r in db.q("SELECT id, status FROM items WHERE id IN (?,?)", (appended["id"], lost["id"]))}
    assert set(st.values()) == {"ok"}
    log = "\n".join(l["msg"] for l in client.get(f"/api/jobs/{jid}/log").json()["lines"])
    assert "orphelin" in log and "inflight" in log


def test_errors_then_retry_errors(client, env):
    inbox = (["Boîte de réception"], "mail", "IPF.Note", "inbox", 5)
    fake_engine.CONFIG["folders"] = [inbox]
    bad = fake_engine.item_key(inbox[0], 2)
    fake_engine.CONFIG["fail_keys"] = {bad}
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    with WorkerThread():
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"))
        assert j["status"] == "done_with_errors" and j["progress"]["mail"]["errors"] == 1
        errs = client.get(f"/api/jobs/{jid}/errors").json()
        assert len(errs) == 1 and errs[0]["item_key"] == bad
        assert client.get(f"/api/jobs/{jid}/report").json()["totals"]["mail"]["errors"] == 1
        fake_engine.CONFIG["fail_keys"] = set()
        assert client.post(f"/api/jobs/{jid}/retry-errors").json()["mode"] == "errors"
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"), cond=lambda j: j["finished_at"])
    assert j["status"] == "done" and j["progress"]["mail"] == {"done": 5, "total": 5, "errors": 0}
    ids = server_import_ids(email)
    assert len(ids) == 5 and set(ids.values()) == {1}


def test_auth_failure_marks_job_failed(client, env):
    """GreenMail en mode auth désactivée accepte tout : on simule un serveur injoignable."""
    from app.server import db
    aid, email = setup_account(client, env)
    sid = client.get(f"/api/accounts/{aid}").json()["server_id"]
    db.ex("UPDATE servers SET imap_port=1 WHERE id=?", (sid,))
    jid = run(client, aid)
    with WorkerThread():
        j = wait_job(client, jid, ("failed",), timeout=120)
    assert "IMAP" in j["error"]


# ------------------------------------------------------------------ isolation des crashs natifs (libpff)
def _items(aid, kind="mail"):
    from app.server import db
    return {r["item_key"]: dict(r) for r in db.q("SELECT * FROM items WHERE account_id=? AND kind=?", (aid, kind))}


def _log(client, jid):
    return "\n".join(l["msg"] for l in client.get(f"/api/jobs/{jid}/log").json()["lines"])


def test_child_crash_on_item_continues(client, env, monkeypatch):
    """SIGSEGV du sous-processus sur un élément : marqué « crash libpff », les autres importés, aucun doublon."""
    inbox = (["Boîte de réception"], "mail", "IPF.Note", "inbox", 6)
    fake_engine.CONFIG["folders"] = [inbox, (["Contacts"], "contacts", "IPF.Contact", None, 2)]
    bad = fake_engine.item_key(inbox[0], 3)
    monkeypatch.setenv("PSTMIG_TEST_CRASH", bad)
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    with WorkerThread():
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"), cond=lambda j: j["finished_at"])
    assert j["status"] == "done_with_errors", _log(client, jid)
    assert j["progress"]["mail"] == {"done": 5, "total": 6, "errors": 1}
    assert j["progress"]["contacts"]["done"] == 2
    items = _items(aid)
    assert items[bad]["status"] == "error" and items[bad]["error"].startswith("crash libpff")
    assert all(r["status"] == "ok" for k, r in items.items() if k != bad)
    ids = server_import_ids(email)
    assert len(ids) == 5 and set(ids.values()) == {1}
    log = _log(client, jid)
    assert "Crash du sous-processus (signal SIGSEGV, 1/3)" in log and bad in log
    # relance complète (crochet toujours actif) : l'élément fautif est sauté, pas de nouveau crash ni doublon
    client.post(f"/api/jobs/{jid}/retry")
    with WorkerThread():
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"), cond=lambda j: j["finished_at"])
    assert j["status"] == "done_with_errors" and j["progress"]["mail"]["errors"] == 1
    assert _log(client, jid).count("Crash du sous-processus") == 1
    ids = server_import_ids(email)
    assert len(ids) == 5 and set(ids.values()) == {1}


def test_retry_errors_retries_crash_item_after_fix(client, env, monkeypatch):
    """Régression (e2e intermittent) : un run « erreurs » qui replante laisse le compteur de crashs à 0 dès que le
    statut final est visible ; et toute relance repart d'un compteur à 0 (cas d'un worker arrêté avant d'avoir
    remis le compteur à zéro). Crochet retiré → « relancer les erreurs » importe l'élément."""
    from app.server import crash, db
    inbox = (["Boîte de réception"], "mail", "IPF.Note", "inbox", 4)
    fake_engine.CONFIG["folders"] = [inbox]
    bad = fake_engine.item_key(inbox[0], 1)
    monkeypatch.setenv("PSTMIG_TEST_CRASH", bad)
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    with WorkerThread():
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"), cond=lambda j: j["finished_at"])
        assert j["status"] == "done_with_errors", _log(client, jid)
        client.post(f"/api/jobs/{jid}/retry-errors")
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"), cond=lambda j: j["finished_at"])
        assert j["status"] == "done_with_errors", _log(client, jid)
        assert crash.get_crashes(jid) == 0  # remis à zéro par le run lui-même, avant le statut final
    assert _log(client, jid).count("Crash du sous-processus") == 2
    # compteur résiduel (worker arrêté entre la fin du run et la sortie du sous-processus)
    db.kv_set(f"job:{jid}:crashes", "1")
    monkeypatch.setenv("PSTMIG_TEST_CRASH", "")
    client.post(f"/api/jobs/{jid}/retry-errors")
    assert crash.get_crashes(jid) == 0
    with WorkerThread():
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"), cond=lambda j: j["finished_at"])
    assert j["status"] == "done", _log(client, jid)
    assert _items(aid)[bad]["status"] == "ok"
    ids = server_import_ids(email)
    assert len(ids) == 4 and set(ids.values()) == {1}


def test_child_crash_on_fetch_skips_index(client, env, monkeypatch):
    """Crash pendant la lecture (iter_items) de l'élément n° 2 : index sauté, élément d'erreur synthétique."""
    fake_engine.CONFIG["folders"] = [(["Boîte de réception"], "mail", "IPF.Note", "inbox", 5)]
    monkeypatch.setenv("PSTMIG_TEST_CRASH", "fetch:2")
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    with WorkerThread():
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"), cond=lambda j: j["finished_at"])
    assert j["status"] == "done_with_errors", _log(client, jid)
    items = _items(aid)
    errs = {k: r for k, r in items.items() if r["status"] == "error"}
    assert len(errs) == 1 and next(iter(errs)).endswith("#crash2")
    assert next(iter(errs.values()))["error"].startswith("crash libpff")
    assert sum(r["status"] == "ok" for r in items.values()) == 4
    assert fake_engine.item_key(["Boîte de réception"], 2) not in items
    ids = server_import_ids(email)
    assert len(ids) == 4 and set(ids.values()) == {1}


def test_child_crash_always_fails_after_n(client, env, monkeypatch):
    """Crash systématique : le job passe en échec après MAX_CHILD_CRASHES (3) crashs, sans boucle infinie."""
    fake_engine.CONFIG["folders"] = [(["Boîte de réception"], "mail", "IPF.Note", "inbox", 6)]
    monkeypatch.setenv("PSTMIG_TEST_CRASH", "*")
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    with WorkerThread():
        j = wait_job(client, jid, ("failed", "done", "done_with_errors"), cond=lambda j: j["finished_at"])
        time.sleep(1.5)  # pas de relance après l'échec
        assert client.get(f"/api/jobs/{jid}").json()["status"] == "failed"
    assert j["status"] == "failed"
    assert j["error"] == "Le sous-processus a planté 3 fois (crash libpff probable)"
    log = _log(client, jid)
    assert "planté 3 fois" in log and log.count("Crash du sous-processus") == 3
    items = _items(aid)
    assert sum(r["status"] == "error" and r["error"].startswith("crash libpff") for r in items.values()) == 3
    from app.server import crash
    assert crash.get_crashes(jid) == 0  # compteur remis à zéro : une relance repart de 0


def test_child_crash_at_open_fails(client, env, monkeypatch):
    monkeypatch.setenv("PSTMIG_TEST_CRASH", "open")
    monkeypatch.setenv("MAX_CHILD_CRASHES", "2")
    env.reload()
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    with WorkerThread():
        j = wait_job(client, jid, ("failed", "done", "done_with_errors"), cond=lambda j: j["finished_at"])
    assert j["status"] == "failed" and "planté 2 fois" in j["error"]
    assert "hors élément (étape open)" in _log(client, jid)


def test_worker_shutdown_terminates_child_and_requeues(client, env):
    """Arrêt du worker pendant un job : sous-processus terminé, job remis en file, reprise sans doublon."""
    fake_engine.CONFIG["folders"] = [(["Boîte de réception"], "mail", "IPF.Note", "inbox", 30)]
    fake_engine.CONFIG["delay"] = 0.1
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    wt = WorkerThread()
    with wt as w:
        wait_job(client, jid, ("running",), cond=lambda j: j["progress"]["mail"]["done"] >= 3)
        procs = list(w.procs.values())
        assert procs and all(p.poll() is None for p in procs)
    assert all(p.poll() is not None for p in procs)
    j = client.get(f"/api/jobs/{jid}").json()
    assert j["status"] == "queued" and j["progress"]["mail"]["done"] < 30
    assert "Worker arrêté" in _log(client, jid) and "Crash" not in _log(client, jid)
    fake_engine.CONFIG["delay"] = 0.0
    with WorkerThread():
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"))
    assert j["status"] == "done"
    ids = server_import_ids(email)
    assert len(ids) == 30 and set(ids.values()) == {1}


def test_rate_eta_on_short_job(client, env):
    """Job court (quelques secondes) : débit et temps restant calculés dès le début de la phase, pas « — »."""
    fake_engine.CONFIG["folders"] = [(["Boîte de réception"], "mail", "IPF.Note", "inbox", 12)]
    fake_engine.CONFIG["delay"] = 0.15
    aid, email = setup_account(client, env)
    jid = run(client, aid)
    with WorkerThread():
        j = wait_job(client, jid, ("running",), cond=lambda j: j["phase"] == "mail" and j["rate"] is not None)
        assert j["rate"] > 0 and j["eta"] is not None, j
        j = wait_job(client, jid, ("done", "done_with_errors", "failed"), cond=lambda j: j["finished_at"])
    assert j["status"] == "done" and j["rate"] > 0, j
