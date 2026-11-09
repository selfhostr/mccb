"""From an `accounts` row to its source and targets. An account without source_kind is a PST account, one without
target_kind writes to its server (IMAP + DAV) with the account's credentials. An account with source_connection_id
takes the connection-scoped fields and the secrets of its source from that `source_connections` row."""
from __future__ import annotations

import json

from ..server import db
from ..server.security import decrypt, encrypt
from . import registry
from .base import CALENDAR, CONTACTS, MAIL, TASKS, ConnectorError, Source, Target

PST = "pst"
FILES = "files"   # an uploaded file that is not a PST: mbox, Maildir, .eml, .msg, .ics, .vcf, .zip


def source_kind(acc) -> str:
    return acc["source_kind"] or PST


def uses_connector_route(acc) -> bool:
    """False: the historical JobRunner path (PST to the server's IMAP and DAV, with builder processes)."""
    return source_kind(acc) != PST or bool(acc["target_kind"])


def load_secrets(token: str | None) -> dict:
    if not token:
        return {}
    try:
        out = json.loads(decrypt(token))
    except ValueError as e:  # includes a key that changed (see security.decrypt)
        raise ConnectorError(f"stored secret unreadable: {e}", ConnectorError.CONFIG) from e
    return out if isinstance(out, dict) else {}


def dump_secrets(secrets: dict) -> str | None:
    return encrypt(json.dumps(secrets)) if secrets else None


def _connection_id(acc) -> int | None:
    if source_kind(acc) in (PST, FILES) or "source_connection_id" not in acc.keys():
        return None
    return acc["source_connection_id"]


def connection_of(acc):
    """The source_connections row the account's source is linked to (None: not linked, or the row is gone)."""
    cid = _connection_id(acc)
    return db.q1("SELECT * FROM source_connections WHERE id=?", (cid,)) if cid is not None else None


def source_config(acc) -> dict:
    if source_kind(acc) == PST:
        return {"pst_id": acc["pst_id"]}
    own = db.jload(acc["source_config"], {}) or {}
    if _connection_id(acc) is None:
        return own
    conn = connection_of(acc)
    spec = registry.get(source_kind(acc))
    if spec is not None and spec.source is not None:  # connection-scoped keys never come from the account
        shared = {f.name for f in registry.split_fields(spec.source, "connection")}
        own = {k: v for k, v in own.items() if k not in shared}
    shared_config = (db.jload(conn["config"], {}) or {}) if conn is not None else {}
    return {**own, **shared_config}


def source_secrets(acc) -> dict:
    """Secrets of the account's source: those of its connection when linked (none if the row is gone)."""
    if _connection_id(acc) is None:
        return load_secrets(acc["source_secret_enc"])
    conn = connection_of(acc)
    return load_secrets(conn["secret_enc"]) if conn is not None else {}


def source_of_account(acc, ctx: dict | None = None) -> Source:
    kind = source_kind(acc)
    return registry.make_source(kind, source_config(acc), source_secrets(acc), ctx)


def sync_files_source(aid: int) -> None:
    """Keeps `source_kind` right after the file linked to an account changed. A PST keeps the historical empty
    source_kind; any other uploaded file (see connectors/files.py) is read by the `files` connector. An account that
    picked another connector (imap, jmap...) is left alone."""
    acc = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
    if acc is None or acc["source_kind"] not in (None, "", FILES):
        return
    pid = acc["pst_id"]
    row = db.q1("SELECT scan FROM psts WHERE id=?", (pid,)) if pid else None
    scan = db.jload(row["scan"], None) if row is not None else None
    if isinstance(scan, dict) and scan.get("format") == FILES:
        db.ex("UPDATE accounts SET source_kind=?, source_config=?, source_secret_enc=NULL WHERE id=?",
              (FILES, db.jdump({"pst_id": pid}), aid))
    elif acc["source_kind"] == FILES:
        db.ex("UPDATE accounts SET source_kind=NULL, source_config=NULL, source_secret_enc=NULL WHERE id=?", (aid,))


def sync_files_sources_of(pst_id: int) -> None:
    """After a file was analysed: the accounts already linked to it."""
    for r in db.q("SELECT id FROM accounts WHERE pst_id=?", (pst_id,)):
        sync_files_source(r["id"])


def items_key_of_account(acc) -> str | None:
    """Value of items.pst_key for the current source of the account (None: nothing to attach items to yet)."""
    if source_kind(acc) in (PST, FILES):
        pid = acc["pst_id"] if source_kind(acc) == PST else source_config(acc).get("pst_id")
        if not pid:
            return None
        return db.pst_key_of(db.q1("SELECT id, pst_sha FROM psts WHERE id=?", (pid,)))
    if _connection_id(acc) is not None and connection_of(acc) is None:
        return None  # see source_problem: without the connection the namespace is unknown
    try:
        return registry.namespace(source_kind(acc), "source", source_config(acc))
    except ConnectorError:
        return None  # connector no longer known: see source_problem


def source_problem(acc) -> str | None:
    """Why a job cannot start for this account (None: it can)."""
    kind = source_kind(acc)
    if kind in (PST, FILES):
        pid = acc["pst_id"] if kind == PST else source_config(acc).get("pst_id")
        p = db.q1("SELECT status FROM psts WHERE id=?", (pid,)) if pid else None
        return None if p is not None and p["status"] == "valid" else f"no valid {'PST' if kind == PST else 'file'} linked"
    spec = registry.get(kind)
    if spec is None or spec.source is None:
        return f"unknown source connector '{kind}'"
    if _connection_id(acc) is not None and connection_of(acc) is None:
        return "source connection missing"
    return None


def targets_of_account(acc, srv) -> dict[str, Target]:
    """{item kind: target}. The account's own target wins for the kinds it serves, the server covers the others."""
    out: dict[str, Target] = {}
    if acc["password_enc"]:
        try:
            pw = decrypt(acc["password_enc"])
        except ValueError as e:
            raise ConnectorError(str(e), ConnectorError.CONFIG) from e
        login = acc["login"] or acc["email"]
        secrets = {"password": pw}
        verify = bool(srv["verify_tls"])
        out[MAIL] = registry.make_target("imap", {
            "host": srv["imap_host"], "port": srv["imap_port"], "security": srv["imap_security"], "verify_tls": verify,
            "login": login, "root_folder": srv["root_folder"]}, secrets)
        if srv["dav_enabled"]:
            for item, kind, col in ((CALENDAR, "caldav", "caldav_url"), (CONTACTS, "carddav", "carddav_url")):
                out[item] = registry.make_target(kind, {
                    "url": srv[col], "login": login, "email": acc["email"], "verify_tls": verify,
                    "discovery_host": srv["imap_host"]}, secrets)
            out[TASKS] = out[CALENDAR].for_item(TASKS)
    kind = acc["target_kind"]
    if kind:
        role = registry.role_of(kind, "target")
        own = role.factory(db.jload(acc["target_config"], {}) or {}, load_secrets(acc["target_secret_enc"]), {})
        for item in role.items:
            pick = getattr(own, "for_item", None)  # DAV: tasks do not go to the same collection as events
            out[item] = pick(item) if pick is not None else own
    return out
