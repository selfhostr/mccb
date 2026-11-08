"""Connect with Microsoft: the Entra application of a tenant created in two clicks, see docs/exchange.md.

A public client (the bootstrap app) gets a delegated token through the device code flow (no redirect URI: works when
mccb is only reachable on a LAN). With it, inside the client tenant, mccb creates a single-tenant application with the
read-only application permissions of a source, grants admin consent, adds a client secret, then waits until the new
credentials work. The delegated token lives in memory for the flow only: never stored, never logged.

Flows are kept in an in-process dict (single worker process, like the other in-memory caches of the app)."""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets as _secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator
from urllib.parse import quote

import httpx

from . import oauth
from .base import ConnectorError
from .m365 import CLOUDS, MAX_CONCURRENT, GraphClient, GraphDirectory, GraphError

# Microsoft's public first-party "Microsoft Graph Command Line Tools" app: present in every tenant, public client,
# device code allowed. Used until selfhostr registers its own bootstrap app. Override with M365_BOOTSTRAP_CLIENT_ID
# (your own multi-tenant public client app); an empty string disables "Connect with Microsoft".
DEFAULT_BOOTSTRAP_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
# User.Read: GET /organization (the name of the tenant); the other two: create the app and grant its admin consent
DELEGATED_SCOPES = ("User.Read", "Application.ReadWrite.All", "AppRoleAssignment.ReadWrite.All")
APP_ROLES = ("User.Read.All", "Mail.Read", "Calendars.Read", "Contacts.Read", "MailboxSettings.Read", "Tasks.Read.All")
APP_NAME = "mccb migration"
SECRET_NAME = "mccb"
SECRET_DAYS = 365
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
PROPAGATION_SECONDS = 180.0   # new credentials and role assignments take 10 s to a few minutes
PROPAGATION_INTERVAL = 10.0
FLOW_KEEP = 900.0             # a finished flow can still be read this long
STEPS = ("signin", "app", "consent", "secret", "propagation", "test", "remove")
# error_code of a flow, translated by the UI (the English `error` text stays as the details)
ERROR_CODES = ("not_admin", "consent_refused", "expired", "step_refused", "propagation_timeout", "login_refused",
               "unreachable", "other_tenant", "cancelled", "unexpected")

log = logging.getLogger("mccb.provision")


def _wait(event: threading.Event, seconds: float) -> bool:
    """Sleeps, True when cancelled meanwhile. Replaced in the tests."""
    return event.wait(seconds)


def bootstrap_client_id() -> str:
    return os.environ.get("M365_BOOTSTRAP_CLIENT_ID", DEFAULT_BOOTSTRAP_CLIENT_ID).strip()


def scrub(text: str, *values: str) -> str:
    for v in values:
        if v:
            text = text.replace(v, "***")
    return text


class ProvisionError(Exception):
    """A message meant for the user, with its error_code (one of ERROR_CODES)."""

    def __init__(self, msg: str, code: str = "step_refused"):
        super().__init__(msg)
        self.code = code

    @property
    def expired(self) -> bool:
        return self.code == "expired"


class Cancelled(Exception):
    pass


def hosts(cloud: str) -> tuple[str, str]:
    if cloud not in CLOUDS:
        raise ProvisionError(f"unknown cloud '{cloud}'")
    return CLOUDS[cloud]


# ------------------------------------------------------------------ device code (login endpoints)
SIGNIN_ERRORS = {
    "65004": ("consent_refused", "Consent was refused: sign in again and accept the permissions asked."),
    "90094": ("not_admin", "An administrator must sign in: use a Global Administrator account of the client tenant."),
    "90095": ("not_admin", "An administrator must sign in: use a Global Administrator account of the client tenant."),
    "700016": ("login_refused", "The bootstrap application is unknown in this cloud: check M365_BOOTSTRAP_CLIENT_ID."),
    "7000218": ("login_refused", "The bootstrap application does not allow public client flows: check "
                                 "M365_BOOTSTRAP_CLIENT_ID."),
    "50020": ("login_refused", "This account is not in a Microsoft Entra tenant: sign in with a work account of the "
                               "client tenant."),
    "50076": ("login_refused", "Multi-factor authentication is required: complete it in the browser."),
}


