"""Duplicates tab against real servers (GreenMail, Radicale): the analysis writes nothing, the removal deletes only
the copies (one of each kept), a second analysis finds nothing."""
from __future__ import annotations

import imaplib
import time

from app.server.dav import DavClient
from tests.e2e.helpers import DAV_URL, IMAP_HOST, IMAP_PORT, TERMINAL, create_user, dav_names
from tests.server.dedup_fakes import ics, mail, vcf


def _imap(user: str) -> imaplib.IMAP4:
    M = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
    M.login(user, "pw")
    return M


def _seed(user: str) -> tuple[str, str]:
    M = _imap(user)
    for raw in (mail("Offre", mid="<o@x>"), mail("Offre", mid="<o@x>"),
                mail("Offre", mid="<o@x>", body="hello, with a footer"),
                mail("Sans id", mid=None), mail("Sans id", mid=None), mail("Seule", mid="<s@x>")):
        assert M.append("INBOX", None, None, raw)[0] == "OK"
    M.logout()
    with DavClient(user, "pw", False) as d:
        cal = d.discover("caldav", DAV_URL, user, IMAP_HOST)
        book = d.discover("carddav", DAV_URL, user, IMAP_HOST)
        for name, text in (("a.ics", ics("e2e-u1")), ("b.ics", ics("e2e-u2")),
                           ("c.ics", ics("e2e-u3", start="20240306T100000", end="20240306T110000"))):
            d.put(cal, name, text, "caldav")
        for name, text in (("z1.vcf", vcf("e2e-c1")), ("z2.vcf", vcf("e2e-c2")), ("y.vcf", vcf("e2e-c3", fn="Yann"))):
            d.put(book, name, text, "carddav")
    return cal, book


def _subjects(user: str) -> list[tuple[str, int]]:
    M = _imap(user)
    M.select("INBOX", readonly=True)
    typ, data = M.uid("SEARCH", "ALL")
    out = []
    for uid in data[0].split():
        typ, d = M.uid("FETCH", uid.decode(), "(RFC822.SIZE FLAGS BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
        head = d[0][0].decode()
        assert "\\Seen" not in head, "the duplicate search must not mark anything read"
        out.append((d[0][1].decode().split(":", 1)[1].strip(), int(head.split("RFC822.SIZE ")[1].split()[0])))
    M.logout()
    return sorted(out)


def _run(st, key: str, mode: str) -> dict:
    body = {"box_keys": [key], "mode": mode, "confirm": mode == "dedup"}
    rid = st.api("POST", "/api/dedup/runs", 202, json=body).json()["runs"][0]["id"]
    end = time.time() + 120
    while time.time() < end:
        r = st.client.get(f"/api/dedup/runs/{rid}").json()
        if r["status"] in TERMINAL:
            return r
        time.sleep(0.2)
    raise AssertionError(f"dedup run {rid} stuck\n--- worker\n{st.tail('worker')}")


def test_analyse_then_remove(stack_factory):
    st = stack_factory()
    user = create_user(prefix="dup")
    cal, book = _seed(user)
    srv = st.create_server(name="srv-dedup")
    acc = st.create_account(srv["id"], user, "pw")
    key = f"account:{acc['id']}"
    assert any(b["key"] == key for b in st.api("GET", "/api/dedup/boxes").json())
    before = _subjects(user)
    names_before = dav_names(cal, user), dav_names(book, user)

    scan = _run(st, key, "scan")
    assert scan["status"] == "done", scan
    t = scan["totals"]
    assert (t["mail"]["groups"], t["mail"]["duplicates"]) == (2, 2), scan["report"]
    assert (t["calendar"]["groups"], t["calendar"]["duplicates"]) == (1, 1), scan["report"]
    assert (t["contacts"]["groups"], t["contacts"]["duplicates"]) == (1, 1), scan["report"]
    assert _subjects(user) == before and (dav_names(cal, user), dav_names(book, user)) == names_before

    done = _run(st, key, "dedup")
    assert done["status"] == "done", done
    assert done["progress"]["deleted"] == 4, done["report"]
    after = _subjects(user)
    assert [s for s, _ in after] == ["Offre", "Offre", "Sans id", "Seule"]
    assert len({size for s, size in after if s == "Offre"}) == 2        # the one with a footer is kept
    assert dav_names(cal, user) == {"a.ics", "c.ics"}
    assert dav_names(book, user) == {"y.vcf", "z1.vcf"}

    again = _run(st, key, "scan")
    assert again["progress"]["duplicates"] == 0, again["report"]
