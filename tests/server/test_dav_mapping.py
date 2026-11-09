"""Calendars to a CalDAV target through the connector route: one collection per source calendar, resources named
after their UID (a server like MDaemon stores them so whatever the URL of the PUT), one check per target collection,
generated calendars left out, and a final count that includes the verification problems."""
import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import httpx
import pytest

from app.connectors import dav as cdav
from app.connectors import m365, registry
from app.connectors.base import CALENDAR, FetchedItem, Source, SourceFolder, SourceItem
from tests.server.test_connectors import log_of, make_account, run_job, start

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
HOME = "/dav/u/"


def ics(uid: str, summary: str) -> bytes:
    return (f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VEVENT\r\nUID:{uid}\r\n"
            f"DTSTAMP:20260301T120000Z\r\nDTSTART:20260301T120000Z\r\nSUMMARY:{summary}\r\nEND:VEVENT\r\n"
            "END:VCALENDAR\r\n").encode()


class FakeMDaemon:
    """CalDAV home with collections; a PUT is stored under "<UID>.ics", not under the name of its URL (MDaemon)."""

    def __init__(self, refuse_mkcalendar: bool = False, drop: bool = False):
        self.lock = threading.Lock()
        self.cols: dict[str, dict] = {HOME + "b64=Q2FsZW5kYXI/": {"name": "Calendar", "items": {}},
                                      HOME + "b64=VHJhdmFpbA/": {"name": "Travail", "items": {}},
                                      HOME + "b64=VGFza3M/": {"name": "Tasks", "items": {}, "comp": "VTODO"}}
        self.refuse = refuse_mkcalendar
        self.drop = drop
        self.puts: list[str] = []
        self.broken: set[str] = set()  # listed by the home, "Invalid Path" when opened (seen on a real MDaemon)
        self.methods: list[tuple[str, str]] = []
        self.mkcalendar_names: list[str] = []

    def by_name(self, name: str) -> dict:
        return next(c["items"] for c in self.cols.values() if c["name"] == name)

    def _ms(self, rows: list[str]) -> httpx.Response:
        body = ('<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">' + "".join(rows)
                + "</d:multistatus>")
        return httpx.Response(207, text=body)

    @staticmethod
    def _row(href: str, props: str) -> str:
        return (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>"
                "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")

    def _col_props(self, path: str) -> str:
        c = self.cols[path]
        return (f'<d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>{c["name"]}</d:displayname>'
                f'<c:supported-calendar-component-set><c:comp name="{c.get("comp", "VEVENT")}"/>'
                "</c:supported-calendar-component-set>")

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = unquote(req.url.path)
        with self.lock:
            self.methods.append((req.method, path))
            if path in self.broken or path.rsplit("/", 1)[0] + "/" in self.broken:
                return httpx.Response(404, text="<d:error xmlns:d='DAV:'><m>Invalid Path</m></d:error>")
            if req.method == "PROPFIND":
                depth = req.headers.get("depth")
                if path == HOME:
                    rows = [self._row(HOME, "<d:resourcetype><d:collection/></d:resourcetype>"
                                            f"<c:calendar-home-set><d:href>{HOME}</d:href></c:calendar-home-set>")]
                    if depth == "1":
                        rows += [self._row(p, self._col_props(p)) for p in self.cols]
                    return self._ms(rows)
                if path in self.cols:
                    rows = [self._row(path, self._col_props(path))]
                    if depth == "1":
                        rows += [self._row(path + n, "<d:resourcetype/>") for n in self.cols[path]["items"]]
                    return self._ms(rows)
                return httpx.Response(404)
            if req.method == "MKCALENDAR":
                if self.refuse:
                    return httpx.Response(403)
                name = ET.fromstring(req.content).find(".//{DAV:}displayname").text
                self.mkcalendar_names.append(name)
                if path in self.cols:
                    return httpx.Response(405)
                self.cols[path] = {"name": name, "items": {}}
                return httpx.Response(201)
            folder, _, name = path.rpartition("/")
            col = self.cols.get(folder + "/")
            if req.method == "PUT":
                if col is None:
                    return httpx.Response(409)
                self.puts.append(path)
                uid = re.search(rb"^UID:(.*?)\r?$", req.content, re.M).group(1).decode()
                if not self.drop:
                    col["items"][f"{uid}.ics"] = req.content
                return httpx.Response(204)
            if req.method == "GET":
                body = col["items"].get(name) if col else None
                return httpx.Response(200, content=body) if body is not None else httpx.Response(404)
        return httpx.Response(405)


