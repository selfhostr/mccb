"""Connect with Microsoft: the device code flow of the bootstrap app creates the tenant app, against FakeEntra."""
import time

import httpx
import pytest

from tests.server import fake_exchange as fx

BOOT = "boot-client-1"
ROLE_VALUES = {"User.Read.All", "Mail.Read", "Calendars.Read", "Contacts.Read", "MailboxSettings.Read", "Tasks.Read.All"}
TERMINAL = ("done", "error", "expired")


@pytest.fixture
def entra(monkeypatch):
    from app.connectors import m365, m365_provision as mp, oauth
    from app.server import api_sources
    monkeypatch.setenv("M365_BOOTSTRAP_CLIENT_ID", BOOT)
    monkeypatch.setattr(m365, "_sleep", lambda s: None)
    monkeypatch.setattr(mp, "_wait", lambda ev, s: ev.wait(0.002))
    api_sources._cache.clear()
    f = fx.FakeEntra()
    f.directory = [{"id": "oid-me", "displayName": "Me", "mail": "me@contoso.test", "userPrincipalName": "me@contoso.test",
                    "proxyAddresses": [], "accountEnabled": True, "assignedLicenses": [{"skuId": "x"}],
                    "userType": "Member"}]
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(f))
    yield f
    with mp._flows_lock:
        flows = list(mp._flows.values())
    for fl in flows:
        fl.cancel.set()


def secrets_of(fake):
    return [fake.DELEGATED, *fake.passwords.values()]


def clean(fake, r):
    for s in secrets_of(fake):
        assert s not in r.text
    return r


