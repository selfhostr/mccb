"""Tenant directory of the Graph connector: listing, classification and permission checks, against FakeGraph."""
import base64
import json

import httpx
import pytest

from app.connectors import m365, oauth
from app.connectors.base import ConnectorError
from tests.server import fake_exchange as fx

CONFIG = {"tenant_id": "contoso", "client_id": "app-1"}
SECRETS = {"client_secret": fx.FakeGraph.SECRET}
ALL_ROLES = ["User.Read.All", "Mail.Read", "Calendars.Read", "Contacts.Read", "MailboxSettings.Read", "Tasks.Read.All"]


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(m365, "_sleep", lambda s: None)
    f = fx.FakeGraph()
    f.roles = list(ALL_ROLES)
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(f))
    return f


def user(uid, mail="", **kw):
    return {"id": uid, "displayName": uid.upper(), "mail": mail or None, "userPrincipalName": f"{uid}@contoso.test",
            "proxyAddresses": [], "accountEnabled": True, "assignedLicenses": [{"skuId": "x"}], "userType": "Member", **kw}


def listing():
    return m365.GraphDirectory().list_mailboxes(CONFIG, SECRETS)


def by_id(res):
    return {m["id"]: m for m in res["mailboxes"]}


def test_listing_pages_and_fields(fake):
    fake.directory = [user(f"u{i}", f"U{i}@Contoso.test") for i in range(5)]  # page_limit 2: 3 pages
    res = listing()
    assert len(res["mailboxes"]) == 5 and res["purpose_checked"] is True
    first = res["mailboxes"][0]
    assert first == {"id": "u0", "display_name": "U0", "mail": "u0@contoso.test", "upn": "u0@contoso.test",
                     "aliases": [], "kind": "user", "enabled": True, "licensed": True}
    assert sum(1 for r in fake.requests if r.url.path.endswith("/users")) == 3


def test_hidden_guests_no_mail_and_no_mailbox(fake):
    fake.directory = [user("a", "a@contoso.test"), user("g", "g@ext.test", userType="Guest"), user("n"),
                      user("x", "x@contoso.test")]
    fake.purposes = {"x": 404}
    res = listing()
    assert list(by_id(res)) == ["a"]
    assert res["hidden"] == {"guests": 1, "no_mailbox": 2}


def test_classification(fake):
    fake.directory = [user("s", "s@contoso.test"), user("r", "r@contoso.test"), user("e", "e@contoso.test"),
                      user("d", "d@contoso.test", accountEnabled=False), user("l", "l@contoso.test")]
    fake.purposes = {"s": "shared", "r": "room", "e": "equipment", "l": "linked"}
    got = by_id(listing())
    assert [got[i]["kind"] for i in "sreld"] == ["shared", "room", "equipment", "user", "user"]
    assert got["d"]["enabled"] is False


def test_forbidden_settings_is_unknown_with_heuristic(fake):
    fake.directory = [user("a", "a@contoso.test"),
                      user("b", "b@contoso.test", accountEnabled=False, assignedLicenses=[]),
                      user("c", "c@contoso.test", assignedLicenses=[])]
    fake.purposes = {"a": 403, "b": 403, "c": 403}
    got = by_id(listing())
    assert (got["a"]["kind"], got["a"]["guess"]) == ("unknown", "user")
    assert got["b"]["guess"] == "shared_or_resource"
    assert got["c"]["guess"] is None


def test_aliases_come_from_smtp_entries_only(fake):
    fake.directory = [user("a", "a@contoso.test", proxyAddresses=[
        "SMTP:a@contoso.test", "smtp:Sales@Contoso.test", "SIP:a@contoso.test", "x500:/o=x", "smtp:sales@contoso.test"])]
    assert listing()["mailboxes"][0]["aliases"] == ["sales@contoso.test"]


def test_probe_skipped_above_cap(fake, monkeypatch):
    monkeypatch.setattr(m365, "PROBE_LIMIT", 2)
    fake.directory = [user(f"u{i}", f"u{i}@contoso.test") for i in range(3)]
    res = listing()
    assert res["purpose_checked"] is False
    assert not any("mailboxSettings" in r.url.path for r in fake.requests)
    assert all(m["kind"] == "unknown" and m["guess"] == "user" for m in res["mailboxes"])


