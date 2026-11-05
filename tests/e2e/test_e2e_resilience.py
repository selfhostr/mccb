"""Résilience réelle : worker tué (SIGKILL) en plein import puis relancé ; crash natif simulé sur un élément."""
from __future__ import annotations

import os
import signal
import time

from tests.e2e.helpers import FIXTURES, create_user, source_model, verify_on_servers

ENRON = FIXTURES / "enron.pst"


def _live_in_group(pgid: int) -> list[int]:
    """Processus vivants (hors zombies) du groupe `pgid`."""
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                st = f.read()
        except OSError:
            continue
        fields = st[st.rindex(")") + 2:].split()
        if int(fields[2]) == pgid and fields[0] != "Z":
            out.append(int(d))
    return out


def _setup(st, prefix: str):
    srv = st.create_server(name=f"srv-{prefix}")
    user = create_user(prefix=prefix)
    acc = st.create_account(srv["id"], user, "pw")
    pst = st.upload(ENRON, bad_chunk=None)
    assert pst["status"] == "valid", pst
    st.api("PUT", f"/api/accounts/{acc['id']}/pst", json={"pst_id": pst["id"]})
    model = source_model(ENRON, pst["pst_sha"])
    return user, acc, pst, model


def test_worker_sigkill_mid_import_resumes_without_duplicates(stack_factory):
    st = stack_factory(PSTMIG_TEST_ITEM_DELAY="0.08")
    user, acc, pst, model = _setup(st, "kill")
    n_mail = sum(len(v) for v in model["mail"].values())
    jid = st.api("POST", "/api/runs", json={"account_ids": [acc["id"]]}).json()[0]["id"]
    st.wait_job(jid, ("running",), timeout=120, cond=lambda j: j["progress"]["mail"]["done"] >= 15)
    w = st.worker
    group = _live_in_group(w.pid)
    assert len(group) >= 2, group  # worker + sous-processus du job
    # SIGKILL du seul worker : ses sous-processus meurent avec lui (PR_SET_PDEATHSIG)
    os.kill(w.pid, signal.SIGKILL)
    w.wait()
    end = time.time() + 15
    while _live_in_group(w.pid):
        assert time.time() < end, f"sous-processus survivants : {_live_in_group(w.pid)}"
        time.sleep(0.1)
    j1 = st.api("GET", f"/api/jobs/{jid}").json()
    assert j1["status"] == "running", j1  # orphelin : personne ne le fait avancer
    done1 = j1["progress"]["mail"]["done"]
    assert done1 < n_mail, j1
    time.sleep(1.5)
    assert st.api("GET", f"/api/jobs/{jid}").json()["progress"] == j1["progress"]
    # relance du worker : job orphelin remis en file et repris sans doublon
    st.start_worker()
    st.wait_job(jid, ("running",), timeout=120, cond=lambda j: j["progress"]["mail"]["done"] >= done1 + 10)
    # arrêt propre du worker (SIGTERM, comme `docker stop`) : sous-processus terminés, job remis en file
    st.worker.terminate()
    assert st.worker.wait(60) == 0
    j2 = st.api("GET", f"/api/jobs/{jid}").json()
    assert j2["status"] == "queued" and j2["progress"]["mail"]["done"] < n_mail, j2
    assert not _live_in_group(st.worker.pid)
    st.start_worker()
    j = st.wait_job(jid, timeout=300)
    assert j["status"] == "done", st.job_log(jid)
    assert "orphelin" in st.job_log(jid)
    rep = st.api("GET", f"/api/jobs/{jid}/report").json()
    got = verify_on_servers(st, user, pst, model, rep)
    assert got["mail"] == n_mail and rep["totals"]["mail"]["sent"] == n_mail and not rep["errors"], got


def test_crash_isolation_real_engine(stack_factory):
    # élément visé : le 6e mail du plus gros dossier d'enron
    from app.engine import PstFile
    from app.server.runner import folder_key
    with PstFile(str(ENRON)) as p:
        f = max((f for f in p.folders() if f.kind == "mail"), key=lambda f: f.count)
        target = [k for i, k, h in p.iter_items(f) if i == 5][0]
    st = stack_factory(PSTMIG_TEST_CRASH=target)
    user, acc, pst, model = _setup(st, "crash")
    n_mail = sum(len(v) for v in model["mail"].values())
    jid = st.api("POST", "/api/runs", json={"account_ids": [acc["id"]]}).json()[0]["id"]
    j = st.wait_job(jid, timeout=300)
    assert j["status"] == "done_with_errors", st.job_log(jid)
    errs = st.api("GET", f"/api/jobs/{jid}/errors").json()
    assert [(e["item_key"], e["folder"]) for e in errs] == [(target, folder_key(f.path))], errs
    assert errs[0]["error"].startswith("crash libpff"), errs
    rep = st.api("GET", f"/api/jobs/{jid}/report").json()
    assert [e["item_key"] for e in rep["errors"]] == [target]
    assert rep["totals"]["mail"]["errors"] == 1 and rep["totals"]["mail"]["sent"] == n_mail - 1
    assert "Crash du sous-processus" in st.job_log(jid)
    got = verify_on_servers(st, user, pst, model, rep)  # le reste importé une seule fois
    assert got["mail"] == n_mail - 1, got
    # « relancer les erreurs » relit une fois l'élément : il plante encore → toujours en erreur, rien d'autre ne bouge
    st.api("POST", f"/api/jobs/{jid}/retry-errors")
    j = st.wait_job(jid, timeout=300)
    assert j["status"] == "done_with_errors", st.job_log(jid)
    assert [e["item_key"] for e in st.api("GET", f"/api/jobs/{jid}/errors").json()] == [target]
    # libpff « réparé » (worker sans crochet) : la relance des erreurs importe l'élément, une seule fois
    st.stop_worker()
    st.start_worker(PSTMIG_TEST_CRASH="")
    st.api("POST", f"/api/jobs/{jid}/retry-errors")
    j = st.wait_job(jid, timeout=300)
    assert j["status"] == "done", st.job_log(jid)
    rep = st.api("GET", f"/api/jobs/{jid}/report").json()
    assert verify_on_servers(st, user, pst, model, rep)["mail"] == n_mail


def test_crash_during_validation_marks_pst_invalid(stack_factory):
    st = stack_factory(PSTMIG_TEST_CRASH="validate")
    p = st.upload(FIXTURES / "aspose-sample-contacts.pst", bad_chunk=None)
    assert p["status"] == "invalid" and "crash libpff pendant l'analyse" in (p["error"] or ""), p
    # le worker a survécu et analyse normalement une fois le crochet retiré
    assert st.worker.poll() is None
    st.stop_worker()
    st.start_worker(PSTMIG_TEST_CRASH="")
    p2 = st.upload(FIXTURES / "aspose-outlook.pst", bad_chunk=None)
    assert p2["status"] == "valid", p2