def wait_flow(client, fake, fid, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        r = clean(fake, client.get(f"/api/source-connections/provision/{fid}"))
        assert r.status_code == 200, r.text
        if r.json()["status"] in TERMINAL:
            return r.json()
        time.sleep(0.01)
    raise AssertionError("flow did not finish")


def start(client, fake, **body):
    r = clean(fake, client.post("/api/source-connections/provision", json=body))
    assert r.status_code == 201, r.text
    return r.json()


def test_happy_path_end_to_end(client, entra):
    f = start(client, entra)
    assert f["user_code"] == "ABCD-EFGH" and f["verification_uri"] == "https://microsoft.com/devicelogin"
    assert f["expires_in"] == 900 and f["interval"] == 5 and f["message"] and f["flow_id"]
    assert set(f) == {"flow_id", "user_code", "verification_uri", "expires_in", "interval", "message"}
    assert "device_code" not in str(f) and entra.device_code not in str(f)
    asked = entra.device_requests[0]
    assert asked["client_id"] == BOOT
    assert "Application.ReadWrite.All" in asked["scope"] and "AppRoleAssignment.ReadWrite.All" in asked["scope"]

    res = wait_flow(client, entra, f["flow_id"])
    assert res["status"] == "done" and res["error"] is None, res
    conn = res["connection"]
    assert conn["name"] == "Contoso Ltd" and conn["kind"] == "m365_graph" and conn["has_secret"] is True
    (app,) = entra.applications.values()
    assert conn["config"] == {"tenant_id": "tid-contoso", "client_id": app["appId"], "cloud": "global"}
    assert conn["provisioned"]["app_object_id"] == app["id"]
    assert conn["provisioned"]["sp_object_id"] in entra.service_principals and conn["provisioned"]["created_at"]
    assert conn["last_test"] and conn["last_test_result"]["ok"] is True
    # the app: single tenant, the six application roles, resolved by value on the Graph service principal
    assert app["displayName"] == "mccb migration" and app["signInAudience"] == "AzureADMyOrg"
    (rra,) = app["requiredResourceAccess"]
    assert rra["resourceAppId"] == fx.GRAPH_APP_ID
    assert {entra.role_value(x["id"]) for x in rra["resourceAccess"]} == ROLE_VALUES
    assert all(x["type"] == "Role" for x in rra["resourceAccess"])
    assert {entra.role_value(a["appRoleId"]) for a in entra.assignments} == ROLE_VALUES
    assert len(entra.assignments) == 6
    # stored like any connection: the saved one works, the secret never comes back
    got = clean(entra, client.get(f"/api/source-connections/{conn['id']}")).json()
    assert got["provisioned"] == conn["provisioned"]
    clean(entra, client.get("/api/source-connections"))
    assert clean(entra, client.post(f"/api/source-connections/{conn['id']}/test")).json()["ok"] is True


def test_name_and_cloud_given(client, entra, monkeypatch):
    from app.connectors import m365, oauth
    seen = []
    monkeypatch.setattr(oauth, "TRANSPORT", httpx.MockTransport(lambda req: (seen.append(req.url.host), entra(req))[1]))
    f = start(client, entra, cloud="usgov", name="Client A")
    res = wait_flow(client, entra, f["flow_id"])
    assert res["status"] == "done" and res["connection"]["name"] == "Client A"
    assert res["connection"]["config"]["cloud"] == "usgov"
    login, graph = m365.CLOUDS["usgov"]
    assert set(seen) == {httpx.URL(login).host, httpx.URL(graph).host}


def test_unknown_cloud_is_422(client, entra):
    assert client.post("/api/source-connections/provision", json={"cloud": "mars"}).status_code == 422


def test_not_admin_is_a_human_error_and_nothing_saved(client, entra):
    entra.admin = False
    res = wait_flow(client, entra, start(client, entra)["flow_id"])
    assert res["status"] == "error" and res["step"] == "app" and res["connection"] is None
    assert "Global Administrator or Application Administrator" in res["error"]
    assert client.get("/api/source-connections").json() == []


def test_consent_assignment_forbidden_removes_the_app(client, entra):
    entra.consent_forbidden = True
    res = wait_flow(client, entra, start(client, entra)["flow_id"])
    assert res["status"] == "error" and res["step"] == "consent" and "Global Administrator" in res["error"]
    assert entra.applications == {} and len(entra.deleted) == 1
    assert client.get("/api/source-connections").json() == []


@pytest.mark.parametrize("error,desc,words", [
    ("access_denied", "AADSTS65004: User declined to consent to access the app.", "refused"),
    ("invalid_grant", "AADSTS90094: Admin consent is required for the permissions requested.", "administrator"),
    ("authorization_declined", "AADSTS70000: The user declined.", "declined"),
])
def test_signin_refused(client, entra, error, desc, words):
    entra.device_error = (error, desc)
    res = wait_flow(client, entra, start(client, entra)["flow_id"])
    assert res["status"] == "error" and res["step"] == "signin" and words in res["error"].lower(), res
    assert "AADSTS" not in res["error"]
    assert entra.applications == {}


def test_expired_code(client, entra):
    entra.device_error = ("expired_token", "AADSTS70020: The device code has expired.")
    res = wait_flow(client, entra, start(client, entra)["flow_id"])
    assert res["status"] == "expired" and res["error"] and res["connection"] is None


def test_cancel(client, entra):
    entra.pending = 10 ** 9
    fid = start(client, entra)["flow_id"]
    time.sleep(0.05)
    assert client.get(f"/api/source-connections/provision/{fid}").json()["status"] == "waiting"
    assert client.delete(f"/api/source-connections/provision/{fid}").status_code == 204
    assert client.get(f"/api/source-connections/provision/{fid}").status_code == 404
    time.sleep(0.05)
    n = len(entra.device_requests)
    time.sleep(0.1)
    assert len(entra.device_requests) == n  # the polling stopped
    assert client.delete(f"/api/source-connections/provision/{fid}").status_code == 204
    assert client.get("/api/source-connections/provision/nope").status_code == 404


def test_propagation_timeout_saves_the_connection_flagged(client, entra, monkeypatch):
    from app.connectors import m365_provision as mp
    monkeypatch.setattr(mp, "PROPAGATION_SECONDS", 30.0)
    monkeypatch.setattr(mp, "PROPAGATION_INTERVAL", 10.0)
    entra.propagation = 10 ** 6
    res = wait_flow(client, entra, start(client, entra)["flow_id"])
    assert res["status"] == "done" and res["step"] == "propagation" and "Test" in res["error"]
    conn = res["connection"]
    assert conn["last_test_result"]["ok"] is False and conn["provisioned"]["app_object_id"] in entra.applications
    assert len(entra.token_requests) == 3
    entra.propagation = 0  # propagated later: Test again works
    assert clean(entra, client.post(f"/api/source-connections/{conn['id']}/test")).json()["ok"] is True


def test_deprovision(client, entra):
    conn = wait_flow(client, entra, start(client, entra)["flow_id"])["connection"]
    entra.pending = 1
    r = clean(entra, client.post(f"/api/source-connections/{conn['id']}/deprovision"))
    assert r.status_code == 201 and r.json()["user_code"] == "ABCD-EFGH"
    res = wait_flow(client, entra, r.json()["flow_id"])
    assert res["status"] == "done" and res["step"] == "remove", res
    assert entra.applications == {} and entra.deleted == [conn["provisioned"]["app_object_id"]]
    assert res["connection"]["provisioned"] is None
    assert client.get(f"/api/source-connections/{conn['id']}").json()["provisioned"] is None
    assert client.post(f"/api/source-connections/{conn['id']}/deprovision").status_code == 409


def test_deprovision_of_another_tenant_is_refused(client, entra):
    conn = wait_flow(client, entra, start(client, entra)["flow_id"])["connection"]
    entra.tenant_id = "tid-other"
    res = wait_flow(client, entra, client.post(f"/api/source-connections/{conn['id']}/deprovision").json()["flow_id"])
    assert res["status"] == "error" and res["step"] == "remove" and entra.deleted == []
    assert client.get(f"/api/source-connections/{conn['id']}").json()["provisioned"] is not None


def test_deprovision_app_already_gone_clears_the_flag(client, entra):
    conn = wait_flow(client, entra, start(client, entra)["flow_id"])["connection"]
    entra.applications.clear()
    res = wait_flow(client, entra, client.post(f"/api/source-connections/{conn['id']}/deprovision").json()["flow_id"])
    assert res["status"] == "done" and res["connection"]["provisioned"] is None


def test_manual_connection_is_not_provisioned(client, entra):
    r = client.post("/api/source-connections", json={"name": "M", "config": {"tenant_id": "t", "client_id": "c"},
                                                     "secrets": {"client_secret": "x"}})
    assert r.status_code == 201 and r.json()["provisioned"] is None
    assert client.post(f"/api/source-connections/{r.json()['id']}/deprovision").status_code == 409
    assert client.post("/api/source-connections/999/deprovision").status_code == 404


def _m365_entry(client):
    return next(c for c in client.get("/api/connectors").json() if c["kind"] == "m365_graph")


def test_disabled_when_the_bootstrap_id_is_empty(client, monkeypatch):
    from app.connectors import m365_provision as mp
    monkeypatch.setenv("M365_BOOTSTRAP_CLIENT_ID", "")
    assert _m365_entry(client)["provision"] is False
    assert client.post("/api/source-connections/provision", json={}).status_code == 409
    monkeypatch.delenv("M365_BOOTSTRAP_CLIENT_ID")
    assert mp.DEFAULT_BOOTSTRAP_CLIENT_ID == "14d82eec-204b-4c2f-b7e8-296a70dab67e"
    assert _m365_entry(client)["provision"] is True  # enabled by default
    monkeypatch.setenv("M365_BOOTSTRAP_CLIENT_ID", BOOT)
    assert _m365_entry(client)["provision"] is True
    assert all("provision" not in c for c in client.get("/api/connectors").json() if c["kind"] != "m365_graph")


def test_disabled_deprovision_is_409(client, entra, monkeypatch):
    conn = wait_flow(client, entra, start(client, entra)["flow_id"])["connection"]
    monkeypatch.setenv("M365_BOOTSTRAP_CLIENT_ID", "")
    assert client.post(f"/api/source-connections/{conn['id']}/deprovision").status_code == 409


def test_routes_need_auth(env):
    from fastapi.testclient import TestClient
    from app.server.web import app
    with TestClient(app) as c:
        assert c.post("/api/source-connections/provision", json={}).status_code == 401
        assert c.get("/api/source-connections/provision/x").status_code == 401
        assert c.delete("/api/source-connections/provision/x").status_code == 401
        assert c.post("/api/source-connections/1/deprovision").status_code == 401


def test_scrub_removes_tokens_and_secrets():
    from app.connectors import m365_provision as mp
    assert mp.scrub("boom tok-1 and sec-2", "tok-1", "sec-2", "") == "boom *** and ***"
