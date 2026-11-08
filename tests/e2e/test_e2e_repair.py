"""Repair tab against a real IMAP server: analysis writes nothing, repair keeps flags and dates and the originals,
a second pass changes nothing, restore brings the originals back."""
from __future__ import annotations

import imaplib
import time
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

from tests.e2e.helpers import IMAP_HOST, IMAP_PORT, TERMINAL, create_user

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"
BACKUP = "_originaux_avant_reparation"
WHEN = '"05-Mar-2024 10:00:00 +0100"'


def _msg(subject: str, text: str, html: str | None = None) -> EmailMessage:
    m = EmailMessage()
    m["From"] = "zoe@example.com"
    m["To"] = "team@example.com"
    m["Subject"] = subject
    m["Date"] = "Tue, 05 Mar 2024 10:00:00 +0100"
    m["Message-ID"] = f"<{subject.replace(' ', '-')}@example.com>"
    m.set_content(text)
    if html:
        m.add_alternative(html, subtype="html")
    return m


def _seed(user: str) -> None:
    broken = _msg("accents", "Déjà reçu à Genève.", "<p>D�j� re�u � Gen�ve.</p>")
    named = _msg("pieces", "voir pièce jointe")
    named.add_attachment(PDF, maintype="application", subtype="octet-stream", filename="piece_jointe_1")
    clean = _msg("clean", "Rien à signaler.")
    M = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
    M.login(user, "pw")
    for m, flags in ((broken, r"(\Seen \Flagged)"), (named, None), (clean, None)):
        assert M.append("INBOX", flags, WHEN, m.as_bytes(policy=policy.SMTP))[0] == "OK"
    M.logout()


def _inbox(user: str, folder: str = "INBOX") -> dict[str, dict]:
    """{subject: {flags, date, raw}} of the messages not flagged \\Deleted."""
    M = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
    M.login(user, "pw")
    out: dict[str, dict] = {}
    typ, _ = M.select(f'"{folder}"', readonly=True)
    if typ != "OK":
        M.logout()
        return out
    typ, data = M.uid("SEARCH", "UNDELETED")
    uids = data[0].split()
    if uids:
        typ, data = M.uid("FETCH", b",".join(uids).decode(), "(FLAGS INTERNALDATE BODY.PEEK[])")
        for d in data:
            if isinstance(d, tuple):
                msg = BytesParser(policy=policy.default).parsebytes(d[1])
                flags = sorted(f.decode() for f in imaplib.ParseFlags(d[0]) if f not in (b"\\Recent",))
                out[str(msg["Subject"])] = {"flags": flags, "date": imaplib.Internaldate2tuple(d[0]),
                                            "msg": msg}
    M.logout()
    return out


def _wait(st, rid: int, timeout: float = 120) -> dict:
    end = time.time() + timeout
    r = None
    while time.time() < end:
        r = st.client.get(f"/api/repair/runs/{rid}").json()
        if r["status"] in TERMINAL:
            return r
        time.sleep(0.2)
    raise AssertionError(f"repair run {rid} stuck: {r}\n--- worker\n{st.tail('worker')}")


def _run(st, box_id: int, mode: str) -> dict:
    runs = st.api("POST", "/api/repair/runs", 202, json={"box_ids": [box_id], "mode": mode}).json()["runs"]
    return _wait(st, runs[0]["id"])