def test_listing_wrong_secret_raises_auth(fake):
    with pytest.raises(ConnectorError) as e:
        m365.GraphDirectory().list_mailboxes(CONFIG, {"client_secret": "nope"})
    assert e.value.kind == ConnectorError.AUTH


def check(secrets=SECRETS):
    return m365.GraphDirectory().check(CONFIG, secrets)


def keyed(res):
    return {c["key"]: c for c in res["checks"]}


def test_check_all_good(fake):
    fake.directory = [user("a", "a@contoso.test")]
    res = check()
    assert res["ok"] is True and res["tenant_name"] == "Contoso Ltd"
    assert all(c["ok"] for c in res["checks"])


def test_check_names_each_missing_role(fake):
    fake.directory = [user("a", "a@contoso.test")]
    fake.roles = ["Mail.ReadWrite", "Calendars.Read", "MailboxSettings.ReadWrite", "Tasks.ReadWrite.All"]
    res = check()
    got = keyed(res)
    assert res["ok"] is False
    assert got["directory"]["missing"] == ["User.Read.All"] and got["directory"]["level"] == "required"
    assert got["contacts"]["missing"] == ["Contacts.Read"]
    assert got["mail"]["ok"] and got["calendars"]["ok"] and got["settings"]["ok"] and got["tasks"]["ok"]


def test_check_alternate_roles_accepted(fake):
    fake.directory = [user("a", "a@contoso.test")]
    fake.roles = ["Directory.Read.All", "Mail.ReadWrite", "Calendars.ReadWrite", "Contacts.ReadWrite",
                  "MailboxSettings.Read"]
    assert check()["ok"] is True


def test_check_empty_roles_asks_for_consent(fake):
    fake.directory = [user("a", "a@contoso.test")]
    fake.roles = []
    res = check()
    assert res["ok"] is False
    assert "admin consent" in keyed(res)["mail"]["msg"]


def test_check_tasks_missing_is_a_warning(fake):
    fake.directory = [user("a", "a@contoso.test")]
    fake.roles = ALL_ROLES[:-1]
    res = check()
    t = keyed(res)["tasks"]
    assert res["ok"] is True and t["ok"] is False and t["level"] == "optional"


def test_check_wrong_secret_is_plain_and_does_not_leak(fake):
    res = check({"client_secret": "hunter2-not-it"})
    assert res["ok"] is False
    msg = res["checks"][0]["msg"]
    assert msg == "Wrong or expired client secret." and "hunter2" not in json.dumps(res)


def test_check_non_jwt_token_does_not_crash(fake):
    fake.roles = None
    fake.directory = [user("a", "a@contoso.test")]
    res = check()
    c = keyed(res)["permissions"]
    assert c["ok"] is False and "could not be read" in c["msg"]
    assert res["ok"] is True  # the live probes still pass


def test_check_probe_failure_reports_graph_code(fake):
    fake.directory = [user("a", "a@contoso.test")]
    fake.fail["GET:/mailFolders/inbox"] = (403, "ErrorAccessDenied")
    fake.directory[0]["mail"] = fake.mailbox  # the mailbox of the simulated server itself
    res = check()
    assert res["ok"] is False and "ErrorAccessDenied" in keyed(res)["mailbox"]["msg"]


def test_check_without_organization_omits_name(fake):
    fake.directory = [user("a", "a@contoso.test")]
    fake.org_name = None
    assert "tenant_name" not in check()


def test_token_roles_helper():
    payload = base64.urlsafe_b64encode(json.dumps({"roles": ["A"]}).encode()).decode().rstrip("=")
    assert m365.token_roles(f"h.{payload}.s") == ["A"]
    assert m365.token_roles("opaque") is None and m365.token_roles("a.b.c") is None


def test_registered_as_directory():
    from app.connectors import registry
    assert isinstance(registry.get("m365_graph").directory, m365.GraphDirectory)