CALS: dict[str, list[tuple[str, str]]] = {}


class CalSource(Source):
    def __init__(self, config, secrets, ctx=None):
        pass

    def open(self):
        pass

    def close(self):
        pass

    def folders(self):
        out = []
        for name, events in CALS.items():
            out.append(SourceFolder(path=[name], kind=CALENDAR, count=len(events), ref=name,
                                    special="default" if name == "Calendrier" else None,
                                    skip="read-only calendar generated by Outlook" if name == "Anniversaires" else None))
        return out

    def iter_items(self, folder):
        for i, (uid, _s) in enumerate(CALS[folder.ref]):
            yield SourceItem(key=f"{folder.ref}-{i}", date=T0 + timedelta(hours=i), ref=i)

    def fetch(self, folder, item, import_id):
        uid, summary = CALS[folder.ref][item.ref]
        return FetchedItem(raw=ics(uid, summary), subject=summary)


@pytest.fixture
def mdaemon(monkeypatch):
    from app.server import dav as sdav
    fake = FakeMDaemon()
    real = sdav.httpx.Client
    monkeypatch.setattr(sdav.httpx, "Client", lambda **kw: real(**{**kw, "transport": httpx.MockTransport(fake)}))
    field = registry.Field("name", "Name", required=True)
    registry.register(registry.ConnectorSpec(
        kind="memcal", label="Calendars", source=registry.Role(CalSource, (CALENDAR,), frozenset(), (field,),
                                                                lambda c: c["name"])))
    CALS.clear()
    CALS.update({"Calendrier": [("040000008200E00074C5B7101A82E0080000000001", "Meeting")],
                 "Jours fériés - France": [(f"hol-{i}", f"Holiday {i}") for i in range(3)],
                 "Travail": [("work-1", "Work 1"), ("work-2", "Work 2")],
                 "Anniversaires": [("bday-1", "Birthday")]})
    yield fake
    registry.unregister("memcal")
    CALS.clear()


def cal_account(client):
    aid = make_account(client)
    r = client.put(f"/api/accounts/{aid}/source", json={"kind": "memcal", "config": {"name": "s"}})
    assert r.status_code == 200, r.text
    r = client.put(f"/api/accounts/{aid}/target", json={
        "kind": "caldav", "config": {"url": f"https://dav.test{HOME}", "login": "u"}, "secrets": {"password": "p"}})
    assert r.status_code == 200, r.text
    from app.server import db
    db.ex("UPDATE accounts SET opt_mail=0, opt_calendar=1, opt_contacts=0 WHERE id=?", (aid,))
    return aid


def test_each_calendar_gets_its_collection_and_is_counted(client, mdaemon):
    jid = start(client, cal_account(client))
    j = run_job(client, jid)
    log = log_of(client, jid)
    assert j["status"] == "done", log
    # default to the default collection, same name to the existing one, others to a new collection
    assert set(mdaemon.by_name("Calendar")) == {"040000008200E00074C5B7101A82E0080000000001.ics"}
    assert set(mdaemon.by_name("Travail")) == {"work-1.ics", "work-2.ics"}
    assert set(mdaemon.by_name("Jours fériés - France")) == {f"hol-{i}.ics" for i in range(3)}
    created = next(p for p, c in mdaemon.cols.items() if c["name"] == "Jours fériés - France")
    assert created.startswith(HOME + "mccb-jours-feries-france-")
    # the generated calendar is left out, with a line saying so
    assert "bday-1.ics" not in str(mdaemon.cols) and "'Anniversaires' (calendar) not migrated" in log
    # resources are PUT under the name the server keeps: the check finds them all
    assert all(p.rsplit("/", 1)[1] in {f"{u}.ics" for evs in CALS.values() for u, _ in evs} for p in mdaemon.puts)
    assert "present <" not in log and "Finished: done (0 error(s))" in log
    rep = client.get(f"/api/jobs/{jid}/report").json()
    assert rep["dav"]["calendar"]["sent"] == 6 and rep["dav"]["calendar"]["present"] == 6
    assert len(rep["dav"]["calendar"]["collections"]) == 3


