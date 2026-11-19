"""Mirror mode, Google source: complete listings (calendar and contacts only when Google ended them with a sync token, mail
by a full scan of All Mail), versions from etags, absence confirmed by reading the item, and nothing ever written."""
import re

import pytest

from app.connectors import google
from app.connectors.base import CAP_MIRROR, ConnectorError
from tests.server.test_google import SA_KEY, FakeGmailImap, GoogleFake, cfg, events, people, rec


class MirrorGoogle(GoogleFake):
    """GoogleFake + reading one event / one person by id, and a record of every HTTP method received."""

    def __init__(self):
        super().__init__()
        self.methods: list[str] = []
        self.fail_get = 0

    def handle(self, req):
        if req.path != "/token":
            self.methods.append(req.method)
        ev = re.match(r"^/calendar/v3/calendars/([^/]+)/events/([^/]+)$", req.path)
        pe = re.match(r"^/v1/people/(c\d+)$", req.path)
        if ev or pe:
            if not self.token_ok(req):
                return 401, {}, {"error": {"code": 401, "message": "Invalid Credentials", "status": "UNAUTHENTICATED"}}
            if self.fail_get:
                return self.fail_get, {}, {"error": {"code": self.fail_get, "message": "boom"}}
            if ev:
                cal, eid = ev.group(1).replace("%40", "@"), ev.group(2)
                found = next((e for e in self.events.get(cal, []) if e["id"] == eid and not e.get("_hidden")), None)
            else:
                found = next((p for p in self.people if p["resourceName"] == "people/" + pe.group(1)), None)
            return (200, {}, found) if found else (404, {}, {"error": {"code": 404, "message": "Not Found"}})
        return super().handle(req)


@pytest.fixture
def gg(monkeypatch):
    g = MirrorGoogle()
    monkeypatch.setattr(google, "TOKEN_URL", g.http.url + "/token")
    monkeypatch.setattr(google, "CALENDAR_API", g.http.url + "/calendar/v3")
    monkeypatch.setattr(google, "PEOPLE_API", g.http.url + "/v1")
    yield g
    g.close()


def opened(**extra):
    src = google.GoogleSource(cfg(**extra), {"service_account_key": SA_KEY})
    src.open()
    return src


def with_etags(evs, n=1):
    for e in evs:
        e["etag"] = f'"{e["id"]}-{n}"'
    return evs


def test_catalogue_declares_the_mirror_for_the_source_only():
    from app.connectors import registry
    cat = {c["kind"]: c for c in registry.catalogue()}
    assert CAP_MIRROR in cat["google"]["source"]["capabilities"] and cat["google"]["target"] is None


# ------------------------------------------------------------------ calendar
def test_calendar_listing_versions_and_removals(gg):
    gg.events["primary"] = with_etags(events())
    src = opened(sync_contacts=False)
    cal = src.folders()[0]
    first = src.mirror_listing(cal)
    assert first.complete and first.problem is None and len(first.items) == 3     # cancelled ev4 and the birthday are not there
    by = {i.alt_key: i for i in first.items}
    series = by["u:" + google._digest("ev1@google.com")]
    assert series.version and len({i.version for i in first.items}) == 3
    # an exception of the series changes: the version of the whole series changes, the others stay
    next(e for e in gg.events["primary"] if e["id"].startswith("ev1_2026011")).update(etag='"ev1-2"', summary="Moved again")
    second = src.mirror_listing(cal)
    by2 = {i.alt_key: i for i in second.items}
    assert by2[series.alt_key].version != series.version
    assert {k: v.version for k, v in by2.items() if k != series.alt_key} == {k: v.version for k, v in by.items() if k != series.alt_key}
    # an event deleted at Google (tombstone with status cancelled) is no longer listed
    next(e for e in gg.events["primary"] if e["id"] == "ev3")["status"] = "cancelled"
    third = src.mirror_listing(cal)
    assert third.complete and "u:" + google._digest("ev3@google.com") not in {i.alt_key for i in third.items}
    assert [r.query.get("showDeleted") for r in gg.http.requests if r.path.endswith("/events")][0] == "true"


