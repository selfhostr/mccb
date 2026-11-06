"""From an `accounts` row to its source and targets. An account without source_kind is a PST account, one without
target_kind writes to its server (IMAP + DAV) with the account's credentials."""
from __future__ import annotations

import json

from ..server import db
from ..server.security import decrypt, encrypt
from . import registry
from .base import CALENDAR, CONTACTS, MAIL, ConnectorError, Source, Target

PST = "pst"


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


def source_config(acc) -> dict:
    if source_kind(acc) == PST:
        return {"pst_id": acc["pst_id"]}
    return db.jload(acc["source_config"], {}) or {}


def source_of_account(acc, ctx: dict | None = None) -> Source:
    kind = source_kind(acc)
    return registry.make_source(kind, source_config(acc), load_secrets(acc["source_secret_enc"]), ctx)


def items_key_of_account(acc) -> str | None:
    """Value of items.pst_key for the current source of the account (None: nothing to attach items to yet)."""
    if source_kind(acc) == PST:
        if not acc["pst_id"]:
            return None
        return db.pst_key_of(db.q1("SELECT id, pst_sha FROM psts WHERE id=?", (acc["pst_id"],)))
    try:
        return registry.namespace(source_kind(acc), "source", source_config(acc))
    except ConnectorError:
        return None  # connector no longer known: see source_problem


def source_problem(acc) -> str | None:
    """Why a job cannot start for this account (None: it can)."""
    kind = source_kind(acc)
    if kind == PST:
        p = db.q1("SELECT status FROM psts WHERE id=?", (acc["pst_id"],)) if acc["pst_id"] else None
        return None if p is not None and p["status"] == "valid" else "no valid PST linked"
    spec = registry.get(kind)
    if spec is None or spec.source is None:
        return f"unknown source connector '{kind}'"
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
    kind = acc["target_kind"]
    if kind:
        role = registry.role_of(kind, "target")
        own = role.factory(db.jload(acc["target_config"], {}) or {}, load_secrets(acc["target_secret_enc"]), {})
        for item in role.items:
            out[item] = own
    return out