def test_refused_creation_falls_back_to_the_default_collection(client, mdaemon):
    mdaemon.refuse = True
    jid = start(client, cal_account(client))
    j = run_job(client, jid)
    log = log_of(client, jid)
    assert j["status"] == "done", log
    assert "refused to create 'Jours fériés - France'" in log and "b64=Q2FsZW5kYXI" in log
    assert len(mdaemon.by_name("Calendar")) == 4 and len(mdaemon.by_name("Travail")) == 2
    rep = client.get(f"/api/jobs/{jid}/report").json()
    default = next(c for c in rep["dav"]["calendar"]["collections"] if "Q2FsZW5kYXI" in c["url"])
    # two source calendars share it: counted once, for both, with their own totals
    assert default["sent"] == 4 and default["present"] == 4
    assert sorted(default["folders"]) == ["Calendrier", "Jours fériés - France"]


def test_missing_items_count_in_the_final_summary(client, mdaemon):
    mdaemon.drop = True
    jid = start(client, cal_account(client))
    j = run_job(client, jid)
    log = log_of(client, jid)
    assert j["status"] == "done_with_errors", log
    # one line per target collection, with that collection's own totals (not the phase's total per folder)
    assert "Folder check 'Travail' -> https://dav.test/dav/u/b64=VHJhdmFpbA/: 0 present < 2 sent" in log
    assert "< 6 sent" not in log
    assert re.search(r"Finished: done_with_errors \(\d+ error\(s\): 0 item error\(s\), [1-9]\d* verification "
                     r"problem\(s\)\)", log), log


def test_uid_names():
    assert cdav.uid_name(ics("abc-1@x.test", "s"), "ics") == "abc-1@x.test.ics"
    folded = b"BEGIN:VEVENT\r\nUID:\r\n 0400000082\r\n 00E0\r\nEND:VEVENT\r\n"
    assert cdav.uid_name(folded, "ics") == "040000008200E0.ics"
    assert cdav.uid_name(b"BEGIN:VEVENT\r\nUID:a/b c\r\nEND:VEVENT\r\n", "ics") is None
    assert cdav.uid_name(b"BEGIN:VEVENT\r\nEND:VEVENT\r\n", "ics") is None
    target = registry.make_target("caldav", {"url": "https://d.test/", "login": "u"}, {"password": "p"})
    assert target.resource_name(b"no uid", "d1.ics") == "d1.ics"
    assert target.resource_name(ics("x1", "s"), "d1.ics") == "x1.ics"
    assert cdav.collection_slug("caldav", "VEVENT", "Jours fériés") == cdav.collection_slug("caldav", "VEVENT",
                                                                                           "Jours fériés")
    assert cdav.collection_slug("caldav", "VEVENT", "Work") != cdav.collection_slug("carddav", "VEVENT", "Work")


def test_graph_generated_calendars_are_skipped():
    # as Graph lists them for a French mailbox
    base = {"groupClassId": "0006f0b7-0000-0000-c000-000000000046", "isShared": False, "isSharedWithMe": False}
    assert m365.generated_calendar({**base, "name": "Calendrier", "isDefaultCalendar": True, "canEdit": True}) is None
    for name in ("Jours fériés - France", "Anniversaires"):
        assert m365.generated_calendar({**base, "name": name, "isDefaultCalendar": False, "canEdit": False,
                                        "isRemovable": True})
    assert m365.generated_calendar({**base, "name": "Mine", "isDefaultCalendar": False, "canEdit": True}) is None
    assert m365.generated_calendar({**base, "isSharedWithMe": True, "isDefaultCalendar": False,
                                    "canEdit": False}) is None


