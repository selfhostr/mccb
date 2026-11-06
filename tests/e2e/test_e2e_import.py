"""Real end-to-end import: chunked upload → validation → preview → run (pause/resume/stop/restart)
→ direct check on GreenMail and Radicale, compared to the source read with the engine."""
from __future__ import annotations

import io
import zipfile

import pytest

from tests.e2e.helpers import FIXTURES, create_user, fffd_problems, parse, source_model, verify_on_servers

FIXTURE_FILES = ["enron.pst", "aspose-outlook.pst", "aspose-sample-contacts.pst", "aspose-sample.ost",
                 "aspose-test-part0.pst"]  # the latter contains an unreadable item (justified error)
RESULTS: dict[str, dict] = {}


@pytest.fixture(scope="module")
def stack(stack_factory):
    # per-item delay and one builder: at least 40 ms per mail, whatever the machine, which leaves time to drive the
    # job (pause/stop) during a real import
    return stack_factory(PSTMIG_TEST_ITEM_DELAY="0.04", BUILD_PROCESSES="1")


@pytest.fixture(scope="module")
def server(stack):
    srv = stack.create_server(name="GreenMail + Radicale")
    t = stack.api("POST", f"/api/servers/{srv['id']}/test").json()
    assert t, t
    return srv


def test_login_required(stack):
    import httpx
    with httpx.Client(base_url=str(stack.client.base_url)) as anon:
        assert anon.get("/api/psts").status_code == 401
        assert anon.post("/api/login", json={"password": "faux"}).status_code in (401, 403)
    assert stack.api("GET", "/api/me").json()


def test_account_wrong_password_auth_failed(stack, server):
    user = create_user(password="bon-mot-de-passe")
    acc = stack.create_account(server["id"], user, "mauvais")
    res = stack.api("POST", f"/api/accounts/{acc['id']}/test").json()
    assert res["imap"]["ok"] is False, res
    assert stack.api("GET", f"/api/accounts/{acc['id']}").json()["status"] == "auth_failed"
    assert res["caldav"]["ok"] is False and res["carddav"]["ok"] is False, res
    # password fixed → OK
    stack.api("PUT", f"/api/accounts/{acc['id']}", json={"password": "bon-mot-de-passe"})
    res = stack.api("POST", f"/api/accounts/{acc['id']}/test").json()
    assert res["imap"]["ok"] and res["caldav"]["ok"] and res["carddav"]["ok"], res
    assert stack.api("GET", f"/api/accounts/{acc['id']}").json()["status"] == "ok"


def test_account_imaps(stack):
    """IMAPS (GreenMail 3993, self-signed certificate): OK without TLS verification, refused with it."""
    user = create_user()
    for verify, ok in ((False, True), (True, False)):
        srv = stack.create_server(name=f"imaps-{verify}", imap_port=3993, imap_security="ssl", verify_tls=verify,
                                  dav_enabled=False)
        acc = stack.create_account(srv["id"], user, "pw")
        res = stack.api("POST", f"/api/accounts/{acc['id']}/test").json()
        assert res["imap"]["ok"] is ok, res
        if not ok:
            assert stack.api("GET", f"/api/accounts/{acc['id']}").json()["status"] == "error"


def test_invalid_upload(stack, tmp_path):
    bad = tmp_path / "pas-un-pst.pst"
    bad.write_bytes(b"NOPE" + b"\0" * (1024 * 1024 + 10))
    p = stack.upload(bad, bad_chunk=None)
    assert p["status"] == "invalid" and p["error"], p
    assert stack.client.get(f"/api/psts/{p['id']}/preview").status_code == 409


def _check_preview(stack, pst: dict, model: dict) -> None:
    r = stack.api("GET", f"/api/psts/{pst['id']}/preview?limit=50")
    assert r.headers["content-type"].startswith("application/zip")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    n_mail = sum(len(v) for v in model["mail"].values())
    n_ev = sum(len(v) for v in model["calendar"].values())
    n_ct = sum(len(v) for v in model["contacts"].values())
    emls = [n for n in names if n.endswith(".eml")]
    assert bool(emls) == bool(n_mail), names
    assert bool([n for n in names if n.endswith(".ics")]) == bool(n_ev), names
    assert bool([n for n in names if n.endswith(".vcf")]) == bool(n_ct), names
    for n in emls:
        bad = fffd_problems(parse(z.read(n)), n)
        assert not bad, bad
    for n in names:
        if n.endswith((".ics", ".vcf")):
            assert "\ufffd" not in z.read(n).decode("utf-8"), n


