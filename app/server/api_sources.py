"""Source connections: one credential reached by many accounts (v1: a Microsoft 365 tenant, kind m365_graph).
See docs/api.md and docs/m365-tenant.md. The secret goes in and never comes out (has_secret only); directory data
(names, addresses) is kept in memory for 5 minutes, never stored."""
from __future__ import annotations

import dataclasses
import hashlib
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from ..connectors import m365_provision as mp
from ..connectors import registry, wiring
from ..connectors.base import ConnectorError
from . import db, repo, runs, tenant_match

router = APIRouter(prefix="/api/source-connections")
ACTIVE = "('queued','running','paused','stopping')"
CACHE_SECONDS = 300
IDENTITY_KEYS = ("tenant_id", "cloud")  # what the linked accounts' namespaces hang on: frozen while linked

_cache: dict[int, tuple[str, float, dict]] = {}   # connection id -> (key of config + secret, time, listing)
_cache_lock = threading.Lock()


class ConnectionIn(BaseModel):
    name: str | None = Field(None, max_length=120)
    kind: str | None = None
    config: dict[str, Any] | None = None
    # omitted: kept. {}: cleared. {name: value}: that value replaced, the others kept.
    secrets: dict[str, str] | None = None
    notes: str | None = Field(None, max_length=2000)


class ProbeIn(BaseModel):
    kind: str = "m365_graph"
    config: dict[str, Any] | None = None
    secrets: dict[str, str] | None = None
    id: int | None = None  # secrets omitted: those stored on this connection


class ProvisionIn(BaseModel):
    cloud: str = "global"
    name: str | None = Field(None, max_length=120)


class LinkItem(BaseModel):
    account_id: int
    mailbox_id: str = Field(..., min_length=1, max_length=200)


class LinkIn(BaseModel):
    server_id: int
    links: list[LinkItem] = Field(..., max_length=10000)
    include_tasks: bool | None = None
    start: bool = False
    mode: str = Field("full", pattern="^(full|delta)$")
    batch_name: str | None = Field(None, max_length=120)


# ------------------------------------------------------------------ helpers
def _row(cid: int):
    r = db.q1("SELECT * FROM source_connections WHERE id=?", (cid,))
    if r is None:
        raise HTTPException(404, "unknown source connection")
    return r


def _spec(kind: str):
    spec = registry.get(kind)
    if spec is None or spec.source is None:
        raise HTTPException(422, f"unknown connector '{kind}'")
    if spec.directory is None:
        raise HTTPException(422, f"connector '{kind}' has no source connection")
    return spec


def _connection_role(spec) -> registry.Role:
    """The source role of the kind cut down to its connection-scoped fields."""
    return dataclasses.replace(spec.source, fields=registry.split_fields(spec.source, "connection"))


def _validated(kind: str, config: dict | None, current: dict, secrets: dict | None) -> tuple[dict, dict]:
    role = _connection_role(_spec(kind))
    try:
        return registry.normalize_config(role, config), registry.merge_secrets(role, current, secrets)
    except ConnectorError as e:
        raise HTTPException(422, str(e)) from e


def _stored(token: str | None) -> dict:
    try:
        return wiring.load_secrets(token)
    except ConnectorError:
        return {}  # unreadable (key changed): as if absent, the new value replaces it


def _linked(cid: int) -> int:
    return db.q1("SELECT COUNT(*) AS n FROM accounts WHERE source_connection_id=?", (cid,))["n"]


def _check(kind: str, config: dict, secrets: dict) -> dict:
    """A refusal is an answer: {ok: false, checks: [...]}, never an HTTP error."""
    try:
        return registry.get(kind).directory.check(config, secrets)
    except ConnectorError as e:
        return {"ok": False, "checks": [{"key": "token", "ok": False, "level": "required",
                                         "msg": _scrub(str(e), secrets)}]}


def _scrub(text: str, secrets: dict) -> str:
    for v in secrets.values():
        if v:
            text = text.replace(str(v), "***")
    return text