def test_calendar_listing_without_sync_token_is_not_complete(gg):
    gg.events["primary"] = events()
    gg.calendar_sync = None                    # Google only gives it on the last page: without it the end was not reached
    lst = opened(sync_contacts=False).mirror_listing(opened(sync_contacts=False).folders()[0])
    assert not lst.complete and "sync token" in lst.problem and lst.items


def test_calendar_absence_is_read_by_id(gg):
    gg.events["primary"] = events()
    src = opened(sync_contacts=False)
    cal = src.folders()[0]
    keys = {i.alt_key: i.key for i in src.mirror_listing(cal).items}
    live = keys["u:" + google._digest("ev3@google.com")]
    gone = google.key_of(f"c{google._slug('primary')}-", "ev9")           # never existed / purged
    cancelled = google.key_of(f"c{google._slug('primary')}-", "ev4")       # tombstone
    hashed = google.key_of(f"c{google._slug('primary')}-", "x" * 300)
    got = src.exists(cal, [live, gone, cancelled, hashed])
    assert got == {live: True, gone: False, cancelled: False, hashed: True}   # an unreadable key is never "gone"
    gg.fail_get = 500                                                          # an error is not an absence
    with pytest.raises(ConnectorError) as e:
        src.exists(cal, [live])
    assert e.value.kind == ConnectorError.TRANSIENT
    gg.fail_get = 403
    with pytest.raises(ConnectorError):
        src.exists(cal, [live])


# ------------------------------------------------------------------ contacts
def test_people_listing_versions_and_absence(gg):
    gg.people = people()
    for p, tag in zip(gg.people, "abc"):
        p["etag"] = tag
    src = opened(sync_calendar=False)
    book = src.folders()[0]
    lst = src.mirror_listing(book)
    assert lst.complete and [i.key for i in lst.items] == ["pc101", "pc102", "pc103"]
    assert [i.version for i in lst.items] == ["a", "b", "c"]
    gg.people[0]["etag"] = "a2"
    assert src.mirror_listing(book).items[0].version == "a2"
    gg.people = gg.people[1:]                                                  # c101 deleted
    lst2 = src.mirror_listing(book)
    assert [i.key for i in lst2.items] == ["pc102", "pc103"]
    assert src.exists(book, ["pc101", "pc102"]) == {"pc101": False, "pc102": True}
    gg.people[0]["metadata"] = {"deleted": True}                               # still readable but flagged deleted
    assert src.exists(book, ["pc102"]) == {"pc102": False}
    gg.fail_get = 500
    with pytest.raises(ConnectorError):
        src.exists(book, ["pc103"])


def test_people_listing_without_sync_token_is_not_complete(gg):
    gg.people = people()
    gg.people_sync = None
    src = opened(sync_calendar=False)
    assert not src.mirror_listing(src.folders()[0]).complete


# ------------------------------------------------------------------ mail
class MirrorImap(FakeGmailImap):
    """FakeGmailImap that says how many UIDs the search listed and the fetch did not return, and can search by X-GM-MSGID."""
    instances: list = []
    missing = [0]          # popped one per scan: the last value stays
    reported = True

    def scan(self, name, after_uid=0):
        if self.reported:
            self.last_scan_missing = self.missing.pop(0) if len(self.missing) > 1 else self.missing[0]
        return super().scan(name, after_uid)

    def has_message(self, name, msgid):
        data = {"[Gmail]/All Mail": self.all_mail, "[Gmail]/Spam": self.spam, "[Gmail]/Trash": self.trash}[name]
        return any(r["msgid"] == msgid for r in data)