def _drive_controls(stack, jid: int, total_mail: int) -> None:
    """During the run: pause → (progress frozen) → resume → stop → restart.
    `total_mail`: every item of the mail folders. `done` counts the items `ok` and `skipped` (a non-mail item filed in
    a mail folder), each at most once: it reaches this total, never more, when the job finished the phase."""
    import time
    j = stack.wait_job(jid, ("running",), timeout=120, cond=lambda j: j["progress"]["mail"]["done"] >= 5)
    assert stack.api("POST", f"/api/jobs/{jid}/pause").json()["status"] == "paused"
    time.sleep(1.5)  # the subprocess notices the pause and releases the job
    j1 = stack.wait_job(jid, ("paused",), timeout=30)
    time.sleep(1.0)
    j2 = stack.api("GET", f"/api/jobs/{jid}").json()
    assert j2["status"] == "paused" and j2["progress"] == j1["progress"], (j1, j2)
    assert j2["progress"]["mail"]["done"] < total_mail, j2
    paused_done = j2["progress"]["mail"]["done"]
    assert stack.api("POST", f"/api/jobs/{jid}/resume").json()["status"] in ("queued", "running")
    stack.wait_job(jid, ("running",), timeout=60, cond=lambda j: j["progress"]["mail"]["done"] >= paused_done + 3)
    st = stack.api("POST", f"/api/jobs/{jid}/stop").json()["status"]
    assert st in ("stopping", "stopped"), st
    j = stack.wait_job(jid, ("stopped",), timeout=60)
    assert j["progress"]["mail"]["done"] <= total_mail, j
    if j["phase"] == "mail":  # otherwise the stop came after the last mail: a slow test machine, not a bug
        assert j["progress"]["mail"]["done"] < total_mail, j
    rep = stack.api("GET", f"/api/jobs/{jid}/report").json()
    assert rep["status"] == "stopped"
    assert stack.api("POST", f"/api/jobs/{jid}/retry").json()["status"] == "queued"