def _cache_key(r) -> str:
    return hashlib.sha256(f"{r['kind']}\0{r['config']}\0{r['secret_enc'] or ''}".encode()).hexdigest()


def _forget(cid: int) -> None:
    with _cache_lock:
        _cache.pop(cid, None)


def _mailboxes(r, refresh: bool) -> dict:
    key = _cache_key(r)
    with _cache_lock:
        hit = _cache.get(r["id"])
    if hit and not refresh and hit[0] == key and time.time() - hit[1] < CACHE_SECONDS:
        return hit[2]
    secrets = _stored(r["secret_enc"])
    try:
        out = registry.get(r["kind"]).directory.list_mailboxes(db.jload(r["config"], {}) or {}, secrets)
    except ConnectorError as e:
        raise HTTPException(502, _scrub(str(e), secrets)) from e
    with _cache_lock:
        _cache[r["id"]] = (key, time.time(), out)
    return out


def _tasks_granted(r) -> bool:
    """From the last test of the connection; true when never tested."""
    res = db.jload(r["last_test_result"], None) or {}
    for c in res.get("checks") or []:
        if c.get("key") == "tasks":
            return bool(c.get("ok"))
    return True


def _server_info(s) -> dict:
    res = db.jload(s["last_test_result"], None) or {}

    def ok(kind):
        v = res.get(kind)
        return bool(v.get("ok")) if isinstance(v, dict) and "ok" in v else None
    return {"id": s["id"], "name": s["name"], "dav_enabled": bool(s["dav_enabled"]),
            "caldav_ok": ok("caldav"), "carddav_ok": ok("carddav")}


def _pst_valid(a) -> bool:
    kind = wiring.source_kind(a)
    pid = a["pst_id"] if kind == wiring.PST else (db.jload(a["source_config"], {}) or {}).get("pst_id")
    p = db.q1("SELECT status FROM psts WHERE id=?", (pid,)) if pid else None
    return p is not None and p["status"] == "valid"


def _account_dict(a) -> dict:
    """An account as tenant_match wants it, plus what the screen shows of it."""
    kind = a["source_kind"] or (wiring.PST if a["pst_id"] else None)
    own = db.jload(a["source_config"], {}) or {}
    job = repo.latest_job_row(a["id"])
    return {"id": a["id"], "email": a["email"], "status": a["status"], "last_test": a["last_test"],
            "last_test_result": db.jload(a["last_test_result"], None), "source_kind": kind,
            "source_connection_id": a["source_connection_id"],
            "source_tenant_id": own.get("tenant_id") if a["source_kind"] and not a["source_connection_id"] else None,
            "pst_valid": _pst_valid(a) if kind in (wiring.PST, wiring.FILES) else False,
            "opt_mail": bool(a["opt_mail"]), "opt_calendar": bool(a["opt_calendar"]),
            "opt_contacts": bool(a["opt_contacts"]),
            "job": {"id": job["id"], "status": job["status"]} if job is not None else None}


ACCOUNT_KEYS = ("id", "email", "status", "last_test", "last_test_result", "source_kind", "source_connection_id")
UNKNOWN = {"mail": "unknown", "calendar": "unknown", "contacts": "unknown", "tasks": "unknown"}