def test_items_of_a_calendar_now_skipped_leave_the_checks(client, mdaemon):
    """A first run sent a generated calendar; the next one leaves it out and no longer checks its items."""
    from app.server import db
    aid = cal_account(client)
    jid = start(client, aid)
    j = run_job(client, jid)
    assert j["status"] == "done"
    # as if the first run had sent 'Anniversaires' under names the server did not keep
    cols = [r[1] for r in db.q("PRAGMA table_info(items)") if r[1] != "id"]
    sel = ", ".join({"item_key": "'old-1'", "folder": "'Anniversaires'", "hash": "'gone.ics'"}.get(c, c) for c in cols)
    db.ex(f"INSERT INTO items({', '.join(cols)}) SELECT {sel} FROM items WHERE account_id=? AND status='ok' LIMIT 1",
          (aid,))
    assert client.post(f"/api/jobs/{jid}/retry").status_code == 200
    j = run_job(client, jid)
    assert j["status"] == "done", log_of(client, jid)
    assert db.q1("SELECT status FROM items WHERE item_key='old-1'")["status"] == "skipped"


def test_listed_collection_that_will_not_open_is_never_created_again(client, mdaemon):
    """Real MDaemon: the home lists 'Calendar' (b64=Q2FsZW5kYXI/) but PROPFIND, PUT and MKCALENDAR on it answer 404.
    No MKCALENDAR over it (named 'PST Import'), a clear folder error, and a run that does not end 'done'."""
    from app.server import db
    broken = HOME + "b64=Q2FsZW5kYXI/"
    mdaemon.broken.add(broken)
    CALS["Calendrier"] = []  # the default calendar of the source is empty, as in the real case
    aid = cal_account(client)
    jid = start(client, aid)
    j = run_job(client, jid)
    log = log_of(client, jid)
    assert j["status"] == "done_with_errors", log
    assert ("MKCALENDAR", broken) not in mdaemon.methods and "PST Import" not in log + str(mdaemon.mkcalendar_names)
    assert "lists the calendar 'Calendar'" in log and "refuses to open it (PROPFIND -> HTTP 404)" in log
    assert "Finished: done_with_errors (1 error(s): 0 item error(s), 1 folder error(s))" in log
    assert "Calendrier" in (j.get("error") or "")
    # the other calendars still go to their collections
    assert set(mdaemon.by_name("Travail")) == {"work-1.ics", "work-2.ics"}
    # "retry errors": the folder is tried again and still reported, even without any item in error
    assert client.post(f"/api/jobs/{jid}/retry-errors").status_code == 200
    j = run_job(client, jid)
    log = log_of(client, jid)
    assert j["status"] == "done_with_errors" and log.count("refuses to open it") == 2, log
    assert ("MKCALENDAR", broken) not in mdaemon.methods
    assert db.q1("SELECT mode FROM jobs WHERE id=?", (jid,))["mode"] == "errors"


def test_collection_gone_since_the_plan_is_recreated_under_its_name(client, mdaemon):
    """A collection created for a source calendar, then deleted: recreated under that calendar's name."""
    from app.connectors.base import OutgoingItem
    target = registry.make_target("caldav", {"url": f"https://dav.test{HOME}", "login": "u"}, {"password": "p"})
    session = target.session()
    session.connect()
    plan = session.plan_folders([SourceFolder(["Calendrier"], CALENDAR, special="default", ref="c"),
                                 SourceFolder(["Projets"], CALENDAR, ref="p")])
    url = plan["Projets"].name
    session.ensure_folder(url, label="Projets")
    path = url.split("dav.test", 1)[1]
    del mdaemon.cols[path]
    other = target.session()
    other.connect()
    other.ensure_folder(url)  # the runner after a 404 on PUT: no label at hand
    assert mdaemon.cols[path]["name"] == "Projets" and "PST Import" not in mdaemon.mkcalendar_names
    other.put(url, OutgoingItem(CALENDAR, ics("p-1", "P"), "<x>", name="p-1.ics"))
    assert set(mdaemon.by_name("Projets")) == {"p-1.ics"}