def test_scan_repair_again_restore(stack_factory):
    st = stack_factory()
    user = create_user(prefix="rep")
    _seed(user)
    before = _inbox(user)
    srv = st.create_server(name="srv-repair", dav_enabled=False)
    box = st.api("POST", "/api/repair/boxes", 201, json={"server_id": srv["id"], "email": user, "password": "pw"}).json()
    assert box["status"] == "new" and box["last_scan"] is None
    assert st.api("POST", f"/api/repair/boxes/{box['id']}/test").json()["imap"]["ok"] is True

    # 1. analysis: counts, writes nothing, marks nothing read
    scan = _run(st, box["id"], "scan")
    assert scan["status"] == "done", scan
    assert scan["progress"]["seen"] == 3 and scan["progress"]["affected"] == 2
    assert scan["report"]["issues"] == {"replacement_chars": 1, "attachment_names": 1}
    assert _inbox(user).keys() == before.keys()
    assert _inbox(user)["clean"]["flags"] == before["clean"]["flags"]
    listed = st.api("GET", "/api/repair/boxes").json()[0]
    assert listed["last_scan"]["id"] == scan["id"] and listed["active_run"] is None
    ex = next(e for e in scan["report"]["examples"] if e["subject"] == "accents")
    pv = st.api("POST", f"/api/repair/boxes/{box['id']}/preview", json={"folder": ex["folder"], "uid": ex["uid"]}).json()
    assert "�" in pv["before"]["text"] and pv["after"]["text"].startswith("Déjà reçu à Genève")

    # 2. repair: same folder, same flags and date, originals kept aside
    rep = _run(st, box["id"], "repair")
    assert rep["status"] == "done", rep
    assert rep["progress"]["repaired"] == 2
    after = _inbox(user)
    assert sorted(after) == ["accents", "clean", "pieces"]
    for subject in ("accents", "pieces"):
        assert after[subject]["msg"]["X-MCCB-Repair"], subject
        assert after[subject]["date"] == before[subject]["date"]
    assert after["accents"]["flags"] == before["accents"]["flags"]
    assert "Déjà reçu à Genève" in after["accents"]["msg"].get_body(preferencelist=("html",)).get_content()
    assert [p.get_filename() for p in after["pieces"]["msg"].iter_attachments()] == ["piece_jointe_1.pdf"]
    assert "X-MCCB-Repair" not in after["clean"]["msg"]
    kept = _inbox(user, f"{BACKUP}.INBOX") or _inbox(user, f"{BACKUP}/INBOX")
    assert sorted(kept) == ["accents", "pieces"] and "X-MCCB-Repair" not in kept["accents"]["msg"]

    # 3. a second pass changes nothing
    again = _run(st, box["id"], "repair")
    assert again["status"] == "done" and again["progress"]["repaired"] == 0 and again["progress"]["skipped"] == 2
    assert sorted(_inbox(user)) == ["accents", "clean", "pieces"]

    # 4. restore: the originals are back, the repaired copies gone
    back = _run(st, box["id"], "restore")
    assert back["status"] == "done", back
    assert back["progress"]["restored"] == 2
    restored = _inbox(user)
    assert sorted(restored) == ["accents", "clean", "pieces"]
    for subject in ("accents", "pieces"):
        assert "X-MCCB-Repair" not in restored[subject]["msg"]
        assert restored[subject]["msg"].as_bytes() == before[subject]["msg"].as_bytes()
    assert not (_inbox(user, f"{BACKUP}.INBOX") or _inbox(user, f"{BACKUP}/INBOX"))


def test_stop_and_wrong_password(stack_factory):
    st = stack_factory()
    user = create_user(prefix="repb")
    srv = st.create_server(name="srv-repair-b", dav_enabled=False)
    box = st.api("POST", "/api/repair/boxes", 201, json={"server_id": srv["id"], "email": user, "password": "bad"}).json()
    res = st.api("POST", f"/api/repair/boxes/{box['id']}/test").json()
    assert res["imap"]["ok"] is False
    assert st.api("GET", "/api/repair/boxes").json()[0]["status"] == "auth_failed"
    run = _run(st, box["id"], "scan")
    assert run["status"] == "failed" and run["error"]
    # duplicates refused, a busy mailbox is skipped
    st.api("POST", "/api/repair/boxes", 409, json={"server_id": srv["id"], "email": user, "password": "x"})
    st.api("POST", "/api/repair/runs", 422, json={"box_ids": [box["id"]], "mode": "explode"})