def _match(r, server, listing: dict) -> dict:
    accounts = [_account_dict(a) for a in db.q("SELECT * FROM accounts WHERE server_id=? ORDER BY email",
                                               (server["id"],))]
    by_id = {a["id"]: a for a in accounts}
    info = _server_info(server)
    tasks = _tasks_granted(r)
    tenant = (db.jload(r["config"], {}) or {}).get("tenant_id")
    res = tenant_match.match(listing["mailboxes"], accounts, r["id"], info, tasks, tenant)
    winners = {row["account_id"]: row["address"] for row in res["rows"]
               if row["account_id"] is not None and row["mailbox"] is not None and row["state"] != "conflict"}
    rows = []
    for row in res["rows"]:
        acct = by_id.get(row["account_id"])
        by = row["matched_by"]
        out = {"state": row["state"], "mailbox": row["mailbox"],
               "account": {k: acct[k] for k in ACCOUNT_KEYS} if acct else None,
               "matched_by": by,
               "matched_address": str(acct["email"]).lower() if acct and by in ("upn", "alias") else None,
               "coverage": row["coverage"] or (tenant_match.coverage(acct, info, tasks) if acct else dict(UNKNOWN)),
               "last_job": acct["job"] if acct and row["state"] == "linked" else None}
        if row["state"] == "conflict":
            out["conflict_with"] = winners.get(row["account_id"])
        rows.append(out)
    return {"connection": {"id": r["id"], "name": r["name"]}, "server": info, "rows": rows,
            "counts": {k: v for k, v in res["counts"].items() if k != "all"},
            "fetched_at": listing["fetched_at"], "purpose_checked": listing["purpose_checked"],
            "hidden": listing["hidden"]}


# ------------------------------------------------------------------ routes
@router.get("")
def list_connections():
    return [repo.source_connection_view(r) for r in db.q("SELECT * FROM source_connections ORDER BY name, id")]


@router.post("", status_code=201)
def create_connection(body: ConnectionIn):
    kind = body.kind or "m365_graph"
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "'name' is required")
    config, secrets = _validated(kind, body.config, {}, body.secrets)
    cid = db.ex("INSERT INTO source_connections(name, kind, config, secret_enc, notes, created_at) VALUES (?,?,?,?,?,?)",
                (name, kind, db.jdump(config), wiring.dump_secrets(secrets), body.notes or "", time.time())).lastrowid
    return repo.source_connection_view(_row(cid))


@router.get("/{cid:int}")
def get_connection(cid: int):
    return repo.source_connection_view(_row(cid))