def mail_source(**extra):
    MirrorImap.instances.clear()
    MirrorImap.missing, MirrorImap.reported = [0], True
    src = google.GoogleSource(cfg(sync_mail=True, sync_calendar=False, sync_contacts=False, **extra),
                              {"service_account_key": SA_KEY}, {"imap": MirrorImap})
    src.open()
    return src


@pytest.fixture
def mail(gg, monkeypatch):
    monkeypatch.setattr(MirrorImap, "all_mail", list(FakeGmailImap.all_mail))
    monkeypatch.setattr(MirrorImap, "trash", list(FakeGmailImap.trash))
    monkeypatch.setattr(MirrorImap, "spam", list(FakeGmailImap.spam))


def test_mail_listing_is_a_full_scan_and_removal_shows(gg, mail):
    src = mail_source()
    folders = {tuple(f.path): f for f in src.folders()}
    inbox = folders[("INBOX",)]
    lst = src.mirror_listing(inbox)
    assert lst.complete and sorted(i.key for i in lst.items) == ["g100", "g106"]
    assert next(i for i in lst.items if i.key == "g100").flags == ["\\Seen"] and all(i.version is None for i in lst.items)
    # the mail was deleted at Gmail: the next full scan does not have it, and the search confirms it is gone
    MirrorImap.all_mail = [r for r in MirrorImap.all_mail if r["msgid"] != "100"]
    lst2 = src.mirror_listing(inbox)                    # a scan from UID 0 every time, never the incremental cursor
    assert lst2.complete and [i.key for i in lst2.items] == ["g106"]
    assert src.exists(inbox, ["g100", "g106"]) == {"g100": False, "g106": True}
    assert src.exists(inbox, ["gnotanumber"]) == {"gnotanumber": True}


def test_mail_in_the_trash_counts_as_present_only_when_the_trash_is_included(gg, mail):
    src = mail_source(include_trash_spam=True)
    assert src.exists(src.folders()[0], ["g901", "g900", "g555"]) == {"g901": True, "g900": True, "g555": False}
    src = mail_source()
    assert src.exists(src.folders()[0], ["g901"]) == {"g901": False}       # not mirrored: the copy goes away with it


def test_mail_listing_that_lost_messages_is_read_again_then_given_up(gg, mail):
    src = mail_source()
    inbox = next(f for f in src.folders() if f.path == ["INBOX"])
    MirrorImap.missing = [2, 0]                          # first scan lost two messages, the second is clean
    assert src.mirror_listing(inbox).complete
    MirrorImap.missing = [1]                             # always
    lst = src.mirror_listing(inbox)
    assert not lst.complete and "could not be read" in lst.problem
    src = mail_source()
    MirrorImap.reported = False                          # a connection that cannot tell: not trusted
    lst = src.mirror_listing(next(f for f in src.folders() if f.path == ["INBOX"]))
    assert not lst.complete and "cannot tell" in lst.problem


# ------------------------------------------------------------------ the source is never written
class StrictImap(MirrorImap):
    """Any call that is not a read raises."""

    def _no(self, *a, **k):
        raise AssertionError("write on the source")
    store = expunge = append = delete = copy = move = set_flags = create = rename = _no


def test_the_source_never_writes(gg, mail):
    gg.events["primary"] = with_etags(events())
    gg.people = people()
    StrictImap.missing, StrictImap.reported = [0], True
    src = google.GoogleSource(cfg(sync_mail=True), {"service_account_key": SA_KEY}, {"imap": StrictImap})
    src.open()
    for f in src.folders():
        if f.skip:
            continue
        lst = src.mirror_listing(f)
        assert lst.complete
        src.exists(f, [i.key for i in lst.items][:2] + ["gx1" if f.kind == "mail" else "zz"])
        for it in lst.items[:1]:
            src.fetch(f, it, "<x>")
        src.changed_since(f, None)
    assert set(gg.methods) == {"GET"}                    # Calendar and People: reads only (and read-only scopes)
    assert all("readonly" in s for s in google.SCOPES.values() if "calendar" in s or "contacts" in s)
