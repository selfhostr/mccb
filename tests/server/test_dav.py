import os

import pytest

from tests.svcusers import create_user

BASE = os.environ.get("TEST_DAV_URL")
if not BASE:
    pytest.skip("TEST_DAV_URL not set", allow_module_level=True)

from app.server.dav import DavClient, DavError, expand_template  # noqa: E402

ICS = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VEVENT\r\n"
       "UID:ev1@test\r\nDTSTAMP:20240101T000000Z\r\nDTSTART:20240102T100000Z\r\n"
       "SUMMARY:Réunion\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
VCF = ("BEGIN:VCARD\r\nVERSION:3.0\r\nUID:c1@test\r\nFN:Jean Dupont\r\n"
       "N:Dupont;Jean;;;\r\nEND:VCARD\r\n")


@pytest.fixture
def email():
    return create_user()


@pytest.fixture
def client(email):
    with DavClient(email, "pw") as c:
        yield c


def test_expand_template():
    assert expand_template("https://x/{domain}/{user}/{email}/{login}",
                           "bob@ex.org", "L") == "https://x/ex.org/bob/bob@ex.org/L"


@pytest.mark.parametrize("kind,name,content", [
    ("caldav", "ev1.ics", ICS), ("carddav", "c1.vcf", VCF)])
def test_discover_put_count(client, email, kind, name, content):
    url = client.discover(kind, BASE, email, "unused.invalid")
    assert url.endswith("/")
    assert "import-pst" in url
    assert client.discover(kind, BASE, email, "unused.invalid") == url
    assert client.count(url) == 0
    assert not client.exists(url, name)
    assert client.put(url, name, content, kind) in (200, 201, 204)
    assert client.put(url, name, content, kind) in (200, 201, 204)
    assert client.count(url) == 1
    assert client.exists(url, name)
    assert "Dupont" in client.get(url, name) or "Réunion" in client.get(url, name)
    assert client.get(url, "absent.x") is None


def test_discover_no_create(client, email):
    with pytest.raises(DavError):
        client.discover("caldav", BASE, email, "x", create=False)


def test_test_method(client, email):
    ok, msg = client.test("caldav", BASE, email, "x")
    assert ok and "will be created" in msg
    client.discover("caldav", BASE, email, "x")
    ok, msg = client.test("caldav", BASE, email, "x")
    assert ok and "import-pst" in msg


def test_unreachable(email):
    with DavClient(email, "pw", timeout=3) as c:
        with pytest.raises(DavError) as ei:
            c.discover("caldav", "http://127.0.0.1:1/", email, "x")
        assert ei.value.temporary
        ok, msg = c.test("caldav", "http://127.0.0.1:1/", email, "x")
        assert not ok and msg


@pytest.mark.parametrize("kind,slug,name,content", [
    ("caldav", "agenda-perso", "ev1.ics", ICS), ("carddav", "carnet-perso", "c1.vcf", VCF)])
def test_template_url_404_creates_collection(client, email, kind, slug, name, content):
    """Target URL configured but nonexistent (404): "PST Import" collection created at that address."""
    tmpl = BASE.rstrip("/") + "/{email}/" + slug + "/"
    url = expand_template(tmpl, email, email)
    ok, msg = client.test(kind, tmpl, email, "x")
    assert ok and "will be created" in msg, msg
    from urllib.parse import unquote
    got = client.discover(kind, tmpl, email, "x")
    assert unquote(got) == url
    assert client.discover(kind, tmpl, email, "x") == got  # 2nd time: found as is, not recreated
    assert client.put(got, name, content, kind) in (200, 201, 204)
    assert client.count(got) == 1


def test_ensure_collection_recreates_deleted(client, email):
    url = client.discover("carddav", BASE, email, "x")
    client._request("DELETE", url)
    assert client.ensure_collection("carddav", url) == url
    assert client.put(url, "c1.vcf", VCF, "carddav") in (200, 201, 204)
    assert client.ensure_collection("carddav", url) == url  # exists: nothing to do


def test_creation_refused_clear_error():
    import httpx

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "PROPFIND":
            return httpx.Response(404)
        return httpx.Response(403)  # MKCALENDAR / MKCOL refused

    with DavClient("u", "p", transport=httpx.MockTransport(handler)) as c:
        for kind, what in (("caldav", "the calendar"), ("carddav", "the address book")):
            with pytest.raises(DavError) as ei:
                c.discover(kind, "https://dav.example/u/x/", "u@example", "x")
            msg = str(ei.value)
            assert f"creation of {what} 'PST Import' refused" in msg and "HTTP 403" in msg, msg
            assert not ei.value.temporary