def _login_post(url: str, form: dict) -> tuple[int, dict]:
    try:
        with oauth.http_client(True, 60.0) as c:
            r = c.post(url, data=form, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise ProvisionError(f"The Microsoft login service cannot be reached ({type(e).__name__}).",
                             "unreachable") from e
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body if isinstance(body, dict) else {}


def _signin_error(body: dict) -> ProvisionError:
    code = str(body.get("error") or "")
    desc = str(body.get("error_description") or "")
    for num, (ecode, plain) in SIGNIN_ERRORS.items():
        if f"AADSTS{num}:" in desc or f"AADSTS{num} " in desc:
            return ProvisionError(plain, ecode)
    if code in ("expired_token", "code_expired"):
        return ProvisionError("The code expired before the sign-in: start again.", "expired")
    if code == "authorization_declined":
        return ProvisionError("The sign-in was declined.", "login_refused")
    if code == "access_denied":
        return ProvisionError("Access was refused: sign in again and accept the permissions asked.", "consent_refused")
    return ProvisionError(f"The sign-in failed ({code or 'unknown error'}).", "login_refused")


def start_device_code(cloud: str, client_id: str) -> dict:
    """{device_code, user_code, verification_uri, expires_in, interval, message}."""
    login, graph = hosts(cloud)
    scope = " ".join(f"{graph}/{s}" for s in DELEGATED_SCOPES)
    status, body = _login_post(f"{login}/organizations/oauth2/v2.0/devicecode", {"client_id": client_id, "scope": scope})
    if status != 200 or not body.get("device_code"):
        raise _signin_error(body)
    return {"device_code": str(body["device_code"]), "user_code": str(body.get("user_code") or ""),
            "verification_uri": str(body.get("verification_uri") or ""),
            "expires_in": int(body.get("expires_in") or 900), "interval": max(1, int(body.get("interval") or 5)),
            "message": str(body.get("message") or "")}


def poll_token(cloud: str, client_id: str, device_code: str) -> tuple[str | None, float]:
    """(delegated token, 0) once signed in; (None, extra seconds to wait) while pending. Raises ProvisionError."""
    login, _ = hosts(cloud)
    status, body = _login_post(f"{login}/organizations/oauth2/v2.0/token",
                               {"grant_type": DEVICE_GRANT, "client_id": client_id, "device_code": device_code})
    if status == 200 and body.get("access_token"):
        return str(body["access_token"]), 0.0
    code = str(body.get("error") or "")
    if code == "authorization_pending":
        return None, 0.0
    if code == "slow_down":
        return None, 5.0
    if status >= 500 or status == 429:
        return None, 5.0
    raise _signin_error(body)


# ------------------------------------------------------------------ Graph, with the delegated token
class _Bearer:
    """A fixed access token, as GraphClient wants a token source."""

    def __init__(self, token: str):
        self._token = token

    def token(self, force: bool = False) -> str:
        return self._token

    def invalidate(self, token: str) -> None:
        pass


class Admin:
    """The calls made in the client tenant with the delegated token of the administrator."""

    def __init__(self, cloud: str, token: str):
        self.cloud = cloud
        self.client = GraphClient({"cloud": cloud}, {}, _Bearer(token), threading.BoundedSemaphore(MAX_CONCURRENT),
                                  tenant=True)

    def close(self) -> None:
        self.client.close()

    def _call(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        r = self.client.request(method, self.client.url(path), params=params, json_body=body)
        if r.status_code == 204 or not r.content:
            return {}
        try:
            return r.json()
        except ValueError as e:
            raise ProvisionError("Microsoft Graph sent an invalid answer.") from e

    def organization(self) -> tuple[str, str]:
        rows = self._call("GET", "/organization", params={"$select": "id,displayName"}).get("value") or []
        if not rows or not rows[0].get("id"):
            raise ProvisionError("Microsoft Graph returned no organization for the signed-in account.")
        return str(rows[0]["id"]), str(rows[0].get("displayName") or "")

    def graph_roles(self) -> tuple[str, dict[str, str]]:
        """Id of the Graph service principal of the tenant and the ids of the application roles needed, by value."""
        rows = self._call("GET", "/servicePrincipals", params={
            "$filter": f"appId eq '{GRAPH_APP_ID}'", "$select": "id,appRoles"}).get("value") or []
        if not rows:
            raise ProvisionError("The Microsoft Graph service principal was not found in the tenant.")
        by_value = {str(r.get("value")): str(r.get("id")) for r in rows[0].get("appRoles") or []
                    if "Application" in (r.get("allowedMemberTypes") or ["Application"])}
        missing = [v for v in APP_ROLES if v not in by_value]
        if missing:
            raise ProvisionError(f"Microsoft Graph does not offer the permissions {', '.join(missing)} in this tenant.")
        return str(rows[0]["id"]), {v: by_value[v] for v in APP_ROLES}

    def create_app(self, role_ids: dict[str, str]) -> tuple[str, str]:
        body = {"displayName": APP_NAME, "signInAudience": "AzureADMyOrg",
                "requiredResourceAccess": [{"resourceAppId": GRAPH_APP_ID,
                                            "resourceAccess": [{"id": i, "type": "Role"} for i in role_ids.values()]}]}
        out = self._call("POST", "/applications", body)
        return str(out["id"]), str(out["appId"])

    def create_sp(self, app_id: str) -> str:
        return str(self._call("POST", "/servicePrincipals", {"appId": app_id})["id"])

    def grant(self, graph_sp: str, sp: str, role_ids: dict[str, str]) -> None:
        for rid in role_ids.values():
            self._call("POST", f"/servicePrincipals/{quote(graph_sp, safe='')}/appRoleAssignedTo",
                       {"principalId": sp, "resourceId": graph_sp, "appRoleId": rid})

    def add_password(self, app_object_id: str) -> str:
        end = (datetime.now(timezone.utc) + timedelta(days=SECRET_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = self._call("POST", f"/applications/{quote(app_object_id, safe='')}/addPassword",
                         {"passwordCredential": {"displayName": SECRET_NAME, "endDateTime": end}})
        if not out.get("secretText"):
            raise ProvisionError("Microsoft Graph did not return the client secret.")
        return str(out["secretText"])

    def delete_app(self, app_object_id: str) -> bool:
        """Deletes the application object mccb created (its service principal goes with it). False: already gone."""
        try:
            self._call("DELETE", f"/applications/{quote(app_object_id, safe='')}")
        except GraphError as e:
            if e.status == 404:
                return False
            raise
        return True


ACTIONS = {
    "organization": "Reading the organization of the tenant",
    "roles": "Reading the permissions Microsoft Graph offers in the tenant",
    "app": "Creating the application",
    "sp": "Creating the service principal of the application",
    "consent": "Granting admin consent",
    "secret": "Adding a client secret",
    "remove": "Removing the application",
    "": "A Microsoft Graph request",
}
APP_ADMIN = "the account must be Global Administrator or Application Administrator of the client tenant"
NEEDS = {"consent": "the account must be Global Administrator (or Privileged Role Administrator) of the client tenant"}


def graph_detail(e: ConnectorError) -> str:
    """What Graph answered, e.g. "Graph 403 Authorization_RequestDenied: Insufficient privileges..."."""
    status, code, text = getattr(e, "status", 0), getattr(e, "code", "") or "", getattr(e, "text", "") or ""
    if not status:
        return str(e)[:200]
    return (f"Graph {status}{' ' + code if code else ''}{': ' + text if text else ''}")[:240]


def graph_failure(e: ConnectorError, action: str) -> ProvisionError:
    """The error of a Graph call made with the delegated token, labelled with the action that was running."""
    status, code = getattr(e, "status", 0), getattr(e, "code", "") or ""
    what = ACTIONS.get(action, ACTIONS[""])
    log.warning("Connect with Microsoft: %s failed (HTTP %s %s)", what.lower(), status or "-", code or "-")
    detail = graph_detail(e)
    if status in (401, 403):
        return ProvisionError(f"{what} was refused: {NEEDS.get(action, APP_ADMIN)} ({detail}).", "not_admin")
    if e.kind in (ConnectorError.UNREACHABLE, ConnectorError.TRANSIENT):
        return ProvisionError(f"{what} failed: Microsoft Graph cannot be reached right now, try again in a few "
                              f"minutes ({detail}).", "unreachable")
    return ProvisionError(f"{what} failed ({detail}).", "step_refused")


@contextmanager
def graph_step(action: str) -> Iterator[None]:
    try:
        yield
    except ConnectorError as e:
        raise graph_failure(e, action) from e


def token_claims(token: str) -> dict:
    """The claims of an access token, read without verification (only to learn the tenant). {} when not a JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def tenant_of(admin: "Admin", token: str) -> tuple[str, str]:
    """(tenant id, name). The id comes from the `tid` claim of the token; the name from GET /organization, which must
    not block the flow: failing that, the domain of the account signed in, then the tenant id."""
    claims = token_claims(token)
    tid, name = str(claims.get("tid") or ""), ""
    try:
        with graph_step("organization"):
            org_id, name = admin.organization()
        tid = tid or org_id
    except ProvisionError:
        if not tid:
            raise
    if not name:
        account = str(claims.get("upn") or claims.get("preferred_username") or claims.get("unique_name") or "")
        name = account.rpartition("@")[2] if "@" in account else ""
    return tid, name or tid


def wait_ready(config: dict, client_secret: str, cancel: threading.Event) -> tuple[dict, bool]:
    """Retries the check of the new credentials until it passes or PROPAGATION_SECONDS. (last check, passed)."""
    attempts = max(1, int(round(PROPAGATION_SECONDS / PROPAGATION_INTERVAL)))
    res: dict = {}
    for n in range(attempts):
        if n and _wait(cancel, PROPAGATION_INTERVAL):
            raise Cancelled()
        if cancel.is_set():
            raise Cancelled()
        try:
            res = GraphDirectory().check(config, {"client_secret": client_secret})
        except ConnectorError as e:
            res = {"ok": False, "checks": [{"key": "token", "ok": False, "level": "required",
                                            "msg": scrub(str(e), client_secret)[:300]}]}
        if res.get("ok"):
            return res, True
    return res, False


# ------------------------------------------------------------------ flows
@dataclass
class Flow:
    kind: str              # "provision" | "deprovision"
    cloud: str
    client_id: str         # the bootstrap app
    device: dict           # the answer of the devicecode endpoint (device_code stays here, never returned)
    name: str = ""
    connection_id: int | None = None
    app_object_id: str = ""  # deprovision: the application to delete
    tenant_id: str = ""      # deprovision: the tenant it lives in
    id: str = field(default_factory=lambda: _secrets.token_urlsafe(16))
    status: str = "waiting"
    step: str | None = "signin"
    error: str | None = None
    error_code: str | None = None
    connection: dict | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    created: float = field(default_factory=time.time)
    finished: float | None = None

    def view(self) -> dict:
        return {"status": self.status, "step": self.step, "error": self.error, "error_code": self.error_code,
                "connection": self.connection}

    def started(self) -> dict:
        d = self.device
        return {"flow_id": self.id, "user_code": d["user_code"], "verification_uri": d["verification_uri"],
                "expires_in": d["expires_in"], "interval": d["interval"], "message": d["message"]}


_flows: dict[str, Flow] = {}
_flows_lock = threading.Lock()


def _prune() -> None:
    now = time.time()
    with _flows_lock:
        for fid in [k for k, f in _flows.items()
                    if (f.finished and now - f.finished > FLOW_KEEP)
                    or now - f.created > f.device["expires_in"] + FLOW_KEEP]:
            _flows.pop(fid).cancel.set()


def get_flow(fid: str) -> Flow | None:
    _prune()
    with _flows_lock:
        return _flows.get(fid)


def cancel_flow(fid: str) -> None:
    with _flows_lock:
        f = _flows.pop(fid, None)
    if f is not None:
        f.cancel.set()


# save(flow, config, client_secret, provisioned, check) -> connection view; clear(flow) -> connection view
Saver = Callable[[Flow, dict, str, dict, dict], dict]
Clearer = Callable[[Flow], dict]


def start(flow_kind: str, cloud: str, save: Saver | None = None, clear: Clearer | None = None, **kw) -> Flow:
    client_id = bootstrap_client_id()
    device = start_device_code(cloud, client_id)
    f = Flow(kind=flow_kind, cloud=cloud, client_id=client_id, device=device, **kw)
    _prune()
    with _flows_lock:
        _flows[f.id] = f
    target = _run_provision if flow_kind == "provision" else _run_deprovision
    threading.Thread(target=target, args=(f, save or clear), name=f"m365-{flow_kind}", daemon=True).start()
    return f


def _signin(f: Flow) -> str:
    deadline = time.time() + f.device["expires_in"]
    interval = float(f.device["interval"])
    while True:
        if _wait(f.cancel, interval):
            raise Cancelled()
        if time.time() > deadline:
            raise ProvisionError("The code expired before the sign-in: start again.", "expired")
        token, extra = poll_token(f.cloud, f.client_id, f.device["device_code"])
        if token:
            return token
        interval += extra


def _finish(f: Flow, status: str, error: str | None = None, code: str | None = None) -> None:
    f.status, f.error, f.error_code, f.finished = status, error, code, time.time()


def _fail(f: Flow, e: BaseException, *values: str) -> None:
    """Ends a flow on an exception, with a message that carries no token nor secret."""
    if isinstance(e, Cancelled):
        _finish(f, "error", "Cancelled.", "cancelled")
        return
    if isinstance(e, ConnectorError):
        e = graph_failure(e, "")
    if isinstance(e, ProvisionError):
        _finish(f, "expired" if e.expired else "error", scrub(str(e), *values), e.code)
        return
    log.warning("Connect with Microsoft: unexpected %s", type(e).__name__)
    _finish(f, "error", scrub(f"Unexpected error ({type(e).__name__}).", *values), "unexpected")


def _run_provision(f: Flow, save: Saver) -> None:
    token, secret, admin, app_object_id = "", "", None, ""
    saved = False
    try:
        token = _signin(f)
        f.status = "working"
        admin = Admin(f.cloud, token)
        f.step = "app"
        tenant_id, org_name = tenant_of(admin, token)
        f.name = f.name or org_name or "Microsoft 365"
        with graph_step("roles"):
            graph_sp, role_ids = admin.graph_roles()
        if f.cancel.is_set():
            raise Cancelled()
        with graph_step("app"):
            app_object_id, app_id = admin.create_app(role_ids)
        with graph_step("sp"):
            sp = admin.create_sp(app_id)
        f.step = "consent"
        with graph_step("consent"):
            admin.grant(graph_sp, sp, role_ids)
        f.step = "secret"
        with graph_step("secret"):
            secret = admin.add_password(app_object_id)
        f.step = "propagation"
        config = {"tenant_id": tenant_id, "client_id": app_id, "cloud": f.cloud}
        check, ready = wait_ready(config, secret, f.cancel)
        if not ready:
            # saved all the same: Test can be run again once Microsoft has propagated the new credentials
            f.connection = save(f, config, secret, _provisioned(app_object_id, sp), check)
            saved = True
            _finish(f, "done", "The new credentials do not work yet (Microsoft can take a few minutes to propagate "
                               "them). The tenant is saved: run Test again later.", "propagation_timeout")
            return
        f.step = "test"
        f.connection = save(f, config, secret, _provisioned(app_object_id, sp), check)
        saved = True
        _finish(f, "done")
    except Exception as e:  # noqa: BLE001 - the flow must end, with a message that carries no secret
        _fail(f, e, token, secret)
    finally:
        if app_object_id and not saved and admin is not None:
            try:  # best effort: do not leave a half-made application in the tenant
                admin.delete_app(app_object_id)
            except Exception:  # noqa: BLE001,S110
                pass
        if admin is not None:
            admin.close()
        token = secret = ""


def _provisioned(app_object_id: str, sp: str) -> dict:
    return {"app_object_id": app_object_id, "sp_object_id": sp, "created_at": time.time()}


def _run_deprovision(f: Flow, clear: Clearer) -> None:
    token, admin = "", None
    try:
        token = _signin(f)
        f.status = "working"
        f.step = "remove"
        admin = Admin(f.cloud, token)
        tenant_id, _ = tenant_of(admin, token)
        if f.tenant_id and tenant_id.lower() != f.tenant_id.lower():
            raise ProvisionError("The account signed in belongs to another tenant: sign in with an administrator of "
                                 "the tenant of this connection.", "other_tenant")
        if f.cancel.is_set():
            raise Cancelled()
        with graph_step("remove"):
            admin.delete_app(f.app_object_id)  # only the application mccb created, nothing else
        f.connection = clear(f)
        _finish(f, "done")
    except Exception as e:  # noqa: BLE001 - the flow must end, with a message that carries no token
        _fail(f, e, token)
    finally:
        if admin is not None:
            admin.close()
        token = ""
