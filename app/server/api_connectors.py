"""Connector API: the catalogue, the source and own target of an account, connection tests. See docs/api.md.
Secrets go in and never come out: views only carry has_secret."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..connectors import m365_provision, registry, sieve, wiring
from ..connectors.base import CAP_RULES, ConnectorError
from . import db, planning, repo
from .security import decrypt

router = APIRouter(prefix="/api")
ACTIVE = "('queued','running','paused','stopping')"


class EndpointIn(BaseModel):
    kind: str | None = None
    config: dict[str, Any] | None = None
    # omitted: kept. {}: cleared. {name: value}: that value replaced, the others kept.
    secrets: dict[str, str] | None = None
    # PUT /accounts/{id}/source only: read through this source connection (config: the mailbox fields only).
    # Omitted: the account's own settings, any link is dropped.
    connection_id: int | None = None


class ProbeIn(BaseModel):
    role: Literal["source", "target"] = "source"
    config: dict[str, Any] | None = None
    secrets: dict[str, str] | None = None
    account_id: int | None = None  # secrets omitted: those stored for this kind on this account


class ManageSieveIn(BaseModel):
    host: str | None = None        # default: the IMAP host of the account's server
    port: int = 4190
    security: Literal["starttls", "ssl", "none"] = "starttls"
    verify_tls: bool = True
    login: str | None = None       # default: those of the account
    password: str | None = None
    script_name: str = sieve.DEFAULT_SCRIPT
    activate: bool = False         # replaces the active script of the user


class RulesIn(BaseModel):
    delimiter: str = "/"
    root_folder: str = ""
    trash_folder: str = "Trash"
    managesieve: ManageSieveIn | None = None


def _account(aid: int):
    r = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
    if r is None:
        raise HTTPException(404, "unknown mailbox")
    return r


def _idle(aid: int) -> None:
    if db.q1(f"SELECT id FROM jobs WHERE account_id=? AND status IN {ACTIVE}", (aid,)):
        raise HTTPException(409, "a job is active for this mailbox")


def _validated(kind: str, role_name: str, config: dict | None, current: dict, secrets: dict | None) -> tuple[dict, dict]:
    try:
        role = registry.role_of(kind, role_name)
        return registry.normalize_config(role, config), registry.merge_secrets(role, current, secrets)
    except ConnectorError as e:
        raise HTTPException(422, str(e)) from e


def _stored(token: str | None) -> dict:
    try:
        return wiring.load_secrets(token)
    except ConnectorError:
        return {}  # unreadable (key changed): as if absent, the new value replaces it


@router.get("/connectors")
def list_connectors():
    out = registry.catalogue()
    for c in out:
        if c["kind"] == "m365_graph":  # "Connect with Microsoft" available (bootstrap app configured)
            c["provision"] = bool(m365_provision.bootstrap_client_id())
    return out


@router.get("/accounts/{aid:int}/source")
def get_source(aid: int):
    return repo.source_view(_account(aid))


@router.put("/accounts/{aid:int}/source")
def put_source(aid: int, body: EndpointIn):
    """The state of each source is kept apart (items.pst_key): pointing elsewhere and back resumes without
    duplicates."""
    r = _account(aid)
    _idle(aid)
    if body.connection_id is not None:
        _link_source(r, body)
        return repo.get_account(aid)
    kind = body.kind or wiring.PST
    if kind == wiring.PST:
        pst_id = (body.config or {}).get("pst_id", r["pst_id"])
        if pst_id is not None and (not isinstance(pst_id, int) or not repo.get_pst(pst_id)):
            raise HTTPException(404, "unknown PST")
        db.ex("UPDATE accounts SET source_kind=NULL, source_config=NULL, source_secret_enc=NULL, "
              "source_connection_id=NULL, pst_id=? WHERE id=?", (pst_id, aid))
        wiring.sync_files_source(aid)  # an uploaded mbox, Maildir, .eml... is read by the `files` connector
    else:
        current = _stored(r["source_secret_enc"]) if r["source_kind"] == kind else {}
        config, secrets = _validated(kind, "source", body.config, current, body.secrets)
        db.ex("UPDATE accounts SET source_kind=?, source_config=?, source_secret_enc=?, source_connection_id=NULL "
              "WHERE id=?", (kind, db.jdump(config), wiring.dump_secrets(secrets), aid))
    return repo.get_account(aid)


def _link_source(r, body: EndpointIn) -> None:
    """The account's source read through a source connection: the merged config is validated, only the
    mailbox-scoped fields are stored on the account, its own secrets are dropped (those of the connection serve)."""
    conn = db.q1("SELECT * FROM source_connections WHERE id=?", (body.connection_id,))
    if conn is None:
        raise HTTPException(404, "unknown source connection")
    kind = body.kind or conn["kind"]
    if kind != conn["kind"]:
        raise HTTPException(422, f"this source connection is for '{conn['kind']}'")
    if body.secrets:
        raise HTTPException(422, "the secrets of a linked source are those of its connection")
    try:
        role = registry.role_of(kind, "source")
    except ConnectorError as e:
        raise HTTPException(422, str(e)) from e
    mailbox = {f.name for f in registry.split_fields(role, "mailbox")}
    given = dict(body.config or {})
    extra = sorted(k for k in given if k not in mailbox)
    if extra:
        raise HTTPException(422, f"'{extra[0]}' is not a mailbox setting: it comes from the source connection")
    try:
        merged = registry.normalize_config(role, {**given, **(db.jload(conn["config"], {}) or {})})
    except ConnectorError as e:
        raise HTTPException(422, str(e)) from e
    config = {k: v for k, v in merged.items() if k in mailbox}
    db.ex("UPDATE accounts SET source_kind=?, source_config=?, source_secret_enc=NULL, source_connection_id=? "
          "WHERE id=?", (kind, db.jdump(config), conn["id"], r["id"]))


@router.get("/accounts/{aid:int}/target")
def get_target(aid: int):
    return repo.target_view(_account(aid))


@router.put("/accounts/{aid:int}/target")
def put_target(aid: int, body: EndpointIn):
    """Own target of the account (kind null: back to its server). Another mailbox means everything must be sent
    again, as when the server of an account changes."""
    r = _account(aid)
    _idle(aid)
    old = (r["target_kind"], db.jload(r["target_config"], None))
    if not body.kind:
        db.ex("UPDATE accounts SET target_kind=NULL, target_config=NULL, target_secret_enc=NULL WHERE id=?", (aid,))
        new = (None, None)
    else:
        current = _stored(r["target_secret_enc"]) if r["target_kind"] == body.kind else {}
        config, secrets = _validated(body.kind, "target", body.config, current, body.secrets)
        db.ex("UPDATE accounts SET target_kind=?, target_config=?, target_secret_enc=? WHERE id=?",
              (body.kind, db.jdump(config), wiring.dump_secrets(secrets), aid))
        new = (body.kind, config)
    if old != new:
        db.ex("DELETE FROM items WHERE account_id=?", (aid,))
        planning.forget_cursors(aid)
    return repo.get_account(aid)


@router.post("/connectors/{kind}/test")
def test_connector(kind: str, body: ProbeIn):
    """Tries a config without saving it. A refused connection is an answer, not an HTTP error:
    {ok: false, error_kind, msg}."""
    if registry.get(kind) is None:
        raise HTTPException(404, "unknown connector")
    current: dict = {}
    if body.account_id is not None:
        r = _account(body.account_id)
        col = "source" if body.role == "source" else "target"
        if r[f"{col}_kind"] == kind:
            current = _stored(r[f"{col}_secret_enc"])
    config, secrets = _validated(kind, body.role, body.config, current, body.secrets)
    try:
        if body.role == "source":
            with registry.make_source(kind, config, secrets) as src:
                folders = src.folders()
            return {"ok": True, "msg": f"{len(folders)} folder(s) found",
                    "folders": [{"path": f.path, "kind": f.kind, "special": f.special, "count": f.count}
                                for f in folders[:1000]]}
        session = registry.make_target(kind, config, secrets).session()
        try:
            session.connect()
            return {"ok": True, "msg": "connection successful", "quota": session.quota(), **session.info()}
        finally:
            session.close()
    except ConnectorError as e:
        return {"ok": False, "error_kind": e.kind, "msg": str(e)}


@router.post("/accounts/{aid:int}/rules")
def convert_rules(aid: int, body: RulesIn):
    """Inbox rules of the account's source as a Sieve script, uploaded if `managesieve` is given. Best effort: see
    docs/exchange.md. A refused connection is an answer, not an HTTP error: {ok: false, error_kind, msg}."""
    r = _account(aid)
    kind = wiring.source_kind(r)
    spec = registry.get(kind)
    if spec is None or spec.source is None or CAP_RULES not in spec.source.capabilities:
        raise HTTPException(422, f"source '{kind}' cannot read mail rules")
    try:
        with wiring.source_of_account(r) as src:
            rules = src.read_rules()
    except ConnectorError as e:
        return {"ok": False, "error_kind": e.kind, "msg": str(e)}
    out = sieve.convert(rules, body.delimiter, body.root_folder, body.trash_folder)
    out.update(ok=True, uploaded=False, activated=False, upload_error=None)
    ms = body.managesieve
    if ms is None:
        return out
    srv = db.q1("SELECT imap_host FROM servers WHERE id=?", (r["server_id"],))
    host = ms.host or (srv["imap_host"] if srv else "")
    login = ms.login or r["login"] or r["email"]
    password = ms.password
    if password is None and r["password_enc"]:
        try:
            password = decrypt(r["password_enc"])
        except ValueError as e:
            out["upload_error"] = {"error_kind": ConnectorError.CONFIG, "msg": str(e)}
            return out
    if not host or not password:
        out["upload_error"] = {"error_kind": ConnectorError.CONFIG, "msg": "ManageSieve: no host or no password"}
        return out
    try:
        sieve.upload(out["script"], host, login, password, ms.port, ms.security, ms.verify_tls, ms.script_name, ms.activate)
    except ConnectorError as e:
        out["upload_error"] = {"error_kind": e.kind, "msg": str(e)}
        return out
    out.update(uploaded=True, activated=ms.activate)
    return out