@router.put("/{cid:int}")
def update_connection(cid: int, body: ConnectionIn):
    r = _row(cid)
    if body.kind and body.kind != r["kind"]:
        raise HTTPException(422, "the kind of a source connection cannot change")
    old_config = db.jload(r["config"], {}) or {}
    current = _stored(r["secret_enc"])
    config, secrets = _validated(r["kind"], old_config if body.config is None else body.config, current, body.secrets)
    if _linked(cid) and any(str(config.get(k) or "").lower() != str(old_config.get(k) or "").lower()
                            for k in IDENTITY_KEYS):
        raise HTTPException(409, "accounts are linked to this connection: the tenant and the cloud cannot change "
                                 "(create another connection)")
    sets: dict[str, Any] = {"config": db.jdump(config)}
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(422, "'name' is required")
        sets["name"] = body.name.strip()
    if body.notes is not None:
        sets["notes"] = body.notes
    if config != old_config or secrets != current:  # the last test no longer says anything about this connection
        sets.update(secret_enc=wiring.dump_secrets(secrets), last_test=None, last_test_result=None)
        _forget(cid)
    db.ex(f"UPDATE source_connections SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), cid))
    return repo.source_connection_view(_row(cid))


@router.delete("/{cid:int}")
def delete_connection(cid: int):
    _row(cid)
    n = _linked(cid)
    if n:
        raise HTTPException(409, {"msg": f"{n} account(s) are linked to this connection", "accounts": n})
    db.ex("DELETE FROM source_connections WHERE id=?", (cid,))
    _forget(cid)
    return {"ok": True}


@router.post("/test")
def test_unsaved(body: ProbeIn):
    """Tries a form without saving it; `id` fills the omitted secrets from that connection."""
    current: dict = {}
    if body.id is not None:
        r = _row(body.id)
        if r["kind"] == body.kind:
            current = _stored(r["secret_enc"])
    config, secrets = _validated(body.kind, body.config, current, body.secrets)
    return _check(body.kind, config, secrets)


@router.post("/{cid:int}/test")
def test_stored(cid: int):
    r = _row(cid)
    _spec(r["kind"])
    res = _check(r["kind"], db.jload(r["config"], {}) or {}, _stored(r["secret_enc"]))
    db.ex("UPDATE source_connections SET last_test=?, last_test_result=? WHERE id=?",
          (time.time(), db.jdump(res), cid))
    return res


@router.get("/{cid:int}/mailboxes")
def list_mailboxes(cid: int, refresh: int = 0):
    r = _row(cid)
    _spec(r["kind"])
    return _mailboxes(r, bool(refresh))


@router.get("/{cid:int}/match")
def match(cid: int, server_id: int | None = None, refresh: int = 0):
    """server_id omitted: the server whose accounts match the most mailboxes (ready or linked)."""
    r = _row(cid)
    _spec(r["kind"])
    listing = _mailboxes(r, bool(refresh))
    if server_id is not None:
        s = db.q1("SELECT * FROM servers WHERE id=?", (server_id,))
        if s is None:
            raise HTTPException(404, "unknown server")
        return _match(r, s, listing)
    best = None
    for s in db.q("SELECT * FROM servers ORDER BY name, id"):
        res = _match(r, s, listing)
        score = res["counts"].get("ready", 0) + res["counts"].get("linked", 0)
        if best is None or score > best[0]:
            best = (score, res)
    if best is None:
        raise HTTPException(422, "no server")
    return best[1]


def _link_one(r, spec, server_id: int, item: LinkItem, include_tasks: bool | None) -> str | None:
    """Links one account (None) or says why not. One transaction: the active job check and the update."""
    conn_config = db.jload(r["config"], {}) or {}
    tenant = str(conn_config.get("tenant_id") or "").lower()
    mailbox_names = {f.name for f in registry.split_fields(spec.source, "mailbox")}
    with db.tx() as c:
        a = c.execute("SELECT * FROM accounts WHERE id=?", (item.account_id,)).fetchone()
        if a is None or a["server_id"] != server_id:
            return "not a mailbox of this server"
        if c.execute(f"SELECT id FROM jobs WHERE account_id=? AND status IN {ACTIVE}", (a["id"],)).fetchone():
            return "a job is active for this mailbox"
        own = db.jload(a["source_config"], {}) or {}
        if a["source_connection_id"] == r["id"] and a["source_kind"] == r["kind"]:
            keep = own  # already linked: same mailbox value, same namespace (idempotent)
        elif (a["source_kind"] == r["kind"] and not a["source_connection_id"]
              and str(own.get("tenant_id") or "").lower() == tenant and tenant):
            keep = own  # configured per mailbox for this very tenant: its namespace and its items carry on
        elif not a["source_kind"] or (a["source_kind"] in (wiring.PST, wiring.FILES) and not _pst_valid(a)):
            keep = {}
        else:
            return "the mailbox has another source"
        mailbox = keep.get("mailbox") or item.mailbox_id
        tasks = include_tasks if include_tasks is not None else keep.get("include_tasks", _tasks_granted(r))
        given = {k: v for k, v in keep.items() if k in mailbox_names}
        given.update(mailbox=mailbox, include_tasks=bool(tasks))
        try:
            merged = registry.normalize_config(spec.source, {**given, **conn_config})
        except ConnectorError as e:
            return str(e)
        config = {k: v for k, v in merged.items() if k in mailbox_names}
        c.execute("UPDATE accounts SET source_kind=?, source_config=?, source_secret_enc=NULL, source_connection_id=? "
                  "WHERE id=?", (r["kind"], db.jdump(config), r["id"], a["id"]))
    return None


@router.post("/{cid:int}/link")
def link(cid: int, body: LinkIn):
    """Points each account at its tenant mailbox; with `start`, queues a job for each linked account, in one named
    batch, as POST /api/runs does. Per-account refusals are listed in `errors`, the others go on."""
    r = _row(cid)
    spec = _spec(r["kind"])
    if db.q1("SELECT id FROM servers WHERE id=?", (body.server_id,)) is None:
        raise HTTPException(404, "unknown server")
    linked, errors = [], []
    for item in body.links:
        msg = _link_one(r, spec, body.server_id, item, body.include_tasks)
        if msg:
            errors.append({"account_id": item.account_id, "msg": msg})
        else:
            linked.append(item.account_id)
    if errors and not linked:
        raise HTTPException(409, {"msg": "no mailbox linked", "errors": errors})
    jobs: list[dict] = []
    batch_id = None
    if body.start and linked:
        jobs, job_errors, batch_id = runs.create_jobs(linked, body.batch_name, body.mode)
        errors += job_errors
    return {"linked": linked, "errors": errors, "jobs": jobs, "batch_id": batch_id}


# ------------------------------------------------------------------ Connect with Microsoft (m365_provision)
def _enabled() -> None:
    if not mp.bootstrap_client_id():
        raise HTTPException(409, "Connect with Microsoft is turned off (M365_BOOTSTRAP_CLIENT_ID is empty)")


def _start(kind: str, cloud: str, **kw) -> dict:
    try:
        return mp.start(kind, cloud, **kw).started()
    except mp.ProvisionError as e:
        raise HTTPException(502, str(e)) from e


def _save_provisioned(flow: mp.Flow, config: dict, secret: str, provisioned: dict, check: dict) -> dict:
    """The connection made by a provisioning flow, saved as POST "" does, with its check stored as a Test."""
    config, secrets = _validated("m365_graph", config, {}, {"client_secret": secret})
    name = (flow.name or "Microsoft 365").strip()[:120]
    cid = db.ex("INSERT INTO source_connections(name, kind, config, secret_enc, notes, created_at, last_test, "
                "last_test_result, provisioned) VALUES (?,?,?,?,?,?,?,?,?)",
                (name, "m365_graph", db.jdump(config), wiring.dump_secrets(secrets), "", time.time(), time.time(),
                 db.jdump(check), db.jdump(provisioned))).lastrowid
    return repo.source_connection_view(_row(cid))


def _clear_provisioned(flow: mp.Flow) -> dict | None:
    if db.q1("SELECT id FROM source_connections WHERE id=?", (flow.connection_id,)) is None:
        return None  # deleted meanwhile
    db.ex("UPDATE source_connections SET provisioned=NULL WHERE id=?", (flow.connection_id,))
    return repo.source_connection_view(_row(flow.connection_id))


@router.post("/provision", status_code=201)
def provision(body: ProvisionIn):
    """Starts the device code flow that creates the Entra app in the client tenant; poll GET /provision/{flow_id}."""
    _enabled()
    if body.cloud not in mp.CLOUDS:
        raise HTTPException(422, f"unknown cloud '{body.cloud}'")
    return _start("provision", body.cloud, save=_save_provisioned, name=(body.name or "").strip())


@router.post("/{cid:int}/deprovision", status_code=201)
def deprovision(cid: int):
    """Starts the device code flow that deletes the application created by provisioning (only that one)."""
    r = _row(cid)
    prov = db.jload(r["provisioned"], None)
    if not prov or not prov.get("app_object_id"):
        raise HTTPException(409, "this connection was not created by Connect with Microsoft")
    _enabled()
    config = db.jload(r["config"], {}) or {}
    return _start("deprovision", config.get("cloud") or "global", clear=_clear_provisioned, connection_id=cid,
                  app_object_id=str(prov["app_object_id"]), tenant_id=str(config.get("tenant_id") or ""))


@router.get("/provision/{flow_id}")
def provision_status(flow_id: str):
    f = mp.get_flow(flow_id)
    if f is None:
        raise HTTPException(404, "unknown flow")
    return f.view()


@router.delete("/provision/{flow_id}", status_code=204)
def provision_cancel(flow_id: str):
    mp.cancel_flow(flow_id)
    return Response(status_code=204)
