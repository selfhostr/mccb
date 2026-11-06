"""CSV import and export of accounts (the parsing is in csvimport.py).

Import: `email;password[;login]` as ever; with a header it may also carry the PST file name, the content toggles and
the settings of a source connector (`source_kind`, `source_<field>`). Export: the same columns, never a password
or any secret (`source_<secret field>` is left out)."""
from __future__ import annotations

import csv
import io
import os
import time

from ..connectors import registry, wiring
from ..connectors.base import ConnectorError
from . import csvimport, db
from .security import encrypt

ACTIVE = "('queued','running','paused','stopping')"


def _find_pst(name: str):
    base = os.path.basename(name.replace("\\", "/")).strip().lower()
    rows = db.q("SELECT * FROM psts ORDER BY (status='valid') DESC, id DESC")
    for r in rows:
        if r["filename"].lower() == base:
            return r
    return None


def _flag(v):
    """A cell is text: 1/true/yes/oui become True, 0/false/no/non False, anything else is left to the validation."""
    t = str(v).strip().lower()
    return True if t in csvimport.TRUE_WORDS else False if t in csvimport.FALSE_WORDS else v


def _split_source(kind: str, cfg: dict, current: dict) -> tuple[dict, dict | None]:
    """(normalized config, secrets to store or None to keep) of a `source_*` group. Raises ConnectorError."""
    role = registry.role_of(kind, "source")
    names = {f.name for f in role.fields if f.secret}
    flags = {f.name for f in role.fields if f.type == "boolean"}
    plain = {k: (_flag(v) if k in flags else v) for k, v in cfg.items() if k not in names}
    given = {k: v for k, v in cfg.items() if k in names}
    return registry.normalize_config(role, plain), registry.merge_secrets(role, current, given or None)


def apply(server_id: int, rows: list[dict], errors: list[dict]) -> dict:
    created = updated = 0
    warnings: list[dict] = []
    for row in rows:
        line = row.get("line")
        ex = db.q1("SELECT * FROM accounts WHERE server_id=? AND email=?", (server_id, row["email"]))
        if not row["password"] and ex is None:
            errors.append({"line": line, "msg": f"missing password for {row['email']}"})
            continue
        sets: dict[str, object] = {}
        pst = None
        if row.get("pst"):
            pst = _find_pst(row["pst"])
            if pst is None:
                warnings.append({"line": line, "msg": f"PST {row['pst']!r} is not on the server: not linked"})
        for t, v in (row.get("options") or {}).items():
            sets[f"opt_{t}"] = int(v)
        if row.get("source"):
            src = row["source"]
            try:
                if src["kind"] == wiring.PST:
                    sets.update(source_kind=None, source_config=None, source_secret_enc=None)
                else:
                    current = wiring.load_secrets(ex["source_secret_enc"]) if ex is not None and \
                        ex["source_kind"] == src["kind"] else {}
                    config, secrets = _split_source(src["kind"], src["config"], current)
                    sets.update(source_kind=src["kind"], source_config=db.jdump(config),
                                source_secret_enc=wiring.dump_secrets(secrets))
            except ConnectorError as e:
                errors.append({"line": line, "msg": f"source: {e}"})
                continue
        if pst is not None:
            sets["pst_id"] = pst["id"]
        if ex is not None and (pst is not None or row.get("source")) and db.q1(
                f"SELECT id FROM jobs WHERE account_id=? AND status IN {ACTIVE}", (ex["id"],)):
            errors.append({"line": line, "msg": f"a job is active for {row['email']}: source not changed"})
            continue
        if ex is not None:
            if row["password"]:
                sets.update(password_enc=encrypt(row["password"]), status="new")
            if row["login"] or row["password"]:
                sets["login"] = row["login"]
            updated += 1
            aid = ex["id"]
        else:
            cur = db.ex("INSERT INTO accounts(server_id,email,login,password_enc,created_at) VALUES (?,?,?,?,?)",
                        (server_id, row["email"], row["login"], encrypt(row["password"]), time.time()))
            aid = cur.lastrowid
            created += 1
        if sets:
            db.ex(f"UPDATE accounts SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), aid))
    out = {"created": created, "updated": updated, "errors": errors}
    if warnings:  # only when there is something to say: the answer to an old three-column file is unchanged
        out["warnings"] = warnings
    return out


def _cell(v) -> str:
    return "" if v is None else ("true" if v is True else "false" if v is False else str(v))


def export(server_id: int | None = None) -> str:
    """One line per account. Source settings of the connectors in use get a column each, secrets excluded."""
    rows = db.q("SELECT a.*, s.name AS server_name, p.filename AS pst_name FROM accounts a "
                "JOIN servers s ON s.id=a.server_id LEFT JOIN psts p ON p.id=a.pst_id "
                + ("WHERE a.server_id=? " if server_id else "") + "ORDER BY s.name, a.email",
                (server_id,) if server_id else ())
    fields: list[str] = []
    for r in rows:
        if r["source_kind"]:
            try:
                for f in registry.role_of(r["source_kind"], "source").fields:
                    if not f.secret and f.name not in fields:
                        fields.append(f.name)
            except ConnectorError:
                pass
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["email", "login", "server", "pst", "mail", "calendar", "contacts", "source_kind",
                *[f"source_{f}" for f in fields]])
    for r in rows:
        cfg = db.jload(r["source_config"], {}) or {}
        w.writerow([r["email"], r["login"] or "", r["server_name"], r["pst_name"] or "", int(r["opt_mail"]),
                    int(r["opt_calendar"]), int(r["opt_contacts"]), r["source_kind"] or "",
                    *[_cell(cfg.get(f)) for f in fields]])
    return buf.getvalue()