@pytest.mark.parametrize("fixture", FIXTURE_FILES)
def test_import_fixture(stack, server, fixture):
    path = FIXTURES / fixture
    user = create_user(prefix="e2e")
    acc = stack.create_account(server["id"], user, "pw")
    res = stack.api("POST", f"/api/accounts/{acc['id']}/test").json()
    assert res["imap"]["ok"] and res["caldav"]["ok"] and res["carddav"]["ok"], res

    pst = stack.upload(path, bad_chunk=min(1, path.stat().st_size // (1024 * 1024)))
    assert pst["status"] == "valid", pst
    from app.engine import PstFile
    with PstFile(str(path)) as p:
        src_scan = p.scan()
    assert pst["scan"]["totals"] == src_scan["totals"], (pst["scan"]["totals"], src_scan["totals"])
    model = source_model(path, pst["pst_sha"])
    # items unreadable by the engine (damaged PST): the only allowed errors, and they must appear in the report
    kind_of = {"mail": "mail", "calendar": "event", "contacts": "contact"}
    src_errs = {(kind_of[e["kind"]], e["key"]) for e in model["errors"]}
    n_err = {k: sum(1 for e in model["errors"] if e["kind"] == k) for k in kind_of}
    final = "done_with_errors" if src_errs else "done"
    n_mail = sum(len(v) for v in model["mail"].values())
    sk_mail = model["skipped"]["mail"]
    n_ev = sum(len(v) for v in model["calendar"].values())
    n_ct = sum(len(v) for v in model["contacts"].values())
    _check_preview(stack, pst, model)

    stack.api("PUT", f"/api/accounts/{acc['id']}/pst", json={"pst_id": pst["id"]})
    jobs = stack.api("POST", "/api/runs", json={"account_ids": [acc["id"]]}).json()
    jid = jobs[0]["id"]
    if n_mail >= 50:
        _drive_controls(stack, jid, n_mail + sk_mail)
    j = stack.wait_job(jid, timeout=300)
    assert j["status"] == final, f"{j}\n{stack.job_log(jid)}"
    sk = model["skipped"]
    assert j["progress"]["mail"] == {"done": n_mail + sk["mail"], "total": src_scan["totals"]["mail"],
                                     "errors": n_err["mail"]}, j["progress"]
    assert j["progress"]["calendar"]["done"] == n_ev + sk["calendar"] and j["progress"]["contacts"]["done"] == \
        n_ct + sk["contacts"], j["progress"]
    rep = stack.api("GET", f"/api/jobs/{jid}/report").json()
    assert rep["status"] == final and not rep["verify"]["problems"], rep["verify"]
    assert {(e["kind"], e["item_key"]) for e in rep["errors"]} == src_errs, (rep["errors"], model["errors"])
    errs = stack.api("GET", f"/api/jobs/{jid}/errors").json()
    assert {(e["kind"], e["item_key"]) for e in errs} == src_errs, errs

    got = verify_on_servers(stack, user, pst, model, rep)
    assert got == {"mail": n_mail, "calendar": n_ev, "contacts": n_ct}, got
    # report consistent with what is actually on the servers
    t = rep["totals"]
    assert t["mail"]["source"] == src_scan["totals"]["mail"]
    assert t["mail"]["sent"] == t["mail"]["present"] == n_mail and t["mail"]["errors"] == n_err["mail"]
    assert t["calendar"]["sent"] == n_ev and t["contacts"]["sent"] == n_ct
    assert (t["mail"]["skipped"], t["calendar"]["skipped"], t["contacts"]["skipped"]) == \
        (sk["mail"], sk["calendar"], sk["contacts"]), t
    assert (t["calendar"]["present"] or 0) == n_ev and (t["contacts"]["present"] or 0) == n_ct
    if n_mail:
        smp = rep["verify"]["sample"]
        assert smp["checked"] == smp["ok"] > 0 and not smp["problems"], smp
    dl = stack.api("GET", f"/api/jobs/{jid}/report?download=1")
    assert "attachment" in dl.headers.get("content-disposition", "")
    RESULTS[fixture] = {**got, "skipped": sk, "errors": n_err}

    # full restart: nothing is resent, still no duplicates
    stack.api("POST", f"/api/jobs/{jid}/retry")
    j = stack.wait_job(jid, timeout=300)
    assert j["status"] == final, stack.job_log(jid)
    assert verify_on_servers(stack, user, pst, model, stack.api("GET", f"/api/jobs/{jid}/report").json(),
                             check_all=False) == got
    print(f"\n[e2e] {fixture}: {got} skipped={sk} errors(unreadable source)={n_err}")


def test_spa_assets_served(stack):
    """The real server serves the built SPA and its assets, or says plainly that the UI was not built
    (the test stack mounts the sources, the image contains the build)."""
    import re
    from pathlib import Path
    r = stack.api("GET", "/")
    if not (Path(__file__).resolve().parents[2] / "app/web/index.html").exists():
        assert "Web interface missing" in r.text
        return
    assert "text/html" in r.headers["content-type"]
    assets = re.findall(r'(?:src|href)="/?(assets/[^"]+)"', r.text)
    assert assets, r.text
    for p in assets:
        a = stack.api("GET", "/" + p)
        kind = "javascript" if p.endswith(".js") else "css" if p.endswith(".css") else ""
        assert kind in a.headers["content-type"], (p, a.headers["content-type"])


def test_upload_resume_and_sse(stack, tmp_path):
    """Upload interrupted then resumed (same file → same upload, already received chunks kept); SSE events."""
    import hashlib
    import threading

    import httpx
    events: list[str] = []

    def listen():
        with httpx.Client(base_url=str(stack.client.base_url), cookies=stack.client.cookies, timeout=30) as c:
            with c.stream("GET", "/api/events?max_seconds=6") as r:
                for line in r.iter_lines():
                    if line.startswith("event: "):
                        events.append(line[7:])

    th = threading.Thread(target=listen)
    th.start()
    import time
    time.sleep(0.5)
    path = FIXTURES / "aspose-outlook.pst"
    data = path.read_bytes()
    meta = {"filename": "reprise.pst", "size": len(data), "last_modified": 42}
    up = stack.api("POST", "/api/uploads", json=meta).json()
    cs = up["chunk_size"]
    for n in (0, 2):
        c = data[n * cs:(n + 1) * cs]
        stack.api("PUT", f"/api/uploads/{up['id']}/chunks/{n}", content=c,
                  headers={"X-Chunk-Sha256": hashlib.sha256(c).hexdigest()})
    # premature finalization refused while chunks are missing
    assert stack.client.post(f"/api/uploads/{up['id']}/complete").status_code >= 400
    again = stack.api("POST", "/api/uploads", json=meta).json()
    assert again["id"] == up["id"] and sorted(again["received"]) == [0, 2], again
    tmpf = tmp_path / "reprise.pst"
    tmpf.write_bytes(data)
    import os
    os.utime(tmpf, (42, 42))
    p = stack.upload(tmpf, bad_chunk=None)
    assert p["id"] == up["id"] and p["status"] == "valid", p
    th.join(15)
    assert "upload" in events or "pst" in events, events
